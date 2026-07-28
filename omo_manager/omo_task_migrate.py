#!/usr/bin/env python3
"""Plan, validate, and apply the v1-to-v2 task metadata migration."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import DEFAULT_ROOT
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking import ENABLE_FILE
from omo_manager.omo_blocking import V2_VERSION
from omo_manager.omo_blocking import generated_id
from omo_manager.omo_blocking import render_task
from omo_manager.omo_blocking import split_task_text
from omo_manager.omo_blocking import task_hash
from omo_manager.omo_blocking import task_paths
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V2
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import TaskMetadata
from omo_manager.omo_task_metadata import first_version
from omo_manager.omo_task_metadata import frontmatter_parts
from omo_manager.omo_task_metadata import frontmatter_text
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_lock import task_file_lock

PLAN_VERSION = "v1"


@dataclass(frozen=True)
class Args:
    root: Path
    command: str
    plan: Path
    resume_statuses: tuple[tuple[str, str], ...] = ()
    long_running_reasons: tuple[tuple[str, str], ...] = ()


def parse_resume_status(value: str) -> tuple[str, str]:
    task, separator, status = value.partition("=")
    if not separator or status not in {"running", "long_running"} or not task:
        raise argparse.ArgumentTypeError("--resume-status must be TASK=running|long_running")
    return task, status


def parse_long_running_reason(value: str) -> tuple[str, str]:
    task, separator, reason = value.partition("=")
    if not separator or not task or not reason.strip() or "\n" in reason or "\r" in reason:
        raise argparse.ArgumentTypeError("--long-running-reason must be TASK=one-line reason")
    return task, reason.strip()


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Write one immutable migration plan.")
    plan.add_argument("--output", dest="plan", type=Path, required=True)
    plan.add_argument("--resume-status", action="append", type=parse_resume_status, default=[])
    plan.add_argument("--long-running-reason", action="append", type=parse_long_running_reason, default=[])
    for command in ("dry-run", "write", "enable"):
        operation = sub.add_parser(command, help=f"{command} one reviewed migration plan.")
        operation.add_argument("--plan", type=Path, required=True)
    parsed = parser.parse_args(argv)
    path = parsed.plan if hasattr(parsed, "plan") else parsed.output
    return Args(
        parsed.root.resolve(),
        parsed.command,
        path.resolve(),
        tuple(getattr(parsed, "resume_status", ())),
        tuple(getattr(parsed, "long_running_reason", ())),
    )


def path_in_root(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BlockingError("migration plan task escapes the configured root") from exc
    return path


def normalized_legacy_long_running_text(text: str, reason: str) -> str | None:
    parts = frontmatter_parts(text)
    if parts is None:
        return None
    frontmatter, body = parts
    fields = v1_fields(frontmatter)
    status = fields.get("status")
    if fields.get("version", (None, ""))[1] != TASK_FRONTMATTER_V1 or status is None or status[1] != "long_running" or "blocked_on" in fields:
        return None
    frontmatter.insert(status[0] + 1, f"blocked_on: {reason}")
    trailing_newline = "\n" if text.endswith("\n") else ""
    return "\n".join(["---", *frontmatter, "---", *body]) + trailing_newline


def v1_fields(frontmatter: list[str]) -> dict[str, tuple[int, str]]:
    fields: dict[str, tuple[int, str]] = {}
    for index, line in enumerate(frontmatter):
        if line.startswith("  - "):
            continue
        key, separator, value = line.partition(":")
        if separator and key in {"version", "status", "blocked_on"} and key not in fields:
            fields[key] = (index, value.strip())
    return fields


def v1_status(text: str) -> str:
    parts = frontmatter_parts(text)
    if parts is None:
        return ""
    return v1_fields(parts[0]).get("status", (None, ""))[1]


def v1_metadata_for_migration(
    text: str, long_running_reason: str | None, work_log_root: Path | None = None
) -> tuple[TaskMetadata, bool]:
    try:
        metadata = parse_task_metadata(text, work_log_root)
        normalized_legacy_reason = False
    except TaskFrontmatterError:
        if normalized_legacy_long_running_text(text, "required reason") is None:
            raise
        if long_running_reason is None:
            raise BlockingError("every legacy long_running task requires a reviewed --long-running-reason choice") from None
        normalized = normalized_legacy_long_running_text(text, long_running_reason)
        assert normalized is not None
        metadata = parse_task_metadata(normalized, work_log_root)
        normalized_legacy_reason = True
    if metadata is None or metadata.version != TASK_FRONTMATTER_V1:
        raise BlockingError("migration planning requires v1 task metadata")
    if metadata.status == "long_running" and not metadata.blocked_on:
        if long_running_reason is None:
            raise BlockingError("every legacy long_running task requires a reviewed --long-running-reason choice")
        normalized = normalized_legacy_long_running_text(text, long_running_reason)
        if normalized is None:
            raise BlockingError("legacy long_running task could not be normalized")
        metadata = parse_task_metadata(normalized, work_log_root)
        assert metadata is not None
        normalized_legacy_reason = True
    return metadata, normalized_legacy_reason


def v2_metadata(
    text: str, resume_status: str | None, long_running_reason: str | None, work_log_root: Path | None = None
) -> tuple[dict[str, Any], str]:
    metadata, normalized_legacy_reason = v1_metadata_for_migration(text, long_running_reason, work_log_root)
    if long_running_reason is not None and metadata.status != "long_running":
        raise BlockingError("--long-running-reason is only valid for a long_running task")
    _frontmatter, body = split_task_text(text)
    migrated: dict[str, Any] = {
        "version": V2_VERSION,
        "task_id": generated_id("task"),
        "status": metadata.status,
        "runat": metadata.runat,
        "tool": metadata.tool,
        "managerat": metadata.managerat,
        "is_manager": metadata.is_manager,
        "pending_task_items": [
            {"id": generated_id("pi"), "text": item, "blocked_on": [], "notices": []} for item in metadata.pending_task_items
        ],
        "resolved_task_items": [],
    }
    if metadata.status == "blocked":
        if resume_status not in {"running", "long_running"}:
            raise BlockingError("every blocked task requires a reviewed --resume-status choice")
        migrated["resume_status"] = resume_status
        migrated["blocked_on"] = [{"kind": "legacy", "text": metadata.blocked_on}]
    elif metadata.status == "long_running":
        if long_running_reason is not None and metadata.blocked_on and not normalized_legacy_reason:
            raise BlockingError("--long-running-reason is only for a legacy long_running task missing `blocked_on`")
        migrated["blocked_on"] = [{"kind": "persistent", "reason": metadata.blocked_on or long_running_reason}]
    elif long_running_reason is not None:
        raise BlockingError("--long-running-reason is only valid for a long_running task")
    elif resume_status is not None:
        raise BlockingError("--resume-status is only valid for a blocked task")
    return migrated, body


def make_plan(root: Path, resume_statuses: dict[str, str], long_running_reasons: dict[str, str]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    used_resume: set[str] = set()
    used_long_running_reasons: set[str] = set()
    for path in task_paths(root):
        text = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(root))
        version = first_version(frontmatter_text(text) or "")
        if version == TASK_FRONTMATTER_V2:
            _ = parse_task_metadata(text, root)
            continue
        if version != TASK_FRONTMATTER_V1:
            raise BlockingError("active task uses an unsupported metadata version")
        resume = resume_statuses.get(relative)
        if resume is not None:
            used_resume.add(relative)
        long_running_reason = long_running_reasons.get(relative)
        if long_running_reason is not None:
            if v1_status(text) == "long_running":
                used_long_running_reasons.add(relative)
        migrated, body = v2_metadata(text, resume, long_running_reason, root)
        rendered = render_task(migrated, body)
        rows.append(
            {
                "task": relative,
                "v1_sha256": task_hash(text),
                "v2_sha256": task_hash(rendered),
                "v2_text": rendered,
            }
        )
    if unused := set(resume_statuses) - used_resume:
        raise BlockingError(f"resume status names an absent or nonblocked task: {sorted(unused)[0]}")
    if unused := set(long_running_reasons) - used_long_running_reasons:
        raise BlockingError(f"long-running reason names an absent or non-long_running task: {sorted(unused)[0]}")
    return {"version": PLAN_VERSION, "tasks": rows}


def write_private(path: Path, text: str, expected_sha256: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with task_file_lock(path):
        before = path.stat() if path.exists() else None
        mode = before.st_mode & 0o777 if before is not None else 0o600
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                _ = stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, mode)
            if expected_sha256 is not None:
                if before is None or task_hash(path.read_text(encoding="utf-8")) != expected_sha256:
                    raise BlockingError("task changed after migration validation; retry from the reviewed plan")
                after = path.stat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise BlockingError("task changed after migration validation; retry from the reviewed plan")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def load_plan(path: Path, work_log_root: Path | None = None) -> list[dict[str, str]]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BlockingError(f"invalid migration plan YAML: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != PLAN_VERSION or set(value) != {"version", "tasks"}:
        raise BlockingError("invalid migration plan header")
    tasks = value["tasks"]
    if not isinstance(tasks, list):
        raise BlockingError("migration plan tasks must be a list")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for value_row in tasks:
        if not isinstance(value_row, dict) or set(value_row) != {"task", "v1_sha256", "v2_sha256", "v2_text"}:
            raise BlockingError("invalid migration plan task entry")
        if not all(isinstance(value_row[key], str) for key in value_row):
            raise BlockingError("migration plan task values must be text")
        row = {key: value_row[key] for key in value_row}
        if row["task"] in seen:
            raise BlockingError("migration plan contains a duplicate task")
        if task_hash(row["v2_text"]) != row["v2_sha256"]:
            raise BlockingError("migration plan v2 bytes do not match their hash")
        metadata = parse_task_metadata(row["v2_text"], work_log_root)
        if metadata is None or metadata.version != TASK_FRONTMATTER_V2:
            raise BlockingError("migration plan contains invalid v2 task bytes")
        seen.add(row["task"])
        rows.append(row)
    return rows


def validate_plan_state(root: Path, rows: list[dict[str, str]]) -> list[tuple[Path, dict[str, str], str]]:
    result: list[tuple[Path, dict[str, str], str]] = []
    for row in rows:
        path = path_in_root(root, row["task"])
        text = path.read_text(encoding="utf-8")
        digest = task_hash(text)
        if digest == row["v1_sha256"]:
            state = "v1"
        elif digest == row["v2_sha256"]:
            state = "v2"
        else:
            raise BlockingError(f"task bytes drifted from the reviewed migration plan: {row['task']}")
        result.append((path, row, state))
    return result


def require_clean_committed(paths: Sequence[Path], purpose: str) -> None:
    """Require each rollout input to be tracked and unchanged in its containing Git repository."""
    for path in paths:
        repository_result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if repository_result.returncode != 0:
            raise BlockingError(f"{purpose} must be clean and committed in Git")
        repository = Path(repository_result.stdout.strip()).resolve()
        try:
            relative = path.resolve().relative_to(repository)
        except ValueError as exc:
            raise BlockingError(f"{purpose} must be clean and committed in Git") from exc
        tracked = subprocess.run(
            ["git", "-C", str(repository), "ls-files", "--error-unmatch", "--", str(relative)],
            capture_output=True,
            text=True,
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain=v1", "--untracked-files=all", "--", str(relative)],
            capture_output=True,
            text=True,
            check=False,
        )
        if tracked.returncode != 0 or status.returncode != 0 or status.stdout:
            raise BlockingError(f"{purpose} must be clean and committed in Git: {path}")


def run(args: Args) -> int:
    if args.command == "plan":
        if args.plan.exists():
            raise BlockingError("migration plan output already exists")
        require_clean_committed(task_paths(args.root), "migration task inputs")
        plan = make_plan(args.root, dict(args.resume_statuses), dict(args.long_running_reasons))
        write_private(args.plan, yaml.safe_dump(plan, allow_unicode=True, sort_keys=False, width=160))
        print(f"planned {len(plan['tasks'])} task(s)")
        return 0
    rows = load_plan(args.plan, args.root)
    states = validate_plan_state(args.root, rows)
    require_clean_committed([args.plan], "migration plan")
    if args.command == "dry-run":
        for _path, row, state in states:
            print(f"{row['task']}\t{state}")
        return 0
    if args.command == "enable":
        if any(state != "v2" for _path, _row, state in states):
            raise BlockingError("v2 enablement requires every planned task to be migrated")
        planned = {path.resolve() for path, _row, _state in states}
        if planned != set(task_paths(args.root)):
            raise BlockingError("v2 enablement requires the plan to cover every active task")
        require_clean_committed([path for path, _row, _state in states], "migrated task files")
        write_private(args.root / ENABLE_FILE, yaml.safe_dump({"version": V2_VERSION, "enabled": True}, sort_keys=False))
        print("enabled v2 task writers and wake delivery")
        return 0
    require_clean_committed([path for path, _row, state in states if state == "v1"], "v1 migration task inputs")
    for path, row, state in states:
        if state == "v1":
            metadata = parse_task_metadata(row["v2_text"], args.root)
            if metadata is None:
                raise BlockingError("migration plan task metadata disappeared")
            with task_target_lock(args.root, metadata.runat):
                write_private(path, row["v2_text"], row["v1_sha256"])
    _ = validate_plan_state(args.root, rows)
    print(f"migrated {sum(state == 'v1' for _path, _row, state in states)} task(s)")
    return 0


def main(argv: list[str]) -> int:
    try:
        return run(parse_args(argv))
    except (OSError, BlockingError, ValueError) as exc:
        print(f"omo_task_migrate.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
