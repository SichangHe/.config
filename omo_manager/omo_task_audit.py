#!/usr/bin/env python3
"""Read-only repository audit of task frontmatter, TODO indexing, and run targets."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeAlias

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import DEFAULT_ROOT, parse_task_lines, parse_task_text
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import RETIRED_RUNAT, TaskBlocker, TaskFrontmatterError, TaskMetadata, UniqueKeyLoader, canonical_target, parse_task_metadata

TERMINAL_DISPOSITION_VERSION = "v1.0.0"
TERMINAL_DISPOSITIONS = {"supported_closure", "owner_disposition_required", "archived_dependency"}
TerminalDispositionMap: TypeAlias = dict[str, str]
FileSnapshot: TypeAlias = tuple[int, int, int, int, bytes]


class TerminalDispositionError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Finding:
    kind: str
    key: str
    tasks: tuple[str, ...]
    detail: str
    action: str


def load_terminal_dispositions(path: Path) -> TerminalDispositionMap:
    """Load a strict reviewed classification manifest without changing records."""

    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, TaskFrontmatterError) as exc:
        raise TerminalDispositionError(f"cannot read terminal disposition manifest: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"version", "records"} or value["version"] != TERMINAL_DISPOSITION_VERSION or not isinstance(value["records"], list):
        raise TerminalDispositionError("terminal disposition manifest must contain only version v1.0.0 and a records list")
    dispositions: TerminalDispositionMap = {}
    for record in value["records"]:
        if not isinstance(record, dict) or set(record) != {"task", "disposition", "evidence"}:
            raise TerminalDispositionError("each terminal disposition record must contain only task, disposition, and evidence")
        task, disposition, evidence = record["task"], record["disposition"], record["evidence"]
        if not isinstance(task, str) or not task or Path(task).is_absolute() or ".." in Path(task).parts or Path(task).suffix != ".md" or Path(task).as_posix() != task:
            raise TerminalDispositionError("terminal disposition task must be a canonical relative Markdown path within the audit root")
        if task in dispositions:
            raise TerminalDispositionError(f"duplicate terminal disposition task: {task}")
        if disposition not in TERMINAL_DISPOSITIONS:
            raise TerminalDispositionError(f"unsupported terminal disposition for {task}: {disposition}")
        if not isinstance(evidence, str) or not evidence.strip():
            raise TerminalDispositionError(f"terminal disposition evidence must be nonempty for {task}")
        dispositions[task] = disposition
    return dispositions


def task_files(root: Path) -> tuple[Path, ...]:
    ignored = {".git", ".venv", "__pycache__"}
    return tuple(sorted((path for path in root.rglob("*.md") if not ignored.intersection(path.parts)), key=lambda path: path.relative_to(root).as_posix()))


def successor_refs(metadata: TaskMetadata, non_task_refs: set[str]) -> tuple[str, ...]:
    typed = tuple(blocker.task for blocker in metadata.blockers if isinstance(blocker, TaskBlocker))
    if typed:
        return tuple(sorted(set(typed)))
    if metadata.version == "v2.0.0":
        return ()
    refs = set(re.findall(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.md", metadata.blocked_on))
    return tuple(sorted(refs.difference(non_task_refs)))


def root_todo_rows(root: Path):
    """Read the root TODO index or fail without silently approving an absent index."""

    try:
        return parse_task_text((root / "TODO.md").read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise TaskFrontmatterError(f"cannot read root TODO file: {exc}") from exc


def file_snapshot(path: Path) -> FileSnapshot:
    """Read one file and fail if its identity changes during the read."""

    try:
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise TaskFrontmatterError(f"cannot read audit input {path}: {exc}") from exc
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise TaskFrontmatterError(f"audit input changed while being read: {path}")
    return (*before_identity, payload)


def audit(root: Path, *, include_terminal: bool = False, terminal_dispositions: TerminalDispositionMap | None = None) -> tuple[Finding, ...]:
    root = root.resolve(strict=True)
    todo_path = root / "TODO.md"
    try:
        todo_snapshot = file_snapshot(todo_path)
        local_rows = parse_task_text(todo_snapshot[-1].decode("utf-8"))
    except (UnicodeError, TaskFrontmatterError) as exc:
        raise TaskFrontmatterError(f"cannot read root TODO file: {exc}") from exc
    todo_rows: dict[Path, list[tuple[str, str]]] = defaultdict(list)
    local_todo_rows = {(row.task_file, row.section, row.line, row.target) for row in local_rows}
    todo_path_findings: list[Finding] = []
    for row in parse_task_lines(todo_path):
        candidate = (root / row.task_file).resolve(strict=False)
        local_row = (row.task_file, row.section, row.line, row.target) in local_todo_rows
        if candidate == root or root not in candidate.parents:
            if local_row:
                todo_path_findings.append(
                    Finding("todo_invalid_task_path", row.task_file, (row.task_file,), "path escapes audit root", "owner_reconciliation")
                )
            continue
        canonical_ref = candidate.relative_to(root).as_posix()
        if local_row and canonical_ref != row.task_file:
            todo_path_findings.append(
                Finding(
                    "todo_invalid_task_path",
                    row.task_file,
                    tuple(sorted({row.task_file, canonical_ref})),
                    f"canonical={canonical_ref}",
                    "owner_reconciliation",
                )
            )
        todo_rows[candidate].append((row.section, row.target))

    paths = task_files(root)
    task_snapshots: dict[Path, FileSnapshot] = {}
    metadata_by_path = {}
    non_task_refs: set[str] = set()
    for path in paths:
        resolved = path.resolve(strict=False)
        if resolved == root or root not in resolved.parents:
            continue
        snapshot = file_snapshot(path)
        task_snapshots[path] = snapshot
        try:
            metadata = parse_task_metadata(snapshot[-1].decode("utf-8"), root)
        except (UnicodeError, TaskFrontmatterError):
            non_task_refs.add(path.relative_to(root).as_posix())
            continue
        if metadata is not None:
            metadata_by_path[resolved] = metadata
        else:
            non_task_refs.add(path.relative_to(root).as_posix())

    findings = todo_path_findings
    terminal_tasks: list[str] = []
    active_targets: dict[str, list[Path]] = defaultdict(list)
    matched_dispositions: set[str] = set()
    for path, rows in todo_rows.items():
        if path in metadata_by_path:
            continue
        relative = path.relative_to(root).as_posix()
        kind = "todo_invalid_task" if path.is_file() else "todo_missing_task"
        findings.append(Finding(kind, relative, (relative,), f"rows={len(rows)}", "owner_reconciliation"))
    for path, metadata in metadata_by_path.items():
        relative = path.relative_to(root).as_posix()
        rows = todo_rows.get(path, [])
        if metadata.status == "done" and metadata.pending_task_items:
            findings.append(
                Finding(
                    "done_pending_items",
                    relative,
                    (relative,),
                    f"items={len(metadata.pending_task_items)}",
                    "owner_reconciliation",
                )
            )
        if len(rows) > 1:
            sections = ",".join(sorted(section for section, _target in rows))
            findings.append(Finding("duplicate_todo", relative, (relative,), f"rows={len(rows)} sections={sections}", "owner_reconciliation"))
        elif not rows:
            successors = successor_refs(metadata, non_task_refs)
            disposition = (terminal_dispositions or {}).get(relative)
            if metadata.status == "done":
                terminal_tasks.append(relative)
                if not include_terminal:
                    continue
                kind, action, detail = "terminal_no_todo", "none", "done task is intentionally terminal"
            elif metadata.status == "blocked" and disposition == "archived_dependency":
                matched_dispositions.add(relative)
                kind, action, detail = "archived_dependency_no_todo", "none", "reviewed terminal disposition preserves this non-live dependency record"
            elif metadata.status == "blocked" and disposition == "supported_closure":
                matched_dispositions.add(relative)
                kind, action, detail = "blocked_no_todo", "supported_closure", f"blocked_on={metadata.blocked_on}"
            elif metadata.status == "blocked" and disposition == "owner_disposition_required":
                matched_dispositions.add(relative)
                kind, action, detail = "blocked_no_todo", "disposition_required", f"blocked_on={metadata.blocked_on}"
            elif metadata.status == "blocked" and successors:
                kind, action, detail = "successor_blocked_no_todo", "verify_successor", f"successors={','.join(successors)}"
            elif metadata.status == "blocked":
                kind, action, detail = "blocked_no_todo", "disposition_required", f"blocked_on={metadata.blocked_on}"
            else:
                kind, action, detail = "zero_todo", "owner_reconciliation", f"status={metadata.status}"
            findings.append(Finding(kind, relative, (relative,), detail, action))
        elif rows[0][1] and canonical_target(rows[0][1]) != canonical_target(metadata.runat):
            findings.append(
                Finding(
                    "todo_runat_mismatch",
                    relative,
                    (relative,),
                    f"todo={rows[0][1]} frontmatter={metadata.runat}",
                    "owner_reconciliation",
                )
            )
        if metadata.status != "done" and metadata.runat != RETIRED_RUNAT:
            active_targets[canonical_target(metadata.runat)].append(path)

    if terminal_tasks and not include_terminal:
        findings.append(Finding("terminal_no_todo_summary", "done", (), f"count={len(terminal_tasks)}", "none"))
    for relative, disposition in sorted((terminal_dispositions or {}).items()):
        if relative not in matched_dispositions:
            findings.append(Finding("terminal_disposition_mismatch", relative, (relative,), f"disposition={disposition}; expected one blocked task absent from TODO", "disposition_required"))
    for target, claimant_paths in active_targets.items():
        if len(claimant_paths) < 2:
            continue
        tasks = tuple(sorted(path.relative_to(root).as_posix() for path in claimant_paths))
        human = target.partition(":")[0].startswith("h")
        findings.append(
            Finding(
                "human_runat_conflict" if human else "duplicate_runat",
                target,
                tasks,
                f"claimants={len(tasks)}",
                "report_only" if human else "owner_reconciliation",
            )
        )
    if file_snapshot(todo_path) != todo_snapshot or task_files(root) != paths:
        raise TaskFrontmatterError("task audit inputs changed during the scan")
    for path, snapshot in task_snapshots.items():
        if file_snapshot(path) != snapshot:
            raise TaskFrontmatterError(f"task audit input changed during the scan: {path}")
    return tuple(sorted(findings))


# 🧑 "Maybe we should have a validation script that agents should run after editing task file/TODO file?"
def selected_findings(findings: tuple[Finding, ...], tasks: tuple[str, ...]) -> tuple[Finding, ...]:
    """Return findings involving any requested canonical task reference."""

    if not tasks:
        return findings
    selected = set(tasks)
    return tuple(finding for finding in findings if selected.intersection(finding.tasks))


def validate_selected_tasks(root: Path, tasks: tuple[str, ...]) -> None:
    """Require each selected path to name one valid task record inside `root`."""

    root = root.resolve(strict=True)
    for task in tasks:
        try:
            path = (root / task).resolve(strict=True)
            if root not in path.parents or not path.is_file() or path.relative_to(root).as_posix() != task:
                raise OSError
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
        except (OSError, UnicodeError, TaskFrontmatterError) as exc:
            raise TaskFrontmatterError(f"--task does not name a valid task record: {task}") from exc
        if metadata is None:
            raise TaskFrontmatterError(f"--task does not name a valid task record: {task}")


def validate_selected_todo_refs(root: Path, refs: tuple[str, ...]) -> None:
    """Require each selected reference to occur literally in the root TODO file."""

    rows = root_todo_rows(root.resolve(strict=True))
    available = {row.task_file for row in rows}
    for ref in refs:
        if ref not in available:
            raise TaskFrontmatterError(f"--todo-ref does not name an exact root TODO task reference: {ref}")


def write_reconciliation_queue(path: Path, findings: tuple[Finding, ...], *, locked: bool = False) -> None:
    """Atomically publish the stable actionable owner queue without notifying anyone."""

    selected = tuple(sorted(finding for finding in findings if finding.action == "owner_reconciliation"))
    payload = (json.dumps([asdict(finding) for finding in selected], sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    def publish() -> None:
        if path.exists() and path.read_bytes() == payload:
            return
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with open(fd, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).chmod(0o600)
            Path(temporary).replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if locked:
        publish()
    else:
        with task_file_lock(path):
            publish()


def audit_and_write_reconciliation_queue(root: Path, path: Path, *, include_terminal: bool = False, terminal_dispositions: TerminalDispositionMap | None = None) -> tuple[Finding, ...]:
    """Serialize source scanning with publication so an older scan cannot win later."""

    with task_file_lock(path):
        findings = audit(root, include_terminal=include_terminal, terminal_dispositions=terminal_dispositions)
        write_reconciliation_queue(path, findings, locked=True)
        return findings


def findings_introduced_since_git_head(
    root: Path,
    current: tuple[Finding, ...],
    *,
    include_terminal: bool = False,
    terminal_dispositions: TerminalDispositionMap | None = None,
) -> tuple[Finding, ...]:
    """Return only current findings absent from the committed work-log snapshot."""

    try:
        archived = subprocess.run(
            ["git", "-C", str(root), "archive", "--format=tar", "HEAD", "--", "*.md"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TaskFrontmatterError(f"cannot read the Git HEAD audit baseline: {exc}") from exc
    if archived.returncode != 0:
        detail = archived.stderr.decode("utf-8", errors="replace").strip()
        raise TaskFrontmatterError(f"cannot read the Git HEAD audit baseline: {detail or 'git archive failed'}")
    try:
        with tempfile.TemporaryDirectory(prefix="omo-task-audit-head-") as tmp:
            baseline_root = Path(tmp)
            with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as archive:
                archive.extractall(baseline_root, filter="data")
            baseline = set(audit(baseline_root, include_terminal=include_terminal, terminal_dispositions=terminal_dispositions))
    except (OSError, tarfile.TarError) as exc:
        raise TaskFrontmatterError(f"cannot materialize the Git HEAD audit baseline: {exc}") from exc
    return tuple(finding for finding in current if finding not in baseline)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Work-log root; defaults to OMO_WORK_LOGS_ROOT from omo_manager/local.env.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--include-terminal", action="store_true", help="Include one finding per done task absent from TODO instead of a summary count.")
    parser.add_argument("--task", action="append", default=[], help="Show only findings involving this canonical root-relative task; repeat as needed.")
    parser.add_argument("--todo-ref", action="append", default=[], help="Show only findings involving this exact task reference in the root TODO file; repeat as needed.")
    parser.add_argument("--check", action="store_true", help="Exit 1 when the selected findings require action.")
    parser.add_argument("--terminal-dispositions", type=Path, help="Strict reviewed YAML classifications for blocked records intentionally absent from TODO.")
    parser.add_argument("--reconciliation-queue", type=Path, help="Atomically write the deterministic owner-reconciliation subset; unchanged scans leave it byte-identical.")
    args = parser.parse_args()
    bare_check = len(sys.argv) == 1
    tasks = tuple(args.task)
    todo_refs = tuple(args.todo_ref)
    if any(Path(task).is_absolute() or Path(task).as_posix() != task or ".." in Path(task).parts or Path(task).suffix != ".md" for task in tasks):
        parser.error("--task must be a canonical root-relative Markdown path")
    try:
        validate_selected_tasks(args.root, tasks)
        validate_selected_todo_refs(args.root, todo_refs)
        dispositions = load_terminal_dispositions(args.terminal_dispositions) if args.terminal_dispositions is not None else None
        all_findings = (
            audit_and_write_reconciliation_queue(args.root, args.reconciliation_queue, include_terminal=args.include_terminal, terminal_dispositions=dispositions)
            if args.reconciliation_queue is not None
            else audit(args.root, include_terminal=args.include_terminal, terminal_dispositions=dispositions)
        )
        findings = (
            findings_introduced_since_git_head(
                args.root,
                all_findings,
                include_terminal=args.include_terminal,
                terminal_dispositions=dispositions,
            )
            if bare_check
            else selected_findings(all_findings, tasks + todo_refs)
        )
    except (TaskFrontmatterError, TerminalDispositionError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps([asdict(finding) for finding in findings], sort_keys=True, separators=(",", ":")))
    else:
        for finding in findings:
            print(f"{finding.kind}: key={finding.key} tasks={','.join(finding.tasks)} action={finding.action} detail={finding.detail}")
    return 1 if (args.check or bare_check) and any(finding.action not in {"none", "report_only"} for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
