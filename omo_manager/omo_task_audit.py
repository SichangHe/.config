#!/usr/bin/env python3
"""Read-only repository audit of task frontmatter, TODO indexing, and run targets."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import parse_task_lines
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_metadata import RETIRED_RUNAT, TaskBlocker, TaskFrontmatterError, TaskMetadata, canonical_target, parse_task_metadata


@dataclass(frozen=True, order=True)
class Finding:
    kind: str
    key: str
    tasks: tuple[str, ...]
    detail: str
    action: str


def task_files(root: Path) -> tuple[Path, ...]:
    ignored = {".git", ".venv", "__pycache__"}
    return tuple(sorted((path for path in root.rglob("*.md") if not ignored.intersection(path.parts)), key=lambda path: path.relative_to(root).as_posix()))


def successor_refs(metadata: TaskMetadata) -> tuple[str, ...]:
    typed = tuple(blocker.task for blocker in metadata.blockers if isinstance(blocker, TaskBlocker))
    if typed:
        return tuple(sorted(set(typed)))
    if metadata.version == "v2.0.0":
        return ()
    return tuple(sorted(set(re.findall(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.md", metadata.blocked_on))))


def audit(root: Path, *, include_terminal: bool = False) -> tuple[Finding, ...]:
    root = root.resolve(strict=True)
    todo_rows: dict[Path, list[str]] = defaultdict(list)
    for row in parse_task_lines(root / "TODO.md"):
        candidate = (root / row.task_file).resolve(strict=False)
        if candidate == root or root not in candidate.parents:
            continue
        todo_rows[candidate].append(row.section)

    metadata_by_path = {}
    for path in task_files(root):
        resolved = path.resolve(strict=False)
        if resolved == root or root not in resolved.parents:
            continue
        try:
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
        except (OSError, UnicodeError, TaskFrontmatterError):
            continue
        if metadata is not None:
            metadata_by_path[resolved] = metadata

    findings: list[Finding] = []
    terminal_tasks: list[str] = []
    active_targets: dict[str, list[Path]] = defaultdict(list)
    for path, metadata in metadata_by_path.items():
        relative = path.relative_to(root).as_posix()
        rows = todo_rows.get(path, [])
        if len(rows) > 1:
            findings.append(Finding("duplicate_todo", relative, (relative,), f"rows={len(rows)} sections={','.join(sorted(rows))}", "owner_reconciliation"))
        elif not rows:
            successors = successor_refs(metadata)
            if metadata.status == "done":
                terminal_tasks.append(relative)
                if not include_terminal:
                    continue
                kind, action, detail = "terminal_no_todo", "none", "done task is intentionally terminal"
            elif metadata.status == "blocked" and successors:
                kind, action, detail = "successor_blocked_no_todo", "verify_successor", f"successors={','.join(successors)}"
            elif metadata.status == "blocked":
                kind, action, detail = "blocked_no_todo", "disposition_required", f"blocked_on={metadata.blocked_on}"
            else:
                kind, action, detail = "zero_todo", "owner_reconciliation", f"status={metadata.status}"
            findings.append(Finding(kind, relative, (relative,), detail, action))
        if metadata.status != "done" and metadata.runat != RETIRED_RUNAT:
            active_targets[canonical_target(metadata.runat)].append(path)

    if terminal_tasks and not include_terminal:
        findings.append(Finding("terminal_no_todo_summary", "done", (), f"count={len(terminal_tasks)}", "none"))
    for target, paths in active_targets.items():
        if len(paths) < 2:
            continue
        tasks = tuple(sorted(path.relative_to(root).as_posix() for path in paths))
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
    return tuple(sorted(findings))


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


def audit_and_write_reconciliation_queue(root: Path, path: Path, *, include_terminal: bool = False) -> tuple[Finding, ...]:
    """Serialize source scanning with publication so an older scan cannot win later."""

    with task_file_lock(path):
        findings = audit(root, include_terminal=include_terminal)
        write_reconciliation_queue(path, findings, locked=True)
        return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--include-terminal", action="store_true", help="Include one finding per done task absent from TODO instead of a summary count.")
    parser.add_argument("--reconciliation-queue", type=Path, help="Atomically write the deterministic owner-reconciliation subset; unchanged scans leave it byte-identical.")
    args = parser.parse_args()
    findings = (
        audit_and_write_reconciliation_queue(args.root, args.reconciliation_queue, include_terminal=args.include_terminal)
        if args.reconciliation_queue is not None
        else audit(args.root, include_terminal=args.include_terminal)
    )
    if args.json:
        print(json.dumps([asdict(finding) for finding in findings], sort_keys=True, separators=(",", ":")))
    else:
        for finding in findings:
            print(f"{finding.kind}: key={finding.key} tasks={','.join(finding.tasks)} action={finding.action} detail={finding.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
