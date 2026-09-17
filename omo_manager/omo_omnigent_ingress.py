"""Single-use, process-bound credentials for reserved native observations."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from collections.abc import Callable
from urllib.parse import parse_qs, unquote

from omo_manager.omo_omnigent_fence import (
    IngressDeferred,
    IngressGrant,
    IngressReply,
    Message,
    Operation,
    PHASES,
    Rejected,
    FenceStore,
    _boot_id,
    _canonical_id,
    _operation,
    _process_start,
    _runner_for_token,
    canonical,
    digest,
)

TOKEN_PREFIX = "omo_native_"
BINDING_HEADER = b"x-omnigent-runner-tunnel-token"
type NativeResolver = Callable[[Operation], dict[str, object] | Rejected]


def _denied(code: str, status: int = 409) -> IngressReply:
    return IngressReply(status, {"error": code})


def _request_digest(scope: Message, payload: object, operation: Operation, suffix: str) -> str:
    return digest(canonical({"method": scope["method"], "session_id": operation.spec.new_session_id, "suffix": suffix, "payload": payload}).encode())


def _native_scope(method: str, suffix: str, payload: object, thread_id: object) -> str | None:
    if method == "PATCH" and suffix == "" and payload == {"external_session_id": thread_id}:
        return "native_metadata"
    if method != "POST" or suffix != "/events" or not isinstance(payload, dict) or set(payload) != {"type", "data"}:
        return None
    data, event = payload["data"], payload["type"]
    fields = {
        "external_conversation_item": ({"item_type", "item_data", "response_id"}, {"item_type", "item_data", "response_id"}),
        "external_session_status": ({"status", "response_id", "output", "reauth_required"}, {"status"}),
        "external_output_text_delta": ({"delta", "message_id", "index", "final"}, {"delta"}),
        "external_tool_output_delta": ({"delta", "call_id"}, {"delta", "call_id"}),
        "external_output_reasoning_delta": ({"delta", "message_id", "index", "started"}, {"delta"}),
        "external_session_interrupted": ({"response_id"}, set()),
        "external_session_todos": ({"todos"}, {"todos"}),
        "external_mcp_startup": ({"servers"}, {"servers"}),
        "external_session_usage": (
            {
                "context_tokens",
                "context_window",
                "cumulative_input_tokens",
                "cumulative_output_tokens",
                "cumulative_cache_read_input_tokens",
                "model",
            },
            set(),
        ),
    }
    if not isinstance(event, str) or event not in fields or not isinstance(data, dict):
        return None
    allowed, required = fields[event]
    if not required <= set(data) <= allowed:
        return None
    if event == "external_conversation_item":
        item = data["item_data"]
        if (
            data["item_type"] != "message"
            or not isinstance(data["response_id"], str)
            or not data["response_id"]
            or not isinstance(item, dict)
            or not {"role", "content"} <= set(item) <= {"role", "content", "is_meta", "phase", "agent"}
            or item["role"] not in ("user", "assistant")
        ):
            return None
        if "agent" in item and (item["role"] != "assistant" or item["agent"] != "codex-native-ui"):
            return None
        content = item["content"]
        if (
            not isinstance(content, list)
            or not content
            or any(not isinstance(block, dict) or set(block) != {"type", "text"} or block["type"] not in ("input_text", "output_text") or not isinstance(block["text"], str) for block in content)
        ):
            return None
    if event == "external_session_status" and data["status"] not in ("idle", "running", "failed", "completed", "interrupted", "initializing"):
        return None
    return event


def _tool_mirror(method: str, suffix: str, payload: object) -> bool:
    """Identify observations to hold, not to authorize with the credential."""
    if method != "POST" or suffix != "/events" or not isinstance(payload, dict) or set(payload) != {"type", "data"} or payload["type"] != "external_conversation_item":
        return False
    data = payload["data"]
    if (
        not isinstance(data, dict)
        or set(data) != {"item_type", "item_data", "response_id"}
        or data["item_type"] not in ("function_call", "function_call_output", "reasoning", "compaction")
        or not isinstance(data["item_data"], dict)
        or not isinstance(data["response_id"], str)
    ):
        return False
    from omnigent.entities.conversation import parse_item_data

    try:
        parse_item_data(data["item_type"], data["item_data"])
        return True
    except (ValueError, TypeError):
        return False


# 🧑 "Replace the current read-only owner atomically ... preserving the task and queue and delivering only open work."
class NativeIngress:
    """Authenticate the installed mint handshake, never a shared owner bearer.

    The immutable first native binding includes the server incarnation. Native
    restart or server refresh requires independent reconciliation, not reminting.
    Token use and persistent callback identity are claimed before downstream
    writes; a crash cannot make an ambiguous transcript append replayable.
    """

    def __init__(
        self,
        store: FenceStore,
        *,
        endpoint: tuple[str, int],
        resolve_owner: Callable[[str], str | None],
        resolve_user: Callable[[Message], str | None],
        resolve_native_pin: NativeResolver | None = None,
        clock: Callable[[], float] = time.time,
        ttl_s: float = 60,
        wait_timeout_s: float = 5,
    ):
        if not 0 < ttl_s < 300:
            raise ValueError("single-use credentials must expire inside the installed client's 300-second refresh skew")
        if not 0 < wait_timeout_s < 10:
            raise ValueError("deferred callbacks must resolve inside the native client's ten-second request timeout")
        if not isinstance(endpoint, tuple) or len(endpoint) != 2 or endpoint[0] != "127.0.0.1" or type(endpoint[1]) is not int or not 1 <= endpoint[1] <= 65535:
            raise ValueError("native ingress requires the exact configured IPv4 loopback endpoint")
        self.store, self.resolve_owner, self.resolve_user = store, resolve_owner, resolve_user
        self.endpoint = endpoint
        self.resolve_native_pin, self.clock, self.ttl_s = resolve_native_pin, clock, ttl_s
        self.wait_timeout_s = wait_timeout_s
        self.epoch = secrets.token_hex(24)
        self.server_pin = {"server_pid": os.getpid(), "server_start_ticks": _process_start(os.getpid()), "server_boot_id": _boot_id()}
        with store._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ingress_tokens (token_sha256 TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(operation_id), binding TEXT NOT NULL, expires_at_s REAL NOT NULL, used_request TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ingress_callbacks (operation_id TEXT NOT NULL REFERENCES operations(operation_id), request_sha256 TEXT NOT NULL, purpose TEXT NOT NULL, PRIMARY KEY(operation_id, request_sha256))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ingress_pending (token_sha256 TEXT PRIMARY KEY REFERENCES ingress_tokens(token_sha256), operation_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL)"
            )

    def bind_native_resolver(self, resolver: NativeResolver) -> None:
        if self.resolve_native_pin is not None:
            raise RuntimeError("native resolver already installed")
        self.resolve_native_pin = resolver

    def _on_endpoint(self, scope: Message) -> bool:
        server = scope.get("server")
        return isinstance(server, (tuple, list)) and len(server) == 2 and type(server[1]) is int and tuple(server) == self.endpoint

    def _operation_error(self, operation: Operation, runner_id: str) -> IngressReply | None:
        claim = operation.receipts.get("successor_launch", {})
        if operation.status == "cancelled" or PHASES.index(operation.phase) < PHASES.index("old_quiesced") or claim.get("runner_id") != runner_id:
            return _denied("native_runner_not_claimed")
        if any(claim.get(key) != value for key, value in self.server_pin.items()):
            return _denied("native_server_generation_changed")
        return None

    def _binding(self, scope: Message, operation: Operation, runner_id: str) -> dict[str, object] | Rejected:
        if not self._on_endpoint(scope):
            return Rejected("native_endpoint_mismatch", "request did not arrive on the configured loopback endpoint")
        if self.resolve_native_pin is None:
            return Rejected("native_resolver_unavailable", "private backend is not installed")
        binding = self.resolve_native_pin(operation)
        if isinstance(binding, Rejected):
            return binding
        required = {
            "session_id": str,
            "runner_id": str,
            "owner": str,
            "thread_id": str,
            "pid": int,
            "start_ticks": int,
            "boot_id": str,
            "argv_sha256": str,
            "host_pid": int,
            "host_start_ticks": int,
            "host_id": str,
        }
        if set(binding) != set(required) or any(type(binding[key]) is not kind for key, kind in required.items()):
            return Rejected("invalid_native_binding", "native proof lacks exact required fields")
        if (
            _canonical_id(str(binding["session_id"])) != _canonical_id(operation.spec.new_session_id)
            or binding["runner_id"] != runner_id
            or _canonical_id(str(binding["host_id"])) != _canonical_id(operation.spec.expected.host_id)
            or binding["owner"] != self.resolve_owner(operation.spec.new_session_id)
            or binding["owner"] != "local"
            or binding["thread_id"] == operation.spec.expected.thread_id
        ):
            return Rejected("native_binding_mismatch", "native identity differs from reserved owner")
        try:
            if binding["boot_id"] != _boot_id() or _process_start(int(str(binding["pid"]))) != binding["start_ticks"] or _process_start(int(str(binding["host_pid"]))) != binding["host_start_ticks"]:
                return Rejected("native_process_changed", "native or host generation changed")
        except (OSError, ValueError, IndexError):
            return Rejected("native_process_missing", "native or host process is not verifiable")
        return (
            binding
            | self.server_pin
            | {"server_epoch": self.epoch, "operation_id": operation.spec.operation_id, "endpoint_host": self.endpoint[0], "endpoint_port": self.endpoint[1], "purpose": "reserved_native_observation"}
        )

    def _headers(self, scope: Message, owner: str) -> list[tuple[bytes, bytes]] | None:
        return list(scope.get("headers", [])) if owner == "local" and self.resolve_user(scope) == owner else None

    def _grant(self, scope: Message, operation: Operation, runner_id: str) -> IngressGrant | IngressReply:
        owner = self.resolve_owner(operation.spec.new_session_id)
        headers = self._headers(scope, owner) if owner else None
        return IngressGrant(operation.spec.new_session_id, runner_id, headers) if headers is not None else _denied("native_owner_auth_unavailable", 401)

    def _mint(self, scope: Message, operation: Operation, runner_id: str) -> IngressGrant | IngressReply:
        binding = self._binding(scope, operation, runner_id)
        if isinstance(binding, Rejected):
            if "native_ingress_binding" not in operation.receipts:
                return IngressReply(200, {"token": "omo_stock_auth_only", "expires_at": self.clock() + self.ttl_s})
            return _denied(binding.code)
        token, expires = TOKEN_PREFIX + secrets.token_urlsafe(32), self.clock() + self.ttl_s
        with self.store._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation.spec.operation_id,)).fetchone()
            current = _operation(row)
            existing = current.receipts.get("native_ingress_binding")
            if existing is not None and existing != binding:
                return _denied("native_generation_changed")
            if existing is None:
                db.execute("UPDATE operations SET receipts=? WHERE operation_id=?", (canonical(current.receipts | {"native_ingress_binding": binding}), operation.spec.operation_id))
            db.execute("INSERT INTO ingress_tokens VALUES (?,?,?,?,NULL)", (digest(token.encode()), operation.spec.operation_id, canonical(binding), expires))
        return IngressReply(200, {"token": token, "expires_at": expires})

    def handle(self, scope: Message, payload: object) -> IngressGrant | IngressReply | IngressDeferred | None:
        path, method = unquote(scope["path"]).rstrip("/"), scope["method"]
        pairs = scope.get("headers", [])
        headers = {key.lower(): value for key, value in pairs}
        bearer = headers.get(b"authorization", b"").decode("latin1").removeprefix("Bearer ")
        binding_token = headers.get(BINDING_HEADER, b"").decode("latin1")
        runner_id = _runner_for_token(binding_token) if binding_token else None
        session = self.store.session_for_runner(runner_id) if runner_id else None
        runner_operation = self.store.for_successor_session(session) if session else None
        match = re.fullmatch(r"/v1/sessions/([^/]+)(.*)", path)
        operation = self.store.for_successor_session(match[1]) if match else None
        if not runner_operation and not operation and not bearer.startswith(TOKEN_PREFIX):
            return None
        if (runner_operation is not None or bearer.startswith(TOKEN_PREFIX)) and not self._on_endpoint(scope):
            return _denied("native_endpoint_mismatch")
        if any(sum(key.lower() == name for key, _ in pairs) > 1 for name in (BINDING_HEADER, b"authorization")):
            return _denied("ambiguous_native_credentials")
        mint_match = re.fullmatch(r"/v1/runners/([^/]+)/token", path)
        if mint_match and runner_operation:
            assert runner_id is not None
            if method != "POST" or mint_match[1] != runner_id or payload is not None or scope.get("query_string"):
                return _denied("invalid_native_mint")
            authentication = self._grant(scope, runner_operation, runner_id)
            if isinstance(authentication, IngressReply):
                return authentication
            if runner_operation.phase == "delivery_confirmed":
                return IngressReply(200, {"token": "omo_stock_auth_only", "expires_at": self.clock() + self.ttl_s})
            invalid = self._operation_error(runner_operation, runner_id)
            return invalid or self._mint(scope, runner_operation, runner_id)
        if bearer.startswith(TOKEN_PREFIX):
            return self._consume(scope, payload, bearer, operation, match[2] if match else "", runner_id)
        if operation is None:
            return None
        if operation.phase == "delivery_confirmed":
            request_hash = _request_digest(scope, payload, operation, match[2] if match else "")
            with self.store._transaction() as db:
                if db.execute("SELECT 1 FROM ingress_callbacks WHERE operation_id=? AND request_sha256=?", (operation.spec.operation_id, request_hash)).fetchone():
                    return _denied("native_callback_replayed")
            return None
        claimed = operation.receipts.get("successor_launch", {}).get("runner_id")
        if not isinstance(claimed, str):
            return None
        if runner_id is not None and runner_id == claimed and runner_operation == operation:
            if invalid := self._operation_error(operation, runner_id):
                return invalid
            suffix = match[2] if match else ""
            query = parse_qs(scope.get("query_string", b"").decode(), keep_blank_values=True)
            query_ok = not query or (suffix == "/items" and query == {"limit": ["1"], "order": ["desc"]})
            if method == "GET" and payload is None and query_ok and suffix in ("", "/items", "/labels", "/agent/contents"):
                return self._grant(scope, operation, runner_id)
        if method == "PATCH" and match and match[2] == "" and isinstance(payload, dict) and set(payload) == {"external_session_id"}:
            return _denied("native_capability_required", 401)
        if method == "POST" and match and match[2] == "/events" and isinstance(payload, dict) and _native_scope(method, "/events", payload, None):
            return _denied("native_capability_required", 401)
        return None

    def _consume(self, scope: Message, payload: object, token: str, operation: Operation | None, suffix: str, runner_id: str | None) -> IngressGrant | IngressReply | IngressDeferred:
        token_hash = digest(token.encode())
        with self.store._transaction() as db:
            row = db.execute("SELECT * FROM ingress_tokens WHERE token_sha256=?", (token_hash,)).fetchone()
        if row is None or operation is None or row["operation_id"] != operation.spec.operation_id:
            return _denied("native_token_binding_mismatch")
        binding = json.loads(row["binding"])
        claimed = binding["runner_id"]
        if runner_id is not None and runner_id != claimed:
            return _denied("native_wrong_runner")
        if invalid := self._operation_error(operation, claimed):
            return invalid
        actual = self._binding(scope, operation, claimed)
        if isinstance(actual, Rejected) or actual != binding:
            return _denied("native_generation_changed")
        purpose = _native_scope(scope["method"], suffix, payload, binding["thread_id"])
        deferred = _tool_mirror(scope["method"], suffix, payload)
        if deferred:
            purpose = "deferred_tool_mirror"
        if purpose is None or scope.get("query_string"):
            return _denied("native_callback_scope_denied")
        if operation.phase not in ("committed", "delivery_confirmed") or "delivery_intent" not in operation.receipts:
            return _denied("native_delivery_not_claimed")
        if purpose == "external_conversation_item" and isinstance(payload, dict):
            item = payload["data"]["item_data"]
            if item["role"] == "user":
                dispatch = operation.receipts.get("manifest_dispatch_intent", {})
                content = item["content"]
                if (
                    set(item) != {"role", "content"}
                    or len(content) != 1
                    or content[0]["type"] != "input_text"
                    or digest(content[0]["text"].encode()) != operation.receipts["delivery_intent"].get("prompt_sha256")
                    or dispatch.get("prompt_sha256") != operation.receipts["delivery_intent"].get("prompt_sha256")
                    or dispatch.get("runner_id") != claimed
                    or dispatch.get("thread_id") != binding["thread_id"]
                ):
                    return _denied("native_manifest_mismatch")
        if deferred:
            if self.resolve_user(scope) != binding["owner"]:
                return _denied("native_deferred_stock_auth_denied")
            grant: IngressGrant | IngressReply | IngressDeferred = IngressDeferred(token_hash, operation.spec.operation_id, operation.spec.new_session_id)
        else:
            grant = self._grant(scope, operation, claimed)
            if isinstance(grant, IngressReply):
                return grant
        request_hash = _request_digest(scope, payload, operation, suffix)
        with self.store._transaction() as db:
            row = db.execute("SELECT * FROM ingress_tokens WHERE token_sha256=?", (token_hash,)).fetchone()
            if row["used_request"] is not None:
                return _denied("native_token_replayed")
            if row["expires_at_s"] <= self.clock():
                return _denied("native_token_expired")
            if (
                purpose in ("native_metadata", "external_conversation_item", "deferred_tool_mirror")
                and db.execute("SELECT 1 FROM ingress_callbacks WHERE operation_id=? AND request_sha256=?", (operation.spec.operation_id, request_hash)).fetchone()
            ):
                return _denied("native_callback_replayed")
            db.execute("UPDATE ingress_tokens SET used_request=? WHERE token_sha256=?", (request_hash, token_hash))
            if purpose in ("native_metadata", "external_conversation_item", "deferred_tool_mirror"):
                db.execute("INSERT INTO ingress_callbacks VALUES (?,?,?)", (operation.spec.operation_id, request_hash, purpose))
            if deferred:
                db.execute(
                    "INSERT INTO ingress_pending VALUES (?,?,?,?,?)",
                    (token_hash, operation.spec.operation_id, request_hash, canonical({"method": scope["method"], "path": scope["path"], "body": payload}), "pending"),
                )
        return grant

    async def wait_for_stock(self, scope: Message, deferred: IngressDeferred) -> IngressReply | None:
        """Release unchanged bytes only after stock admission independently opens."""
        deadline = time.monotonic() + self.wait_timeout_s
        released = False
        try:
            while time.monotonic() < deadline:
                with self.store._transaction() as db:
                    token = db.execute("SELECT * FROM ingress_tokens WHERE token_sha256=?", (deferred.token_sha256,)).fetchone()
                    pending = db.execute("SELECT status FROM ingress_pending WHERE token_sha256=?", (deferred.token_sha256,)).fetchone()
                if token is None or pending is None or pending[0] != "pending" or token["expires_at_s"] <= self.clock():
                    return _denied("native_deferred_expired_or_replayed")
                binding = json.loads(token["binding"])
                operation = self.store.get(deferred.operation_id)
                if operation is None or self._operation_error(operation, binding["runner_id"]) or self._binding(scope, operation, binding["runner_id"]) != binding:
                    return _denied("native_deferred_generation_changed")
                if operation.phase == "delivery_confirmed":
                    if self.resolve_user(scope) != binding["owner"]:
                        return _denied("native_deferred_stock_auth_denied")
                    with self.store._transaction() as db:
                        changed = db.execute("UPDATE ingress_pending SET status='released' WHERE token_sha256=? AND status='pending'", (deferred.token_sha256,)).rowcount
                    if changed != 1:
                        return _denied("native_deferred_replayed")
                    released = True
                    return None
                await asyncio.sleep(0.02)
            return _denied("native_deferred_confirmation_timeout")
        finally:
            if not released:
                with self.store._transaction() as db:
                    db.execute("UPDATE ingress_pending SET status='uncertain' WHERE token_sha256=? AND status='pending'", (deferred.token_sha256,))
