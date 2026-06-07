#!/usr/bin/env python3
"""Wait for a Codex tmux target to become prompt-ready, then send `/compact`."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_status import Args as StatusArgs
from omo_manager.omo_codex_status import Report, inspect
from omo_manager.omo_tmux_send import Args as SendArgs
from omo_manager.omo_tmux_send import run_tmux


COMPACT_MESSAGE = "/compact\n"


@dataclass(frozen=True)
class Args:
    target: str
    timeout_s: float
    interval_s: float
    lines: int
    background: bool
    worker: bool
    notify_target: str
    notify_enter_count: int
    log_file: Path | None
    submit_verify_timeout_s: float


class ParsedArgs(argparse.Namespace):
    target: str = ""
    timeout_s: float = 1800.0
    interval_s: float = 5.0
    lines: int = 80
    background: bool = False
    worker: bool = False
    notify_target: str = ""
    notify_enter_count: int = 1
    log_file: Path | None = None
    submit_verify_timeout_s: float = 5.0


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--target", required=True, help="Codex tmux target pane/window, e.g. cfg:1.0.")
    _ = parser.add_argument("--timeout-s", type=float, default=1800.0, help="Maximum seconds to wait for a ready Codex prompt; default: 1800.")
    _ = parser.add_argument("--interval-s", type=float, default=5.0, help="Polling interval in seconds; default: 5.")
    _ = parser.add_argument("--lines", type=int, default=80, help="Codex status tail lines to inspect; default: 80.")
    _ = parser.add_argument("--background", action="store_true", help="Start a detached worker and return immediately.")
    _ = parser.add_argument("--notify-target", default="", help="Optional tmux target to notify after success or failure.")
    _ = parser.add_argument("--notify-enter-count", type=int, default=1, help="Enter keys to send after the optional notification; default: 1.")
    _ = parser.add_argument("--log-file", type=Path, help="Background worker stdout/stderr file; default: private temp log.")
    _ = parser.add_argument("--submit-verify-timeout-s", type=float, default=5.0, help="Seconds to verify `/compact` left the input after Enter; default: 5.")
    _ = parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.timeout_s < 0:
        parser.error("--timeout-s must be non-negative.")
    if parsed.interval_s <= 0:
        parser.error("--interval-s must be positive.")
    if parsed.lines <= 0:
        parser.error("--lines must be positive.")
    if parsed.notify_enter_count < 0:
        parser.error("--notify-enter-count must be non-negative.")
    if parsed.submit_verify_timeout_s < 0:
        parser.error("--submit-verify-timeout-s must be non-negative.")
    if parsed.background and parsed.worker:
        parser.error("--background and --worker cannot be combined.")
    return Args(
        parsed.target,
        parsed.timeout_s,
        parsed.interval_s,
        parsed.lines,
        parsed.background,
        parsed.worker,
        parsed.notify_target,
        parsed.notify_enter_count,
        parsed.log_file,
        parsed.submit_verify_timeout_s,
    )


def wait_until_ready(args: Args) -> Report:
    deadline_s = time.monotonic() + args.timeout_s
    last_report = Report("unknown", [])
    while True:
        last_report = inspect(StatusArgs(args.target, args.lines))
        if last_report.status == "ready":
            return last_report
        remaining_s = deadline_s - time.monotonic()
        if remaining_s <= 0:
            raise RuntimeError(f"target {args.target} was not ready after {args.timeout_s:g}s; last status: {last_report.status}")
        time.sleep(min(args.interval_s, remaining_s))


def send_compact(args: Args) -> None:
    report = wait_until_ready(args)
    if report.status != "ready":
        raise RuntimeError(f"target {args.target} is not ready; status: {report.status}")
    send_args = SendArgs(args.target, None, 1, 0.15, 0, False, submit_verify_timeout_s=args.submit_verify_timeout_s)
    run_tmux(send_args, COMPACT_MESSAGE)


def result_message(args: Args, ok: bool, result: str) -> str:
    status = "succeeded" if ok else "failed"
    return f"Codex compact-when-idle {status} for {args.target}.\nResult: {result}\n"


def notify(args: Args, ok: bool, result: str) -> None:
    if not args.notify_target:
        return
    notify_args = SendArgs(args.notify_target, None, args.notify_enter_count, 0.15, 0, False)
    run_tmux(notify_args, result_message(args, ok, result))


def worker_argv(args: Args) -> list[str]:
    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--target",
        args.target,
        "--timeout-s",
        str(args.timeout_s),
        "--interval-s",
        str(args.interval_s),
        "--lines",
        str(args.lines),
        "--submit-verify-timeout-s",
        str(args.submit_verify_timeout_s),
        "--worker",
    ]
    if args.notify_target:
        argv.extend(["--notify-target", args.notify_target, "--notify-enter-count", str(args.notify_enter_count)])
    return argv


def default_log_file() -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="omo-codex-compact.", suffix=".log", text=True)
    os.close(fd)
    path = Path(raw_path)
    os.chmod(path, 0o600)
    return path


def launch_background(args: Args) -> None:
    log_file = args.log_file or default_log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab", buffering=0) as log:
        proc = subprocess.Popen(
            worker_argv(args),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    print(f"omo_codex_compact_when_idle: worker pid={proc.pid} log={log_file}")


def run_worker(args: Args) -> int:
    ok = True
    result = "sent /compact"
    try:
        send_compact(args)
    except Exception as exc:
        ok = False
        result = str(exc)
    try:
        notify(args, ok, result)
    except Exception as exc:
        print(f"omo_codex_compact_when_idle notify failed: {exc}", file=sys.stderr)
        if ok:
            return 1
    print(result_message(args, ok, result), end="")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.background:
            launch_background(args)
            return 0
        return run_worker(args)
    except Exception as exc:
        print(f"omo_codex_compact_when_idle: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
