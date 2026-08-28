#!/usr/bin/env python3
"""Explicitly bind Codex UUIDs to eligible live task records.

The default operation is a plan.  ``--apply`` is required to write task files;
each candidate is revalidated under the task/target locks immediately before
the guarded ``/status`` query and frontmatter update. Existing visible input is
submitted once before querying status, as explicitly requested by the human.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_start import UUID_RE, query_exact_status_session_id, record_session_id, resolve_pane
from omo_manager.omo_codex_status import Args as StatusArgs, current_input_text, inspect, is_stock_placeholder_input_text
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_blocking import task_paths

CODEX_PANE_COMMANDS = {"bun", "bunx", "codex"}
TARGET_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def line_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", value)
    if match is None or int(match.group(1)) > int(match.group(2)):
        raise argparse.ArgumentTypeError("expected START-END")
    return int(match.group(1)), int(match.group(2))


def submit_existing_input(pane) -> None:
    """Submit existing input only through the original exact process guard."""
    nonce = f"{__import__('os').getpid()}-{time.monotonic_ns()}"
    accepted = f"OMO_INPUT_ACCEPTED_{nonce}"
    condition = "#{&&:#{==:#{pane_id},%s},#{&&:#{==:#{window_id},%s},#{&&:#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{&&:#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}}}}" % (pane.pane_id, pane.window_id, pane.target, pane.pane_pid, pane.command)
    sequence = f"send-keys -t {pane.pane_id} Enter ; display-message -p {accepted}"
    result = subprocess.run(["tmux", "if-shell", "-F", "-t", pane.target, condition, sequence, "display-message -p OMO_INPUT_REJECTED"], capture_output=True, text=True, timeout=5)
    if result.returncode != 0 or result.stdout != accepted + "\n":
        raise RuntimeError("pane identity changed before submitting existing input")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Write captured UUIDs; otherwise print a dry-run plan.")
    parser.add_argument("--include-human-owned", action="store_true", help="Enable only the exact h* targets named by the bound human authority options.")
    parser.add_argument("--human-target", action="append", default=[], help="Exact authorized h* target; repeat as needed.")
    parser.add_argument("--human-authority-file", type=Path, help="Owner-private manager_mail source explicitly authorizing the named human targets.")
    parser.add_argument("--human-authority-lines", type=line_range, help="Inclusive authoritative source line range.")
    parser.add_argument("--human-authority-sha256", default="", help="SHA-256 of the complete authority file.")
    args = parser.parse_args(argv)
    authority = (args.human_target, args.human_authority_file, args.human_authority_lines, args.human_authority_sha256)
    if args.include_human_owned and (not args.apply or not all(authority)):
        parser.error("--include-human-owned requires --apply, one or more --human-target values, and complete authority file/lines/digest.")
    if not args.include_human_owned and any(authority):
        parser.error("human target and authority options require --include-human-owned.")
    return args


def authorized_human_targets(args: argparse.Namespace, root: Path) -> set[str]:
    if not getattr(args, "include_human_owned", False):
        return set()
    targets = set(getattr(args, "human_target", ()))
    if not targets or any(TARGET_RE.fullmatch(target) is None or not target.partition(":")[0].startswith("h") for target in targets):
        raise ValueError("human targets must be exact h* tmux targets")
    source_arg = getattr(args, "human_authority_file", None)
    line_spec = getattr(args, "human_authority_lines", None)
    expected_digest = getattr(args, "human_authority_sha256", "")
    if source_arg is None or line_spec is None or SHA256_RE.fullmatch(expected_digest) is None:
        raise ValueError("complete human authority binding is required")
    mail_root = (root / "manager_mail").resolve(strict=True)
    source = (source_arg if source_arg.is_absolute() else root / source_arg).resolve(strict=True)
    source_state = source.stat()
    mail_state = mail_root.stat()
    if source.parent != mail_root or not stat.S_ISREG(source_state.st_mode) or source_state.st_uid != os.getuid() or stat.S_IMODE(source_state.st_mode) & 0o077 or mail_state.st_uid != os.getuid() or stat.S_IMODE(mail_state.st_mode) & 0o077:
        raise ValueError("human authority must be one owner-private manager_mail file")
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise ValueError("human authority digest changed")
    lines = payload.decode("utf-8").splitlines()
    start, end = line_spec
    if end > len(lines):
        raise ValueError("human authority line range exceeds source")
    excerpt = "\n".join(lines[start - 1 : end])
    if not ("Do these to the human windows as well" in excerpt or "send the status stash command to running agents" in excerpt):
        raise ValueError("selected human authority does not authorize status capture")
    named_targets = set(TARGET_RE.findall(excerpt))
    if not targets <= named_targets:
        raise ValueError("selected human authority does not name every requested human target")
    return targets


def candidates(root: Path, allowed_human_targets: set[str] | None = None) -> list[Path]:
    result = []
    targets: set[str] = set()
    for path in task_paths(root):
        metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
        if metadata is None or metadata.tool != "codex" or metadata.session_id:
            continue
        if metadata.status == "done" or metadata.runat == "retired" or (metadata.runat.partition(":")[0].startswith("h") and metadata.runat not in (allowed_human_targets or set())):
            continue
        try:
            pane = resolve_pane(metadata.runat)
        except Exception:
            continue
        if pane.command not in CODEX_PANE_COMMANDS:
            continue
        report = inspect(StatusArgs(pane.target, 80))
        if report.status not in {"ready", "running"}:
            continue
        if metadata.runat in targets:
            continue
        targets.add(metadata.runat)
        result.append(path)
    return result


def run(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    allowed_human_targets = authorized_human_targets(args, root)
    paths = candidates(root, allowed_human_targets)
    if not args.apply:
        for path in paths:
            print(f"eligible\t{path.relative_to(root)}")
        print(f"dry-run: {len(paths)} eligible task(s)")
        return 0
    for path in paths:
        with task_file_lock(path):
            expected_sha256 = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
            if metadata is None or metadata.tool != "codex" or metadata.session_id or metadata.status == "done" or metadata.runat == "retired" or (metadata.runat.partition(":")[0].startswith("h") and metadata.runat not in allowed_human_targets):
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
                if report.status not in {"ready", "running"}:
                    print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tnot ready or has input", file=sys.stderr)
                    continue
                existing_input = current_input_text(report.lines).strip()
                if existing_input and not is_stock_placeholder_input_text(existing_input):
                    try:
                        submit_existing_input(pane)
                    except Exception as exc:
                        print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tinput submission failed: {exc}", file=sys.stderr)
                        continue
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        report = inspect(StatusArgs(pane.target, 80))
                        current_input = current_input_text(report.lines).strip()
                        if report.status in {"ready", "running"} and (not current_input or is_stock_placeholder_input_text(current_input)):
                            break
                        time.sleep(0.1)
                    else:
                        print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tinput remained after Enter", file=sys.stderr)
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
                if after is None or after.tool != "codex" or after.session_id or after.status != metadata.status or after.status == "done" or after.runat != metadata.runat or after.runat == "retired" or (after.runat.partition(":")[0].startswith("h") and after.runat not in allowed_human_targets):
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
