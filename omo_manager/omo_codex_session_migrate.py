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
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_start import UUID_RE, exact_process_tmux_condition, query_exact_status_session_id, record_session_id, resolve_pane, start_ticks_guarded_tmux_action
from omo_manager.omo_codex_status import Args as StatusArgs, current_input_text, inspect, is_stock_placeholder_input_text
from omo_manager.omo_tmux_input_lock import tmux_input_lock
from omo_manager.omo_task_lock import canonical_target, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_status import authoritative_active_target_task_paths, root_membership_lock
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
    condition = exact_process_tmux_condition(pane)
    sequence = f"send-keys -t {pane.pane_id} Enter ; display-message -p {accepted}"
    rejected_command = "display-message -p OMO_INPUT_REJECTED"
    guarded = start_ticks_guarded_tmux_action(pane, sequence, rejected_command)
    result = subprocess.run(["tmux", "if-shell", "-F", "-t", pane.target, condition, guarded, rejected_command], capture_output=True, text=True, timeout=5)
    if result.returncode != 0 or result.stdout != accepted + "\n":
        raise RuntimeError("pane identity changed before submitting existing input")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    # 🧑 Source `manager_mail/85c5dff58359-1269.txt:3-9`: "Whatever the previous responsible agents were doing, they completely failed. Replace them."
    parser.add_argument("--task", help="Bind only this exact eligible root-relative task path.")
    parser.add_argument("--task-sha256", default="", help="Required current SHA-256 binding for --task.")
    # 🧑 "The email watcher that should replace agents is broken. Current config agents are not following instructions and need to be replaced."
    parser.add_argument("--expected-old-session-id", default="", help="Select this exact stale task UUID for replacement by the independently queried live UUID.")
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
    if bool(args.task) != bool(args.task_sha256):
        parser.error("--task and --task-sha256 are required together.")
    if args.task_sha256 and SHA256_RE.fullmatch(args.task_sha256) is None:
        parser.error("--task-sha256 must be one lowercase SHA-256 digest.")
    if args.expected_old_session_id:
        if UUID_RE.fullmatch(args.expected_old_session_id) is None:
            parser.error("--expected-old-session-id must be a Codex UUID.")
        if not args.apply or not args.task:
            parser.error("--expected-old-session-id requires --apply, --task, and --task-sha256.")
    return args


def authorized_human_targets(args: argparse.Namespace, root: Path) -> set[str]:
    if not getattr(args, "include_human_owned", False):
        return set()
    targets: set[str] = {str(target) for target in getattr(args, "human_target", ())}
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


def candidates(
    root: Path,
    allowed_human_targets: set[str] | None = None,
    candidate_paths: tuple[Path, ...] | None = None,
    expected_old_session_id: str = "",
) -> list[Path]:
    result = []
    targets: set[str] = set()
    for path in task_paths(root) if candidate_paths is None else candidate_paths:
        metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
        if metadata is None or metadata.tool != "codex" or metadata.session_id != expected_old_session_id:
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


def active_target_owners(root: Path, target: str) -> tuple[Path, ...]:
    owners: list[Path] = []
    canonical = canonical_target(target)
    for path in task_paths(root):
        metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
        if metadata is not None and metadata.status != "done" and canonical_target(metadata.runat) == canonical:
            owners.append(path.resolve())
    return tuple(sorted(owners))


def selected_candidates(
    root: Path,
    allowed_human_targets: set[str],
    task: str | None,
    task_sha256: str = "",
    expected_old_session_id: str = "",
) -> list[Path]:
    if task is None:
        if expected_old_session_id:
            raise ValueError("expected old session id requires one exact task binding")
        return candidates(root, allowed_human_targets)
    if expected_old_session_id and UUID_RE.fullmatch(expected_old_session_id) is None:
        raise ValueError("expected old session id must be one Codex UUID")
    task_ref = PurePosixPath(task)
    if not task or task_ref.is_absolute() or task_ref.as_posix() != task or any(part in {".", ".."} for part in task_ref.parts):
        raise ValueError("--task must be one normalized root-relative POSIX task path")
    selected: list[Path] = []
    for path in task_paths(root):
        try:
            relative = path.relative_to(root)
        except ValueError:
            # A malformed or concurrently stale TODO entry must not make an
            # exact, digest-bound selection inspect files outside this root.
            continue
        if relative.as_posix() == task:
            selected.append(path)
    if len(selected) != 1 or not task_sha256:
        raise ValueError("selected task is not exactly one eligible live Codex task")
    if hashlib.sha256(selected[0].read_bytes()).hexdigest() != task_sha256:
        raise ValueError("selected task digest changed before eligibility inspection")
    metadata = parse_task_metadata(selected[0].read_text(encoding="utf-8"), root)
    if metadata is None or active_target_owners(root, metadata.runat) != (selected[0].resolve(),):
        raise ValueError("selected task target does not have exactly one active owner")
    eligible = (
        candidates(root, allowed_human_targets, tuple(selected), expected_old_session_id)
        if expected_old_session_id
        else candidates(root, allowed_human_targets, tuple(selected))
    )
    if len(eligible) != 1:
        raise ValueError("selected task is not exactly one eligible live Codex task")
    return eligible


def run(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    allowed_human_targets = authorized_human_targets(args, root)
    expected_old_session_id = getattr(args, "expected_old_session_id", "")
    paths = selected_candidates(root, allowed_human_targets, getattr(args, "task", None), getattr(args, "task_sha256", ""), expected_old_session_id)
    if not args.apply:
        for path in paths:
            print(f"eligible\t{path.relative_to(root)}")
        print(f"dry-run: {len(paths)} eligible task(s)")
        return 0
    for path in paths:
        preliminary = parse_task_metadata(path.read_text(encoding="utf-8"), root)
        if preliminary is None:
            if expected_old_session_id:
                raise ValueError("selected task no longer has the asserted stale session UUID")
            continue
        if expected_old_session_id and preliminary.session_id != expected_old_session_id:
            raise ValueError("selected task no longer has the asserted stale session UUID")
        preliminary_target = preliminary.runat
        with ExitStack() as locks:
            locks.enter_context(tmux_input_lock(preliminary_target))
            if expected_old_session_id:
                locks.enter_context(root_membership_lock(root))
            locks.enter_context(task_target_lock(root, preliminary_target))
            if expected_old_session_id:
                for lock_path in sorted((root / "TODO.md", path), key=str):
                    locks.enter_context(task_file_lock(lock_path))
            else:
                locks.enter_context(task_file_lock(path))
            if expected_old_session_id and path not in task_paths(root):
                raise ValueError("selected task is no longer in an active TODO section")
            expected_sha256 = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            if getattr(args, "task_sha256", "") and expected_sha256 != args.task_sha256:
                raise ValueError("selected task digest changed under its task lock")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
            if metadata is None or metadata.tool != "codex" or metadata.session_id != expected_old_session_id or metadata.status == "done" or metadata.runat == "retired" or (metadata.runat.partition(":")[0].startswith("h") and metadata.runat not in allowed_human_targets):
                if expected_old_session_id:
                    raise ValueError("selected task no longer has the asserted stale session UUID")
                continue
            if metadata.runat != preliminary_target:
                if expected_old_session_id:
                    raise ValueError("selected task target changed before its lifecycle locks were acquired")
                print(f"skipped\t{path.relative_to(root)}\ttarget changed before its lifecycle locks were acquired", file=sys.stderr)
                continue
            if getattr(args, "task", None):
                owners = authoritative_active_target_task_paths(root, metadata.runat) if expected_old_session_id else active_target_owners(root, metadata.runat)
                if owners != (path.resolve(),):
                    raise ValueError("selected task target ownership changed under its target lock")
            try:
                pane = resolve_pane(metadata.runat)
            except Exception as exc:
                if expected_old_session_id:
                    raise ValueError(f"selected live owner became unavailable: {exc}") from exc
                print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tpane unavailable: {exc}", file=sys.stderr)
                continue
            if pane.command not in CODEX_PANE_COMMANDS:
                if expected_old_session_id:
                    raise ValueError("selected live owner is no longer a Codex pane")
                print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tnot a Codex pane", file=sys.stderr)
                continue
            report = inspect(StatusArgs(pane.target, 80))
            if report.status not in {"ready", "running"}:
                if expected_old_session_id:
                    raise ValueError("selected live owner is no longer ready or running")
                print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tnot ready or has input", file=sys.stderr)
                continue
            existing_input = current_input_text(report.lines).strip()
            if existing_input and not is_stock_placeholder_input_text(existing_input):
                if expected_old_session_id:
                    raise ValueError("stale-session repair requires an empty Codex composer")
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
                session_id = (
                    query_exact_status_session_id(pane, 240, 10.0, expected_old_session_id, require_complete_status_card=True)
                    if expected_old_session_id
                    else query_exact_status_session_id(pane, 240, 10.0)
                )
            except Exception as exc:
                if expected_old_session_id:
                    raise ValueError(f"live UUID query failed: {exc}") from exc
                print(f"skipped\t{path.relative_to(root)}\t{exc}", file=sys.stderr)
                continue
            if UUID_RE.fullmatch(session_id) is None:
                if expected_old_session_id:
                    raise ValueError("/status did not return a valid live UUID")
                print(f"skipped\t{path.relative_to(root)}\t/status did not return a valid UUID", file=sys.stderr)
                continue
            if expected_old_session_id and session_id.lower() == expected_old_session_id.lower():
                raise ValueError("independently queried live UUID still equals the asserted stale UUID")
            try:
                current = resolve_pane(metadata.runat)
            except Exception as exc:
                if expected_old_session_id:
                    raise ValueError(f"selected live owner became unavailable after capture: {exc}") from exc
                print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tpane unavailable after capture: {exc}", file=sys.stderr)
                continue
            if current != pane:
                if expected_old_session_id:
                    raise ValueError("complete pane identity changed during live UUID capture")
                print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\tcomplete pane identity changed during capture", file=sys.stderr)
                continue
            after = parse_task_metadata(path.read_text(encoding="utf-8"), root)
            if after is None or after.tool != "codex" or after.session_id != expected_old_session_id or after.status != metadata.status or after.status == "done" or after.runat != metadata.runat or after.runat == "retired" or (after.runat.partition(":")[0].startswith("h") and after.runat not in allowed_human_targets):
                if expected_old_session_id:
                    raise ValueError("selected task eligibility changed during live UUID capture")
                print(f"skipped\t{path.relative_to(root)}\t{metadata.runat}\ttask eligibility changed during capture", file=sys.stderr)
                continue
            if getattr(args, "task", None):
                owners = authoritative_active_target_task_paths(root, metadata.runat) if expected_old_session_id else active_target_owners(root, metadata.runat)
                if owners != (path.resolve(),):
                    raise ValueError("selected task target ownership changed before session UUID binding")
            if expected_old_session_id:
                record_session_id(path, session_id, expected_sha256, lock_held=True, replace_existing=True)
            else:
                record_session_id(path, session_id, expected_sha256, lock_held=True)
            print(f"migrated\t{path.relative_to(root)}\t{session_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args(sys.argv[1:])))
    except Exception as exc:
        print(f"omo_codex_session_migrate.py: {exc}", file=sys.stderr)
        raise SystemExit(2)
