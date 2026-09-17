#!/usr/bin/env python3
"""Authenticate the OmniGent session that owns the current Codex process."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MAX_STATE_BYTES = 64 * 1024
DEFAULT_SERVER_URL = "http://127.0.0.1:6767"


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


def _read_owned_file(path: Path, maximum: int, field: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OmniGentIdentityError(f"cannot read {field}: {path}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > maximum:
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


def _is_app_server(proc_root: Path, pid: int, socket_path: str, codex_home: Path) -> bool:
    argv = [part.decode(errors="replace") for part in _proc_text(proc_root, pid, "cmdline").split(b"\0") if part]
    if len(argv) < 4 or Path(argv[0].split(" ", 1)[0]).name != "codex" or "app-server" not in argv:
        return False
    try:
        listen_index = argv.index("--listen")
    except ValueError:
        return False
    if listen_index + 1 >= len(argv) or argv[listen_index + 1] != socket_path:
        return False
    configured_home = _proc_environment(proc_root, pid).get("CODEX_HOME", "")
    return Path(configured_home).resolve(strict=False) == codex_home.resolve(strict=False)


def find_app_server(socket_path: str, codex_home: Path, *, proc_root: Path = Path("/proc"), ancestor_pid: int | None = None) -> int:
    if ancestor_pid is not None:
        pid = ancestor_pid
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            if _is_app_server(proc_root, pid, socket_path, codex_home):
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
    bridge_root: Path | None = None,
    proc_root: Path = Path("/proc"),
    server: str | None = None,
) -> OmniGentIdentity:
    configured_home = str(codex_home) if codex_home is not None else os.environ.get("CODEX_HOME", "")
    if not configured_home:
        raise NotOmniGentEnvironment("CODEX_HOME is unavailable")
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
