#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
# pyright: basic
"""Reconcile two stopped worker records into one prepared distinct-target worker.

Installation is intentionally insufficient to activate this helper.  Every
transaction authenticates the exact inactive instruction document and a
separate Human-approval record that binds those instruction bytes.
"""

from __future__ import annotations

import argparse
import base64
import imaplib
import json
import os
import pwd
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path
from typing import NoReturn

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.email_idle_watcher import (
    exact_human_sender,
    fetch_gmail_metadata,
    mailbox_state_identity,
    message_text,
    normalize_human_subject,
)
from omo_manager.omo_email_config import GMAIL_IMAP_HOST, AgentMailSettings, configured_agent_mail
from omo_manager.omo_hees_final_artifact_replace import active_owners, has_pending_marker
from omo_manager.omo_manager_replace import (
    Snapshot,
    canonical_target,
    create_snapshot,
    digest,
    markdown_paths,
    metadata,
    read_snapshot,
    replace_snapshot,
    replace_v1_fields,
    target_session,
    task_path,
)
from omo_manager.omo_manager_rotate import process_is_under, read_processes
from omo_manager.omo_task_edit import render_pending_items
from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1, TaskMetadata, parse_task_metadata
from omo_manager.omo_task_status import authoritative_active_target_task_paths, root_membership_lock, update_frontmatter_status
from omo_manager.omo_worker_successor import (
    minimal_launch_environment,
    minimal_tmux_environment,
    pinned_shell_identity,
    pinned_tmux_identity,
    protected_digest,
    queue_digest,
    read_frozen_prompt,
    read_launch_manifest,
    read_pinned_system_executable,
)

VERSION = "v1.0.0"
OPERATION = "dual-worker-distinct-successor"
LAUNCH_SCHEMA = "dual-worker-distinct-successor-launch-v1"
BLOCKER = "prepared dual-record successor awaiting exact digest-bound distinct-target launch"
WITHDRAWN_BLOCKER = "launch authority withdrawn after process creation; reconciliation required"
UNKNOWN_AUTHORITY_BLOCKER = "launch authority became ambiguous after process creation; reconciliation required"
APPROVAL_QUOTE = "I approve this exact instruction text."
WITHDRAWAL_QUOTE = "I withdraw this exact instruction text."
AUTHENTICATED_APPROVAL_SCHEMA = "dual-worker-successor-authenticated-gmail-approval/v1"
APPROVAL_AGENT_MAILBOX = "sichangheagent@gmail.com"
APPROVAL_HUMAN_MAILBOX = "stevensichanghe@gmail.com"
APPROVAL_SUBJECT = "Approve exact AMH dual-worker successor procedure"
WITHDRAWAL_SUBJECT = "Withdraw exact AMH dual-worker successor procedure"
COMMIT_SUBJECT = "Commit exact AMH dual-worker successor launch"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_RE = re.compile(r"^[A-Za-z0-9_./-]+\.md$")
JOURNAL_RE = re.compile(r"^\.omo-dual-successor-[0-9a-f]{16,64}\.transaction$")
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
MAX_RECORD_BYTES = 32 * 1024 * 1024
PREPARE_PHASES = ("prepared", "shadow", "canonical", "todo", "successor", "committed")
LAUNCH_PHASES = (
    "reserved",
    "process",
    "task",
    "authority-pending",
    "authority",
    "authority-blocked",
    "terminated",
    "withdrawn",
    "committed",
)
LAUNCH_CRASH_POINTS = ("reserved", "process", "task", "authority-pending", "authority-committed", "committed")
WITHDRAWAL_CRASH_POINTS = ("authority-blocked", "terminated", "withdrawn")
AUTHORITY_REF_RE = re.compile(r"^(?:[0-9]{6}/)?manager_mail/[A-Za-z0-9_.-]+\.txt$")


class DualSuccessorError(RuntimeError):
    """The dual-record transaction cannot prove its exact safe state."""


class AuthorityBlocked(DualSuccessorError):
    """A durable authoritative withdrawal or ambiguous controlling message won."""

    def __init__(self, evidence: AuthorityEvidence) -> None:
        super().__init__(f"launch authority is {evidence.outcome}; reconciliation is required")
        self.evidence: AuthorityEvidence = evidence


@dataclass(frozen=True)
class PrepareArgs:
    root: Path
    shadow_task: str
    canonical_task: str
    successor_task: str
    old_target: str
    new_target: str
    manager_target: str
    shadow_sha256: str
    canonical_sha256: str
    todo_sha256: str
    expected_pending_items: tuple[str, ...]
    queue_sha256: str
    prompt_file: Path
    prompt_sha256: str
    launch_manifest: Path
    launch_manifest_sha256: str
    instructions_file: Path
    instructions_sha256: str
    approval_file: Path
    approval_sha256: str
    protected_targets: tuple[str, ...]
    protected_sha256: str
    custody_sha256: str
    journal: Path


@dataclass(frozen=True)
class PreparePlan:
    shadow: Snapshot
    canonical: Snapshot
    todo: Snapshot
    prompt: Snapshot
    manifest: Snapshot
    instructions: Snapshot
    approval: Snapshot
    successor_path: Path
    shadow_after: bytes
    canonical_after: bytes
    todo_after: bytes
    successor_data: bytes
    initial_markdown_paths: tuple[Path, ...]


@dataclass(frozen=True)
class PreparedBinding:
    journal: Snapshot
    prepare_args: PrepareArgs
    root: Path
    shadow_path: Path
    canonical_path: Path
    successor_path: Path
    todo_path: Path
    old_target: str
    new_target: str
    manager_target: str
    queue: tuple[str, ...]
    queue_sha256: str
    prompt_path: Path
    prompt_data: bytes
    prompt_sha256: str
    manifest_path: Path
    manifest_data: bytes
    manifest_sha256: str
    manifest: dict[str, object]
    instructions_path: Path
    instructions_data: bytes
    instructions_sha256: str
    approval_path: Path
    approval_data: bytes
    approval_sha256: str
    authority_binding: AuthorityBinding
    protected_targets: tuple[str, ...]
    shadow_after: bytes
    canonical_after: bytes
    todo_after: bytes
    successor_data: bytes


@dataclass(frozen=True)
class Pane:
    target: str
    pane_id: str
    pane_pid: int
    start_ticks: int


@dataclass(frozen=True)
class ProcessProof:
    pid: int
    executable: Path
    argv: tuple[str, ...]
    argv_sha256: str
    environment_sha256: str
    start_ticks: int
    process_group_id: int


@dataclass(frozen=True)
class AuthorityBinding:
    source_path: str
    source_sha256: str
    mailbox_identity_sha256: str
    approval_uid: int
    approval_message_id: str
    approval_thread_id: str
    approval_internaldate_unix_ms: int
    approval_raw_mime_sha256: str
    approval_rfc_message_id: str
    procedure_sha256: str
    custody_sha256: str


@dataclass(frozen=True)
class AuthorityEvidence:
    outcome: str
    mailbox_identity_sha256: str
    observed_sequence: int
    controlling_uid: int
    controlling_message_id: str
    controlling_thread_id: str
    controlling_internaldate_unix_ms: int
    controlling_raw_mime_sha256: str
    controlling_rfc_message_id: str


@dataclass(frozen=True)
class AuthorityCommit:
    mailbox_identity_sha256: str
    sequence: int
    gmail_uid: int
    gmail_message_id: str
    gmail_thread_id: str
    gmail_internaldate_unix_ms: int
    raw_mime_sha256: str
    rfc_message_id: str


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def decoded(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise DualSuccessorError(f"journal {label} is not text")
    try:
        result = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise DualSuccessorError(f"journal {label} is not canonical base64") from exc
    if encoded(result) != value:
        raise DualSuccessorError(f"journal {label} is not canonical base64")
    return result


def canonical_json(value: object) -> bytes:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_RECORD_BYTES:
        raise DualSuccessorError("durable record exceeds its size bound")
    return data


def require_sha(value: str, label: str) -> None:
    if SHA256_RE.fullmatch(value) is None:
        raise DualSuccessorError(f"{label} SHA-256 must be lowercase hexadecimal")


def required_int(record: dict[str, object], key: str) -> int:
    value = record.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DualSuccessorError(f"durable record {key} must be an integer")
    return value


def required_string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DualSuccessorError(f"{label} must be a list of strings")
    return tuple(value)


def required_home_inventory(value: object) -> tuple[tuple[str, int, str], ...]:
    if not isinstance(value, list):
        raise DualSuccessorError("Codex home inventory must be a list")
    result: list[tuple[str, int, str]] = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 3 or not isinstance(entry[0], str) or not isinstance(entry[1], int) or isinstance(entry[1], bool) or not isinstance(entry[2], str):
            raise DualSuccessorError("Codex home inventory entry is invalid")
        result.append((entry[0], entry[1], entry[2]))
    return tuple(result)


def require_root(args: PrepareArgs) -> None:
    if args.root.resolve(strict=True) != args.root or not args.root.is_dir() or args.root.is_symlink():
        raise DualSuccessorError("root must be one canonical existing non-symlink directory")
    paths = (
        args.prompt_file,
        args.launch_manifest,
        args.instructions_file,
        args.approval_file,
        args.journal,
    )
    if len(set(paths)) != len(paths):
        raise DualSuccessorError("prompt, manifest, instructions, approval, and journal paths must differ")
    if args.journal.parent != args.root or JOURNAL_RE.fullmatch(args.journal.name) is None:
        raise DualSuccessorError("journal must be a canonical direct ROOT child with the supported name")
    if (
        tuple(sorted(args.protected_targets)) != args.protected_targets
        or len(set(args.protected_targets)) != len(args.protected_targets)
        or args.old_target in args.protected_targets
        or args.new_target in args.protected_targets
        or protected_digest(args.protected_targets) != args.protected_sha256
    ):
        raise DualSuccessorError("protected targets are not the exact canonical disjoint bound set")


def custody_digest(args: PrepareArgs) -> str:
    """Bind the approval-independent exact transaction identity.

    ``approval_sha256`` is deliberately excluded because the approval record
    contains this digest.  Every other invocation identity, including the
    approval path, exact queue, and journal path, is included; the approval
    bytes retain their own independently supplied and journaled digest.
    """

    value = {
        "root": str(args.root),
        "shadow_task": args.shadow_task,
        "canonical_task": args.canonical_task,
        "successor_task": args.successor_task,
        "old_target": args.old_target,
        "new_target": args.new_target,
        "manager_target": args.manager_target,
        "shadow_sha256": args.shadow_sha256,
        "canonical_sha256": args.canonical_sha256,
        "todo_sha256": args.todo_sha256,
        "expected_pending_items": list(args.expected_pending_items),
        "queue_sha256": args.queue_sha256,
        "prompt_file": str(args.prompt_file),
        "prompt_sha256": args.prompt_sha256,
        "launch_manifest": str(args.launch_manifest),
        "launch_manifest_sha256": args.launch_manifest_sha256,
        "instructions_file": str(args.instructions_file),
        "instructions_sha256": args.instructions_sha256,
        "approval_file": str(args.approval_file),
        "protected_targets": list(args.protected_targets),
        "protected_sha256": args.protected_sha256,
        "journal": str(args.journal),
    }
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def safe_external_snapshot(path: Path, label: str, *, exact_mode: int | None = None) -> Snapshot:
    try:
        snapshot = read_frozen_prompt(path)
    except (OSError, RuntimeError) as exc:
        raise DualSuccessorError(f"{label} is unavailable or unsafe") from exc
    mode = stat.S_IMODE(snapshot.state.st_mode)
    if snapshot.state.st_uid != os.getuid() or (exact_mode is not None and mode != exact_mode):
        expected = f" mode {exact_mode:04o}" if exact_mode is not None else ""
        raise DualSuccessorError(f"{label} must be owner-owned{expected}")
    return snapshot


def helper_identity() -> tuple[Path, str, int]:
    path = Path(__file__).resolve(strict=True)
    snapshot = read_pinned_system_executable(path, "dual-successor helper")
    mode = stat.S_IMODE(snapshot.state.st_mode)
    if path.name != "omo_dual_worker_successor.py" or mode != 0o755:
        raise DualSuccessorError("approved helper must be the exact executable mode-0755 implementation")
    return path, digest(snapshot.data), mode


def _installed_file_identity(path: Path, label: str, *, executable: bool) -> dict[str, str]:
    """Authenticate one fixed installed component without consulting PATH/HOME."""

    try:
        snapshot = read_pinned_system_executable(path, label) if executable else read_frozen_prompt(path)
    except (OSError, RuntimeError) as exc:
        raise DualSuccessorError(f"{label} is unavailable or unsafe") from exc
    mode = stat.S_IMODE(snapshot.state.st_mode)
    if snapshot.path != path or snapshot.state.st_uid != os.getuid() or mode & 0o022 or (executable and not mode & 0o100):
        raise DualSuccessorError(f"{label} is not an exact owner-controlled installed file")
    return {"path": str(path), "sha256": digest(snapshot.data), "mode": f"{mode:04o}"}


def _installed_codex_identity() -> dict[str, object]:
    """Return the only production Codex installation identity accepted by this helper.

    The paths are derived from the passwd database, never a caller manifest, PATH,
    HOME, or executable basename.  Tests replace this private verifier in-process;
    neither CLI entry point has an injection or fake-runtime option.
    """

    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    launcher = home / ".local/bin/codex"
    cli_link = home / ".bun/bin/codex"
    cli_program = home / ".bun/install/global/node_modules/@openai/codex/bin/codex.js"
    package_manifest = home / ".bun/install/global/node_modules/@openai/codex/package.json"
    native_manifest = home / ".bun/install/global/node_modules/@openai/codex-linux-x64/package.json"
    native_runtime = home / ".bun/install/global/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
    try:
        link_state = cli_link.lstat()
        link_value = os.readlink(cli_link)
        resolved_link = cli_link.resolve(strict=True)
    except OSError as exc:
        raise DualSuccessorError("installed Codex CLI link is unavailable") from exc
    if (
        not stat.S_ISLNK(link_state.st_mode)
        or link_state.st_uid != os.getuid()
        or link_value != "../install/global/node_modules/@openai/codex/bin/codex.js"
        or resolved_link != cli_program
    ):
        raise DualSuccessorError("installed Codex CLI link identity changed")
    launcher_identity = _installed_file_identity(launcher, "installed Codex launcher", executable=True)
    program_identity = _installed_file_identity(cli_program, "installed Codex program", executable=True)
    package_identity = _installed_file_identity(package_manifest, "installed Codex package manifest", executable=False)
    native_manifest_identity = _installed_file_identity(native_manifest, "installed Codex native manifest", executable=False)
    runtime_identity = _installed_file_identity(native_runtime, "installed Codex native runtime", executable=True)
    try:
        package_value = json.loads(package_manifest.read_bytes())
        native_value = json.loads(native_manifest.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DualSuccessorError("installed Codex package metadata is invalid") from exc
    version = package_value.get("version") if isinstance(package_value, dict) else None
    if (
        not isinstance(version, str)
        or not version
        or package_value.get("name") != "@openai/codex"
        or not isinstance(native_value, dict)
        or native_value.get("name") != "@openai/codex-linux-x64"
        or native_value.get("version") != version
    ):
        raise DualSuccessorError("installed Codex package/runtime version identity changed")
    return {
        "schema": "installed-codex-exact-identity/v1",
        "version": version,
        "launcher": launcher_identity,
        "cli_link_path": str(cli_link),
        "cli_link_target": link_value,
        "program": program_identity,
        "package_manifest": package_identity,
        "native_manifest": native_manifest_identity,
        "runtime": runtime_identity,
    }


def _runtime_from_codex_install(value: object) -> Path:
    current = _installed_codex_identity()
    if value != current:
        raise DualSuccessorError("installed Codex identity changed after manifest creation")
    runtime = current.get("runtime")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("path"), str):
        raise DualSuccessorError("installed Codex identity lacks its native runtime")
    return Path(runtime["path"])


def authority_snapshot(root: Path, relative: str, *, label: str, pattern: re.Pattern[str]) -> Snapshot:
    if pattern.fullmatch(relative) is None:
        raise DualSuccessorError(f"{label} must use the exact trusted root-relative namespace")
    lexical = root.joinpath(*Path(relative).parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise DualSuccessorError(f"{label} is unavailable") from exc
    if resolved != lexical or resolved_root not in resolved.parents:
        raise DualSuccessorError(f"{label} path is not canonical or escaped the work-log root")
    current = resolved_root
    for part in Path(relative).parts[:-1]:
        current = current / part
        state = current.lstat()
        if not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) & 0o022:
            raise DualSuccessorError(f"{label} directory chain is not owner-controlled")
    snapshot = safe_external_snapshot(resolved, label)
    if stat.S_IMODE(snapshot.state.st_mode) & 0o022:
        raise DualSuccessorError(f"{label} must not be group/world writable")
    return snapshot


def _canonical_human_approval_body(expected: dict[str, str], instructions: bytes) -> str:
    try:
        instruction_text = instructions.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DualSuccessorError("persistent instructions are not UTF-8") from exc
    if not instruction_text.endswith("\n"):
        raise DualSuccessorError("persistent instructions must end with one newline")
    binding = "".join(f"{key}: {value}\n" for key, value in expected.items())
    return (
        f"{APPROVAL_QUOTE}\n"
        f"{binding}"
        "----- BEGIN COMPLETE EXACT INSTRUCTION TEXT -----\n"
        f"{instruction_text}"
        "----- END COMPLETE EXACT INSTRUCTION TEXT -----\n"
    )


def authority_snapshot_sha256(approval: dict[str, str]) -> str:
    """Commit the immutable provider ordering and exact approval identity."""

    value = {
        key: approval[key]
        for key in (
            "authority_source",
            "authority_source_sha256",
            "gmail_mailbox_identity_sha256",
            "gmail_uid",
            "gmail_message_id",
            "gmail_thread_id",
            "gmail_internaldate_unix_ms",
            "raw_mime_sha256",
            "rfc_message_id",
        )
    }
    return digest(canonical_json(value))


def canonical_message_text(message: Message) -> str:
    text = message_text(message)
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized:
        raise DualSuccessorError("authenticated Gmail plain-text body contains a noncanonical carriage return")
    return normalized


def _authenticated_human_approval(
    *,
    root: Path,
    approval: dict[str, str],
    expected: dict[str, str],
    instructions: bytes,
) -> None:
    """Re-fetch and authenticate the exact Human approval from Gmail.

    A local manager_mail file or agent-authored envelope is never authority on
    its own.  The external Gmail object is selected by immutable provider ID,
    then its transport-authenticated sender, MIME, raw bytes, metadata, exact
    body, and stored local rendering are all compared.
    """

    required = {
        "authority_schema",
        "authority_source",
        "authority_source_sha256",
        "gmail_mailbox_identity_sha256",
        "gmail_uid",
        "gmail_message_id",
        "gmail_thread_id",
        "gmail_internaldate_unix_ms",
        "raw_mime_sha256",
        "rfc_message_id",
        "authority_subject",
        "authority_sequence",
        "authority_snapshot_sha256",
        "procedure_sha256",
    }
    if not required.issubset(approval):
        raise DualSuccessorError("approval lacks immutable authenticated Gmail source identity")
    if approval["authority_schema"] != AUTHENTICATED_APPROVAL_SCHEMA or approval["authority_subject"] != APPROVAL_SUBJECT:
        raise DualSuccessorError("approval authenticated-source schema or subject is not exact")
    for field in ("authority_source_sha256", "gmail_mailbox_identity_sha256", "raw_mime_sha256"):
        require_sha(approval[field], field)
    for field in ("gmail_uid", "gmail_message_id", "gmail_thread_id", "gmail_internaldate_unix_ms"):
        if not approval[field].isdigit():
            raise DualSuccessorError(f"approval {field} is not a canonical provider identity")
    source = authority_snapshot(root, approval["authority_source"], label="approval Human source", pattern=AUTHORITY_REF_RE)
    if digest(source.data) != approval["authority_source_sha256"]:
        raise DualSuccessorError("approval Human source bytes changed")
    settings = configured_agent_mail()
    if settings is None or settings.agent_address.casefold() != APPROVAL_AGENT_MAILBOX or settings.human_address.casefold() != APPROVAL_HUMAN_MAILBOX:
        raise DualSuccessorError("authenticated Human mailbox configuration is unavailable or changed")
    expected_body = _canonical_human_approval_body(expected, instructions)
    try:
        with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, timeout=15) as client:
            client.login(settings.agent_address, settings.app_password)
            selected, _ = client.select('"[Gmail]/All Mail"', readonly=True)
            if selected != "OK":
                raise DualSuccessorError("authenticated Gmail archive cannot be selected")
            mailbox_identity = mailbox_state_identity(client, settings.agent_address)
            if digest(mailbox_identity.encode()) != approval["gmail_mailbox_identity_sha256"]:
                raise DualSuccessorError("authenticated Gmail UIDVALIDITY identity changed")
            typ, uid_data = client.uid("search", None, "X-GM-MSGID", approval["gmail_message_id"])  # pyright: ignore[reportArgumentType]
            uids = b" ".join(item for item in uid_data if isinstance(item, bytes)).split() if typ == "OK" and uid_data else []
            if uids != [approval["gmail_uid"].encode("ascii")]:
                raise DualSuccessorError("authenticated Gmail object is absent or ambiguous")
            typ, msg_data = client.uid("fetch", approval["gmail_uid"], "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple) or not isinstance(msg_data[0][1], bytes):
                raise DualSuccessorError("authenticated Gmail MIME fetch failed")
            raw_mime = msg_data[0][1]
            provider = fetch_gmail_metadata(client, approval["gmail_uid"])
    except DualSuccessorError:
        raise
    except (OSError, imaplib.IMAP4.error, RuntimeError) as exc:
        raise DualSuccessorError("authenticated Gmail source verification failed closed") from exc
    if digest(raw_mime) != approval["raw_mime_sha256"] or provider != (
        approval["gmail_message_id"],
        approval["gmail_thread_id"],
        approval["gmail_internaldate_unix_ms"],
    ):
        raise DualSuccessorError("authenticated Gmail MIME or provider metadata changed")
    message = BytesParser(policy=policy.default).parsebytes(raw_mime)
    auth_headers = [str(item) for item in message.get_all("Authentication-Results", [])]
    strict_transport = False
    if len(auth_headers) == 1:
        segments = auth_headers[0].split(";")
        strict_transport = segments[0].strip().casefold() == "mx.google.com" and any(
            re.search(r"(?:^|\s)spf=pass(?:\s|$)", " ".join(segment.casefold().split())) is not None
            and re.search(
                rf"(?:^|\s)smtp\.mailfrom={re.escape(settings.human_address.casefold())}(?:\s|$)",
                " ".join(segment.casefold().split()),
            )
            is not None
            for segment in segments[1:]
        )
    recipients = [address.casefold() for _name, address in getaddresses([str(item) for item in message.get_all("To", [])]) if address]
    if (
        not exact_human_sender(message, settings.human_address, require_transport_identity=True)
        or not strict_transport
        or recipients != [settings.agent_address.casefold()]
        or message.get_all("Cc", [])
        or message.get_all("Bcc", [])
        or message.is_multipart()
        or message.get_content_type() != "text/plain"
        or [str(item) for item in message.get_all("Subject", [])] != [APPROVAL_SUBJECT]
        or [str(item) for item in message.get_all("Message-ID", [])] != [approval["rfc_message_id"]]
        or canonical_message_text(message) != expected_body
    ):
        raise DualSuccessorError("Gmail object is not the exact transport-authenticated Human approval")
    rendered = f"Subject: {normalize_human_subject(APPROVAL_SUBJECT)}\n\n{expected_body}".encode()
    if source.data != rendered:
        raise DualSuccessorError("local manager_mail source is not the exact authenticated Gmail rendering")


def approval_record(
    snapshot: Snapshot,
    *,
    root: Path,
    instructions: Snapshot,
    manifest: dict[str, object],
    custody_sha256: str,
    authenticate: bool = True,
) -> dict[str, str]:
    if stat.S_IMODE(snapshot.state.st_mode) != 0o400:
        raise DualSuccessorError("approval record must be frozen mode 0400")
    try:
        value = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DualSuccessorError(f"approval record is invalid JSON: {exc}") from exc
    keys = {
        "version",
        "operation",
        "launch_schema",
        "launch_schema_sha256",
        "instructions_sha256",
        "helper_path",
        "helper_sha256",
        "helper_mode",
        "codex_install_sha256",
        "custody_sha256",
        "argv_sha256",
        "approval_quote",
        "authority_schema",
        "authority_source",
        "authority_source_sha256",
        "gmail_mailbox_identity_sha256",
        "gmail_uid",
        "gmail_message_id",
        "gmail_thread_id",
        "gmail_internaldate_unix_ms",
        "raw_mime_sha256",
        "rfc_message_id",
        "authority_subject",
        "authority_sequence",
        "authority_snapshot_sha256",
        "procedure_sha256",
    }
    if not isinstance(value, dict) or set(value) != keys or not all(isinstance(item, str) for item in value.values()):
        raise DualSuccessorError("approval record must contain only the exact supported string fields")
    result = {str(key): str(item) for key, item in value.items()}
    helper_path, helper_sha256, helper_mode = helper_identity()
    install = manifest.get("codex_install")
    if not isinstance(install, dict):
        raise DualSuccessorError("launch manifest lacks exact installed Codex identity")
    install_sha256 = digest(canonical_json(install))
    require_sha(custody_sha256, "transaction custody")
    argv_sha256 = digest(b"\0".join(item.encode() for item in required_string_list(manifest.get("argv"), "manifest argv")))
    schema_sha256 = digest(LAUNCH_SCHEMA.encode())
    expected = {
        "version": VERSION,
        "operation": OPERATION,
        "launch_schema": LAUNCH_SCHEMA,
        "launch_schema_sha256": schema_sha256,
        "instructions_sha256": digest(instructions.data),
        "helper_path": str(helper_path),
        "helper_sha256": helper_sha256,
        "helper_mode": f"{helper_mode:04o}",
        "codex_install_sha256": install_sha256,
        "custody_sha256": custody_sha256,
        "argv_sha256": argv_sha256,
        "approval_quote": APPROVAL_QUOTE,
        "procedure_sha256": digest(instructions.data),
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise DualSuccessorError(
            "approval record does not bind the exact instructions, helper, schema, and runtime or exact transaction custody"
        )
    if result["approval_quote"] != APPROVAL_QUOTE:
        raise DualSuccessorError("approval record does not contain the exact required approval quote")
    if (
        result["authority_sequence"] != result["gmail_uid"]
        or result["authority_snapshot_sha256"] != authority_snapshot_sha256(result)
    ):
        raise DualSuccessorError("approval record does not bind its frozen authoritative-source sequence and identity")
    if authenticate:
        _authenticated_human_approval(root=root, approval=result, expected=expected, instructions=instructions.data)
    return result


def authority_binding(approval: dict[str, str], *, custody_sha256: str) -> AuthorityBinding:
    return AuthorityBinding(
        approval["authority_source"],
        approval["authority_source_sha256"],
        approval["gmail_mailbox_identity_sha256"],
        int(approval["authority_sequence"]),
        approval["gmail_message_id"],
        approval["gmail_thread_id"],
        int(approval["gmail_internaldate_unix_ms"]),
        approval["raw_mime_sha256"],
        approval["rfc_message_id"],
        approval["procedure_sha256"],
        custody_sha256,
    )


def authority_binding_fields(binding: AuthorityBinding) -> dict[str, object]:
    return {
        "source_path": binding.source_path,
        "source_sha256": binding.source_sha256,
        "mailbox_identity_sha256": binding.mailbox_identity_sha256,
        "approval_uid": binding.approval_uid,
        "approval_message_id": binding.approval_message_id,
        "approval_thread_id": binding.approval_thread_id,
        "approval_internaldate_unix_ms": binding.approval_internaldate_unix_ms,
        "approval_raw_mime_sha256": binding.approval_raw_mime_sha256,
        "approval_rfc_message_id": binding.approval_rfc_message_id,
        "procedure_sha256": binding.procedure_sha256,
        "custody_sha256": binding.custody_sha256,
    }


def _mailbox_sequence(client: imaplib.IMAP4_SSL) -> int:
    name, data = client.response("UIDNEXT")
    if name != "UIDNEXT" or not data or not data[0]:
        raise DualSuccessorError("authenticated Gmail source omitted its authoritative sequence")
    try:
        next_uid = int(data[0])
    except ValueError as exc:
        raise DualSuccessorError("authenticated Gmail source returned an invalid authoritative sequence") from exc
    if next_uid <= 0:
        raise DualSuccessorError("authenticated Gmail source returned a nonpositive authoritative sequence")
    return next_uid - 1


def _fetch_authority_message(client: imaplib.IMAP4_SSL, uid: int) -> tuple[bytes, Message, tuple[str, str, str]]:
    typ, msg_data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
    if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple) or not isinstance(msg_data[0][1], bytes):
        raise DualSuccessorError("authenticated Gmail controlling-message fetch failed")
    raw_mime = msg_data[0][1]
    provider = fetch_gmail_metadata(client, str(uid))
    if provider is None or provider[1] is None or provider[2] is None:
        raise DualSuccessorError("authenticated Gmail controlling-message metadata is incomplete")
    return raw_mime, BytesParser(policy=policy.default).parsebytes(raw_mime), (provider[0], provider[1], provider[2])


def _strict_human_transport(message: Message, *, human_address: str, agent_address: str) -> bool:
    auth_headers = [str(item) for item in message.get_all("Authentication-Results", [])]
    strict_transport = False
    if len(auth_headers) == 1:
        segments = auth_headers[0].split(";")
        strict_transport = segments[0].strip().casefold() == "mx.google.com" and any(
            re.search(r"(?:^|\s)spf=pass(?:\s|$)", " ".join(segment.casefold().split())) is not None
            and re.search(
                rf"(?:^|\s)smtp\.mailfrom={re.escape(human_address.casefold())}(?:\s|$)",
                " ".join(segment.casefold().split()),
            )
            is not None
            for segment in segments[1:]
        )
    recipients = [address.casefold() for _name, address in getaddresses([str(item) for item in message.get_all("To", [])]) if address]
    return bool(
        exact_human_sender(message, human_address, require_transport_identity=True)
        and strict_transport
        and recipients == [agent_address.casefold()]
        and not message.get_all("Cc", [])
        and not message.get_all("Bcc", [])
        and not message.is_multipart()
        and message.get_content_type() == "text/plain"
    )


def _authority_identity_lines(binding: AuthorityBinding) -> str:
    return (
        f"custody_sha256: {binding.custody_sha256}\n"
        f"procedure_sha256: {binding.procedure_sha256}\n"
        f"approval_gmail_message_id: {binding.approval_message_id}\n"
        f"approval_rfc_message_id: {binding.approval_rfc_message_id}\n"
    )


def _withdrawal_body(binding: AuthorityBinding) -> str:
    return f"{WITHDRAWAL_QUOTE}\n{_authority_identity_lines(binding)}"


def _authority_commit_body(binding: AuthorityBinding, creation_capability: str) -> str:
    return f"Commit the exact launch authority boundary.\n{_authority_identity_lines(binding)}creation_capability: {creation_capability}\n"


def authority_commit_rfc_id(binding: AuthorityBinding, creation_capability: str) -> str:
    identity = digest(f"{binding.custody_sha256}\0{creation_capability}".encode())
    return f"<dual-successor-{identity}@authority.invalid>"


def _authority_commit_message(binding: AuthorityBinding, creation_capability: str, agent_address: str) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = agent_address
    message["To"] = agent_address
    message["Subject"] = COMMIT_SUBJECT
    message["Message-ID"] = authority_commit_rfc_id(binding, creation_capability)
    message.set_content(_authority_commit_body(binding, creation_capability), cte="7bit")
    return message.as_bytes()


def _classify_authority_before_commit(
    client: imaplib.IMAP4_SSL,
    *,
    binding: AuthorityBinding,
    commit_uid: int,
    settings: AgentMailSettings,
) -> AuthorityEvidence:
    if commit_uid <= binding.approval_uid:
        raise DualSuccessorError("authoritative commit sequence does not follow the bound approval")
    if commit_uid == binding.approval_uid + 1:
        return AuthorityEvidence("approved", binding.mailbox_identity_sha256, commit_uid, 0, "", "", 0, "", "")
    if commit_uid - binding.approval_uid - 1 > 10000:
        return AuthorityEvidence("unknown", binding.mailbox_identity_sha256, commit_uid, 0, "", "", 0, "", "")
    typ, data = client.uid("search", None, "UID", f"{binding.approval_uid + 1}:{commit_uid - 1}")  # pyright: ignore[reportArgumentType]
    if typ != "OK" or data is None:
        raise DualSuccessorError("authenticated Gmail controlling-message search failed")
    raw_uids = b" ".join(item for item in data if isinstance(item, bytes)).split()
    if len(raw_uids) > 10000:
        return AuthorityEvidence("unknown", binding.mailbox_identity_sha256, commit_uid, 0, "", "", 0, "", "")
    expected_uids = set(range(binding.approval_uid + 1, commit_uid))
    if any(not raw_uid.isdigit() for raw_uid in raw_uids):
        raise DualSuccessorError("authenticated Gmail search returned an invalid sequence")
    observed_uids = [int(raw_uid) for raw_uid in raw_uids]
    if len(observed_uids) != len(set(observed_uids)) or set(observed_uids) != expected_uids:
        missing = min(expected_uids - set(observed_uids), default=0)
        return AuthorityEvidence("unknown", binding.mailbox_identity_sha256, commit_uid, missing, "", "", 0, "", "")
    relevant: list[AuthorityEvidence] = []
    for uid in observed_uids:
        raw_mime, message, provider = _fetch_authority_message(client, uid)
        message_id, thread_id, internaldate = provider
        subject = [str(item) for item in message.get_all("Subject", [])]
        rfc_ids = [str(item) for item in message.get_all("Message-ID", [])]
        is_relevant = thread_id == binding.approval_thread_id or subject in ([APPROVAL_SUBJECT], [WITHDRAWAL_SUBJECT], [COMMIT_SUBJECT])
        if not is_relevant:
            continue
        evidence = AuthorityEvidence(
            "unknown",
            binding.mailbox_identity_sha256,
            commit_uid,
            uid,
            message_id,
            thread_id,
            int(internaldate),
            digest(raw_mime),
            rfc_ids[0] if len(rfc_ids) == 1 else "",
        )
        if (
            _strict_human_transport(message, human_address=settings.human_address, agent_address=settings.agent_address)
            and subject == [WITHDRAWAL_SUBJECT]
            and len(rfc_ids) == 1
            and canonical_message_text(message) == _withdrawal_body(binding)
        ):
            evidence = replace(evidence, outcome="withdrawn")
        relevant.append(evidence)
    if not relevant:
        return AuthorityEvidence("approved", binding.mailbox_identity_sha256, commit_uid, 0, "", "", 0, "", "")
    controlling = max(relevant, key=lambda item: item.controlling_uid)
    if any(item.outcome == "unknown" for item in relevant):
        return replace(controlling, outcome="unknown")
    return replace(controlling, outcome="withdrawn")


def check_current_authority(binding: AuthorityBinding) -> None:
    """Reject known withdrawal or ambiguity before creating a process."""

    settings = configured_agent_mail()
    if settings is None or settings.agent_address.casefold() != APPROVAL_AGENT_MAILBOX or settings.human_address.casefold() != APPROVAL_HUMAN_MAILBOX:
        raise DualSuccessorError("authenticated Human mailbox configuration is unavailable or changed")
    try:
        with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, timeout=15) as client:
            client.login(settings.agent_address, settings.app_password)
            selected, _ = client.select('"[Gmail]/All Mail"', readonly=True)
            if selected != "OK" or digest(mailbox_state_identity(client, settings.agent_address).encode()) != binding.mailbox_identity_sha256:
                raise DualSuccessorError("authenticated Gmail authority sequence identity changed")
            sequence = _mailbox_sequence(client)
            if sequence < binding.approval_uid:
                raise DualSuccessorError("authenticated Gmail authority sequence regressed behind approval")
            evidence = _classify_authority_before_commit(client, binding=binding, commit_uid=sequence + 1, settings=settings)
    except (DualSuccessorError, AuthorityBlocked):
        raise
    except (OSError, imaplib.IMAP4.error, RuntimeError, ValueError) as exc:
        raise DualSuccessorError("authenticated Gmail authority check failed closed") from exc
    if evidence.outcome != "approved":
        raise AuthorityBlocked(replace(evidence, observed_sequence=sequence))


def final_authority_commit(
    binding: AuthorityBinding,
    creation_capability: str,
    *,
    expected_commit: AuthorityCommit | None = None,
    reconcile_only: bool = False,
) -> AuthorityCommit:
    """Linearize a new commit or reconcile its exact durable Gmail marker."""

    settings = configured_agent_mail()
    if settings is None or settings.agent_address.casefold() != APPROVAL_AGENT_MAILBOX or settings.human_address.casefold() != APPROVAL_HUMAN_MAILBOX:
        raise DualSuccessorError("authenticated Human mailbox configuration is unavailable or changed")
    if TOKEN_RE.fullmatch(creation_capability) is None:
        raise DualSuccessorError("authority commit requires the exact process creation capability")
    raw_commit = _authority_commit_message(binding, creation_capability, settings.agent_address)
    commit_rfc_id = authority_commit_rfc_id(binding, creation_capability)
    try:
        with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, timeout=15) as client:
            client.login(settings.agent_address, settings.app_password)
            selected, _ = client.select('"[Gmail]/All Mail"', readonly=False)
            if selected != "OK" or digest(mailbox_state_identity(client, settings.agent_address).encode()) != binding.mailbox_identity_sha256:
                raise DualSuccessorError("authenticated Gmail authority sequence identity changed")
            typ, found = client.uid("search", None, "HEADER", "Message-ID", commit_rfc_id)  # pyright: ignore[reportArgumentType]
            if typ != "OK" or found is None or any(not isinstance(item, bytes) for item in found):
                raise DualSuccessorError("authenticated Gmail authority commit lookup failed closed")
            found_uids = b" ".join(found).split()
            if not found_uids:
                if expected_commit is not None or reconcile_only:
                    raise DualSuccessorError("authenticated Gmail authority commit is missing during reconciliation")
                appended, _ = client.append('"[Gmail]/All Mail"', "()", imaplib.Time2Internaldate(time.time()), raw_commit)
                if appended != "OK":
                    raise DualSuccessorError("authenticated Gmail authority commit append failed")
                typ, found = client.uid("search", None, "HEADER", "Message-ID", commit_rfc_id)  # pyright: ignore[reportArgumentType]
                if typ != "OK" or found is None or any(not isinstance(item, bytes) for item in found):
                    raise DualSuccessorError("authenticated Gmail authority commit lookup failed after append")
                found_uids = b" ".join(found).split()
            if len(found_uids) != 1 or not found_uids[0].isdigit():
                raise DualSuccessorError("authenticated Gmail authority commit identity is absent or ambiguous")
            commit_uid = int(found_uids[0])
            evidence = _classify_authority_before_commit(client, binding=binding, commit_uid=commit_uid, settings=settings)
            if evidence.outcome != "approved":
                raise AuthorityBlocked(evidence)
            raw_stored, message, provider = _fetch_authority_message(client, commit_uid)
            if _mailbox_sequence(client) < commit_uid:
                raise DualSuccessorError("authenticated Gmail authority sequence regressed")
    except (DualSuccessorError, AuthorityBlocked):
        raise
    except (OSError, imaplib.IMAP4.error, RuntimeError, ValueError) as exc:
        raise DualSuccessorError("authenticated Gmail authority commit failed closed") from exc
    message_id, thread_id, internaldate = provider
    senders = [address.casefold() for _name, address in getaddresses([str(item) for item in message.get_all("From", [])]) if address]
    recipients = [address.casefold() for _name, address in getaddresses([str(item) for item in message.get_all("To", [])]) if address]
    if (
        [str(item) for item in message.get_all("Message-ID", [])] != [commit_rfc_id]
        or [str(item) for item in message.get_all("Subject", [])] != [COMMIT_SUBJECT]
        or senders != [settings.agent_address.casefold()]
        or recipients != [settings.agent_address.casefold()]
        or message.get_all("Sender", [])
        or message.get_all("Cc", [])
        or message.get_all("Bcc", [])
        or message.is_multipart()
        or message.get_content_type() != "text/plain"
        or canonical_message_text(message) != _authority_commit_body(binding, creation_capability)
    ):
        raise DualSuccessorError("authenticated Gmail authority commit object changed")
    result = AuthorityCommit(
        binding.mailbox_identity_sha256,
        commit_uid,
        commit_uid,
        message_id,
        thread_id,
        int(internaldate),
        digest(raw_stored),
        commit_rfc_id,
    )
    if expected_commit is not None and result != expected_commit:
        raise DualSuccessorError("authenticated Gmail authority commit evidence changed during reconciliation")
    return result


def directory_inventory(path: Path) -> tuple[tuple[str, int, str], ...]:
    canonical = path.resolve(strict=True)
    info = canonical.lstat()
    if canonical != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DualSuccessorError("Codex home must be a canonical owner-private non-symlink directory")
    entries: list[tuple[str, int, str]] = []
    for candidate in sorted(canonical.rglob("*"), key=lambda item: item.as_posix()):
        relative = candidate.relative_to(canonical).as_posix()
        state = candidate.lstat()
        if candidate.is_symlink() or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) & 0o022:
            raise DualSuccessorError(f"Codex home contains an unsafe entry: {relative}")
        if candidate.is_dir():
            entries.append((relative + "/", stat.S_IMODE(state.st_mode), ""))
        elif candidate.is_file() and state.st_size <= 16 * 1024 * 1024:
            snapshot = read_frozen_prompt(candidate)
            entries.append((relative, stat.S_IMODE(snapshot.state.st_mode), digest(snapshot.data)))
        else:
            raise DualSuccessorError(f"Codex home contains an unsupported entry: {relative}")
    return tuple(entries)


def codex_argv(runtime: Path, *, workdir: Path, model: str, reasoning_effort: str, prompt: str) -> tuple[str, ...]:
    return (
        str(runtime),
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--cd",
        str(workdir),
        prompt,
    )


def launch_environment(*, workdir: Path, target: str, codex_home: Path, token: str) -> dict[str, str]:
    result = minimal_launch_environment()
    result.update(
        {
            "CODEX_HOME": str(codex_home),
            "OMO_AGENT_TMUX_TARGET": canonical_target(target),
            "OMO_DUAL_SUCCESSOR_TOKEN": token,
            "PWD": str(workdir),
        }
    )
    return result


def launch_manifest_bytes(
    *,
    root: Path,
    task_file: str,
    target: str,
    manager_target: str,
    workdir: Path,
    model: str,
    reasoning_effort: str,
    prompt: bytes,
    codex_home: Path,
    launch_token: str,
) -> bytes:
    canonical_root = root.resolve(strict=True)
    canonical_workdir = workdir.resolve(strict=True)
    canonical_home = codex_home.resolve(strict=True)
    canonical_worker = canonical_target(target)
    if TASK_RE.fullmatch(task_file) is None:
        raise DualSuccessorError("launch task reference is invalid")
    if target_session(canonical_worker).startswith("h") or target_session(manager_target).startswith("h"):
        raise DualSuccessorError("prepared launch cannot address Human-owned h* targets")
    if MODEL_RE.fullmatch(model) is None or reasoning_effort not in EFFORTS:
        raise DualSuccessorError("launch model or reasoning effort is invalid")
    if TOKEN_RE.fullmatch(launch_token) is None:
        raise DualSuccessorError("launch token must be one 64-character lowercase hexadecimal value")
    if not canonical_workdir.is_dir() or canonical_workdir.is_symlink():
        raise DualSuccessorError("launch workdir must be one canonical non-symlink directory")
    try:
        prompt_text = prompt.decode()
    except UnicodeDecodeError as exc:
        raise DualSuccessorError("launch prompt is not UTF-8") from exc
    codex_install = _installed_codex_identity()
    runtime = _runtime_from_codex_install(codex_install)
    runtime_value = codex_install["runtime"]
    assert isinstance(runtime_value, dict)
    runtime_snapshot = read_pinned_system_executable(runtime, "Codex native runtime")
    if runtime.resolve(strict=True) != runtime or digest(runtime_snapshot.data) != runtime_value.get("sha256"):
        raise DualSuccessorError("installed Codex native runtime does not match its pinned identity")
    home_inventory = directory_inventory(canonical_home)
    shell = pinned_shell_identity()
    tmux = pinned_tmux_identity()
    environment = launch_environment(workdir=canonical_workdir, target=canonical_worker, codex_home=canonical_home, token=launch_token)
    argv = codex_argv(runtime, workdir=canonical_workdir, model=model, reasoning_effort=reasoning_effort, prompt=prompt_text)
    value = {
        "version": VERSION,
        "tool": "codex",
        "root": str(canonical_root),
        "task_file": task_file,
        "target": canonical_worker,
        "manager_target": canonical_target(manager_target),
        "workdir": str(canonical_workdir),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "prompt_sha256": digest(prompt),
        "launch_token": launch_token,
        "argv": list(argv),
        "environment": environment,
        "codex_install": codex_install,
        "codex_home": str(canonical_home),
        "codex_home_inventory": [list(item) for item in home_inventory],
        "codex_home_inventory_sha256": digest(json.dumps(home_inventory, separators=(",", ":")).encode()),
        "shell_runtime": shell,
        "tmux_runtime": tmux,
        "tmux_environment": minimal_tmux_environment(),
    }
    return canonical_json(value)


def validated_manifest(data: bytes, args: PrepareArgs) -> dict[str, object]:
    if digest(data) != args.launch_manifest_sha256:
        raise DualSuccessorError("launch manifest digest changed")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DualSuccessorError(f"launch manifest is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DualSuccessorError("launch manifest must contain one object")
    expected = launch_manifest_bytes(
        root=args.root,
        task_file=args.successor_task,
        target=args.new_target,
        manager_target=args.manager_target,
        workdir=Path(str(value.get("workdir", ""))),
        model=str(value.get("model", "")),
        reasoning_effort=str(value.get("reasoning_effort", "")),
        prompt=read_frozen_prompt(args.prompt_file).data,
        codex_home=Path(str(value.get("codex_home", ""))),
        launch_token=str(value.get("launch_token", "")),
    )
    if data != expected:
        raise DualSuccessorError("launch manifest is not the canonical exact Codex launch binding")
    return value


def task_done(data: bytes, root: Path) -> bytes:
    text = data.decode()
    cleared = render_pending_items(text, ())
    return update_frontmatter_status(cleared, "done", "", root).encode()


def successor_bytes(canonical_data: bytes, canonical_metadata: TaskMetadata, args: PrepareArgs) -> bytes:
    text = canonical_data.decode()
    successor = replace_v1_fields(text, status="blocked", runat=args.new_target, blocked_on=BLOCKER, remove_session=True)
    successor = render_pending_items(successor, args.expected_pending_items)
    prompt = read_frozen_prompt(args.prompt_file).data.decode()
    if "</manager_delegation>" in prompt.casefold():
        raise DualSuccessorError("prompt contains reserved manager-delegation closing text")
    successor += ("" if successor.endswith("\n") else "\n") + (
        f'<prepared_dual_worker_successor version="{VERSION}" prompt-sha256="{args.prompt_sha256}" '
        f'queue-sha256="{args.queue_sha256}" custody-sha256="{args.custody_sha256}" />\n'
        f'<manager_delegation from="{args.manager_target}">\n{prompt.rstrip()}\n</manager_delegation>\n'
    )
    result = parse_task_metadata(successor, args.root)
    if (
        result is None
        or result.status != "blocked"
        or result.blocked_on != BLOCKER
        or result.runat != args.new_target
        or result.managerat != args.manager_target
        or result.tool != "codex"
        or result.is_manager
        or result.session_id
        or result.pending_task_items != args.expected_pending_items
        or canonical_metadata.pending_task_items != args.expected_pending_items
    ):
        raise DualSuccessorError("successor construction lost an exact lifecycle binding")
    return successor.encode()


def todo_after(data: bytes, root: Path, source_paths: tuple[Path, Path], successor_path: Path, old_target: str, new_target: str) -> bytes:
    try:
        text = data.decode()
    except UnicodeDecodeError as exc:
        raise DualSuccessorError("TODO is not UTF-8") from exc
    lines = text.splitlines(keepends=True)
    contents = [line.rstrip("\r\n") for line in lines]
    heading_names = ("current", "human pending", "low priority", "previous")
    headings = {name: [index for index, item in enumerate(contents) if item == f"{name}:"] for name in heading_names}
    if any(len(value) != 1 for value in headings.values()) or tuple(headings[name][0] for name in heading_names) != tuple(sorted(headings[name][0] for name in heading_names)):
        raise DualSuccessorError("TODO lifecycle headings are not canonical")
    source_rows: list[tuple[int, Path]] = []
    successor_rows: list[int] = []
    row_re = re.compile(r"^\s*([^\s]+\.md)\s+([^\s]+)\s*$")
    for index, content in enumerate(contents):
        match = row_re.fullmatch(content)
        if match is None:
            continue
        lexical = root.joinpath(*Path(match.group(1)).parts)
        resolved = lexical.resolve(strict=False)
        if resolved in source_paths:
            if canonical_target(match.group(2)) != old_target:
                raise DualSuccessorError("source TODO row target drifted")
            if not (headings["current"][0] < index < headings["low priority"][0]):
                raise DualSuccessorError("source TODO rows must be current or Human-pending")
            source_rows.append((index, resolved))
        elif resolved == successor_path:
            successor_rows.append(index)
    if not source_rows or len(source_rows) > 2 or len({path for _, path in source_rows}) != len(source_rows) or successor_rows:
        raise DualSuccessorError("TODO must contain one or two unique source rows and no successor row")
    ending = "\r\n" if "\r\n" in text else "\n"
    first = min(index for index, _path in source_rows)
    successor_ref = successor_path.relative_to(root).as_posix()
    lines[first] = f"{successor_ref} {new_target}{ending}"
    for index, _path in sorted(source_rows, reverse=True):
        if index != first:
            del lines[index]
    result = "".join(lines)
    result_lines = result.splitlines(keepends=True)
    previous_index = next(index for index, line in enumerate(result_lines) if line.rstrip("\r\n") == "previous:")
    existing = {line.rstrip("\r\n") for line in result_lines[previous_index + 1 :]}
    inserts = [f"{path.relative_to(root).as_posix()} {old_target}" for path in source_paths]
    if any(item in existing for item in inserts):
        raise DualSuccessorError("TODO already contains a source-history row")
    result_lines[previous_index + 1 : previous_index + 1] = [item + ending for item in inserts]
    final = "".join(result_lines)
    if final.count(f"{successor_ref} {new_target}") != 1 or any(final.count(item) != 1 for item in inserts):
        raise DualSuccessorError("TODO normalization lost or duplicated ownership history")
    return final.encode()


def validate_source(metadata_value: TaskMetadata, *, args: PrepareArgs, queue: tuple[str, ...], label: str) -> None:
    if (
        metadata_value.version != TASK_FRONTMATTER_V1
        or metadata_value.status == "done"
        or metadata_value.is_manager
        or canonical_target(metadata_value.runat) != args.old_target
        or canonical_target(metadata_value.managerat) != args.manager_target
        or metadata_value.tool != "codex"
        or metadata_value.pending_task_items != queue
    ):
        raise DualSuccessorError(f"{label} does not match its exact role, owner, target, tool, and queue")


def any_task_record_at_target(root: Path, target: str) -> tuple[Path, ...]:
    """Return every versioned task record, including done history, at one target."""

    matches: list[Path] = []
    for path in markdown_paths(root):
        try:
            parsed = parse_task_metadata(path.read_text(encoding="utf-8"), root)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise DualSuccessorError(f"cannot authenticate Markdown membership at {path}: {exc}") from exc
        if parsed is not None and parsed.runat != "retired" and canonical_target(parsed.runat) == target:
            matches.append(path)
    return tuple(matches)


def build_plan(args: PrepareArgs) -> PreparePlan:
    require_root(args)
    shadow_path = task_path(args.root, args.shadow_task)
    canonical_path = task_path(args.root, args.canonical_task)
    successor_path = task_path(args.root, args.successor_task)
    todo_path = args.root / "TODO.md"
    if successor_path.exists() or successor_path.is_symlink():
        raise DualSuccessorError("successor task path already exists")
    shadow = read_snapshot(shadow_path, "shadow source task")
    canonical = read_snapshot(canonical_path, "canonical source task")
    todo = read_snapshot(todo_path, "TODO")
    prompt = safe_external_snapshot(args.prompt_file, "successor prompt")
    manifest = read_launch_manifest(args.launch_manifest)
    instructions = safe_external_snapshot(args.instructions_file, "inactive instructions", exact_mode=0o400)
    approval = safe_external_snapshot(args.approval_file, "Human approval record", exact_mode=0o400)
    expected_digests = (
        (shadow.data, args.shadow_sha256, "shadow source"),
        (canonical.data, args.canonical_sha256, "canonical source"),
        (todo.data, args.todo_sha256, "TODO"),
        (prompt.data, args.prompt_sha256, "prompt"),
        (manifest.data, args.launch_manifest_sha256, "launch manifest"),
        (instructions.data, args.instructions_sha256, "instructions"),
        (approval.data, args.approval_sha256, "approval"),
    )
    if any(digest(data) != expected for data, expected, _label in expected_digests):
        labels = ", ".join(label for data, expected, label in expected_digests if digest(data) != expected)
        raise DualSuccessorError(f"bound bytes changed: {labels}")
    manifest_config = validated_manifest(manifest.data, args)
    _ = approval_record(
        approval,
        root=args.root,
        instructions=instructions,
        manifest=manifest_config,
        custody_sha256=args.custody_sha256,
    )
    shadow_metadata = metadata(shadow.data, args.root, "shadow source task")
    canonical_metadata = metadata(canonical.data, args.root, "canonical source task")
    validate_source(shadow_metadata, args=args, queue=(), label="shadow source")
    validate_source(canonical_metadata, args=args, queue=args.expected_pending_items, label="canonical source")
    if queue_digest(canonical_metadata.pending_task_items) != args.queue_sha256 or not args.expected_pending_items:
        raise DualSuccessorError("canonical source queue does not match the exact nonempty queue binding")
    if has_pending_marker(shadow.data.decode()) or has_pending_marker(canonical.data.decode()):
        raise DualSuccessorError("a source task has a pending delivery marker")
    inventory = pinned_pane_inventory(manifest_config=manifest_config)
    if args.old_target in inventory or args.new_target in inventory:
        raise DualSuccessorError("old and new targets must both be absent before reconciliation")
    source_paths = tuple(sorted((shadow_path.resolve(), canonical_path.resolve()), key=str))
    if authoritative_active_target_task_paths(args.root, args.old_target) != source_paths or active_owners(args.root, args.old_target, {}) != source_paths:
        raise DualSuccessorError("the two sources are not the exact authoritative old-target owners")
    if authoritative_active_target_task_paths(args.root, args.new_target) or active_owners(args.root, args.new_target, {}):
        raise DualSuccessorError("the fresh target already has an ownership claim")
    if any_task_record_at_target(args.root, args.new_target):
        raise DualSuccessorError("the fresh target is not unused across task history")
    shadow_after = task_done(shadow.data, args.root)
    canonical_after = task_done(canonical.data, args.root)
    successor_data = successor_bytes(canonical.data, canonical_metadata, args)
    normalized_todo = todo_after(todo.data, args.root, (shadow_path, canonical_path), successor_path, args.old_target, args.new_target)
    overrides = {
        shadow_path.resolve(): shadow_after,
        canonical_path.resolve(): canonical_after,
        successor_path.resolve(strict=False): successor_data,
    }
    if active_owners(args.root, args.old_target, overrides):
        raise DualSuccessorError("candidate state retained an old-target active owner")
    if active_owners(args.root, args.new_target, overrides) != (successor_path.resolve(strict=False),):
        raise DualSuccessorError("candidate state does not have exactly one distinct-target successor owner")
    return PreparePlan(
        shadow,
        canonical,
        todo,
        prompt,
        manifest,
        instructions,
        approval,
        successor_path,
        shadow_after,
        canonical_after,
        normalized_todo,
        successor_data,
        markdown_paths(args.root),
    )


def binding_fields(args: PrepareArgs) -> dict[str, object]:
    return {
        "version": VERSION,
        "operation": OPERATION,
        "root": str(args.root),
        "shadow_task": args.shadow_task,
        "canonical_task": args.canonical_task,
        "successor_task": args.successor_task,
        "old_target": args.old_target,
        "new_target": args.new_target,
        "manager_target": args.manager_target,
        "shadow_sha256": args.shadow_sha256,
        "canonical_sha256": args.canonical_sha256,
        "todo_sha256": args.todo_sha256,
        "queue": list(args.expected_pending_items),
        "queue_sha256": args.queue_sha256,
        "prompt_path": str(args.prompt_file),
        "prompt_sha256": args.prompt_sha256,
        "manifest_path": str(args.launch_manifest),
        "manifest_sha256": args.launch_manifest_sha256,
        "instructions_path": str(args.instructions_file),
        "instructions_sha256": args.instructions_sha256,
        "approval_path": str(args.approval_file),
        "approval_sha256": args.approval_sha256,
        "protected_targets": list(args.protected_targets),
        "protected_sha256": args.protected_sha256,
        "custody_sha256": args.custody_sha256,
        "journal": str(args.journal),
    }


def journal_record(args: PrepareArgs, plan: PreparePlan, phase: str) -> dict[str, object]:
    approval_value = json.loads(plan.approval.data)
    if not isinstance(approval_value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in approval_value.items()):
        raise DualSuccessorError("approval record cannot form an authority binding")
    bound_authority = authority_binding(
        {str(key): str(item) for key, item in approval_value.items()},
        custody_sha256=args.custody_sha256,
    )
    value = {
        **binding_fields(args),
        "phase": phase,
        "shadow_before": encoded(plan.shadow.data),
        "shadow_after": encoded(plan.shadow_after),
        "canonical_before": encoded(plan.canonical.data),
        "canonical_after": encoded(plan.canonical_after),
        "todo_before": encoded(plan.todo.data),
        "todo_after": encoded(plan.todo_after),
        "successor_data": encoded(plan.successor_data),
        "prompt_data": encoded(plan.prompt.data),
        "manifest_data": encoded(plan.manifest.data),
        "instructions_data": encoded(plan.instructions.data),
        "approval_data": encoded(plan.approval.data),
        "authority_binding": authority_binding_fields(bound_authority),
        "source_mode": stat.S_IMODE(plan.canonical.state.st_mode),
        "source_gid": plan.canonical.state.st_gid,
        "shadow_mode": stat.S_IMODE(plan.shadow.state.st_mode),
        "shadow_gid": plan.shadow.state.st_gid,
        "todo_mode": stat.S_IMODE(plan.todo.state.st_mode),
        "todo_gid": plan.todo.state.st_gid,
        "initial_markdown_paths": [str(path) for path in plan.initial_markdown_paths],
    }
    value["commitment_sha256"] = digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    return value


def parse_record(snapshot: Snapshot, args: PrepareArgs) -> dict[str, object]:
    if stat.S_IMODE(snapshot.state.st_mode) != 0o600 or snapshot.state.st_uid != os.getuid():
        raise DualSuccessorError("journal must remain owner-private mode 0600")
    try:
        value = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DualSuccessorError(f"journal is invalid: {exc}") from exc
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in binding_fields(args).items()):
        raise DualSuccessorError("journal invocation binding changed")
    if value.get("phase") not in PREPARE_PHASES:
        raise DualSuccessorError("journal phase is invalid")
    commitment = value.get("commitment_sha256")
    unsigned = {key: item for key, item in value.items() if key != "commitment_sha256"}
    if not isinstance(commitment, str) or digest(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()) != commitment:
        raise DualSuccessorError("journal commitment changed")
    return value


def plan_from_record(args: PrepareArgs, record: dict[str, object], *, authenticate_approval: bool = True) -> PreparePlan:
    shadow_path = task_path(args.root, args.shadow_task)
    canonical_path = task_path(args.root, args.canonical_task)
    todo_path = args.root / "TODO.md"
    paths = record.get("initial_markdown_paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise DualSuccessorError("journal Markdown inventory is invalid")

    def placeholder(path: Path, data: bytes, mode: int, gid: int) -> Snapshot:
        state = os.stat_result((stat.S_IFREG | mode, 0, 0, 0, os.getuid(), gid, len(data), 0, 0, 0))
        return Snapshot(path, data, state)

    shadow_before = decoded(record.get("shadow_before"), "shadow_before")
    canonical_before = decoded(record.get("canonical_before"), "canonical_before")
    todo_before = decoded(record.get("todo_before"), "todo_before")
    plan = PreparePlan(
        placeholder(shadow_path, shadow_before, required_int(record, "shadow_mode"), required_int(record, "shadow_gid")),
        placeholder(canonical_path, canonical_before, required_int(record, "source_mode"), required_int(record, "source_gid")),
        placeholder(todo_path, todo_before, required_int(record, "todo_mode"), required_int(record, "todo_gid")),
        placeholder(args.prompt_file, decoded(record.get("prompt_data"), "prompt_data"), 0o600, os.getgid()),
        placeholder(args.launch_manifest, decoded(record.get("manifest_data"), "manifest_data"), 0o600, os.getgid()),
        placeholder(args.instructions_file, decoded(record.get("instructions_data"), "instructions_data"), 0o400, os.getgid()),
        placeholder(args.approval_file, decoded(record.get("approval_data"), "approval_data"), 0o400, os.getgid()),
        task_path(args.root, args.successor_task),
        decoded(record.get("shadow_after"), "shadow_after"),
        decoded(record.get("canonical_after"), "canonical_after"),
        decoded(record.get("todo_after"), "todo_after"),
        decoded(record.get("successor_data"), "successor_data"),
        tuple(Path(path) for path in paths),
    )
    for data, expected, label in (
        (shadow_before, args.shadow_sha256, "shadow source"),
        (canonical_before, args.canonical_sha256, "canonical source"),
        (todo_before, args.todo_sha256, "TODO"),
        (plan.prompt.data, args.prompt_sha256, "prompt"),
        (plan.manifest.data, args.launch_manifest_sha256, "manifest"),
        (plan.instructions.data, args.instructions_sha256, "instructions"),
        (plan.approval.data, args.approval_sha256, "approval"),
    ):
        if digest(data) != expected:
            raise DualSuccessorError(f"journal {label} bytes do not match their bound digest")
    manifest_config = validated_manifest(plan.manifest.data, args)
    _ = approval_record(
        safe_external_snapshot(args.approval_file, "Human approval record", exact_mode=0o400),
        root=args.root,
        instructions=plan.instructions,
        manifest=manifest_config,
        custody_sha256=args.custody_sha256,
        authenticate=authenticate_approval,
    )
    approval_value = json.loads(plan.approval.data)
    if not isinstance(approval_value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in approval_value.items()):
        raise DualSuccessorError("journal approval record is invalid")
    expected_authority = authority_binding_fields(
        authority_binding(
            {str(key): str(item) for key, item in approval_value.items()},
            custody_sha256=args.custody_sha256,
        )
    )
    if record.get("authority_binding") != expected_authority:
        raise DualSuccessorError("journal authoritative-source binding changed")
    current_instructions = safe_external_snapshot(args.instructions_file, "inactive instructions", exact_mode=0o400)
    if current_instructions.data != plan.instructions.data:
        raise DualSuccessorError("current inactive instruction bytes differ from their journal binding")
    shadow_metadata = metadata(shadow_before, args.root, "journal shadow")
    canonical_metadata = metadata(canonical_before, args.root, "journal canonical")
    validate_source(shadow_metadata, args=args, queue=(), label="journal shadow")
    validate_source(canonical_metadata, args=args, queue=args.expected_pending_items, label="journal canonical")
    if (
        plan.shadow_after != task_done(shadow_before, args.root)
        or plan.canonical_after != task_done(canonical_before, args.root)
        or plan.successor_data != successor_bytes(canonical_before, canonical_metadata, args)
        or plan.todo_after != todo_after(todo_before, args.root, (shadow_path, canonical_path), plan.successor_path, args.old_target, args.new_target)
    ):
        raise DualSuccessorError("journal after-state is not canonical from its bound before-state")
    return plan


def transition(journal: Snapshot, args: PrepareArgs, plan: PreparePlan, phase: str) -> Snapshot:
    return replace_snapshot(journal, canonical_json(journal_record(args, plan, phase)), "dual-successor journal")


def maybe_crash(phase: str) -> None:
    if os.environ.get("OMO_DUAL_SUCCESSOR_CRASH_AFTER") == phase:
        os._exit(86)


def current_bytes(path: Path, *, absent_ok: bool = False) -> bytes | None:
    if absent_ok and not path.exists() and not path.is_symlink():
        return None
    return read_snapshot(path, path.name).data


def pinned_pane_inventory(*, manifest_config: dict[str, object]) -> dict[str, Pane]:
    tmux_value = manifest_config.get("tmux_runtime")
    tmux_env = manifest_config.get("tmux_environment")
    if not isinstance(tmux_value, dict) or not isinstance(tmux_env, dict):
        raise DualSuccessorError("manifest lost its tmux binding")
    path = Path(str(tmux_value.get("tmux_path", "")))
    snapshot = read_pinned_system_executable(path, "tmux client")
    if digest(snapshot.data) != tmux_value.get("tmux_sha256") or {str(k): str(v) for k, v in tmux_env.items()} != minimal_tmux_environment():
        raise DualSuccessorError("pinned tmux runtime or environment changed")
    result = subprocess.run(
        [str(path), "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=minimal_tmux_environment(),
        cwd=Path("/"),
    )
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())
        raise DualSuccessorError(f"pinned tmux inventory failed: {detail or result.returncode}")
    inventory: dict[str, Pane] = {}
    for row in result.stdout.splitlines():
        fields = row.split("\t")
        if len(fields) != 4 or re.fullmatch(r"%\d+", fields[1]) is None or not fields[2].isdigit() or fields[3] not in {"0", "1"}:
            raise DualSuccessorError("pinned tmux inventory returned a malformed row")
        if fields[3] == "1":
            continue
        target = canonical_target(fields[0])
        ticks = process_start_ticks(int(fields[2]))
        if ticks is None or target in inventory:
            raise DualSuccessorError("pinned tmux inventory cannot prove unique pane identity")
        inventory[target] = Pane(target, fields[1], int(fields[2]), ticks)
    return inventory


def prove_prepared(args: PrepareArgs, plan: PreparePlan, journal: Snapshot) -> None:
    if parse_record(journal, args)["phase"] != "committed":
        raise DualSuccessorError("final journal is not committed")
    if current_bytes(plan.shadow.path) != plan.shadow_after or current_bytes(plan.canonical.path) != plan.canonical_after:
        raise DualSuccessorError("final source task bytes do not match the journal")
    if current_bytes(plan.todo.path) != plan.todo_after or current_bytes(plan.successor_path) != plan.successor_data:
        raise DualSuccessorError("final TODO or successor bytes do not match the journal")
    if read_frozen_prompt(args.prompt_file).data != plan.prompt.data or read_launch_manifest(args.launch_manifest).data != plan.manifest.data:
        raise DualSuccessorError("final prompt or manifest bytes changed")
    instructions = safe_external_snapshot(args.instructions_file, "inactive instructions", exact_mode=0o400)
    approval = safe_external_snapshot(args.approval_file, "Human approval record", exact_mode=0o400)
    if instructions.data != plan.instructions.data or approval.data != plan.approval.data:
        raise DualSuccessorError("final instruction or approval bytes changed")
    manifest_config = validated_manifest(plan.manifest.data, args)
    _ = approval_record(
        approval,
        root=args.root,
        instructions=instructions,
        manifest=manifest_config,
        custody_sha256=args.custody_sha256,
    )
    successor = metadata(plan.successor_data, args.root, "final successor")
    if successor.status != "blocked" or successor.pending_task_items != args.expected_pending_items:
        raise DualSuccessorError("final successor is not blocked with the exact queue")
    if authoritative_active_target_task_paths(args.root, args.old_target) or active_owners(args.root, args.old_target, {}):
        raise DualSuccessorError("old target retained an active owner")
    expected_owner = (plan.successor_path.resolve(),)
    if authoritative_active_target_task_paths(args.root, args.new_target) != expected_owner or active_owners(args.root, args.new_target, {}) != expected_owner:
        raise DualSuccessorError("fresh target does not have exactly one prepared owner")
    inventory = pinned_pane_inventory(manifest_config=manifest_config)
    if args.old_target in inventory or args.new_target in inventory:
        raise DualSuccessorError("old or new target became live during preparation")
    expected_paths = tuple(sorted((*plan.initial_markdown_paths, plan.successor_path.resolve()), key=str))
    if markdown_paths(args.root) != expected_paths:
        raise DualSuccessorError("Markdown membership changed during transaction")


def apply_plan(args: PrepareArgs, plan: PreparePlan, journal: Snapshot) -> Snapshot:
    config = validated_manifest(plan.manifest.data, args)
    if read_frozen_prompt(args.prompt_file).data != plan.prompt.data or read_launch_manifest(args.launch_manifest).data != plan.manifest.data:
        raise DualSuccessorError("prompt or manifest changed during recovery")
    instructions = safe_external_snapshot(args.instructions_file, "inactive instructions", exact_mode=0o400)
    approval = safe_external_snapshot(args.approval_file, "Human approval record", exact_mode=0o400)
    if instructions.data != plan.instructions.data or approval.data != plan.approval.data:
        raise DualSuccessorError("instructions or approval changed during recovery")
    _ = approval_record(
        approval,
        root=args.root,
        instructions=instructions,
        manifest=config,
        custody_sha256=args.custody_sha256,
    )
    inventory = pinned_pane_inventory(manifest_config=config)
    if args.old_target in inventory or args.new_target in inventory:
        raise DualSuccessorError("a bound target became live during recovery")
    steps = (
        ("shadow", plan.shadow.path, plan.shadow.data, plan.shadow_after),
        ("canonical", plan.canonical.path, plan.canonical.data, plan.canonical_after),
        ("todo", plan.todo.path, plan.todo.data, plan.todo_after),
    )
    for phase, path, before, after in steps:
        current = current_bytes(path)
        if current not in {before, after}:
            raise DualSuccessorError(f"recovery found unknown {phase} bytes")
        if current == before:
            _ = replace_snapshot(read_snapshot(path, phase), after, phase)
        journal = transition(journal, args, plan, phase)
        maybe_crash(phase)
    successor = current_bytes(plan.successor_path, absent_ok=True)
    if successor not in {None, plan.successor_data}:
        raise DualSuccessorError("recovery found unknown successor bytes")
    if successor is None:
        _ = create_snapshot(plan.successor_path, plan.successor_data, stat.S_IMODE(plan.canonical.state.st_mode), plan.canonical.state.st_gid)
    journal = transition(journal, args, plan, "successor")
    maybe_crash("successor")
    journal = transition(journal, args, plan, "committed")
    maybe_crash("committed")
    prove_prepared(args, plan, journal)
    return journal


def prepare_successor(args: PrepareArgs) -> str:
    require_root(args)
    if custody_digest(args) != args.custody_sha256:
        raise DualSuccessorError("custody digest does not match the complete invocation")
    shadow_path = task_path(args.root, args.shadow_task)
    canonical_path = task_path(args.root, args.canonical_task)
    successor_path = task_path(args.root, args.successor_task)
    files = (
        shadow_path,
        canonical_path,
        successor_path,
        args.root / "TODO.md",
        args.prompt_file,
        args.launch_manifest,
        args.instructions_file,
        args.approval_file,
        args.journal,
    )
    with ExitStack() as locks:
        locks.enter_context(root_membership_lock(args.root))
        for target in sorted((args.old_target, args.new_target)):
            locks.enter_context(task_target_lock(args.root, target))
        for path in sorted(files, key=str):
            locks.enter_context(task_file_lock(path))
        if args.journal.exists() or args.journal.is_symlink():
            journal = read_snapshot(args.journal, "dual-successor journal")
            record = parse_record(journal, args)
            plan = plan_from_record(args, record)
        else:
            plan = build_plan(args)
            journal = create_snapshot(args.journal, canonical_json(journal_record(args, plan, "prepared")), 0o600)
        maybe_crash("prepared")
        journal = apply_plan(args, plan, journal)
    return (
        f"prepared dual-record successor {args.successor_task} at distinct target {args.new_target}; "
        f"task-sha256={digest(plan.successor_data)}; queue-sha256={args.queue_sha256}; "
        f"manifest-sha256={args.launch_manifest_sha256}; journal-sha256={digest(journal.data)}"
    )


def binding_from_journal(
    journal_path: Path,
    *,
    expected_journal_sha256: str,
    expected_task_sha256: str,
    expected_prompt_sha256: str,
    expected_queue_sha256: str,
    expected_manifest_sha256: str,
) -> PreparedBinding:
    for value, label in (
        (expected_journal_sha256, "journal"),
        (expected_task_sha256, "task"),
        (expected_prompt_sha256, "prompt"),
        (expected_queue_sha256, "queue"),
        (expected_manifest_sha256, "manifest"),
    ):
        require_sha(value, label)
    journal = read_snapshot(journal_path, "committed dual-successor journal")
    if stat.S_IMODE(journal.state.st_mode) != 0o600 or digest(journal.data) != expected_journal_sha256:
        raise DualSuccessorError("committed journal mode or digest changed")
    try:
        record = json.loads(journal.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DualSuccessorError("committed journal is invalid") from exc
    if not isinstance(record, dict) or record.get("version") != VERSION or record.get("operation") != OPERATION or record.get("phase") != "committed":
        raise DualSuccessorError("journal is not one committed dual-successor transaction")
    commitment = record.get("commitment_sha256")
    unsigned = {key: item for key, item in record.items() if key != "commitment_sha256"}
    if not isinstance(commitment, str) or digest(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()) != commitment:
        raise DualSuccessorError("committed journal commitment changed")
    root = Path(str(record["root"]))
    if root.resolve(strict=True) != root or journal_path.resolve(strict=True) != journal.path or Path(str(record.get("journal", ""))) != journal.path:
        raise DualSuccessorError("committed journal path or root identity changed")
    shadow_path = task_path(root, str(record["shadow_task"]))
    canonical_path = task_path(root, str(record["canonical_task"]))
    successor_path = task_path(root, str(record["successor_task"]))
    queue_raw = record.get("queue")
    if not isinstance(queue_raw, list) or not queue_raw or not all(isinstance(item, str) for item in queue_raw):
        raise DualSuccessorError("journal queue is invalid")
    queue = tuple(queue_raw)
    protected_raw = record.get("protected_targets")
    if not isinstance(protected_raw, list) or not all(isinstance(item, str) for item in protected_raw):
        raise DualSuccessorError("journal protected target set is invalid")
    reconstructed = PrepareArgs(
        root,
        str(record["shadow_task"]),
        str(record["canonical_task"]),
        str(record["successor_task"]),
        canonical_target(str(record["old_target"])),
        canonical_target(str(record["new_target"])),
        canonical_target(str(record["manager_target"])),
        str(record["shadow_sha256"]),
        str(record["canonical_sha256"]),
        str(record["todo_sha256"]),
        queue,
        str(record["queue_sha256"]),
        Path(str(record["prompt_path"])),
        str(record["prompt_sha256"]),
        Path(str(record["manifest_path"])),
        str(record["manifest_sha256"]),
        Path(str(record["instructions_path"])),
        str(record["instructions_sha256"]),
        Path(str(record["approval_path"])),
        str(record["approval_sha256"]),
        tuple(canonical_target(str(item)) for item in protected_raw),
        str(record["protected_sha256"]),
        str(record["custody_sha256"]),
        journal.path,
    )
    require_root(reconstructed)
    if custody_digest(reconstructed) != reconstructed.custody_sha256:
        raise DualSuccessorError("committed journal custody binding changed")
    record = parse_record(journal, reconstructed)
    plan = plan_from_record(reconstructed, record, authenticate_approval=False)
    prompt_data = plan.prompt.data
    manifest_data = plan.manifest.data
    successor_data = plan.successor_data
    checks = (
        (digest(journal.data), expected_journal_sha256, "journal"),
        (digest(successor_data), expected_task_sha256, "task"),
        (digest(prompt_data), expected_prompt_sha256, "prompt"),
        (queue_digest(queue), expected_queue_sha256, "queue"),
        (digest(manifest_data), expected_manifest_sha256, "manifest"),
    )
    if any(actual != expected for actual, expected, _label in checks):
        raise DualSuccessorError("one or more immutable launch handoff digests do not match")
    try:
        manifest = json.loads(manifest_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DualSuccessorError("bound launch manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise DualSuccessorError("bound launch manifest is not an object")
    approval_value = json.loads(plan.approval.data)
    if not isinstance(approval_value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in approval_value.items()):
        raise DualSuccessorError("bound approval record is invalid")
    bound_authority = authority_binding(
        {str(key): str(item) for key, item in approval_value.items()},
        custody_sha256=reconstructed.custody_sha256,
    )
    binding = PreparedBinding(
        journal,
        reconstructed,
        root,
        shadow_path,
        canonical_path,
        successor_path,
        root / "TODO.md",
        canonical_target(str(record["old_target"])),
        canonical_target(str(record["new_target"])),
        canonical_target(str(record["manager_target"])),
        queue,
        str(record["queue_sha256"]),
        Path(str(record["prompt_path"])),
        prompt_data,
        str(record["prompt_sha256"]),
        Path(str(record["manifest_path"])),
        manifest_data,
        str(record["manifest_sha256"]),
        manifest,
        Path(str(record["instructions_path"])),
        decoded(record.get("instructions_data"), "instructions_data"),
        str(record["instructions_sha256"]),
        Path(str(record["approval_path"])),
        decoded(record.get("approval_data"), "approval_data"),
        str(record["approval_sha256"]),
        bound_authority,
        reconstructed.protected_targets,
        decoded(record.get("shadow_after"), "shadow_after"),
        decoded(record.get("canonical_after"), "canonical_after"),
        decoded(record.get("todo_after"), "todo_after"),
        successor_data,
    )
    return binding


def launch_receipt_path(journal: Path) -> Path:
    return journal.with_name(f".{journal.name}.launch")


def protected_inventory_sha256(binding: PreparedBinding, inventory: dict[str, Pane]) -> str:
    value = [
        [target, None] if inventory.get(target) is None else [target, inventory[target].pane_id, inventory[target].pane_pid, inventory[target].start_ticks] for target in binding.protected_targets
    ]
    return digest(json.dumps(value, separators=(",", ":")).encode())


def authority_evidence_fields(evidence: AuthorityEvidence | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "outcome": evidence.outcome,
        "mailbox_identity_sha256": evidence.mailbox_identity_sha256,
        "observed_sequence": evidence.observed_sequence,
        "controlling_uid": evidence.controlling_uid,
        "controlling_message_id": evidence.controlling_message_id,
        "controlling_thread_id": evidence.controlling_thread_id,
        "controlling_internaldate_unix_ms": evidence.controlling_internaldate_unix_ms,
        "controlling_raw_mime_sha256": evidence.controlling_raw_mime_sha256,
        "controlling_rfc_message_id": evidence.controlling_rfc_message_id,
    }


def authority_commit_fields(commit: AuthorityCommit | None) -> dict[str, object] | None:
    if commit is None:
        return None
    return {
        "mailbox_identity_sha256": commit.mailbox_identity_sha256,
        "sequence": commit.sequence,
        "gmail_uid": commit.gmail_uid,
        "gmail_message_id": commit.gmail_message_id,
        "gmail_thread_id": commit.gmail_thread_id,
        "gmail_internaldate_unix_ms": commit.gmail_internaldate_unix_ms,
        "raw_mime_sha256": commit.raw_mime_sha256,
        "rfc_message_id": commit.rfc_message_id,
    }


def authority_evidence_from_record(value: object, binding: PreparedBinding) -> AuthorityEvidence:
    if not isinstance(value, dict) or set(value) != set(authority_evidence_fields(AuthorityEvidence("unknown", "0" * 64, 1, 0, "", "", 0, "", "")) or {}):
        raise DualSuccessorError("launch receipt authority evidence is malformed")
    evidence = AuthorityEvidence(
        str(value["outcome"]),
        str(value["mailbox_identity_sha256"]),
        required_int(value, "observed_sequence"),
        required_int(value, "controlling_uid"),
        str(value["controlling_message_id"]),
        str(value["controlling_thread_id"]),
        required_int(value, "controlling_internaldate_unix_ms"),
        str(value["controlling_raw_mime_sha256"]),
        str(value["controlling_rfc_message_id"]),
    )
    if (
        evidence.outcome not in {"withdrawn", "unknown"}
        or evidence.mailbox_identity_sha256 != binding.authority_binding.mailbox_identity_sha256
        or evidence.observed_sequence <= binding.authority_binding.approval_uid
        or evidence.controlling_uid < 0
    ):
        raise DualSuccessorError("launch receipt authority evidence does not match its binding")
    if evidence.controlling_uid:
        for field, label in ((evidence.controlling_raw_mime_sha256, "controlling MIME"),):
            require_sha(field, label)
    return evidence


def authority_commit_from_record(value: object, binding: PreparedBinding, creation_capability: str) -> AuthorityCommit:
    if not isinstance(value, dict) or set(value) != set(authority_commit_fields(AuthorityCommit("0" * 64, 1, 1, "", "", 1, "0" * 64, "")) or {}):
        raise DualSuccessorError("launch receipt authority commit is malformed")
    commit = AuthorityCommit(
        str(value["mailbox_identity_sha256"]),
        required_int(value, "sequence"),
        required_int(value, "gmail_uid"),
        str(value["gmail_message_id"]),
        str(value["gmail_thread_id"]),
        required_int(value, "gmail_internaldate_unix_ms"),
        str(value["raw_mime_sha256"]),
        str(value["rfc_message_id"]),
    )
    require_sha(commit.raw_mime_sha256, "authority commit MIME")
    if (
        commit.mailbox_identity_sha256 != binding.authority_binding.mailbox_identity_sha256
        or commit.sequence != commit.gmail_uid
        or commit.gmail_uid <= binding.authority_binding.approval_uid
        or commit.rfc_message_id != authority_commit_rfc_id(binding.authority_binding, creation_capability)
    ):
        raise DualSuccessorError("launch receipt authority commit does not match its transaction")
    return commit


def receipt_record(
    binding: PreparedBinding,
    phase: str,
    *,
    protected_inventory_sha256: str,
    pane: Pane | None = None,
    process: ProcessProof | None = None,
    creation_capability: str = "",
    authority_evidence: AuthorityEvidence | None = None,
    authority_commit: AuthorityCommit | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "version": VERSION,
        "operation": OPERATION,
        "phase": phase,
        "journal_path": str(binding.journal.path),
        "journal_sha256": digest(binding.journal.data),
        "task_path": str(binding.successor_path),
        "task_sha256": digest(binding.successor_data),
        "prompt_sha256": binding.prompt_sha256,
        "queue_sha256": binding.queue_sha256,
        "manifest_sha256": binding.manifest_sha256,
        "target": binding.new_target,
        "launch_token": str(binding.manifest["launch_token"]),
        "protected_inventory_sha256": protected_inventory_sha256,
        "creation_capability": creation_capability,
        "pane_id": pane.pane_id if pane else "",
        "pane_pid": pane.pane_pid if pane else 0,
        "pane_start_ticks": pane.start_ticks if pane else 0,
        "process_pid": process.pid if process else 0,
        "process_argv_sha256": process.argv_sha256 if process else "",
        "process_environment_sha256": process.environment_sha256 if process else "",
        "process_start_ticks": process.start_ticks if process else 0,
        "process_group_id": process.process_group_id if process else 0,
        "authority_binding": authority_binding_fields(binding.authority_binding),
        "authority_evidence": authority_evidence_fields(authority_evidence),
        "authority_commit": authority_commit_fields(authority_commit),
    }
    value["commitment_sha256"] = digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    return value


def parse_receipt(snapshot: Snapshot, binding: PreparedBinding) -> dict[str, object]:
    if stat.S_IMODE(snapshot.state.st_mode) != 0o600 or snapshot.state.st_uid != os.getuid():
        raise DualSuccessorError("launch receipt must remain owner-private mode 0600")
    try:
        value = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DualSuccessorError("launch receipt is invalid") from exc
    if not isinstance(value, dict) or value.get("phase") not in LAUNCH_PHASES:
        raise DualSuccessorError("launch receipt phase is invalid")
    protected_sha = value.get("protected_inventory_sha256")
    if not isinstance(protected_sha, str) or SHA256_RE.fullmatch(protected_sha) is None:
        raise DualSuccessorError("launch receipt protected inventory digest is invalid")
    creation_capability = value.get("creation_capability")
    if not isinstance(creation_capability, str) or ((value["phase"] == "reserved" and creation_capability) or (value["phase"] != "reserved" and TOKEN_RE.fullmatch(creation_capability) is None)):
        raise DualSuccessorError("launch receipt creation capability is invalid for its phase")
    fixed = receipt_record(
        binding,
        str(value["phase"]),
        protected_inventory_sha256=protected_sha,
        creation_capability=creation_capability,
    )
    for key in (
        "version",
        "operation",
        "journal_path",
        "journal_sha256",
        "task_path",
        "task_sha256",
        "prompt_sha256",
        "queue_sha256",
        "manifest_sha256",
        "target",
        "launch_token",
        "protected_inventory_sha256",
        "creation_capability",
        "authority_binding",
    ):
        if value.get(key) != fixed[key]:
            raise DualSuccessorError("launch receipt immutable binding changed")
    phase = str(value["phase"])
    if phase in {"authority-blocked", "terminated", "withdrawn"}:
        evidence = value.get("authority_evidence")
        if not isinstance(evidence, dict) or evidence.get("outcome") not in {"withdrawn", "unknown"} or value.get("authority_commit") is not None:
            raise DualSuccessorError("launch receipt lacks durable blocked authority evidence")
    elif phase in {"authority", "committed"}:
        commit = value.get("authority_commit")
        if not isinstance(commit, dict) or commit.get("mailbox_identity_sha256") != binding.authority_binding.mailbox_identity_sha256 or value.get("authority_evidence") is not None:
            raise DualSuccessorError("launch receipt lacks its exact authority commit")
    elif value.get("authority_evidence") is not None or value.get("authority_commit") is not None:
        raise DualSuccessorError("launch receipt contains authority evidence before its authority phase")
    commitment = value.get("commitment_sha256")
    unsigned = {key: item for key, item in value.items() if key != "commitment_sha256"}
    if not isinstance(commitment, str) or digest(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()) != commitment:
        raise DualSuccessorError("launch receipt commitment changed")
    return value


def transition_receipt(
    receipt: Snapshot,
    binding: PreparedBinding,
    phase: str,
    pane: Pane,
    process: ProcessProof,
    protected_sha256: str,
    creation_capability: str,
    authority_evidence: AuthorityEvidence | None = None,
    authority_commit: AuthorityCommit | None = None,
) -> Snapshot:
    return replace_snapshot(
        receipt,
        canonical_json(
            receipt_record(
                binding,
                phase,
                pane=pane,
                process=process,
                protected_inventory_sha256=protected_sha256,
                creation_capability=creation_capability,
                authority_evidence=authority_evidence,
                authority_commit=authority_commit,
            )
        ),
        "dual-successor launch receipt",
    )


def maybe_crash_launch(phase: str) -> None:
    if os.environ.get("OMO_DUAL_SUCCESSOR_LAUNCH_CRASH_AFTER") == phase:
        os._exit(87)


def blocked_authority_task_bytes(binding: PreparedBinding, outcome: str) -> bytes:
    blocker = WITHDRAWN_BLOCKER if outcome == "withdrawn" else UNKNOWN_AUTHORITY_BLOCKER
    result = replace_v1_fields(
        running_task_bytes(binding).decode(),
        status="blocked",
        runat=binding.new_target,
        blocked_on=blocker,
        remove_session=True,
    ).encode()
    parsed = metadata(result, binding.root, "authority-blocked successor")
    if parsed.status != "blocked" or parsed.pending_task_items != binding.queue or parsed.runat != binding.new_target or parsed.blocked_on != blocker:
        raise DualSuccessorError("authority-blocked task construction lost its exact queue, target, or evidence")
    return result


def task_bytes_for_receipt_phase(
    binding: PreparedBinding,
    receipt_phase: str | None,
    authority_evidence: AuthorityEvidence | None = None,
) -> tuple[bytes, ...]:
    """Return the only successor bytes coherent with one durable receipt phase."""

    blocked = binding.successor_data
    running = running_task_bytes(binding)
    if receipt_phase is None or receipt_phase == "reserved":
        return (blocked,)
    if receipt_phase in {"process", "task", "authority-pending"}:
        return (blocked,)
    if receipt_phase == "authority":
        # The authoritative marker is the serialized launch boundary. A crash
        # may follow it before or after local publication of the running task.
        return (blocked, running)
    if receipt_phase == "committed":
        return (running,)
    if receipt_phase == "authority-blocked":
        if authority_evidence is None:
            raise DualSuccessorError("blocked authority receipt lacks evidence")
        return (blocked, running, blocked_authority_task_bytes(binding, authority_evidence.outcome))
    if receipt_phase == "terminated":
        if authority_evidence is None:
            raise DualSuccessorError("terminated authority receipt lacks evidence")
        return (blocked, running, blocked_authority_task_bytes(binding, authority_evidence.outcome))
    if receipt_phase == "withdrawn":
        if authority_evidence is None:
            raise DualSuccessorError("withdrawn authority receipt lacks evidence")
        return (blocked_authority_task_bytes(binding, authority_evidence.outcome),)
    raise DualSuccessorError("launch receipt phase is invalid for task custody")


def require_task_receipt_coherence(
    binding: PreparedBinding,
    receipt_phase: str | None,
    authority_evidence: AuthorityEvidence | None = None,
) -> bytes:
    current = current_bytes(binding.successor_path)
    if current not in task_bytes_for_receipt_phase(binding, receipt_phase, authority_evidence):
        phase = "absent" if receipt_phase is None else receipt_phase
        raise DualSuccessorError(f"successor task bytes are incoherent with launch receipt phase {phase}")
    return current


def reauthenticate_binding(
    binding: PreparedBinding,
    *,
    receipt_phase: str | None,
    authority_evidence: AuthorityEvidence | None = None,
    authenticate_authority: bool = True,
) -> None:
    if custody_digest(binding.prepare_args) != binding.prepare_args.custody_sha256:
        raise DualSuccessorError("exact PrepareArgs custody binding changed before launch")
    current_journal = read_snapshot(binding.journal.path, "committed dual-successor journal")
    if current_journal.data != binding.journal.data or stat.S_IMODE(current_journal.state.st_mode) != 0o600:
        raise DualSuccessorError("committed journal changed after immutable handoff")
    _ = require_task_receipt_coherence(binding, receipt_phase, authority_evidence)
    for path, expected, label in (
        (binding.shadow_path, binding.shadow_after, "shadow source"),
        (binding.canonical_path, binding.canonical_after, "canonical source"),
        (binding.todo_path, binding.todo_after, "TODO"),
        (binding.prompt_path, binding.prompt_data, "prompt"),
    ):
        if read_frozen_prompt(path).data != expected:
            raise DualSuccessorError(f"{label} changed after immutable handoff")
    manifest = read_launch_manifest(binding.manifest_path)
    instructions = safe_external_snapshot(binding.instructions_path, "inactive instructions", exact_mode=0o400)
    approval = safe_external_snapshot(binding.approval_path, "Human approval record", exact_mode=0o400)
    if manifest.data != binding.manifest_data or instructions.data != binding.instructions_data or approval.data != binding.approval_data:
        raise DualSuccessorError("manifest, instructions, or approval changed after immutable handoff")
    _ = approval_record(
        approval,
        root=binding.root,
        instructions=instructions,
        manifest=binding.manifest,
        custody_sha256=binding.prepare_args.custody_sha256,
        authenticate=authenticate_authority,
    )
    expected_manifest = launch_manifest_bytes(
        root=binding.root,
        task_file=binding.successor_path.relative_to(binding.root).as_posix(),
        target=binding.new_target,
        manager_target=binding.manager_target,
        workdir=Path(str(binding.manifest["workdir"])),
        model=str(binding.manifest["model"]),
        reasoning_effort=str(binding.manifest["reasoning_effort"]),
        prompt=binding.prompt_data,
        codex_home=Path(str(binding.manifest["codex_home"])),
        launch_token=str(binding.manifest["launch_token"]),
    )
    if expected_manifest != binding.manifest_data:
        raise DualSuccessorError("current launch boundary no longer matches the canonical bound manifest")
    if authoritative_active_target_task_paths(binding.root, binding.old_target) or active_owners(binding.root, binding.old_target, {}):
        raise DualSuccessorError("old target regained an active owner")
    owner = (binding.successor_path.resolve(),)
    if authoritative_active_target_task_paths(binding.root, binding.new_target) != owner or active_owners(binding.root, binding.new_target, {}) != owner:
        raise DualSuccessorError("successor is not the sole active owner")
    if any_task_record_at_target(binding.root, binding.new_target) != owner:
        raise DualSuccessorError("successor target is not historically unique to the prepared task")


def receipt_matches_process(value: dict[str, object], pane: Pane, process: ProcessProof) -> bool:
    return (
        value.get("pane_id") == pane.pane_id
        and value.get("pane_pid") == pane.pane_pid
        and value.get("pane_start_ticks") == pane.start_ticks
        and value.get("process_pid") == process.pid
        and value.get("process_argv_sha256") == process.argv_sha256
        and value.get("process_environment_sha256") == process.environment_sha256
        and value.get("process_start_ticks") == process.start_ticks
        and value.get("process_group_id") == process.process_group_id
    )


def running_task_bytes(binding: PreparedBinding) -> bytes:
    text = binding.successor_data.decode()
    result = replace_v1_fields(text, status="running", runat=binding.new_target, blocked_on="", remove_session=False).encode()
    parsed = metadata(result, binding.root, "running successor")
    if parsed.status != "running" or parsed.pending_task_items != binding.queue or parsed.runat != binding.new_target:
        raise DualSuccessorError("running-state construction lost its exact queue or target")
    return result


def canonical_environment(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise DualSuccessorError("manifest environment is invalid")
    return {str(key): str(item) for key, item in value.items()}


def prove_process(
    binding: PreparedBinding,
    pane: Pane,
    *,
    creation_capability: str,
    proc_root: Path = Path("/proc"),
    timeout_s: float = 10.0,
) -> ProcessProof:
    if TOKEN_RE.fullmatch(creation_capability) is None:
        raise DualSuccessorError("process proof requires the exact transaction creation capability")
    manifest = binding.manifest
    runtime = _runtime_from_codex_install(manifest.get("codex_install"))
    manifest_argv = required_string_list(manifest.get("argv"), "manifest argv")
    if manifest_argv != codex_argv(
        runtime,
        workdir=Path(str(manifest["workdir"])),
        model=str(manifest["model"]),
        reasoning_effort=str(manifest["reasoning_effort"]),
        prompt=binding.prompt_data.decode(),
    ):
        raise DualSuccessorError("manifest Codex argv is not canonical")
    expected_argv = manifest_argv
    expected_env = canonical_environment(manifest["environment"])
    expected_env["OMO_DUAL_CREATION_CAPABILITY"] = creation_capability
    expected_inventory = required_home_inventory(manifest.get("codex_home_inventory"))
    if directory_inventory(Path(str(manifest["codex_home"]))) != expected_inventory:
        raise DualSuccessorError("Codex home inventory changed")
    deadline = time.monotonic() + timeout_s
    last_count = -1
    while time.monotonic() < deadline:
        inventory = pinned_pane_inventory(manifest_config=manifest)
        current_pane = inventory.get(binding.new_target)
        if current_pane != pane:
            raise DualSuccessorError("distinct target pane identity changed")
        processes = read_processes(proc_root)
        matches: list[ProcessProof] = []
        for process in processes.values():
            if process.state == "Z" or not process_is_under(process.pid, pane.pane_pid, processes) or process.argv != expected_argv:
                continue
            try:
                executable = (proc_root / str(process.pid) / "exe").resolve(strict=True)
                raw_environment = (proc_root / str(process.pid) / "environ").read_bytes()
                cwd = (proc_root / str(process.pid) / "cwd").resolve(strict=True)
            except OSError:
                continue
            if executable != runtime or cwd != Path(str(manifest["workdir"])):
                continue
            parts = raw_environment.rstrip(b"\0").split(b"\0") if raw_environment else []
            environment: dict[str, str] = {}
            try:
                for item in parts:
                    key, separator, value = item.partition(b"=")
                    decoded_key, decoded_value = key.decode(), value.decode()
                    if not separator or not decoded_key or decoded_key in environment:
                        raise ValueError
                    environment[decoded_key] = decoded_value
            except (UnicodeDecodeError, ValueError) as exc:
                raise DualSuccessorError("Codex process environment is malformed") from exc
            if environment != expected_env:
                continue
            argv_sha = digest(b"\0".join(item.encode() for item in process.argv))
            env_sha = digest(b"\0".join(f"{key}={value}".encode() for key, value in sorted(environment.items())))
            ticks = process_start_ticks(process.pid)
            try:
                process_group_id = os.getpgid(process.pid)
            except ProcessLookupError:
                continue
            if ticks is None or process_group_id != process.pid:
                continue
            matches.append(ProcessProof(process.pid, executable, process.argv, argv_sha, env_sha, ticks, process_group_id))
        last_count = len(matches)
        if last_count == 1:
            return matches[0]
        if last_count > 1:
            break
        time.sleep(0.1)
    raise DualSuccessorError(f"target does not contain exactly one exact bound Codex process (matches={last_count})")


def before_target_create(_binding: PreparedBinding, _creation_capability: str) -> None:
    """Deterministic test hook at the final pre-create race boundary."""


def create_process(binding: PreparedBinding, *, creation_capability: str) -> Pane:
    if TOKEN_RE.fullmatch(creation_capability) is None:
        raise DualSuccessorError("process creation requires one unguessable capability")
    manifest = binding.manifest
    inventory = pinned_pane_inventory(manifest_config=manifest)
    if binding.new_target in inventory:
        raise DualSuccessorError("fresh target became occupied during creation; foreign state is preserved")
    session, window_pane = binding.new_target.split(":", 1)
    window, pane_index = window_pane.split(".", 1)
    if pane_index != "0" or any(target.startswith(f"{session}:{window}.") for target in inventory):
        raise DualSuccessorError("distinct launch target must be an absent pane-zero window")
    if not any(target.startswith(f"{session}:") for target in inventory):
        raise DualSuccessorError("distinct launch requires an existing non-Human tmux session")
    tmux_value = manifest["tmux_runtime"]
    if not isinstance(tmux_value, dict):
        raise DualSuccessorError("manifest tmux runtime is invalid")
    tmux_path = Path(str(tmux_value["tmux_path"]))
    env_path = Path(str(manifest["shell_runtime"]["env_path"])) if isinstance(manifest["shell_runtime"], dict) else Path()
    manifest_argv = required_string_list(manifest.get("argv"), "manifest argv")
    environment = canonical_environment(manifest["environment"])
    environment["OMO_DUAL_CREATION_CAPABILITY"] = creation_capability
    before_target_create(binding, creation_capability)
    result = subprocess.run(
        [
            str(tmux_path),
            "new-window",
            "-d",
            "-t",
            f"{session}:{window}",
            "-n",
            f"dual-{Path(binding.successor_path).stem}",
            "-c",
            str(manifest["workdir"]),
            "-P",
            "-F",
            "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_pid}",
            str(env_path),
            "-i",
            *(f"{key}={value}" for key, value in sorted(environment.items())),
            *manifest_argv,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=minimal_tmux_environment(),
        cwd=Path("/"),
    )
    if result.returncode != 0:
        raise DualSuccessorError(f"distinct target creation failed without cleanup: {(result.stderr or result.stdout).strip()}")
    fields = result.stdout.strip().split("\t")
    if len(fields) != 3 or canonical_target(fields[0]) != binding.new_target or re.fullmatch(r"%\d+", fields[1]) is None or not fields[2].isdigit():
        raise DualSuccessorError("distinct target creation returned ambiguous identity; state is preserved")
    ticks = process_start_ticks(int(fields[2]))
    if ticks is None:
        raise DualSuccessorError("distinct target process identity cannot be authenticated")
    return Pane(binding.new_target, fields[1], int(fields[2]), ticks)


def _proc_process_group(entry: Path) -> int:
    raw = (entry / "stat").read_text(encoding="utf-8")
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    if len(fields) < 3 or not fields[2].isdigit():
        raise DualSuccessorError("process-group membership record is malformed; no process was signaled")
    return int(fields[2])


def _group_members(process_group_id: int, creation_capability: str, proc_root: Path = Path("/proc")) -> tuple[int, ...]:
    members: list[int] = []
    expected = f"OMO_DUAL_CREATION_CAPABILITY={creation_capability}".encode()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if _proc_process_group(entry) != process_group_id:
                continue
            if entry.stat().st_uid != os.getuid():
                raise DualSuccessorError("recorded process group contains a foreign-owner process; no process was signaled")
            environment = (entry / "environ").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as exc:
            raise DualSuccessorError("recorded process group cannot be fully authenticated; no process was signaled") from exc
        if expected not in environment:
            raise DualSuccessorError("recorded process group contains a foreign process; no process was signaled")
        members.append(int(entry.name))
    return tuple(sorted(members))


def terminate_recorded_process_group(process: ProcessProof, *, creation_capability: str) -> None:
    """Terminate only the transaction-capability process group and prove it empty."""

    if process.process_group_id != process.pid or TOKEN_RE.fullmatch(creation_capability) is None:
        raise DualSuccessorError("recorded process group identity is invalid; no process was signaled")
    if process_start_ticks(process.pid) not in {None, process.start_ticks}:
        raise DualSuccessorError("recorded process PID was reused; no process was signaled")
    members = _group_members(process.process_group_id, creation_capability)
    if not members:
        return
    try:
        os.killpg(process.process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _group_members(process.process_group_id, creation_capability):
        time.sleep(0.05)
    if _group_members(process.process_group_id, creation_capability):
        try:
            os.killpg(process.process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _group_members(process.process_group_id, creation_capability):
        time.sleep(0.05)
    if _group_members(process.process_group_id, creation_capability):
        raise DualSuccessorError("recorded process group could not be reaped")


def process_from_receipt(value: dict[str, object], binding: PreparedBinding) -> tuple[Pane, ProcessProof, str]:
    try:
        pane = Pane(
            binding.new_target,
            str(value["pane_id"]),
            required_int(value, "pane_pid"),
            required_int(value, "pane_start_ticks"),
        )
        process = ProcessProof(
            required_int(value, "process_pid"),
            _runtime_from_codex_install(binding.manifest.get("codex_install")),
            required_string_list(binding.manifest.get("argv"), "manifest argv"),
            str(value["process_argv_sha256"]),
            str(value["process_environment_sha256"]),
            required_int(value, "process_start_ticks"),
            required_int(value, "process_group_id"),
        )
        capability = str(value["creation_capability"])
    except KeyError as exc:
        raise DualSuccessorError("launch receipt process proof is incomplete") from exc
    if not receipt_matches_process(value, pane, process):
        raise DualSuccessorError("launch receipt process proof is internally inconsistent")
    return pane, process, capability


def reconcile_authority_block(
    receipt: Snapshot,
    binding: PreparedBinding,
    *,
    receipt_value: dict[str, object],
    protected_sha: str,
) -> NoReturn:
    phase = str(receipt_value["phase"])
    evidence = authority_evidence_from_record(receipt_value.get("authority_evidence"), binding)
    pane, process, capability = process_from_receipt(receipt_value, binding)
    if phase == "authority-blocked":
        terminate_recorded_process_group(process, creation_capability=capability)
        _ = require_task_receipt_coherence(binding, phase, evidence)
        current = read_snapshot(binding.successor_path, "authority-blocked successor task")
        blocked = blocked_authority_task_bytes(binding, evidence.outcome)
        if current.data in {binding.successor_data, running_task_bytes(binding)}:
            _ = replace_snapshot(current, blocked, "authority-blocked successor task")
        elif current.data != blocked:
            raise DualSuccessorError("authority-blocked successor has unknown bytes")
        receipt = transition_receipt(
            receipt,
            binding,
            "terminated",
            pane,
            process,
            protected_sha,
            capability,
            authority_evidence=evidence,
        )
        phase = "terminated"
        maybe_crash_launch("terminated")
    if phase == "terminated":
        _ = require_task_receipt_coherence(binding, phase, evidence)
        blocked = blocked_authority_task_bytes(binding, evidence.outcome)
        if current_bytes(binding.successor_path) != blocked:
            raise DualSuccessorError("authority-blocked successor has unknown bytes")
        receipt = transition_receipt(
            receipt,
            binding,
            "withdrawn",
            pane,
            process,
            protected_sha,
            capability,
            authority_evidence=evidence,
        )
        maybe_crash_launch("withdrawn")
    if phase == "withdrawn":
        _ = require_task_receipt_coherence(binding, phase, evidence)
    raise AuthorityBlocked(evidence)


def block_after_process_error(
    receipt: Snapshot,
    binding: PreparedBinding,
    *,
    pane: Pane,
    process: ProcessProof,
    protected_sha: str,
    creation_capability: str,
    error: DualSuccessorError,
) -> NoReturn:
    """Durably fail closed and reap a transaction whose authority became unknown."""

    unknown = AuthorityEvidence(
        "unknown",
        binding.authority_binding.mailbox_identity_sha256,
        binding.authority_binding.approval_uid + 1,
        0,
        "authority-verification-failed",
        "",
        0,
        digest(str(error).encode()),
        "",
    )
    receipt = transition_receipt(
        receipt,
        binding,
        "authority-blocked",
        pane,
        process,
        protected_sha,
        creation_capability,
        authority_evidence=unknown,
    )
    maybe_crash_launch("authority-blocked")
    reconcile_authority_block(
        receipt,
        binding,
        receipt_value=parse_receipt(receipt, binding),
        protected_sha=protected_sha,
    )


def launch_successor(
    journal_path: Path,
    *,
    expected_journal_sha256: str,
    expected_task_sha256: str,
    expected_prompt_sha256: str,
    expected_queue_sha256: str,
    expected_manifest_sha256: str,
) -> str:
    binding = binding_from_journal(
        journal_path,
        expected_journal_sha256=expected_journal_sha256,
        expected_task_sha256=expected_task_sha256,
        expected_prompt_sha256=expected_prompt_sha256,
        expected_queue_sha256=expected_queue_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    receipt_path = launch_receipt_path(journal_path)
    with ExitStack() as locks:
        locks.enter_context(root_membership_lock(binding.root))
        locks.enter_context(task_target_lock(binding.root, binding.old_target))
        locks.enter_context(task_target_lock(binding.root, binding.new_target))
        for path in sorted(
            (
                binding.shadow_path,
                binding.canonical_path,
                binding.successor_path,
                binding.todo_path,
                binding.prompt_path,
                binding.manifest_path,
                binding.instructions_path,
                binding.approval_path,
                binding.journal.path,
                receipt_path,
            ),
            key=str,
        ):
            locks.enter_context(task_file_lock(path))
        if receipt_path.exists() or receipt_path.is_symlink():
            receipt = read_snapshot(receipt_path, "dual-successor launch receipt")
            receipt_value = parse_receipt(receipt, binding)
            phase: str | None = str(receipt_value["phase"])
        else:
            receipt = None
            receipt_value = None
            phase = None
        initial_inventory = pinned_pane_inventory(manifest_config=binding.manifest)
        protected_sha = protected_inventory_sha256(binding, initial_inventory)
        if receipt is not None and receipt_value is not None:
            if receipt_value["protected_inventory_sha256"] != protected_sha:
                raise DualSuccessorError("protected target inventory changed after launch receipt creation")
        else:
            _ = require_task_receipt_coherence(binding, None)
            if binding.new_target in initial_inventory:
                raise DualSuccessorError("fresh target became occupied before launch receipt creation")
            receipt = create_snapshot(
                receipt_path,
                canonical_json(receipt_record(binding, "reserved", protected_inventory_sha256=protected_sha)),
                0o600,
            )
            receipt_value = parse_receipt(receipt, binding)
            phase = "reserved"
        assert receipt is not None and receipt_value is not None and phase is not None
        if phase in {"authority-blocked", "terminated", "withdrawn"}:
            current_protected_sha = protected_inventory_sha256(binding, pinned_pane_inventory(manifest_config=binding.manifest))
            if current_protected_sha != str(receipt_value["protected_inventory_sha256"]):
                raise DualSuccessorError("protected target inventory changed during authority-blocked recovery")
            reconcile_authority_block(
                receipt,
                binding,
                receipt_value=receipt_value,
                protected_sha=current_protected_sha,
            )
        if phase == "reserved":
            reauthenticate_binding(binding, receipt_phase=phase)
            _ = require_task_receipt_coherence(binding, phase)
            maybe_crash_launch("reserved")
        pane = initial_inventory.get(binding.new_target)
        creation_capability = str(receipt_value["creation_capability"])
        if pane is None:
            if phase != "reserved":
                raise DualSuccessorError("recorded launch process disappeared; outcome is ambiguous")
            check_current_authority(binding.authority_binding)
            creation_capability = secrets.token_hex(32)
            pane = create_process(binding, creation_capability=creation_capability)
            maybe_crash_launch("process-unrecorded")
        elif phase == "reserved":
            raise DualSuccessorError("an unrecorded target appeared after reservation; foreign state is preserved")
        process = prove_process(binding, pane, creation_capability=creation_capability)
        if phase == "reserved":
            _ = require_task_receipt_coherence(binding, "reserved")
            receipt = transition_receipt(receipt, binding, "process", pane, process, protected_sha, creation_capability)
            phase = "process"
            maybe_crash_launch("process")
        elif not receipt_matches_process(receipt_value, pane, process):
            raise DualSuccessorError("recorded process identity does not match the exact live Codex process")
        _ = require_task_receipt_coherence(binding, phase)
        running = running_task_bytes(binding)
        if phase == "process":
            receipt = transition_receipt(receipt, binding, "task", pane, process, protected_sha, creation_capability)
            phase = "task"
            maybe_crash_launch("task")
        try:
            reauthenticate_binding(binding, receipt_phase=phase, authenticate_authority=phase != "committed")
            final_process = prove_process(binding, pane, creation_capability=creation_capability)
            if final_process != process:
                raise DualSuccessorError("Codex process identity changed before launch commit")
            current_protected_sha = protected_inventory_sha256(binding, pinned_pane_inventory(manifest_config=binding.manifest))
            if current_protected_sha != protected_sha:
                raise DualSuccessorError("protected target inventory changed during prepared launch")
        except DualSuccessorError as exc:
            if phase == "committed":
                raise
            block_after_process_error(
                receipt,
                binding,
                pane=pane,
                process=process,
                protected_sha=protected_sha,
                creation_capability=creation_capability,
                error=exc,
            )
        authority_commit: AuthorityCommit
        fresh_authority_pending = phase == "task"
        if fresh_authority_pending:
            receipt = transition_receipt(
                receipt,
                binding,
                "authority-pending",
                pane,
                process,
                protected_sha,
                creation_capability,
            )
            phase = "authority-pending"
            maybe_crash_launch("authority-pending")
        if phase == "authority-pending":
            try:
                authority_commit = (
                    final_authority_commit(binding.authority_binding, creation_capability)
                    if fresh_authority_pending
                    else final_authority_commit(binding.authority_binding, creation_capability, reconcile_only=True)
                )
            except AuthorityBlocked as blocked:
                receipt = transition_receipt(
                    receipt,
                    binding,
                    "authority-blocked",
                    pane,
                    process,
                    protected_sha,
                    creation_capability,
                    authority_evidence=blocked.evidence,
                )
                maybe_crash_launch("authority-blocked")
                reconcile_authority_block(
                    receipt,
                    binding,
                    receipt_value=parse_receipt(receipt, binding),
                    protected_sha=protected_sha,
                )
            except DualSuccessorError as exc:
                block_after_process_error(
                    receipt,
                    binding,
                    pane=pane,
                    process=process,
                    protected_sha=protected_sha,
                    creation_capability=creation_capability,
                    error=exc,
                )
            receipt = transition_receipt(
                receipt,
                binding,
                "authority",
                pane,
                process,
                protected_sha,
                creation_capability,
                authority_commit=authority_commit,
            )
            phase = "authority"
            maybe_crash_launch("authority-committed")
        elif phase == "authority":
            authority_commit = authority_commit_from_record(receipt_value.get("authority_commit"), binding, creation_capability)
            try:
                _ = final_authority_commit(
                    binding.authority_binding,
                    creation_capability,
                    expected_commit=authority_commit,
                )
            except DualSuccessorError as exc:
                block_after_process_error(
                    receipt,
                    binding,
                    pane=pane,
                    process=process,
                    protected_sha=protected_sha,
                    creation_capability=creation_capability,
                    error=exc,
                )
        else:
            authority_commit = authority_commit_from_record(receipt_value.get("authority_commit"), binding, creation_capability)
        if phase == "authority":
            current = read_snapshot(binding.successor_path, "prepared successor task")
            if current.data == binding.successor_data:
                _ = replace_snapshot(current, running, "prepared successor task")
            elif current.data != running:
                raise DualSuccessorError("successor task has unknown bytes after authoritative launch commit")
            receipt = transition_receipt(
                receipt,
                binding,
                "committed",
                pane,
                process,
                protected_sha,
                creation_capability,
                authority_commit=authority_commit,
            )
            phase = "committed"
            maybe_crash_launch("committed")
        if parse_receipt(receipt, binding)["phase"] != "committed" or current_bytes(binding.successor_path) != running:
            raise DualSuccessorError("launch did not reach its exact committed state")
        final_process = prove_process(binding, pane, creation_capability=creation_capability)
        if final_process != process:
            raise DualSuccessorError("Codex process identity changed after launch commit")
    return f"launched exactly one prepared Codex successor at {binding.new_target}; task={binding.successor_path.name}; process-pid={process.pid}; receipt-sha256={digest(receipt.data)}"


def parse_prepare_args(argv: list[str]) -> PrepareArgs:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--shadow-task", required=True)
    _ = parser.add_argument("--canonical-task", required=True)
    _ = parser.add_argument("--successor-task", required=True)
    _ = parser.add_argument("--old-target", required=True)
    _ = parser.add_argument("--new-target", required=True)
    _ = parser.add_argument("--manager-target", required=True)
    _ = parser.add_argument("--shadow-sha256", required=True)
    _ = parser.add_argument("--canonical-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--expected-pending-item", action="append", required=True)
    _ = parser.add_argument("--queue-sha256", required=True)
    _ = parser.add_argument("--prompt-file", type=Path, required=True)
    _ = parser.add_argument("--prompt-sha256", required=True)
    _ = parser.add_argument("--launch-manifest", type=Path, required=True)
    _ = parser.add_argument("--launch-manifest-sha256", required=True)
    _ = parser.add_argument("--instructions-file", type=Path, required=True)
    _ = parser.add_argument("--instructions-sha256", required=True)
    _ = parser.add_argument("--approval-file", type=Path, required=True)
    _ = parser.add_argument("--approval-sha256", required=True)
    _ = parser.add_argument("--protected-target", action="append", required=True)
    _ = parser.add_argument("--protected-sha256", required=True)
    _ = parser.add_argument("--custody-sha256", required=True)
    _ = parser.add_argument("--journal", type=Path, required=True)
    parsed = parser.parse_args(argv)
    tasks = (parsed.shadow_task, parsed.canonical_task, parsed.successor_task)
    if any(TASK_RE.fullmatch(task) is None for task in tasks) or len(set(tasks)) != 3:
        parser.error("source and successor tasks must be three distinct safe Markdown references")
    digests = (
        parsed.shadow_sha256,
        parsed.canonical_sha256,
        parsed.todo_sha256,
        parsed.queue_sha256,
        parsed.prompt_sha256,
        parsed.launch_manifest_sha256,
        parsed.instructions_sha256,
        parsed.approval_sha256,
        parsed.protected_sha256,
        parsed.custody_sha256,
    )
    if any(SHA256_RE.fullmatch(value) is None for value in digests):
        parser.error("all SHA-256 arguments must be lowercase hexadecimal")
    queue = tuple(parsed.expected_pending_item)
    if not queue or any(not item or "\0" in item for item in queue) or queue_digest(queue) != parsed.queue_sha256:
        parser.error("exact ordered queue is invalid or does not match its digest")
    try:
        old_target = canonical_target(parsed.old_target)
        new_target = canonical_target(parsed.new_target)
        manager_target = canonical_target(parsed.manager_target)
        protected = tuple(canonical_target(target) for target in parsed.protected_target)
    # canonical_target is a shared boundary whose implementations may reject
    # malformed targets with more than one concrete exception type.
    except Exception as exc:  # noqa: BLE001
        parser.error(str(exc))
    if len({old_target, new_target, manager_target}) != 3 or target_session(old_target).startswith("h") or target_session(new_target).startswith("h"):
        parser.error("old, new, and manager targets must be distinct and worker targets non-Human")
    if tuple(sorted(protected)) != protected or len(set(protected)) != len(protected) or old_target in protected or new_target in protected:
        parser.error("protected targets must be unique, sorted, and exclude old/new targets")
    if protected_digest(protected) != parsed.protected_sha256:
        parser.error("protected target digest does not match")
    root = parsed.root.expanduser().resolve(strict=False)

    def absolute(path: Path) -> Path:
        return Path(os.path.abspath(path.expanduser()))

    result = PrepareArgs(
        root,
        *tasks,
        old_target,
        new_target,
        manager_target,
        parsed.shadow_sha256,
        parsed.canonical_sha256,
        parsed.todo_sha256,
        queue,
        parsed.queue_sha256,
        absolute(parsed.prompt_file),
        parsed.prompt_sha256,
        absolute(parsed.launch_manifest),
        parsed.launch_manifest_sha256,
        absolute(parsed.instructions_file),
        parsed.instructions_sha256,
        absolute(parsed.approval_file),
        parsed.approval_sha256,
        protected,
        parsed.protected_sha256,
        parsed.custody_sha256,
        absolute(parsed.journal),
    )
    if custody_digest(result) != result.custody_sha256:
        parser.error("custody digest does not bind the exact invocation")
    return result


def main(argv: list[str]) -> int:
    try:
        if not argv or argv[0] not in {"prepare", "launch"}:
            raise DualSuccessorError("first argument must be prepare or launch")
        if argv[0] == "prepare":
            print(prepare_successor(parse_prepare_args(argv[1:])))
        else:
            parser = argparse.ArgumentParser(description="Launch one committed dual-record successor", allow_abbrev=False)
            _ = parser.add_argument("--journal", type=Path, required=True)
            _ = parser.add_argument("--expected-journal-sha256", required=True)
            _ = parser.add_argument("--expected-task-sha256", required=True)
            _ = parser.add_argument("--expected-prompt-sha256", required=True)
            _ = parser.add_argument("--expected-queue-sha256", required=True)
            _ = parser.add_argument("--expected-manifest-sha256", required=True)
            parsed = parser.parse_args(argv[1:])
            print(
                launch_successor(
                    parsed.journal.expanduser().resolve(strict=False),
                    expected_journal_sha256=parsed.expected_journal_sha256,
                    expected_task_sha256=parsed.expected_task_sha256,
                    expected_prompt_sha256=parsed.expected_prompt_sha256,
                    expected_queue_sha256=parsed.expected_queue_sha256,
                    expected_manifest_sha256=parsed.expected_manifest_sha256,
                )
            )
    # Keep every fail-closed validation/library error inside the CLI boundary;
    # callers receive one nonzero result and no traceback containing bound data.
    except Exception as exc:  # noqa: BLE001
        print(f"omo_dual_worker_successor: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
