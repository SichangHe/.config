#!/usr/bin/env python3
"""Update only the historical scorer blocker after exact archived custody checks."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TaskFrontmatterError, parse_task_metadata, parse_task_text, resolve_task_path, same_tmux_target
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_status import same_file_generation

OLD_TASK = Path("202608/dw_rescore_pages.md")
ARCHIVE = Path("202608/old_todos.md")
SUCCESSOR = Path("scorer_recovery_0926.md")
TODO = Path("TODO.md")
HUMAN_SOURCE = Path("manager_mail/85c5dff58359-2172.txt")
HUMAN_SOURCE_SHA256 = "fb4b502ad342d9cd0a1f18d391fcf72691276ba1130bb51eb431189f090ad346"
OLD_BLOCKER = "scorer_recovery_0926.md: historical owner absent; fresh Bino/socket-bound preflight and replacement custody pending"
NEW_BLOCKER = "sole live successor scorer_recovery_0926.md at dw:2 owns scoring; historical record remains BLOCKED pending supported closure"
SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")


def reconcile(
    root: Path,
    expected: dict[str, str],
    *,
    apply: bool = False,
) -> str:
    """Check all custody evidence under locks; publish one exact blocker line only when requested."""

    root = root.resolve()
    paths = {"old": root / OLD_TASK, "archive": root / ARCHIVE, "todo": root / TODO,
             "successor": root / SUCCESSOR, "human": root / HUMAN_SOURCE}
    if set(expected) != set(paths) or any(SHA256_RE.fullmatch(value) is None for value in expected.values()):
        raise TaskFrontmatterError("five exact lowercase SHA-256 assertions are required")
    if expected["human"] != HUMAN_SOURCE_SHA256:
        raise TaskFrontmatterError("Human custody source does not match the reviewed message")
    with ExitStack() as locks:
        for path in sorted(paths.values()):
            locks.enter_context(task_file_lock(path))
        payloads = {}
        states = {}
        for name, path in paths.items():
            if not path.is_file() or path.resolve(strict=True) != path:
                raise TaskFrontmatterError(f"{name} is not a regular canonical file")
            states[name] = path.stat()
            payloads[name] = path.read_bytes()
            if hashlib.sha256(payloads[name]).hexdigest() != expected[name] or not same_file_generation(path.stat(), states[name]):
                raise TaskFrontmatterError(f"{name} changed or does not match its expected digest")
        old = payloads["old"].decode("utf-8")
        archive = payloads["archive"].decode("utf-8")
        todo = payloads["todo"].decode("utf-8")
        human = payloads["human"].decode("utf-8")
        historical = parse_task_metadata(old, root)
        successor = parse_task_metadata(payloads["successor"].decode("utf-8"), root)
        if (
            historical is None or historical.status != "blocked" or historical.runat != "dw:39"
            or historical.blocked_on != OLD_BLOCKER or historical.pending_task_items
            or historical.is_manager or historical.managerat != "wl:1"
            or successor is None or successor.status != "running" or successor.runat != "dw:2"
            or successor.is_manager or successor.managerat != "wl:1"
        ):
            raise TaskFrontmatterError("historical blocked record or sole active successor is not exact")
        if (
            [line for line in archive.splitlines() if "dw_rescore_pages.md" in line]
            != ["dw_rescore_pages.md dw:39", "202608/dw_rescore_pages.md dw:39"]
            or any("dw_rescore_pages.md" in line for line in todo.splitlines())
            or [(task.section, task.target, task.line) for task in parse_task_text(todo)
                if resolve_task_path(root, task.task_file) == paths["successor"]]
            != [("todo:current", "dw:2", "scorer_recovery_0926.md dw:2")]
            or "Become responsible for that" not in human
            or "old scoring task is archived and blocked" not in human
        ):
            raise TaskFrontmatterError("archived index, current owner, or Human custody source differs")
        current_owners = [task for task in parse_task_text(todo)
                          if task.section == "todo:current" and same_tmux_target(task.target, "dw:2")]
        if len(current_owners) != 1 or resolve_task_path(root, current_owners[0].task_file) != paths["successor"]:
            raise TaskFrontmatterError("successor is not the sole current dw:2 target owner")
        # 🧑 "Become responsible for that".
        windows = subprocess.run(
            ["tmux", "list-windows", "-t", "dw", "-F", "#{window_index}"],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.splitlines()
        if "39" in windows or "2" not in windows or len(windows) != len(set(windows)):
            raise TaskFrontmatterError("historical window reappeared or sole successor window is absent")
        previous = f"blocked_on: {OLD_BLOCKER}\n"
        if old.count(previous) != 1:
            raise TaskFrontmatterError("old blocker is not unique")
        updated = old.replace(previous, f"blocked_on: {NEW_BLOCKER}\n", 1)
        changed = parse_task_metadata(updated, root)
        if changed != replace(historical, blocked_on=NEW_BLOCKER):
            raise TaskFrontmatterError("other historical metadata would change")
        if apply:
            for name, path in paths.items():
                if path.read_bytes() != payloads[name] or not same_file_generation(path.stat(), states[name]):
                    raise TaskFrontmatterError(f"{name} changed before historical blocker publication")
            windows_now = subprocess.run(
                ["tmux", "list-windows", "-t", "dw", "-F", "#{window_index}"],
                capture_output=True, text=True, check=True, timeout=5,
            ).stdout.splitlines()
            if windows_now != windows:
                raise TaskFrontmatterError("window inventory changed before publication")
            replace_if_unchanged_locked(paths["old"], updated, states["old"])
        return hashlib.sha256(updated.encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    for name in ("old", "archive", "todo", "successor", "human"):
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--apply", action="store_true", help="Publish only after separately reviewed owner and manager approval.")
    args = parser.parse_args(argv)
    try:
        digest = reconcile(args.root, {name: getattr(args, f"{name}_sha256") for name in ("old", "archive", "todo", "successor", "human")}, apply=args.apply)
    except (OSError, UnicodeError, subprocess.SubprocessError, TaskFrontmatterError) as exc:
        print(f"omo_archived_custody.py: {exc}", file=sys.stderr)
        return 2
    print(f"{'Published' if args.apply else 'Read-only preflight for'} one historical blocked_on line; resulting SHA-256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
