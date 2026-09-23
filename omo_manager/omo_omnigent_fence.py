"""Durable replacement admission and ASGI fencing; native shutdown is external."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

PHASES = ("prepared", "fenced", "old_quiesced", "new_prepared", "committed", "delivery_confirmed")
OMNIGENT_VERSION = "0.11.0"
OMNIGENT_SOURCE_SHA256 = "828c324f54096440d0be15eb2744a923bfa99da7cb1a06b95c6f26af1a6d3475"
HASH = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_.:-]+\Z")
MAX_BODY_BYTES = 16 * 1024 * 1024
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGI = Callable[[Message, Receive, Send], Awaitable[None]]
_runner_context: ContextVar[str | None] = ContextVar("omnigent_fence_runner", default=None)
_operation_context: ContextVar[str | None] = ContextVar("omnigent_fence_operation", default=None)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _canonical_id(value: str) -> str:
    try:
        return uuid.UUID(value).hex
    except ValueError:
        return value


def _runner_for_token(token: str) -> str:
    from omnigent.runner.identity import token_bound_runner_id

    return token_bound_runner_id(token)


async def _cancel_receive_task(task: asyncio.Task[Message]) -> None:
    """Join receive-only children without replacing the caller's cancellation."""
    from anyio import CancelScope

    task.cancel()
    with CancelScope(shield=True):
        await asyncio.gather(task, return_exceptions=True)


@dataclass(frozen=True)
class Rejected:
    code: str
    detail: str


@dataclass(frozen=True)
class IngressReply:
    status: int
    body: dict[str, object]


@dataclass(frozen=True)
class IngressGrant:
    session_id: str
    runner_id: str
    headers: list[tuple[bytes, bytes]]


@dataclass(frozen=True)
class IngressDeferred:
    token_sha256: str
    operation_id: str
    session_id: str


class HTTPIngress(Protocol):
    def handle(self, scope: Message, payload: object) -> IngressGrant | IngressReply | IngressDeferred | None: ...

    async def wait_for_stock(self, scope: Message, deferred: IngressDeferred) -> IngressReply | None: ...


@dataclass(frozen=True)
class ExpectedState:
    session_id: str
    runner_id: str
    host_id: str
    thread_id: str
    task_sha256: str
    todo_sha256: str
    queue_sha256: str
    workspace: str
    workspace_sha256: str
    host_sha256: str
    process_sha256: str
    routing_sha256: str
    queue_count: int
    routing_generation: int
    host_generation: int


# 🧑 "Replace the current read-only owner atomically with one unrestricted OmniGent session ... preserving the task and queue and delivering only open work."
@dataclass(frozen=True)
class ReplacementSpec:
    operation_id: str
    new_session_id: str
    authority_sha256: str
    request_sha256: str
    expected: ExpectedState

    @property
    def old_session_id(self) -> str:
        return self.expected.session_id


@dataclass(frozen=True)
class Operation:
    spec: ReplacementSpec
    phase: str
    status: str
    receipts: dict[str, dict[str, object]]


@dataclass(frozen=True)
class Admission:
    token: str
    session_ids: tuple[str, ...]
    kind: str
    owner_pid: int
    owner_start_ticks: int
    owner_boot_id: str
    host_id: str | None = None


def _process_start(pid: int) -> int:
    return int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19])


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _operation(row: sqlite3.Row) -> Operation:
    value = json.loads(row["spec"])
    value["expected"] = ExpectedState(**value["expected"])
    return Operation(ReplacementSpec(**value), row["phase"], row["status"], json.loads(row["receipts"]))


def _validate(spec: ReplacementSpec) -> Rejected | None:
    for key, value in asdict(spec.expected).items():
        if key.endswith("sha256") and (not isinstance(value, str) or not HASH.fullmatch(value)):
            return Rejected("invalid_binding", key)
        if key.endswith("_id") and (not isinstance(value, str) or not IDENTITY.fullmatch(value)):
            return Rejected("invalid_binding", key)
    for value in (spec.operation_id, spec.new_session_id):
        if not isinstance(value, str) or not IDENTITY.fullmatch(value):
            return Rejected("invalid_identity", "operation and successor identities must be nonempty")
    if not HASH.fullmatch(spec.authority_sha256) or not HASH.fullmatch(spec.request_sha256):
        return Rejected("invalid_digest", "authority and request require SHA-256")
    if _canonical_id(spec.old_session_id) == _canonical_id(spec.new_session_id) or spec.expected.queue_count != 9 or type(spec.expected.queue_count) is not int:
        return Rejected("invalid_binding", "replacement requires a fresh identity and the complete nine-item queue")
    if type(spec.expected.routing_generation) is not int or spec.expected.routing_generation < 0:
        return Rejected("invalid_binding", "routing_generation must be a nonnegative integer")
    if type(spec.expected.host_generation) is not int or spec.expected.host_generation < 0:
        return Rejected("invalid_binding", "host_generation must be a nonnegative integer")
    if not isinstance(spec.expected.workspace, str) or not Path(spec.expected.workspace).is_absolute():
        return Rejected("invalid_binding", "workspace must be absolute")
    return None


def _quiescence_error(operation: Operation, receipt: dict[str, object]) -> Rejected | None:
    expected = operation.spec.expected
    bindings = {key: getattr(expected, key) for key in ("session_id", "runner_id", "thread_id", "process_sha256", "host_sha256")}
    bindings["operation_id"] = operation.spec.operation_id
    if (
        any(receipt.get(key) != value for key, value in bindings.items())
        or any(receipt.get(key) is not True for key in ("native_writers_gone", "pending_launches_absent", "transport_drained"))
        or not HASH.fullmatch(str(receipt.get("evidence_sha256", "")))
    ):
        return Rejected("invalid_quiescence", "native writers and pending launches need independent bound verification")
    return None


def validate_fence_path(path: Path) -> Rejected | None:
    """Validate configured storage without creating directories or database files."""
    path = Path(path)
    try:
        parent = path.parent
        if not path.is_absolute() or parent.resolve(strict=True) != parent:
            return Rejected("unsafe_fence_path", "fence path must have an existing canonical private parent")
        info = parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return Rejected("unsafe_fence_path", "fence directory must be owned by this user with mode 0700")
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            return Rejected("unsafe_fence_path", "fence database must be an owned private regular file with one link")
    except OSError as error:
        return Rejected("unsafe_fence_path", str(error))
    return None


class FenceStore:
    """One durable admission owner, serialized by SQLite transactions.

    Only the trusted coordinator may call lifecycle methods. Receipt contents
    are assertions from its independent verifiers, never HTTP client input.
    Outstanding remote-work admissions survive connection loss and process death.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        parent = self.path.parent
        invalid = validate_fence_path(self.path)
        if invalid:
            raise ValueError(invalid.detail)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.fsync(fd)
            os.close(fd)
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        self._validate_path()
        db = self._connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY, old_session_id TEXT NOT NULL,
                    new_session_id TEXT NOT NULL, runner_id TEXT NOT NULL, host_id TEXT NOT NULL,
                    spec TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,
                    receipts TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS old_owner ON operations(old_session_id) WHERE status!='cancelled';
                CREATE UNIQUE INDEX IF NOT EXISTS new_owner ON operations(new_session_id) WHERE status!='cancelled';
                CREATE UNIQUE INDEX IF NOT EXISTS old_runner ON operations(runner_id) WHERE status!='cancelled';
                CREATE TABLE IF NOT EXISTS admissions (
                    token TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS admission_sessions (
                    token TEXT NOT NULL REFERENCES admissions(token) ON DELETE CASCADE,
                    session_id TEXT NOT NULL, PRIMARY KEY(token, session_id));
                CREATE TABLE IF NOT EXISTS generations (
                    session_id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS host_generations (
                    host_id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY, operation_id TEXT, action TEXT NOT NULL,
                    evidence TEXT NOT NULL);
            """)
        finally:
            db.close()

    def _validate_path(self) -> None:
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("fence database must be an owned private regular file with one link")

    def _connect(self) -> sqlite3.Connection:
        self._validate_path()
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, operation_id: str) -> Operation | None:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            return _operation(row) if row else None

    def for_old_session(self, session_id: str) -> Operation | None:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE old_session_id=? AND status!='cancelled'", (_canonical_id(session_id),)).fetchone()
            return _operation(row) if row else None

    def for_successor_session(self, session_id: str) -> Operation | None:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE new_session_id=? AND status!='cancelled'", (_canonical_id(session_id),)).fetchone()
            return _operation(row) if row else None

    @contextmanager
    def successor_context(self, operation_id: str) -> Iterator[None]:
        """Attach trusted coordinator provenance; every admission still validates it."""
        token = _operation_context.set(operation_id)
        try:
            yield
        finally:
            _operation_context.reset(token)

    def prepare(self, spec: ReplacementSpec, observed: ExpectedState, authority_bytes: bytes, request_bytes: bytes) -> Operation | Rejected:
        invalid = _validate(spec)
        if invalid:
            return invalid
        if digest(authority_bytes) != spec.authority_sha256 or digest(request_bytes) != spec.request_sha256:
            return Rejected("authority_or_request_mismatch", "raw bytes differ from the bound request")
        serialized = canonical(asdict(spec))
        with self._transaction() as db:
            existing = db.execute("SELECT * FROM operations WHERE operation_id=?", (spec.operation_id,)).fetchone()
            if existing:
                if existing["status"] == "cancelled":
                    return Rejected("operation_cancelled", "cancelled operation IDs cannot be reused")
                return _operation(existing) if existing["spec"] == serialized else Rejected("operation_conflict", "operation ID is bound to different inputs")
            if spec.expected != observed or self._generation(db, spec.old_session_id) != observed.routing_generation or self._host_generation(db, observed.host_id) != observed.host_generation:
                return Rejected("expected_state_mismatch", "observed identities or revisions changed")
            owners = (_canonical_id(spec.old_session_id), _canonical_id(spec.new_session_id))
            conflict = db.execute(
                "SELECT 1 FROM operations WHERE status!='cancelled' AND (old_session_id IN (?,?) OR new_session_id IN (?,?) OR runner_id=?)", (*owners, *owners, spec.expected.runner_id)
            ).fetchone()
            if conflict:
                return Rejected("replacement_conflict", "old owner, successor, or runner already reserved")
            receipts: dict[str, dict[str, object]] = {
                "exact_packet": {"operation_id": spec.operation_id, "evidence_sha256": spec.request_sha256, "packet_b64": base64.b64encode(request_bytes).decode("ascii")}
            }
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?)",
                (spec.operation_id, *owners, spec.expected.runner_id, _canonical_id(spec.expected.host_id), serialized, "prepared", "active", canonical(receipts)),
            )
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (spec.operation_id, "prepared", serialized))
            return Operation(spec, "prepared", "active", receipts)

    def fence(self, operation_id: str, observed: ExpectedState) -> Operation | Rejected:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            if operation.phase != "prepared":
                return operation
            if (
                operation.status != "active"
                or operation.spec.expected != observed
                or self._generation(db, observed.session_id) != observed.routing_generation
                or self._host_generation(db, observed.host_id) != observed.host_generation
            ):
                return Rejected("expected_state_mismatch", "fence requires the exact prepared state")
            active = db.execute("SELECT payload FROM admissions JOIN admission_sessions USING(token) WHERE session_id=?", (_canonical_id(observed.session_id),)).fetchall()
            if any(json.loads(row[0])["kind"] not in ("websocket", "observe", "history", "remote:stream") for row in active):
                return Rejected("admissions_outstanding", "mutating old work must finish before the expected-state fence")
            if db.execute("SELECT 1 FROM admissions WHERE json_extract(payload,'$.host_id') IN (?, '*')", (_canonical_id(observed.host_id),)).fetchone():
                return Rejected("host_launch_outstanding", "an admitted launch still owns the host")
            if self._host_admission_error(db, observed.host_id, observed.session_id):
                return Rejected("host_reserved", "another replacement holds the host")
            db.execute("UPDATE operations SET phase='fenced' WHERE operation_id=?", (operation_id,))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, "fenced", canonical(asdict(observed))))
            return Operation(operation.spec, "fenced", "active", operation.receipts)

    def cancel_prepared(self, operation_id: str, receipt: dict[str, object]) -> Operation | Rejected:
        """Release an unused reservation after the coordinator verifies no creation."""
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            if operation.status == "cancelled":
                return operation if operation.receipts.get("cancelled") == receipt else Rejected("receipt_conflict", "cancelled")
            if operation.phase != "prepared":
                return Rejected("irreversible_fence", "a fenced owner can never be restored")
            if (
                receipt.get("operation_id") != operation_id
                or receipt.get("new_session_id") != operation.spec.new_session_id
                or receipt.get("successor_absent") is not True
                or not HASH.fullmatch(str(receipt.get("evidence_sha256", "")))
            ):
                return Rejected("invalid_receipt", "cancellation requires verified successor absence")
            receipts = operation.receipts | {"cancelled": receipt}
            db.execute("UPDATE operations SET status='cancelled',receipts=? WHERE operation_id=?", (canonical(receipts), operation_id))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, "cancelled", canonical(receipt)))
            return Operation(operation.spec, operation.phase, "cancelled", receipts)

    def is_fenced(self, session_id: str) -> bool:
        with self._transaction() as db:
            return db.execute("SELECT 1 FROM operations WHERE old_session_id=? AND phase!='prepared'", (_canonical_id(session_id),)).fetchone() is not None

    def is_public_blocked(self, session_id: str) -> bool:
        session_id = _canonical_id(session_id)
        with self._transaction() as db:
            return (
                db.execute(
                    "SELECT 1 FROM operations WHERE (old_session_id=? AND phase!='prepared') OR (new_session_id=? AND status!='cancelled' AND phase!='delivery_confirmed')", (session_id, session_id)
                ).fetchone()
                is not None
            )

    def successor_dispatch_conflict(self, session_id: str, *, initialization: bool, runner_id: str | None = None) -> Rejected | None:
        session_id = _canonical_id(session_id)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE new_session_id=? AND status!='cancelled'", (session_id,)).fetchone()
            if not row or row["phase"] == "delivery_confirmed":
                return None
            operation = _operation(row)
            if runner_id != operation.receipts.get("successor_launch", {}).get("runner_id") or runner_id is None:
                return Rejected("successor_runner_mismatch", "successor transport must match the uniquely claimed runner")
            if initialization and PHASES.index(operation.phase) >= PHASES.index("old_quiesced"):
                return None
            if operation.phase == "committed" and "manifest_dispatch_intent" in operation.receipts:
                return None
            return Rejected("successor_reserved", "successor dispatch requires committed custody and a durable delivery intent")

    def session_for_runner(self, runner_id: str) -> str | None:
        with self._transaction() as db:
            row = db.execute("SELECT old_session_id FROM operations WHERE runner_id=? AND status!='cancelled'", (runner_id,)).fetchone()
            if row:
                return row[0]
            row = db.execute("SELECT new_session_id FROM operations WHERE json_extract(receipts,'$.successor_launch.runner_id')=? AND status!='cancelled'", (runner_id,)).fetchone()
            return row[0] if row else None

    def claim_successor_launch(self, host_id: str, session_id: str, runner_id: str, request_id: str) -> Operation | Rejected | None:
        """Bind one physical launch attempt before emitting its host command."""
        host_id, session_id = _canonical_id(host_id), _canonical_id(session_id)
        if not IDENTITY.fullmatch(runner_id) or not IDENTITY.fullmatch(request_id):
            return Rejected("invalid_launch", "launch requires exact runner and request identities")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE new_session_id=? AND status!='cancelled'", (session_id,)).fetchone()
            if not row or row["phase"] == "delivery_confirmed":
                return None
            operation = _operation(row)
            if host_id != _canonical_id(operation.spec.expected.host_id) or PHASES.index(operation.phase) < PHASES.index("old_quiesced"):
                return Rejected("successor_reserved", "launch requires the prepared host and verified old quiescence")
            if "successor_launch" in operation.receipts:
                return Rejected("launch_already_claimed", "reconcile the prior attempt; do not respawn")
            if db.execute("SELECT 1 FROM operations WHERE runner_id=? OR json_extract(receipts,'$.successor_launch.runner_id')=?", (runner_id, runner_id)).fetchone():
                return Rejected("runner_conflict", "runner identity already belongs to a replacement")
            receipt: dict[str, object] = {
                "operation_id": operation.spec.operation_id,
                "host_id": host_id,
                "session_id": session_id,
                "runner_id": runner_id,
                "request_id": request_id,
                "server_pid": os.getpid(),
                "server_start_ticks": _process_start(os.getpid()),
                "server_boot_id": _boot_id(),
            }
            receipts = operation.receipts | {"successor_launch": receipt}
            db.execute("UPDATE operations SET receipts=? WHERE operation_id=?", (canonical(receipts), operation.spec.operation_id))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation.spec.operation_id, "successor_launch", canonical(receipt)))
            return Operation(operation.spec, operation.phase, operation.status, receipts)

    @staticmethod
    def _generation(db: sqlite3.Connection, session_id: str) -> int:
        row = db.execute("SELECT generation FROM generations WHERE session_id=?", (_canonical_id(session_id),)).fetchone()
        return row[0] if row else 0

    def generation(self, session_id: str) -> int:
        with self._transaction() as db:
            return self._generation(db, session_id)

    @staticmethod
    def _host_generation(db: sqlite3.Connection, host_id: str) -> int:
        row = db.execute("SELECT generation FROM host_generations WHERE host_id=?", (_canonical_id(host_id),)).fetchone()
        return row[0] if row else 0

    def host_generation(self, host_id: str) -> int:
        with self._transaction() as db:
            return self._host_generation(db, host_id)

    @staticmethod
    def _host_admission_error(db: sqlite3.Connection, host_id: str, session_id: str) -> Rejected | None:
        host_id, session_id = _canonical_id(host_id), _canonical_id(session_id)
        rows = db.execute("SELECT * FROM operations WHERE (host_id=? OR ?='*') AND phase NOT IN ('prepared','delivery_confirmed')", (host_id, host_id)).fetchall()
        for row in rows:
            if session_id != row["new_session_id"] or PHASES.index(row["phase"]) < PHASES.index("old_quiesced"):
                return Rejected("host_reserved", "replacement holds exclusive launch admission on this host")
        return None

    def host_launch_conflict(self, host_id: str, session_id: str) -> Rejected | None:
        with self._transaction() as db:
            return self._host_admission_error(db, host_id, session_id)

    def admit_host_launch(self, host_id: str, session_id: str, kind: str) -> Admission | Rejected:
        return self.admit([session_id] if session_id else [], kind, host_id=host_id)

    def admit(self, session_ids: Iterable[str], kind: str, *, host_id: str | None = None, public: bool = False) -> Admission | Rejected:
        sessions = tuple(sorted({_canonical_id(session_id) for session_id in session_ids}))
        host_id = _canonical_id(host_id) if host_id else None
        admission = Admission(uuid.uuid4().hex, sessions, kind, os.getpid(), _process_start(os.getpid()), _boot_id(), host_id)
        with self._transaction() as db:
            if host_id:
                invalid_host = self._host_admission_error(db, host_id, sessions[0] if len(sessions) == 1 else "")
                if invalid_host:
                    return invalid_host
            for session_id in sessions:
                if kind != "history" and db.execute("SELECT 1 FROM operations WHERE old_session_id=? AND phase!='prepared'", (session_id,)).fetchone():
                    return Rejected("session_fenced", session_id)
                if public and db.execute("SELECT 1 FROM operations WHERE new_session_id=? AND status!='cancelled' AND phase!='delivery_confirmed'", (session_id,)).fetchone():
                    return Rejected("successor_reserved", session_id)
                if kind.startswith("store:"):
                    reserved = db.execute("SELECT * FROM operations WHERE new_session_id=? AND status!='cancelled' AND phase!='delivery_confirmed'", (session_id,)).fetchone()
                    if reserved:
                        operation = _operation(reserved)
                        runner_id = operation.receipts.get("successor_launch", {}).get("runner_id")
                        trusted_operation = _operation_context.get() == operation.spec.operation_id
                        trusted_runner = isinstance(runner_id, str) and _runner_context.get() == runner_id
                        if PHASES.index(operation.phase) < PHASES.index("old_quiesced") or not isinstance(runner_id, str) or not (trusted_operation or trusted_runner):
                            return Rejected("successor_provenance", "reserved successor writes require the claimed runner or exact coordinator operation")
            if sessions or host_id:
                if host_id:
                    db.execute("INSERT INTO host_generations VALUES (?,1) ON CONFLICT(host_id) DO UPDATE SET generation=generation+1", (host_id,))
                if kind not in ("websocket", "observe", "history", "remote:stream"):
                    db.executemany("INSERT INTO generations VALUES (?,1) ON CONFLICT(session_id) DO UPDATE SET generation=generation+1", [(session_id,) for session_id in sessions])
                db.execute("INSERT INTO admissions VALUES (?,?)", (admission.token, canonical(asdict(admission))))
                db.executemany("INSERT INTO admission_sessions VALUES (?,?)", [(admission.token, session_id) for session_id in sessions])
        return admission

    def finish(self, admission: Admission) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM admissions WHERE token=? AND payload=?", (admission.token, canonical(asdict(admission))))

    def active_admissions(self, session_id: str) -> tuple[Admission, ...]:
        with self._transaction() as db:
            rows = db.execute("SELECT payload FROM admissions JOIN admission_sessions USING(token) WHERE session_id=?", (_canonical_id(session_id),)).fetchall()
            values = [json.loads(row[0]) for row in rows]
            return tuple(Admission(**(value | {"session_ids": tuple(value["session_ids"])})) for value in values)

    def reconcile_admission(self, admission: Admission, evidence_sha256: str) -> Rejected | None:
        """Clear a crashed local holder only after checking its exact generation died."""
        if not HASH.fullmatch(evidence_sha256):
            return Rejected("invalid_evidence", "a durable reconciliation evidence digest is required")
        try:
            alive = admission.owner_boot_id == _boot_id() and _process_start(admission.owner_pid) == admission.owner_start_ticks
        except FileNotFoundError:
            alive = False
        except (OSError, ValueError, IndexError):
            return Rejected("unverifiable_holder", admission.token)
        if alive:
            return Rejected("live_holder", admission.token)
        with self._transaction() as db:
            db.execute("DELETE FROM admissions WHERE token=? AND payload=?", (admission.token, canonical(asdict(admission))))
            db.execute("INSERT INTO audit(action,evidence) VALUES (?,?)", ("dead_holder_reconciled", canonical({"admission": asdict(admission), "evidence_sha256": evidence_sha256})))
        return None

    def reconcile_remote_admissions(self, operation_id: str, receipt: dict[str, object]) -> Rejected | None:
        """Record the trusted remote shutdown barrier, retaining an audit of leases."""
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            error = _quiescence_error(operation, receipt)
            if error:
                return error
            if operation.phase == "prepared":
                return Rejected("phase_conflict", "remote reconciliation requires permanent retirement")
            rows = db.execute("SELECT payload FROM admissions JOIN admission_sessions USING(token) WHERE session_id=?", (_canonical_id(operation.spec.old_session_id),)).fetchall()
            retired = [json.loads(row[0]) for row in rows if json.loads(row[0])["kind"].startswith("remote:")]
            db.executemany("DELETE FROM admissions WHERE token=?", [(admission["token"],) for admission in retired])
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, "remote_barrier_reconciled", canonical({"receipt": receipt, "admissions": retired})))
        return None

    def record_evidence(self, operation_id: str, key: str, receipt: dict[str, object]) -> Operation | Rejected:
        """Persist one immutable coordinator journal entry without advancing phase."""
        if not key or key in (*PHASES, "cancelled", "delivery_intent", "manifest_dispatch_intent", "successor_launch", "exact_packet", "native_ingress_binding"):
            return Rejected("reserved_receipt", key)
        if receipt.get("operation_id") != operation_id or not HASH.fullmatch(str(receipt.get("evidence_sha256", ""))):
            return Rejected("invalid_receipt", "journal entries require bound durable evidence")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            if operation.status == "cancelled":
                return Rejected("operation_cancelled", operation_id)
            if key in operation.receipts:
                return operation if operation.receipts[key] == receipt else Rejected("receipt_conflict", key)
            receipts = operation.receipts | {key: receipt}
            db.execute("UPDATE operations SET receipts=? WHERE operation_id=?", (canonical(receipts), operation_id))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, key, canonical(receipt)))
            return Operation(operation.spec, operation.phase, operation.status, receipts)

    def claim_delivery(self, operation_id: str, delivery_id: str, prompt_sha256: str) -> Operation | Rejected:
        """Claim one dispatch attempt; identical retries must reconcile, never resend."""
        if not delivery_id or not HASH.fullmatch(prompt_sha256):
            return Rejected("invalid_delivery", "stable delivery ID and exact prompt digest are required")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            if "delivery_intent" in operation.receipts:
                return Rejected("delivery_already_claimed", "reconcile the existing attempt; do not resend")
            if operation.phase != "committed" or operation.status != "active":
                return Rejected("phase_conflict", "delivery requires an active committed replacement")
            receipt: dict[str, object] = {"operation_id": operation_id, "new_session_id": operation.spec.new_session_id, "delivery_id": delivery_id, "prompt_sha256": prompt_sha256}
            receipts = operation.receipts | {"delivery_intent": receipt}
            db.execute("UPDATE operations SET receipts=? WHERE operation_id=?", (canonical(receipts), operation_id))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, "delivery_intent", canonical(receipt)))
            return Operation(operation.spec, operation.phase, operation.status, receipts)

    def claim_manifest_dispatch(self, operation_id: str, delivery_id: str, prompt_sha256: str, runner_id: str, thread_id: str) -> Operation | Rejected:
        """Claim the sole native input after separately verified initialization."""
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            if "manifest_dispatch_intent" in operation.receipts:
                return Rejected("manifest_dispatch_already_claimed", "inspect the first attempt; never resend")
            delivery = operation.receipts.get("delivery_intent", {})
            native = operation.receipts.get("native_successor_verified", {})
            if (
                operation.phase != "committed"
                or operation.status not in ("active", "uncertain")
                or delivery.get("delivery_id") != delivery_id
                or delivery.get("prompt_sha256") != prompt_sha256
                or operation.receipts.get("successor_launch", {}).get("runner_id") != runner_id
                or native.get("thread_id") != thread_id
                or not thread_id
            ):
                return Rejected("manifest_dispatch_binding_mismatch", "input requires exact delivery, claimed runner and independently verified native thread")
            receipt: dict[str, object] = {
                "operation_id": operation_id,
                "new_session_id": operation.spec.new_session_id,
                "delivery_id": delivery_id,
                "prompt_sha256": prompt_sha256,
                "runner_id": runner_id,
                "thread_id": thread_id,
            }
            receipts = operation.receipts | {"manifest_dispatch_intent": receipt}
            db.execute("UPDATE operations SET receipts=? WHERE operation_id=?", (canonical(receipts), operation_id))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, "manifest_dispatch_intent", canonical(receipt)))
            return Operation(operation.spec, operation.phase, operation.status, receipts)

    def mark_uncertain(self, operation_id: str, reason: str) -> Operation | Rejected:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            if operation.status == "cancelled":
                return Rejected("operation_cancelled", operation_id)
            db.execute("UPDATE operations SET status='uncertain' WHERE operation_id=?", (operation_id,))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, "uncertain", canonical({"reason": reason})))
            return Operation(operation.spec, operation.phase, "uncertain", operation.receipts)

    def reconcile(self, operation_id: str, receipt: dict[str, object]) -> Operation | Rejected:
        """Record explicit reconciliation; only verified resolution permits resumption."""
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            if operation.status not in ("uncertain", "reconciliation") or receipt.get("operation_id") != operation_id or not HASH.fullmatch(str(receipt.get("evidence_sha256", ""))):
                return Rejected("invalid_reconciliation", "uncertain operation needs bound reconciliation evidence")
            status = "active" if receipt.get("resolved") is True else "reconciliation"
            db.execute("UPDATE operations SET status=? WHERE operation_id=?", (status, operation_id))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, "reconciliation", canonical(receipt)))
            return Operation(operation.spec, operation.phase, status, operation.receipts)

    def advance(self, operation_id: str, phase: str, receipt: dict[str, object]) -> Operation | Rejected:
        """Accept independently verified coordinator receipts, never public payloads."""
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                return Rejected("unknown_operation", operation_id)
            operation = _operation(row)
            if phase in operation.receipts:
                return operation if operation.receipts[phase] == receipt else Rejected("receipt_conflict", phase)
            if operation.status != "active" or phase not in PHASES or PHASES.index(phase) != PHASES.index(operation.phase) + 1 or phase == "fenced":
                return Rejected("phase_conflict", "phase must advance once from active state")
            if receipt.get("operation_id") != operation_id or not isinstance(receipt.get("evidence_sha256"), str) or not HASH.fullmatch(str(receipt["evidence_sha256"])):
                return Rejected("invalid_receipt", "receipt lacks operation-bound durable evidence")
            if phase == "old_quiesced":
                expected = operation.spec.expected
                error = _quiescence_error(operation, receipt)
                if error:
                    return error
                if db.execute("SELECT 1 FROM admission_sessions WHERE session_id=?", (_canonical_id(expected.session_id),)).fetchone():
                    return Rejected("admissions_outstanding", "admitted old work has not drained or been reconciled")
            elif receipt.get("new_session_id") != operation.spec.new_session_id:
                return Rejected("invalid_receipt", "receipt does not bind the reserved successor")
            if phase == "delivery_confirmed" and (not receipt.get("delivery_id") or receipt.get("delivery_id") != operation.receipts.get("delivery_intent", {}).get("delivery_id")):
                return Rejected("invalid_receipt", "delivery confirmation must match its single durable intent")
            receipts = operation.receipts | {phase: receipt}
            db.execute("UPDATE operations SET phase=?,receipts=? WHERE operation_id=?", (phase, canonical(receipts), operation_id))
            db.execute("INSERT INTO audit(operation_id,action,evidence) VALUES (?,?,?)", (operation_id, phase, canonical(receipt)))
            return Operation(operation.spec, phase, "active", receipts)


def _strict_json(raw: str | bytes) -> object:
    def pairs(entries: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in entries:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> object:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _session_ids(value: object) -> set[str]:
    if isinstance(value, dict):
        sessions = {
            _canonical_id(item) for key, item in value.items() if key in ("session_id", "conversation_id", "parent_session_id", "target_session_id", "source_session_id") and isinstance(item, str)
        }
        initialization = value.get("session_init")
        if isinstance(initialization, dict) and isinstance(initialization.get("session_id"), str):
            sessions.add(_canonical_id(initialization["session_id"]))
        return sessions
    return set()


def _path_sessions(path: str) -> set[str]:
    return {_canonical_id(value) for value in re.findall(r"/(?:sessions|conversations)/([^/?]+)", unquote(urlsplit(path).path))}


def _require_tunnel_frame(raw: str, *, is_host: bool) -> None:
    """Reject non-protocol tunnel JSON; host tunnels also carry runner ping/pong."""
    if is_host:
        from omnigent.host.frames import decode_host_frame
        from omnigent.runner.transports.ws_tunnel.frames import PingFrame, PongFrame, decode_frame

        try:
            decode_host_frame(raw)
            return
        except ValueError:
            keepalive = decode_frame(raw)
            if isinstance(keepalive, PingFrame | PongFrame):
                return
            raise ValueError("host tunnel frame is not a host frame or runner keepalive")
    from omnigent.runner.transports.ws_tunnel.frames import decode_frame

    decode_frame(raw)


class FenceGuard:
    """ASGI guard for HTTP, server tunnels, and terminal attachments.

    It fences remote admission and tracks issued frames through acknowledgments.
    Native direct sockets and already running processes require separate shutdown.
    """

    def __init__(self, app: ASGI, store: FenceStore, *, ingress: HTTPIngress | None = None):
        self.app = app
        self.store = store
        self.ingress = ingress

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        runner_match = re.search(r"/runners/([^/]+)/tunnel/?$", scope["path"]) if scope["type"] == "websocket" else None
        token = _runner_context.set(runner_match[1] if runner_match else None)
        try:
            if scope["type"] == "http":
                await self._http(scope, receive, send)
            else:
                await self._websocket(scope, receive, send)
        finally:
            _runner_context.reset(token)

    async def _http(self, scope: Message, receive: Receive, send: Send) -> None:
        messages: list[Message] = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                await self._deny_http(send, 413, "request body too large for fenced admission")
                return
            if not message.get("more_body", False):
                break
        sessions = _path_sessions(scope["path"])
        payload: object = None
        runner_match = re.search(r"/runners/([^/]+)", scope["path"])
        if runner_match:
            bound_session = self.store.session_for_runner(runner_match[1])
            if bound_session:
                sessions.add(bound_session)
        query = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="strict"))
        sessions.update(_session_ids({key: values[-1] for key, values in query.items()}))
        if body:
            try:
                payload = _strict_json(bytes(body))
                sessions.update(_session_ids(payload))
            except (ValueError, UnicodeError):
                content_types = [value for key, value in scope.get("headers", []) if key.lower() == b"content-type"]
                if sessions or any(b"json" in value for value in content_types) or scope["path"].rstrip("/").endswith("/sessions"):
                    await self._deny_http(send, 400, "ambiguous JSON request")
                    return
        pure_read = scope.get("method") in ("GET", "HEAD") and bool(re.fullmatch(r"/v1/sessions/[^/]+(?:/items)?/?", scope["path"]))
        refresh = any(value.lower() not in ("false", "0") for key in ("refresh", "refresh_state") for value in query.get(key, []))
        history_read = scope.get("method") in ("GET", "HEAD") and not refresh and bool(re.fullmatch(r"/v1/sessions/[^/]+/items/?", scope["path"]))
        host_match = re.search(r"/hosts/([^/]+)/runners/?$", scope["path"]) if scope.get("method") == "POST" else None
        host_id = host_match[1] if host_match else None
        if scope.get("method") == "POST" and scope["path"].rstrip("/").endswith("/sessions"):
            host_id = payload.get("host_id") if isinstance(payload, dict) and isinstance(payload.get("host_id"), str) else "*"
        grant = None
        if self.ingress is not None:
            decision = self.ingress.handle(scope, payload)
            if isinstance(decision, IngressDeferred):
                waiting = self.store.admit([decision.session_id], "native:waiting")
                if isinstance(waiting, Rejected):
                    await self._deny_http(send, 409, waiting.code)
                    return
                try:
                    decision = await self.ingress.wait_for_stock(scope, decision)
                finally:
                    self.store.finish(waiting)
            if isinstance(decision, IngressReply):
                response = canonical(decision.body).encode()
                await send({"type": "http.response.start", "status": decision.status, "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": response})
                return
            if isinstance(decision, IngressGrant):
                if sessions != {_canonical_id(decision.session_id)}:
                    await self._deny_http(send, 409, "ingress session binding mismatch")
                    return
                grant = decision
                scope = dict(scope) | {"headers": decision.headers}
        runner_token = _runner_context.set(grant.runner_id if grant else None)
        admission = self.store.admit(sessions, "history" if history_read else "observe" if pure_read else "http", host_id=host_id, public=grant is None)
        if isinstance(admission, Rejected):
            _runner_context.reset(runner_token)
            await self._deny_http(send, 409, admission.code)
            return
        pending = iter(messages)

        async def replay() -> Message:
            return next(pending, None) or await receive()

        try:
            await self.app(scope, replay, send)
        finally:
            self.store.finish(admission)
            _runner_context.reset(runner_token)

    @staticmethod
    async def _deny_http(send: Send, status: int, detail: str) -> None:
        body = canonical({"error": detail}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    async def _websocket(self, scope: Message, receive: Receive, send: Send) -> None:
        path = scope["path"]
        sessions = _path_sessions(path)
        runner_match = re.search(r"/runners/([^/]+)/tunnel/?$", path)
        host_match = re.search(r"/hosts/([^/]+)/tunnel/?$", path)
        host_id = _canonical_id(host_match[1]) if host_match else None
        is_host = bool(host_match)
        is_tunnel = bool(runner_match or is_host)
        requests: dict[str, Admission] = {}
        channels: dict[str, Admission] = {}
        launches: dict[str, Admission] = {}
        launch_runners: dict[str, str] = {}
        rejected_launches: asyncio.Queue[Message] = asyncio.Queue()
        closed = False
        inbound_admission: Admission | None = None
        pending_input: asyncio.Task[Message] | None = None
        inspected_sessions: set[str] = set()

        def connection_sessions() -> set[str]:
            current = set(sessions)
            if runner_match:
                session_id = self.store.session_for_runner(runner_match[1])
                if session_id:
                    current.add(_canonical_id(session_id))
            return current

        def retired() -> bool:
            predicate = self.store.is_fenced if is_tunnel else self.store.is_public_blocked
            return any(predicate(session_id) for session_id in connection_sessions())

        async def close() -> None:
            nonlocal closed
            if not closed:
                closed = True
                await send({"type": "websocket.close", "code": 1008, "reason": "session fenced or ambiguous tunnel frame"})

        if retired():
            await close()
            return
        connection_admission = self.store.admit(sessions, "websocket", public=not is_tunnel)
        if isinstance(connection_admission, Rejected):
            await close()
            return

        def inspect(message: Message, outgoing: bool) -> bool:
            nonlocal inspected_sessions
            inspected_sessions = connection_sessions()
            if not is_tunnel or message["type"] not in ("websocket.send", "websocket.receive"):
                return True
            try:
                raw = message.get("text")
                if not isinstance(raw, str):
                    return False
                frame = _strict_json(raw)
                if not isinstance(frame, dict) or not isinstance(frame.get("kind"), str):
                    return False
                _require_tunnel_frame(raw, is_host=is_host)
                kind = frame["kind"]
                current = connection_sessions()
                work: dict[str, Admission] | None = None
                correlation = ""
                if kind == "request" and outgoing:
                    if not isinstance(frame.get("method"), str) or not isinstance(frame.get("path"), str):
                        return False
                    current.update(_path_sessions(frame["path"]))
                    query = parse_qs(str(frame.get("query_string", "")))
                    current.update(_session_ids({key: values[-1] for key, values in query.items()}))
                    body = frame.get("body")
                    headers = frame.get("headers", [])
                    needs_json = (
                        frame["path"].rstrip("/").endswith(("/sessions", "/summarize"))
                        or bool(_path_sessions(frame["path"]))
                        or any(str(key).lower() == "content-type" and "json" in str(value) for key, value in headers)
                    )
                    if body and needs_json:
                        if frame.get("encoding", "utf-8") == "base64":
                            body = base64.b64decode(body, validate=True)
                        elif frame.get("encoding", "utf-8") != "utf-8":
                            return False
                        current.update(_session_ids(_strict_json(body)))
                    work, correlation = requests, str(frame.get("id", ""))
                elif kind == "ws.open" and outgoing:
                    if not isinstance(frame.get("path"), str):
                        return False
                    current.update(_path_sessions(frame["path"]))
                    query = parse_qs(str(frame.get("query_string", "")))
                    current.update(_session_ids({key: values[-1] for key, values in query.items()}))
                    work, correlation = channels, str(frame.get("ch_id", ""))
                elif kind == "host.launch_runner" and outgoing:
                    session_id, token = frame.get("session_id"), frame.get("binding_token")
                    if not isinstance(session_id, str) or not session_id or not isinstance(token, str) or not token:
                        return False
                    current.add(_canonical_id(session_id))
                    bound_session = self.store.session_for_runner(_runner_for_token(token))
                    if bound_session:
                        current.add(bound_session)
                    work, correlation = launches, str(frame.get("request_id", ""))
                elif kind.startswith("response."):
                    admission = requests.get(str(frame.get("id", "")))
                    if admission:
                        current.update(admission.session_ids)
                    elif runner_match:
                        return False
                elif kind in ("ws.frame", "ws.close"):
                    admission = channels.get(str(frame.get("ch_id", "")))
                    if not admission:
                        return False
                    current.update(admission.session_ids)
                elif kind == "host.launch_runner_result":
                    admission = launches.get(str(frame.get("request_id", "")))
                    if not admission:
                        return False
                    if frame.get("status") not in ("launched", "failed") or (frame.get("status") == "launched" and frame.get("runner_id") != launch_runners.get(str(frame.get("request_id", "")))):
                        return False
                    current.update(admission.session_ids)
                elif kind not in ("hello", "ping", "pong", "request.cancel") and not (is_host and kind.startswith("host.")):
                    return False
                inspected_sessions = current
                if any(self.store.is_fenced(session_id) for session_id in current):
                    return False
                if kind in ("request", "ws.open", "ws.frame") and outgoing:
                    initialization = kind == "request" and (frame["method"] in ("GET", "HEAD") or (frame["method"] == "POST" and frame["path"].rstrip("/") == "/v1/sessions"))
                    if any(self.store.successor_dispatch_conflict(session_id, initialization=initialization, runner_id=runner_match[1] if runner_match else None) for session_id in current):
                        return False
                if work is not None:
                    if not correlation or correlation in work:
                        return False
                    if kind == "host.launch_runner" and host_match:
                        claim = self.store.claim_successor_launch(host_id or "", str(frame["session_id"]), _runner_for_token(str(frame["binding_token"])), correlation)
                        if isinstance(claim, Rejected):
                            return False
                    admission_kind = "remote:stream" if kind == "request" and frame.get("method") == "GET" and frame.get("stream") is True else "remote:" + kind
                    admission = self.store.admit(current, admission_kind, host_id=host_id if kind == "host.launch_runner" else None)
                    if isinstance(admission, Rejected):
                        return False
                    work[correlation] = admission
                    if kind == "host.launch_runner":
                        launch_runners[correlation] = _runner_for_token(str(frame["binding_token"]))
                return True
            except (ValueError, TypeError, UnicodeError):
                return False

        async def guarded_receive() -> Message:
            nonlocal inbound_admission, pending_input
            if inbound_admission:
                self.store.finish(inbound_admission)
                inbound_admission = None
            if pending_input is None:

                async def read_input() -> Message:
                    return await receive()

                pending_input = asyncio.create_task(read_input())
            pending = pending_input
            rejected = asyncio.create_task(rejected_launches.get())
            try:
                while not pending.done() and not rejected.done():
                    if retired() or closed:
                        await close()
                        return {"type": "websocket.disconnect", "code": 1008}
                    await asyncio.wait({pending, rejected}, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                if rejected.done():
                    return rejected.result()
                message = pending.result()
                pending_input = None
                frame: object = None
                if retired() or not inspect(message, False):
                    await close()
                    return {"type": "websocket.disconnect", "code": 1008}
                if message["type"] == "websocket.receive" and is_tunnel:
                    frame = _strict_json(message["text"])
                    if isinstance(frame, dict):
                        table, key = {"response.end": (requests, "id"), "ws.close": (channels, "ch_id"), "host.launch_runner_result": (launches, "request_id")}.get(str(frame.get("kind")), ({}, ""))
                        completed = table.pop(str(frame.get(key, "")), None)
                        if completed:
                            self.store.finish(completed)
                            if table is launches:
                                launch_runners.pop(str(frame.get(key, "")), None)
                if message["type"] == "websocket.receive":
                    heartbeat = is_tunnel and isinstance(frame, dict) and frame.get("kind") in ("hello", "ping", "pong")
                    incoming = self.store.admit(inspected_sessions, "observe" if heartbeat else "websocket:frame", public=not is_tunnel)
                    if isinstance(incoming, Rejected):
                        await close()
                        return {"type": "websocket.disconnect", "code": 1008}
                    inbound_admission = incoming
                return message
            finally:
                await _cancel_receive_task(rejected)

        async def guarded_send(message: Message) -> None:
            launch_request_id: str | None = None
            if closed:
                return
            if message["type"] == "websocket.close":
                await send(message)
                return
            if is_host and message["type"] == "websocket.send":
                try:
                    frame = _strict_json(message.get("text", ""))
                    if isinstance(frame, dict) and frame.get("kind") == "host.launch_runner":
                        from omnigent.host.frames import HostLaunchRunnerResultFrame, decode_host_frame, encode_host_frame

                        decode_host_frame(message["text"])
                        launch_request_id = str(frame["request_id"])
                        session_id = frame.get("session_id")
                        bound_session = self.store.session_for_runner(_runner_for_token(str(frame.get("binding_token", ""))))
                        blocked = any(self.store.is_fenced(value) for value in (session_id, bound_session) if isinstance(value, str))
                        if not blocked and host_match:
                            blocked = self.store.host_launch_conflict(host_id or "", str(session_id or "")) is not None
                        if blocked:
                            result = HostLaunchRunnerResultFrame(str(frame["request_id"]), "failed", error="session or host fenced", error_code="session_fenced")
                            rejected_launches.put_nowait({"type": "websocket.receive", "text": encode_host_frame(result)})
                            return
                except (ValueError, TypeError, UnicodeError):
                    await close()
                    return
            if retired() or not inspect(message, True):
                if launch_request_id is not None:
                    from omnigent.host.frames import HostLaunchRunnerResultFrame, encode_host_frame

                    result = HostLaunchRunnerResultFrame(launch_request_id, "failed", error="launch admission rejected", error_code="session_fenced")
                    rejected_launches.put_nowait({"type": "websocket.receive", "text": encode_host_frame(result)})
                    return
                await close()
                return
            outgoing = None
            if message["type"] == "websocket.send":
                frame = _strict_json(message["text"]) if is_tunnel else None
                heartbeat = isinstance(frame, dict) and frame.get("kind") in ("hello", "ping", "pong")
                outgoing = self.store.admit(inspected_sessions, "observe" if heartbeat else "websocket:send", public=not is_tunnel)
            if isinstance(outgoing, Rejected):
                await close()
                return
            try:
                await send(message)
            finally:
                if outgoing:
                    self.store.finish(outgoing)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        finally:
            self.store.finish(connection_admission)
            if inbound_admission:
                self.store.finish(inbound_admission)
            if pending_input:
                await _cancel_receive_task(pending_input)
