#!/usr/bin/env python3
"""Serialize bidirectional-blocking graph mutations in the pending watcher."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any

from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking import add_dependency
from omo_manager.omo_blocking import load_task
from omo_manager.omo_blocking import queue_due_notices
from omo_manager.omo_blocking import reconcile
from omo_manager.omo_blocking import remove_dependency
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_agent_status import same_tmux_target
from omo_manager.omo_task_context import infer_active_task


def _ancestor_pids(pid: int) -> set[int]:
    """Return the kernel-observed process ancestry for a Unix-socket peer."""
    ancestors: set[int] = set()
    current = pid
    while current > 1 and current not in ancestors:
        ancestors.add(current)
        try:
            stat = Path(f"/proc/{current}/stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 2 :].split()
            current = int(fields[1])
        except (OSError, ValueError, IndexError):
            break
    return ancestors


def actor_socket(root: Path) -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return state / f"blocking-{digest}.sock"


def request(root: Path, payload: dict[str, object], timeout_s: float = 10) -> dict[str, Any]:
    path = actor_socket(root)
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(timeout_s)
            client.connect(str(path))
            client.sendall(json.dumps(payload).encode() + b"\n")
            response = b""
            while not response.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
    except (OSError, TimeoutError) as exc:
        raise BlockingError("bidirectional-blocking watcher actor is unavailable") from exc
    try:
        value = json.loads(response)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BlockingError("bidirectional-blocking watcher actor returned an invalid response") from exc
    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        raise BlockingError("bidirectional-blocking watcher actor returned an invalid response")
    if not value["ok"]:
        raise BlockingError(str(value.get("error", "blocking mutation failed")))
    return value


class BlockingActor:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = actor_socket(self.root)
        self.server = socket.socket(socket.AF_UNIX)
        self.thread = threading.Thread(target=self._serve, name="omo-blocking-actor", daemon=True)
        self.stopping = threading.Event()

    def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(self.path))
            except OSError:
                self.path.unlink()
            else:
                raise BlockingError("bidirectional-blocking watcher actor is already running")
        self.server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self.server.listen()
        self.server.settimeout(0.2)
        self.thread.start()

    def close(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=2)
        self.server.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _address = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                try:
                    payload = json.loads(connection.makefile("rb").readline())
                    peer_pid, _uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                    result = self._handle(payload, peer_pid)
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    result = {"ok": False, "error": str(exc)}
                connection.sendall(json.dumps(result).encode() + b"\n")

    def _task_path(self, value: object) -> Path:
        if not isinstance(value, str):
            raise BlockingError("actor task path must be text")
        path = (self.root / value).resolve(strict=False)
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise BlockingError("actor task path escapes the configured root") from exc
        return path

    def _authorize(self, payload: dict[object, object], peer_pid: int) -> None:
        environ = Path(f"/proc/{peer_pid}/environ").read_bytes().split(b"\0")
        pane = next((entry.split(b"=", 1)[1].decode() for entry in environ if entry.startswith(b"TMUX_PANE=")), "")
        if not pane:
            raise BlockingError("dependency changes require an identifiable manager pane")
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{session_name}:#{window_index}.#{pane_index}\t#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        target, separator, pane_pid_text = result.stdout.strip().partition("\t")
        if result.returncode != 0 or not target or not separator:
            raise BlockingError("dependency changes require an active manager pane")
        try:
            pane_pid = int(pane_pid_text)
        except ValueError as exc:
            raise BlockingError("dependency changes require an identifiable manager process") from exc
        if pane_pid not in _ancestor_pids(peer_pid):
            raise BlockingError("dependency changes must originate from the claimed manager pane")
        caller = load_task(infer_active_task(self.root, target), root=self.root)
        owner = load_task(self._task_path(payload["task"]), root=self.root)
        if not caller.metadata["is_manager"] or not same_tmux_target(owner.metadata["managerat"], caller.metadata["runat"]):
            raise BlockingError("the current manager does not directly own the edited task")

    def _handle(self, payload: object, peer_pid: int) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise BlockingError("actor request must be a mapping")
        operation = payload.get("operation")
        if not v2_enabled(self.root):
            if operation == "queue":
                return {"ok": True, "changed": []}
            raise BlockingError("v2 blocking mutations are disabled until reviewed migration enablement")
        if operation in {"dependency-add", "dependency-remove"}:
            self._authorize(payload, peer_pid)
        if operation == "dependency-add":
            add_dependency(
                self.root,
                self._task_path(payload["task"]),
                str(payload["item_id"]),
                self._task_path(payload["on_task"]),
                str(payload["on_item_id"]),
            )
            return {"ok": True}
        if operation == "dependency-remove":
            source = load_task(self._task_path(payload["on_task"]), root=self.root)
            remove_dependency(
                self.root,
                self._task_path(payload["task"]),
                str(payload["item_id"]),
                source.metadata["task_id"],
                str(payload["on_item_id"]),
                str(payload["evidence"]),
            )
            result = reconcile(self.root)
            if result.errors:
                raise BlockingError(result.errors[0])
            return {"ok": True}
        if operation == "reconcile":
            result = reconcile(self.root)
            if result.errors:
                raise BlockingError(result.errors[0])
            return {"ok": True, "changed": [str(path.relative_to(self.root)) for path in result.changed_paths]}
        if operation == "queue":
            paths = queue_due_notices(self.root)
            return {"ok": True, "changed": [str(path.relative_to(self.root)) for path in paths]}
        raise BlockingError("unknown blocking actor operation")
