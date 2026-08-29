#!/usr/bin/env python3
"""Snapshot/export manager mail and trash explicit superseded UIDs."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import imaplib
import json
import os
import pwd
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import zipfile
import zipimport
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import Callable, TypeVar

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.email_idle_watcher import LEGACY_MANAGER_SUBJECT_TOKENS, is_mail_cleanup_excluded_subject, message_text, parse_env_config
from omo_manager.omo_email_config import configured_agent_mail, human_config_path, parse_env_file
from omo_manager.omo_email_subject import TMUX_TARGET_RE, canonical_tmux_target, subject_tmux_target

HEADER_FETCH = "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID X-OMO-AGENT-SESSION-ID)])"
HEADER_BATCH_FETCH = "(UID BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID X-OMO-AGENT-SESSION-ID)])"
FULL_FETCH = "(BODY.PEEK[])"
FULL_BATCH_FETCH = "(UID BODY.PEEK[])"
FINAL_GATE_FETCH = "(UID FLAGS X-GM-MSGID X-GM-THRID X-GM-LABELS BODY.PEEK[])"
GMAIL_METADATA_FETCH = "(FLAGS X-GM-MSGID X-GM-THRID X-GM-LABELS)"
GMAIL_METADATA_BATCH_FETCH = "(UID FLAGS X-GM-MSGID X-GM-THRID X-GM-LABELS)"
SUPERSESSION_HEADER_FETCH = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID X-OMO-SUPERSEDES X-OMO-AGENT-SESSION-ID)])"
TRASH_MAILBOX = "[Gmail]/Trash"
ACCOUNT_HOME = Path(pwd.getpwuid(os.geteuid()).pw_dir)
CONFIG_PATH = human_config_path()
DEFAULT_THREADS_PER_BATCH = 10
DEFAULT_WORK_LOGS_ROOT = ACCOUNT_HOME / "work_logs"
TRUSTED_LOCAL_ENV_PATH = ACCOUNT_HOME / ".config/omo_manager/local.env"
LOCAL_ENV_PATH = TRUSTED_LOCAL_ENV_PATH
SOURCE_815_APPROVAL_FILE = "85c5dff58359-815.txt"
SOURCE_815_APPROVAL_QUOTE = "This is not an email I did not read. Anyway, just remove it"
SOURCE_815_APPROVAL_QUOTE_SHA256 = hashlib.sha256(SOURCE_815_APPROVAL_QUOTE.encode("utf-8")).hexdigest()
SOURCE_815_APPROVAL_SHA256 = "0e6c0fad72ea98b04749c6cd0294ba3a168411c757d049be5c78a29307867bf2"
SOURCE_815_SOURCE_BINDING = "17338:1874072255391401971:1873525670359176908:11ad6ed3f1b029318d15b5b45f78b270aa8e774203687571e1ad2e615faea755"
SOURCE_815_TASK_ID = "manager-rewrite-owner-transfer"
SOURCE_815_EXACT_REMOVAL_EXCEPTION = "source-815-human-approved-exact-removal"
SOURCE_1140_APPROVAL_FILE = "85c5dff58359-1140.txt"
SOURCE_1140_APPROVAL_QUOTE = "Perhaps all of them."
SOURCE_1140_APPROVAL_SHA256 = "a80ed239e1acbd07750c2f55202ec2d5a68e6bd53068ae9c1eed36925fc7e436"
GMAIL_IDENTITY_UID_BATCH = 40
GMAIL_THREAD_OR_BATCH = 32
EXPORT_FULL_FETCH_ATTEMPTS = 2
IMAP_OPERATION_TIMEOUT_S = 45.0
TRASH_EXPLICIT_PRE_MOVE_TIMEOUT_S = 300.0
T = TypeVar("T")
RUNTIME_CLOSURE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CLOSURE_ENTRYPOINT = Path(__file__).resolve()


def final_gate_event(_name: str) -> None:
    """Deterministic test seam after each ordered final-gate observation."""


def local_python_import_closure_entries(
    entrypoint: Path = RUNTIME_CLOSURE_ENTRYPOINT,
    root: Path = RUNTIME_CLOSURE_ROOT,
) -> tuple[tuple[Path, bytes], ...]:
    """Capture the complete statically reachable local Python import closure."""
    root = root.resolve()
    pending = [entrypoint.resolve()]
    found: dict[Path, bytes] = {}
    while pending:
        path = pending.pop()
        if path in found:
            continue
        try:
            relative = path.relative_to(root)
            content = path.read_bytes()
            tree = ast.parse(content, filename=str(path))
        except (OSError, SyntaxError, ValueError) as exc:
            raise RuntimeError(f"cannot resolve local Python runtime closure: {path}") from exc
        if path.suffix != ".py":
            raise RuntimeError(f"local Python runtime closure contains a non-Python file: {relative}")
        found[path] = content
        current_module = ".".join(relative.with_suffix("").parts)
        current_package = current_module.split(".")[:-1]
        candidates: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                candidates.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    keep = len(current_package) - node.level + 1
                    base = current_package[: max(keep, 0)]
                    if node.module:
                        base.extend(node.module.split("."))
                    candidates.add(".".join(base))
                    candidates.update(".".join([*base, alias.name]) for alias in node.names if alias.name != "*")
                elif node.module:
                    candidates.add(node.module)
                    candidates.update(f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*")
        for module in candidates:
            module_parts = module.split(".")
            possible = root.joinpath(*module_parts).with_suffix(".py")
            if not possible.is_file() and len(module_parts) == 1:
                possible = root / "omo_manager" / f"{module}.py"
            if possible.is_file():
                resolved = possible.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise RuntimeError(f"local Python runtime closure escapes its root: {possible}") from exc
                pending.append(resolved)
    return tuple(sorted(found.items(), key=lambda item: item[0].relative_to(root).as_posix()))


def local_python_import_closure(entrypoint: Path = RUNTIME_CLOSURE_ENTRYPOINT, root: Path = RUNTIME_CLOSURE_ROOT) -> tuple[Path, ...]:
    """Return paths in the complete statically reachable local Python import closure."""
    return tuple(path for path, _content in local_python_import_closure_entries(entrypoint, root))


def runtime_closure_entries() -> tuple[tuple[str, str], ...]:
    """Return canonical relative-path and content-digest runtime entries."""
    return tuple(
        (path.relative_to(RUNTIME_CLOSURE_ROOT).as_posix(), hashlib.sha256(content).hexdigest())
        for path, content in local_python_import_closure_entries()
    )


def runtime_closure_sha256() -> str:
    """Digest the complete canonical local Python runtime closure."""
    digest = hashlib.sha256()
    for relative, content_sha256 in runtime_closure_entries():
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_runtime_bundle(out_path: Path) -> str:
    """Create one immutable zipapp containing the complete local Python closure."""
    if not out_path.is_absolute():
        raise ValueError("runtime bundle path must be absolute")
    out_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_stat = out_path.parent.stat()
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
        raise ValueError("runtime bundle parent must be owner-only")
    closure_entries = local_python_import_closure_entries()
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w+b", closefd=False) as bundle_file:
            bundle_file.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(bundle_file, mode="a", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
                entries = [("omo_manager/__init__.py", b"")]
                entries.extend(
                    (path.relative_to(RUNTIME_CLOSURE_ROOT).as_posix(), content)
                    for path, content in closure_entries
                )
                entries.append(
                    (
                        "__main__.py",
                        b"from omo_manager.omo_manager_mail_compress import main\n"
                        b"import sys\n"
                        b"raise SystemExit(main(sys.argv[1:]))\n",
                    )
                )
                for relative, content in sorted(entries):
                    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100400 << 16
                    bundle.writestr(info, content)
            bundle_file.flush()
            os.fsync(bundle_file.fileno())
    except BaseException:
        try:
            out_path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    out_path.chmod(0o500)
    fsync_dir(out_path.parent)
    return hashlib.sha256(out_path.read_bytes()).hexdigest()


def validate_runtime_bundle(expected_sha256: str) -> None:
    """Require execution from the exact reviewed immutable runtime zipapp."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("runtime bundle SHA-256 must be lowercase hexadecimal")
    loader = __loader__
    if not isinstance(loader, zipimport.zipimporter):
        raise ValueError("trash-explicit must execute from a reviewed .pyz runtime bundle")
    runtime_path = Path(loader.archive).resolve()
    if runtime_path.suffix != ".pyz" or not zipfile.is_zipfile(runtime_path):
        raise ValueError("trash-explicit must execute from a reviewed .pyz runtime bundle")
    if hashlib.sha256(runtime_path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("runtime bundle SHA-256 changed")


def cmd_build_runtime_bundle(args: argparse.Namespace) -> int:
    digest = build_runtime_bundle(args.out)
    print(f"runtime_bundle={args.out} runtime_bundle_sha256={digest} local_python_files={len(local_python_import_closure_entries())}")
    return 0


class ImapOperationError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        self.stage = stage
        super().__init__(message)


@dataclass(frozen=True)
class MailRecord:
    uid: str
    date: str
    sender: str
    to: str
    subject: str
    msgid_sha256: str
    body: str = ""
    gmail_msgid: str = ""
    gmail_thrid: str = ""
    flags: str = ""
    labels: str = ""
    raw_sha256: str = ""
    source_uidvalidity: str = ""
    message_id: str = ""
    agent_session_id: str = ""

    @property
    def body_bytes(self) -> int:
        return len(self.body.encode())


@dataclass(frozen=True)
class GmailMetadata:
    gmail_msgid: str
    gmail_thrid: str
    flags: str
    labels: str


@dataclass(frozen=True)
class PostMoveVerification:
    same_mailbox: bool = False
    verified_message_count: int = 0
    verified_thread_count: int = 0
    changed_thread_count: int = 0
    imap_failure_count: int = 0
    verified_records: tuple[MailRecord, ...] = ()

    @property
    def complete(self) -> bool:
        return self.same_mailbox and not (self.changed_thread_count or self.imap_failure_count)


@dataclass(frozen=True)
class ScopedSource:
    task_id: str
    uid: str
    gmail_msgid: str
    gmail_thrid: str
    raw_sha256: str


@dataclass(frozen=True)
class RetainedReplacement:
    uid: str
    gmail_msgid: str
    gmail_thrid: str
    raw_sha256: str
    body_bytes: int
    body_sha256: str


@dataclass
class FinalGateState:
    """Ordered, explicitly non-atomic observations made before one MOVE."""

    observations: list[str] = field(default_factory=list)

    def observed(self, name: str) -> None:
        self.observations.append(name)
        final_gate_event(name)

    @property
    def receipt(self) -> str:
        return summary_token(">".join(self.observations)) if self.observations else "none"


@dataclass(frozen=True)
class ReviewedScope:
    sources: tuple[ScopedSource, ...]
    preparer: str
    reviewer: str
    provenance: str
    sha256: str


@dataclass(frozen=True)
class ExactRemovalEvidence:
    exception: str
    approval_sha256: str
    approval_quote_sha256: str
    approval_source_binding: str


def parse_explicit_source(value: str, *, with_task: bool = False) -> ScopedSource:
    """Parse one in-memory source binding without creating evidence files."""
    fields = value.split(":")
    expected_fields = 5 if with_task else 4
    if len(fields) != expected_fields:
        prefix = "TASK-ID:" if with_task else ""
        raise ValueError(f"explicit source must be {prefix}UID:GMAIL-MSGID:GMAIL-THRID:RAW-SHA256")
    task_id = fields.pop(0) if with_task else ""
    uid, gmail_msgid, gmail_thrid, raw_sha256 = fields
    if with_task and (not task_id or tsv_value(task_id) != task_id):
        raise ValueError("explicit source task identity must be one nonempty line")
    if not uid.isdecimal() or not gmail_msgid.isdecimal() or not gmail_thrid.isdecimal():
        raise ValueError("explicit source UID and Gmail identities must be decimal")
    if not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
        raise ValueError("explicit source raw SHA-256 must be lowercase hexadecimal")
    return ScopedSource(task_id, uid, gmail_msgid, gmail_thrid, raw_sha256)


def parse_explicit_context(value: str) -> ScopedSource:
    """Parse one thread member binding whose mailbox-specific UID is irrelevant."""
    fields = value.split(":")
    if len(fields) != 3:
        raise ValueError("explicit context must be GMAIL-MSGID:GMAIL-THRID:RAW-SHA256")
    gmail_msgid, gmail_thrid, raw_sha256 = fields
    if not gmail_msgid.isdecimal() or not gmail_thrid.isdecimal():
        raise ValueError("explicit context Gmail identities must be decimal")
    if not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
        raise ValueError("explicit context raw SHA-256 must be lowercase hexadecimal")
    return ScopedSource("", "", gmail_msgid, gmail_thrid, raw_sha256)


def parse_retained_replacement(value: str) -> RetainedReplacement:
    """Parse one reviewed retained replacement binding."""
    fields = value.split(":")
    if len(fields) != 6:
        raise ValueError("retained replacement must be UID:GMAIL-MSGID:GMAIL-THRID:RAW-SHA256:BODY-BYTES:BODY-SHA256")
    uid, gmail_msgid, gmail_thrid, raw_sha256, body_bytes, body_sha256 = fields
    if not uid.isdecimal() or not gmail_msgid.isdecimal() or not gmail_thrid.isdecimal() or not body_bytes.isdecimal():
        raise ValueError("retained replacement UID, Gmail identities, and body size must be decimal")
    if int(body_bytes) < 1:
        raise ValueError("retained replacement body size must be positive")
    if not re.fullmatch(r"[0-9a-f]{64}", raw_sha256) or not re.fullmatch(r"[0-9a-f]{64}", body_sha256):
        raise ValueError("retained replacement digests must be lowercase SHA-256")
    return RetainedReplacement(uid, gmail_msgid, gmail_thrid, raw_sha256, int(body_bytes), body_sha256)


def load_reviewed_scope(path: Path) -> ReviewedScope:
    """Load a private, independently reviewed exact-identity successor scope."""
    try:
        stat = path.lstat()
    except OSError as exc:
        raise RuntimeError("could not inspect scope file") from exc
    if not path.is_file() or path.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise RuntimeError("scope file must be a regular owner-only file owned by the current user")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read()
    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8")), delimiter="\t"))
    except UnicodeDecodeError as exc:
        raise RuntimeError("scope file must be UTF-8 TSV") from exc
    fields = ("version", "task_id", "uid", "gmail_msgid", "gmail_thrid", "raw_sha256", "preparer", "reviewer", "provenance")
    if not rows or tuple(rows[0]) != fields:
        raise RuntimeError(f"scope file must have exactly these columns: {','.join(fields)}")
    metadata = {(row["version"], row["preparer"], row["reviewer"], row["provenance"]) for row in rows}
    if len(metadata) != 1:
        raise RuntimeError("scope review metadata must be identical on every row")
    version, preparer, reviewer, provenance = metadata.pop()
    if version != "v1.0.0" or not preparer.strip() or not reviewer.strip() or not provenance.strip():
        raise RuntimeError("scope requires v1.0.0 and nonempty preparer, reviewer, and provenance")
    if preparer.strip() == reviewer.strip():
        raise RuntimeError("scope reviewer must be distinct from its preparer")
    sources: list[ScopedSource] = []
    for row in rows:
        source = ScopedSource(*(row[name].strip() for name in fields[1:6]))
        if not source.task_id or not source.uid.isdecimal() or not source.gmail_msgid.isdecimal() or not source.gmail_thrid.isdecimal():
            raise RuntimeError("scope has an invalid task or source identity")
        if not re.fullmatch(r"[0-9a-f]{64}", source.raw_sha256):
            raise RuntimeError("scope raw_sha256 must be a lowercase SHA-256 digest")
        sources.append(source)
    identities = [(source.uid, source.gmail_msgid) for source in sources]
    if len(identities) != len(set(identities)) or len({source.uid for source in sources}) != len(sources):
        raise RuntimeError("scope contains a duplicate or ambiguous source identity")
    tasks_by_thread: dict[str, set[str]] = {}
    for source in sources:
        tasks_by_thread.setdefault(source.gmail_thrid, set()).add(source.task_id)
    if any(len(tasks) != 1 for tasks in tasks_by_thread.values()):
        raise RuntimeError("scope assigns one mail thread to multiple tasks")
    return ReviewedScope(tuple(sources), preparer.strip(), reviewer.strip(), provenance.strip(), hashlib.sha256(raw).hexdigest())


def validate_scoped_records(scope: ReviewedScope, records: list[MailRecord]) -> None:
    expected = {source.uid: source for source in scope.sources}
    if len(records) != len(expected) or {record.uid for record in records} != set(expected):
        raise RuntimeError("current fixed-start messages do not exactly match reviewed scope")
    for record in records:
        source = expected[record.uid]
        if (record.gmail_msgid, record.gmail_thrid, record.raw_sha256) != (source.gmail_msgid, source.gmail_thrid, source.raw_sha256):
            raise RuntimeError(f"current message identity does not match reviewed scope: uid={record.uid}")


def imap_operation(client: imaplib.IMAP4_SSL, stage: str, operation: Callable[[], T]) -> T:
    """Run one IMAP operation with an absolute deadline and abort its socket on expiry."""
    if getattr(client, "_omo_operation_timed_out", False):
        raise ImapOperationError(stage, f"IMAP client is unusable after timeout: stage={stage}")
    results: list[T] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(operation())
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run, name=f"imap:{stage}", daemon=True)
    worker.start()
    worker.join(IMAP_OPERATION_TIMEOUT_S)
    if worker.is_alive():
        abort_imap_client(client)
        raise ImapOperationError(stage, f"IMAP operation timed out: stage={stage} timeout_s={IMAP_OPERATION_TIMEOUT_S:g}")
    if failures:
        exc = failures[0]
        if isinstance(exc, (EOFError, OSError, imaplib.IMAP4.error)):
            raise ImapOperationError(stage, f"IMAP operation failed: stage={stage} error={exc}") from exc
        raise exc
    return results[0]


def imap_uid(client: imaplib.IMAP4_SSL, stage: str, *args: str | None) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
    return imap_operation(client, stage, lambda: client.uid(*args))  # pyright: ignore[reportArgumentType, reportReturnType]


def logout_mailbox(client: imaplib.IMAP4_SSL) -> None:
    if getattr(client, "_omo_operation_timed_out", False):
        return
    imap_operation(client, "logout", client.logout)


def connect_mailbox(host: str, connected: Callable[[imaplib.IMAP4_SSL], None] | None = None) -> imaplib.IMAP4_SSL:
    """Connect with an absolute deadline and close any client created after expiry."""
    clients: list[imaplib.IMAP4_SSL] = []
    failures: list[BaseException] = []
    completed = False
    expired = False
    state_changed = threading.Condition()

    def run() -> None:
        nonlocal completed, expired
        try:
            client = imaplib.IMAP4_SSL(host, timeout=IMAP_OPERATION_TIMEOUT_S)
        except BaseException as exc:
            with state_changed:
                failures.append(exc)
                completed = True
                state_changed.notify()
            return
        with state_changed:
            if not expired:
                if connected is not None:
                    connected(client)
                clients.append(client)
                completed = True
                state_changed.notify()
                return
        try:
            client.shutdown()
        except (AttributeError, OSError, imaplib.IMAP4.error):
            pass

    worker = threading.Thread(target=run, name="imap:connect", daemon=True)
    try:
        worker.start()
        with state_changed:
            if not state_changed.wait_for(lambda: completed, timeout=IMAP_OPERATION_TIMEOUT_S):
                expired = True
                raise ImapOperationError("connect", f"IMAP operation timed out: stage=connect timeout_s={IMAP_OPERATION_TIMEOUT_S:g}")
        if failures:
            exc = failures[0]
            if isinstance(exc, (EOFError, OSError, imaplib.IMAP4.error)):
                raise ImapOperationError("connect", f"IMAP operation failed: stage=connect error={exc}") from exc
            raise exc
        return clients[0]
    except BaseException:
        with state_changed:
            expired = True
            accepted_clients = list(clients)
            clients.clear()
        for client in accepted_clients:
            try:
                client.shutdown()
            except (AttributeError, OSError, imaplib.IMAP4.error):
                pass
        raise


def parse_uid_text(text: str) -> list[str]:
    values = [value for value in re.split(r"[\s,]+", text.strip()) if value]
    seen: set[str] = set()
    uids: list[str] = []
    for value in values:
        if not value.isdecimal():
            raise ValueError(f"UID must be decimal: {value}")
        if value not in seen:
            seen.add(value)
            uids.append(value)
    return uids


def parse_uids(raw: str, uid_file: Path | None) -> list[str]:
    parts = [raw]
    if uid_file is not None:
        parts.append(uid_file.read_text(encoding="utf-8"))
    return parse_uid_text("\n".join(parts))


def msgid_digest(msg: Message) -> str:
    msgid = rfc_message_id(msg)
    return hashlib.sha256(msgid.encode()).hexdigest()[:12] if msgid else "no-msgid"


def rfc_message_id(msg: Message) -> str:
    values = msg.get_all("Message-ID", [])
    if len(values) != 1:
        return ""
    value = " ".join(str(values[0]).split())
    return value if re.fullmatch(r"<[^<>\s]+>", value) else ""


def record_from_msg(
    uid: str,
    msg: Message,
    body: str = "",
    gmail_msgid: str = "",
    gmail_thrid: str = "",
    flags: str = "",
    labels: str = "",
    raw_sha256: str = "",
) -> MailRecord:
    return MailRecord(
        uid=uid,
        date=str(msg.get("Date", "")).replace("\n", " "),
        sender=", ".join(str(value).replace("\n", " ") for value in msg.get_all("From", [])),
        to=", ".join(str(value).replace("\n", " ") for value in msg.get_all("To", [])),
        subject=str(msg.get("Subject", "")).replace("\n", " "),
        msgid_sha256=msgid_digest(msg),
        body=body.replace("\r\n", "\n"),
        gmail_msgid=gmail_msgid,
        gmail_thrid=gmail_thrid,
        flags=flags,
        labels=labels,
        raw_sha256=raw_sha256,
        message_id=rfc_message_id(msg),
        agent_session_id=" ".join(str(msg.get("X-OMO-Agent-Session-ID", "")).split()).lower(),
    )


def is_manager_record(record: MailRecord, sender_email: str, recipient_email: str) -> bool:
    if is_mail_cleanup_excluded_subject(record.subject):
        return False
    senders = [address.casefold() for _name, address in getaddresses([record.sender]) if address]
    sender_matches = senders == [sender_email.casefold()]
    recipients = [address.casefold() for _name, address in getaddresses([record.to]) if address]
    recipient_matches = recipients == [recipient_email.casefold()]
    if sender_email.casefold() != recipient_email.casefold():
        return sender_matches and recipient_matches
    subject_matches = any(token.lower() in record.subject.lower() for token in LEGACY_MANAGER_SUBJECT_TOKENS)
    return sender_matches and recipient_matches and subject_matches


def imap_quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', r"\"") + '"'


def load_config() -> dict[str, str]:
    config = parse_env_config(CONFIG_PATH)
    missing = {"host", "user", "password"} - set(config)
    if missing:
        raise RuntimeError(f"missing email config keys in {CONFIG_PATH}: {sorted(missing)}")
    return config


def manager_candidate_uids(client: imaplib.IMAP4_SSL, self_email: str) -> list[str]:
    """Return current INBOX UIDs from the configured agent sender.

    Live Gmail times out a mailbox-wide `SEARCH ALL` freeze, so the freeze set
    is the sender search. Split-account mode uses that one search. Legacy
    self-addressed mode intersects subject-token searches with it.
    """
    found: list[str] = []
    seen: set[str] = set()
    settings = configured_agent_mail()
    criteria = ["FROM", f'"{self_email}"']
    typ, data = imap_uid(client, "manager-sender-search", "search", None, *criteria)
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {typ}")
    sender_uids = [raw.decode() for raw in data[0].split()] if data and data[0] else []
    if settings is not None:
        return list(dict.fromkeys(sender_uids))
    frozen = set(sender_uids)
    for token in LEGACY_MANAGER_SUBJECT_TOKENS:
        typ, data = imap_uid(client, "manager-candidate-search", "search", None, *(criteria + ["SUBJECT", f'"{token}"']))
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        for uid in [raw.decode() for raw in data[0].split()] if data and data[0] else []:
            if uid in frozen and uid not in seen:
                seen.add(uid)
                found.append(uid)
    return found


def manager_unread_candidate_uids(client: imaplib.IMAP4_SSL, self_email: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    settings = configured_agent_mail()
    criteria = ["UNSEEN", "FROM", f'"{self_email}"']
    subject_tokens = ("",) if settings is not None else LEGACY_MANAGER_SUBJECT_TOKENS
    for token in subject_tokens:
        typ, data = imap_uid(client, "unread-manager-candidate-search", "search", None, *(criteria + (["SUBJECT", f'"{token}"'] if token else [])))
        if typ != "OK":
            raise RuntimeError(f"IMAP unread search failed: {typ}")
        for uid in [raw.decode() for raw in data[0].split()] if data and data[0] else []:
            if uid not in seen:
                seen.add(uid)
                found.append(uid)
    return found


def inbox_subset(client: imaplib.IMAP4_SSL, uids: list[str]) -> list[str]:
    if not uids:
        return []
    typ, data = imap_uid(client, "inbox-subset-search", "search", None, "UID", ",".join(uids))
    if typ != "OK":
        raise RuntimeError(f"IMAP UID search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def mailbox_exists(client: imaplib.IMAP4_SSL, mailbox: str) -> bool:
    typ, data = imap_operation(client, "mailbox-list", client.list)
    if typ != "OK":
        raise RuntimeError(f"IMAP mailbox list failed: {typ}")
    return any(mailbox.encode() in raw for raw in data if isinstance(raw, bytes))


def fetch_msg_bytes(client: imaplib.IMAP4_SSL, uid: str, fetch_expr: str, n_attempts: int = 1) -> bytes:
    if n_attempts < 1:
        raise ValueError("IMAP fetch attempts must be positive")
    for _attempt in range(n_attempts):
        typ, data = imap_uid(client, f"message-fetch uid={uid}", "fetch", uid, fetch_expr)
        if typ != "OK":
            raise RuntimeError(f"IMAP fetch failed: uid={uid} typ={typ}")
        if not data or not isinstance(data[0], tuple):
            continue
        payload = data[0][1]
        if isinstance(payload, bytes):
            return payload
    raise RuntimeError(f"IMAP fetch returned no usable record: uid={uid}")


def fetch_msg(client: imaplib.IMAP4_SSL, uid: str, fetch_expr: str, n_attempts: int = 1) -> tuple[Message, str]:
    payload = fetch_msg_bytes(client, uid, fetch_expr, n_attempts)
    return BytesParser(policy=policy.default).parsebytes(payload), hashlib.sha256(payload).hexdigest()


def imap_response_text(data: list[object]) -> str:
    chunks: list[str] = []
    for item in data:
        if isinstance(item, bytes):
            chunks.append(item.decode("utf-8", errors="replace"))
        elif isinstance(item, str):
            chunks.append(item)
        elif isinstance(item, tuple):
            for part in item:
                if isinstance(part, bytes):
                    chunks.append(part.decode("utf-8", errors="replace"))
                elif isinstance(part, str):
                    chunks.append(part)
    return " ".join(chunks)


def imap_fetch_attributes(text: str, *, reject_duplicate_keys: frozenset[str] | None = None) -> dict[str, str]:
    """Return top-level FETCH attributes without treating nested labels as fields."""
    outer = text.find("(")
    if outer < 0:
        return {}
    attributes: dict[str, str] = {}
    index = outer + 1
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] == ")":
            return attributes
        key_start = index
        while index < len(text) and not text[index].isspace() and text[index] not in "()":
            index += 1
        key = text[key_start:index]
        while index < len(text) and text[index].isspace():
            index += 1
        if not key or index >= len(text):
            return attributes
        value_start = index
        if text[index] == "(":
            depth = 0
            quoted = False
            escaped = False
            while index < len(text):
                char = text[index]
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                elif char == '"':
                    quoted = True
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        index += 1
                        break
                index += 1
            if depth != 0:
                return attributes
        elif text[index] == '"':
            index += 1
            escaped = False
            while index < len(text):
                char = text[index]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    index += 1
                    break
                index += 1
        else:
            while index < len(text) and not text[index].isspace() and text[index] != ")":
                index += 1
        normalized_key = key.upper()
        if normalized_key in attributes and reject_duplicate_keys is not None and normalized_key in reject_duplicate_keys:
            return {}
        attributes[normalized_key] = text[value_start:index]
    return attributes


def imap_list_value(value: str) -> str:
    return " ".join(value[1:-1].split()) if value.startswith("(") and value.endswith(")") else ""


def fetch_gmail_metadata_detail(client: imaplib.IMAP4_SSL, uid: str) -> GmailMetadata:
    typ, data = imap_uid(client, f"gmail-metadata-fetch uid={uid}", "fetch", uid, GMAIL_METADATA_FETCH)
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail metadata fetch failed: typ={typ}")
    attributes = imap_fetch_attributes(imap_response_text(data))
    gmail_msgid = attributes.get("X-GM-MSGID", "")
    gmail_thrid = attributes.get("X-GM-THRID", "")
    return GmailMetadata(
        gmail_msgid if gmail_msgid.isdecimal() else "",
        gmail_thrid if gmail_thrid.isdecimal() else "",
        imap_list_value(attributes.get("FLAGS", "")),
        imap_list_value(attributes.get("X-GM-LABELS", "")),
    )


def fetch_gmail_metadata(client: imaplib.IMAP4_SSL, uid: str) -> tuple[str, str, str, str]:
    metadata = fetch_gmail_metadata_detail(client, uid)
    return metadata.gmail_msgid, metadata.gmail_thrid, metadata.flags, metadata.labels


def fetch_gmail_metadata_records(client: imaplib.IMAP4_SSL, uids: list[str]) -> dict[str, GmailMetadata]:
    """Fetch Gmail identity metadata for a fixed UID set in one request."""
    if not uids:
        return {}
    typ, data = imap_uid(client, "gmail-metadata-batch-fetch", "fetch", ",".join(uids), GMAIL_METADATA_BATCH_FETCH)
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail metadata batch fetch failed: typ={typ}")
    metadata_by_uid: dict[str, GmailMetadata] = {}
    for item in data:
        if isinstance(item, bytes):
            response = item
        elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], bytes):
            response = item[0]
        else:
            continue
        attributes = imap_fetch_attributes(response.decode("utf-8", errors="replace"))
        uid = attributes.get("UID", "")
        message_id = attributes.get("X-GM-MSGID", "")
        thread_id = attributes.get("X-GM-THRID", "")
        if not uid.isdecimal() or not message_id.isdecimal() or not thread_id.isdecimal():
            continue
        if uid in metadata_by_uid:
            raise RuntimeError("IMAP Gmail metadata batch fetch returned duplicate UID")
        metadata_by_uid[uid] = GmailMetadata(
            message_id,
            thread_id,
            imap_list_value(attributes.get("FLAGS", "")),
            imap_list_value(attributes.get("X-GM-LABELS", "")),
        )
    if set(metadata_by_uid) != set(uids):
        raise RuntimeError("IMAP Gmail metadata batch fetch returned incomplete or duplicate UIDs")
    return metadata_by_uid


def fetch_gmail_metadata_records_compatible(client: imaplib.IMAP4_SSL, uids: list[str]) -> dict[str, GmailMetadata]:
    """Fetch Gmail metadata in batch, with a per-UID fallback for incomplete IMAP replies."""
    try:
        return fetch_gmail_metadata_records(client, uids)
    except RuntimeError as exc:
        if str(exc) != "IMAP Gmail metadata batch fetch returned incomplete or duplicate UIDs":
            raise
        return {uid: fetch_gmail_metadata_detail(client, uid) for uid in uids}


def gmail_extension_advertised(client: imaplib.IMAP4_SSL) -> bool:
    typ, data = imap_operation(client, "capability", client.capability)
    if typ != "OK":
        raise RuntimeError(f"IMAP capability query failed: {typ}")
    return "X-GM-EXT-1" in imap_response_text(data).upper().split()


def fetch_record(client: imaplib.IMAP4_SSL, uid: str, with_body: bool, with_metadata: bool, n_fetch_attempts: int = 1) -> MailRecord:
    msg, raw_sha256 = fetch_msg(client, uid, FULL_FETCH if with_body else HEADER_FETCH, n_fetch_attempts)
    gmail_msgid, gmail_thrid, flags, labels = fetch_gmail_metadata(client, uid) if with_metadata else ("", "", "", "")
    return record_from_msg(
        uid,
        msg,
        message_text(msg) if with_body else "",
        gmail_msgid,
        gmail_thrid,
        flags,
        labels,
        raw_sha256,
    )


def fetch_records(client: imaplib.IMAP4_SSL, uids: list[str], with_body: bool, with_metadata: bool = False, n_fetch_attempts: int = 1) -> list[MailRecord]:
    return [fetch_record(client, uid, with_body, with_metadata, n_fetch_attempts) for uid in uids]


def fetch_header_records(client: imaplib.IMAP4_SSL, uids: list[str]) -> list[MailRecord]:
    """Fetch a fixed header set in one read-only IMAP request.

    Snapshot and identity preflight used to issue one network round-trip per
    manager candidate before printing anything.  A large mailbox consequently
    looked hung even though every individual request was healthy.  Requesting
    UID in the response lets this batched fetch retain the exact caller order.
    """
    if not uids:
        return []
    typ, data = imap_uid(client, "manager-header-batch-fetch", "fetch", ",".join(uids), HEADER_BATCH_FETCH)
    if typ != "OK":
        raise RuntimeError(f"IMAP manager header batch fetch failed: typ={typ}")
    records_by_uid: dict[str, MailRecord] = {}
    for item in data:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        metadata, payload = item
        if not isinstance(metadata, bytes) or not isinstance(payload, bytes):
            continue
        match = re.search(rb"\bUID (\d+)\b", metadata)
        if match is None:
            continue
        uid = match.group(1).decode()
        msg = BytesParser(policy=policy.default).parsebytes(payload)
        if uid in records_by_uid:
            raise RuntimeError("IMAP manager header batch fetch returned duplicate UID")
        records_by_uid[uid] = record_from_msg(uid, msg, raw_sha256=hashlib.sha256(payload).hexdigest())
    if set(records_by_uid) != set(uids):
        raise RuntimeError("IMAP manager header batch fetch returned incomplete or duplicate UIDs")
    return [records_by_uid[uid] for uid in uids]


def fetch_full_records_batched(client: imaplib.IMAP4_SSL, uids: list[str]) -> list[MailRecord]:
    if not uids:
        return []
    typ, data = imap_uid(client, "full-message-batch-fetch", "fetch", ",".join(uids), FULL_BATCH_FETCH)
    if typ != "OK":
        raise RuntimeError(f"IMAP full message batch fetch failed: typ={typ}")
    records: dict[str, MailRecord] = {}
    for item in data:
        if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], bytes) or not isinstance(item[1], bytes):
            continue
        match = re.search(rb"\bUID (\d+)\b", item[0])
        if match is None:
            continue
        uid = match.group(1).decode()
        if uid in records:
            raise RuntimeError("IMAP full message batch fetch returned duplicate UID")
        msg = BytesParser(policy=policy.default).parsebytes(item[1])
        records[uid] = record_from_msg(uid, msg, message_text(msg), raw_sha256=hashlib.sha256(item[1]).hexdigest())
    if set(records) != set(uids):
        raise RuntimeError("IMAP full message batch fetch returned incomplete or duplicate UIDs")
    metadata = fetch_gmail_metadata_records(client, uids)
    return [replace(records[uid], gmail_msgid=metadata[uid].gmail_msgid, gmail_thrid=metadata[uid].gmail_thrid, flags=metadata[uid].flags, labels=metadata[uid].labels) for uid in uids]


def fetch_full_records(client: imaplib.IMAP4_SSL, uids: list[str], n_fetch_attempts: int = 1) -> list[MailRecord]:
    """Fetch full records in batches, with the bounded per-UID compatibility path."""
    try:
        return fetch_full_records_batched(client, uids)
    except RuntimeError as exc:
        if str(exc) not in {
            "IMAP full message batch fetch returned incomplete or duplicate UIDs",
            "IMAP Gmail metadata batch fetch returned incomplete or duplicate UIDs",
        }:
            raise
        return fetch_records(client, uids, with_body=True, with_metadata=True, n_fetch_attempts=n_fetch_attempts)


def fetch_final_gate_records(client: imaplib.IMAP4_SSL, uids: list[str]) -> list[MailRecord]:
    """Fetch exact full content and Gmail identity for all selected-INBOX UIDs once."""
    if not uids:
        return []
    typ, data = imap_uid(client, "final-gate-inbox-fetch", "fetch", ",".join(uids), FINAL_GATE_FETCH)
    if typ != "OK":
        raise RuntimeError(f"IMAP final-gate fetch failed: typ={typ}")
    records: dict[str, MailRecord] = {}
    index = 0
    while index < len(data):
        item = data[index]
        if not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], bytes) or not isinstance(item[1], bytes):
            raise RuntimeError("IMAP final-gate fetch returned an unassociated or malformed response")
        response_parts = [item[0]]
        index += 1
        while index < len(data):
            trailer = data[index]
            if not isinstance(trailer, bytes):
                break
            response_parts.append(trailer)
            index += 1
        attributes = imap_fetch_attributes(
            b" ".join(response_parts).decode("ascii", errors="strict"),
            reject_duplicate_keys=frozenset({"UID", "FLAGS", "X-GM-MSGID", "X-GM-THRID", "X-GM-LABELS"}),
        )
        uid = attributes.get("UID", "")
        gmail_msgid = attributes.get("X-GM-MSGID", "")
        gmail_thrid = attributes.get("X-GM-THRID", "")
        if not uid.isdecimal() or not gmail_msgid.isdecimal() or not gmail_thrid.isdecimal():
            raise RuntimeError("IMAP final-gate fetch returned incomplete or ambiguous identity metadata")
        if uid in records:
            raise RuntimeError("IMAP final-gate fetch returned duplicate UID")
        msg = BytesParser(policy=policy.default).parsebytes(item[1])
        records[uid] = record_from_msg(
            uid,
            msg,
            message_text(msg),
            gmail_msgid,
            gmail_thrid,
            imap_list_value(attributes.get("FLAGS", "")),
            imap_list_value(attributes.get("X-GM-LABELS", "")),
            hashlib.sha256(item[1]).hexdigest(),
        )
    if set(records) != set(uids):
        raise RuntimeError("IMAP final-gate fetch returned incomplete or duplicate UIDs")
    return [records[uid] for uid in uids]


def accepted_manager_headers(client: imaplib.IMAP4_SSL, uids: list[str], sender_email: str, recipient_email: str) -> tuple[list[MailRecord], list[str]]:
    try:
        records = fetch_header_records(client, uids)
    except RuntimeError as exc:
        if str(exc) != "IMAP manager header batch fetch returned incomplete or duplicate UIDs":
            raise
        # Keep the read-only helper compatible with IMAP servers that do not
        # include UID in a multi-message FETCH response.  Gmail includes it,
        # so normal manager-mail snapshots use the single round-trip above.
        records = fetch_records(client, uids, with_body=False)
    accepted = [record for record in records if is_manager_record(record, sender_email, recipient_email)]
    skipped = [record.uid for record in records if not is_manager_record(record, sender_email, recipient_email)]
    return accepted, skipped


def mail_boundary(config: dict[str, str]) -> tuple[str, str]:
    """Return the exact agent sender and human mailbox recipient boundary."""
    split_settings = configured_agent_mail()
    if split_settings is None:
        return config["user"], config["user"]
    if config["user"].casefold() != split_settings.human_address.casefold():
        raise RuntimeError("human cleanup mailbox does not match OMO_HUMAN_EMAIL_ADDRESS")
    return split_settings.agent_address, split_settings.human_address


def open_mailbox(readonly: bool, connected: Callable[[imaplib.IMAP4_SSL], None] | None = None) -> tuple[imaplib.IMAP4_SSL, dict[str, str]]:
    config = load_config()
    client = connect_mailbox(config["host"], connected)
    try:
        imap_operation(client, "login", lambda: client.login(config["user"], config["password"]))
        typ, _data = imap_operation(client, "select mailbox=INBOX", lambda: client.select("INBOX", readonly=readonly))
        if typ != "OK":
            raise RuntimeError(f"IMAP select INBOX failed: {typ}")
    except Exception as exc:
        if isinstance(exc, ImapOperationError):
            abort_imap_client(client)
        try:
            logout_mailbox(client)
        except RuntimeError:
            pass
        raise
    return client, config


def selected_uidvalidity(client: imaplib.IMAP4_SSL) -> str:
    _name, data = imap_operation(client, "selected-uidvalidity", lambda: client.response("UIDVALIDITY"))
    values = [value.decode() for value in data or [] if isinstance(value, bytes)]
    if len(values) != 1 or not values[0].isdecimal():
        raise RuntimeError("selected mailbox omitted UIDVALIDITY")
    return values[0]


def select_mailbox(client: imaplib.IMAP4_SSL, mailbox: str, readonly: bool) -> None:
    typ, _data = imap_operation(client, f"select mailbox={mailbox}", lambda: client.select(imap_quoted(mailbox), readonly=readonly))
    if typ != "OK":
        raise RuntimeError(f"IMAP select failed: mailbox={mailbox} typ={typ}")


def imap_mailbox_name(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace(r"\"", '"').replace(r"\\", "\\")
    return value


def special_use_mailboxes(client: imaplib.IMAP4_SSL) -> dict[str, str]:
    typ, data = imap_operation(client, "special-use-mailbox-list", client.list)
    if typ != "OK":
        raise RuntimeError(f"IMAP mailbox list failed: {typ}")
    mailboxes: dict[str, str] = {}
    for raw in data:
        if not isinstance(raw, bytes):
            continue
        try:
            line = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError("IMAP special-use mailbox name was not ASCII modified UTF-7") from exc
        match = re.fullmatch(r"\((?P<attributes>[^)]*)\)\s+(?:\"[^\"]*\"|NIL)\s+(?P<mailbox>.+)", line)
        if match is None:
            continue
        attributes = set(match.group("attributes").split())
        mailbox = imap_mailbox_name(match.group("mailbox"))
        for special_use in (r"\All", r"\Sent"):
            if special_use in attributes:
                mailboxes[special_use] = mailbox
    return mailboxes


def gmail_thread_uids(client: imaplib.IMAP4_SSL, gmail_thrid: str) -> list[str]:
    if not gmail_thrid.isdecimal():
        raise RuntimeError("Gmail thread identity was missing or malformed")
    typ, data = imap_uid(client, f"gmail-thread-search thread={gmail_thrid}", "search", None, "X-GM-THRID", gmail_thrid)
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail thread search failed: {typ}")
    uids = [raw.decode() for raw in data[0].split()] if data and data[0] else []
    if any(not uid.isdecimal() for uid in uids) or len(uids) != len(set(uids)):
        raise RuntimeError("IMAP Gmail thread search returned incomplete or duplicate UIDs")
    return uids


def gmail_thrid_or_query(thrids: list[str]) -> str:
    """Return one nested IMAP OR search-key of `X-GM-THRID` keys."""
    if not thrids or any(not thrid.isdecimal() for thrid in thrids):
        raise RuntimeError("Gmail thread identity was missing or malformed")
    query = f"X-GM-THRID {thrids[0]}"
    for thrid in thrids[1:]:
        query = f"(OR ({query}) X-GM-THRID {thrid})"
    return query


def gmail_thread_uids_union(client: imaplib.IMAP4_SSL, thrids: list[str]) -> list[str]:
    """Search already-selected All Mail for every listed thread in one nested OR.

    Live Gmail accepts this parenthesized form and returns the same UID set as
    one `X-GM-THRID <id>` search per thread. `X-GM-RAW thrid:` is not equivalent.
    """
    if not thrids:
        return []
    if len(thrids) == 1:
        return gmail_thread_uids(client, thrids[0])
    typ, data = imap_uid(client, f"gmail-thread-or-search n={len(thrids)}", "search", None, gmail_thrid_or_query(thrids))
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail thread search failed: {typ}")
    uids = [raw.decode() for raw in data[0].split()] if data and data[0] else []
    if any(not uid.isdecimal() for uid in uids) or len(uids) != len(set(uids)):
        raise RuntimeError("IMAP Gmail thread search returned incomplete or duplicate UIDs")
    return uids


def gmail_message_uids(client: imaplib.IMAP4_SSL, gmail_msgid: str) -> list[str]:
    if not gmail_msgid.isdecimal():
        raise RuntimeError("Gmail message identity was missing or malformed")
    typ, data = imap_uid(client, f"gmail-message-search message={gmail_msgid}", "search", None, "X-GM-MSGID", gmail_msgid)
    if typ != "OK":
        raise RuntimeError(f"IMAP Gmail message search failed: {typ}")
    return [raw.decode() for raw in data[0].split()] if data and data[0] else []


def require_gmail_identities(records: list[MailRecord]) -> None:
    missing = [record.uid for record in records if not record.gmail_msgid or not record.gmail_thrid or not record.raw_sha256]
    if missing:
        raise RuntimeError(f"Gmail identity metadata missing for {len(missing)} source messages")


def discover_gmail_thread_member_uids(
    client: imaplib.IMAP4_SSL,
    records: list[MailRecord],
    *,
    report: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Locate each source thread in All Mail after requiring the Sent special-use mailbox."""
    require_gmail_identities(records)
    if len({record.gmail_msgid for record in records}) != len(records):
        raise RuntimeError("configured IMAP mailbox returned duplicate Gmail message identities")
    special_use = special_use_mailboxes(client)
    all_mailbox = special_use.get(r"\All", "")
    if not all_mailbox:
        raise RuntimeError("Gmail All Mail special-use mailbox was not discovered")
    if not special_use.get(r"\Sent"):
        raise RuntimeError("Gmail Sent special-use mailbox was not discovered")
    source_ids_by_thread: dict[str, set[str]] = {}
    for record in records:
        source_ids_by_thread.setdefault(record.gmail_thrid, set()).add(record.gmail_msgid)
    select_mailbox(client, all_mailbox, readonly=True)
    if not source_ids_by_thread:
        return special_use, []
    seen_uids: set[str] = set()
    all_uids: list[str] = []
    thrids = sorted(source_ids_by_thread)
    n_threads = len(thrids)
    for start in range(0, n_threads, GMAIL_THREAD_OR_BATCH):
        batch = thrids[start : start + GMAIL_THREAD_OR_BATCH]
        if report:
            print(f"identity_preflight thread_search={start + len(batch)}/{n_threads}", file=sys.stderr)
        try:
            batch_uids = gmail_thread_uids_union(client, batch)
        except RuntimeError as exc:
            if not str(exc).startswith("IMAP Gmail thread search failed:"):
                raise
            batch_uids = []
            for gmail_thrid in batch:
                thread_uids = gmail_thread_uids(client, gmail_thrid)
                if not thread_uids:
                    raise RuntimeError("Gmail thread context was incomplete or changed during discovery")
                if seen_uids.intersection(thread_uids):
                    raise RuntimeError("IMAP Gmail thread search returned duplicate UID")
                seen_uids.update(thread_uids)
                batch_uids.extend(thread_uids)
            all_uids.extend(batch_uids)
            continue
        if not batch_uids:
            raise RuntimeError("Gmail thread context was incomplete or changed during discovery")
        if seen_uids.intersection(batch_uids):
            raise RuntimeError("IMAP Gmail thread search returned duplicate UID")
        seen_uids.update(batch_uids)
        all_uids.extend(batch_uids)
    return special_use, all_uids


def complete_gmail_thread_identities(client: imaplib.IMAP4_SSL, records: list[MailRecord]) -> dict[str, list[GmailMetadata]]:
    """Verify All Mail thread membership from Gmail identities after discovering Sent."""
    source_ids_by_thread: dict[str, set[str]] = {}
    for record in records:
        source_ids_by_thread.setdefault(record.gmail_thrid, set()).add(record.gmail_msgid)
    _special_use, all_uids = discover_gmail_thread_member_uids(client, records, report=True)
    identities_by_uid: dict[str, GmailMetadata] = {}
    for start in range(0, len(all_uids), GMAIL_IDENTITY_UID_BATCH):
        chunk = all_uids[start : start + GMAIL_IDENTITY_UID_BATCH]
        try:
            identities_by_uid.update(fetch_gmail_metadata_records(client, chunk))
        except RuntimeError as exc:
            if str(exc) != "IMAP Gmail metadata batch fetch returned incomplete or duplicate UIDs":
                raise
            for uid in chunk:
                identities_by_uid[uid] = fetch_gmail_metadata_detail(client, uid)
    records_by_thread: dict[str, list[GmailMetadata]] = {}
    for uid in all_uids:
        item = identities_by_uid[uid]
        records_by_thread.setdefault(item.gmail_thrid, []).append(item)
    if set(records_by_thread) != set(source_ids_by_thread):
        raise RuntimeError("Gmail thread context was incomplete or changed during discovery")
    for gmail_thrid, source_ids in source_ids_by_thread.items():
        identities = records_by_thread[gmail_thrid]
        context_ids = [item.gmail_msgid for item in identities]
        if (
            not identities
            or any(not item.gmail_msgid.isdecimal() or not item.gmail_thrid.isdecimal() for item in identities)
            or len(context_ids) != len(set(context_ids))
            or any(item.gmail_thrid != gmail_thrid for item in identities)
            or not source_ids.issubset(context_ids)
        ):
            raise RuntimeError("Gmail thread context was incomplete or changed during discovery")
    return records_by_thread


def fetch_imap_thread_contexts(
    client: imaplib.IMAP4_SSL,
    records: list[MailRecord],
) -> tuple[dict[str, str], dict[str, list[MailRecord]]]:
    """Fetch complete Gmail thread context through the configured IMAP session.

    Live Gmail accepts a nested `X-GM-THRID` OR search for a bounded thread
    batch, so each source thread is located that way in All Mail after Sent is
    discovered. Duplicate UID responses fail closed.
    """
    special_use, all_uids = discover_gmail_thread_member_uids(client, records)
    all_context_records = fetch_full_records(client, all_uids)
    source_ids_by_thread: dict[str, set[str]] = {}
    for record in records:
        source_ids_by_thread.setdefault(record.gmail_thrid, set()).add(record.gmail_msgid)
    records_by_thread: dict[str, list[MailRecord]] = {}
    for record in all_context_records:
        records_by_thread.setdefault(record.gmail_thrid, []).append(record)
    if set(records_by_thread) != set(source_ids_by_thread):
        raise RuntimeError("Gmail thread context was incomplete or changed during discovery")
    for gmail_thrid, source_ids in source_ids_by_thread.items():
        context_records = records_by_thread[gmail_thrid]
        require_gmail_identities(context_records)
        context_ids = [item.gmail_msgid for item in context_records]
        if not context_records or len(context_ids) != len(set(context_ids)) or any(item.gmail_thrid != gmail_thrid for item in context_records) or not source_ids.issubset(context_ids):
            raise RuntimeError("Gmail thread context was incomplete or changed during discovery")
    return special_use, records_by_thread


def fetch_direct_thread_contexts(
    client: imaplib.IMAP4_SSL,
    records: list[MailRecord],
) -> dict[str, list[MailRecord]]:
    """Fetch the exact All Mail plus recoverable Trash context used by `trash-explicit`."""
    _special_use, records_by_thread = fetch_imap_thread_contexts(client, records)
    select_mailbox(client, TRASH_MAILBOX, readonly=True)
    for gmail_thrid, all_records in records_by_thread.items():
        trash_records = fetch_full_records(client, gmail_thread_uids(client, gmail_thrid))
        require_gmail_identities(trash_records)
        if any(record.gmail_thrid != gmail_thrid for record in trash_records):
            raise RuntimeError("Gmail Trash thread context changed during discovery")
        all_ids = {record.gmail_msgid for record in all_records}
        trash_ids = {record.gmail_msgid for record in trash_records}
        if len(all_ids) != len(all_records) or len(trash_ids) != len(trash_records) or all_ids & trash_ids:
            raise RuntimeError("Gmail All Mail and Trash context was duplicate or ambiguous")
        records_by_thread[gmail_thrid] = [*all_records, *trash_records]
    return records_by_thread


def thread_context_digest(records: list[MailRecord]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda value: value.gmail_msgid):
        digest.update(
            "\t".join(
                (
                    record.gmail_msgid,
                    record.gmail_thrid,
                    record.msgid_sha256,
                    record.raw_sha256,
                    record.flags,
                    record.labels,
                )
            ).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


def tsv_value(value: str) -> str:
    return " ".join(value.replace("\t", " ").replace("\r", " ").replace("\n", " ").split())


def summary_token(value: str) -> str:
    return re.sub(r"\s+", "_", tsv_value(value)) or "unknown"


def abort_imap_client(client: imaplib.IMAP4_SSL) -> None:
    setattr(client, "_omo_operation_timed_out", True)
    sock = getattr(client, "sock", None)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
    try:
        sock.close()
    except OSError:
        pass


def trash_explicit_summary(
    task_ids: list[str],
    replacement_ids: list[str],
    retained_replacement_count: int,
    sources: list[ScopedSource],
    runtime_bundle_sha256: str,
    source_location_mode: str,
    final_context_mode: str,
    final_gate_passed: bool,
    final_gate_observations: str,
    move_attempted: int,
    final_inbox: list[MailRecord],
    verified_inbox: list[MailRecord],
    verified_trash: list[MailRecord],
    move_outcome: str,
    post_move_verification_error: str,
    post_move_reconciliation_ran: bool,
    post_move_reconciled: bool,
) -> str:
    moved_now = len({record.gmail_msgid for record in verified_trash} & {record.gmail_msgid for record in final_inbox})
    post_move_verified = not post_move_verification_error and not verified_inbox and len(verified_trash) == len(sources)
    return (
        f"trash_explicit: task_ids={','.join(task_ids)} replacements={len(replacement_ids)}"
        f" retained_replacements={retained_replacement_count} requested={len(sources)}"
        f" runtime_bundle_sha256={runtime_bundle_sha256}"
        f" source_location_mode={source_location_mode}"
        f" final_context={final_context_mode} final_gate_passed={int(final_gate_passed)}"
        f" final_gate_observations={final_gate_observations}"
        f" move_attempted={move_attempted} moved_now={moved_now} move_outcome={move_outcome}"
        f" verified_inbox={len(verified_inbox)} verified_trash={len(verified_trash)}"
        f" post_move_verified={int(post_move_verified)}"
        f" post_move_verification_error={post_move_verification_error or 'none'}"
        f" final_gate_atomic=0 residual_arrival_race=nonzero"
        f" post_move_reconciliation_ran={int(post_move_reconciliation_ran)} post_move_reconciled={int(post_move_reconciled)}"
        f" later_arrivals_moved=0 permanent_deleted=0 persisted_evidence=0"
    )


def arm_trash_explicit_pre_move_timer(stage: Callable[[], str], client: Callable[[], imaplib.IMAP4_SSL | None]) -> Callable[[], None]:
    if threading.current_thread() is not threading.main_thread():
        raise ImapOperationError("arm-pre-move-timer", "trash-explicit pre-move timer requires the main thread")
    try:
        timer_id = signal.ITIMER_REAL
        alarm_signal = signal.SIGALRM
        set_timer = signal.setitimer
        get_timer = signal.getitimer
    except AttributeError as exc:
        raise ImapOperationError("arm-pre-move-timer", "trash-explicit pre-move timer is unavailable") from exc
    previous_timer = get_timer(timer_id)
    if previous_timer[0] > 0 or previous_timer[1] > 0:
        raise ImapOperationError("arm-pre-move-timer", "trash-explicit pre-move timer would replace an active alarm")
    previous_handler = signal.getsignal(alarm_signal)

    def timeout_handler(_signum: int, _frame: object) -> None:
        current_stage = stage()
        active_client = client()
        if active_client is not None:
            abort_imap_client(active_client)
        raise ImapOperationError(
            current_stage,
            f"trash-explicit pre-move timed out: stage={current_stage} timeout_s={TRASH_EXPLICIT_PRE_MOVE_TIMEOUT_S:g}",
        )

    signal.signal(alarm_signal, timeout_handler)
    try:
        set_timer(timer_id, TRASH_EXPLICIT_PRE_MOVE_TIMEOUT_S)
    except BaseException:
        signal.signal(alarm_signal, previous_handler)
        raise
    active = True

    def disarm_timer() -> None:
        nonlocal active
        if not active:
            return
        try:
            set_timer(timer_id, previous_timer[0], previous_timer[1])
        finally:
            signal.signal(alarm_signal, previous_handler)
        active = False

    return disarm_timer


def write_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)


def write_thread_context(out_dir: Path, records_by_thread: dict[str, list[MailRecord]], sender_email: str, recipient_email: str) -> dict[str, str]:
    context_dir = out_dir / "threads"
    write_private_dir(context_dir)
    rows = ["gmail_thrid\tgmail_msgid\tmsgid_sha256\traw_sha256\tflags\tlabels\tscope\tsender\trecipient\tall_mailbox_uid\tbody_bytes"]
    digests: dict[str, str] = {}
    for gmail_thrid, records in sorted(records_by_thread.items()):
        digests[gmail_thrid] = thread_context_digest(records)
        for record in records:
            scope = "manager-to-human" if is_manager_record(record, sender_email, recipient_email) else "other"
            rows.append(
                "\t".join(
                    (
                        gmail_thrid,
                        record.gmail_msgid,
                        record.msgid_sha256,
                        record.raw_sha256,
                        tsv_value(record.flags),
                        tsv_value(record.labels),
                        scope,
                        tsv_value(record.sender),
                        tsv_value(record.to),
                        record.uid,
                        str(record.body_bytes),
                    )
                )
            )
            write_private(context_dir / f"{gmail_thrid}-{record.gmail_msgid}.txt", export_body(record, include_addresses=True))
    write_private(out_dir / "thread-context.tsv", "\n".join(rows) + "\n")
    write_private(
        out_dir / "thread-digests.tsv",
        "gmail_thrid\tthread_context_sha256\n" + "\n".join(f"{thread}\t{digest}" for thread, digest in sorted(digests.items())) + "\n",
    )
    return digests


def print_records(records: list[MailRecord]) -> None:
    print("uid\tdate\tmsgid_sha256\tsubject")
    for record in records:
        print(f"{record.uid}\t{record.date}\t{record.msgid_sha256}\t{record.subject}")


def cmd_identity_preflight(_args: argparse.Namespace) -> int:
    """Print aggregate IMAP Gmail identity evidence without identifiers."""
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        headers, skipped = accepted_manager_headers(client, manager_candidate_uids(client, sender_email), sender_email, recipient_email)
        uidvalidity = ""
        source_records: list[MailRecord] = []
        records_by_thread: dict[str, list[GmailMetadata]] = {}
        gmail_extension = 0
        imap_failure = 0
        try:
            gmail_extension = int(gmail_extension_advertised(client))
            if not gmail_extension:
                raise RuntimeError("configured IMAP mailbox does not advertise Gmail identity support")
            uidvalidity = selected_uidvalidity(client)
            try:
                metadata_by_uid = fetch_gmail_metadata_records(client, [header.uid for header in headers])
                source_records = [
                    replace(
                        header,
                        gmail_msgid=metadata_by_uid[header.uid].gmail_msgid,
                        gmail_thrid=metadata_by_uid[header.uid].gmail_thrid,
                        flags=metadata_by_uid[header.uid].flags,
                        labels=metadata_by_uid[header.uid].labels,
                        source_uidvalidity=uidvalidity,
                    )
                    for header in headers
                ]
                records_by_thread = complete_gmail_thread_identities(client, source_records)
            except RuntimeError as exc:
                if str(exc) != "IMAP Gmail metadata batch fetch returned incomplete or duplicate UIDs":
                    raise
                source_records = [replace(record, source_uidvalidity=uidvalidity) for record in fetch_records(client, [header.uid for header in headers], with_body=True, with_metadata=True)]
                records_by_thread = complete_gmail_thread_identities(client, source_records)
            require_gmail_identities(source_records)
            if len({record.gmail_msgid for record in source_records}) != len(source_records):
                raise RuntimeError("configured IMAP mailbox returned duplicate Gmail message identities")
        except (imaplib.IMAP4.error, RuntimeError) as exc:
            imap_failure = 1
            stage = exc.stage if isinstance(exc, ImapOperationError) else "identity-evidence"
            print(f"identity_preflight blocked failed_stage={stage}", file=sys.stderr)
        expected_threads = len({record.gmail_thrid for record in source_records})
        gate = "pass" if uidvalidity and gmail_extension and not imap_failure and len(source_records) == len(headers) and len(records_by_thread) == expected_threads else "block"
        print(
            "identity_preflight"
            f" accepted={len(headers)}"
            f" skipped_boundary_mismatch={len(skipped)}"
            f" source_uidvalidity_present={int(bool(uidvalidity))}"
            f" gmail_imap_extension={gmail_extension}"
            f" imap_evidence_failure_count={imap_failure}"
            f" unique_identity_count={len(source_records) if not imap_failure else 0}"
            f" complete_thread_count={len(records_by_thread)}"
            f" gate={gate}"
        )
    finally:
        logout_mailbox(client)
    return 0 if gate == "pass" else 1


def cmd_snapshot(_args: argparse.Namespace) -> int:
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        uids = manager_candidate_uids(client, sender_email)
        records, skipped = accepted_manager_headers(client, uids, sender_email, recipient_email)
        print(f"manager_candidate_count={len(records)}")
        if skipped:
            print(f"skipped_boundary_mismatch={len(skipped)}")
        print_records(records)
    finally:
        logout_mailbox(client)
    return 0


def validate_unread_summary_bounds(max_body_chars: int, max_threads: int, max_messages_per_thread: int) -> None:
    if max_body_chars < 80 or max_body_chars > 5000:
        raise RuntimeError("max body characters must be between 80 and 5000")
    if max_threads < 1 or max_threads > 100:
        raise RuntimeError("max threads must be between 1 and 100")
    if max_messages_per_thread < 1 or max_messages_per_thread > 50:
        raise RuntimeError("max messages per thread must be between 1 and 50")


def bounded_clean_body(body: str, max_chars: int) -> str:
    text = clean_body_text(body)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def clean_body_text(body: str) -> str:
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            continue
        lines.append(stripped)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def stable_text_digest(values: list[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(str(len(encoded)).encode())
        digest.update(b":")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def unread_records_with_metadata(client: imaplib.IMAP4_SSL, headers: list[MailRecord]) -> list[MailRecord]:
    metadata_by_uid = fetch_gmail_metadata_records_compatible(client, [header.uid for header in headers])
    records = [
        replace(
            header,
            gmail_msgid=metadata_by_uid[header.uid].gmail_msgid,
            gmail_thrid=metadata_by_uid[header.uid].gmail_thrid,
            flags=metadata_by_uid[header.uid].flags,
            labels=metadata_by_uid[header.uid].labels,
        )
        for header in headers
    ]
    missing_thread = [record.uid for record in records if not record.gmail_thrid]
    if missing_thread:
        raise RuntimeError(f"unread summary requires Gmail thread identities: uid={','.join(missing_thread)}")
    return records


def selected_unread_summary_headers(
    records: list[MailRecord],
    max_threads: int,
    max_messages_per_thread: int,
) -> tuple[list[tuple[list[MailRecord], list[MailRecord]]], int]:
    validate_unread_summary_bounds(120, max_threads, max_messages_per_thread)
    threads: dict[str, list[MailRecord]] = {}
    for record in records:
        threads.setdefault(record.gmail_thrid, []).append(record)
    ordered_threads = sorted(
        threads.values(),
        key=lambda thread_records: max(int(record.uid) for record in thread_records if record.uid.isdecimal()),
        reverse=True,
    )
    selected: list[tuple[list[MailRecord], list[MailRecord]]] = []
    for thread_records in ordered_threads[:max_threads]:
        ordered = sorted(thread_records, key=lambda record: int(record.uid) if record.uid.isdecimal() else -1)
        selected.append((ordered, ordered[-max_messages_per_thread:]))
    return selected, max(0, len(ordered_threads) - max_threads)


def unread_thread_summaries(
    header_threads: list[tuple[list[MailRecord], list[MailRecord]]],
    body_records: dict[str, MailRecord],
    max_body_chars: int,
) -> list[dict[str, object]]:
    validate_unread_summary_bounds(max_body_chars, max(1, len(header_threads)), 1)
    summaries: list[dict[str, object]] = []
    for all_headers, included_headers in header_threads:
        included = [replace(header, body=body_records[header.uid].body, raw_sha256=body_records[header.uid].raw_sha256) for header in included_headers]
        latest = all_headers[-1]
        targets = [target for target in (subject_tmux_target(record.subject) for record in reversed(all_headers)) if target]
        read_now_items: list[dict[str, str]] = []
        read_now_parts: list[str] = []
        remaining_chars = max_body_chars
        for record in reversed(included):
            prefix = f"UID {record.uid}: "
            if remaining_chars <= len(prefix):
                break
            text_budget = remaining_chars - len(prefix)
            text = bounded_clean_body(record.body, text_budget)
            if not text:
                continue
            part = f"{prefix}{text}"
            if read_now_parts and len(part) + 2 > remaining_chars:
                break
            if read_now_parts:
                remaining_chars -= 2
            read_now_parts.append(part)
            read_now_items.append({"uid": record.uid, "subject": record.subject, "text": text})
            remaining_chars -= len(part)
        all_unread_uid_values = [record.uid for record in all_headers]
        all_unread_subject_values = [record.subject for record in all_headers]
        summaries.append(
            {
                "gmail_thread_id": latest.gmail_thrid,
                "unread_count": len(all_headers),
                "included_message_count": len(included),
                "omitted_older_unread_count": max(0, len(all_headers) - len(included)),
                "uids": [record.uid for record in included],
                "all_unread_uid_sha256": stable_text_digest(all_unread_uid_values),
                "latest_uid": latest.uid,
                "latest_subject": latest.subject,
                "latest_date": latest.date,
                "latest_sender": latest.sender,
                "latest_target": targets[0] if targets else "",
                "subjects": [record.subject for record in included],
                "all_unread_subject_sha256": stable_text_digest(all_unread_subject_values),
                "read_now": "\n\n".join(read_now_parts),
                "read_now_items": read_now_items,
            }
        )
    return summaries


def cmd_unread_summary(args: argparse.Namespace) -> int:
    validate_unread_summary_bounds(args.max_body_chars, args.max_threads, args.max_messages_per_thread)
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        candidate_uids = manager_unread_candidate_uids(client, sender_email)
        headers, skipped = accepted_manager_headers(client, candidate_uids, sender_email, recipient_email)
        metadata_headers = unread_records_with_metadata(client, headers)
        selected_threads, truncated = selected_unread_summary_headers(metadata_headers, args.max_threads, args.max_messages_per_thread)
        selected_uids = [record.uid for _all_headers, included_headers in selected_threads for record in included_headers]
        body_records = {record.uid: record for record in fetch_full_records(client, selected_uids)}
        if {record.uid for record in body_records.values() if is_manager_record(record, sender_email, recipient_email)} != set(selected_uids):
            raise RuntimeError("unread summary boundary changed between header and body fetch")
        summaries = unread_thread_summaries(selected_threads, body_records, args.max_body_chars)
        result = {
            "schema": "omo-manager-mail-unread-summary/v1",
            "read_only": True,
            "mailbox": "INBOX",
            "source_uidvalidity": selected_uidvalidity(client),
            "manager_sender": sender_email,
            "human_recipient": recipient_email,
            "candidate_unread_count": len(candidate_uids),
            "accepted_unread_count": len(headers),
            "fetched_body_count": len(selected_uids),
            "skipped_boundary_mismatch": skipped,
            "thread_count": len(summaries),
            "truncated_thread_count": truncated,
            "threads": summaries,
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    finally:
        logout_mailbox(client)
    return 0


def current_agent_mail_target() -> str:
    pane = os.environ.get("TMUX_PANE", "").strip()
    if pane:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#S:#I.#P"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("could not resolve the current tmux pane")
        target = result.stdout.strip()
    else:
        target = os.environ.get("OMO_AGENT_TMUX_TARGET", "").strip()
    target = canonical_tmux_target(target)
    if TMUX_TARGET_RE.fullmatch(target) is None:
        raise RuntimeError("could not resolve the current agent tmux target")
    return target


def current_agent_session_id() -> str:
    value = (os.environ.get("CODEX_SESSION_ID", "").strip() or os.environ.get("CODEX_THREAD_ID", "").strip()).lower()
    return value if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value) else ""


def agent_unread_records(client: imaplib.IMAP4_SSL, sender_email: str, recipient_email: str, target: str, agent_session: str) -> list[MailRecord]:
    candidates = manager_unread_candidate_uids(client, sender_email)
    headers, _skipped = accepted_manager_headers(client, candidates, sender_email, recipient_email)
    records = unread_records_with_metadata(client, headers)
    return [
        record
        for record in records
        if r"\Seen" not in record.flags
        and subject_tmux_target(record.subject) == target
        and record.agent_session_id in {"", agent_session}
    ]


def cmd_agent_unread(args: argparse.Namespace) -> int:
    del args
    target = current_agent_mail_target()
    agent_session = current_agent_session_id()
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        records = agent_unread_records(client, sender_email, recipient_email, target, agent_session)
        result = {
            "schema": "omo-agent-unread-mail/v1",
            "target": target,
            "source_uidvalidity": selected_uidvalidity(client),
            "count": len(records),
            "messages": [
                {
                    "uid": record.uid,
                    "date": record.date,
                    "subject": record.subject,
                    "message_id": record.message_id,
                    "trashable": bool(agent_session and record.agent_session_id == agent_session),
                }
                for record in records
            ],
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    finally:
        logout_mailbox(client)
    return 0


def agent_move_paths(target: str, agent_session: str, source_uidvalidity: str, uids: list[str], replacement_id: str) -> tuple[Path, Path]:
    state = Path(os.environ.get("OMO_MANAGER_STATE_DIR", Path.home() / ".local/state/omo-manager")) / "agent-mail-moves"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    digest = hashlib.sha256("\0".join((target, agent_session, source_uidvalidity, *uids, replacement_id)).encode()).hexdigest()
    return state / f"{digest}.intent.json", state / f"{digest}.outcome.json"


def agent_move_locations(client: imaplib.IMAP4_SSL, gmail_ids: list[str]) -> dict[str, str]:
    locations: dict[str, str] = {}
    for name, mailbox in (("inbox", "INBOX"), ("trash", TRASH_MAILBOX)):
        select_mailbox(client, mailbox, readonly=True)
        for gmail_id in gmail_ids:
            if gmail_message_uids(client, gmail_id):
                if gmail_id in locations:
                    raise RuntimeError("source appears in both Inbox and Trash")
                locations[gmail_id] = name
    return locations


def cmd_agent_trash_replaced(args: argparse.Namespace) -> int:
    if not args.yes:
        print("refusing to move unread mail to Trash without --yes", file=sys.stderr)
        return 2
    target = current_agent_mail_target()
    agent_session = current_agent_session_id()
    if not agent_session:
        raise RuntimeError("current agent has no stable Codex session identity")
    requested = parse_uid_text("\n".join(args.uid))
    if not requested:
        raise RuntimeError("at least one source UID is required")
    intent_path, outcome_path = agent_move_paths(target, agent_session, args.source_uidvalidity, requested, args.replacement_message_id)
    client, config = open_mailbox(readonly=False)
    try:
        sender_email, recipient_email = mail_boundary(config)
        if outcome_path.exists():
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            if (
                outcome.get("schema") != "omo-agent-mail-move-outcome/v1"
                or outcome.get("result") != "trash"
                or outcome.get("target") != target
                or outcome.get("agent_session_id") != agent_session
            ):
                raise RuntimeError("saved move outcome is malformed")
            print(f"agent_trash_replaced: recovered=1 permanent_deleted=0 target={target}")
            return 0
        if intent_path.exists():
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            gmail_ids = intent.get("source_gmail_ids", [])
            if (
                intent.get("schema") != "omo-agent-mail-move-intent/v1"
                or intent.get("target") != target
                or intent.get("agent_session_id") != agent_session
                or intent.get("source_uidvalidity") != args.source_uidvalidity
                or intent.get("source_uids") != requested
                or intent.get("replacement_message_id") != args.replacement_message_id
                or not isinstance(gmail_ids, list)
                or not gmail_ids
                or any(not isinstance(value, str) or not value.isdecimal() for value in gmail_ids)
            ):
                raise RuntimeError("saved move intent is malformed")
            locations = agent_move_locations(client, gmail_ids)
            if set(locations.values()) == {"trash"} and set(locations) == set(gmail_ids):
                write_private_exclusive(outcome_path, json.dumps({"schema": "omo-agent-mail-move-outcome/v1", "result": "trash", "target": target, "agent_session_id": agent_session, "source_gmail_ids": gmail_ids}, sort_keys=True) + "\n")
                print(f"agent_trash_replaced: recovered=1 moved={len(gmail_ids)} permanent_deleted=0 target={target}")
                return 0
            if set(locations.values()) != {"inbox"} or set(locations) != set(gmail_ids):
                raise RuntimeError("saved move intent has a mixed or missing mailbox outcome; inspect Inbox and Trash")
            select_mailbox(client, "INBOX", readonly=False)
        expected_uidvalidity = selected_uidvalidity(client)
        if expected_uidvalidity != args.source_uidvalidity:
            raise RuntimeError("source UIDVALIDITY changed; rerun agent-unread")
        records = agent_unread_records(client, sender_email, recipient_email, target, agent_session)
        records_by_uid = {record.uid: record for record in records}
        if set(requested) != set(records_by_uid).intersection(requested):
            raise RuntimeError("every source must still be unread mail sent by the current agent")
        sources = [records_by_uid[uid] for uid in requested]
        if any(record.agent_session_id != agent_session for record in sources):
            raise RuntimeError("legacy or prior-session mail is visible but not trashable by this agent")
        special_use = special_use_mailboxes(client)
        all_mailbox = special_use.get(r"\All", "")
        if not all_mailbox or not special_use.get(r"\Sent") or not mailbox_exists(client, TRASH_MAILBOX):
            raise RuntimeError("required Gmail All Mail, Sent, or Trash mailbox is unavailable")
        if not replacement_exists(client, all_mailbox, args.replacement_message_id, sender_email, recipient_email):
            raise RuntimeError("replacement Message-ID is missing, ambiguous, or outside the agent-human mail boundary")
        if subject_tmux_target(replacement_subject(client, all_mailbox, args.replacement_message_id, sender_email, recipient_email)) != target:
            raise RuntimeError("replacement was not sent by the current agent target")
        source_message_ids = {record.message_id for record in sources}
        if "" in source_message_ids or replacement_supersedes_ids(client, all_mailbox, args.replacement_message_id) != source_message_ids:
            raise RuntimeError("replacement does not explicitly supersede exactly the selected sources")
        if replacement_agent_session_id(client, all_mailbox, args.replacement_message_id) != agent_session:
            raise RuntimeError("replacement was not sent by the current agent session")
        replacement_gmail_id = replacement_gmail_msgid(client, all_mailbox, args.replacement_message_id)
        if replacement_gmail_id in {record.gmail_msgid for record in sources}:
            raise RuntimeError("replacement must be different from every source")
        if int(replacement_gmail_id) <= max(int(record.gmail_msgid) for record in sources):
            raise RuntimeError("replacement must be newer than every selected source")
        select_mailbox(client, "INBOX", readonly=False)
        if selected_uidvalidity(client) != expected_uidvalidity or set(inbox_subset(client, requested)) != set(requested):
            raise RuntimeError("source mailbox changed before mutation; rerun agent-unread")
        final_metadata = fetch_gmail_metadata_records_compatible(client, requested)
        if any(
            r"\Seen" in final_metadata[record.uid].flags
            or final_metadata[record.uid].gmail_msgid != record.gmail_msgid
            or final_metadata[record.uid].gmail_thrid != record.gmail_thrid
            for record in sources
        ):
            raise RuntimeError("a source was read or changed before mutation; nothing was moved")
        if not intent_path.exists():
            write_private_exclusive(
                intent_path,
                json.dumps(
                    {
                        "schema": "omo-agent-mail-move-intent/v1",
                        "target": target,
                        "agent_session_id": agent_session,
                        "source_uidvalidity": expected_uidvalidity,
                        "source_uids": requested,
                        "source_gmail_ids": [record.gmail_msgid for record in sources],
                        "source_message_ids": sorted(source_message_ids),
                        "replacement_message_id": args.replacement_message_id,
                    },
                    sort_keys=True,
                )
                + "\n",
            )
        typ, _data = imap_uid(client, "move-agent-unread-to-trash", "MOVE", ",".join(requested), imap_quoted(TRASH_MAILBOX))
        if typ != "OK":
            raise RuntimeError(f"Gmail MOVE failed: {typ}")
        select_mailbox(client, "INBOX", readonly=True)
        remaining = inbox_subset(client, requested)
        select_mailbox(client, TRASH_MAILBOX, readonly=True)
        trashed = {record.gmail_msgid: gmail_message_uids(client, record.gmail_msgid) for record in sources}
        if remaining or any(len(uids) != 1 for uids in trashed.values()):
            raise RuntimeError("move outcome could not be verified; inspect Inbox and Trash before retrying")
        write_private_exclusive(
            outcome_path,
            json.dumps({"schema": "omo-agent-mail-move-outcome/v1", "result": "trash", "target": target, "agent_session_id": agent_session, "source_gmail_ids": [record.gmail_msgid for record in sources]}, sort_keys=True) + "\n",
        )
        print(f"agent_trash_replaced: moved={len(sources)} permanent_deleted=0 target={target}")
    finally:
        logout_mailbox(client)
    return 0


def cmd_inspect_explicit(args: argparse.Namespace) -> int:
    """Print live source/context bindings and bodies without persisted evidence."""
    try:
        if not args.task_id or "\n" in args.task_id or "\r" in args.task_id:
            raise ValueError("one nonempty task identity is required")
        uids = parse_uids(args.uids, None)
        if not uids:
            raise ValueError("at least one explicit source UID is required")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        uidvalidity = selected_uidvalidity(client)
        records = [replace(record, source_uidvalidity=uidvalidity) for record in fetch_records(client, uids, with_body=True, with_metadata=True)]
        if len(records) != len(uids) or {record.uid for record in records} != set(uids):
            raise RuntimeError("one or more explicit source UIDs are missing or duplicated")
        require_gmail_identities(records)
        if any(not is_manager_record(record, sender_email, recipient_email) for record in records):
            raise RuntimeError("one or more explicit sources are outside the manager-mail boundary")
        records_by_thread = fetch_direct_thread_contexts(client, records)
        print(f"task_id={args.task_id}")
        print(f"source_uidvalidity={uidvalidity}")
        for record in records:
            print(f"source={record.uid}:{record.gmail_msgid}:{record.gmail_thrid}:{record.raw_sha256}")
        selected_message_ids = {record.gmail_msgid for record in records}
        for gmail_thrid in sorted(records_by_thread):
            for record in sorted(records_by_thread[gmail_thrid], key=lambda value: value.gmail_msgid):
                print(f"context={record.gmail_msgid}:{record.gmail_thrid}:{record.raw_sha256}")
                print(f"context_selected={int(record.gmail_msgid in selected_message_ids)}")
                print(f"context_date={tsv_value(record.date)}")
                print(f"context_from={tsv_value(record.sender)}")
                print(f"context_to={tsv_value(record.to)}")
                print(f"context_sender_tmux_target={subject_tmux_target(record.subject)}")
                print("----- context body -----")
                print(export_body(record, include_addresses=False), end="")
        for record in records:
            print(f"selected_source_sender_tmux_target={subject_tmux_target(record.subject)}")
            print("----- selected source body -----")
            print(export_body(record, include_addresses=False), end="")
    finally:
        logout_mailbox(client)
    return 0


def cmd_locate_replacement(args: argparse.Namespace) -> int:
    """Print the unique RFC Message-ID for an exact current manager-mail subject."""
    if not args.subject or "\n" in args.subject or "\r" in args.subject:
        print("one nonempty exact subject is required", file=sys.stderr)
        return 2
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        all_mailbox = special_use_mailboxes(client).get(r"\All")
        if not all_mailbox:
            print("refusing because Gmail All Mail is missing", file=sys.stderr)
            return 1
        select_mailbox(client, all_mailbox, readonly=True)
        records, _skipped = accepted_manager_headers(client, manager_candidate_uids(client, sender_email), sender_email, recipient_email)
        matches = [record for record in records if record.subject == args.subject]
        if len(matches) != 1:
            print(f"replacement_subject_matches={len(matches)}", file=sys.stderr)
            return 1
        msg, _raw_sha256 = fetch_msg(client, matches[0].uid, HEADER_FETCH)
        message_id = rfc_message_id(msg)
        if not message_id:
            print("replacement has no unique valid Message-ID", file=sys.stderr)
            return 1
        print(f"message_id={message_id}")
        print(f"uid={matches[0].uid}")
    finally:
        logout_mailbox(client)
    return 0


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)


def write_private_exclusive(path: Path, text: str) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"private evidence already exists: {path.name}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def export_receipt_path(out_dir: Path) -> Path:
    out_dir = out_dir.resolve(strict=False)
    return out_dir.parent / f".{out_dir.name}.export-terminal.tsv"


def export_receipt_text(out_dir: Path, category: str, stage: str, error: str) -> str:
    run_dir_sha256 = hashlib.sha256(str(out_dir.resolve(strict=False)).encode()).hexdigest()
    return (
        "version\trun_dir_sha256\texit_category\tstage\terror\tauthority\n"
        f"1\t{run_dir_sha256}\t{tsv_value(category)}\t{tsv_value(stage)}\t{tsv_value(error)}\tnone\n"
    )


def export_failure_diagnostics(exc: BaseException, export_stage: str) -> tuple[str, str, str]:
    if isinstance(exc, ImapOperationError):
        stage = re.sub(r"\bmailbox=.*", "mailbox=<redacted>", exc.stage)
        stage = re.sub(r"\b(uid|thread|message)=[^ ]+", r"\1=<redacted>", stage)
        if "timed out" in str(exc):
            return "imap-timeout", stage, "deadline-expired"
        return "imap-failure", stage, "operation-failed"
    if isinstance(exc, (OSError, RuntimeError, imaplib.IMAP4.error)):
        return "export-failure", export_stage, type(exc).__name__
    return "unexpected-exception", export_stage, type(exc).__name__


def write_export_receipt(out_dir: Path, category: str, stage: str, error: str) -> None:
    receipt_path = export_receipt_path(out_dir)
    try:
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"private evidence already exists: {receipt_path.name}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(export_receipt_text(out_dir, category, stage, error))
            handle.flush()
            os.fsync(handle.fileno())
        fsync_dir(receipt_path.parent)
    except BaseException as exc:
        fd = os.open(receipt_path, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(export_receipt_text(out_dir, "receipt-failure", "terminal-receipt", type(exc).__name__))
            handle.flush()
            os.fsync(handle.fileno())
        fsync_dir(receipt_path.parent)
        raise


def ensure_empty_private_dir(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise RuntimeError(f"export directory must be empty: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def export_body(record: MailRecord, include_addresses: bool = False) -> str:
    addresses = f"From: {record.sender}\nTo: {record.to}\n" if include_addresses else ""
    return (
        f"UID: {record.uid}\n"
        f"Date: {record.date}\n"
        f"Subject: {record.subject}\n"
        f"{addresses}"
        f"Message-ID-SHA256: {record.msgid_sha256}\n"
        f"Gmail-Message-ID: {record.gmail_msgid}\n"
        f"Gmail-Thread-ID: {record.gmail_thrid}\n"
        f"Source-UIDVALIDITY: {record.source_uidvalidity}\n"
        f"Flags: {record.flags}\n"
        f"Labels: {record.labels}\n"
        f"Raw-SHA256: {record.raw_sha256}\n\n"
        f"{record.body.rstrip()}\n"
    )


def export_manifest(records: list[MailRecord], thread_digests: dict[str, str], scope: ReviewedScope | None = None, scope_tasks_sha256: str = "") -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=(
            "uid",
            "source_mailbox",
            "uidvalidity",
            "date",
            "gmail_msgid",
            "gmail_thrid",
            "msgid_sha256",
            "raw_sha256",
            "flags",
            "labels",
            "thread_context_sha256",
            "scope_tasks_sha256",
            "scope_sha256",
            "scope_preparer",
            "scope_reviewer",
            "scope_provenance",
            "body_bytes",
            "subject",
        ),
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                "uid": record.uid,
                "source_mailbox": "INBOX",
                "uidvalidity": record.source_uidvalidity,
                "date": tsv_value(record.date),
                "gmail_msgid": record.gmail_msgid,
                "gmail_thrid": record.gmail_thrid,
                "msgid_sha256": record.msgid_sha256,
                "raw_sha256": record.raw_sha256,
                "flags": tsv_value(record.flags),
                "labels": tsv_value(record.labels),
                "thread_context_sha256": thread_digests[record.gmail_thrid],
                "scope_tasks_sha256": scope_tasks_sha256,
                "scope_sha256": scope.sha256 if scope else "",
                "scope_preparer": scope.preparer if scope else "",
                "scope_reviewer": scope.reviewer if scope else "",
                "scope_provenance": scope.provenance if scope else "",
                "body_bytes": str(record.body_bytes),
                "subject": tsv_value(record.subject),
            }
        )
    return output.getvalue()


def export_batches(records: list[MailRecord], threads_per_batch: int) -> str:
    if threads_per_batch < 1:
        raise ValueError("threads per batch must be positive")
    threads = sorted({record.gmail_thrid for record in records})
    batch_by_thread = {thread: f"batch-{index // threads_per_batch + 1:04d}" for index, thread in enumerate(threads)}
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=("batch_id", "gmail_thrid", "uid", "gmail_msgid", "subject", "body_file"),
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for record in sorted(records, key=lambda value: (value.gmail_thrid, value.gmail_msgid)):
        writer.writerow(
            {
                "batch_id": batch_by_thread[record.gmail_thrid],
                "gmail_thrid": record.gmail_thrid,
                "uid": record.uid,
                "gmail_msgid": record.gmail_msgid,
                "subject": tsv_value(record.subject),
                "body_file": f"{record.uid}.txt",
            }
        )
    return output.getvalue()


def read_tsv(path: Path, required_fields: set[str]) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
                raise RuntimeError(f"private source map is missing required fields: {path.name}")
            return [{key: value or "" for key, value in row.items()} for row in reader]
    except OSError as exc:
        raise RuntimeError(f"could not read private source map: {path.name}") from exc


def read_literal_tsv(path: Path, required_fields: set[str]) -> list[dict[str, str]]:
    """Read historical tab-joined evidence without interpreting quote characters."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"could not read private source map: {path.name}") from exc
    if not lines:
        raise RuntimeError(f"private source map is missing required fields: {path.name}")
    fieldnames = lines[0].split("\t")
    if len(fieldnames) != len(set(fieldnames)) or not required_fields.issubset(fieldnames):
        raise RuntimeError(f"private source map is missing required fields: {path.name}")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        values = line.split("\t")
        if len(values) != len(fieldnames):
            raise RuntimeError(f"private source map has malformed row: {path.name}")
        rows.append(dict(zip(fieldnames, values, strict=True)))
    return rows


def read_tsv_text(text: str, required_fields: set[str], evidence_name: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
        raise RuntimeError(f"private source map is missing required fields: {evidence_name}")
    return [{key: value or "" for key, value in row.items()} for row in reader]


def export_source_map(out_dir: Path, requested: list[str]) -> dict[str, dict[str, str]]:
    rows = read_tsv(
        out_dir / "manifest.tsv",
        {
            "uid",
            "source_mailbox",
            "uidvalidity",
            "gmail_msgid",
            "gmail_thrid",
            "msgid_sha256",
            "raw_sha256",
            "flags",
            "labels",
            "thread_context_sha256",
        },
    )
    source_map = {row["uid"]: row for row in rows if row["uid"]}
    gmail_identities = [(row["gmail_thrid"], row["gmail_msgid"]) for row in rows]
    if len(source_map) != len(rows) or len(set(gmail_identities)) != len(rows) or any(uid not in source_map for uid in requested):
        raise RuntimeError("requested sources were absent or ambiguous in the private source map")
    return {uid: source_map[uid] for uid in requested}


def export_mailboxes(out_dir: Path) -> dict[str, str]:
    rows = read_tsv(out_dir / "mailboxes.tsv", {"role", "mailbox"})
    mailboxes = {row["role"]: row["mailbox"] for row in rows if row["role"] and row["mailbox"]}
    if r"\All" not in mailboxes or r"\Sent" not in mailboxes:
        raise RuntimeError("private source map lacks required special-use mailboxes")
    return mailboxes


def batch_rows(source_dir: Path) -> list[dict[str, str]]:
    rows = read_tsv(source_dir / "batches.tsv", {"batch_id", "gmail_thrid", "uid", "gmail_msgid", "subject", "body_file"})
    uids = [row["uid"] for row in rows]
    identities = [(row["gmail_thrid"], row["gmail_msgid"]) for row in rows]
    thread_batches: dict[str, set[str]] = {}
    for row in rows:
        thread_batches.setdefault(row["gmail_thrid"], set()).add(row["batch_id"])
    if (
        any(not re.fullmatch(r"batch-[0-9]{4}", row["batch_id"]) or not row["gmail_thrid"].isdecimal() or not row["uid"].isdecimal() for row in rows)
        or len(uids) != len(set(uids))
        or len(identities) != len(set(identities))
        or any(len(batches) != 1 for batches in thread_batches.values())
    ):
        raise RuntimeError("private batch map is malformed or assigns a source more than once")
    return rows


def validate_owner(owner: str) -> str:
    if not owner or tsv_value(owner) != owner:
        raise RuntimeError("batch owner must be one nonempty line")
    return owner


def claim_batch(source_dir: Path, batch_id: str, owner: str) -> None:
    owner = validate_owner(owner)
    if batch_id not in {row["batch_id"] for row in batch_rows(source_dir)}:
        raise RuntimeError(f"unknown batch: {batch_id}")
    claim_path = source_dir / "claims" / f"{batch_id}.tsv"
    content = f"batch_id\towner\n{batch_id}\t{owner}\n"
    try:
        write_private_exclusive(claim_path, content)
    except RuntimeError:
        try:
            existing = claim_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not read batch claim: {batch_id}") from exc
        if existing != content:
            raise RuntimeError(f"batch already belongs to another owner: {batch_id}")


def require_batch_owner(source_dir: Path, batch_id: str, owner: str) -> list[dict[str, str]]:
    owner = validate_owner(owner)
    rows = [row for row in batch_rows(source_dir) if row["batch_id"] == batch_id]
    if not rows:
        raise RuntimeError(f"unknown batch: {batch_id}")
    expected = f"batch_id\towner\n{batch_id}\t{owner}\n"
    try:
        actual = (source_dir / "claims" / f"{batch_id}.tsv").read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"batch is not claimed: {batch_id}") from exc
    if actual != expected:
        raise RuntimeError(f"batch is not owned by {owner}: {batch_id}")
    return rows


def evidence_digest(path: Path, kind: str) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not read {kind} evidence") from exc
    if not data.strip():
        raise RuntimeError(f"{kind} evidence must not be empty")
    return hashlib.sha256(data).hexdigest()


def thread_batch_rows(source_dir: Path, batch_id: str, owner: str, gmail_thrid: str) -> list[dict[str, str]]:
    if not gmail_thrid.isdecimal():
        raise RuntimeError("Gmail thread identity must be decimal")
    rows = [row for row in require_batch_owner(source_dir, batch_id, owner) if row["gmail_thrid"] == gmail_thrid]
    if not rows:
        raise RuntimeError("thread is not assigned to the claimed batch")
    return rows


def scoped_task_map(source_dir: Path) -> dict[str, str]:
    path = source_dir / "scope-tasks.tsv"
    manifest_path = source_dir / "manifest.tsv"
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("could not read immutable manifest") from exc
    reader = csv.DictReader(io.StringIO(manifest_text), delimiter="\t")
    if reader.fieldnames is None or "uid" not in reader.fieldnames:
        raise RuntimeError("private source map is missing required fields: manifest.tsv")
    scope_fields = {"scope_tasks_sha256", "scope_sha256", "scope_preparer", "scope_reviewer", "scope_provenance"}
    if not scope_fields.issubset(reader.fieldnames):
        if path.exists() or (source_dir / "scope.tsv").exists():
            raise RuntimeError("legacy unscoped manifest has unexpected scope artifacts")
        return {}
    manifest_rows = list(reader)
    if not manifest_rows:
        raise RuntimeError("immutable manifest must not be empty")
    manifest_digests = {row["scope_tasks_sha256"] for row in manifest_rows}
    if len(manifest_digests) != 1:
        raise RuntimeError("manifest has inconsistent scoped task map digests")
    manifest_digest = next(iter(manifest_digests))
    scope_path = source_dir / "scope.tsv"
    if not manifest_digest:
        if path.exists() or scope_path.exists():
            raise RuntimeError("unscoped manifest has unexpected scope artifacts")
        return {}
    if not path.is_file() or not scope_path.is_file():
        raise RuntimeError("scoped manifest lacks required reviewed task evidence")
    rows = read_tsv(path, {"uid", "task_id", "gmail_msgid", "gmail_thrid", "raw_sha256"})
    mapping = {row["uid"]: row["task_id"] for row in rows}
    manifest = export_source_map(source_dir, [row["uid"] for row in manifest_rows])
    if len(mapping) != len(rows) or set(mapping) != set(manifest):
        raise RuntimeError("scoped task map is missing, duplicate, or extra")
    for row in rows:
        source = manifest[row["uid"]]
        if (row["gmail_msgid"], row["gmail_thrid"], row["raw_sha256"]) != (source["gmail_msgid"], source["gmail_thrid"], source["raw_sha256"]):
            raise RuntimeError("scoped task map does not match immutable manifest")
    scope_rows = read_tsv(scope_path, {"scope_sha256", "scope_tasks_sha256", "preparer", "reviewer", "provenance"})
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    bound_review = {(row["scope_sha256"], row["scope_preparer"], row["scope_reviewer"], row["scope_provenance"]) for row in manifest_rows}
    if len(scope_rows) != 1 or len(bound_review) != 1 or scope_rows[0]["scope_tasks_sha256"] != digest or manifest_digest != digest:
        raise RuntimeError("scoped task map digest is missing or tampered")
    scope_row = scope_rows[0]
    if (scope_row["scope_sha256"], scope_row["preparer"], scope_row["reviewer"], scope_row["provenance"]) not in bound_review or not scope_row["preparer"] or not scope_row["reviewer"] or scope_row["preparer"] == scope_row["reviewer"]:
        raise RuntimeError("scoped review identity or provenance is missing or tampered")
    for evidence_path in (path, scope_path):
        stat = evidence_path.lstat()
        if evidence_path.is_symlink() or not evidence_path.is_file() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
            raise RuntimeError("scoped evidence must remain regular owner-only files")
    return mapping


def require_scoped_task(source_dir: Path, rows: list[dict[str, str]], task_id: str) -> None:
    mapping = scoped_task_map(source_dir)
    if mapping and any(mapping.get(row["uid"]) != task_id for row in rows):
        raise RuntimeError("task identity does not match independently reviewed scope")


def configured_work_logs_root() -> Path:
    """Return the trusted work-log root from the manager local-env file only."""
    local_root = parse_env_file(LOCAL_ENV_PATH).get("OMO_WORK_LOGS_ROOT", "").strip()
    if local_root:
        return Path(local_root).expanduser()
    return DEFAULT_WORK_LOGS_ROOT


def read_owner_only_regular_file(path: Path, error_prefix: str) -> tuple[Path, bytes]:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{error_prefix} is unreadable") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode) or path_stat.st_uid != os.geteuid() or path_stat.st_mode & 0o077:
        raise RuntimeError(f"{error_prefix} must be a regular owner-only file")
    try:
        resolved = path.resolve(strict=True)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError(f"{error_prefix} is unreadable") from exc
    close_fd = True
    try:
        fd_stat = os.fstat(fd)
        if not stat.S_ISREG(fd_stat.st_mode) or fd_stat.st_uid != os.geteuid() or fd_stat.st_mode & 0o077:
            raise RuntimeError(f"{error_prefix} must be a regular owner-only file")
        with os.fdopen(fd, "rb") as handle:
            close_fd = False
            return resolved, handle.read()
    except Exception:
        if close_fd:
            os.close(fd)
        raise


def require_human_approved_exact_removal(
    source_dir: Path,
    requested: list[str],
    task_id: str,
    replacement_not_required: bool,
    reason_file: Path,
    approval_file: Path | None,
    approval_quote: str | None,
) -> ExactRemovalEvidence:
    source_map = export_source_map(source_dir, requested)
    if not replacement_not_required:
        raise RuntimeError("human-approved exact removal requires --replacement-not-required")
    mapping = scoped_task_map(source_dir)
    if len(requested) != 1 or set(mapping) != set(requested) or any(value != task_id for value in mapping.values()):
        raise RuntimeError("human-approved exact removal requires one reviewed scoped source for this task")
    if task_id != SOURCE_815_TASK_ID:
        raise RuntimeError("human-approved exact removal is bound to source-815 task identity")
    if approval_file is None or not approval_quote or tsv_value(approval_quote) != approval_quote:
        raise RuntimeError("human-approved exact removal requires an approval file and exact one-line quote")
    if approval_quote != SOURCE_815_APPROVAL_QUOTE:
        raise RuntimeError("human-approved exact removal quote does not match source-815 approval")
    approval_arg = approval_file.expanduser()
    if not approval_arg.is_absolute():
        raise RuntimeError("human-approved exact removal requires an absolute regular manager_mail approval file")
    try:
        work_logs_root_arg = configured_work_logs_root()
        work_logs_root_stat = work_logs_root_arg.lstat()
        if stat.S_ISLNK(work_logs_root_stat.st_mode):
            raise RuntimeError("human-approved exact removal work-log root must not be a symlink")
        work_logs_root = work_logs_root_arg.resolve(strict=True)
        mail_root_arg = work_logs_root / "manager_mail"
        mail_root_stat = mail_root_arg.lstat()
        mail_root = mail_root_arg.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("human-approved exact removal work-log root is unreadable") from exc
    if stat.S_ISLNK(mail_root_stat.st_mode) or not stat.S_ISDIR(mail_root_stat.st_mode) or mail_root_stat.st_uid != os.geteuid() or mail_root_stat.st_mode & 0o077:
        raise RuntimeError("human-approved exact removal manager_mail root must be an owner-only directory")
    if approval_arg.parent != mail_root_arg or approval_arg.parent.resolve(strict=False) != mail_root or approval_arg.name != SOURCE_815_APPROVAL_FILE:
        raise RuntimeError("human-approved exact removal approval file is not the trusted source-815 manager mail")
    try:
        approval_path, approval_bytes = read_owner_only_regular_file(approval_arg, "human-approved exact removal approval file")
        approval_text = approval_bytes.decode("utf-8")
        reason_text = reason_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("human-approved exact removal approval evidence is unreadable") from exc
    approval_sha256 = hashlib.sha256(approval_bytes).hexdigest()
    source = source_map[requested[0]]
    source_binding = f"{requested[0]}:{source['gmail_msgid']}:{source['gmail_thrid']}:{source['raw_sha256']}"
    if approval_sha256 != SOURCE_815_APPROVAL_SHA256 or source_binding != SOURCE_815_SOURCE_BINDING:
        raise RuntimeError("human-approved exact removal evidence is not bound to source-815 identity")
    if (
        approval_quote not in approval_text
        or approval_quote not in reason_text
        or str(approval_path) not in reason_text
        or approval_sha256 not in reason_text
        or source_binding not in reason_text
    ):
        raise RuntimeError("human-approved exact removal evidence does not bind the exact approval quote")
    scope_rows = read_tsv(source_dir / "scope.tsv", {"provenance"})
    if len(scope_rows) != 1:
        raise RuntimeError("human-approved exact removal requires one scoped approval provenance")
    provenance_path = Path(scope_rows[0]["provenance"]).expanduser()
    try:
        resolved_provenance = provenance_path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("human-approved exact removal scope provenance is unreadable") from exc
    if not provenance_path.is_absolute() or resolved_provenance != approval_path:
        raise RuntimeError("human-approved exact removal scope provenance does not match approval file")
    return ExactRemovalEvidence(
        exception=SOURCE_815_EXACT_REMOVAL_EXCEPTION,
        approval_sha256=approval_sha256,
        approval_quote_sha256=SOURCE_815_APPROVAL_QUOTE_SHA256,
        approval_source_binding=source_binding,
    )


def require_source_1140_direct_removal(
    approval_file: Path | None,
    approval_quote: str | None,
    review_file: Path | None,
    task_id: str,
    sources: list[ScopedSource],
    contexts: list[ScopedSource],
    preparer: str,
    reviewer: str,
) -> None:
    if approval_file is None or approval_quote != SOURCE_1140_APPROVAL_QUOTE:
        raise RuntimeError("replacement-free removal requires the exact source-1140 approval")
    approval_arg = approval_file.expanduser()
    work_logs_root_arg = configured_work_logs_root()
    mail_root_arg = work_logs_root_arg / "manager_mail"
    if not approval_arg.is_absolute() or approval_arg.parent != mail_root_arg or approval_arg.name != SOURCE_1140_APPROVAL_FILE:
        raise RuntimeError("replacement-free removal requires the trusted source-1140 manager mail")
    try:
        work_logs_root_stat = work_logs_root_arg.lstat()
        mail_root_stat = mail_root_arg.lstat()
        work_logs_root = work_logs_root_arg.resolve(strict=True)
        mail_root = mail_root_arg.resolve(strict=True)
        approval_path, approval_bytes = read_owner_only_regular_file(approval_arg, "replacement-free removal approval file")
        approval_text = approval_bytes.decode("utf-8")
    except (OSError, RuntimeError, UnicodeDecodeError) as exc:
        raise RuntimeError("replacement-free removal approval evidence is unreadable") from exc
    if (
        stat.S_ISLNK(work_logs_root_stat.st_mode)
        or stat.S_ISLNK(mail_root_stat.st_mode)
        or not stat.S_ISDIR(mail_root_stat.st_mode)
        or mail_root_stat.st_uid != os.geteuid()
        or mail_root_stat.st_mode & 0o077
        or approval_arg.parent.resolve(strict=False) != mail_root
        or approval_path.parent != mail_root
        or hashlib.sha256(approval_bytes).hexdigest() != SOURCE_1140_APPROVAL_SHA256
        or SOURCE_1140_APPROVAL_QUOTE not in approval_text
        or work_logs_root != mail_root.parent
    ):
        raise RuntimeError("replacement-free removal approval evidence does not match source-1140")
    if review_file is None:
        raise RuntimeError("replacement-free removal requires exact independent review evidence")
    try:
        _review_path, review_bytes = read_owner_only_regular_file(review_file.expanduser(), "replacement-free removal review file")
        review_text = review_bytes.decode("utf-8")
    except (OSError, RuntimeError, UnicodeDecodeError) as exc:
        raise RuntimeError("replacement-free removal review evidence is unreadable") from exc
    rows = read_tsv_text(review_text, {"kind", "value"}, "replacement-free removal review file")
    values: dict[str, list[str]] = {}
    for row in rows:
        values.setdefault(row["kind"], []).append(row["value"])
    expected_single = {
        "version": ["v1.0.0"],
        "approval_sha256": [SOURCE_1140_APPROVAL_SHA256],
        "task_id": [task_id],
        "preparer": [preparer],
        "reviewer": [reviewer],
        "verdict": ["PASS"],
    }
    expected_sources = sorted(f"{source.uid}:{source.gmail_msgid}:{source.gmail_thrid}:{source.raw_sha256}" for source in sources)
    expected_contexts = sorted(f"{context.gmail_msgid}:{context.gmail_thrid}:{context.raw_sha256}" for context in contexts)
    if (
        set(values) != {*expected_single, "source", "context"}
        or any(values.get(kind) != expected for kind, expected in expected_single.items())
        or sorted(values.get("source", [])) != expected_sources
        or sorted(values.get("context", [])) != expected_contexts
        or not expected_sources
        or not expected_contexts
    ):
        raise RuntimeError("replacement-free removal review evidence does not match the exact operation")


def disposition_text(
    rows: list[dict[str, str]],
    batch_id: str,
    owner: str,
    moved_uids: set[str],
    reason_sha256: str,
    task_evidence_sha256: str,
    replacement: str,
    task_id: str,
    reviewer: str,
    exact_removal: ExactRemovalEvidence | None = None,
) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=(
            "batch_id",
            "owner",
            "reviewer",
            "gmail_thrid",
            "uid",
            "disposition",
            "reason_sha256",
            "task_evidence_sha256",
            "replacement",
            "task_id",
            "exact_removal_exception",
            "exact_removal_approval_sha256",
            "exact_removal_quote_sha256",
            "exact_removal_source_binding",
        ),
        delimiter="\t",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "batch_id": batch_id,
                "owner": owner,
                "reviewer": reviewer,
                "gmail_thrid": row["gmail_thrid"],
                "uid": row["uid"],
                "disposition": "trashed" if row["uid"] in moved_uids else "retained",
                "reason_sha256": reason_sha256,
                "task_evidence_sha256": task_evidence_sha256,
                "replacement": replacement,
                "task_id": task_id,
                "exact_removal_exception": exact_removal.exception if exact_removal is not None else "",
                "exact_removal_approval_sha256": exact_removal.approval_sha256 if exact_removal is not None else "",
                "exact_removal_quote_sha256": exact_removal.approval_quote_sha256 if exact_removal is not None else "",
                "exact_removal_source_binding": exact_removal.approval_source_binding if exact_removal is not None else "",
            }
        )
    return output.getvalue()


def prepare_thread_disposition(
    source_dir: Path,
    batch_id: str,
    owner: str,
    gmail_thrid: str,
    moved_uids: set[str],
    reason_file: Path,
    task_evidence_file: Path,
    replacement: str,
    task_id: str = "test-task",
    reviewer: str = "independent-reviewer",
    exact_removal: ExactRemovalEvidence | None = None,
) -> tuple[list[dict[str, str]], str, bool]:
    if not task_id or tsv_value(task_id) != task_id:
        raise RuntimeError("task identity must be one nonempty line")
    if not reviewer or tsv_value(reviewer) != reviewer or reviewer == owner:
        raise RuntimeError("reviewer must be a distinct nonempty one-line identity")
    rows = thread_batch_rows(source_dir, batch_id, owner, gmail_thrid)
    require_scoped_task(source_dir, rows, task_id)
    thread_uids = {row["uid"] for row in rows}
    thread_sources = export_source_map(source_dir, sorted(thread_uids))
    if {source["gmail_thrid"] for source in thread_sources.values()} != {gmail_thrid}:
        raise RuntimeError("batch thread does not match the fixed-start source map")
    if not moved_uids.issubset(thread_uids):
        raise RuntimeError("requested source is outside the claimed thread batch")
    evidence = disposition_text(
        rows,
        batch_id,
        owner,
        moved_uids,
        evidence_digest(reason_file, "reason"),
        evidence_digest(task_evidence_file, "task"),
        replacement,
        task_id,
        reviewer,
        exact_removal,
    )
    outcome_path = source_dir / "outcomes" / f"{gmail_thrid}.tsv"
    if outcome_path.exists():
        raise RuntimeError("thread already has a final disposition")
    intent_path = source_dir / "intents" / f"{gmail_thrid}.tsv"
    if not intent_path.exists():
        write_private_exclusive(intent_path, evidence)
        return rows, evidence, False
    try:
        existing = intent_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("could not read existing thread intent") from exc
    if existing == evidence:
        return rows, evidence, False
    if moved_uids:
        raise RuntimeError("thread already has a different mutation intent")
    return rows, evidence, True


def cmd_claim_batch(args: argparse.Namespace) -> int:
    claim_batch(args.source_dir, args.batch_id, args.owner)
    print(f"claimed batch={args.batch_id} owner={args.owner}")
    return 0


def cmd_retain_thread(args: argparse.Namespace) -> int:
    rows, evidence, recovery_needed = prepare_thread_disposition(
        args.source_dir,
        args.batch_id,
        args.owner,
        args.gmail_thrid,
        set(),
        args.reason_file,
        args.task_evidence_file,
        "not-required-retained",
        args.task_id,
        args.reviewer,
    )
    if recovery_needed:
        source_map = export_source_map(args.source_dir, [row["uid"] for row in rows])
        client, _config = open_mailbox(readonly=True)
        try:
            for source in source_map.values():
                uids = gmail_message_uids(client, source["gmail_msgid"])
                if len(uids) != 1:
                    raise RuntimeError("cannot recover intent because a source is absent or ambiguous in INBOX")
                record = fetch_record(client, uids[0], with_body=True, with_metadata=True)
                if record.gmail_msgid != source["gmail_msgid"] or record.gmail_thrid != source["gmail_thrid"]:
                    raise RuntimeError("cannot recover intent because source identity drifted")
        finally:
            logout_mailbox(client)
        write_private_exclusive(args.source_dir / "recoveries" / f"{args.gmail_thrid}.tsv", evidence)
    write_private_exclusive(args.source_dir / "outcomes" / f"{args.gmail_thrid}.tsv", evidence)
    print(f"retained_thread={args.gmail_thrid} source_count={len(rows)}")
    return 0


def intent_reconciliation_evidence(source_dir: Path, gmail_thrid: str) -> tuple[str, list[dict[str, str]], dict[str, dict[str, str]]]:
    if not gmail_thrid.isdecimal():
        raise RuntimeError("Gmail thread identity must be decimal")
    outcome_path = source_dir / "outcomes" / f"{gmail_thrid}.tsv"
    if outcome_path.exists():
        raise RuntimeError("thread already has a final disposition")
    intent_path = source_dir / "intents" / f"{gmail_thrid}.tsv"
    try:
        evidence = intent_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("thread has no readable immutable intent") from exc
    fields = {"batch_id", "owner", "reviewer", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement", "task_id"}
    rows = read_tsv_text(evidence, fields, intent_path.name)
    if not rows or any(
        row["gmail_thrid"] != gmail_thrid
        or row["disposition"] not in {"retained", "trashed"}
        or not re.fullmatch(r"[0-9a-f]{64}", row["reason_sha256"])
        or not re.fullmatch(r"[0-9a-f]{64}", row["task_evidence_sha256"])
        or not row["replacement"]
        or not row["task_id"]
        or not row["reviewer"]
        or row["reviewer"] == row["owner"]
        for row in rows
    ):
        raise RuntimeError("immutable intent is malformed or names a different thread")
    identities = {(row["batch_id"], row["owner"]) for row in rows}
    if len(identities) != 1:
        raise RuntimeError("immutable intent has ambiguous batch ownership")
    batch_id, owner = identities.pop()
    assigned = thread_batch_rows(source_dir, batch_id, owner, gmail_thrid)
    intended_uids = [row["uid"] for row in rows]
    if len(intended_uids) != len(set(intended_uids)) or set(intended_uids) != {row["uid"] for row in assigned}:
        raise RuntimeError("immutable intent does not cover the claimed fixed-start thread exactly once")
    source_map = export_source_map(source_dir, intended_uids)
    return evidence, rows, source_map


def is_source_815_exact_removal_row(row: dict[str, str]) -> bool:
    approved_uid, _separator, _rest = SOURCE_815_SOURCE_BINDING.partition(":")
    return (
        row.get("disposition", "") == "trashed"
        and row.get("uid", "") == approved_uid
        and row.get("replacement", "") == "not-required"
        and row.get("task_id", "") == SOURCE_815_TASK_ID
        and row.get("exact_removal_exception", "") == SOURCE_815_EXACT_REMOVAL_EXCEPTION
        and row.get("exact_removal_approval_sha256", "") == SOURCE_815_APPROVAL_SHA256
        and row.get("exact_removal_quote_sha256", "") == SOURCE_815_APPROVAL_QUOTE_SHA256
        and row.get("exact_removal_source_binding", "") == SOURCE_815_SOURCE_BINDING
    )


def is_source_815_exact_removal_intent(rows: list[dict[str, str]], source_map: dict[str, dict[str, str]]) -> bool:
    if len(rows) != 1 or not is_source_815_exact_removal_row(rows[0]):
        return False
    row = rows[0]
    source = source_map.get(row["uid"])
    if source is None:
        return False
    source_binding = f"{row['uid']}:{source['gmail_msgid']}:{source['gmail_thrid']}:{source['raw_sha256']}"
    return source_binding == row.get("exact_removal_source_binding", "")


def record_matches_reconciliation_location(record: MailRecord, source: dict[str, str], _location: str) -> bool:
    return (
        record.gmail_msgid == source["gmail_msgid"]
        and record.gmail_thrid == source["gmail_thrid"]
        and record.msgid_sha256 == source["msgid_sha256"]
        and record.raw_sha256 == source["raw_sha256"]
    )


def observe_reconciliation_locations(
    client: imaplib.IMAP4_SSL,
    source_map: dict[str, dict[str, str]],
) -> dict[str, dict[str, MailRecord]]:
    observed: dict[str, dict[str, MailRecord]] = {"INBOX": {}, "Trash": {}}
    for location, mailbox in (("INBOX", "INBOX"), ("Trash", TRASH_MAILBOX)):
        select_mailbox(client, mailbox, readonly=True)
        for uid, source in source_map.items():
            matches = gmail_message_uids(client, source["gmail_msgid"])
            if len(matches) > 1:
                raise RuntimeError(f"source is ambiguous in {location}")
            if matches:
                observed[location][uid] = fetch_record(client, matches[0], with_body=True, with_metadata=True)
    return observed


def require_reconciliation_locations(
    rows: list[dict[str, str]],
    source_map: dict[str, dict[str, str]],
    observed: dict[str, dict[str, MailRecord]],
    sender_email: str,
    recipient_email: str,
) -> None:
    for row in rows:
        uid = row["uid"]
        expected = "Trash" if row["disposition"] == "trashed" else "INBOX"
        other = "INBOX" if expected == "Trash" else "Trash"
        if uid not in observed[expected] or uid in observed[other]:
            raise RuntimeError("source is in both, neither, or the wrong reconciliation location")
        record = observed[expected][uid]
        if not is_manager_record(record, sender_email, recipient_email) or not record_matches_reconciliation_location(record, source_map[uid], expected):
            raise RuntimeError("source identity or content changed")


def frozen_thread_context(source_dir: Path, gmail_thrid: str) -> dict[str, dict[str, str]]:
    expected_rows = [
        row
        for row in read_literal_tsv(
            source_dir / "thread-context.tsv",
            {"gmail_thrid", "gmail_msgid", "msgid_sha256", "raw_sha256", "flags", "labels"},
        )
        if row["gmail_thrid"] == gmail_thrid
    ]
    expected = {row["gmail_msgid"]: row for row in expected_rows}
    if not expected_rows or len(expected) != len(expected_rows):
        raise RuntimeError("frozen thread evidence is missing or ambiguous")
    exact_records: list[MailRecord] = []
    required_headers = {
        "Gmail-Message-ID",
        "Gmail-Thread-ID",
        "Message-ID-SHA256",
        "Raw-SHA256",
        "Flags",
        "Labels",
    }
    for gmail_msgid, row in expected.items():
        context_path = source_dir / "threads" / f"{gmail_thrid}-{gmail_msgid}.txt"
        header_text = context_path.read_text(encoding="utf-8").split("\n\n", 1)[0]
        headers: dict[str, str] = {}
        for line in header_text.splitlines():
            name, separator, value = line.partition(": ")
            if name not in required_headers:
                continue
            if not separator or name in headers:
                raise RuntimeError("frozen thread evidence has malformed exported headers")
            headers[name] = value
        if set(headers) != required_headers:
            raise RuntimeError("frozen thread evidence has incomplete exported headers")
        if (
            headers["Gmail-Message-ID"] != gmail_msgid
            or headers["Gmail-Thread-ID"] != gmail_thrid
            or headers["Message-ID-SHA256"] != row["msgid_sha256"]
            or headers["Raw-SHA256"] != row["raw_sha256"]
            or tsv_value(headers["Flags"]) != row["flags"]
            or tsv_value(headers["Labels"]) != row["labels"]
        ):
            raise RuntimeError("frozen thread evidence files disagree")
        exact_records.append(
            MailRecord(
                uid="",
                date="",
                sender="",
                to="",
                subject="",
                msgid_sha256=headers["Message-ID-SHA256"],
                raw_sha256=headers["Raw-SHA256"],
                gmail_msgid=headers["Gmail-Message-ID"],
                gmail_thrid=headers["Gmail-Thread-ID"],
                flags=headers["Flags"],
                labels=headers["Labels"],
            )
        )
    frozen_digest = thread_context_digest(exact_records)
    digest_rows = [row for row in read_tsv(source_dir / "thread-digests.tsv", {"gmail_thrid", "thread_context_sha256"}) if row["gmail_thrid"] == gmail_thrid]
    manifest_digests = {
        row["thread_context_sha256"]
        for row in read_tsv(source_dir / "manifest.tsv", {"gmail_thrid", "thread_context_sha256"})
        if row["gmail_thrid"] == gmail_thrid
    }
    if len(digest_rows) != 1 or digest_rows[0]["thread_context_sha256"] != frozen_digest or manifest_digests != {frozen_digest}:
        raise RuntimeError("frozen thread evidence digest binding failed")
    return expected


def reconciliation_thread_unchanged(
    client: imaplib.IMAP4_SSL,
    all_mailbox: str,
    source_dir: Path,
    gmail_thrid: str,
    trash_records: list[MailRecord],
) -> bool:
    expected = frozen_thread_context(source_dir, gmail_thrid)
    if not expected:
        return False
    select_mailbox(client, all_mailbox, readonly=True)
    records = fetch_records(client, gmail_thread_uids(client, gmail_thrid), with_body=True, with_metadata=True)
    require_gmail_identities(records)
    all_msgids = {record.gmail_msgid for record in records}
    if any(record.gmail_msgid in all_msgids for record in trash_records):
        return False
    records.extend(trash_records)
    actual = {record.gmail_msgid: record for record in records}
    if len(actual) != len(records) or not set(expected).issubset(actual) or any(record.gmail_thrid != gmail_thrid for record in records):
        return False
    for gmail_msgid, row in expected.items():
        record = actual[gmail_msgid]
        if (
            record.gmail_thrid != gmail_thrid
            or record.msgid_sha256 != row["msgid_sha256"]
            or record.raw_sha256 != row["raw_sha256"]
        ):
            return False
    return True


def additive_recovery_thread_intact(
    client: imaplib.IMAP4_SSL,
    all_mailbox: str,
    source_dir: Path,
    gmail_thrid: str,
    trash_records: list[MailRecord],
) -> bool:
    expected = frozen_thread_context(source_dir, gmail_thrid)
    trashed_msgids = {record.gmail_msgid for record in trash_records}
    if not expected or len(trashed_msgids) != len(trash_records):
        return False
    select_mailbox(client, all_mailbox, readonly=True)
    records = fetch_records(client, gmail_thread_uids(client, gmail_thrid), with_body=True, with_metadata=True)
    require_gmail_identities(records)
    all_msgids = {record.gmail_msgid for record in records}
    if len(all_msgids) != len(records) or any(record.gmail_msgid in all_msgids for record in trash_records):
        return False
    records.extend(trash_records)
    actual = {record.gmail_msgid: record for record in records}
    if len(actual) != len(records) or not set(expected).issubset(actual) or not set(actual) - set(expected):
        return False
    for gmail_msgid, row in expected.items():
        record = actual[gmail_msgid]
        if (
            record.gmail_thrid != gmail_thrid
            or record.msgid_sha256 != row["msgid_sha256"]
            or record.raw_sha256 != row["raw_sha256"]
        ):
            return False
    return all(record.gmail_thrid == gmail_thrid for record in records)


def terminal_recovery_text(intent_rows: list[dict[str, str]]) -> str:
    fields = ("batch_id", "owner", "reviewer", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement", "task_id", "terminal_recovery")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in intent_rows:
        writer.writerow({**{field: row[field] for field in fields[:-1]}, "terminal_recovery": "skipped_already_trashed"})
    return output.getvalue()


def cmd_reconcile_intent(args: argparse.Namespace) -> int:
    evidence, rows, source_map = intent_reconciliation_evidence(args.source_dir, args.gmail_thrid)
    expected_mailboxes = export_mailboxes(args.source_dir)
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        validate_replacements_in_mailbox(client, expected_mailboxes[r"\All"], rows, sender_email, recipient_email)
        select_mailbox(client, "INBOX", readonly=True)
        expected_uidvalidities = {source["uidvalidity"] for source in source_map.values()}
        if len(expected_uidvalidities) != 1 or not next(iter(expected_uidvalidities)).isdecimal() or selected_uidvalidity(client) not in expected_uidvalidities:
            raise RuntimeError("INBOX UIDVALIDITY changed from the frozen source map")
        if not mailbox_exists(client, TRASH_MAILBOX):
            raise RuntimeError(f"mailbox is missing: {TRASH_MAILBOX}")
        if special_use_mailboxes(client).get(r"\All") != expected_mailboxes[r"\All"]:
            raise RuntimeError("All Mail mailbox identity changed from the frozen source map")
        observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, observed, sender_email, recipient_email)
        final_observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, final_observed, sender_email, recipient_email)
        final_trash_records = [final_observed["Trash"][row["uid"]] for row in rows if row["disposition"] == "trashed"]
        if is_source_815_exact_removal_intent(rows, source_map):
            if len(final_trash_records) != 1:
                raise RuntimeError("source-815 exact removal recovery did not verify the exact Trash source")
        elif not reconciliation_thread_unchanged(client, expected_mailboxes[r"\All"], args.source_dir, args.gmail_thrid, final_trash_records):
            raise RuntimeError("complete Gmail thread context changed")
    finally:
        logout_mailbox(client)
    write_private_exclusive(args.source_dir / "outcomes" / f"{args.gmail_thrid}.tsv", evidence)
    trashed = sum(row["disposition"] == "trashed" for row in rows)
    print(f"reconciled_thread={args.gmail_thrid} sources={len(rows)} retained={len(rows) - trashed} trashed={trashed} mailbox_mutations=0 permanent_deleted=0")
    return 0


def cmd_recover_already_trashed(args: argparse.Namespace) -> int:
    _evidence, rows, source_map = intent_reconciliation_evidence(args.source_dir, args.gmail_thrid)
    if any(row["disposition"] != "trashed" for row in rows):
        raise RuntimeError("terminal Trash recovery requires an all-trashed immutable intent")
    expected_mailboxes = export_mailboxes(args.source_dir)
    client, config = open_mailbox(readonly=True)
    try:
        sender_email, recipient_email = mail_boundary(config)
        validate_replacements_in_mailbox(client, expected_mailboxes[r"\All"], rows, sender_email, recipient_email)
        select_mailbox(client, "INBOX", readonly=True)
        expected_uidvalidities = {source["uidvalidity"] for source in source_map.values()}
        if len(expected_uidvalidities) != 1 or not next(iter(expected_uidvalidities)).isdecimal() or selected_uidvalidity(client) not in expected_uidvalidities:
            raise RuntimeError("INBOX UIDVALIDITY changed from the frozen source map")
        if not mailbox_exists(client, TRASH_MAILBOX):
            raise RuntimeError(f"mailbox is missing: {TRASH_MAILBOX}")
        if special_use_mailboxes(client).get(r"\All") != expected_mailboxes[r"\All"]:
            raise RuntimeError("All Mail mailbox identity changed from the frozen source map")
        observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, observed, sender_email, recipient_email)
        final_observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, final_observed, sender_email, recipient_email)
        trash_records = [final_observed["Trash"][row["uid"]] for row in rows]
        if not additive_recovery_thread_intact(client, expected_mailboxes[r"\All"], args.source_dir, args.gmail_thrid, trash_records):
            raise RuntimeError("frozen Gmail thread context changed or disappeared")
        receipt_observed = observe_reconciliation_locations(client, source_map)
        require_reconciliation_locations(rows, source_map, receipt_observed, sender_email, recipient_email)
    finally:
        logout_mailbox(client)
    receipt_path = args.source_dir / "recoveries" / f"{args.gmail_thrid}.skipped-already-trashed.tsv"
    write_private_exclusive(receipt_path, terminal_recovery_text(rows))
    print(f"recovered_thread={args.gmail_thrid} skipped_already_trashed={len(rows)} mailbox_mutations=0 permanent_deleted=0")
    return 0


def cmd_verify_run(args: argparse.Namespace) -> int:
    run_rows = read_tsv(args.source_dir / "run.tsv", {"fixed_start_utc", "source_count", "thread_count", "threads_per_batch"})
    if len(run_rows) != 1:
        raise RuntimeError("fixed-start run evidence must contain exactly one row")
    manifest = read_tsv(args.source_dir / "manifest.tsv", {"uid", "gmail_thrid", "gmail_msgid"})
    batches = batch_rows(args.source_dir)
    approved_tasks = scoped_task_map(args.source_dir)
    manifest_uids = [row["uid"] for row in manifest]
    batch_uids = [row["uid"] for row in batches]
    if len(manifest_uids) != len(set(manifest_uids)) or set(manifest_uids) != set(batch_uids):
        raise RuntimeError("fixed-start manifest and batch map do not match")
    threads = {row["gmail_thrid"] for row in manifest}
    run = run_rows[0]
    if run["source_count"] != str(len(manifest)) or run["thread_count"] != str(len(threads)) or not run["fixed_start_utc"]:
        raise RuntimeError("fixed-start run counts do not match the immutable manifest")
    missing_terminal_evidence = [
        gmail_thrid
        for gmail_thrid in sorted(threads)
        if not (args.source_dir / "outcomes" / f"{gmail_thrid}.tsv").exists()
        and not (args.source_dir / "recoveries" / f"{gmail_thrid}.skipped-already-trashed.tsv").exists()
    ]
    if missing_terminal_evidence:
        raise RuntimeError(
            f"fixed-start threads lack outcome or terminal recovery: count={len(missing_terminal_evidence)} threads={','.join(missing_terminal_evidence)}"
        )
    dispositions: list[dict[str, str]] = []
    skipped_already_trashed = 0
    skipped_already_trashed_threads = 0
    for gmail_thrid in sorted(threads):
        outcome_path = args.source_dir / "outcomes" / f"{gmail_thrid}.tsv"
        intent_path = args.source_dir / "intents" / f"{gmail_thrid}.tsv"
        try:
            intent = intent_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"fixed-start thread lacks immutable intent: {gmail_thrid}") from exc
        if outcome_path.exists():
            if (args.source_dir / "recoveries" / f"{gmail_thrid}.skipped-already-trashed.tsv").exists():
                raise RuntimeError(f"thread has both normal outcome and terminal recovery evidence: {gmail_thrid}")
            rows = read_tsv(
                outcome_path,
                {"batch_id", "owner", "reviewer", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement", "task_id"},
            )
            outcome = outcome_path.read_text(encoding="utf-8")
            if intent != outcome:
                try:
                    recovery = (args.source_dir / "recoveries" / f"{gmail_thrid}.tsv").read_text(encoding="utf-8")
                except OSError as exc:
                    raise RuntimeError(f"changed intent lacks recovery evidence for thread: {gmail_thrid}") from exc
                if recovery != outcome:
                    raise RuntimeError(f"recovery evidence does not match outcome for thread: {gmail_thrid}")
        else:
            recovery_path = args.source_dir / "recoveries" / f"{gmail_thrid}.skipped-already-trashed.tsv"
            try:
                recovery_text = recovery_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(f"fixed-start thread lacks outcome or terminal recovery: {gmail_thrid}") from exc
            rows = read_tsv_text(
                recovery_text,
                {"batch_id", "owner", "reviewer", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement", "task_id", "terminal_recovery"},
                recovery_path.name,
            )
            intent_rows = read_tsv_text(intent, {"batch_id", "owner", "reviewer", "gmail_thrid", "uid", "disposition", "reason_sha256", "task_evidence_sha256", "replacement", "task_id"}, intent_path.name)
            if recovery_text != terminal_recovery_text(intent_rows) or any(row["terminal_recovery"] != "skipped_already_trashed" or row["disposition"] != "trashed" for row in rows):
                raise RuntimeError(f"invalid terminal recovery evidence for thread: {gmail_thrid}")
            skipped_already_trashed += len(rows)
            skipped_already_trashed_threads += 1
        if approved_tasks and any(approved_tasks.get(row["uid"]) != row["task_id"] for row in rows):
            raise RuntimeError(f"terminal disposition regrouped an independently reviewed task: {gmail_thrid}")
        if any(row["gmail_thrid"] != gmail_thrid or row["disposition"] not in {"retained", "trashed"} for row in rows):
            raise RuntimeError(f"invalid disposition outcome for thread: {gmail_thrid}")
        dispositions.extend(rows)
    disposition_uids = [row["uid"] for row in dispositions]
    if len(disposition_uids) != len(set(disposition_uids)) or set(disposition_uids) != set(manifest_uids):
        raise RuntimeError("fixed-start sources are not each classified exactly once")
    disposition_source_map = export_source_map(args.source_dir, disposition_uids)
    for row in dispositions:
        if row.get("exact_removal_exception", "") and not is_source_815_exact_removal_intent([row], disposition_source_map):
            raise RuntimeError(f"terminal disposition has invalid exact-removal source binding: {row['gmail_thrid']}")
    expected_batch = {row["uid"]: (row["batch_id"], row["gmail_thrid"]) for row in batches}
    for row in dispositions:
        if expected_batch[row["uid"]] != (row["batch_id"], row["gmail_thrid"]):
            raise RuntimeError("disposition attempted cross-batch mutation")
        if not re.fullmatch(r"[0-9a-f]{64}", row["reason_sha256"]) or not re.fullmatch(r"[0-9a-f]{64}", row["task_evidence_sha256"]):
            raise RuntimeError("disposition lacks bound reason or task evidence")
        if not row["replacement"]:
            raise RuntimeError("disposition lacks replacement decision evidence")
        if not row["task_id"] or tsv_value(row["task_id"]) != row["task_id"]:
            raise RuntimeError("disposition lacks a valid task identity")
        if not row["reviewer"] or tsv_value(row["reviewer"]) != row["reviewer"] or row["reviewer"] == row["owner"]:
            raise RuntimeError("disposition lacks an independent reviewer identity")
        require_batch_owner(args.source_dir, row["batch_id"], row["owner"])
    validate_task_terminal_dispositions(dispositions)
    replacement_ids = {row["replacement"] for row in dispositions if row["replacement"] not in {"not-required", "not-required-retained"}}
    if replacement_ids:
        client, config = open_mailbox(readonly=True)
        try:
            sender_email, recipient_email = mail_boundary(config)
            all_mailbox = export_mailboxes(args.source_dir)[r"\All"]
            validate_replacements_in_mailbox(client, all_mailbox, dispositions, sender_email, recipient_email)
        finally:
            logout_mailbox(client)
    retained = sum(row["disposition"] == "retained" for row in dispositions)
    trashed = sum(row["disposition"] == "trashed" for row in dispositions)
    print(f"fixed_start_verified=1 sources={len(manifest)} threads={len(threads)} retained={retained} trashed={trashed - skipped_already_trashed} skipped_already_trashed={skipped_already_trashed} skipped_already_trashed_threads={skipped_already_trashed_threads} later_arrivals_included=0 live_full_scan=0 permanent_deleted=0")
    return 0


def validate_task_terminal_dispositions(dispositions: list[dict[str, str]]) -> None:
    by_task: dict[str, list[dict[str, str]]] = {}
    for row in dispositions:
        by_task.setdefault(row["task_id"], []).append(row)
    for task_id, rows in by_task.items():
        exact_removal_rows = [row for row in rows if row.get("exact_removal_exception", "")]
        if exact_removal_rows:
            if len(rows) != 1 or len(exact_removal_rows) != 1 or not is_source_815_exact_removal_row(rows[0]):
                raise RuntimeError(f"task has invalid exact-removal exception evidence: {task_id}")
            continue
        retained_uids = {row["uid"] for row in rows if row["disposition"] == "retained"}
        replacement_values = {row["replacement"] for row in rows if row["replacement"] not in {"not-required", "not-required-retained"}}
        if any(not re.fullmatch(r"<[^<>\s]+>", value) for value in replacement_values):
            raise RuntimeError(f"task has malformed replacement identity: {task_id}")
        replacement_ids = replacement_values
        if len(replacement_ids) > 1:
            raise RuntimeError(f"task has duplicate or conflicting replacement identities: {task_id}")
        if replacement_ids and any(row["replacement"] not in replacement_ids for row in rows):
            raise RuntimeError(f"task has originals not bound to its sole replacement identity: {task_id}")
        if replacement_ids and retained_uids:
            raise RuntimeError(f"task replacement does not supersede every fixed-start original: {task_id}")
        if not replacement_ids and len(retained_uids) != 1:
            raise RuntimeError(f"task must have exactly one retained manager message or one verified replacement: {task_id}")


def validate_replacements_in_mailbox(
    client: imaplib.IMAP4_SSL,
    all_mailbox: str,
    dispositions: list[dict[str, str]],
    sender_email: str,
    recipient_email: str,
) -> None:
    replacement_ids = {row["replacement"] for row in dispositions if row["replacement"] not in {"not-required", "not-required-retained"}}
    for replacement_id in replacement_ids:
        if not re.fullmatch(r"<[^<>\s]+>", replacement_id) or not replacement_exists(
            client, all_mailbox, replacement_id, sender_email, recipient_email, restore_readonly=True
        ):
            raise RuntimeError("replacement identity is malformed, missing, ambiguous, or has the wrong mail boundary")


def record_matches_source_map(record: MailRecord, source: dict[str, str]) -> bool:
    return (
        source["source_mailbox"] == "INBOX"
        and record.source_uidvalidity == source["uidvalidity"]
        and record.gmail_msgid == source["gmail_msgid"]
        and record.gmail_thrid == source["gmail_thrid"]
        and record.msgid_sha256 == source["msgid_sha256"]
        and record.raw_sha256 == source["raw_sha256"]
    )


def replacement_exists(
    client: imaplib.IMAP4_SSL,
    mailbox: str,
    replacement_id: str,
    sender_email: str,
    recipient_email: str,
    *,
    restore_readonly: bool = False,
) -> bool:
    if not re.fullmatch(r"<[^<>\s]+>", replacement_id):
        return False
    select_mailbox(client, mailbox, readonly=True)
    try:
        typ, data = imap_uid(client, "replacement-message-search", "search", None, "HEADER", "Message-ID", imap_quoted(replacement_id))
        if typ != "OK":
            return False
        uids = [raw.decode() for raw in data[0].split()] if data and data[0] else []
        if len(uids) != 1:
            return False
        msg, _digest = fetch_msg(client, uids[0], HEADER_FETCH)
        senders = [address.casefold() for _name, address in getaddresses(msg.get_all("From", [])) if address]
        recipients = [address.casefold() for _name, address in getaddresses(msg.get_all("To", [])) if address]
        return rfc_message_id(msg) == replacement_id and senders == [sender_email.casefold()] and recipient_email.casefold() in recipients
    finally:
        if not getattr(client, "_omo_operation_timed_out", False):
            select_mailbox(client, "INBOX", readonly=restore_readonly)


def replacement_gmail_msgid(client: imaplib.IMAP4_SSL, mailbox: str, replacement_id: str) -> str:
    """Resolve one already-validated replacement to its exact Gmail identity."""
    select_mailbox(client, mailbox, readonly=True)
    try:
        typ, data = imap_uid(client, "replacement-gmail-identity-search", "search", None, "HEADER", "Message-ID", imap_quoted(replacement_id))
        uids = [raw.decode() for raw in data[0].split()] if typ == "OK" and data and data[0] else []
        if len(uids) != 1:
            raise RuntimeError("replacement identity is missing or ambiguous")
        record = fetch_record(client, uids[0], with_body=False, with_metadata=True)
        require_gmail_identities([record])
        return record.gmail_msgid
    finally:
        if not getattr(client, "_omo_operation_timed_out", False):
            select_mailbox(client, "INBOX", readonly=False)


def replacement_subject(
    client: imaplib.IMAP4_SSL,
    mailbox: str,
    replacement_id: str,
    sender_email: str,
    recipient_email: str,
) -> str:
    """Fetch the subject of one exact replacement with its mail boundary."""
    select_mailbox(client, mailbox, readonly=True)
    try:
        typ, data = imap_uid(client, "replacement-subject-search", "search", None, "HEADER", "Message-ID", imap_quoted(replacement_id))
        uids = [raw.decode() for raw in data[0].split()] if typ == "OK" and data and data[0] else []
        if len(uids) != 1:
            raise RuntimeError("replacement identity is missing or ambiguous")
        msg, _digest = fetch_msg(client, uids[0], HEADER_FETCH)
        senders = [address.casefold() for _name, address in getaddresses(msg.get_all("From", [])) if address]
        recipients = [address.casefold() for _name, address in getaddresses(msg.get_all("To", [])) if address]
        if rfc_message_id(msg) != replacement_id or senders != [sender_email.casefold()] or recipient_email.casefold() not in recipients:
            raise RuntimeError("replacement identity has the wrong mail boundary")
        return str(msg.get("Subject", "")).replace("\n", " ")
    finally:
        if not getattr(client, "_omo_operation_timed_out", False):
            select_mailbox(client, "INBOX", readonly=False)


def replacement_supersedes_ids(client: imaplib.IMAP4_SSL, mailbox: str, replacement_id: str) -> set[str]:
    select_mailbox(client, mailbox, readonly=True)
    try:
        typ, data = imap_uid(client, "replacement-supersession-search", "search", None, "HEADER", "Message-ID", imap_quoted(replacement_id))
        uids = [raw.decode() for raw in data[0].split()] if typ == "OK" and data and data[0] else []
        if len(uids) != 1:
            raise RuntimeError("replacement identity is missing or ambiguous")
        msg, _digest = fetch_msg(client, uids[0], SUPERSESSION_HEADER_FETCH)
        values = {" ".join(str(value).split()) for value in msg.get_all("X-OMO-Supersedes", [])}
        if any(re.fullmatch(r"<[^<>\s]+>", value) is None for value in values):
            raise RuntimeError("replacement has a malformed supersession binding")
        return values
    finally:
        if not getattr(client, "_omo_operation_timed_out", False):
            select_mailbox(client, "INBOX", readonly=False)


def replacement_agent_session_id(client: imaplib.IMAP4_SSL, mailbox: str, replacement_id: str) -> str:
    select_mailbox(client, mailbox, readonly=True)
    try:
        typ, data = imap_uid(client, "replacement-agent-session-search", "search", None, "HEADER", "Message-ID", imap_quoted(replacement_id))
        uids = [raw.decode() for raw in data[0].split()] if typ == "OK" and data and data[0] else []
        if len(uids) != 1:
            raise RuntimeError("replacement identity is missing or ambiguous")
        msg, _digest = fetch_msg(client, uids[0], SUPERSESSION_HEADER_FETCH)
        return " ".join(str(msg.get("X-OMO-Agent-Session-ID", "")).split()).lower()
    finally:
        if not getattr(client, "_omo_operation_timed_out", False):
            select_mailbox(client, "INBOX", readonly=False)


def original_sender_targets_by_task(
    task_ids: list[str],
    task_sources: set[tuple[int, str]],
    source_records: list[MailRecord],
    context_records: list[MailRecord],
    replacement_gmail_ids: set[str],
) -> list[str]:
    """Derive one fail-closed original sender target for every task."""
    source_by_id = {record.gmail_msgid: record for record in source_records}
    targets: list[str] = []
    for task_index, task_id in enumerate(task_ids, 1):
        bound_sources = [source_by_id[gmail_msgid] for index, gmail_msgid in task_sources if index == task_index]
        thread_ids = {record.gmail_thrid for record in bound_sources}
        relevant = [record for record in context_records if record.gmail_thrid in thread_ids and record.gmail_msgid not in replacement_gmail_ids]
        found = [subject_tmux_target(record.subject) for record in relevant]
        unique = set(found)
        if not found or "" in unique:
            raise RuntimeError(f"task has missing original sender tmux target: {task_id}")
        if len(unique) != 1:
            raise RuntimeError(f"task has conflicting original sender tmux targets: {task_id}")
        targets.append(next(iter(unique)))
    return targets


def parse_route_resolutions(values: list[str], task_ids: list[str]) -> dict[str, str]:
    """Parse an optional complete reviewed task-to-target route transition."""
    if not values:
        return {}
    resolutions: dict[str, str] = {}
    for value in values:
        task_id, separator, raw_target = value.rpartition("=")
        if not separator or not task_id or tsv_value(task_id) != task_id or not TMUX_TARGET_RE.fullmatch(raw_target):
            raise ValueError("route-resolution must be TASK-ID=SESSION:WINDOW[.PANE]")
        if task_id in resolutions:
            raise ValueError("route-resolution contains a duplicate task identity")
        resolutions[task_id] = canonical_tmux_target(raw_target)
    if set(resolutions) != set(task_ids):
        raise ValueError("route-resolution must bind every exact task identity once")
    return resolutions


def run_export(args: argparse.Namespace, set_stage: Callable[[str], None]) -> int:
    out_dir = args.out_dir
    set_stage("prepare-output")
    ensure_empty_private_dir(out_dir)
    set_stage("open-readonly-mailbox")
    client, config = open_mailbox(readonly=True)
    try:
        set_stage("freeze-candidates")
        fixed_start_utc = datetime.now(timezone.utc).isoformat()
        sender_email, recipient_email = mail_boundary(config)
        candidate_uids = manager_candidate_uids(client, sender_email)
        scope_file = getattr(args, "scope_file", None)
        scope = load_reviewed_scope(scope_file) if scope_file is not None else None
        if scope is not None:
            requested = {source.uid for source in scope.sources}
            missing = requested - set(candidate_uids)
            if missing:
                raise RuntimeError(f"reviewed scope identity is absent at fixed start: count={len(missing)}")
            candidate_uids = [uid for uid in candidate_uids if uid in requested]
        header_records, skipped = accepted_manager_headers(client, candidate_uids, sender_email, recipient_email)
        if scope is not None and {record.uid for record in header_records} != {source.uid for source in scope.sources}:
            raise RuntimeError("reviewed scope contains a boundary-mismatched or ambiguous identity")
        uidvalidity = selected_uidvalidity(client)
        set_stage("fetch-fixed-start-sources")
        source_uids = [record.uid for record in header_records]
        fetched_source_records = fetch_full_records(client, source_uids, EXPORT_FULL_FETCH_ATTEMPTS)
        source_records = [replace(record, source_uidvalidity=uidvalidity) for record in fetched_source_records]
        if scope is not None:
            validate_scoped_records(scope, source_records)
        set_stage("fetch-thread-context")
        special_use, records_by_thread = fetch_imap_thread_contexts(client, source_records)
        all_mailbox = special_use.get(r"\All", "")
        sent_mailbox = special_use.get(r"\Sent", "")
        records = source_records
    finally:
        logout_mailbox(client)
    set_stage("persist-thread-context")
    thread_digests = write_thread_context(out_dir, records_by_thread, sender_email, recipient_email)
    set_stage("persist-source-records")
    for record in records:
        write_private(out_dir / f"{record.uid}.txt", export_body(record))
    set_stage("persist-run-evidence")
    write_private(out_dir / "mailboxes.tsv", f"role\tmailbox\nINBOX\tINBOX\n\\All\t{tsv_value(all_mailbox)}\n\\Sent\t{tsv_value(sent_mailbox)}\n")
    write_private(out_dir / "batches.tsv", export_batches(records, args.threads_per_batch))
    write_private(
        out_dir / "run.tsv",
        f"fixed_start_utc\tsource_count\tthread_count\tthreads_per_batch\n{fixed_start_utc}\t{len(records)}\t{len(records_by_thread)}\t{args.threads_per_batch}\n",
    )
    if scope is not None:
        scope_tasks = io.StringIO()
        writer = csv.DictWriter(scope_tasks, fieldnames=("uid", "task_id", "gmail_msgid", "gmail_thrid", "raw_sha256"), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for source in sorted(scope.sources, key=lambda value: int(value.uid)):
            writer.writerow({field: getattr(source, field) for field in writer.fieldnames})
        scope_tasks_text = scope_tasks.getvalue()
        scope_tasks_sha256 = hashlib.sha256(scope_tasks_text.encode()).hexdigest()
        write_private(out_dir / "scope-tasks.tsv", scope_tasks_text)
        write_private(
            out_dir / "scope.tsv",
            "scope_sha256\tscope_tasks_sha256\tpreparer\treviewer\tprovenance\n"
            f"{scope.sha256}\t{scope_tasks_sha256}\t{tsv_value(scope.preparer)}\t{tsv_value(scope.reviewer)}\t{tsv_value(scope.provenance)}\n",
        )
    else:
        scope_tasks_sha256 = ""
    write_private(out_dir / "uids.txt", "\n".join(record.uid for record in records) + ("\n" if records else ""))
    write_private_dir(out_dir / "claims")
    write_private_dir(out_dir / "intents")
    write_private_dir(out_dir / "outcomes")
    write_private_dir(out_dir / "recoveries")
    set_stage("publish-manifest")
    write_private(out_dir / "manifest.tsv", export_manifest(records, thread_digests, scope, scope_tasks_sha256))
    set_stage("report-success")
    suffix = f" skipped_boundary_mismatch={len(skipped)}" if skipped else ""
    print(f"exported={len(records)}{suffix} out_dir={out_dir}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    out_dir = args.out_dir.resolve(strict=False)
    args.out_dir = out_dir
    receipt_path = export_receipt_path(out_dir)
    if os.path.lexists(receipt_path):
        raise RuntimeError(f"terminal export receipt already exists: {receipt_path.name}")
    export_stage = "start"

    def set_stage(stage: str) -> None:
        nonlocal export_stage
        export_stage = stage

    try:
        result = run_export(args, set_stage)
    except BaseException as exc:
        category, stage, error = export_failure_diagnostics(exc, export_stage)
        write_export_receipt(out_dir, category, stage, error)
        raise
    write_export_receipt(out_dir, "success", "complete", "none")
    return result


def cmd_mark_seen(args: argparse.Namespace) -> int:
    print("mark-seen is retired for manager mail compression; use trash-superseded with an explicit superseded UID file", file=sys.stderr)
    return 2


def verify_post_move_imap(
    client: imaplib.IMAP4_SSL,
    source_map: dict[str, dict[str, str]],
    sender_email: str,
    recipient_email: str,
) -> PostMoveVerification:
    """Verify each moved fixed-start source through the same IMAP session.

    Exact lookup from the selected Trash mailbox proves Trash membership because
    Gmail may omit its ``\\Trash`` system label from ``X-GM-LABELS``. Message-ID
    lookup excludes arrivals that were not in the frozen source set.
    """
    sources_by_thread: dict[str, dict[str, dict[str, str]]] = {}
    for source in source_map.values():
        sources_by_thread.setdefault(source["gmail_thrid"], {})[source["gmail_msgid"]] = source
    select_mailbox(client, TRASH_MAILBOX, readonly=True)
    verified_messages = 0
    verified_threads = 0
    changed_threads = 0
    failures = 0
    verified_records: list[MailRecord] = []
    for gmail_thrid, sources_by_id in sources_by_thread.items():
        thread_verified = 0
        for gmail_msgid, source in sources_by_id.items():
            try:
                uids = gmail_message_uids(client, gmail_msgid)
                if len(uids) != 1:
                    raise RuntimeError("moved source was absent or ambiguous in Trash")
                record = fetch_record(client, uids[0], with_body=True, with_metadata=True)
                require_gmail_identities([record])
            except (imaplib.IMAP4.error, RuntimeError):
                failures += 1
                break
            if (
                not record_matches_reconciliation_location(record, source, "Trash")
                or not is_manager_record(record, sender_email, recipient_email)
            ):
                break
            thread_verified += 1
            verified_records.append(record)
        else:
            verified_messages += thread_verified
            verified_threads += 1
            continue
        changed_threads += 1
    return PostMoveVerification(
        same_mailbox=True,
        verified_message_count=verified_messages,
        verified_thread_count=verified_threads,
        changed_thread_count=changed_threads,
        imap_failure_count=failures,
        verified_records=tuple(verified_records),
    )


def verified_existing_trash_records(
    client: imaplib.IMAP4_SSL,
    source_map: dict[str, dict[str, str]],
    sender_email: str,
    recipient_email: str,
) -> list[MailRecord]:
    select_mailbox(client, TRASH_MAILBOX, readonly=True)
    records: list[MailRecord] = []
    try:
        for source in source_map.values():
            uids = gmail_message_uids(client, source["gmail_msgid"])
            if len(uids) != 1:
                raise RuntimeError("interrupted source was absent or ambiguous in Trash")
            record = fetch_record(client, uids[0], with_body=True, with_metadata=True)
            if not is_manager_record(record, sender_email, recipient_email) or not record_matches_reconciliation_location(record, source, "Trash"):
                raise RuntimeError("interrupted Trash source identity or content changed")
            records.append(record)
        return records
    finally:
        if not getattr(client, "_omo_operation_timed_out", False):
            select_mailbox(client, "INBOX", readonly=False)


def direct_context_intact(
    client: imaplib.IMAP4_SSL,
    all_mailbox: str,
    expected_sources: list[ScopedSource],
    *,
    allow_additive: bool,
    expected_nontrash_ids: set[str] | None = None,
) -> bool:
    """Revalidate an in-memory fixed-start thread under the selected additive policy."""
    if not expected_sources:
        return False
    gmail_thrids = {source.gmail_thrid for source in expected_sources}
    if len(gmail_thrids) != 1:
        return False
    gmail_thrid = next(iter(gmail_thrids))
    select_mailbox(client, TRASH_MAILBOX, readonly=True)
    trash_records = fetch_full_records(client, gmail_thread_uids(client, gmail_thrid))
    final_gate_event(f"context-trash:{gmail_thrid}")
    select_mailbox(client, all_mailbox, readonly=True)
    current = fetch_full_records(client, gmail_thread_uids(client, gmail_thrid))
    final_gate_event(f"context-all:{gmail_thrid}")
    require_gmail_identities([*current, *trash_records])
    current_ids = {record.gmail_msgid for record in current}
    trash_ids = {record.gmail_msgid for record in trash_records}
    if len(current_ids) != len(current) or len(trash_ids) != len(trash_records) or current_ids & trash_ids:
        return False
    if expected_nontrash_ids is not None and not expected_nontrash_ids.issubset(current_ids):
        return False
    actual = {record.gmail_msgid: record for record in [*current, *trash_records]}
    expected = {source.gmail_msgid: source for source in expected_sources}
    if len(expected) != len(expected_sources) or not set(expected).issubset(actual):
        return False
    if not allow_additive and set(expected) != set(actual):
        return False
    return all(
        source.gmail_thrid == gmail_thrid
        and actual[gmail_msgid].gmail_thrid == gmail_thrid
        and actual[gmail_msgid].raw_sha256 == source.raw_sha256
        for gmail_msgid, source in expected.items()
    ) and all(record.gmail_thrid == gmail_thrid for record in actual.values())


def direct_contexts_intact(
    client: imaplib.IMAP4_SSL,
    all_mailbox: str,
    expected_by_thread: dict[str, list[ScopedSource]],
    *,
    allow_additive: bool,
    expected_nontrash_by_thread: dict[str, set[str]] | None = None,
    observed: Callable[[str], None] | None = None,
) -> bool:
    """Revalidate all reviewed threads through one Trash read followed by one All Mail read."""
    if not expected_by_thread:
        return False
    observe = observed or final_gate_event
    thread_ids = list(expected_by_thread)
    select_mailbox(client, TRASH_MAILBOX, readonly=True)
    trash_records = fetch_full_records(client, gmail_thread_uids_union(client, thread_ids))
    observe("contexts-trash")
    select_mailbox(client, all_mailbox, readonly=True)
    current = fetch_full_records(client, gmail_thread_uids_union(client, thread_ids))
    observe("contexts-all")
    require_gmail_identities([*trash_records, *current])
    actual_records = [*trash_records, *current]
    actual_ids = [record.gmail_msgid for record in actual_records]
    if len(actual_ids) != len(set(actual_ids)) or any(record.gmail_thrid not in expected_by_thread for record in actual_records):
        return False
    actual_by_thread = {
        thread_id: {record.gmail_msgid: record for record in actual_records if record.gmail_thrid == thread_id}
        for thread_id in expected_by_thread
    }
    current_ids_by_thread = {
        thread_id: {record.gmail_msgid for record in current if record.gmail_thrid == thread_id}
        for thread_id in expected_by_thread
    }
    for thread_id, expected_sources in expected_by_thread.items():
        expected = {source.gmail_msgid: source for source in expected_sources}
        actual = actual_by_thread[thread_id]
        if len(expected) != len(expected_sources) or not set(expected).issubset(actual):
            return False
        if not allow_additive and set(expected) != set(actual):
            return False
        if expected_nontrash_by_thread is not None and not expected_nontrash_by_thread.get(thread_id, set()).issubset(current_ids_by_thread[thread_id]):
            return False
        if any(
            source.gmail_thrid != thread_id
            or actual[gmail_msgid].gmail_thrid != thread_id
            or actual[gmail_msgid].raw_sha256 != source.raw_sha256
            for gmail_msgid, source in expected.items()
        ):
            return False
    return True


def retained_replacements_intact(
    client: imaplib.IMAP4_SSL,
    expected: list[RetainedReplacement],
) -> bool:
    """Authenticate reviewed retained replacements in the selected INBOX."""
    if (
        not expected
        or len({item.uid for item in expected}) != len(expected)
        or len({item.gmail_msgid for item in expected}) != len(expected)
    ):
        return False
    expected_by_uid = {item.uid: item for item in expected}
    if set(inbox_subset(client, list(expected_by_uid))) != set(expected_by_uid):
        return False
    records = fetch_records(client, list(expected_by_uid), with_body=True, with_metadata=True)
    if (
        len(records) != len(expected_by_uid)
        or {record.uid for record in records} != set(expected_by_uid)
        or {record.gmail_msgid for record in records} != {item.gmail_msgid for item in expected}
    ):
        return False
    return all(
        record.gmail_msgid == expected_by_uid[record.uid].gmail_msgid
        and record.gmail_thrid == expected_by_uid[record.uid].gmail_thrid
        and record.raw_sha256 == expected_by_uid[record.uid].raw_sha256
        and record.body_bytes == expected_by_uid[record.uid].body_bytes
        and hashlib.sha256(record.body.encode()).hexdigest() == expected_by_uid[record.uid].body_sha256
        for record in records
    )


def final_inbox_bindings_intact(
    client: imaplib.IMAP4_SSL,
    expected_uidvalidity: str,
    sources: list[ScopedSource],
    retained: list[RetainedReplacement],
    sender_email: str,
    recipient_email: str,
    observed: Callable[[str], None] | None = None,
) -> bool:
    """Authenticate source and retained Inbox bindings in the last pre-MOVE fetch."""
    expected_uids = [item.uid for item in [*sources, *retained]]
    if not expected_uids or len(expected_uids) != len(set(expected_uids)):
        return False
    select_mailbox(client, "INBOX", readonly=True)
    if selected_uidvalidity(client) != expected_uidvalidity:
        return False
    records = fetch_final_gate_records(client, expected_uids)
    if len(records) != len(expected_uids) or {record.uid for record in records} != set(expected_uids):
        return False
    records_by_uid = {record.uid: record for record in records}
    sources_by_uid = {item.uid: item for item in sources}
    retained_by_uid = {item.uid: item for item in retained}
    sources_intact = all(
        is_manager_record(record, sender_email, recipient_email)
        and (
            record.gmail_msgid,
            record.gmail_thrid,
            record.raw_sha256,
        )
        == (
            sources_by_uid[record.uid].gmail_msgid,
            sources_by_uid[record.uid].gmail_thrid,
            sources_by_uid[record.uid].raw_sha256,
        )
        for record in (records_by_uid[uid] for uid in sources_by_uid)
    )
    retained_intact = all(
        (
            record.gmail_msgid,
            record.gmail_thrid,
            record.raw_sha256,
            record.body_bytes,
            hashlib.sha256(record.body.encode()).hexdigest(),
        )
        == (
            retained_by_uid[record.uid].gmail_msgid,
            retained_by_uid[record.uid].gmail_thrid,
            retained_by_uid[record.uid].raw_sha256,
            retained_by_uid[record.uid].body_bytes,
            retained_by_uid[record.uid].body_sha256,
        )
        for record in (records_by_uid[uid] for uid in retained_by_uid)
    )
    (observed or final_gate_event)("inbox-bindings")
    return sources_intact and retained_intact


def observe_explicit_sources(
    client: imaplib.IMAP4_SSL,
    sources: list[ScopedSource],
    sender_email: str,
    recipient_email: str,
) -> tuple[list[MailRecord], list[MailRecord]]:
    """Resolve exact bound sources in either INBOX or recoverable Trash."""
    observed: dict[str, list[MailRecord]] = {"INBOX": [], "Trash": []}
    for location, mailbox in (("INBOX", "INBOX"), ("Trash", TRASH_MAILBOX)):
        select_mailbox(client, mailbox, readonly=True)
        for source in sources:
            matches = gmail_message_uids(client, source.gmail_msgid)
            if len(matches) > 1:
                raise RuntimeError(f"explicit source is ambiguous in {location}")
            if not matches:
                continue
            record = fetch_record(client, matches[0], with_body=True, with_metadata=True)
            if (
                (location == "INBOX" and record.uid != source.uid)
                or
                not is_manager_record(record, sender_email, recipient_email)
                or (record.gmail_msgid, record.gmail_thrid, record.raw_sha256)
                != (source.gmail_msgid, source.gmail_thrid, source.raw_sha256)
            ):
                raise RuntimeError("explicit source identity, content, or boundary changed")
            observed[location].append(record)
    inbox_ids = {record.gmail_msgid for record in observed["INBOX"]}
    trash_ids = {record.gmail_msgid for record in observed["Trash"]}
    expected_ids = {source.gmail_msgid for source in sources}
    if inbox_ids & trash_ids or inbox_ids | trash_ids != expected_ids:
        raise RuntimeError("explicit source is in both, neither, or an unknown mailbox")
    return observed["INBOX"], observed["Trash"]


def strict_fresh_source_locations_intact(
    sources: list[ScopedSource],
    inbox_records: list[MailRecord],
    trash_records: list[MailRecord],
) -> bool:
    """Require every exact bound source, and no other record, in INBOX."""
    expected = {(source.uid, source.gmail_msgid) for source in sources}
    actual = {(record.uid, record.gmail_msgid) for record in inbox_records}
    return bool(expected) and not trash_records and len(inbox_records) == len(expected) and actual == expected


def cmd_trash_explicit(args: argparse.Namespace) -> int:
    """Validate the immutable runtime before entering the mailbox mutation path."""
    runtime_bundle_digest = getattr(args, "runtime_bundle_sha256", "")
    if not isinstance(runtime_bundle_digest, str) or not runtime_bundle_digest:
        print("refusing trash-explicit without --runtime-bundle-sha256", file=sys.stderr)
        return 2
    try:
        validate_runtime_bundle(runtime_bundle_digest)
    except ValueError as exc:
        print(f"refusing trash-explicit: {exc}", file=sys.stderr)
        return 2
    if not args.yes:
        print("refusing to move superseded mail to Trash without --yes", file=sys.stderr)
        return 2
    mutation_complete = False
    pre_move_complete = False
    move_attempted = 0
    move_outcome = "not-attempted"
    move_error = ""
    post_move_verification_error = ""
    post_move_reconciliation_ran = False
    post_move_reconciled = False
    trash_stage = "parse-arguments"
    pre_move_failure_summarized = False
    task_ids: list[str] = []
    replacement_ids: list[str] = []
    retained_replacements: list[RetainedReplacement] = []
    sources: list[ScopedSource] = []
    source_location_mode = "unvalidated"
    allow_additive_final_context = bool(getattr(args, "allow_additive_final_context", False))
    final_context_mode = "additive-compatible" if allow_additive_final_context else "strict"
    final_gate_passed = False
    final_gate = FinalGateState()
    final_inbox: list[MailRecord] = []
    verified_inbox: list[MailRecord] = []
    verified_trash: list[MailRecord] = []
    final_context_client: imaplib.IMAP4_SSL | None = None
    active_client: imaplib.IMAP4_SSL | None = None

    def set_trash_stage(stage: str) -> None:
        nonlocal trash_stage
        trash_stage = stage

    def refuse_trash(reason: str) -> int:
        print(reason, file=sys.stderr)
        print(
            trash_explicit_summary(
                task_ids,
                replacement_ids,
                len(retained_replacements),
                sources,
                runtime_bundle_digest,
                source_location_mode,
                final_context_mode,
                final_gate_passed,
                final_gate.receipt,
                move_attempted,
                final_inbox,
                verified_inbox,
                verified_trash,
                move_outcome,
                summary_token(trash_stage),
                post_move_reconciliation_ran,
                post_move_reconciled,
            )
        )
        return 1

    try:
        task_ids = args.task_id if isinstance(args.task_id, list) else [args.task_id]
        replacement_ids = args.replacement_id if isinstance(args.replacement_id, list) else ([args.replacement_id] if args.replacement_id else [])
        replacement_not_required = bool(getattr(args, "replacement_not_required", False))
        raw_source_location_mode = getattr(args, "source_location_mode", "strict-fresh")
        if raw_source_location_mode not in {"strict-fresh", "recover-partial-move"}:
            raise ValueError("choose exactly one source-location mode: --strict-fresh or --recover-partial-move")
        source_location_mode = raw_source_location_mode
        raw_task_sources = getattr(args, "task_source", [])
        task_source_values = raw_task_sources if isinstance(raw_task_sources, list) else [raw_task_sources]
        task_identity = ",".join(task_ids)
        sources = [replace(parse_explicit_source(value), task_id=task_identity) for value in args.source]
        contexts = [parse_explicit_context(value) for value in args.context]
        raw_retained_replacements = getattr(args, "retained_replacement", None)
        retained_replacements = [parse_retained_replacement(value) for value in raw_retained_replacements or []]
        if not sources:
            raise ValueError("at least one explicit source is required")
        if len({source.uid for source in sources}) != len(sources) or len({source.gmail_msgid for source in sources}) != len(sources):
            raise ValueError("explicit sources contain duplicate or ambiguous identities")
        if (
            not task_ids
            or (not replacement_not_required and len(task_ids) != len(replacement_ids))
            or (replacement_not_required and (replacement_ids or len(task_ids) != 1))
            or len(set(task_ids)) != len(task_ids)
            or any(not task_id or tsv_value(task_id) != task_id for task_id in task_ids)
        ):
            raise ValueError("one unique nonempty task identity is required per replacement")
        if len(set(replacement_ids)) != len(replacement_ids):
            raise ValueError("replacement identities must be unique")
        if not replacement_not_required and len(retained_replacements) != len(replacement_ids):
            raise ValueError("one reviewed retained replacement binding is required per replacement")
        if replacement_not_required and retained_replacements:
            raise ValueError("replacement-free removal does not accept a retained replacement binding")
        if replacement_not_required:
            require_source_1140_direct_removal(
                getattr(args, "human_approval_file", None),
                getattr(args, "human_approval_quote", None),
                getattr(args, "independent_review_file", None),
                task_ids[0],
                sources,
                contexts,
                args.preparer,
                args.reviewer,
            )
        elif (
            getattr(args, "human_approval_file", None) is not None
            or getattr(args, "human_approval_quote", None)
            or getattr(args, "independent_review_file", None) is not None
        ):
            raise ValueError("human approval evidence requires --replacement-not-required")
        task_sources: set[tuple[int, str]] = set()
        for value in task_source_values:
            fields = value.split(":")
            if len(fields) != 2 or not fields[0].isdecimal() or not fields[1].isdecimal():
                raise ValueError("task-source binding must be TASK-INDEX:GMAIL-MSGID")
            task_sources.add((int(fields[0]), fields[1]))
        source_ids = {source.gmail_msgid for source in sources}
        expected_task_indexes = set(range(1, len(task_ids) + 1))
        if (
            {task_index for task_index, _gmail_msgid in task_sources} != expected_task_indexes
            or {gmail_msgid for _task_index, gmail_msgid in task_sources} != source_ids
            or any(gmail_msgid not in source_ids for _task_index, gmail_msgid in task_sources)
        ):
            raise ValueError("task-source bindings must cover every task and source exactly within this operation")
        raw_route_resolutions = getattr(args, "route_resolution", [])
        route_resolution_values = raw_route_resolutions if isinstance(raw_route_resolutions, list) else [raw_route_resolutions]
        route_resolutions = parse_route_resolutions(route_resolution_values, task_ids) if not replacement_not_required else {}
        if replacement_not_required and route_resolution_values:
            raise ValueError("replacement-free removal does not accept a replacement route")
        if (
            not args.preparer
            or not args.reviewer
            or args.preparer.strip() != args.preparer
            or args.reviewer.strip() != args.reviewer
            or args.preparer == args.reviewer
        ):
            raise ValueError("distinct nonempty preparer and reviewer identities are required")
        if not contexts or {source.gmail_thrid for source in sources} - {context.gmail_thrid for context in contexts}:
            raise ValueError("explicit context must cover every source thread")
        context_ids = {context.gmail_msgid for context in contexts}
        if len(context_ids) != len(contexts) or any(source.gmail_msgid not in context_ids for source in sources):
            raise ValueError("explicit context must contain every source identity exactly once")
        if any(not re.fullmatch(r"<[^<>\s]+>", replacement_id) for replacement_id in replacement_ids):
            raise ValueError("each replacement identity must be a syntactically valid Message-ID")
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    def disarm_pre_move_timer() -> None:
        return None

    client: imaplib.IMAP4_SSL | None = None

    def set_pending_client(pending_client: imaplib.IMAP4_SSL) -> None:
        nonlocal active_client
        active_client = pending_client

    try:
        set_trash_stage("arm-pre-move-timer")
        disarm_pre_move_timer = arm_trash_explicit_pre_move_timer(lambda: trash_stage, lambda: active_client)
        set_trash_stage("open-mailbox")
        client, config = open_mailbox(readonly=False, connected=set_pending_client)
        active_client = client
        set_trash_stage("mail-boundary")
        sender_email, recipient_email = mail_boundary(config)
        set_trash_stage("select-inbox")
        select_mailbox(client, "INBOX", readonly=False)
        set_trash_stage("selected-uidvalidity")
        expected_uidvalidity = selected_uidvalidity(client)
        if expected_uidvalidity != getattr(args, "source_uidvalidity", ""):
            return refuse_trash("refusing because inspected INBOX UIDVALIDITY changed")
        set_trash_stage("trash-mailbox-exists")
        if not mailbox_exists(client, TRASH_MAILBOX):
            return refuse_trash(f"refusing because mailbox is missing: {TRASH_MAILBOX}")
        set_trash_stage("special-use-mailboxes")
        special_use = special_use_mailboxes(client)
        if not special_use.get(r"\All") or not special_use.get(r"\Sent"):
            return refuse_trash("refusing because Gmail special-use mailboxes are missing")
        set_trash_stage("observe-explicit-sources")
        inbox_records, trash_records = observe_explicit_sources(client, sources, sender_email, recipient_email)
        if source_location_mode == "strict-fresh" and not strict_fresh_source_locations_intact(sources, inbox_records, trash_records):
            return refuse_trash("refusing because strict-fresh requires every bound source to remain in INBOX")
        set_trash_stage("replacement-exists")
        if any(
            not replacement_exists(client, special_use[r"\All"], replacement_id, sender_email, recipient_email)
            for replacement_id in replacement_ids
        ):
            return refuse_trash("refusing because a recorded replacement was not found in the recipient mailbox")
        set_trash_stage("replacement-gmail-identity")
        replacement_gmail_ids = [
            replacement_gmail_msgid(client, special_use[r"\All"], replacement_id)
            for replacement_id in replacement_ids
        ]
        if len(set(replacement_gmail_ids)) != len(replacement_gmail_ids):
            return refuse_trash("refusing because replacement identities resolve to the same message")
        if set(replacement_gmail_ids) & {source.gmail_msgid for source in sources}:
            return refuse_trash("refusing because a replacement is one of the superseded sources")
        set_trash_stage("fetch-direct-thread-context")
        context_records_by_thread = fetch_direct_thread_contexts(client, [*inbox_records, *trash_records])
        bound_context_records = [
            record
            for records in context_records_by_thread.values()
            for record in records
            if record.gmail_msgid in context_ids
        ]
        try:
            set_trash_stage("derive-original-targets")
            original_targets = [] if replacement_not_required else (
                [route_resolutions[task_id] for task_id in task_ids]
                if route_resolutions
                else original_sender_targets_by_task(
                    task_ids,
                    task_sources,
                    [*inbox_records, *trash_records],
                    bound_context_records,
                    set(replacement_gmail_ids),
                )
            )
            set_trash_stage("replacement-subject")
            replacement_subjects = [
                replacement_subject(client, special_use[r"\All"], replacement_id, sender_email, recipient_email)
                for replacement_id in replacement_ids
            ]
        except ImapOperationError:
            raise
        except RuntimeError as exc:
            return refuse_trash(f"refusing because {exc}")
        set_trash_stage("special-use-mailboxes-recheck")
        if replacement_ids and [subject_tmux_target(subject) for subject in replacement_subjects] != original_targets:
            return refuse_trash("refusing because a replacement does not preserve its original sender tmux target")
        if special_use_mailboxes(client) != special_use:
            return refuse_trash("refusing because special-use mailbox identity changed")
        set_trash_stage("select-inbox-recheck")
        select_mailbox(client, "INBOX", readonly=False)
        set_trash_stage("uidvalidity-recheck")
        if selected_uidvalidity(client) != expected_uidvalidity:
            return refuse_trash("refusing because INBOX UIDVALIDITY changed")
        contexts_by_thread: dict[str, list[ScopedSource]] = {}
        for context in contexts:
            contexts_by_thread.setdefault(context.gmail_thrid, []).append(context)
        set_trash_stage("direct-context-intact")
        if any(
            not direct_context_intact(client, special_use[r"\All"], records, allow_additive=False)
            for records in contexts_by_thread.values()
        ):
            return refuse_trash("refusing because complete Gmail thread context changed")
        set_trash_stage("select-inbox-before-subset")
        select_mailbox(client, "INBOX", readonly=False)
        inbox_uids = [record.uid for record in inbox_records]
        set_trash_stage("inbox-subset-recheck")
        if set(inbox_subset(client, inbox_uids)) != set(inbox_uids):
            return refuse_trash("refusing because a planned source moved during revalidation")
        set_trash_stage("observe-explicit-sources-final")
        final_inbox, final_trash = observe_explicit_sources(client, sources, sender_email, recipient_email)
        if {record.gmail_msgid for record in final_inbox} != {record.gmail_msgid for record in inbox_records}:
            return refuse_trash("refusing because a planned source moved immediately before mutation")
        if source_location_mode == "strict-fresh" and not strict_fresh_source_locations_intact(sources, final_inbox, final_trash):
            return refuse_trash("refusing because strict-fresh source location changed immediately before mutation")
        set_trash_stage("replacement-exists-final")
        if any(
            not replacement_exists(client, special_use[r"\All"], replacement_id, sender_email, recipient_email)
            for replacement_id in replacement_ids
        ):
            return refuse_trash("refusing because a replacement changed immediately before move")
        set_trash_stage("replacement-gmail-identity-final")
        if [
            replacement_gmail_msgid(client, special_use[r"\All"], replacement_id)
            for replacement_id in replacement_ids
        ] != replacement_gmail_ids:
            return refuse_trash("refusing because a replacement identity changed immediately before move")
        try:
            set_trash_stage("replacement-subject-final")
            final_replacement_subjects = [
                replacement_subject(client, special_use[r"\All"], replacement_id, sender_email, recipient_email)
                for replacement_id in replacement_ids
            ]
        except ImapOperationError:
            raise
        except RuntimeError as exc:
            return refuse_trash(f"refusing because {exc}")
        if final_replacement_subjects != replacement_subjects or (replacement_ids and [subject_tmux_target(subject) for subject in final_replacement_subjects] != original_targets):
            return refuse_trash("refusing because a replacement sender tmux target changed immediately before move")
        set_trash_stage("select-inbox-mutation-gate")
        select_mailbox(client, "INBOX", readonly=False)
        set_trash_stage("uidvalidity-mutation-gate")
        if selected_uidvalidity(client) != expected_uidvalidity:
            return refuse_trash("refusing because INBOX UIDVALIDITY changed immediately before move")
        set_trash_stage("inbox-subset-mutation-gate")
        final_inbox_uids = [record.uid for record in final_inbox]
        if set(inbox_subset(client, final_inbox_uids)) != set(final_inbox_uids):
            return refuse_trash("refusing because a planned source left INBOX at the mutation gate")
        set_trash_stage("open-final-context-mailbox")
        final_context_client, final_context_config = open_mailbox(readonly=True, connected=set_pending_client)
        set_trash_stage("final-context-boundary")
        if mail_boundary(final_context_config) != (sender_email, recipient_email):
            return refuse_trash("refusing because the final context mailbox boundary changed")
        final_gate.observed("boundary")
        set_trash_stage("final-context-uidvalidity")
        select_mailbox(final_context_client, "INBOX", readonly=True)
        if selected_uidvalidity(final_context_client) != expected_uidvalidity:
            return refuse_trash("refusing because INBOX UIDVALIDITY changed at the final context gate")
        final_gate.observed("uidvalidity")
        set_trash_stage("final-context-special-use")
        if special_use_mailboxes(final_context_client) != special_use:
            return refuse_trash("refusing because special-use mailbox identity changed at the final context gate")
        final_gate.observed("special-use")
        if retained_replacements and {item.gmail_msgid for item in retained_replacements} != set(replacement_gmail_ids):
            return refuse_trash("refusing because a reviewed retained replacement identity does not match the replacement")
        final_inbox_ids = {record.gmail_msgid for record in final_inbox}
        expected_final_inbox_sources = sources if source_location_mode == "strict-fresh" else [
            source for source in sources if source.gmail_msgid in final_inbox_ids
        ]
        final_inbox_ids_by_thread: dict[str, set[str]] = {}
        for source in expected_final_inbox_sources:
            final_inbox_ids_by_thread.setdefault(source.gmail_thrid, set()).add(source.gmail_msgid)
        set_trash_stage("direct-context-intact-final")
        if not direct_contexts_intact(
            final_context_client,
            special_use[r"\All"],
            contexts_by_thread,
            allow_additive=allow_additive_final_context,
            expected_nontrash_by_thread=final_inbox_ids_by_thread,
            observed=final_gate.observed,
        ):
            return refuse_trash("refusing because complete Gmail thread context changed immediately before move")
        set_trash_stage("inbox-bindings-final")
        if (expected_final_inbox_sources or retained_replacements) and not final_inbox_bindings_intact(
            final_context_client,
            expected_uidvalidity,
            expected_final_inbox_sources,
            retained_replacements,
            sender_email,
            recipient_email,
            observed=final_gate.observed,
        ):
            return refuse_trash("refusing because a source or retained replacement Inbox binding changed at the final mutation gate")
        final_gate_passed = True
        active_client = client
        disarm_pre_move_timer()
        pre_move_complete = True
        if final_inbox:
            move_attempted = len(final_inbox)
            try:
                typ, _data = imap_uid(
                    client,
                    "move-explicit-sources-to-trash",
                    "MOVE",
                    ",".join(record.uid for record in final_inbox),
                    imap_quoted(TRASH_MAILBOX),
                )
            except ImapOperationError as exc:
                mutation_complete = True
                move_outcome = "unknown"
                verified_inbox = []
                verified_trash = []
                move_error = summary_token(exc.stage)
            else:
                move_outcome = "ok"
                if typ != "OK":
                    move_outcome = "failed"
                    move_error = summary_token(f"move-explicit-sources-to-trash:{typ}")
        else:
            move_outcome = "not-needed"
        mutation_complete = True
        post_move_reconciliation_ran = True
        reconciliation_errors: list[str] = []
        try:
            verified_inbox, verified_trash = observe_explicit_sources(final_context_client, sources, sender_email, recipient_email)
        except (OSError, imaplib.IMAP4.error, RuntimeError) as exc:
            verified_inbox = []
            verified_trash = []
            reconciliation_errors.append(exc.stage if isinstance(exc, ImapOperationError) else f"source:{exc.__class__.__name__}:{exc}")
        if retained_replacements:
            try:
                select_mailbox(final_context_client, "INBOX", readonly=True)
                if selected_uidvalidity(final_context_client) != expected_uidvalidity or not retained_replacements_intact(final_context_client, retained_replacements):
                    raise RuntimeError("post-MOVE retained replacement reconciliation failed")
            except (OSError, imaplib.IMAP4.error, RuntimeError) as exc:
                reconciliation_errors.append(exc.stage if isinstance(exc, ImapOperationError) else f"retained:{exc.__class__.__name__}:{exc}")
        try:
            if not direct_contexts_intact(
                final_context_client,
                special_use[r"\All"],
                contexts_by_thread,
                allow_additive=allow_additive_final_context,
            ):
                raise RuntimeError("post-MOVE complete context reconciliation failed")
        except (OSError, imaplib.IMAP4.error, RuntimeError) as exc:
            reconciliation_errors.append(exc.stage if isinstance(exc, ImapOperationError) else f"context:{exc.__class__.__name__}:{exc}")
        post_move_reconciled = not reconciliation_errors
        post_move_verification_error = summary_token("+".join(filter(None, [move_error, *reconciliation_errors]))) if move_error or reconciliation_errors else ""
    except (OSError, RuntimeError, imaplib.IMAP4.error) as exc:
        pre_move_failure_summarized = True
        if pre_move_complete:
            raise
        if client is not None and isinstance(exc, ImapOperationError) and "pre-move timed out" in str(exc):
            abort_imap_client(client)
        stage = exc.stage if isinstance(exc, ImapOperationError) else trash_stage
        post_move_verification_error = summary_token(stage)
        print(
            trash_explicit_summary(
                task_ids,
                replacement_ids,
                len(retained_replacements),
                sources,
                runtime_bundle_digest,
                source_location_mode,
                final_context_mode,
                final_gate_passed,
                final_gate.receipt,
                move_attempted,
                final_inbox,
                verified_inbox,
                verified_trash,
                move_outcome,
                post_move_verification_error,
                post_move_reconciliation_ran,
                post_move_reconciled,
            )
        )
        return 1
    finally:
        if not pre_move_complete:
            disarm_pre_move_timer()
        try:
            if final_context_client is not None:
                logout_mailbox(final_context_client)
            if client is not None:
                logout_mailbox(client)
        except (imaplib.IMAP4.error, RuntimeError) as exc:
            if not mutation_complete and not pre_move_failure_summarized:
                raise
            if not post_move_verification_error:
                post_move_verification_error = summary_token(exc.stage if isinstance(exc, ImapOperationError) else f"logout:{exc.__class__.__name__}:{exc}")
    post_move_verified = not post_move_verification_error and not verified_inbox and len(verified_trash) == len(sources)
    print(
        trash_explicit_summary(
            task_ids,
            replacement_ids,
            len(retained_replacements),
            sources,
            runtime_bundle_digest,
            source_location_mode,
            final_context_mode,
            final_gate_passed,
            final_gate.receipt,
            move_attempted,
            final_inbox,
            verified_inbox,
            verified_trash,
            move_outcome,
            post_move_verification_error,
            post_move_reconciliation_ran,
            post_move_reconciled,
        )
    )
    return 0 if post_move_verified else 1


def cmd_trash_superseded(args: argparse.Namespace) -> int:
    if not args.yes:
        print("refusing to move superseded mail to Trash without --yes", file=sys.stderr)
        return 2
    if args.uid_file is None:
        print("refusing without a private superseded UID file beside its source map", file=sys.stderr)
        return 2
    if args.uids.strip():
        print("refusing inline UIDs; trash-superseded uses only the reviewed private --uid-file", file=sys.stderr)
        return 2
    try:
        requested = parse_uids(args.uids, args.uid_file)
        if not requested:
            raise RuntimeError("superseded source list must not be empty")
        source_dir = args.source_dir
        if args.uid_file.parent.resolve() != source_dir.resolve():
            raise RuntimeError("superseded UID file must be inside the fixed-start source directory")
        source_map = export_source_map(source_dir, requested)
        if {source["gmail_thrid"] for source in source_map.values()} != {args.gmail_thrid}:
            raise RuntimeError("one Trash operation must contain sources from exactly one claimed thread")
        expected_mailboxes = export_mailboxes(source_dir)
        expected_uidvalidities = {source["uidvalidity"] for source in source_map.values()}
        if len(expected_uidvalidities) != 1 or not next(iter(expected_uidvalidities)).isdecimal():
            raise RuntimeError("private source map has ambiguous UIDVALIDITY")
        expected_uidvalidity = next(iter(expected_uidvalidities))
        if bool(args.replacement_id) == bool(args.replacement_not_required):
            raise RuntimeError("record exactly one replacement identity or --replacement-not-required")
        replacement = args.replacement_id if args.replacement_id else "not-required"
        if tsv_value(replacement) != replacement:
            raise RuntimeError("replacement identity must be one line")
        if args.replacement_id and not re.fullmatch(r"<[^<>\s]+>", args.replacement_id):
            raise RuntimeError("replacement identity must be a syntactically valid Message-ID")
        human_approved_exact_removal = bool(getattr(args, "human_approved_exact_removal", False))
        exact_removal = None
        if human_approved_exact_removal:
            exact_removal = require_human_approved_exact_removal(
                source_dir,
                requested,
                args.task_id,
                args.replacement_not_required,
                args.reason_file,
                getattr(args, "human_approval_file", None),
                getattr(args, "human_approval_quote", None),
            )
        elif getattr(args, "human_approval_file", None) is not None or getattr(args, "human_approval_quote", None):
            raise RuntimeError("human approval evidence requires --human-approved-exact-removal")
        _thread_rows, outcome_evidence, recovery_needed = prepare_thread_disposition(
            source_dir,
            args.batch_id,
            args.owner,
            args.gmail_thrid,
            set(requested),
            args.reason_file,
            args.task_evidence_file,
            replacement,
            args.task_id,
            args.reviewer,
            exact_removal,
        )
        if recovery_needed:
            raise RuntimeError("Trash disposition cannot replace a different existing intent")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    client, config = open_mailbox(readonly=False)
    try:
        sender_email, recipient_email = mail_boundary(config)
        if selected_uidvalidity(client) != expected_uidvalidity:
            print("refusing because INBOX UIDVALIDITY changed", file=sys.stderr)
            return 1
        if not mailbox_exists(client, TRASH_MAILBOX):
            print(f"refusing because mailbox is missing: {TRASH_MAILBOX}", file=sys.stderr)
            return 1
        current_mailboxes = special_use_mailboxes(client)
        if current_mailboxes.get(r"\All") != expected_mailboxes[r"\All"] or current_mailboxes.get(r"\Sent") != expected_mailboxes[r"\Sent"]:
            print("refusing because special-use mailbox identity changed", file=sys.stderr)
            return 1
        if args.replacement_id and not replacement_exists(
            client,
            expected_mailboxes[r"\All"],
            args.replacement_id,
            sender_email,
            recipient_email,
        ):
            print("refusing because the recorded replacement was not found in the recipient mailbox", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        already_trashed = set(requested) - set(still_in_inbox)
        try:
            existing_trash_records = verified_existing_trash_records(
                client,
                {uid: source_map[uid] for uid in requested if uid in already_trashed},
                sender_email,
                recipient_email,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        records = [replace(record, source_uidvalidity=expected_uidvalidity) for record in fetch_records(client, still_in_inbox, with_body=True, with_metadata=True)]
        require_gmail_identities(records)
        if any(not is_manager_record(record, sender_email, recipient_email) for record in records):
            print("refusing boundary mismatch", file=sys.stderr)
            return 1
        if any(not record_matches_source_map(record, source_map[record.uid]) for record in records):
            print("refusing because source identity or content changed", file=sys.stderr)
            return 1
        thread_unchanged = True if human_approved_exact_removal else reconciliation_thread_unchanged(
            client,
            expected_mailboxes[r"\All"],
            source_dir,
            args.gmail_thrid,
            existing_trash_records,
        )
        select_mailbox(client, "INBOX", readonly=False)
        if not thread_unchanged:
            print("refusing because complete Gmail thread context changed", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        if set(still_in_inbox) != set(requested) - already_trashed:
            print("refusing because a planned source changed during revalidation", file=sys.stderr)
            return 1
        final_records = [replace(record, source_uidvalidity=expected_uidvalidity) for record in fetch_records(client, still_in_inbox, with_body=True, with_metadata=True)]
        require_gmail_identities(final_records)
        if any(not is_manager_record(record, sender_email, recipient_email) or not record_matches_source_map(record, source_map[record.uid]) for record in final_records):
            print("refusing because a planned source changed immediately before move", file=sys.stderr)
            return 1
        if existing_trash_records:
            try:
                existing_trash_records = verified_existing_trash_records(
                    client,
                    {uid: source_map[uid] for uid in requested if uid in already_trashed},
                    sender_email,
                    recipient_email,
                )
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        thread_unchanged = True if human_approved_exact_removal else reconciliation_thread_unchanged(
            client,
            expected_mailboxes[r"\All"],
            source_dir,
            args.gmail_thrid,
            existing_trash_records,
        )
        select_mailbox(client, "INBOX", readonly=False)
        if not thread_unchanged:
            print("refusing because complete Gmail thread context changed immediately before move", file=sys.stderr)
            return 1
        still_in_inbox = inbox_subset(client, requested)
        if set(still_in_inbox) != set(requested) - already_trashed:
            print("refusing because a planned source left INBOX immediately before move", file=sys.stderr)
            return 1
        if still_in_inbox:
            typ, _data = imap_uid(client, "move-reviewed-sources-to-trash", "MOVE", ",".join(still_in_inbox), imap_quoted(TRASH_MAILBOX))
            if typ != "OK":
                print(f"IMAP MOVE failed: {typ}", file=sys.stderr)
                return 1
        remaining = inbox_subset(client, requested)
        post_move = verify_post_move_imap(
            client,
            source_map,
            sender_email,
            recipient_email,
        )
        if remaining or post_move.verified_message_count != len(requested) or not post_move.complete:
            post_thread_unchanged = False
        else:
            post_thread_unchanged = True if human_approved_exact_removal else reconciliation_thread_unchanged(
                client,
                expected_mailboxes[r"\All"],
                source_dir,
                args.gmail_thrid,
                list(post_move.verified_records),
            )
    finally:
        logout_mailbox(client)
    if not remaining and post_move.verified_message_count == len(requested) and post_move.complete and post_thread_unchanged:
        write_private_exclusive(source_dir / "outcomes" / f"{args.gmail_thrid}.tsv", outcome_evidence)
    print(
        f"trash_superseded: requested={len(requested)} moved={len(still_in_inbox)}"
        f" already_not_in_inbox={len(requested) - len(still_in_inbox)} verify_remaining={len(remaining)}"
        f" verify_trash_count={post_move.verified_message_count}"
        f" verify_thread_count={post_move.verified_thread_count}"
        f" verify_changed_thread_count={post_move.changed_thread_count}"
        f" verify_imap_failure_count={post_move.imap_failure_count}"
        f" same_mailbox_after_move={int(post_move.same_mailbox)} permanent_deleted=0"
    )
    if remaining or post_move.verified_message_count != len(requested) or not post_move.complete or not post_thread_unchanged:
        print(f"remaining_inbox_count={len(remaining)}")
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    sub = arg_parser.add_subparsers(dest="cmd", required=True)
    runtime_bundle = sub.add_parser("build-runtime-bundle", help="Build one immutable zipapp containing the complete local Python import closure without mailbox access.")
    runtime_bundle.add_argument("--out", type=Path, required=True, help="New absolute .pyz path inside an existing or newly created owner-only directory.")
    runtime_bundle.set_defaults(func=cmd_build_runtime_bundle)
    identity_preflight = sub.add_parser("identity-preflight", help="Read-only aggregate Gmail identity preflight for manager mail.")
    identity_preflight.set_defaults(func=cmd_identity_preflight)
    snapshot = sub.add_parser("snapshot", help="Print manager mail headers and UIDs.")
    snapshot.set_defaults(func=cmd_snapshot)
    unread_summary = sub.add_parser("unread-summary", help="Print read-only JSON summaries of unread manager-human chains.")
    unread_summary.add_argument("--max-threads", type=int, default=20, help="Maximum unread chains to print; 1..100.")
    unread_summary.add_argument("--max-body-chars", type=int, default=1200, help="Maximum read-now text per chain; 80..5000.")
    unread_summary.add_argument("--max-messages-per-thread", type=int, default=20, help="Maximum unread messages to fetch per chain; 1..50.")
    unread_summary.set_defaults(func=cmd_unread_summary)
    agent_unread = sub.add_parser("agent-unread", help="List unread agent-to-human mail sent by the current tmux agent.")
    agent_unread.set_defaults(func=cmd_agent_unread)
    agent_trash = sub.add_parser(
        "agent-trash-replaced",
        help="Move the current agent's still-unread mail to recoverable Trash after a verified replacement exists.",
        description="""Remove a report you previously sent to the human. Only revoke mail the human has not read: run agent-unread and select only messages it marks trashable.
Send the replacement before moving the old unread message, using email_me.py --supersedes-message-id for each selected message_id and retaining the replacement Message-ID.
The replacement must retain everything the human absolutely needs now; omitting obsolete information is allowed and encouraged.
The source and replacement must preserve the current exact tmux sender and Codex session; legacy or prior-session mail is refused.
This command moves the old message only from Inbox to recoverable Gmail Trash and never expunges it. A repeated identical command reconciles an interrupted move; mixed or missing Inbox/Trash outcomes require manual inspection. Do not schedule this cleanup.""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    agent_trash.add_argument("--uid", action="append", required=True, help="Unread Inbox UID printed by agent-unread; repeat for multiple messages.")
    agent_trash.add_argument("--source-uidvalidity", required=True, help="Exact UIDVALIDITY printed by agent-unread.")
    agent_trash.add_argument("--replacement-message-id", required=True, help="Message-ID printed by email_me.py after sending the replacement.")
    agent_trash.add_argument("--yes", action="store_true")
    agent_trash.set_defaults(func=cmd_agent_trash_replaced)
    inspect_explicit = sub.add_parser("inspect-explicit", help="Print exact live bindings and bodies for selected manager UIDs without creating evidence files.")
    inspect_explicit.add_argument("--uids", required=True, help="Comma or whitespace separated current INBOX UIDs.")
    inspect_explicit.add_argument("--task-id", required=True, help="Task identity assigned to every selected source.")
    inspect_explicit.set_defaults(func=cmd_inspect_explicit)
    locate_replacement = sub.add_parser("locate-replacement", help="Find the unique exact current manager-mail subject and print its RFC Message-ID.")
    locate_replacement.add_argument("--subject", required=True, help="Exact current subject, including any manager prefix.")
    locate_replacement.set_defaults(func=cmd_locate_replacement)
    export = sub.add_parser("export", help="Export manager mail bodies into a private local directory.")
    export.add_argument("--out-dir", type=Path, required=True)
    export.add_argument("--threads-per-batch", type=int, default=DEFAULT_THREADS_PER_BATCH)
    export.add_argument("--scope-file", type=Path, help="Private independently reviewed v1.0.0 TSV limiting this new fixed-start export to exact current identities.")
    export.set_defaults(func=cmd_export)
    claim = sub.add_parser("claim-batch", help="Atomically claim one fixed-start review batch.")
    claim.add_argument("--source-dir", type=Path, required=True)
    claim.add_argument("--batch-id", required=True)
    claim.add_argument("--owner", required=True)
    claim.set_defaults(func=cmd_claim_batch)
    retain = sub.add_parser("retain-thread", help="Record one claimed thread as retained without mailbox mutation.")
    retain.add_argument("--source-dir", type=Path, required=True)
    retain.add_argument("--batch-id", required=True)
    retain.add_argument("--owner", required=True)
    retain.add_argument("--gmail-thread-id", dest="gmail_thrid", required=True)
    retain.add_argument("--reason-file", type=Path, required=True)
    retain.add_argument("--task-evidence-file", type=Path, required=True)
    retain.add_argument("--task-id", required=True, help="Immutable task identity; terminal verification permits exactly one useful manager message per task.")
    retain.add_argument("--reviewer", required=True, help="Independent reviewer identity; must differ from --owner.")
    retain.set_defaults(func=cmd_retain_thread)
    reconcile = sub.add_parser("reconcile-intent", help="Read-only reconciliation of one interrupted immutable intent in INBOX or Trash.")
    reconcile.add_argument("--source-dir", type=Path, required=True)
    reconcile.add_argument("--gmail-thread-id", dest="gmail_thrid", required=True)
    reconcile.set_defaults(func=cmd_reconcile_intent)
    recover_trashed = sub.add_parser("recover-already-trashed", help="Read-only terminal recovery when every frozen intent source is already exact in Trash and only later thread context was added.")
    recover_trashed.add_argument("--source-dir", type=Path, required=True)
    recover_trashed.add_argument("--gmail-thread-id", dest="gmail_thrid", required=True)
    recover_trashed.set_defaults(func=cmd_recover_already_trashed)
    verify = sub.add_parser("verify-run", help="Reconcile final outcomes against only the immutable fixed-start set.")
    verify.add_argument("--source-dir", type=Path, required=True)
    verify.set_defaults(func=cmd_verify_run)
    mark_seen = sub.add_parser("mark-seen", help="Retired; use trash-superseded for manager mail compression.")
    mark_seen.add_argument("--uids", default="", help="Comma or whitespace separated UID list.")
    mark_seen.add_argument("--uid-file", type=Path)
    mark_seen.add_argument("--yes", action="store_true")
    mark_seen.set_defaults(func=cmd_mark_seen)
    trash = sub.add_parser("trash-superseded", help="Move an explicit superseded manager UID set from INBOX to Trash after replacement summaries are sent.")
    trash.add_argument("--uids", default="", help="Retired for this command; inline UIDs are refused and --uid-file is authoritative.")
    trash.add_argument("--uid-file", type=Path)
    trash.add_argument("--source-dir", type=Path, required=True)
    trash.add_argument("--batch-id", required=True)
    trash.add_argument("--owner", required=True)
    trash.add_argument("--gmail-thread-id", dest="gmail_thrid", required=True)
    trash.add_argument("--reason-file", type=Path, required=True)
    trash.add_argument("--task-evidence-file", type=Path, required=True)
    trash.add_argument("--task-id", required=True, help="Immutable task identity shared by every fixed-start message for the task.")
    trash.add_argument("--reviewer", required=True, help="Independent reviewer identity; must differ from --owner.")
    trash.add_argument("--human-approved-exact-removal", action="store_true", help="Move one reviewed scoped message without full-thread supersession proof after explicit Human approval.")
    trash.add_argument("--human-approval-file", type=Path, help="Absolute manager_mail approval file required with --human-approved-exact-removal.")
    trash.add_argument("--human-approval-quote", help="Exact one-line Human quote required with --human-approved-exact-removal.")
    trash.add_argument("--yes", action="store_true")
    replacement = trash.add_mutually_exclusive_group(required=True)
    replacement.add_argument("--replacement-message-id", dest="replacement_id", default="")
    replacement.add_argument("--replacement-not-required", action="store_true")
    trash.set_defaults(func=cmd_trash_superseded)
    direct = sub.add_parser(
        "trash-explicit",
        help="Move live-bound superseded manager sources to recoverable Trash without an evidence directory.",
        description=(
            "Run a sequential, non-atomic final read gate, MOVE only exact bound sources, then reconcile immediately. "
            "A nonzero race remains after each observation; receipts report final_gate_atomic=0."
        ),
    )
    direct.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="UID:GMAIL-MSGID:GMAIL-THRID:RAW-SHA256",
        help="Exact current source binding; repeat once per superseded message.",
    )
    direct.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="GMAIL-MSGID:GMAIL-THRID:RAW-SHA256",
        help="Exact frozen thread context binding; repeat for every member present when execution starts. The final pre-MOVE gate rejects additions by default.",
    )
    source_location = direct.add_mutually_exclusive_group(required=True)
    source_location.add_argument(
        "--strict-fresh",
        action="store_const",
        dest="source_location_mode",
        const="strict-fresh",
        help="Fresh-operation mode: require every bound source UID to remain in INBOX through the final combined source-and-retained fetch; any prior or concurrent movement aborts before MOVE.",
    )
    source_location.add_argument(
        "--recover-partial-move",
        action="store_const",
        dest="source_location_mode",
        const="recover-partial-move",
        help="Explicit interrupted-operation recovery mode: reconcile exact bound sources already in Trash and MOVE only the remaining bound INBOX subset. Never use for a fresh reviewed manifest.",
    )
    direct.add_argument(
        "--allow-additive-final-context",
        action="store_true",
        help="Compatibility mode: permit only newly added thread members at the final pre-MOVE gate; removals and identity or content drift still abort.",
    )
    direct.add_argument(
        "--retained-replacement",
        action="append",
        default=[],
        metavar="UID:GMAIL-MSGID:GMAIL-THRID:RAW-SHA256:BODY-BYTES:BODY-SHA256",
        help="Required reviewed retained Inbox binding; repeat once per --replacement-message-id. Omit only with --replacement-not-required. The final gate authenticates UIDVALIDITY separately.",
    )
    direct.add_argument(
        "--runtime-bundle-sha256",
        required=True,
        help="Exact SHA-256 printed by build-runtime-bundle. trash-explicit refuses direct source-tree execution and requires that reviewed immutable .pyz runtime.",
    )
    direct.add_argument(
        "--replacement-message-id",
        action="append",
        dest="replacement_id",
        default=[],
        help="Verified replacement Message-ID; repeat once per task when splitting multi-task sources.",
    )
    direct.add_argument(
        "--replacement-not-required",
        action="store_true",
        help="Move one independently reviewed task with no retained agent mail under exact Human Source-1140 approval.",
    )
    direct.add_argument("--human-approval-file", type=Path, help="Exact owner-only Source-1140 manager-mail file.")
    direct.add_argument("--human-approval-quote", help="Exact Source-1140 approval sentence.")
    direct.add_argument("--independent-review-file", type=Path, help="Owner-only TSV binding the exact PASS-reviewed operation.")
    direct.add_argument(
        "--task-id",
        action="append",
        required=True,
        help="Explicit task identity; repeat in replacement order when splitting multi-task sources.",
    )
    direct.add_argument(
        "--task-source",
        action="append",
        required=True,
        metavar="TASK-INDEX:GMAIL-MSGID",
        help="Bind one 1-based task/replacement position to one source Gmail identity; repeat for shared or multi-source tasks.",
    )
    direct.add_argument(
        "--route-resolution",
        action="append",
        default=[],
        metavar="TASK-ID=SESSION:WINDOW[.PANE]",
        help="Independently reviewed authoritative current sender target for a verified route transition; when used, repeat exactly once for every task.",
    )
    direct.add_argument("--source-uidvalidity", required=True, help="Exact INBOX UIDVALIDITY printed by inspect-explicit.")
    direct.add_argument("--preparer", required=True, help="Identity that prepared the task grouping and replacement decision.")
    direct.add_argument("--reviewer", required=True, help="Distinct identity that independently reviewed the task grouping and replacement decision.")
    direct.add_argument("--yes", action="store_true")
    direct.set_defaults(func=cmd_trash_explicit)
    return arg_parser


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, RuntimeError, imaplib.IMAP4.error) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
