#!/usr/bin/env python3
"""Explicitly bind Codex UUIDs to eligible idle task records.

The default operation is a plan.  ``--apply`` is required to write task files;
each candidate is revalidated under the task/target locks immediately before
the guarded ``/status`` query and the frontmatter update.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_start import UUID_RE, query_exact_status_session_id, record_session_id, resolve_pane
from omo_manager.omo_codex_status import Args as StatusArgs, current_input_text, inspect
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_blocking import task_paths

CODEX_PANE_COMMANDS = {"bun", "bunx", "codex"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write captured UUIDs; otherwise print a dry-run plan.")
    parser.add_argument("--include-human-owned", action="store_true", help="Explicitly include h* task targets (still requires ready/empty Codex state).")
    return parser.parse_args(argv)


def candidates(root: Path, include_human_owned: bool = False) -> list[Path]:
    result = []
    targets: set[str] = set()
    for path in task_paths(root):
        metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
        if metadata is None or metadata.tool != "codex" or metadata.session_id:
            continue
        if metadata.status == "done" or metadata.runat == "retired" or (metadata.runat.partition(":")[0].startswith("h") and not include_human_owned):
            continue
        try:
            pane = resolve_pane(metadata.runat)
        except Exception:
            continue
        if pane.command not in CODEX_PANE_COMMANDS:
            continue
        report = inspect(StatusArgs(pane.target, 80))
        if report.status != "ready" or current_input_text(report.lines).strip():
            continue
        if metadata.runat in targets:
            continue
        targets.add(metadata.runat)
        result.append(path)
    return result


def run(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    include_human_owned = getattr(args, "include_human_owned", False)
    paths = candidates(root, include_human_owned)
    if not args.apply:
        for path in paths:
            print(f"eligible\t{path.relative_to(root)}")
        print(f"dry-run: {len(paths)} eligible task(s)")
        return 0
    for path in paths:
        with task_file_lock(path):
            expected_sha256 = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
            if metadata is None or metadata.tool != "codex" or metadata.session_id or metadata.status == "done" or metadata.runat == "retired" or (metadata.runat.partition(":")[0].startswith("h") and not include_human_owned):
                continue
            with task_target_lock(root, metadata.runat):
                try:
                    pane = resolve_pane(metadata.runat)
                except Exception as exc:
                    print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tpane unavailable: {exc}", file=sys.stderr)
                    continue
                if pane.command not in CODEX_PANE_COMMANDS:
                    print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tnot a Codex pane", file=sys.stderr)
                    continue
                report = inspect(StatusArgs(pane.target, 80))
                if report.status != "ready" or current_input_text(report.lines).strip():
                    print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tnot ready or has input", file=sys.stderr)
                    continue
                try:
                    session_id = query_exact_status_session_id(pane, 240, 10.0)
                except Exception as exc:
                    print(f"skipped\t{path.relative_to(root)}\t{exc}", file=sys.stderr)
                    continue
                if UUID_RE.fullmatch(session_id) is None:
                    print(f"skipped\t{path.relative_to(root)}\t/status did not return a valid UUID", file=sys.stderr)
                    continue
                try:
                    current = resolve_pane(metadata.runat)
                except Exception as exc:
                    print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tpane unavailable after capture: {exc}", file=sys.stderr)
                    continue
                if (current.pane_id, current.window_id, current.pane_pid, current.command) != (pane.pane_id, pane.window_id, pane.pane_pid, pane.command):
                    print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tpane identity changed during capture", file=sys.stderr)
                    continue
                after = parse_task_metadata(path.read_text(encoding="utf-8"), root)
                if after is None or after.tool != "codex" or after.session_id or after.status != metadata.status or after.status == "done" or after.runat != metadata.runat or after.runat == "retired" or (after.runat.partition(":")[0].startswith("h") and not include_human_owned):
                    print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\ttask eligibility changed during capture", file=sys.stderr)
                    continue
                record_session_id(path, session_id, expected_sha256, lock_held=True)
                print(f"migrated\t{path.relative_to(root)}\t{session_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args(sys.argv[1:])))
    except Exception as exc:
        print(f"omo_codex_session_migrate.py: {exc}", file=sys.stderr)
        raise SystemExit(2)
