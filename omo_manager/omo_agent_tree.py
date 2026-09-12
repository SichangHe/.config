#!/usr/bin/env python3
"""Print a validated manager-agent hierarchy from task frontmatter."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_external_task_register import RegistrationError
from omo_manager.omo_external_task_register import active_receipts
from omo_manager.omo_external_task_register import assert_plan_current
from omo_manager.omo_external_task_register import default_registry_dir
from omo_manager.omo_external_task_register import plan_from_receipt
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import TaskMetadata
from omo_manager.omo_task_metadata import canonical_target
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_metadata import runat_kind
from omo_manager.omo_agent_status import TaskLine
from omo_manager.omo_agent_status import parse_task_text


DEFAULT_STATUSES = ("running", "long_running")
STATUS_ORDER = ("running", "long_running", "blocked", "done")
LOCAL_ENV_KEYS = ("OMO_WORK_LOGS_ROOT", "OMO_MANAGER_TMUX_TARGET")
ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?(?P<key>OMO_WORK_LOGS_ROOT|OMO_MANAGER_TMUX_TARGET)\s*=", re.MULTILINE)
TMUX_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
MAX_FRONTMATTER_LINES = 100_000
TASK_KEYS = {"version", "status", "runat", "tool", "managerat", "is_manager", "pending_task_items"}
EXIT_OK = 0
EXIT_CLI = 2
EXIT_MISSING_ROOT = 3
EXIT_CONFIGURATION = 4
EXIT_INVALID_STATE = 5


class TreeError(ValueError):
    """A deterministic configuration, record, or hierarchy failure."""

    exit_code = EXIT_INVALID_STATE


class MissingRootError(TreeError):
    """An explicitly requested filesystem or agent root does not exist."""

    exit_code = EXIT_MISSING_ROOT


class ConfigurationError(TreeError):
    """Required hierarchy configuration cannot be read or resolved."""

    exit_code = EXIT_CONFIGURATION


@dataclass(frozen=True)
class Config:
    root: Path
    main_manager: str


@dataclass(frozen=True)
class TaskRecord:
    path: str
    purpose: str
    status: str
    runat: str
    managerat: str
    is_manager: bool
    pending_items: tuple[str, ...]
    blocked_on: str
    todo_membership: str | None
    external: bool = False


@dataclass
class Agent:
    target: str
    manager: str | None
    is_manager: bool
    tasks: list[TaskRecord] = field(default_factory=list)
    children: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Args:
    root: Path | None
    agent: str | None
    full_tree: bool
    main_manager: str | None
    depth: int | None
    statuses: tuple[str, ...]
    json: bool


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("depth must be a nonnegative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("depth must be a nonnegative integer")
    return parsed


def agent_target(value: str) -> str:
    if runat_kind(value) not in {"tmux", "omnigent"}:
        raise argparse.ArgumentTypeError("agent must be a tmux target or omnigent:// session identifier")
    return canonical_target(value)


def main_manager_target(value: str) -> str:
    if runat_kind(value) != "tmux":
        raise argparse.ArgumentTypeError("main manager must be a tmux target")
    return canonical_target(value)


# 🧑 “make the command work like the tree command but with depth configurable ... by default print out the subtree of agents under it ... flags to control everything.”
def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="omo_agent_tree.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=dedent(
            """
            Print the manager-agent reporting tree from authoritative task metadata.

            The command is read-only. It derives each edge from canonical
            `managerat -> runat` task frontmatter, never from visible pane text.
            It validates the complete selected hierarchy before printing any part.
            """
        ).strip(),
        epilog=dedent(
            """
            Defaults and root selection
              The work-log root comes from --root, then OMO_WORK_LOGS_ROOT in the
              process environment, then the selected local.env, then ~/work_logs.
              The main manager comes from --main-manager, then
              OMO_MANAGER_TMUX_TARGET in the process environment, then local.env.
              The local file is OMO_MANAGER_LOCAL_ENV or
              ~/.config/omo_manager/local.env. A missing or ambiguous main-manager
              value and an unreadable or unevaluable needed local.env are errors.

              With no root-selection flag, a command run inside the current tmux pane whose
              canonical target belongs to a selected agent prints that agent's
              subtree. Otherwise it prints from the configured main manager.
              --full-tree always selects the main manager. --agent selects any
              represented tmux or omnigent:// agent. The main manager is never
              inferred from pane state.

            Records and validation
              The default statuses are running and long_running. --status is
              repeatable. --all-statuses selects running, long_running, blocked,
              and done. `runat: retired` is always excluded.

              The command scans Markdown task records under the root and validates
              registered external records through their receipt-bound source
              TODO.md. Every selected in-root running or long_running record must
              have exactly one matching root TODO.md row. A selected blocked or
              done record may be unindexed, but any row present must be unique and
              match its canonical runat. External records use their validated
              source TODO.md row and need no duplicate root row.

              Each canonical runat has at most one selected running or long_running
              record. Historical blocked or done records may group only when their
              canonical manager and declared role agree. The command rejects
              malformed selected or indexed records, duplicate active owners or
              index rows, mismatched index rows, self-parenting, missing parents,
              cycles, and agents unreachable from the configured main manager. Any
              metadata-recorded agent may have children; its displayed manager or
              worker role remains the role declared by its records.

            Output and depth
              Text output lists every agent before its descendants. Each agent
              shows its canonical target and role. Each task shows its path and
              purpose from the first prose paragraph of its first manager_delegation
              block, or its first human_instruction block when no manager delegation
              exists. It also shows status, every pending item, `no open work
              recorded` when empty, and blocked_on when applicable. Siblings sort
              lexicographically.

              -L N and --depth N limit reporting edges below the selected root.
              Depth 0 prints only that root. Omitting depth prints all descendants.
              JSON contains selected_root, main_manager, statuses, depth, and a
              nested tree. Each agent includes current_work. Each task includes
              path, purpose, status, pending_items, blocked_on, todo_membership,
              and external.

            Exit status
              0  hierarchy printed successfully
              2  malformed command line, invalid option value, or conflicting flag
              3  explicitly requested work-log or agent root does not exist
              4  required root or main-manager configuration is missing, unreadable,
                 ambiguous, invalid, or cannot be evaluated
              5  task, index, receipt, or hierarchy state is invalid

            Examples
              omo_agent_tree.py --depth 1
              omo_agent_tree.py --agent config:27 --depth 2
              omo_agent_tree.py --full-tree
              omo_agent_tree.py --status running --status long_running --status blocked
              omo_agent_tree.py --all-statuses --json
            """
        ).strip(),
    )
    command.add_argument("--root", type=Path, help="work-log root override")
    roots = command.add_mutually_exclusive_group()
    roots.add_argument("--agent", type=agent_target, help="explicit subtree root")
    roots.add_argument("--full-tree", action="store_true", help="force the configured main-manager root")
    command.add_argument("--main-manager", type=main_manager_target, help="configured main-manager override")
    command.add_argument("-L", "--depth", type=nonnegative_int, help="maximum reporting edges below the selected root")
    statuses = command.add_mutually_exclusive_group()
    statuses.add_argument("--status", action="append", choices=STATUS_ORDER, help="selected task status; repeat for several")
    statuses.add_argument("--all-statuses", action="store_true", help="select running, long_running, blocked, and done")
    command.add_argument("--json", action="store_true", help="emit the validated nested result as JSON")
    return command


def parse_args(argv: list[str]) -> Args:
    parsed = parser().parse_args(argv)
    statuses = STATUS_ORDER if parsed.all_statuses else tuple(dict.fromkeys(parsed.status or DEFAULT_STATUSES))
    return Args(parsed.root, parsed.agent, parsed.full_tree, parsed.main_manager, parsed.depth, statuses, parsed.json)


def needed_local_values(root: Path | None, main_manager: str | None) -> tuple[dict[str, str], Path | None]:
    needed = {
        key
        for key, explicit in (("OMO_WORK_LOGS_ROOT", root), ("OMO_MANAGER_TMUX_TARGET", main_manager))
        if explicit is None and key not in os.environ
    }
    if not needed:
        return {}, None
    configured_path = os.environ.get("OMO_MANAGER_LOCAL_ENV")
    path = Path(configured_path).expanduser() if configured_path else Path.home() / ".config" / "omo_manager" / "local.env"
    if not path.exists():
        if configured_path:
            raise ConfigurationError(f"configured local environment does not exist: {path}")
        return {}, path
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ConfigurationError(f"configured local environment is unreadable: {path}")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"configured local environment is unreadable: {path}") from exc
    assignments: dict[str, int] = {key: 0 for key in LOCAL_ENV_KEYS}
    for match in ASSIGNMENT_RE.finditer(source):
        assignments[match.group("key")] += 1
    ambiguous = sorted(key for key in needed if assignments[key] > 1)
    if ambiguous:
        raise ConfigurationError(f"configured local environment defines {ambiguous[0]} more than once: {path}")
    try:
        loaded = subprocess.run(
            ["bash", "-c", 'set -a; source "$1"; env -0', "bash", str(path)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigurationError(f"configured local environment cannot be evaluated: {path}") from exc
    if loaded.returncode != 0:
        raise ConfigurationError(f"configured local environment cannot be evaluated: {path}")
    values: dict[str, str] = {}
    for item in loaded.stdout.split(b"\0"):
        if b"=" not in item:
            continue
        raw_key, raw_value = item.split(b"=", 1)
        try:
            key = raw_key.decode()
            value = raw_value.decode()
        except UnicodeError as exc:
            raise ConfigurationError(f"configured local environment is not UTF-8: {path}") from exc
        if key in needed:
            values[key] = value
    return values, path


def configuration(args: Args) -> Config:
    local, _path = needed_local_values(args.root, args.main_manager)
    root_value = str(args.root) if args.root is not None else os.environ.get("OMO_WORK_LOGS_ROOT", local.get("OMO_WORK_LOGS_ROOT", ""))
    root = Path(root_value).expanduser() if root_value else Path.home() / "work_logs"
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        error = MissingRootError if args.root is not None else ConfigurationError
        raise error(f"work-log root does not exist: {root}") from exc
    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        raise ConfigurationError(f"work-log root is unreadable or not a directory: {root}")
    manager = args.main_manager or os.environ.get("OMO_MANAGER_TMUX_TARGET", local.get("OMO_MANAGER_TMUX_TARGET", ""))
    if not manager:
        raise ConfigurationError("OMO_MANAGER_TMUX_TARGET is not configured")
    if runat_kind(manager) != "tmux":
        raise ConfigurationError(f"configured main manager is not a tmux target: {manager}")
    return Config(root, canonical_target(manager))


def frontmatter_only(path: Path, *, indexed: bool) -> str | None:
    try:
        with path.open(encoding="utf-8", newline="") as source:
            first = source.readline()
            if first.rstrip("\r\n") != "---":
                if indexed:
                    raise TreeError(f"indexed task has no task frontmatter: {path}")
                return None
            lines: list[str] = []
            for _index in range(MAX_FRONTMATTER_LINES):
                line = source.readline()
                if not line:
                    keys = {item.partition(":")[0] for item in lines if ":" in item}
                    if indexed or keys & TASK_KEYS:
                        raise TreeError(f"task frontmatter has no closing marker: {path}")
                    return None
                if line.rstrip("\r\n").strip() == "---":
                    break
                lines.append(line.rstrip("\r\n"))
            else:
                raise TreeError(f"task frontmatter is too long: {path}")
    except (OSError, UnicodeError) as exc:
        raise TreeError(f"cannot read Markdown record: {path}") from exc
    keys = {item.partition(":")[0] for item in lines if ":" in item}
    if not keys & TASK_KEYS:
        if indexed:
            raise TreeError(f"indexed task has no task frontmatter: {path}")
        return None
    return "---\n" + "\n".join(lines) + "\n---\n"


def assignment_paragraph(path: Path, tag: str) -> str | None:
    opening = f"<{tag}"
    closing = f"</{tag}>"
    inside = False
    paragraph: list[str] = []
    try:
        with path.open(encoding="utf-8") as source:
            for line in source:
                stripped = line.strip()
                if not inside:
                    if stripped.startswith(opening):
                        inside = True
                    continue
                if stripped.startswith(closing):
                    break
                boundary = not stripped or stripped.startswith(("<", ">", "#", "- ", "* ", "Subject:"))
                if paragraph and boundary:
                    break
                if not paragraph and boundary:
                    continue
                paragraph.append(stripped)
    except (OSError, UnicodeError) as exc:
        raise TreeError(f"cannot read task purpose: {path}") from exc
    return " ".join(paragraph) or None


def recorded_purpose(path: Path) -> str:
    purpose = assignment_paragraph(path, "manager_delegation") or assignment_paragraph(path, "human_instruction")
    if purpose is None:
        raise TreeError(f"selected task has no assignment paragraph explaining its purpose: {path}")
    return purpose


def relative_index_ref(root: Path, task_ref: str) -> str | None:
    candidate = Path(task_ref)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        normalized = candidate.resolve(strict=False)
        return normalized.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def root_index(root: Path) -> dict[str, list[TaskLine]]:
    todo = root / "TODO.md"
    try:
        text = todo.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TreeError(f"root TODO.md is unreadable: {todo}") from exc
    indexed: dict[str, list[TaskLine]] = {}
    for row in parse_task_text(text):
        key = relative_index_ref(root, row.task_file)
        if key is not None:
            indexed.setdefault(key, []).append(row)
    return indexed


def indexed_membership(root: Path, path: str, metadata: TaskMetadata, indexed: dict[str, list[TaskLine]]) -> str | None:
    rows = indexed.get(path, [])
    if len(rows) > 1:
        raise TreeError(f"task has duplicate root TODO.md rows: {path}")
    if metadata.status in DEFAULT_STATUSES and not rows:
        raise TreeError(f"active task is missing its root TODO.md row: {path}")
    if not rows:
        return None
    row = rows[0]
    if not row.target or canonical_target(row.target) != canonical_target(metadata.runat):
        raise TreeError(f"task root TODO.md target does not match frontmatter runat: {path}")
    return row.section


def task_record(path: str, purpose: str, metadata: TaskMetadata, membership: str | None, *, external: bool = False) -> TaskRecord:
    return TaskRecord(
        path,
        purpose,
        metadata.status,
        canonical_target(metadata.runat),
        canonical_target(metadata.managerat),
        metadata.is_manager,
        metadata.pending_task_items,
        metadata.blocked_on,
        membership,
        external,
    )


def selected_frontmatter(source: str, statuses: tuple[str, ...]) -> bool:
    lines = source.splitlines()[1:-1]
    declared = [line.partition(":")[2].strip() for line in lines if line.startswith("status:")]
    if len(declared) != 1:
        return any(line.startswith("version:") for line in lines)
    return declared[0] in statuses


def local_records(root: Path, statuses: tuple[str, ...]) -> list[TaskRecord]:
    indexed = root_index(root)
    records: list[TaskRecord] = []
    paths = set(root.rglob("*.md"))
    paths.update(root / relative for relative in indexed)
    for path in sorted(paths):
        if path == root / "TODO.md":
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise TreeError(f"indexed task escapes the work-log root: {path}") from exc
        required = relative in indexed
        if required and not path.exists():
            raise TreeError(f"indexed task does not exist: {relative}")
        source = frontmatter_only(path, indexed=required)
        if source is None:
            continue
        if path.is_symlink():
            raise TreeError(f"task record must not be a symlink: {relative}")
        if not required and not selected_frontmatter(source, statuses):
            continue
        try:
            metadata = parse_task_metadata(source, root)
        except TaskFrontmatterError as exc:
            raise TreeError(f"invalid task frontmatter in {relative}: {exc}") from exc
        if metadata is None:
            raise TreeError(f"indexed or selected task has no task metadata: {relative}")
        if metadata.status not in statuses or metadata.runat == "retired":
            continue
        membership = indexed_membership(root, relative, metadata, indexed)
        records.append(task_record(relative, recorded_purpose(path), metadata, membership))
    return records


def external_records(root: Path, statuses: tuple[str, ...]) -> list[TaskRecord]:
    if "blocked" not in statuses:
        return []
    registry = default_registry_dir()
    if not registry.exists():
        return []
    try:
        receipts = active_receipts(registry)
        root_info = root.stat()
    except (OSError, RegistrationError) as exc:
        raise TreeError(f"invalid external-task registry: {exc}") from exc
    records: list[TaskRecord] = []
    for receipt in receipts:
        if (receipt.root_device, receipt.root_inode) != (root_info.st_dev, root_info.st_ino):
            continue
        try:
            snapshot, _todo = assert_plan_current(plan_from_receipt(receipt))
            metadata = parse_task_metadata(snapshot.data.decode(), Path(receipt.source_root))
        except (OSError, UnicodeError, RegistrationError, TaskFrontmatterError) as exc:
            raise TreeError(f"invalid registered external task {receipt.task}: {exc}") from exc
        if metadata is None:
            raise TreeError(f"registered external task has no task frontmatter: {receipt.task}")
        if metadata.status not in statuses or metadata.runat == "retired":
            continue
        membership = f"{receipt.todo_section}: {receipt.todo_line}"
        records.append(task_record(receipt.task, recorded_purpose(Path(receipt.task)), metadata, membership, external=True))
    return records


def hierarchy(records: list[TaskRecord], main_manager: str) -> dict[str, Agent]:
    agents: dict[str, Agent] = {main_manager: Agent(main_manager, None, True)}
    for record in records:
        existing = agents.get(record.runat)
        if existing is None:
            agents[record.runat] = Agent(record.runat, record.managerat, record.is_manager, [record])
            continue
        if record.runat == main_manager:
            if not record.is_manager:
                raise TreeError(f"configured main manager has a worker task record: {record.path}")
            if record.status in DEFAULT_STATUSES and any(task.status in DEFAULT_STATUSES for task in existing.tasks):
                raise TreeError(f"duplicate selected owner records for {record.runat}: {existing.tasks[0].path}, {record.path}")
            existing.tasks.append(record)
            continue
        if record.status in DEFAULT_STATUSES and any(task.status in DEFAULT_STATUSES for task in existing.tasks):
            raise TreeError(f"duplicate active owner records for {record.runat}: {existing.tasks[0].path}, {record.path}")
        if existing.manager != record.managerat:
            raise TreeError(f"agent has conflicting managers: {record.runat}")
        if existing.is_manager != record.is_manager:
            raise TreeError(f"agent has conflicting manager roles: {record.runat}")
        existing.tasks.append(record)
    for agent in agents.values():
        agent.tasks.sort(key=lambda task: task.path)
    for target, agent in sorted(agents.items()):
        if target == main_manager:
            continue
        if agent.manager == target:
            raise TreeError(f"agent reports to itself: {target}")
        if agent.manager not in agents:
            raise TreeError(f"agent {target} has missing parent {agent.manager}")
        agents[agent.manager].children.append(target)
    for agent in agents.values():
        agent.children.sort()
    checked: set[str] = set()
    active: set[str] = set()

    def visit(target: str) -> None:
        if target in active:
            raise TreeError(f"manager cycle includes {target}")
        if target in checked:
            return
        active.add(target)
        for child in agents[target].children:
            visit(child)
        active.remove(target)
        checked.add(target)

    for target in sorted(agents):
        visit(target)
    reached: set[str] = set()
    pending = [main_manager]
    while pending:
        target = pending.pop()
        if target in reached:
            continue
        reached.add(target)
        pending.extend(agents[target].children)
    unreachable = sorted(agents.keys() - reached)
    if unreachable:
        raise TreeError(f"agent is unreachable from main manager {main_manager}: {unreachable[0]}")
    return agents


def current_tmux_target() -> str | None:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    target = result.stdout.strip()
    if result.returncode != 0 or TMUX_TARGET_RE.fullmatch(target) is None:
        return None
    return canonical_target(target)


def selected_root(args: Args, agents: dict[str, Agent], main_manager: str) -> str:
    if args.agent is not None:
        if args.agent not in agents:
            raise MissingRootError(f"requested agent is not represented by the selected records: {args.agent}")
        return args.agent
    if args.full_tree:
        return main_manager
    current = current_tmux_target()
    return current if current in agents else main_manager


def role(agent: Agent, main_manager: str) -> str:
    if agent.target == main_manager:
        return "main manager"
    return "manager" if agent.is_manager else "worker"


def text_tree(agents: dict[str, Agent], root: str, main_manager: str, depth: int | None) -> str:
    lines: list[str] = []

    def render(target: str, prefix: str, branch: str, level: int) -> None:
        agent = agents[target]
        lines.append(f"{prefix}{branch}{target} [{role(agent, main_manager)}]")
        detail = prefix + ("    " if branch == "└── " else "│   " if branch else "")
        if not agent.tasks:
            lines.append(f"{detail}work: no open work recorded")
        for task in agent.tasks:
            lines.append(f"{detail}task: {task.path} [{task.status}]")
            lines.append(f"{detail}  purpose: {task.purpose}")
            if task.pending_items:
                lines.extend(f"{detail}  work: {item}" for item in task.pending_items)
            else:
                lines.append(f"{detail}  work: no open work recorded")
            if task.blocked_on:
                lines.append(f"{detail}  blocked_on: {task.blocked_on}")
        if depth is not None and level >= depth:
            return
        children = agent.children
        for index, child in enumerate(children):
            render(child, detail if branch else "", "└── " if index == len(children) - 1 else "├── ", level + 1)

    render(root, "", "", 0)
    return "\n".join(lines) + "\n"


def task_json(task: TaskRecord) -> dict[str, object]:
    return {
        "path": task.path,
        "purpose": task.purpose,
        "status": task.status,
        "pending_items": list(task.pending_items),
        "blocked_on": task.blocked_on or None,
        "todo_membership": task.todo_membership,
        "external": task.external,
    }


def agent_json(agents: dict[str, Agent], target: str, main_manager: str, depth: int | None, level: int = 0) -> dict[str, object]:
    agent = agents[target]
    children = [] if depth is not None and level >= depth else [agent_json(agents, child, main_manager, depth, level + 1) for child in agent.children]
    return {
        "target": target,
        "role": role(agent, main_manager),
        "current_work": [item for task in agent.tasks for item in (task.pending_items or ("no open work recorded",))]
        or ["no open work recorded"],
        "tasks": [task_json(task) for task in agent.tasks],
        "children": children,
    }


def run(args: Args) -> str:
    configured = configuration(args)
    records = local_records(configured.root, args.statuses)
    records.extend(external_records(configured.root, args.statuses))
    agents = hierarchy(records, configured.main_manager)
    root = selected_root(args, agents, configured.main_manager)
    if not args.json:
        return text_tree(agents, root, configured.main_manager, args.depth)
    result = {
        "selected_root": root,
        "main_manager": configured.main_manager,
        "statuses": list(args.statuses),
        "depth": args.depth,
        "tree": agent_json(agents, root, configured.main_manager, args.depth),
    }
    return json.dumps(result, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        print(run(args), end="")
    except TreeError as exc:
        print(f"omo_agent_tree: {exc}", file=sys.stderr)
        return exc.exit_code
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
