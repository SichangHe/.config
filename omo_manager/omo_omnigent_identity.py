#!/usr/bin/env python3
"""Authenticate the OmniGent session that owns the current native process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MAX_STATE_BYTES = 64 * 1024
DEFAULT_SERVER_URL = "http://127.0.0.1:6767"
AGY_PLACEHOLDER_CONVERSATION_PREFIX = "agy_conv_"
AGY_CONVERSATION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
ANTIGRAVITY_BRIDGE_DIR_ENV = "HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR"
ANTIGRAVITY_SESSION_ENV = "HARNESS_ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID"
ANTIGRAVITY_RUNNER_HARNESS_ENV = "OMNIGENT_RUNNER_LAUNCH_HARNESS"
ANTIGRAVITY_RUNNER_SESSION_ENV = "OMNIGENT_RUNNER_PRIMARY_SESSION_ID"
CURSOR_BRIDGE_DIR_ENV = "HARNESS_CURSOR_NATIVE_BRIDGE_DIR"
CURSOR_HARNESS = "cursor-native"
CURSOR_AGENT_NAMES = frozenset({"agent", "cursor-agent"})
CURSOR_FORWARDER_STATE = "cursor_forwarder.json"
ADVERTISED_FILE_MODES = (0o600, 0o644)


class OmniGentIdentityError(RuntimeError):
    pass


class NotOmniGentEnvironment(OmniGentIdentityError):
    pass


@dataclass(frozen=True)
class OmniGentIdentity:
    target: str
    session_id: str
    thread_id: str
    workspace: str
    state_path: str
    socket_path: str
    app_server_pid: int
    codex_home: str

    def lines(self) -> tuple[str, ...]:
        return (
            self.target,
            self.session_id,
            self.thread_id,
            self.workspace,
            self.state_path,
            self.socket_path,
            str(self.app_server_pid),
            self.codex_home,
        )


def _read_owned_file(path: Path, maximum: int, field: str, *, allowed_modes: tuple[int, ...] = (0o600,)) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OmniGentIdentityError(f"cannot read {field}: {path}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) not in allowed_modes or before.st_nlink != 1 or before.st_size > maximum:
            raise OmniGentIdentityError(f"{field} is not a safe owner-controlled regular file")
        payload = b""
        while len(payload) <= maximum:
            chunk = os.read(fd, min(64 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(fd)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or len(payload) != before.st_size
            or len(payload) > maximum
        ):
            raise OmniGentIdentityError(f"{field} changed while it was read")
        return payload
    finally:
        os.close(fd)


def _state_for_codex_home(codex_home: Path, bridge_root: Path) -> tuple[Path, dict[str, object]]:
    try:
        resolved_home = codex_home.resolve(strict=True)
        resolved_root = bridge_root.resolve(strict=True)
    except OSError as exc:
        raise NotOmniGentEnvironment("CODEX_HOME is not an installed OmniGent bridge") from exc
    if resolved_home.name != "codex-home" or resolved_home.parent.parent != resolved_root:
        raise NotOmniGentEnvironment("CODEX_HOME is not an installed OmniGent bridge")
    state_path = resolved_home.parent / "state.json"
    try:
        state = json.loads(_read_owned_file(state_path, MAX_STATE_BYTES, "OmniGent bridge state"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OmniGentIdentityError("OmniGent bridge state is not valid JSON") from exc
    if not isinstance(state, dict) or not all(isinstance(key, str) for key in state):
        raise OmniGentIdentityError("OmniGent bridge state is not an object")
    for field in ("session_id", "thread_id", "socket_path", "codex_home"):
        if not isinstance(state.get(field), str) or not state[field]:
            raise OmniGentIdentityError(f"OmniGent bridge state lacks {field}")
    if Path(str(state["codex_home"])).resolve(strict=False) != resolved_home:
        raise OmniGentIdentityError("OmniGent bridge state CODEX_HOME does not match the process")
    return state_path, state


def _proc_text(proc_root: Path, pid: int, name: str) -> bytes:
    try:
        return (proc_root / str(pid) / name).read_bytes()
    except OSError as exc:
        raise OmniGentIdentityError(f"cannot inspect process {pid} {name}") from exc


def _proc_parent(proc_root: Path, pid: int) -> int:
    for line in _proc_text(proc_root, pid, "status").decode(errors="replace").splitlines():
        if line.startswith("PPid:"):
            value = line.partition(":")[2].strip()
            if value.isdigit():
                return int(value)
    raise OmniGentIdentityError(f"process {pid} has no parent identity")


def _proc_environment(proc_root: Path, pid: int) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in _proc_text(proc_root, pid, "environ").split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator:
            values[key.decode(errors="replace")] = value.decode(errors="replace")
    return values


# 🧑 "make it so that basically agents spawned in omnigent can do whatever agents spawned in Tmux can do now that is report to managers"
def _best_effort_proc_environment(proc_root: Path, pid: int) -> dict[str, str]:
    """Read optional harness hints without rejecting an otherwise bound native process."""
    try:
        return _proc_environment(proc_root, pid)
    except OmniGentIdentityError:
        return {}


def _is_app_server(proc_root: Path, pid: int, socket_path: str, codex_home: Path, inherited_home: str = "") -> bool:
    argv = [part.decode(errors="replace") for part in _proc_text(proc_root, pid, "cmdline").split(b"\0") if part]
    if len(argv) < 4 or Path(argv[0].split(" ", 1)[0]).name != "codex" or "app-server" not in argv:
        return False
    try:
        listen_index = argv.index("--listen")
    except ValueError:
        return False
    if listen_index + 1 >= len(argv) or argv[listen_index + 1] != socket_path:
        return False
    configured_home = _best_effort_proc_environment(proc_root, pid).get("CODEX_HOME", "") or inherited_home
    return Path(configured_home).resolve(strict=False) == codex_home.resolve(strict=False)


def find_app_server(socket_path: str, codex_home: Path, *, proc_root: Path = Path("/proc"), ancestor_pid: int | None = None) -> int:
    if ancestor_pid is not None:
        pid = ancestor_pid
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            if _is_app_server(proc_root, pid, socket_path, codex_home, os.environ.get("CODEX_HOME", "")):
                return pid
            pid = _proc_parent(proc_root, pid)
        raise OmniGentIdentityError("current process is not descended from the bound OmniGent Codex app-server")
    matches = []
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise OmniGentIdentityError("cannot enumerate live processes") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if _is_app_server(proc_root, int(entry.name), socket_path, codex_home):
                matches.append(int(entry.name))
        except OmniGentIdentityError:
            continue
    if len(matches) != 1:
        raise OmniGentIdentityError(f"expected one bound OmniGent Codex app-server, found {len(matches)}")
    return matches[0]


def _same_path(value: str, expected: Path) -> bool:
    return bool(value) and Path(value).resolve(strict=False) == expected.resolve(strict=False)


def _gemini_dir_from_argv(argv: list[str]) -> Path | None:
    prefix = "--gemini_dir="
    for index, part in enumerate(argv):
        if part.startswith(prefix):
            value = part.removeprefix(prefix)
            return Path(value) if value else None
        if part == "--gemini_dir" and index + 1 < len(argv) and argv[index + 1]:
            return Path(argv[index + 1])
    return None


def _has_gemini_dir(argv: list[str], gemini_dir: Path) -> bool:
    found = _gemini_dir_from_argv(argv)
    return found is not None and _same_path(str(found), gemini_dir)


def _bridge_dir_from_gemini(gemini_dir: Path, bridge_root: Path) -> Path | None:
    try:
        resolved = gemini_dir.resolve(strict=True)
        root = bridge_root.resolve(strict=True)
    except OSError:
        return None
    if resolved.name != ".gemini" or resolved.parent.name != "agy-home":
        return None
    bridge_dir = resolved.parent.parent
    if bridge_dir.parent != root:
        return None
    return bridge_dir


def _proc_argv(proc_root: Path, pid: int) -> list[str]:
    return [part.decode(errors="replace") for part in _proc_text(proc_root, pid, "cmdline").split(b"\0") if part]


def _is_antigravity(proc_root: Path, pid: int, bridge_dir: Path, gemini_dir: Path, session_id: str) -> bool:
    argv = _proc_argv(proc_root, pid)
    if not argv or Path(argv[0].split(" ", 1)[0]).name != "agy" or not _has_gemini_dir(argv, gemini_dir):
        return False
    environment = _best_effort_proc_environment(proc_root, pid)
    configured_bridge = environment.get(ANTIGRAVITY_BRIDGE_DIR_ENV, "")
    if configured_bridge and not _same_path(configured_bridge, bridge_dir):
        return False
    harness = environment.get(ANTIGRAVITY_RUNNER_HARNESS_ENV, "")
    if harness and harness != "antigravity-native":
        return False
    for key in (ANTIGRAVITY_SESSION_ENV,):
        configured_session = environment.get(key, "")
        if configured_session and configured_session != session_id:
            return False
    return True


def discover_antigravity_bridge(*, ancestor_pid: int, bridge_root: Path, proc_root: Path) -> Path:
    pid = ancestor_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            argv = _proc_argv(proc_root, pid)
        except OmniGentIdentityError:
            pid = _proc_parent(proc_root, pid)
            continue
        if argv and Path(argv[0].split(" ", 1)[0]).name == "agy":
            gemini_dir = _gemini_dir_from_argv(argv)
            if gemini_dir is not None:
                bridge_dir = _bridge_dir_from_gemini(gemini_dir, bridge_root)
                if bridge_dir is not None:
                    return bridge_dir
        pid = _proc_parent(proc_root, pid)
    raise NotOmniGentEnvironment("OmniGent process identity is unavailable")


def _runner_native_session(*, harness: str, ancestor_pid: int, proc_root: Path) -> str:
    current = os.environ.get(ANTIGRAVITY_RUNNER_SESSION_ENV, "")
    if os.environ.get(ANTIGRAVITY_RUNNER_HARNESS_ENV, "") == harness and current:
        return current
    pid = ancestor_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            environment = _proc_environment(proc_root, pid)
        except OmniGentIdentityError:
            pid = _proc_parent(proc_root, pid)
            continue
        if environment.get(ANTIGRAVITY_RUNNER_HARNESS_ENV, "") == harness:
            session_id = environment.get(ANTIGRAVITY_RUNNER_SESSION_ENV, "")
            if session_id:
                return session_id
        pid = _proc_parent(proc_root, pid)
    raise NotOmniGentEnvironment("OmniGent process identity is unavailable")


def _runner_antigravity_session(*, ancestor_pid: int, proc_root: Path) -> str:
    return _runner_native_session(harness="antigravity-native", ancestor_pid=ancestor_pid, proc_root=proc_root)


def _peek_antigravity_session_id(bridge_dir: Path) -> str:
    try:
        state = json.loads(_read_owned_file(bridge_dir / "state.json", MAX_STATE_BYTES, "OmniGent bridge state"))
    except (OmniGentIdentityError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    session_id = state.get("session_id") if isinstance(state, dict) else None
    return session_id if isinstance(session_id, str) else ""


def _bridge_dir_for_session(session_id: str, bridge_root: Path) -> Path:
    ready: list[Path] = []
    pending = False
    try:
        children = tuple(bridge_root.iterdir())
    except OSError as exc:
        raise NotOmniGentEnvironment("Antigravity bridge is not an installed OmniGent bridge") from exc
    for child in children:
        try:
            info = child.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        try:
            _, _, _, state, _ = _state_for_antigravity_bridge(child, bridge_root)
        except NotOmniGentEnvironment:
            continue
        except OmniGentIdentityError as exc:
            if _peek_antigravity_session_id(child) == session_id and "not ready" in str(exc):
                pending = True
            continue
        if str(state["session_id"]) == session_id:
            ready.append(child)
    if len(ready) == 1:
        return ready[0]
    if pending and not ready:
        raise OmniGentIdentityError("Antigravity conversation identity is not ready")
    raise OmniGentIdentityError(f"expected one Antigravity bridge for the runner session, found {len(ready)}")


def find_antigravity(bridge_dir: Path, gemini_dir: Path, session_id: str, *, proc_root: Path = Path("/proc"), ancestor_pid: int | None = None) -> int:
    if ancestor_pid is not None:
        pid = ancestor_pid
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            if _is_antigravity(proc_root, pid, bridge_dir, gemini_dir, session_id):
                return pid
            pid = _proc_parent(proc_root, pid)
        raise OmniGentIdentityError("current process is not descended from the bound OmniGent Antigravity process")
    matches = []
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise OmniGentIdentityError("cannot enumerate live processes") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if _is_antigravity(proc_root, int(entry.name), bridge_dir, gemini_dir, session_id):
                matches.append(int(entry.name))
        except OmniGentIdentityError:
            continue
    if len(matches) != 1:
        raise OmniGentIdentityError(f"expected one bound OmniGent Antigravity process, found {len(matches)}")
    return matches[0]


def _real_agy_conversation_id(gemini_dir: Path) -> str | None:
    try:
        entries = tuple((gemini_dir / "antigravity-cli" / "conversations").iterdir())
    except OSError:
        return None
    found: list[str] = []
    for path in entries:
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or path.suffix != ".db":
            continue
        if AGY_CONVERSATION_ID_RE.fullmatch(path.stem) is None:
            continue
        found.append(path.stem)
    if len(found) > 1:
        raise OmniGentIdentityError(f"expected one Antigravity conversation, found {len(found)}")
    return found[0] if found else None


def _state_for_antigravity_bridge(bridge_dir: Path, bridge_root: Path) -> tuple[Path, Path, Path, dict[str, object], bool]:
    try:
        resolved_dir = bridge_dir.resolve(strict=True)
        resolved_root = bridge_root.resolve(strict=True)
    except OSError as exc:
        raise NotOmniGentEnvironment("Antigravity bridge is not an installed OmniGent bridge") from exc
    if resolved_dir.parent != resolved_root or not resolved_dir.is_dir():
        raise NotOmniGentEnvironment("Antigravity bridge is not an installed OmniGent bridge")
    agy_home = resolved_dir / "agy-home"
    try:
        home_info = agy_home.lstat()
    except OSError as exc:
        raise OmniGentIdentityError("Antigravity bridge lacks agy-home") from exc
    if not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != os.getuid():
        raise OmniGentIdentityError("Antigravity agy-home is not an owner-controlled directory")
    try:
        gemini_dir = (agy_home / ".gemini").resolve(strict=True)
        resolved_home = agy_home.resolve(strict=True)
    except OSError as exc:
        raise OmniGentIdentityError("Antigravity bridge lacks an isolated gemini dir") from exc
    if gemini_dir.parent != resolved_home:
        raise OmniGentIdentityError("Antigravity gemini dir is not inside agy-home")
    state_path = resolved_dir / "state.json"
    try:
        state = json.loads(_read_owned_file(state_path, MAX_STATE_BYTES, "OmniGent bridge state"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OmniGentIdentityError("OmniGent bridge state is not valid JSON") from exc
    if not isinstance(state, dict) or not all(isinstance(key, str) for key in state):
        raise OmniGentIdentityError("OmniGent bridge state is not an object")
    for field in ("session_id", "conversation_id"):
        if not isinstance(state.get(field), str) or not state[field]:
            raise OmniGentIdentityError(f"OmniGent bridge state lacks {field}")
    conversation_id = str(state["conversation_id"])
    disk_conversation = False
    if conversation_id.startswith(AGY_PLACEHOLDER_CONVERSATION_PREFIX):
        discovered = _real_agy_conversation_id(gemini_dir)
        if not discovered:
            raise OmniGentIdentityError("Antigravity conversation identity is not ready")
        state = {**state, "conversation_id": discovered}
        disk_conversation = True
    return state_path, resolved_home, gemini_dir, state, disk_conversation


def _session(session_id: str, server: str) -> dict[str, object]:
    url = f"{server.rstrip('/')}/v1/sessions/{urllib.parse.quote(session_id, safe='')}?include_items=false&refresh_state=true"
    headers = {"Accept": "application/json"}
    token = os.environ.get("OMO_MANAGER_OMNIGENT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OmniGentIdentityError("cannot authenticate OmniGent session with its server") from exc
    try:
        session = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OmniGentIdentityError("OmniGent session response is not valid JSON") from exc
    if not isinstance(session, dict) or not all(isinstance(key, str) for key in session):
        raise OmniGentIdentityError("OmniGent session response is not an object")
    return session


def authenticate_omnigent_session(
    *,
    codex_home: Path,
    ancestor_pid: int | None,
    expected_target: str = "",
    expected_thread_id: str = "",
    expected_workspace: str = "",
    expected_state_path: str = "",
    expected_socket_path: str = "",
    expected_app_server_pid: int = 0,
    bridge_root: Path | None = None,
    proc_root: Path = Path("/proc"),
    server: str | None = None,
) -> OmniGentIdentity:
    root = bridge_root or (Path.home() / ".omnigent" / "codex-native")
    state_path, state = _state_for_codex_home(codex_home, root)
    resolved_home = codex_home.resolve(strict=True)
    session_id = str(state["session_id"])
    thread_id = str(state["thread_id"])
    socket_path = str(state["socket_path"])
    target = f"omnigent://{session_id}"
    app_server_pid = find_app_server(socket_path, resolved_home, proc_root=proc_root, ancestor_pid=ancestor_pid)
    session = _session(session_id, server or os.environ.get("OMO_MANAGER_OMNIGENT_URL", DEFAULT_SERVER_URL))
    workspace = session.get("workspace")
    if (
        session.get("id") != session_id
        or session.get("external_session_id") != thread_id
        or session.get("harness") != "codex-native"
        or session.get("runner_online") is not True
        or session.get("host_online") is not True
        or session.get("archived") is True
        or session.get("status") not in {"idle", "running", "waiting"}
        or not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
    ):
        raise OmniGentIdentityError("OmniGent session does not match its live Codex bridge")
    identity = OmniGentIdentity(target, session_id, thread_id, workspace, str(state_path), socket_path, app_server_pid, str(resolved_home))
    expected = {
        "target": (expected_target, identity.target),
        "thread": (expected_thread_id, identity.thread_id),
        "workspace": (expected_workspace, identity.workspace),
        "state path": (expected_state_path, identity.state_path),
        "socket": (expected_socket_path, identity.socket_path),
    }
    mismatch = next((field for field, (wanted, actual) in expected.items() if wanted and wanted != actual), "")
    if mismatch or (expected_app_server_pid and expected_app_server_pid != identity.app_server_pid):
        raise OmniGentIdentityError(f"OmniGent {mismatch or 'app-server process'} identity changed")
    if any("\t" in value or "\n" in value for value in identity.lines()):
        raise OmniGentIdentityError("OmniGent identity contains unsafe control characters")
    return identity


def authenticate_antigravity_session(
    *,
    bridge_dir: Path,
    ancestor_pid: int | None,
    expected_target: str = "",
    expected_thread_id: str = "",
    expected_workspace: str = "",
    expected_state_path: str = "",
    expected_socket_path: str = "",
    expected_app_server_pid: int = 0,
    require_agy_ancestor: bool = True,
    bridge_root: Path | None = None,
    proc_root: Path = Path("/proc"),
    server: str | None = None,
) -> OmniGentIdentity:
    root = bridge_root or (Path.home() / ".omnigent" / "antigravity-native")
    state_path, agy_home, gemini_dir, state, _ = _state_for_antigravity_bridge(bridge_dir, root)
    session_id = str(state["session_id"])
    thread_id = str(state["conversation_id"])
    socket_path = str(gemini_dir)
    target = f"omnigent://{session_id}"
    app_server_pid = find_antigravity(
        agy_home.parent,
        gemini_dir,
        session_id,
        proc_root=proc_root,
        ancestor_pid=ancestor_pid if require_agy_ancestor else None,
    )
    session = _session(session_id, server or os.environ.get("OMO_MANAGER_OMNIGENT_URL", DEFAULT_SERVER_URL))
    workspace = session.get("workspace")
    external = session.get("external_session_id")
    external_pending = external is None or (isinstance(external, str) and external.startswith(AGY_PLACEHOLDER_CONVERSATION_PREFIX))
    if (
        session.get("id") != session_id
        or (external != thread_id and not (external_pending and _real_agy_conversation_id(gemini_dir) == thread_id))
        or session.get("harness") != "antigravity-native"
        or session.get("runner_online") is not True
        or session.get("host_online") is not True
        or session.get("archived") is True
        or session.get("status") not in {"idle", "running", "waiting"}
        or not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
    ):
        raise OmniGentIdentityError("OmniGent session does not match its live Antigravity bridge")
    identity = OmniGentIdentity(target, session_id, thread_id, workspace, str(state_path), socket_path, app_server_pid, str(agy_home))
    expected = {
        "target": (expected_target, identity.target),
        "thread": (expected_thread_id, identity.thread_id),
        "workspace": (expected_workspace, identity.workspace),
        "state path": (expected_state_path, identity.state_path),
        "socket": (expected_socket_path, identity.socket_path),
    }
    mismatch = next((field for field, (wanted, actual) in expected.items() if wanted and wanted != actual), "")
    if mismatch or (expected_app_server_pid and expected_app_server_pid != identity.app_server_pid):
        raise OmniGentIdentityError(f"OmniGent {mismatch or 'Antigravity process'} identity changed")
    if any("\t" in value or "\n" in value for value in identity.lines()):
        raise OmniGentIdentityError("OmniGent identity contains unsafe control characters")
    return identity


def _cursor_bridge_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def _default_cursor_bridge_root() -> Path:
    return Path(tempfile.gettempdir()) / f"omnigent-{os.getuid()}" / "cursor-native"


def _cursor_executable_name(argv: list[str]) -> str:
    if not argv:
        return ""
    return Path(argv[0].split(" ", 1)[0]).name


def _argv_workspace(argv: list[str]) -> str:
    for index, part in enumerate(argv):
        if part == "--workspace" and index + 1 < len(argv):
            return argv[index + 1]
        if part.startswith("--workspace="):
            return part.split("=", 1)[1]
    return ""


def _cursor_tui_argv(argv: list[str]) -> bool:
    commands = {part for part in argv[1:] if part and not part.startswith("-")}
    if commands & {"models", "mcp"}:
        return False
    return "--trust" in argv or "--force" in argv or "-f" in argv


def _is_cursor(proc_root: Path, pid: int, bridge_dir: Path, workspace: str = "", inherited_bridge: str = "") -> bool:
    argv = _proc_argv(proc_root, pid)
    if _cursor_executable_name(argv) not in CURSOR_AGENT_NAMES:
        return False
    configured = _best_effort_proc_environment(proc_root, pid).get(CURSOR_BRIDGE_DIR_ENV, "")
    if configured:
        return _same_path(configured, bridge_dir)
    if inherited_bridge and _same_path(inherited_bridge, bridge_dir):
        return True
    if not workspace or not _cursor_tui_argv(argv):
        return False
    if _argv_workspace(argv) == workspace:
        return True
    try:
        return Path(os.readlink(proc_root / str(pid) / "cwd")).resolve(strict=False) == Path(workspace).resolve(strict=False)
    except OSError:
        return False


def discover_cursor_bridge(*, ancestor_pid: int, bridge_root: Path, proc_root: Path) -> Path:
    try:
        resolved_root = bridge_root.resolve(strict=True)
    except OSError as exc:
        raise NotOmniGentEnvironment("Cursor bridge is not an installed OmniGent bridge") from exc
    pid = ancestor_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            environment = _proc_environment(proc_root, pid)
        except OmniGentIdentityError:
            pid = _proc_parent(proc_root, pid)
            continue
        configured = environment.get(CURSOR_BRIDGE_DIR_ENV, "")
        if configured:
            try:
                bridge_dir = Path(configured).resolve(strict=True)
            except OSError:
                pid = _proc_parent(proc_root, pid)
                continue
            if bridge_dir.parent == resolved_root and _is_cursor(proc_root, pid, bridge_dir):
                return bridge_dir
        pid = _proc_parent(proc_root, pid)
    raise NotOmniGentEnvironment("OmniGent process identity is unavailable")


def find_cursor(bridge_dir: Path, *, workspace: str = "", proc_root: Path = Path("/proc"), ancestor_pid: int | None = None) -> int:
    if ancestor_pid is not None:
        pid = ancestor_pid
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            if _is_cursor(proc_root, pid, bridge_dir, workspace, os.environ.get(CURSOR_BRIDGE_DIR_ENV, "")):
                return pid
            pid = _proc_parent(proc_root, pid)
        raise OmniGentIdentityError("current process is not descended from the bound OmniGent Cursor process")
    matches = []
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        raise OmniGentIdentityError("cannot enumerate live processes") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if _is_cursor(proc_root, int(entry.name), bridge_dir, workspace):
                matches.append(int(entry.name))
        except OmniGentIdentityError:
            continue
    if len(matches) != 1:
        raise OmniGentIdentityError(f"expected one bound OmniGent Cursor process, found {len(matches)}")
    return matches[0]


def _state_for_cursor_bridge(bridge_dir: Path, bridge_root: Path, session_id: str) -> tuple[Path, str]:
    try:
        resolved_dir = bridge_dir.resolve(strict=True)
        resolved_root = bridge_root.resolve(strict=True)
    except OSError as exc:
        raise NotOmniGentEnvironment("Cursor bridge is not an installed OmniGent bridge") from exc
    if resolved_dir.parent != resolved_root or resolved_dir.name != _cursor_bridge_digest(session_id):
        raise NotOmniGentEnvironment("Cursor bridge is not an installed OmniGent bridge")
    tmux_path = resolved_dir / "tmux.json"
    try:
        tmux = json.loads(_read_owned_file(tmux_path, MAX_STATE_BYTES, "Cursor tmux target", allowed_modes=ADVERTISED_FILE_MODES))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OmniGentIdentityError("Cursor tmux target is not valid JSON") from exc
    if not isinstance(tmux, dict) or not all(isinstance(key, str) for key in tmux):
        raise OmniGentIdentityError("Cursor tmux target is not an object")
    socket_path = tmux.get("socket_path")
    if not isinstance(socket_path, str) or not socket_path:
        raise OmniGentIdentityError("Cursor tmux target lacks socket_path")
    return tmux_path, socket_path


def _cursor_thread_id(bridge_dir: Path, external: object) -> str:
    disk = ""
    forwarder = bridge_dir / CURSOR_FORWARDER_STATE
    try:
        payload = json.loads(_read_owned_file(forwarder, MAX_STATE_BYTES, "Cursor forwarder state", allowed_modes=ADVERTISED_FILE_MODES))
    except OmniGentIdentityError:
        payload = None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OmniGentIdentityError("Cursor forwarder state is not valid JSON") from exc
    if isinstance(payload, dict):
        store_path = payload.get("store_path")
        if isinstance(store_path, str) and store_path:
            chat_id = Path(store_path).parent.name
            if AGY_CONVERSATION_ID_RE.fullmatch(chat_id):
                disk = chat_id
    if isinstance(external, str) and external:
        if disk and disk != external:
            raise OmniGentIdentityError("OmniGent session does not match its live Cursor bridge")
        return external
    if disk:
        return disk
    raise OmniGentIdentityError("Cursor conversation identity is not ready")


def authenticate_cursor_session(
    *,
    bridge_dir: Path,
    session_id: str,
    ancestor_pid: int | None,
    expected_target: str = "",
    expected_thread_id: str = "",
    expected_workspace: str = "",
    expected_state_path: str = "",
    expected_socket_path: str = "",
    expected_app_server_pid: int = 0,
    require_agent_ancestor: bool = True,
    bridge_root: Path | None = None,
    proc_root: Path = Path("/proc"),
    server: str | None = None,
) -> OmniGentIdentity:
    root = bridge_root or _default_cursor_bridge_root()
    state_path, socket_path = _state_for_cursor_bridge(bridge_dir, root, session_id)
    target = f"omnigent://{session_id}"
    session = _session(session_id, server or os.environ.get("OMO_MANAGER_OMNIGENT_URL", DEFAULT_SERVER_URL))
    workspace = session.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise OmniGentIdentityError("OmniGent session does not match its live Cursor bridge")
    app_server_pid = find_cursor(
        bridge_dir.resolve(strict=True),
        workspace=workspace,
        proc_root=proc_root,
        ancestor_pid=ancestor_pid if require_agent_ancestor else None,
    )
    thread_id = _cursor_thread_id(bridge_dir.resolve(strict=True), session.get("external_session_id"))
    if (
        session.get("id") != session_id
        or session.get("harness") != CURSOR_HARNESS
        or session.get("runner_online") is not True
        or session.get("host_online") is not True
        or session.get("archived") is True
        or session.get("status") not in {"idle", "running", "waiting"}
        or not isinstance(workspace, str)
        or not Path(workspace).is_absolute()
    ):
        raise OmniGentIdentityError("OmniGent session does not match its live Cursor bridge")
    identity = OmniGentIdentity(target, session_id, thread_id, workspace, str(state_path), socket_path, app_server_pid, str(bridge_dir.resolve(strict=True)))
    expected = {
        "target": (expected_target, identity.target),
        "thread": (expected_thread_id, identity.thread_id),
        "workspace": (expected_workspace, identity.workspace),
        "state path": (expected_state_path, identity.state_path),
        "socket": (expected_socket_path, identity.socket_path),
    }
    mismatch = next((field for field, (wanted, actual) in expected.items() if wanted and wanted != actual), "")
    if mismatch or (expected_app_server_pid and expected_app_server_pid != identity.app_server_pid):
        raise OmniGentIdentityError(f"OmniGent {mismatch or 'Cursor process'} identity changed")
    if any("\t" in value or "\n" in value for value in identity.lines()):
        raise OmniGentIdentityError("OmniGent identity contains unsafe control characters")
    return identity


def _authenticate_current_cursor(
    *,
    expected_target: str,
    expected_thread_id: str,
    expected_workspace: str,
    expected_state_path: str,
    expected_socket_path: str,
    expected_app_server_pid: int,
    cursor_bridge: Path | None,
    cursor_bridge_root: Path | None,
    proc_root: Path,
    server: str | None,
) -> OmniGentIdentity:
    root = cursor_bridge_root or _default_cursor_bridge_root()
    configured = str(cursor_bridge) if cursor_bridge is not None else os.environ.get(CURSOR_BRIDGE_DIR_ENV, "")
    require_agent_ancestor = True
    session_id = ""
    if not configured:
        try:
            discovered = discover_cursor_bridge(ancestor_pid=os.getppid(), bridge_root=root, proc_root=proc_root)
            configured = str(discovered)
        except NotOmniGentEnvironment:
            session_id = _runner_native_session(harness=CURSOR_HARNESS, ancestor_pid=os.getppid(), proc_root=proc_root)
            configured = str(root / _cursor_bridge_digest(session_id))
            require_agent_ancestor = False
    elif cursor_bridge_root is None:
        root = Path(configured).resolve(strict=False).parent
    if not session_id:
        try:
            session_id = _runner_native_session(harness=CURSOR_HARNESS, ancestor_pid=os.getppid(), proc_root=proc_root)
        except NotOmniGentEnvironment as exc:
            if not require_agent_ancestor:
                raise
            raise OmniGentIdentityError("Cursor runner session identity is unavailable") from exc
    return authenticate_cursor_session(
        bridge_dir=Path(configured),
        session_id=session_id,
        ancestor_pid=os.getppid(),
        expected_target=expected_target,
        expected_thread_id=expected_thread_id,
        expected_workspace=expected_workspace,
        expected_state_path=expected_state_path,
        expected_socket_path=expected_socket_path,
        expected_app_server_pid=expected_app_server_pid,
        require_agent_ancestor=require_agent_ancestor,
        bridge_root=root,
        proc_root=proc_root,
        server=server,
    )


# 🧑 "There’s still no reason to create dir/tmux session"
def authenticate_current_omnigent(
    *,
    expected_target: str = "",
    expected_thread_id: str = "",
    expected_workspace: str = "",
    expected_state_path: str = "",
    expected_socket_path: str = "",
    expected_app_server_pid: int = 0,
    codex_home: Path | None = None,
    antigravity_bridge: Path | None = None,
    cursor_bridge: Path | None = None,
    bridge_root: Path | None = None,
    antigravity_bridge_root: Path | None = None,
    cursor_bridge_root: Path | None = None,
    proc_root: Path = Path("/proc"),
    server: str | None = None,
) -> OmniGentIdentity:
    configured_home = str(codex_home) if codex_home is not None else os.environ.get("CODEX_HOME", "")
    if configured_home:
        try:
            return authenticate_omnigent_session(
                codex_home=Path(configured_home),
                ancestor_pid=os.getppid(),
                expected_target=expected_target,
                expected_thread_id=expected_thread_id,
                expected_workspace=expected_workspace,
                expected_state_path=expected_state_path,
                expected_socket_path=expected_socket_path,
                expected_app_server_pid=expected_app_server_pid,
                bridge_root=bridge_root,
                proc_root=proc_root,
                server=server,
            )
        except NotOmniGentEnvironment:
            pass
    try:
        return _authenticate_current_cursor(
            expected_target=expected_target,
            expected_thread_id=expected_thread_id,
            expected_workspace=expected_workspace,
            expected_state_path=expected_state_path,
            expected_socket_path=expected_socket_path,
            expected_app_server_pid=expected_app_server_pid,
            cursor_bridge=cursor_bridge,
            cursor_bridge_root=cursor_bridge_root,
            proc_root=proc_root,
            server=server,
        )
    except NotOmniGentEnvironment:
        pass
    configured_bridge = str(antigravity_bridge) if antigravity_bridge is not None else os.environ.get(ANTIGRAVITY_BRIDGE_DIR_ENV, "")
    root = antigravity_bridge_root or (Path.home() / ".omnigent" / "antigravity-native")
    require_agy_ancestor = True
    if not configured_bridge:
        try:
            configured_bridge = str(discover_antigravity_bridge(ancestor_pid=os.getppid(), bridge_root=root, proc_root=proc_root))
        except NotOmniGentEnvironment:
            session_id = _runner_antigravity_session(ancestor_pid=os.getppid(), proc_root=proc_root)
            configured_bridge = str(_bridge_dir_for_session(session_id, root))
            require_agy_ancestor = False
    return authenticate_antigravity_session(
        bridge_dir=Path(configured_bridge),
        ancestor_pid=os.getppid(),
        expected_target=expected_target,
        expected_thread_id=expected_thread_id,
        expected_workspace=expected_workspace,
        expected_state_path=expected_state_path,
        expected_socket_path=expected_socket_path,
        expected_app_server_pid=expected_app_server_pid,
        require_agy_ancestor=require_agy_ancestor,
        bridge_root=antigravity_bridge_root or root,
        proc_root=proc_root,
        server=server,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optional", action="store_true")
    args = parser.parse_args(argv)
    try:
        identity = authenticate_current_omnigent()
    except NotOmniGentEnvironment as exc:
        if args.optional:
            return 0
        print(f"omo_omnigent_identity: {exc}", file=sys.stderr)
        return 2
    except OmniGentIdentityError as exc:
        print(f"omo_omnigent_identity: {exc}", file=sys.stderr)
        return 2
    print("\n".join(identity.lines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
