#!/usr/bin/env python3
"""Launch, inspect, message, and stop OmniGent-backed agent sessions."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_task_metadata import OMNIGENT_RUNAT_RE

DEFAULT_SERVER_URL = "http://127.0.0.1:6767"
DEFAULT_TIMEOUT_S = 30.0
ManagerStatus = Literal["ready", "running", "error", "missing"]


class SessionNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    status: str
    harness: str
    runner_online: bool | None
    host_online: bool | None


def server_url() -> str:
    return os.environ.get("OMO_MANAGER_OMNIGENT_URL", DEFAULT_SERVER_URL).rstrip("/")


def session_id(target: str) -> str:
    match = OMNIGENT_RUNAT_RE.fullmatch(target)
    if match is None:
        raise RuntimeError("OmniGent target must be `omnigent://SESSION_ID`")
    return match.group(1)


def target_for_session(value: str) -> str:
    target = f"omnigent://{value}"
    _ = session_id(target)
    return target


def request_json(method: str, path: str, payload: object | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> object:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    token = os.environ.get("OMO_MANAGER_OMNIGENT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{server_url()}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        if exc.code == 404 and path.startswith("/v1/sessions/"):
            missing_id = path.removeprefix("/v1/sessions/").partition("?")[0].partition("/")[0]
            raise SessionNotFoundError(f"OmniGent session not found: {missing_id}") from exc
        raise RuntimeError(f"OmniGent {method} {path} failed with HTTP {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"OmniGent server unavailable at {server_url()}: {exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OmniGent {method} {path} returned invalid JSON") from exc


def require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RuntimeError(f"OmniGent {label} response is not an object")
    return value


def require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"OmniGent response lacks {label}")
    return value


def session_snapshot(target: str) -> SessionSnapshot:
    value = require_mapping(
        request_json("GET", f"/v1/sessions/{urllib.parse.quote(session_id(target), safe='')}?include_items=false&refresh_state=true"),
        "session",
    )
    runner_online = value.get("runner_online")
    host_online = value.get("host_online")
    if runner_online is not None and not isinstance(runner_online, bool):
        raise RuntimeError("OmniGent session `runner_online` is not boolean or null")
    if host_online is not None and not isinstance(host_online, bool):
        raise RuntimeError("OmniGent session `host_online` is not boolean or null")
    harness = value.get("harness")
    return SessionSnapshot(
        require_text(value.get("id"), "session id"),
        require_text(value.get("status"), "session status"),
        harness if isinstance(harness, str) else "",
        runner_online,
        host_online,
    )


def manager_status(snapshot: SessionSnapshot) -> ManagerStatus:
    if snapshot.status == "failed":
        return "error"
    if snapshot.runner_online is not True:
        return "missing"
    if snapshot.status in {"running", "waiting"}:
        return "running"
    if snapshot.status == "idle":
        return "ready"
    return "missing"


def status_evidence(snapshot: SessionSnapshot) -> str:
    def flag(value: bool | None) -> str:
        return "unknown" if value is None else str(value).lower()

    harness = snapshot.harness or "unknown"
    return f"runtime=omnigent session_status={snapshot.status} harness={harness} runner_online={flag(snapshot.runner_online)} host_online={flag(snapshot.host_online)}"


def send_message(target: str, message: str, *, dry_run: bool = False) -> None:
    if not message:
        raise RuntimeError("OmniGent message must not be empty")
    if dry_run:
        print(f"would send {len(message.encode())} bytes to {target} through OmniGent")
        return
    payload = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": message}]},
    }
    value = require_mapping(
        request_json("POST", f"/v1/sessions/{urllib.parse.quote(session_id(target), safe='')}/events", payload),
        "message delivery",
    )
    if value.get("queued") is not True:
        raise RuntimeError("OmniGent did not acknowledge queued message delivery")


def stop_session(target: str, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"would stop {target} through OmniGent")
        return
    value = require_mapping(
        request_json("POST", f"/v1/sessions/{urllib.parse.quote(session_id(target), safe='')}/events", {"type": "stop_session", "data": {}}),
        "session stop",
    )
    if value.get("queued") is not False:
        raise RuntimeError("OmniGent did not acknowledge session stop")


def agent_id_for_tool(tool: str) -> str:
    expected_name = f"{tool}-native-ui"
    expected_harness = f"{tool}-native"
    value = require_mapping(request_json("GET", "/v1/agents?limit=1000"), "agent list")
    agents = value.get("data")
    if not isinstance(agents, list):
        raise RuntimeError("OmniGent agent list lacks `data`")
    matches = [
        agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("name") == expected_name and agent.get("harness") == expected_harness
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"OmniGent requires exactly one registered `{expected_name}` agent with `{expected_harness}` harness, found {len(matches)}"
        )
    return require_text(matches[0].get("id"), "agent id")


def online_host_id(explicit_host_id: str = "") -> str:
    configured = explicit_host_id.strip() or os.environ.get("OMO_MANAGER_OMNIGENT_HOST_ID", "").strip()
    if configured:
        return configured
    value = require_mapping(request_json("GET", "/v1/hosts"), "host list")
    hosts = value.get("hosts")
    if not isinstance(hosts, list):
        raise RuntimeError("OmniGent host list lacks `hosts`")
    online = [host for host in hosts if isinstance(host, dict) and host.get("status") == "online"]
    if len(online) != 1:
        raise RuntimeError(f"OmniGent launch requires one online host or OMO_MANAGER_OMNIGENT_HOST_ID, found {len(online)} online hosts")
    return require_text(online[0].get("host_id"), "host id")


# 🧑 "Implement at least the launch, stop, messaging, status checks, how else would we be able to try it?"
def launch_session(tool: str, workdir: Path, model: str, reasoning_effort: str, *, host_id: str = "", title: str = "", dry_run: bool = False) -> str:
    if tool not in {"codex", "cursor"}:
        raise RuntimeError("OmniGent launch currently supports the actual `codex` and `cursor` harnesses")
    if dry_run:
        print(f"would launch OmniGent-backed {tool} in {workdir}")
        return "omnigent://DRYRUN"
    payload = {
        "agent_id": agent_id_for_tool(tool),
        "host_id": online_host_id(host_id),
        "workspace": str(workdir),
        "title": title or None,
        "model_override": model or None,
        "reasoning_effort": reasoning_effort or None,
    }
    value = require_mapping(request_json("POST", "/v1/sessions", payload), "session launch")
    return target_for_session(require_text(value.get("id"), "launched session id"))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="")
    parser.add_argument("--dry-run", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("launch")
    launch.add_argument("--tool", choices=("codex", "cursor"), required=True)
    launch.add_argument("--workdir", type=Path, required=True)
    launch.add_argument("--model", required=True)
    launch.add_argument("--reasoning-effort", required=True)
    launch.add_argument("--host-id", default="")
    launch.add_argument("--title", default="")
    send = commands.add_parser("send")
    send.add_argument("--message-file", type=Path, required=True)
    _ = commands.add_parser("status")
    _ = commands.add_parser("stop")
    parsed = parser.parse_args(argv)
    if parsed.command != "launch" and not parsed.target:
        parser.error("--target is required for send, status, and stop")
    return parsed


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        if args.command == "launch":
            print(launch_session(args.tool, args.workdir.resolve(), args.model, args.reasoning_effort, host_id=args.host_id, title=args.title, dry_run=args.dry_run))
        elif args.command == "send":
            send_message(args.target, args.message_file.read_text(encoding="utf-8"), dry_run=args.dry_run)
        elif args.command == "stop":
            stop_session(args.target, dry_run=args.dry_run)
        else:
            snapshot = session_snapshot(args.target)
            print(f"{manager_status(snapshot)}: target={args.target} {status_evidence(snapshot)}")
    except (OSError, RuntimeError) as exc:
        print(f"omo_omnigent: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
