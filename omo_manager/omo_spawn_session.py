#!/usr/bin/env python3
"""Spawn an OpenCode TUI in tmux with an explicit localhost port."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"


DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_REGISTRY = Path(os.environ.get("OMO_MANAGER_SESSION_REGISTRY", default_state_dir() / "sessions.json"))


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: str
    tmux_target: str
    workdir: Path
    port: int
    prompt_file: Path | None
    registry: Path
    session_id: str
    dry_run: bool
    force: bool


@dataclass(frozen=True)
class SessionRecord:
    task_file: str
    tmux_target: str
    workdir: str
    port: int
    url: str
    started_at_s: float
    prompt_file: str
    session_id: str


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task_file: str = ""
    tmux_target: str = ""
    workdir: Path = Path.cwd()
    port: int = 0
    prompt_file: Path | None = None
    registry: Path = DEFAULT_REGISTRY
    session_id: str = ""
    dry_run: bool = False
    force: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--task-file", required=True)
    _ = parser.add_argument("--tmux-target", required=True, help="Existing pane, e.g. `cfg:2.0`.")
    _ = parser.add_argument("--workdir", type=Path, default=Path.cwd())
    _ = parser.add_argument("--port", type=int, required=True)
    _ = parser.add_argument("--prompt-file", type=Path)
    _ = parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    _ = parser.add_argument("--session-id", default="", help="Known OpenCode session id for later stuck-history inspection.")
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--force", action="store_true", help="Send Ctrl-C before starting OpenCode in the pane.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.port < 1 or parsed.port > 65535:
        parser.error("--port must be 1..65535.")
    return Args(parsed.root.resolve(), parsed.task_file, parsed.tmux_target, parsed.workdir.resolve(), parsed.port, parsed.prompt_file, parsed.registry, parsed.session_id, parsed.dry_run, parsed.force)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def tmux_pane_exists(target: str) -> bool:
    out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"], capture_output=True, text=True, timeout=5, check=False)
    return out.returncode == 0 and target in out.stdout.splitlines()


def health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/global/health", timeout=1.0) as resp:
            status = int(resp.status)
            return status == 200
    except Exception:
        return False


def load_registry(path: Path) -> dict[str, object]:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {"sessions": []}
    return data if isinstance(data, dict) else {"sessions": []}


def save_record(path: Path, record: SessionRecord) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != Path("/tmp"):
        path.parent.chmod(0o700)
    data = load_registry(path)
    raw_sessions = data.get("sessions")
    sessions = raw_sessions if isinstance(raw_sessions, list) else []
    sessions = [item for item in sessions if not (isinstance(item, dict) and item.get("task_file") == record.task_file)]
    sessions.append(asdict(record))
    data["sessions"] = sessions
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        _ = handle.write("\n")


def command(args: Args) -> str:
    return f"cd {shlex.quote(str(args.workdir))} && opencode --hostname 127.0.0.1 --port {args.port} ."


def post_prompt(port: int, workdir: Path, prompt_file: Path) -> None:
    prompt = prompt_file.read_text(encoding="utf-8")
    query = urllib.parse.urlencode({"directory": str(workdir)})
    base_url = f"http://127.0.0.1:{port}"
    for route, payload in (("/tui/append-prompt", {"text": prompt}), ("/tui/submit-prompt", {})):
        req = urllib.request.Request(f"{base_url}{route}?{query}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            _ = resp.read()


def spawn(args: Args) -> None:
    cmd = command(args)
    if args.dry_run:
        print(cmd)
        if args.prompt_file:
            print(f"post-prompt-file={args.prompt_file}")
        return
    if not tmux_pane_exists(args.tmux_target):
        raise RuntimeError(f"tmux pane not found: {args.tmux_target}")
    if not port_free(args.port):
        raise RuntimeError(f"port already in use: {args.port}")
    if args.force:
        _ = subprocess.run(["tmux", "send-keys", "-t", args.tmux_target, "C-c"], timeout=5, check=True)
    _ = subprocess.run(["tmux", "send-keys", "-t", args.tmux_target, cmd, "Enter"], timeout=5, check=True)
    deadline_s = time.monotonic() + 15.0
    while time.monotonic() < deadline_s:
        if health_ok(args.port):
            if args.prompt_file:
                post_prompt(args.port, args.workdir, args.prompt_file)
            return
        time.sleep(0.25)
    raise RuntimeError(f"OpenCode did not become healthy on port {args.port}")


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        spawn(args)
        record = SessionRecord(args.task_file, args.tmux_target, str(args.workdir), args.port, f"http://127.0.0.1:{args.port}", time.time(), str(args.prompt_file or ""), args.session_id)
        if not args.dry_run:
            save_record(args.registry, record)
        print(record.url)
    except Exception as exc:
        print(f"omo_spawn_session: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
