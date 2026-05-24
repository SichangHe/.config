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
from typing import cast


def default_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"


DEFAULT_ROOT = Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs"))
DEFAULT_REGISTRY = Path(os.environ.get("OMO_MANAGER_SESSION_REGISTRY", default_state_dir() / "sessions.json"))
SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}


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
    prompt_file = parsed.prompt_file.resolve(strict=False) if parsed.prompt_file else None
    return Args(parsed.root.resolve(), parsed.task_file, parsed.tmux_target, parsed.workdir.resolve(), parsed.port, prompt_file, parsed.registry, parsed.session_id, parsed.dry_run, parsed.force)


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_port_free(port: int, deadline_s: float) -> bool:
    while time.monotonic() < deadline_s:
        if port_free(port):
            return True
        time.sleep(0.25)
    return False


def tmux_pane_exists(target: str) -> bool:
    out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"], capture_output=True, text=True, timeout=5, check=False)
    return out.returncode == 0 and target in out.stdout.splitlines()


def tmux_current_command(target: str) -> str:
    out = subprocess.run(["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"], capture_output=True, text=True, timeout=5, check=False)
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def tmux_target_pid(target: str) -> int | None:
    out = subprocess.run(["tmux", "display-message", "-p", "-t", target, "#{pane_pid}"], capture_output=True, text=True, timeout=5, check=False)
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip())
    except ValueError:
        return None


def port_listener_pids(port: int) -> set[int]:
    out = subprocess.run(["ss", "-ltnp", f"sport = :{port}"], capture_output=True, text=True, timeout=5, check=False)
    if out.returncode != 0:
        return set()
    pids: set[int] = set()
    for part in out.stdout.split("pid=")[1:]:
        raw_pid = part.split(",", 1)[0]
        try:
            pids.add(int(raw_pid))
        except ValueError:
            continue
    return pids


def is_descendant_process(pid: int, ancestor_pid: int) -> bool:
    current = pid
    seen: set[int] = set()
    while current > 1 and current not in seen:
        if current == ancestor_pid:
            return True
        seen.add(current)
        try:
            stat = Path(f"/proc/{current}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        try:
            current = int(stat.rsplit(")", 1)[1].split()[1])
        except (IndexError, ValueError):
            return False
    return False


def port_owned_by_target(port: int, target: str) -> bool:
    target_pid = tmux_target_pid(target)
    pids = port_listener_pids(port)
    if target_pid is None or not pids:
        return False
    return all(is_descendant_process(pid, target_pid) for pid in pids)


def wait_pane_shell_ready(target: str, deadline_s: float) -> bool:
    while time.monotonic() < deadline_s:
        if tmux_current_command(target) in SHELL_COMMANDS:
            return True
        time.sleep(0.25)
    return False


def require_pane_shell_ready(target: str) -> None:
    command_name = tmux_current_command(target)
    if command_name not in SHELL_COMMANDS:
        raise RuntimeError(f"tmux pane {target} is running {command_name or 'unknown'}, not a shell; refusing to type spawn command")


def health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/global/health", timeout=1.0) as resp:
            status = int(resp.status)
            return status == 200
    except Exception:
        return False


def session_timestamp_ms(raw_session: dict[str, object], field: str) -> int:
    raw_time = raw_session.get("time")
    if not isinstance(raw_time, dict):
        return 0
    value = raw_time.get(field)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def fetch_sessions(port: int, workdir: Path) -> list[dict[str, object]]:
    try:
        query = urllib.parse.urlencode({"directory": str(workdir)})
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/session?{query}", timeout=2.0) as resp:
            raw_sessions: object = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if not isinstance(raw_sessions, list):
        return []
    workdir_text = str(workdir)
    sessions: list[dict[str, object]] = []
    for raw_session in raw_sessions:
        if isinstance(raw_session, dict) and raw_session.get("directory") == workdir_text:
            sessions.append(cast(dict[str, object], raw_session))
    return sessions


def session_ids(sessions: list[dict[str, object]]) -> set[str]:
    ids: set[str] = set()
    for session in sessions:
        raw_id = session.get("id")
        if isinstance(raw_id, str):
            ids.add(raw_id)
    return ids


def latest_new_session_id(port: int, workdir: Path, known_ids: set[str], since_ms: int) -> str:
    latest_id = ""
    latest_created = 0
    for session in fetch_sessions(port, workdir):
        raw_id = session.get("id")
        if not isinstance(raw_id, str) or raw_id in known_ids:
            continue
        created = session_timestamp_ms(session, "created")
        if created >= since_ms and created >= latest_created:
            latest_id = raw_id
            latest_created = created
    return latest_id


def wait_new_session_id(port: int, workdir: Path, known_ids: set[str], since_ms: int, deadline_s: float) -> str:
    while time.monotonic() < deadline_s:
        session_id = latest_new_session_id(port, workdir, known_ids, since_ms)
        if session_id:
            return session_id
        time.sleep(0.5)
    return ""


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
    prompt = ""
    prompt_arg = ""
    if args.prompt_file:
        prompt_file = shlex.quote(str(args.prompt_file))
        prompt = f"test -f {prompt_file} && "
        prompt_arg = f" --prompt \"$(cat -- {prompt_file})\""
    return f"cd {shlex.quote(str(args.workdir))} && {prompt}opencode --hostname 127.0.0.1 --port {args.port}{prompt_arg} ."


def spawn(args: Args) -> str:
    cmd = command(args)
    if args.dry_run:
        print(cmd)
        return ""
    if not tmux_pane_exists(args.tmux_target):
        raise RuntimeError(f"tmux pane not found: {args.tmux_target}")
    if args.prompt_file and not args.prompt_file.is_file():
        raise RuntimeError(f"prompt file not found: {args.prompt_file}")
    if args.force:
        if not port_free(args.port) and not port_owned_by_target(args.port, args.tmux_target):
            raise RuntimeError(f"port {args.port} is not owned by tmux target {args.tmux_target}; refusing --force restart")
        _ = subprocess.run(["tmux", "send-keys", "-t", args.tmux_target, "C-c"], timeout=5, check=True)
        if not wait_pane_shell_ready(args.tmux_target, time.monotonic() + 10.0):
            raise RuntimeError(f"tmux pane {args.tmux_target} did not return to a shell-like command after Ctrl-C")
        if not wait_port_free(args.port, time.monotonic() + 10.0):
            raise RuntimeError(f"port still in use after stopping {args.tmux_target}: {args.port}")
    else:
        require_pane_shell_ready(args.tmux_target)
        if not port_free(args.port):
            raise RuntimeError(f"port already in use: {args.port}")
    known_session_ids = session_ids(fetch_sessions(args.port, args.workdir)) if args.prompt_file else set()
    start_ms = int(time.time() * 1000)
    _ = subprocess.run(["tmux", "send-keys", "-t", args.tmux_target, cmd, "Enter"], timeout=5, check=True)
    deadline_s = time.monotonic() + 15.0
    while time.monotonic() < deadline_s:
        if health_ok(args.port):
            if args.prompt_file:
                session_id = wait_new_session_id(args.port, args.workdir, known_session_ids, start_ms - 1000, time.monotonic() + 60.0)
                if not session_id:
                    raise RuntimeError(f"OpenCode became healthy on port {args.port}, but no new session appeared for {args.workdir}")
                return session_id
            return ""
        time.sleep(0.25)
    raise RuntimeError(f"OpenCode did not become healthy on port {args.port}")


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        detected_session_id = spawn(args)
        if args.session_id:
            detected_session_id = args.session_id
        record = SessionRecord(args.task_file, args.tmux_target, str(args.workdir), args.port, f"http://127.0.0.1:{args.port}", time.time(), str(args.prompt_file or ""), detected_session_id)
        if not args.dry_run:
            save_record(args.registry, record)
        print(record.url)
    except Exception as exc:
        print(f"omo_spawn_session: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
