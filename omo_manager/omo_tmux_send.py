#!/usr/bin/env python3
"""Paste file-backed text into a tmux target via a tmux buffer."""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from omo_manager.omo_codex_status import current_block, status, tail
except ModuleNotFoundError:
    from omo_codex_status import current_block, status, tail


@dataclass(frozen=True)
class Args:
    target: str
    message_file: Path | None
    enter_count: int
    enter_delay_s: float
    ready_timeout_s: float
    dry_run: bool
    pending_root: Path | None = None
    pending_file: Path | None = None
    pending_line: int = 0
    pending_digest: str = ""
    submit_verify_timeout_s: float = 0
    async_mode: bool = False
    async_notify_target: str = ""
    async_notify_enter_count: int = 1
    async_worker: bool = False
    async_cleanup_message_file: bool = False


class ParsedArgs(argparse.Namespace):
    target: str = ""
    message_file: Path | None = None
    enter: bool = False
    enter_count: int = 1
    enter_delay_s: float = 0.15
    ready_timeout_s: float = 0
    submit_verify_timeout_s: float = 5
    dry_run: bool = False
    pending_root: Path | None = None
    pending_file: Path | None = None
    pending_line: int = 0
    pending_digest: str = ""
    async_mode: bool = False
    async_notify_target: str = ""
    async_notify_enter_count: int = 1
    async_worker: bool = False
    async_cleanup_message_file: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--target", required=True, help="tmux target pane/window, e.g. cfg:1.0")
    _ = parser.add_argument("--message-file", type=Path, required=True, help="Read prompt text from this file.")
    enter_group = parser.add_mutually_exclusive_group()
    _ = enter_group.add_argument("--enter", dest="enter", action="store_true", help="Send Enter after pasting.")
    _ = enter_group.add_argument("--no-enter", dest="enter", action="store_false", help="Paste only; default.")
    _ = parser.add_argument("--enter-count", type=int, default=1, help="Number of Enter keys to send when submitting; default: 1.")
    _ = parser.add_argument("--enter-delay-s", type=float, default=0.15, help="Delay between repeated Enter keys; default: 0.15.")
    _ = parser.add_argument("--ready-timeout-s", type=float, default=0, help="When submitting to Codex, wait up to this many seconds for an idle input box before paste; default: 0.")
    _ = parser.add_argument("--submit-verify-timeout-s", type=float, default=5, help="After Enter, wait up to this many seconds to verify Codex no longer has the prompt in its input; default: 5.")
    _ = parser.add_argument("--dry-run", action="store_true", help="Validate inputs and print planned tmux actions without touching tmux.")
    _ = parser.add_argument("--pending-root", type=Path, help="Skip paste unless the pending marker still exists under this root.")
    _ = parser.add_argument("--pending-file", type=Path, help="Root-relative pending marker file.")
    _ = parser.add_argument("--pending-line", type=int, default=0, help="One-based pending marker line.")
    _ = parser.add_argument("--pending-digest", default="", help="Optional digest of the marker context.")
    _ = parser.add_argument(
        "--async",
        dest="async_mode",
        action="store_true",
        help="Return immediately and run the verified send in a background worker.",
    )
    _ = parser.add_argument("--async-notify-target", default="", help="Tmux target to notify when an async send completes.")
    _ = parser.add_argument("--async-notify-enter-count", type=int, default=1, help="Enter keys to send after the async completion notice; default: 1.")
    _ = parser.add_argument("--async-worker", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--async-cleanup-message-file", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(enter=False)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.enter_count < 1:
        parser.error("--enter-count must be positive.")
    if parsed.enter_delay_s < 0:
        parser.error("--enter-delay-s must be non-negative.")
    if parsed.ready_timeout_s < 0:
        parser.error("--ready-timeout-s must be non-negative.")
    if parsed.submit_verify_timeout_s < 0:
        parser.error("--submit-verify-timeout-s must be non-negative.")
    if parsed.async_notify_enter_count < 0:
        parser.error("--async-notify-enter-count must be non-negative.")
    if parsed.async_mode and not parsed.async_notify_target:
        parser.error("--async-notify-target is required with --async.")
    if any((parsed.pending_root, parsed.pending_file, parsed.pending_line)) and not all((parsed.pending_root, parsed.pending_file, parsed.pending_line > 0)):
        parser.error("--pending-root, --pending-file, and --pending-line must be passed together.")
    return Args(
        parsed.target,
        parsed.message_file,
        parsed.enter_count if parsed.enter else 0,
        parsed.enter_delay_s,
        parsed.ready_timeout_s if parsed.enter else 0,
        parsed.dry_run,
        parsed.pending_root,
        parsed.pending_file,
        parsed.pending_line,
        parsed.pending_digest,
        parsed.submit_verify_timeout_s if parsed.enter else 0,
        parsed.async_mode,
        parsed.async_notify_target,
        parsed.async_notify_enter_count,
        parsed.async_worker,
        parsed.async_cleanup_message_file,
    )


def read_message(args: Args) -> str:
    if args.message_file is None:
        raise RuntimeError("--message-file is required.")
    if not args.message_file.is_file():
        raise RuntimeError(f"message file not found: {args.message_file}")
    return args.message_file.read_text(encoding="utf-8")


def write_private_temp(message: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="omo-tmux-send.", text=True)
    path = Path(raw_path)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(message)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def wait_ready(args: Args) -> None:
    if args.ready_timeout_s <= 0:
        return
    deadline_s = time.monotonic() + args.ready_timeout_s
    last_status = "unknown"
    while True:
        lines = tail(args.target, 80)
        last_status = status(lines, current_block(lines))
        if last_status in {"ready", "not_codex"}:
            return
        if time.monotonic() >= deadline_s:
            raise RuntimeError(f"target not ready after {args.ready_timeout_s:g}s: {last_status}")
        time.sleep(min(0.5, max(0.05, deadline_s - time.monotonic())))


def pending_marker_present(args: Args) -> bool:
    if args.pending_root is None or args.pending_file is None:
        return True
    path = args.pending_root / args.pending_file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    idx = args.pending_line - 1
    if idx < 0 or idx >= len(lines) or lines[idx].strip() != "(pending)":
        return False
    if not args.pending_digest:
        return True
    next_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
    digest = hashlib.sha256(f"{args.pending_file}:{args.pending_line}:{next_line}".encode("utf-8")).hexdigest()[:16]
    return digest == args.pending_digest


def message_probe(message: str) -> str:
    for line in message.splitlines():
        probe = line.strip()
        if probe:
            return probe[:80]
    return ""


def message_probes(message: str) -> list[str]:
    probes: list[str] = []
    for line in message.splitlines():
        probe = line.strip()
        if probe:
            probes.append(probe[:80])
    if len(probes) <= 2:
        return probes
    return [probes[0], probes[-1]]


def inspect_lines_for_message(message: str) -> int:
    return min(2000, max(80, len(message.splitlines()) + 20))


def current_input_text(lines: list[str]) -> str:
    body = lines[:-1] if lines and lines[-1].startswith("  gpt-") else lines[:]
    while body and not body[-1].strip():
        body.pop()
    for idx in range(len(body) - 1, -1, -1):
        line = body[idx].lstrip()
        if line.startswith("›"):
            input_lines = body[idx:]
            if any(after.startswith(("• ", "│", "└", "├", "─")) for after in input_lines[1:]):
                return ""
            text_lines = [line[1:].strip()]
            text_lines.extend(after.rstrip() for after in input_lines[1:])
            text = "\n".join(text_lines).strip()
            return "" if text == "Use /skills to list available skills" else text
    return ""


def input_has_probe(lines: list[str], probe: str) -> bool:
    return bool(probe and probe in current_input_text(lines))


def input_has_any_probe(lines: list[str], probes: list[str]) -> bool:
    input_text = current_input_text(lines)
    return any(probe in input_text for probe in probes)


def send_enter(target: str) -> None:
    _ = subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], timeout=5, check=True)


def wait_paste_visible(args: Args, message: str) -> None:
    if args.enter_count <= 0 or args.submit_verify_timeout_s <= 0:
        return
    probes = message_probes(message)
    if not probes:
        return
    n_lines = inspect_lines_for_message(message)
    deadline_s = time.monotonic() + args.submit_verify_timeout_s
    last_status = "unknown"
    last_input = ""
    while True:
        lines = tail(args.target, n_lines)
        last_status = status(lines, current_block(lines))
        if last_status == "not_codex" or input_has_any_probe(lines, probes):
            return
        last_input = current_input_text(lines)
        now_s = time.monotonic()
        if now_s >= deadline_s:
            suffix = "input box has different text" if last_input else "prompt not visible in input"
            raise RuntimeError(f"Codex paste not verified after {args.submit_verify_timeout_s:g}s: {suffix}, status={last_status}")
        time.sleep(min(0.25, max(0.05, deadline_s - now_s)))


def verify_submit(args: Args, message: str) -> None:
    if args.enter_count <= 0 or args.submit_verify_timeout_s <= 0:
        return
    probe = message_probe(message)
    if not probe:
        return
    n_lines = inspect_lines_for_message(message)
    deadline_s = time.monotonic() + args.submit_verify_timeout_s
    last_status = "unknown"
    next_enter_s = 0.0
    while True:
        lines = tail(args.target, n_lines)
        last_status = status(lines, current_block(lines))
        if last_status == "not_codex":
            return
        input_text = current_input_text(lines)
        if not input_text:
            return
        now_s = time.monotonic()
        if probe in input_text and now_s >= next_enter_s:
            send_enter(args.target)
            next_enter_s = now_s + max(args.enter_delay_s, 0.25)
        if now_s >= deadline_s:
            suffix = "prompt still in input" if probe in input_text else "input box still has text"
            raise RuntimeError(f"Codex submit not verified after {args.submit_verify_timeout_s:g}s: {suffix}, status={last_status}")
        time.sleep(min(0.25, max(0.05, min(deadline_s, next_enter_s) - now_s)))


def run_tmux(args: Args, message: str) -> None:
    temp_path = write_private_temp(message)
    buffer_name = f"omo-tmux-send-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        if args.dry_run:
            _ = print(f"would load tmux buffer {buffer_name} from {temp_path}")
            _ = print(f"would paste buffer {buffer_name} to {args.target}")
            for _ in range(args.enter_count):
                _ = print(f"would send Enter to {args.target}")
            return
        wait_ready(args)
        if not pending_marker_present(args):
            raise RuntimeError("pending marker cleared before tmux paste")
        if args.enter_count:
            _ = subprocess.run(["tmux", "send-keys", "-t", args.target, "C-u"], timeout=5, check=True)
        _ = subprocess.run(["tmux", "load-buffer", "-b", buffer_name, str(temp_path)], timeout=5, check=True)
        _ = subprocess.run(["tmux", "paste-buffer", "-b", buffer_name, "-t", args.target], timeout=5, check=True)
        wait_paste_visible(args, message)
        for idx in range(args.enter_count):
            if idx:
                time.sleep(args.enter_delay_s)
            send_enter(args.target)
        verify_submit(args, message)
    finally:
        temp_path.unlink(missing_ok=True)
        if not args.dry_run:
            _ = subprocess.run(["tmux", "delete-buffer", "-b", buffer_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)


def worker_argv(args: Args, payload_file: Path) -> list[str]:
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--target",
        args.target,
        "--message-file",
        str(payload_file),
        "--async-worker",
        "--async-cleanup-message-file",
        "--async-notify-target",
        args.async_notify_target,
        "--async-notify-enter-count",
        str(args.async_notify_enter_count),
    ]
    if args.enter_count:
        argv.extend(
            [
                "--enter",
                "--enter-count",
                str(args.enter_count),
                "--enter-delay-s",
                str(args.enter_delay_s),
                "--submit-verify-timeout-s",
                str(args.submit_verify_timeout_s),
                "--ready-timeout-s",
                str(args.ready_timeout_s),
            ]
        )
    if args.pending_root is not None and args.pending_file is not None:
        argv.extend(
            [
                "--pending-root",
                str(args.pending_root),
                "--pending-file",
                str(args.pending_file),
                "--pending-line",
                str(args.pending_line),
            ]
        )
    if args.pending_digest:
        argv.extend(["--pending-digest", args.pending_digest])
    return argv


def launch_async(args: Args, message: str) -> None:
    payload_file = write_private_temp(message)
    if args.dry_run:
        _ = print(f"would start async tmux send using {payload_file}")
        _ = print(f"would notify {args.async_notify_target} after completion")
        payload_file.unlink(missing_ok=True)
        return
    try:
        proc = subprocess.Popen(
            worker_argv(args, payload_file),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        payload_file.unlink(missing_ok=True)
        raise
    _ = print(f"omo_tmux_send: async worker pid={proc.pid}")


def async_result_message(args: Args, ok: bool, result: str) -> str:
    status_text = "succeeded" if ok else "failed"
    return f"Previous async omo_tmux_send command {status_text} for {args.target}.\nResult: {result}\n"


def notify_async_result(args: Args, ok: bool, result: str) -> None:
    if not args.async_notify_target:
        return
    message = async_result_message(args, ok, result)
    notify_path = write_private_temp(message)
    try:
        notify_args = Args(args.async_notify_target, notify_path, args.async_notify_enter_count, args.enter_delay_s, 0, False)
        run_tmux(notify_args, message)
    finally:
        notify_path.unlink(missing_ok=True)


def run_async_worker(args: Args) -> int:
    ok = True
    result = "sent"
    try:
        run_tmux(args, read_message(args))
    except Exception as exc:
        ok = False
        result = str(exc)
    finally:
        if args.async_cleanup_message_file and args.message_file is not None:
            args.message_file.unlink(missing_ok=True)
    try:
        notify_async_result(args, ok, result)
    except Exception as exc:
        print(f"omo_tmux_send async notify failed: {exc}", file=sys.stderr)
        if ok:
            return 1
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.async_mode:
            launch_async(args, read_message(args))
            return 0
        if args.async_worker:
            return run_async_worker(args)
        run_tmux(args, read_message(args))
    except Exception as exc:
        print(f"omo_tmux_send: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
