#!/usr/bin/env python3
"""Launch, inspect, message, and stop OmniGent-backed agent sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_task_metadata import ANTIGRAVITY_EFFORTS, OMNIGENT_RUNAT_RE, OMNIGENT_TOOLS

DEFAULT_SERVER_URL = "http://127.0.0.1:6767"
DEFAULT_TIMEOUT_S = 30.0
FULL_ACCESS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
FULL_ACCESS_LABEL = "omnigent.codex_native.bypass_sandbox"
CURSOR_TERMINAL_LAUNCH_ARGS = ("--force", "--sandbox", "disabled", "--trust")
HOST_ONLINE_WAIT_S = 45.0
HOST_ONLINE_POLL_S = 1.0
AGY_BIND_WAIT_S = 30.0
AGY_BIND_POLL_S = 0.5
AGY_PLACEHOLDER_PREFIX = "agy_conv_"
AGY_CONVERSATION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
AGY_EXPECTED_TURN_FILE = "omo-manager-expected-turn.json"
REPORT_HELPER_RE = re.compile(r"(?:^|[\s\"'`=(/])(?:omo_report\.sh|email_me\.py)(?:[\s\"'`)]|$)")
TOOL_ITEM_TYPES = frozenset({"function_call", "function_call_output"})
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
    status = require_text(value.get("status"), "session status")
    if harness == "antigravity-native" and status == "running" and antigravity_terminal_ready(target):
        status = "idle"
    return SessionSnapshot(
        require_text(value.get("id"), "session id"),
        status,
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


def session_ready_report(target: str) -> tuple[str, bool] | None:
    """Return the newest completed assistant item id and whether that turn invoked a report helper."""
    capture = antigravity_terminal_capture(target)
    ready = _expected_antigravity_turn(target, capture or "")
    if ready is not None:
        turn, delivery_id = ready
        return hashlib.sha256(f"{delivery_id}\0{turn}".encode()).hexdigest(), False
    value = require_mapping(
        request_json("GET", f"/v1/sessions/{urllib.parse.quote(session_id(target), safe='')}/items?limit=100&order=desc"),
        "session items",
    )
    data = value.get("data")
    if not isinstance(data, list):
        return None
    nearby = False
    assistant_id = None
    for item in data:
        if not isinstance(item, dict) or item.get("status") != "completed":
            continue
        if assistant_id is None:
            if item.get("type") == "message" and item.get("role") == "assistant":
                assistant_id = require_text(item.get("id"), "item id")
            elif item.get("role") != "user":
                nearby = nearby or _item_invokes_report_helper(item)
            continue
        if item.get("type") == "message" and item.get("role") == "user":
            break
        if item.get("role") != "user":
            nearby = nearby or _item_invokes_report_helper(item)
    return None if assistant_id is None else (assistant_id, nearby)


def _item_texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        texts: list[str] = []
        for nested in value.values():
            texts.extend(_item_texts(nested))
        return texts
    if isinstance(value, list):
        texts = []
        for nested in value:
            texts.extend(_item_texts(nested))
        return texts
    return []


def _item_invokes_report_helper(item: dict[str, object]) -> bool:
    if item.get("type") not in TOOL_ITEM_TYPES:
        return False
    return any(REPORT_HELPER_RE.search(text) is not None for text in _item_texts(item))


def antigravity_terminal_capture(target: str) -> str | None:
    """Capture a same-host Antigravity terminal advertised by OmniGent."""
    try:
        value = require_mapping(request_json("GET", f"/v1/sessions/{urllib.parse.quote(session_id(target), safe='')}/resources"), "session resources")
    except RuntimeError:
        return None
    data = value.get("data")
    if not isinstance(data, list):
        return None
    terminals = [resource for resource in data if isinstance(resource, dict) and resource.get("type") == "terminal" and resource.get("name") == "antigravity:main"]
    if len(terminals) != 1:
        return None
    metadata = terminals[0].get("metadata")
    if not isinstance(metadata, dict) or metadata.get("running") is not True:
        return None
    socket = metadata.get("tmux_socket")
    pane = metadata.get("tmux_target")
    if not isinstance(socket, str) or not isinstance(pane, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", pane):
        return None
    try:
        socket_info = Path(socket).stat()
    except OSError:
        return None
    if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.getuid():
        return None
    try:
        result = subprocess.run(("tmux", "-S", socket, "capture-pane", "-p", "-t", pane, "-S", "-"), capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _antigravity_last_turn(capture: str) -> str | None:
    lines = [line.rstrip() for line in capture.splitlines()]
    prompts = [index for index, line in enumerate(lines) if line.strip() == ">"]
    if not prompts:
        return None
    prompt = prompts[-1]
    footer = "\n".join(lines[prompt + 1 :])
    if "? for shortcuts" not in footer or "esc to cancel" in footer:
        return None
    separators = [index for index, line in enumerate(lines[:prompt]) if re.fullmatch(r"─{8,}", line.strip())]
    if len(separators) < 2:
        return None
    turn = "\n".join(lines[separators[-2] + 1 : separators[-1]]).strip()
    return turn or None


def _antigravity_capture_has_turn(capture: str) -> bool:
    return _antigravity_last_turn(capture) is not None


def antigravity_terminal_ready(target: str) -> bool:
    capture = antigravity_terminal_capture(target)
    return capture is not None and _expected_antigravity_turn(target, capture) is not None


# 🧑 "make anti-gravity work with that"
def _local_antigravity_bridges(root: Path, wanted: str) -> list[Path]:
    try:
        children = tuple(root.iterdir())
    except OSError:
        return []
    matches: list[Path] = []
    for child in children:
        state_path = child / "state.json"
        try:
            info = state_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                continue
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("session_id") == wanted:
            matches.append(child)
    return matches


def local_antigravity_bridge_present(target: str) -> bool:
    root = Path(os.environ.get("OMO_MANAGER_OMNIGENT_AGY_BRIDGE_ROOT", Path.home() / ".omnigent" / "antigravity-native"))
    return len(_local_antigravity_bridges(root, session_id(target))) == 1


def _local_antigravity_bridge(target: str) -> Path | None:
    root = Path(os.environ.get("OMO_MANAGER_OMNIGENT_AGY_BRIDGE_ROOT", Path.home() / ".omnigent" / "antigravity-native"))
    matches = _local_antigravity_bridges(root, session_id(target))
    return matches[0] if len(matches) == 1 else None


def _normalized_turn_text(value: str) -> str:
    return " ".join(value.split())


def _record_expected_antigravity_turn(target: str, message: str, prior_capture: str) -> None:
    bridge = _local_antigravity_bridge(target)
    if bridge is None:
        return
    normalized = _normalized_turn_text(message)
    if not normalized:
        return
    payload = (
        json.dumps(
            {
                "delivery_id": secrets.token_hex(16),
                "message_suffix": normalized[-256:],
                "prior_capture_sha256": hashlib.sha256(prior_capture.encode()).hexdigest(),
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    fd, temporary = tempfile.mkstemp(prefix=f"{AGY_EXPECTED_TURN_FILE}.", dir=bridge)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, bridge / AGY_EXPECTED_TURN_FILE)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _expected_antigravity_turn(target: str, capture: str) -> tuple[str, str] | None:
    bridge = _local_antigravity_bridge(target)
    if bridge is None:
        return None
    marker = bridge / AGY_EXPECTED_TURN_FILE
    try:
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            return None
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    delivery_id = value.get("delivery_id") if isinstance(value, dict) else None
    suffix = value.get("message_suffix") if isinstance(value, dict) else None
    prior_capture_sha256 = value.get("prior_capture_sha256") if isinstance(value, dict) else None
    turn = _antigravity_last_turn(capture)
    if not isinstance(delivery_id, str) or not isinstance(suffix, str) or not isinstance(prior_capture_sha256, str) or turn is None:
        return None
    if suffix not in _normalized_turn_text(turn):
        return None
    if hashlib.sha256(capture.encode()).hexdigest() == prior_capture_sha256:
        return None
    return turn, delivery_id


def bind_local_antigravity_conversation(target: str, *, wait_s: float = AGY_BIND_WAIT_S) -> bool:
    """Bind a first TUI-created Antigravity conversation when OmniGent leaves its placeholder behind."""
    root = Path(os.environ.get("OMO_MANAGER_OMNIGENT_AGY_BRIDGE_ROOT", Path.home() / ".omnigent" / "antigravity-native"))
    wanted = session_id(target)
    deadline = time.monotonic() + wait_s
    bridge: Path | None = None
    while True:
        matches = _local_antigravity_bridges(root, wanted)
        if len(matches) > 1:
            return False
        if matches:
            bridge = matches[0]
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(AGY_BIND_POLL_S, max(0.0, deadline - time.monotonic())))
    state_path = bridge / "state.json"
    while True:
        try:
            before = state_path.read_bytes()
            state = json.loads(before)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(state, dict) or state.get("session_id") != wanted:
            return False
        conversation = state.get("conversation_id")
        conversations = bridge / "agy-home" / ".gemini" / "antigravity-cli" / "conversations"
        try:
            ids = [path.stem for path in conversations.iterdir() if path.is_file() and path.suffix == ".db" and AGY_CONVERSATION_RE.fullmatch(path.stem)]
        except OSError:
            ids = []
        selected = conversation if isinstance(conversation, str) and AGY_CONVERSATION_RE.fullmatch(conversation) else (ids[0] if len(ids) == 1 else "")
        if selected:
            try:
                _ = request_json("PATCH", f"/v1/sessions/{urllib.parse.quote(wanted, safe='')}", {"external_session_id": selected})
            except RuntimeError:
                pass
            return True
        if len(ids) > 1 or time.monotonic() >= deadline:
            return False
        time.sleep(min(AGY_BIND_POLL_S, max(0.0, deadline - time.monotonic())))


def send_message(target: str, message: str, *, dry_run: bool = False) -> None:
    if not message:
        raise RuntimeError("OmniGent message must not be empty")
    if dry_run:
        print(f"would send {len(message.encode())} bytes to {target} through OmniGent")
        return
    local_antigravity = local_antigravity_bridge_present(target)
    prior_capture = antigravity_terminal_capture(target) if local_antigravity else None
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
    if local_antigravity or local_antigravity_bridge_present(target):
        _record_expected_antigravity_turn(target, message, prior_capture or "")
        _ = bind_local_antigravity_conversation(target)


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
    matches = [agent for agent in agents if isinstance(agent, dict) and agent.get("name") == expected_name and agent.get("harness") == expected_harness]
    if len(matches) != 1:
        raise RuntimeError(f"OmniGent requires exactly one registered `{expected_name}` agent with `{expected_harness}` harness, found {len(matches)}")
    return require_text(matches[0].get("id"), "agent id")


def online_host_id(explicit_host_id: str = "", *, wait_s: float = HOST_ONLINE_WAIT_S) -> str:
    """Return the sole online host, waiting through a brief tunnel drop."""
    configured = explicit_host_id.strip() or os.environ.get("OMO_MANAGER_OMNIGENT_HOST_ID", "").strip()
    if configured:
        return configured
    deadline = time.monotonic() + wait_s
    while True:
        value = require_mapping(request_json("GET", "/v1/hosts"), "host list")
        hosts = value.get("hosts")
        if not isinstance(hosts, list):
            raise RuntimeError("OmniGent host list lacks `hosts`")
        online = [host for host in hosts if isinstance(host, dict) and host.get("status") == "online"]
        if len(online) == 1:
            return require_text(online[0].get("host_id"), "host id")
        if len(online) > 1 or time.monotonic() >= deadline:
            raise RuntimeError(f"OmniGent launch requires one online host or OMO_MANAGER_OMNIGENT_HOST_ID, found {len(online)} online hosts")
        time.sleep(min(HOST_ONLINE_POLL_S, max(0.0, deadline - time.monotonic())))


# 🧑 "Implement at least the launch, stop, messaging, status checks, how else would we be able to try it?"
def launch_session(
    tool: str,
    workdir: Path,
    model: str,
    reasoning_effort: str,
    *,
    host_id: str = "",
    title: str = "",
    codex_flags: tuple[str, ...] = (),
    dry_run: bool = False,
) -> str:
    if tool not in OMNIGENT_TOOLS:
        raise RuntimeError("OmniGent launch currently supports the actual `antigravity`, `codex`, and `cursor` harnesses")
    if tool == "antigravity" and reasoning_effort not in ANTIGRAVITY_EFFORTS:
        raise RuntimeError("OmniGent Antigravity launch accepts only reasoning effort `low`, `medium`, or `high`")
    if codex_flags and (tool != "codex" or codex_flags != (FULL_ACCESS_FLAG,)):
        raise RuntimeError(f"OmniGent launch supports only the exact Codex access flag `{FULL_ACCESS_FLAG}`")
    if dry_run:
        suffix = f" with {FULL_ACCESS_FLAG}" if codex_flags else ""
        print(f"would launch OmniGent-backed {tool} in {workdir}{suffix}")
        return "omnigent://DRYRUN"
    model_override = f"{model}-{reasoning_effort}" if tool == "cursor" and model and reasoning_effort else (model or None)
    payload = {
        "agent_id": agent_id_for_tool(tool),
        "host_id": online_host_id(host_id),
        "workspace": str(workdir),
        "title": title or None,
        "model_override": model_override,
        "reasoning_effort": reasoning_effort or None,
        "terminal_launch_args": (
            list(CURSOR_TERMINAL_LAUNCH_ARGS)
            if tool == "cursor"
            else ["--dangerously-skip-permissions"]
            if tool == "antigravity"
            else None
        ),
        **({"labels": {FULL_ACCESS_LABEL: "1"}} if codex_flags else {}),
    }
    value = require_mapping(request_json("POST", "/v1/sessions", payload), "session launch")
    return target_for_session(require_text(value.get("id"), "launched session id"))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="")
    parser.add_argument("--dry-run", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("launch")
    launch.add_argument("--tool", choices=tuple(sorted(OMNIGENT_TOOLS)), required=True)
    launch.add_argument("--workdir", type=Path, required=True)
    launch.add_argument("--model", required=True)
    launch.add_argument("--reasoning-effort", required=True)
    launch.add_argument("--host-id", default="")
    launch.add_argument("--title", default="")
    launch.add_argument("--codex-flag", action="append", default=[])
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
            print(
                launch_session(
                    args.tool,
                    args.workdir.resolve(),
                    args.model,
                    args.reasoning_effort,
                    host_id=args.host_id,
                    title=args.title,
                    codex_flags=tuple(args.codex_flag),
                    dry_run=args.dry_run,
                )
            )
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
