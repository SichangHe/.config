#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Cancel one helper-originated stale ``/status`` input under bound evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_status import CODEX_EMPTY_INPUT_TEXTS, report_from_lines
from omo_manager.omo_codex_stop import (
    SHELL_COMMANDS,
    bound_guarded_read,
    done_live_close_started_path,
    guarded_tmux_sequence,
    path_entry_exists,
    tmux,
    tmux_guard_condition,
)
from omo_manager.omo_report_receipt import bound_receipt_id
from omo_manager.omo_repository_custody import (
    CustodyError,
    HeldAbsolute,
    absolute_file_binding,
    canonical_json,
    directory_identity_from,
    file_identity_from,
    hold_absolute,
    publish_or_validate,
    validate_held_absolute,
)
from omo_manager.omo_stale_predecessor_close import (
    REVIEW_SCHEMA as CLOSE_REVIEW_SCHEMA,
)
from omo_manager.omo_stale_predecessor_close import (
    SCHEMA as CLOSE_SCHEMA,
)
from omo_manager.omo_stale_predecessor_close import (
    PanePin,
    current_pin,
    object_map,
    pane_record,
    parse_pin,
    prepared_audit as close_prepared_audit,
    session_from_process,
    sha256,
    validate_packet as validate_close_packet,
)
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, canonical_target, parse_task_metadata
from omo_manager.omo_task_status import authoritative_active_target_task_paths
from omo_manager.omo_tmux_input_lock import tmux_input_lock
from omo_manager.omo_tmux_send import CODEX_PLACEHOLDER_INPUT_TEXTS, exact_complete_input_text

SCHEMA = "omo-stale-predecessor-input-disposition/v1"
REVIEW_SCHEMA = "omo-stale-predecessor-input-disposition-review/v1"
TODO_RECOVERY_SCHEMA = "omo-stale-predecessor-input-disposition-todo-recovery/v2"
TODO_RECOVERY_REVIEW_SCHEMA = "omo-stale-predecessor-input-disposition-todo-recovery-review/v2"
OPERATION = "cancel-proven-stale-status-input"
TODO_RECOVERY_OPERATION = "rebind-exact-held-todo-and-source1485-task-inputs"
AUTHORIZED_INPUT = "/status"
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TMUX_BUFFER_INVENTORY = 4096
TEMPORARY_TMUX_BUFFER_LIMIT = 2_147_483_647
EXPECTED_PRIOR_PACKET_SHA256 = "530dde7d9fbb46b46b1997153dc839b2e5a5f58dd86af6ee87f20fd0f40dce65"
EXPECTED_PRIOR_REVIEW_SHA256 = "e0cca46f3a8f4ac8046dbabdae9436091cb9bdc80cfeae8295c83974bb6a350a"
EXPECTED_PREPARED_CLOSE_SHA256 = "e83329cb11bcf2645cb4ce0a3e4ff979edb6f1fb177b863ebeb38276d5907d45"
EXPECTED_PRIOR_HELPER_SHA256 = "b76f7c4e6004e57603500ecda359cee68ac9b342253dba32b418fc8112bad09c"
EXPECTED_EXECUTION_REPORT_SHA256 = "7295fee83791bba7fee4bad674dfee04305323821ebb6d232ad97512b811bdf7"
EXPECTED_RECOVERY_REPORT_SHA256 = "d099413459ae713e4195b0a05c6dbeb3577ad4d5f48090050e7b8ddb6fefde37"
EXPECTED_INPUT_AUTHORIZATION_SHA256 = "a2e2f1561ac197865943d326d9b71c91b41e28f35b70d3f8e6c1d041e315aa2c"
RECOVERY_REPORT_REQUIRED_TEXT = (
    "exact `/status` cancellation dry-run",
    "target input is not in a complete Codex view",
    "running",
    "menu visible",
    "This supplies no safe-stop evidence.",
)
RECOVERABLE_PACKET_SHA256 = "e7350f2b8ce7d28d9e5480837b5a1985242c43cccfecb62ab2fa2fbc0ceae176"
RECOVERABLE_REVIEW_SHA256 = "feb306c06dd5fb0cef6614b0e6a78866a9fd46d35f3b94f2359e8249b00ddc38"
RECOVERABLE_PREPARED_AUDIT_SHA256 = "ea0cc462c460598775244919ee913448e1f0655008e0a4420c8d03cbf44fc9fd"
RECOVERABLE_HELPER_SHA256 = "ab4c54f5cdc06823ce8d36333e7ee928b82e9813bb7de1da76df526e7f90cb85"
SOURCE1485_ROOT_AUDIT_SHA256 = "dd2cd04c1c6cd6c4050c7cd537d893e3c24aec45c504c1db3dbe0e4c0c792f2b"
EXPECTED_SCOPE = {
    "manager_target": "dw:0",
    "predecessor_target": "dw8:0",
    "predecessor_pane": {"pane_id": "%756", "pane_pid": 1890387, "pane_start_ticks": 26503216, "target": "dw8:0"},
    "predecessor_session_id": "01a07f0f-ffbd-7f13-89f1-4936c50be5c2",
    "protected_target": "dw8:1",
    "protected_pane": {"pane_id": "%864", "pane_pid": 2863234, "pane_start_ticks": 27512569, "target": "dw8:1"},
    "protected_session_id": "01a07faa-011d-7983-86bb-978cf5a25169",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
SENT_LINE_RE = re.compile(
    r"^\(sent from ([A-Za-z0-9_.-]+) via omo_report\.sh tmux=([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) "
    r"time=\S+ task-file=([A-Za-z0-9_.-]+)\)$"
)
HASH_LINE_RE = re.compile(r"^\[message-sha256: ([0-9a-f]{64})\]$")
OWNER_PREFIX_LINE_RE = re.compile(
    r"^\[omo-report-owner-prefix: manager-path-sha256=([0-9a-f]{64}) sha256=([0-9a-f]{64}) "
    r"size-bytes=(0|[1-9][0-9]*) separator-bytes=([12])\]$"
)
STATUS_MENU_ROWS = (
    "/status      show current session configuration and token usage",
    "/statusline  configure which items appear in the status line",
)
PACKET_KEYS = {
    "schema",
    "operation",
    "root",
    "prior_packet",
    "prior_packet_sha256",
    "prior_review",
    "prior_review_sha256",
    "prepared_close_audit",
    "prepared_close_audit_sha256",
    "execution_report",
    "execution_report_sha256",
    "execution_report_replay_id",
    "recovery_report",
    "recovery_report_sha256",
    "recovery_report_replay_id",
    "input_authorization",
    "input_authorization_sha256",
    "authorized_input",
    "task",
    "task_sha256",
    "todo",
    "todo_sha256",
    "manager_task",
    "manager_task_sha256",
    "manager_target",
    "predecessor_target",
    "predecessor_pane",
    "predecessor_session_id",
    "protected_target",
    "protected_pane",
    "protected_session_id",
    "menu_capture_base64",
    "menu_capture_sha256",
    "helper",
    "helper_sha256",
    "audit",
    "inputs",
    "binding_id",
}
AUDIT_KEYS = {
    "schema",
    "operation",
    "state",
    "packet_sha256",
    "prior_packet_sha256",
    "prepared_close_audit",
    "prepared_close_audit_sha256",
    "execution_report_replay_id",
    "recovery_report_replay_id",
    "authorized_input",
    "input_authorization_sha256",
    "predecessor_target",
    "predecessor_pane",
    "predecessor_session_id",
    "protected_target",
    "protected_pane",
    "protected_session_id",
    "menu_capture_sha256",
    "binding_id",
}
TODO_RECOVERY_KEYS = {
    "schema",
    "operation",
    "packet",
    "packet_sha256",
    "review_report",
    "review_report_sha256",
    "prepared_audit",
    "prepared_audit_sha256",
    "task",
    "original_task_sha256",
    "current_task_input",
    "todo",
    "original_todo_sha256",
    "owned_rows_base64",
    "unrelated_todo_base64",
    "unrelated_todo_sha256",
    "current_todo_input",
    "recovery_helper_input",
    "source1485_root_audit",
    "source1485_root_audit_sha256",
    "current_manager_task",
    "current_manager_target",
    "binding_id",
}


def read_bound(path: Path, expected_sha256: str, label: str, *, private: bool = False) -> bytes:
    if not path.is_absolute() or SHA256_RE.fullmatch(expected_sha256) is None:
        raise TaskFrontmatterError(f"{label} identity is invalid.")
    data, identity, _ancestors = absolute_file_binding(path, label, private=private)
    if identity.uid != os.getuid() or identity.size_bytes > MAX_FILE_BYTES or sha256(data) != expected_sha256:
        raise TaskFrontmatterError(f"{label} changed.")
    return data


def canonical_object(data: bytes, label: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError(f"{label} is not JSON.") from exc
    record = object_map(parsed, label)
    if canonical_json(record) != data:
        raise TaskFrontmatterError(f"{label} is not canonical JSON.")
    return record


def exact_status_menu(lines: list[str], authorized_input: str = AUTHORIZED_INPUT) -> bool:
    """Recognize only the bottom-anchored two-row ``/status`` completion menu."""

    visible = [line.rstrip() for line in lines]
    while visible and not visible[-1]:
        visible.pop()
    expected = [f"› {authorized_input}", "", *(f"  {row}" for row in STATUS_MENU_ROWS)]
    return len(visible) >= len(expected) and visible[-len(expected) :] == expected


def exact_recovery_state(lines: list[str], authorized_input: str = AUTHORIZED_INPUT) -> str:
    if exact_status_menu(lines, authorized_input):
        return "status_menu"
    try:
        input_text = exact_complete_input_text(lines, allow_codex_footer_spacer=True)
    except RuntimeError:
        return "other"
    if input_text == authorized_input:
        return "status_input"
    report = report_from_lines(lines)
    if input_text in CODEX_PLACEHOLDER_INPUT_TEXTS and input_text in CODEX_EMPTY_INPUT_TEXTS and report.status == "ready":
        return "ready"
    return "other"


def capture_pinned(pin: PanePin) -> bytes:
    if not current_pin(pin):
        raise TaskFrontmatterError("bound pane identity changed before capture.")
    raw = bound_guarded_read(
        pin.target,
        pin.pane_id,
        ["capture-pane", "-p", "-J", "-N", "-t", pin.pane_id],
        pin.pane_pid,
    ).encode()
    if not current_pin(pin):
        raise TaskFrontmatterError("bound pane identity changed during capture.")
    return raw


def capture_lines(data: bytes) -> list[str]:
    try:
        return data.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("bound pane capture is not UTF-8.") from exc


def restore_temporary_capture_state(
    pin: PanePin,
    *,
    lease_option: str,
    capture_option: str,
    original_buffer_limit: str,
) -> None:
    """Conditionally restore only this invocation's temporary tmux state."""

    token = lease_option.removeprefix("@omo-disposition-buffer-limit-")
    owned = f"OMO_DISPOSITION_STATE_RELEASED_{token}"
    foreign = f"OMO_DISPOSITION_STATE_FOREIGN_{token}"
    lease_condition = f"#{{==:#{{{lease_option}}},{token}}}"
    temporary_limit_condition = f"#{{==:#{{buffer-limit}},{TEMPORARY_TMUX_BUFFER_LIMIT}}}"
    unset_lease = shlex.join(["set-option", "-s", "-q", "-u", lease_option])
    restore = " ; ".join(
        (
            shlex.join(["set-option", "-g", "buffer-limit", original_buffer_limit]),
            unset_lease,
            f"display-message -p {owned}",
        )
    )
    leave_external_limit = " ; ".join((unset_lease, f"display-message -p {owned}"))
    release_owned = shlex.join(["if-shell", "-F", temporary_limit_condition, restore, leave_external_limit])
    result = tmux(
        [
            "if-shell",
            "-F",
            lease_condition,
            release_owned,
            f"display-message -p {foreign}",
        ]
    )
    if result.returncode != 0 or result.stdout not in {f"{owned}\n", f"{foreign}\n"}:
        raise TaskFrontmatterError("temporary tmux buffer limit could not be restored.")
    if result.stdout == f"{foreign}\n":
        return

    option_cleared = f"OMO_DISPOSITION_OPTION_CLEARED_{token}"
    identity = tmux_guard_condition(pin.target, pin.pane_id, pin.pane_pid)
    unset_capture = " ; ".join(
        (
            shlex.join(["set-option", "-p", "-q", "-u", "-t", pin.pane_id, capture_option]),
            f"display-message -p {option_cleared}",
        )
    )
    pane_result = tmux(
        [
            "if-shell",
            "-F",
            "-t",
            pin.pane_id,
            identity,
            unset_capture,
            f"display-message -p {option_cleared}",
        ]
    )
    if pane_result.returncode == 0 and pane_result.stdout != f"{option_cleared}\n":
        raise TaskFrontmatterError("temporary tmux capture option could not be cleared.")


def guarded_tmux_command_for_capture(
    pin: PanePin,
    command: list[str],
    *,
    validate_before: Callable[[], tuple[str, bytes]],
    validate_after: Callable[[], tuple[str, bytes]],
) -> tuple[str, bytes]:
    """Mutate only between full bound-state checks and an exact tmux capture guard."""

    allowed_commands = {
        ("send-keys", "-t", pin.pane_id, "Escape"),
        ("send-keys", "-t", pin.pane_id, "C-c"),
    }
    if tuple(command) not in allowed_commands:
        raise TaskFrontmatterError("input disposition command is not cancellation-only.")
    foreground_record = bound_guarded_read(
        pin.target,
        pin.pane_id,
        ["display-message", "-p", "-t", pin.pane_id, "#{pane_current_command}|#{pane_dead}|#{buffer-limit}"],
        pin.pane_pid,
    ).strip()
    foreground, separator, remainder = foreground_record.partition("|")
    dead, limit_separator, raw_buffer_limit = remainder.partition("|")
    if (
        not separator
        or not limit_separator
        or dead != "0"
        or not raw_buffer_limit.isdecimal()
        or not 1 <= int(raw_buffer_limit) <= TEMPORARY_TMUX_BUFFER_LIMIT
        or foreground in SHELL_COMMANDS
        or re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", foreground) is None
    ):
        raise TaskFrontmatterError("bound pane lacks one live non-shell foreground command.")
    buffer_inventory = bound_guarded_read(
        pin.target,
        pin.pane_id,
        ["list-buffers", "-F", "1"],
        pin.pane_pid,
    ).splitlines()
    if len(buffer_inventory) > MAX_TMUX_BUFFER_INVENTORY or any(item != "1" for item in buffer_inventory):
        raise TaskFrontmatterError("tmux buffer inventory exceeds the bounded capture allowance.")
    token = secrets.token_hex(16)
    option = f"@omo-disposition-capture-{token}"
    lease_option = f"@omo-disposition-buffer-limit-{token}"
    if bound_guarded_read(
        pin.target,
        pin.pane_id,
        ["show-options", "-p", "-q", "-t", pin.pane_id, option],
        pin.pane_pid,
    ):
        raise TaskFrontmatterError("tmux capture guard option already exists.")
    if bound_guarded_read(
        pin.target,
        pin.pane_id,
        ["show-options", "-s", "-q", lease_option],
        pin.pane_pid,
    ):
        raise TaskFrontmatterError("tmux capture lease option already exists.")
    _state, capture = validate_before()
    try:
        expected = capture.decode()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("bound pane capture is not UTF-8.") from exc
    accepted = f"OMO_DISPOSITION_CAPTURE_ACCEPTED_{token}"
    rejected = f"OMO_DISPOSITION_CAPTURE_REJECTED_{token}"
    identity = tmux_guard_condition(pin.target, pin.pane_id, pin.pane_pid)

    def all_of(predicates: tuple[str, ...]) -> str:
        condition = predicates[-1]
        for predicate in reversed(predicates[:-1]):
            condition = f"#{{&&:{predicate},{condition}}}"
        return condition

    stable_pane = (
        identity,
        "#{==:#{pane_dead},0}",
        f"#{{==:#{{pane_current_command}},{foreground}}}",
    )
    initial_condition = all_of((*stable_pane, f"#{{==:#{{buffer-limit}},{raw_buffer_limit}}}"))
    capture_condition = all_of(
        (
            *stable_pane,
            f"#{{==:#{{buffer-limit}},{raw_buffer_limit}}}",
            f"#{{==:#{{buffer_full}},#{{{option}}}}}",
        )
    )
    cleanup = (
        "delete-buffer",
        shlex.join(["set-option", "-p", "-u", "-t", pin.pane_id, option]),
    )
    success = " ; ".join((*cleanup, shlex.join(command), f"display-message -p {accepted}"))
    failure = " ; ".join((*cleanup, f"display-message -p {rejected}"))
    guarded_capture = " ; ".join(
        (
            shlex.join(["set-option", "-s", "-o", lease_option, token]),
            shlex.join(["set-option", "-p", "-o", "-t", pin.pane_id, option, expected]),
            shlex.join(["set-option", "-g", "buffer-limit", str(TEMPORARY_TMUX_BUFFER_LIMIT)]),
            shlex.join(["capture-pane", "-J", "-N", "-t", pin.pane_id]),
            shlex.join(["set-option", "-g", "buffer-limit", raw_buffer_limit]),
            shlex.join(["set-option", "-s", "-u", lease_option]),
            shlex.join(["if-shell", "-F", "-t", pin.pane_id, capture_condition, success, failure]),
        )
    )
    try:
        output = guarded_tmux_sequence(
            pin.target,
            pin.pane_id,
            [
                # Raising to tmux's accepted numeric maximum provides a
                # bounded non-evicting slot.  Lowering the option does not
                # prune existing buffers; cleanup then removes only the new
                # top automatic buffer before any key can be sent.
                ["if-shell", "-F", "-t", pin.pane_id, initial_condition, guarded_capture, f"display-message -p {rejected}"],
            ],
            pin.pane_pid,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        restore_temporary_capture_state(
            pin,
            lease_option=lease_option,
            capture_option=option,
            original_buffer_limit=raw_buffer_limit,
        )
        validate_after()
        raise TaskFrontmatterError("guarded input disposition did not complete.") from exc
    if output != f"{accepted}\n":
        raise TaskFrontmatterError("bound pane capture changed before guarded input disposition.")
    return validate_after()


def report_commitment_path(attached_transfer: dict[str, object], envelope_path: Path) -> Path:
    commitment_path = Path(str(attached_transfer.get("commitment_path", "")))
    expected_parent = Path.home() / ".local" / "state" / "omo-manager" / "report-receipts"
    if (
        not commitment_path.is_absolute()
        or commitment_path.parent != expected_parent
        or commitment_path.suffix != ".commitment"
        or SHA256_RE.fullmatch(commitment_path.stem) is None
        or envelope_path.parent != Path("/tmp") / f"omo-agent-messages-{os.getuid()}"
    ):
        raise TaskFrontmatterError("terminal report commitment path is invalid.")
    return commitment_path


def validate_terminal_report(
    path: Path,
    expected_sha256: str,
    *,
    executor_target: str,
    executor_task: Path,
    required_text: tuple[str, ...],
) -> tuple[str, Path]:
    """Authenticate one immutable ``omo_report.sh`` envelope and its body."""

    data = read_bound(path, expected_sha256, "terminal report", private=True)
    header, separator, body = data.partition(b"message:\n")
    if not separator:
        raise TaskFrontmatterError("terminal report envelope is malformed.")
    try:
        header_lines = header.decode().splitlines()
        text = body.decode()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("terminal report envelope is not UTF-8.") from exc
    if len(header_lines) not in {4, 6}:
        raise TaskFrontmatterError("terminal report envelope header is malformed.")
    sent = SENT_LINE_RE.fullmatch(header_lines[0])
    message_hash = HASH_LINE_RE.fullmatch(header_lines[1])
    owner_prefix = OWNER_PREFIX_LINE_RE.fullmatch(header_lines[2])
    transfer_line = header_lines[3]
    if (
        sent is None
        or message_hash is None
        or owner_prefix is None
        or not transfer_line.startswith("[omo-transfer: ")
        or not transfer_line.endswith("]")
        or sent.group(2) != executor_target
        or sent.group(3) != executor_task.name
        or hashlib.sha256(body).hexdigest() != message_hash.group(1)
        or any(token not in text for token in required_text)
        or (len(header_lines) == 6 and (header_lines[4] != "route-warning:" or not header_lines[5]))
    ):
        raise TaskFrontmatterError("terminal report envelope identity is inconsistent.")
    try:
        attached_value: object = json.loads(transfer_line[len("[omo-transfer: ") : -1])
    except json.JSONDecodeError as exc:
        raise TaskFrontmatterError("terminal report transfer is invalid.") from exc
    attached = object_map(attached_value, "terminal report transfer")
    commitment_path = report_commitment_path(attached, path)
    commitment_data, commitment_identity, _commitment_ancestors = absolute_file_binding(
        commitment_path,
        "terminal report commitment",
        private=True,
    )
    if commitment_identity.uid != os.getuid() or commitment_identity.size_bytes > MAX_FILE_BYTES:
        raise TaskFrontmatterError("terminal report commitment changed.")
    commitment = canonical_object(commitment_data, "terminal report commitment")
    unsigned = dict(commitment)
    commitment_id = unsigned.pop("commitment_id", None)
    transfer = object_map(commitment.get("transfer"), "terminal report committed transfer")
    expected_attached_base: dict[str, object] = {**transfer, "commitment_id": commitment_id}
    expected_attached = {**expected_attached_base, "transfer_id": bound_receipt_id(expected_attached_base)}
    preflight = object_map(commitment.get("preflight"), "terminal report preflight")
    unsigned_preflight = dict(preflight)
    preflight_sha256 = unsigned_preflight.pop("sha256", None)
    preflight_allocation = object_map(preflight.get("allocation"), "terminal report preflight allocation")
    records = object_map(preflight.get("records"), "terminal report records")
    owner = object_map(preflight.get("owner_prefix"), "terminal report owner binding")
    allocation = object_map(commitment.get("allocation"), "terminal report allocation")
    submitted = object_map(allocation.get("file_at_submission"), "terminal report submitted input")
    authority = object_map(transfer.get("authority"), "terminal report authority")
    routing = object_map(transfer.get("routing"), "terminal report routing")
    queue_item = object_map(transfer.get("queue_item"), "terminal report queue item")
    replay_id = str(commitment.get("replay_id", ""))
    if (
        set(commitment) != {"allocation", "commitment", "commitment_id", "preflight", "replay_id", "schema", "transfer"}
        or commitment.get("schema") != "omo-report-transaction-commitment/v2"
        or set(transfer) != {"authority", "commitment_path", "queue_item", "receiver", "routing", "schema"}
        or transfer.get("schema") != "omo-report-transfer-receipt/v1"
        or preflight.get("schema") != "omo-report-preflight-transaction-set/v1"
        or preflight_sha256 != hashlib.sha256(canonical_json(unsigned_preflight).rstrip(b"\n")).hexdigest()
        or not isinstance(commitment_id, str)
        or commitment_id != bound_receipt_id(unsigned)
        or attached != expected_attached
        or authority != {"kind": "agent-originated", "producer_target": executor_target, "source_task": str(executor_task)}
        or routing.get("task") != str(executor_task)
        or routing.get("producer_target") != executor_target
        or queue_item.get("producer") != str(executor_task)
        or queue_item.get("input_sha256") != message_hash.group(1)
        or queue_item.get("replay_id") != replay_id
        or queue_item.get("pointer") != f"(from agent {executor_target} {path})"
        or transfer.get("receiver") != routing.get("manager")
        or queue_item.get("manager") != routing.get("manager")
        or records.get("producer") != str(executor_task)
        or records.get("private_envelope") != str(path)
        or records.get("transaction_commitment") != str(commitment_path)
        or preflight_allocation.get("file_sha256") != message_hash.group(1)
        or preflight_allocation.get("file_size_bytes") != len(body)
        or allocation.get("file") != preflight_allocation.get("file")
        or submitted.get("sha256") != message_hash.group(1)
        or submitted.get("size") != len(body)
        or owner.get("manager_path_sha256") != owner_prefix.group(1)
        or owner.get("sha256") != owner_prefix.group(2)
        or owner.get("size_bytes") != int(owner_prefix.group(3))
        or owner.get("separator_bytes") != int(owner_prefix.group(4))
        or SHA256_RE.fullmatch(replay_id) is None
        or commitment.get("replay_id") != replay_id
        or commitment_path.stem != replay_id
    ):
        raise TaskFrontmatterError("terminal report transaction is inconsistent.")
    return replay_id, commitment_path


def validate_close_artifacts(
    prior_packet_path: Path,
    prior_packet_sha256: str,
    prior_review_path: Path,
    prior_review_sha256: str,
    prepared_path: Path,
    prepared_sha256: str,
) -> dict[str, object]:
    if prior_packet_sha256 != EXPECTED_PRIOR_PACKET_SHA256 or prior_review_sha256 != EXPECTED_PRIOR_REVIEW_SHA256 or prepared_sha256 != EXPECTED_PREPARED_CLOSE_SHA256:
        raise TaskFrontmatterError("close evidence is outside the exact dw8 prepared transaction.")
    prior_packet = validate_close_packet(read_bound(prior_packet_path, prior_packet_sha256, "prior close packet", private=True), prior_packet_sha256)
    prepared = read_bound(prepared_path, prepared_sha256, "prepared close audit", private=True)
    if prepared != close_prepared_audit(prior_packet, prior_packet_sha256):
        raise TaskFrontmatterError("prepared close audit does not match the prior packet.")
    review = canonical_object(read_bound(prior_review_path, prior_review_sha256, "prior close review", private=True), "prior close review")
    expected_review = {
        "schema": CLOSE_REVIEW_SCHEMA,
        "verdict": "PASS",
        "packet_sha256": prior_packet_sha256,
        "predecessor_session_id": prior_packet["predecessor_session_id"],
        "protected_session_id": prior_packet["protected_session_id"],
        "predecessor_pane": prior_packet["predecessor_pane"],
        "protected_pane": prior_packet["protected_pane"],
    }
    complete_path = Path(str(prior_packet["audit"]))
    proof_path = prepared_path.with_name(f".{prepared_path.name}.owner-stopped")
    prior_helper = Path(str(prior_packet["helper"]))
    _prior_helper_data = read_bound(
        prior_helper,
        str(prior_packet["helper_sha256"]),
        "prior close helper",
    )
    if (
        review != expected_review
        or prior_packet.get("schema") != CLOSE_SCHEMA
        or prior_packet.get("helper_sha256") != EXPECTED_PRIOR_HELPER_SHA256
        or any(prior_packet.get(key) != value for key, value in EXPECTED_SCOPE.items())
        or prepared_path != Path(f"{prior_packet['audit']}.prepared")
        or path_entry_exists(complete_path)
        or path_entry_exists(done_live_close_started_path(prepared_path))
        or path_entry_exists(proof_path)
    ):
        raise TaskFrontmatterError("prior close transaction is not one unstarted prepared failure.")
    return prior_packet


def prior_close_control_paths(complete_path: Path, prepared_path: Path) -> set[Path]:
    """Return every path reserved by the prepared predecessor-close transaction."""

    complete_path = complete_path.resolve(strict=False)
    prepared_path = prepared_path.resolve(strict=False)
    return {
        complete_path,
        prepared_path,
        done_live_close_started_path(prepared_path),
        prepared_path.with_name(f".{prepared_path.name}.owner-stopped"),
    }


def validate_disposition_output_paths(output: Path, audit: Path, reserved: set[Path]) -> None:
    """Keep all generated disposition files disjoint from immutable evidence."""

    generated = {output, audit, Path(f"{audit}.prepared")}
    if len(generated) != 3 or generated & reserved:
        raise TaskFrontmatterError("disposition output paths overlap immutable evidence.")


def is_recoverable_prepared_packet(packet: dict[str, object]) -> bool:
    """Recognize only the exact packet stranded by the first guarded attempt."""

    return packet.get("helper") == str(Path(__file__).resolve(strict=True)) and packet.get("helper_sha256") == RECOVERABLE_HELPER_SHA256 and sha256(packet_bytes(packet)) == RECOVERABLE_PACKET_SHA256


def validate_incident_digests(packet: dict[str, object]) -> None:
    expected = {
        "prior_packet_sha256": EXPECTED_PRIOR_PACKET_SHA256,
        "prior_review_sha256": EXPECTED_PRIOR_REVIEW_SHA256,
        "prepared_close_audit_sha256": EXPECTED_PREPARED_CLOSE_SHA256,
        "execution_report_sha256": EXPECTED_EXECUTION_REPORT_SHA256,
        "recovery_report_sha256": EXPECTED_RECOVERY_REPORT_SHA256,
        "input_authorization_sha256": EXPECTED_INPUT_AUTHORIZATION_SHA256,
    }
    if any(packet.get(key) != value for key, value in expected.items()) or any(packet.get(key) != value for key, value in EXPECTED_SCOPE.items()):
        raise TaskFrontmatterError("disposition evidence is outside the exact dw8 incident.")


def owned_todo_row(packet: dict[str, object]) -> bytes:
    root = Path(str(packet["root"]))
    task = Path(str(packet["task"]))
    try:
        task_ref = task.relative_to(root).as_posix()
    except ValueError as exc:
        raise TaskFrontmatterError("protected task is outside the lifecycle root.") from exc
    return f"{task_ref} {packet['protected_target']}\n".encode()


def partition_todo(data: bytes, row: bytes) -> tuple[bytes, tuple[bytes, bytes]]:
    """Separate one exact transaction-owned row without normalizing other bytes."""

    try:
        data.decode()
        row.decode()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("current TODO is not UTF-8.") from exc
    if not row.endswith(b"\n") or data.count(row) != 1:
        raise TaskFrontmatterError("current TODO does not contain the one exact transaction-owned row.")
    before, after = data.split(row, 1)
    return row, (before, after)


def todo_recovery_bytes(record: dict[str, object]) -> bytes:
    unsigned = dict(record)
    observed_binding = unsigned.pop("binding_id", None)
    if set(record) != TODO_RECOVERY_KEYS or observed_binding != bound_receipt_id(unsigned):
        raise TaskFrontmatterError("TODO recovery packet schema is invalid.")
    return canonical_json(record)


def unique_record(records: object, key: str, value: object, label: str) -> dict[str, object]:
    if not isinstance(records, list):
        raise TaskFrontmatterError(f"{label} is invalid.")
    matches = [object_map(item, label) for item in records if isinstance(item, dict) and item.get(key) == value]
    if len(matches) != 1:
        raise TaskFrontmatterError(f"{label} is not unique.")
    return matches[0]


def validate_source1485_transition(packet: dict[str, object], recovery: dict[str, object]) -> tuple[bytes, Path, str, str]:
    """Authenticate the one committed root migration allowed to rebind the task."""

    audit_path = Path(str(recovery.get("source1485_root_audit", "")))
    if recovery.get("source1485_root_audit_sha256") != SOURCE1485_ROOT_AUDIT_SHA256:
        raise TaskFrontmatterError("Source-1485 root audit identity is invalid.")
    audit_data = read_bound(audit_path, SOURCE1485_ROOT_AUDIT_SHA256, "Source-1485 root audit", private=True)
    try:
        audit = object_map(json.loads(audit_data), "Source-1485 root audit")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("Source-1485 root audit is not JSON.") from exc
    expected_scope = {
        "state": "committed",
        "operation": "manager-replace",
        "old_task": "dw_manager.md",
        "old_target": "dw:0",
        "successor_task": "dw_root_new.md",
        "new_target": "dw:15",
        "parent_target": "config:1",
    }
    if any(audit.get(key) != value for key, value in expected_scope.items()):
        raise TaskFrontmatterError("Source-1485 root audit scope is invalid.")
    root = Path(str(packet["root"]))
    task = Path(str(packet["task"]))
    task_ref = task.relative_to(root).as_posix()
    if (
        recovery.get("task") != str(task)
        or recovery.get("original_task_sha256") != packet.get("task_sha256")
        or recovery.get("current_manager_task") != str(root / str(audit["successor_task"]))
        or canonical_target(str(recovery.get("current_manager_target"))) != canonical_target(str(audit["new_target"]))
    ):
        raise TaskFrontmatterError("Source-1485 task recovery scope is invalid.")
    child = unique_record(audit.get("children"), "task", task_ref, "Source-1485 original child")
    topology = object_map(audit.get("source1485_topology"), "Source-1485 topology")
    row = unique_record(topology.get("rows"), "task", task_ref, "Source-1485 migrated child")
    manager_row = unique_record(topology.get("rows"), "task", audit["successor_task"], "Source-1485 successor manager")
    file_change = unique_record(audit.get("files"), "task", task_ref, "Source-1485 task change")
    if (
        child.get("sha256") != packet.get("task_sha256")
        or child.get("queue_sha256") != sha256(b"[]")
        or row.get("sha256")
        != file_identity_from(
            object_map(recovery.get("current_task_input"), "current task recovery input").get("file"),
            "current task recovery input",
        ).sha256
        or row.get("status") != "blocked"
        or canonical_target(str(row.get("runat"))) != canonical_target(str(packet["protected_target"]))
        or canonical_target(str(row.get("managerat"))) != canonical_target(str(audit["new_target"]))
        or row.get("tool") != "codex"
        or row.get("is_manager") is not False
        or row.get("queue_sha256") != sha256(b"[]")
        or manager_row.get("is_manager") is not True
        or canonical_target(str(manager_row.get("runat"))) != canonical_target(str(audit["new_target"]))
        or canonical_target(str(manager_row.get("managerat"))) != canonical_target(str(audit["parent_target"]))
        or topology.get("root_task") != audit["successor_task"]
        or canonical_target(str(topology.get("root_target"))) != canonical_target(str(audit["new_target"]))
        or canonical_target(str(topology.get("parent_target"))) != canonical_target(str(audit["parent_target"]))
    ):
        raise TaskFrontmatterError("Source-1485 task transition evidence is invalid.")
    try:
        before = base64.b64decode(str(file_change["before"]), validate=True)
        after = base64.b64decode(str(file_change["after"]), validate=True)
    except (KeyError, ValueError) as exc:
        raise TaskFrontmatterError("Source-1485 task transition encoding is invalid.") from exc
    if (
        sha256(before) != packet.get("task_sha256")
        or sha256(after) != row.get("sha256")
        or before.replace(b"managerat: dw:0\n", b"managerat: dw:15\n") != after
        or before.count(b"managerat: dw:0\n") != 1
        or after.count(b"managerat: dw:15\n") != 1
    ):
        raise TaskFrontmatterError("Source-1485 task transition changed unsupported bytes.")
    current_input = object_map(recovery.get("current_task_input"), "current task recovery input")
    current_identity = file_identity_from(current_input.get("file"), "current task recovery input")
    if current_identity.path != str(task):
        raise TaskFrontmatterError("current task recovery path is invalid.")
    current_task = read_bound(task, current_identity.sha256, "rebound protected task")
    if current_task != after or current_input != file_input(task, "rebound protected task"):
        raise TaskFrontmatterError("current task recovery input changed.")
    return (
        current_task,
        root / str(audit["successor_task"]),
        canonical_target(str(audit["new_target"])),
        canonical_target(str(audit["parent_target"])),
    )


def validate_todo_recovery_current(packet: dict[str, object], recovery: dict[str, object]) -> tuple[bytes, bytes, Path, str, str]:
    """Validate the exact current TODO/task bytes and current helper binding."""

    if (
        recovery.get("schema") != TODO_RECOVERY_SCHEMA
        or recovery.get("operation") != TODO_RECOVERY_OPERATION
        or recovery.get("task") != packet.get("task")
        or recovery.get("todo") != packet.get("todo")
        or recovery.get("original_todo_sha256") != packet.get("todo_sha256")
    ):
        raise TaskFrontmatterError("TODO recovery packet scope is invalid.")
    todo_input = object_map(recovery.get("current_todo_input"), "current TODO recovery input")
    todo_identity = file_identity_from(todo_input.get("file"), "current TODO recovery input")
    if todo_identity.path != str(packet["todo"]):
        raise TaskFrontmatterError("TODO recovery packet path is invalid.")
    current_todo = read_bound(Path(todo_identity.path), todo_identity.sha256, "rebound TODO")
    if todo_input != file_input(Path(todo_identity.path), "rebound TODO"):
        raise TaskFrontmatterError("current TODO recovery input changed.")
    expected_row = owned_todo_row(packet)
    try:
        encoded_rows = cast(list[object], recovery["owned_rows_base64"])
        rows = [base64.b64decode(str(value), validate=True) for value in encoded_rows]
        unrelated = base64.b64decode(str(recovery["unrelated_todo_base64"]), validate=True)
    except (KeyError, ValueError) as exc:
        raise TaskFrontmatterError("TODO recovery row encoding is invalid.") from exc
    row, chunks = partition_todo(current_todo, expected_row)
    if rows != [row] or unrelated != b"".join(chunks) or sha256(unrelated) != recovery.get("unrelated_todo_sha256"):
        raise TaskFrontmatterError("TODO recovery rows or unrelated bytes changed.")
    helper = Path(__file__).resolve(strict=True)
    if recovery.get("recovery_helper_input") != file_input(helper, "TODO recovery helper"):
        raise TaskFrontmatterError("TODO recovery helper changed.")
    current_task, manager_task, manager_target, manager_parent = validate_source1485_transition(packet, recovery)
    return current_todo, current_task, manager_task, manager_target, manager_parent


def validate_lifecycle(
    packet: dict[str, object],
    *,
    rebound_todo_data: bytes | None = None,
    rebound_task_data: bytes | None = None,
    current_manager_task: Path | None = None,
    current_manager_target: str | None = None,
    current_manager_parent: str | None = None,
) -> None:
    root = Path(str(packet["root"]))
    task = Path(str(packet["task"]))
    todo = Path(str(packet["todo"]))
    manager_task = Path(str(packet["manager_task"])) if current_manager_task is None else current_manager_task
    task_data = read_bound(task, str(packet["task_sha256"]), "protected task") if rebound_task_data is None else rebound_task_data
    todo_data = read_bound(todo, str(packet["todo_sha256"]), "TODO") if rebound_todo_data is None else rebound_todo_data
    if current_manager_task is not None:
        manager_data, manager_identity, _manager_ancestors = absolute_file_binding(manager_task, "current manager task")
        if manager_identity.uid != os.getuid() or manager_identity.size_bytes > MAX_FILE_BYTES:
            raise TaskFrontmatterError("current manager task changed.")
    else:
        manager_data = read_bound(manager_task, str(packet["manager_task_sha256"]), "manager task")
    try:
        task_metadata = parse_task_metadata(task_data.decode(), root)
        manager_metadata = parse_task_metadata(manager_data.decode(), root)
        todo_lines = todo_data.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("current lifecycle files are not UTF-8.") from exc
    task_ref = task.relative_to(root).as_posix()
    rows = [line for line in todo_lines if task_ref in line.split()]
    protected = str(packet["protected_target"])
    manager_target = str(packet["manager_target"]) if current_manager_target is None else current_manager_target
    predecessor = str(packet["predecessor_target"])
    if (
        task_metadata is None
        or task_metadata.status not in {"running", "long_running", "blocked"}
        or task_metadata.runat != protected
        or task_metadata.managerat != manager_target
        or task_metadata.is_manager
        or task_metadata.tool != "codex"
        or task_metadata.pending_task_items
        or rows != [f"{task_ref} {protected}"]
        or authoritative_active_target_task_paths(root, protected) != (task,)
        or authoritative_active_target_task_paths(root, predecessor)
        or manager_metadata is None
        or manager_metadata.status not in {"running", "long_running", "blocked"}
        or not manager_metadata.is_manager
        or manager_metadata.runat != manager_target
        or (
            current_manager_task is not None
            and (manager_metadata.tool != "codex" or current_manager_parent is None or canonical_target(manager_metadata.managerat) != canonical_target(current_manager_parent))
        )
    ):
        raise TaskFrontmatterError("current predecessor/successor lifecycle custody is invalid.")


def static_evidence(
    packet: dict[str, object],
    *,
    rebind_recoverable_helper: bool = False,
    todo_recovery: dict[str, object] | None = None,
) -> tuple[PanePin, PanePin]:
    validate_incident_digests(packet)
    prior = validate_close_artifacts(
        Path(str(packet["prior_packet"])),
        str(packet["prior_packet_sha256"]),
        Path(str(packet["prior_review"])),
        str(packet["prior_review_sha256"]),
        Path(str(packet["prepared_close_audit"])),
        str(packet["prepared_close_audit_sha256"]),
    )
    execution_replay, execution_commitment = validate_terminal_report(
        Path(str(packet["execution_report"])),
        str(packet["execution_report_sha256"]),
        executor_target="config:4",
        executor_task=Path(str(prior["root"])) / "dw_rotate_exec.md",
        required_text=(
            str(packet["prior_packet_sha256"]),
            str(packet["prepared_close_audit"]),
            EXPECTED_PRIOR_HELPER_SHA256,
            "completed predecessor resumed or changed",
            "No durable close-started/stopped proof exists",
            str(packet["predecessor_target"]),
            str(packet["protected_target"]),
        ),
    )
    recovery_replay, recovery_commitment = validate_terminal_report(
        Path(str(packet["recovery_report"])),
        str(packet["recovery_report_sha256"]),
        executor_target="config:4",
        executor_task=Path(str(prior["root"])) / "dw_rotate_exec.md",
        required_text=(
            str(packet["predecessor_target"]),
            *RECOVERY_REPORT_REQUIRED_TEXT,
            str(packet["protected_target"]),
        ),
    )
    authorization = read_bound(Path(str(packet["input_authorization"])), str(packet["input_authorization_sha256"]), "input authorization")
    if authorization not in {AUTHORIZED_INPUT.encode(), f"{AUTHORIZED_INPUT}\n".encode()} or packet.get("authorized_input") != AUTHORIZED_INPUT:
        raise TaskFrontmatterError("input authorization is not exact /status text.")
    predecessor = parse_pin(packet["predecessor_pane"], "predecessor pane")
    protected = parse_pin(packet["protected_pane"], "protected pane")
    if (
        packet.get("schema") != SCHEMA
        or packet.get("operation") != OPERATION
        or packet.get("predecessor_pane") != prior.get("predecessor_pane")
        or packet.get("predecessor_session_id") != prior.get("predecessor_session_id")
        or packet.get("protected_pane") != prior.get("protected_pane")
        or packet.get("protected_session_id") != prior.get("protected_session_id")
        or packet.get("predecessor_target") != prior.get("predecessor_target")
        or packet.get("protected_target") != prior.get("protected_target")
        or packet.get("manager_target") != prior.get("manager_target")
        or packet.get("task") != prior.get("task")
        or packet.get("todo") != prior.get("todo")
        or packet.get("manager_task") != prior.get("manager_task")
        or packet.get("execution_report_replay_id") != execution_replay
        or packet.get("recovery_report_replay_id") != recovery_replay
        or execution_commitment == recovery_commitment
        or predecessor.target == protected.target
        # 🧑 "Treat tmux sessions whose names begin with `h` as human-owned."
        or any(target.partition(":")[0].startswith("h") for target in (predecessor.target, protected.target, str(packet["manager_target"])))
        or SESSION_RE.fullmatch(str(packet["predecessor_session_id"])) is None
        or SESSION_RE.fullmatch(str(packet["protected_session_id"])) is None
    ):
        raise TaskFrontmatterError("disposition packet scope is inconsistent.")
    recovered = validate_todo_recovery_current(packet, todo_recovery) if todo_recovery is not None else None
    validate_lifecycle(
        packet,
        rebound_todo_data=None if recovered is None else recovered[0],
        rebound_task_data=None if recovered is None else recovered[1],
        current_manager_task=None if recovered is None else recovered[2],
        current_manager_target=None if recovered is None else recovered[3],
        current_manager_parent=None if recovered is None else recovered[4],
    )
    expected_inputs = [
        file_input(path, label, private=private)
        for path, label, private in (
            (Path(str(packet["prior_packet"])), "prior close packet", True),
            (Path(str(packet["prior_review"])), "prior close review", True),
            (Path(str(packet["prepared_close_audit"])), "prepared close audit", True),
            (Path(str(packet["execution_report"])), "execution report", True),
            (execution_commitment, "execution report commitment", True),
            (Path(str(packet["recovery_report"])), "recovery report", True),
            (recovery_commitment, "recovery report commitment", True),
            (Path(str(packet["input_authorization"])), "input authorization", False),
            (Path(str(prior["helper"])), "prior close helper", False),
            (Path(str(packet["task"])), "protected task", False),
            (Path(str(packet["todo"])), "TODO", False),
            (Path(str(packet["manager_task"])), "manager task", False),
            (Path(str(packet["helper"])), "input disposition helper", False),
        )
    ]
    raw_inputs = packet.get("inputs")
    if todo_recovery is not None:
        if not is_recoverable_prepared_packet(packet) or not isinstance(raw_inputs, list) or len(raw_inputs) != len(expected_inputs):
            raise TaskFrontmatterError("prepared disposition TODO recovery binding is invalid.")
        prior_todo_input = object_map(raw_inputs[-3], "recoverable TODO input")
        prior_todo_identity = file_identity_from(prior_todo_input.get("file"), "recoverable TODO input")
        prior_manager_input = object_map(raw_inputs[-2], "recoverable manager input")
        prior_manager_identity = file_identity_from(prior_manager_input.get("file"), "recoverable manager input")
        prior_task_input = object_map(raw_inputs[-4], "recoverable protected task input")
        prior_task_identity = file_identity_from(prior_task_input.get("file"), "recoverable protected task input")
        if prior_todo_identity.path != str(packet["todo"]) or prior_todo_identity.sha256 != packet["todo_sha256"]:
            raise TaskFrontmatterError("prepared disposition TODO recovery binding is invalid.")
        if prior_manager_identity.path != str(packet["manager_task"]) or prior_manager_identity.sha256 != packet["manager_task_sha256"]:
            raise TaskFrontmatterError("prepared disposition manager recovery binding is invalid.")
        if prior_task_identity.path != str(packet["task"]) or prior_task_identity.sha256 != packet["task_sha256"]:
            raise TaskFrontmatterError("prepared disposition task recovery binding is invalid.")
        expected_inputs[-4] = prior_task_input
        expected_inputs[-3] = prior_todo_input
        expected_inputs[-2] = prior_manager_input
    if rebind_recoverable_helper:
        if not is_recoverable_prepared_packet(packet) or not isinstance(raw_inputs, list) or len(raw_inputs) != len(expected_inputs):
            raise TaskFrontmatterError("prepared disposition helper recovery binding is invalid.")
        prior_helper_input = object_map(raw_inputs[-1], "recoverable input disposition helper")
        prior_helper_identity = file_identity_from(prior_helper_input.get("file"), "recoverable input disposition helper")
        if prior_helper_identity.path != str(packet["helper"]) or prior_helper_identity.sha256 != RECOVERABLE_HELPER_SHA256:
            raise TaskFrontmatterError("prepared disposition helper recovery binding is invalid.")
        expected_inputs[-1] = prior_helper_input
    if packet.get("inputs") != expected_inputs:
        raise TaskFrontmatterError("disposition packet does not bind its exact complete input set.")
    return predecessor, protected


def live_state(
    packet: dict[str, object],
    *,
    require_original_menu: bool,
    rebind_recoverable_helper: bool = False,
    todo_recovery: dict[str, object] | None = None,
) -> tuple[PanePin, PanePin, str, bytes]:
    predecessor, protected = static_evidence(
        packet,
        rebind_recoverable_helper=rebind_recoverable_helper,
        todo_recovery=todo_recovery,
    )
    if not current_pin(predecessor) or not current_pin(protected):
        raise TaskFrontmatterError("predecessor or protected pane identity changed.")
    predecessor_session = session_from_process(predecessor)
    protected_session = session_from_process(protected)
    if not current_pin(predecessor) or not current_pin(protected):
        raise TaskFrontmatterError("predecessor or protected pane identity changed during session validation.")
    if predecessor_session != packet["predecessor_session_id"] or protected_session != packet["protected_session_id"]:
        raise TaskFrontmatterError("predecessor or protected Codex session changed.")
    capture = capture_pinned(predecessor)
    state = exact_recovery_state(capture_lines(capture), str(packet["authorized_input"]))
    expected_capture = base64.b64decode(str(packet["menu_capture_base64"]), validate=True)
    if sha256(expected_capture) != packet["menu_capture_sha256"]:
        raise TaskFrontmatterError("bound menu capture bytes are invalid.")
    if require_original_menu and (state != "status_menu" or capture != expected_capture):
        raise TaskFrontmatterError("live status menu differs from the independently reviewed capture.")
    return predecessor, protected, state, capture


def packet_bytes(record: dict[str, object]) -> bytes:
    unsigned = dict(record)
    unsigned.pop("binding_id", None)
    return canonical_json({**unsigned, "binding_id": bound_receipt_id(unsigned)})


def validate_packet(data: bytes, expected_sha256: str = "") -> dict[str, object]:
    if expected_sha256 and sha256(data) != expected_sha256:
        raise TaskFrontmatterError("disposition packet digest changed.")
    packet = canonical_object(data, "input disposition packet")
    unsigned = dict(packet)
    observed_binding = unsigned.pop("binding_id", None)
    if set(packet) != PACKET_KEYS or observed_binding != bound_receipt_id(unsigned):
        raise TaskFrontmatterError("input disposition packet schema is invalid.")
    validate_incident_digests(packet)
    if not Path(str(packet["audit"])).is_absolute():
        raise TaskFrontmatterError("input disposition audit path is invalid.")
    return packet


def file_input(path: Path, label: str, *, private: bool = False) -> dict[str, object]:
    _data, identity, ancestors = absolute_file_binding(path, label, private=private)
    return {"file": asdict(identity), "ancestors": [asdict(item) for item in ancestors]}


def prepare(args: argparse.Namespace) -> None:
    if (
        args.execution_report_sha256 != EXPECTED_EXECUTION_REPORT_SHA256
        or args.recovery_report_sha256 != EXPECTED_RECOVERY_REPORT_SHA256
        or args.input_authorization_sha256 != EXPECTED_INPUT_AUTHORIZATION_SHA256
    ):
        raise TaskFrontmatterError("sender evidence is outside the exact dw8 incident.")
    prior = validate_close_artifacts(
        args.prior_packet.resolve(strict=True),
        args.prior_packet_sha256,
        args.prior_review.resolve(strict=True),
        args.prior_review_sha256,
        args.prepared_close_audit.resolve(strict=True),
        args.prepared_close_audit_sha256,
    )
    root = Path(str(prior["root"])).resolve(strict=True)
    task = Path(str(prior["task"])).resolve(strict=True)
    todo = Path(str(prior["todo"])).resolve(strict=True)
    manager_task = Path(str(prior["manager_task"])).resolve(strict=True)
    executor_task = root / "dw_rotate_exec.md"
    execution_report = args.execution_report.resolve(strict=True)
    recovery_report = args.recovery_report.resolve(strict=True)
    execution_replay, execution_commitment = validate_terminal_report(
        execution_report,
        args.execution_report_sha256,
        executor_target="config:4",
        executor_task=executor_task,
        required_text=(
            args.prior_packet_sha256,
            str(args.prepared_close_audit.resolve(strict=True)),
            EXPECTED_PRIOR_HELPER_SHA256,
            "completed predecessor resumed or changed",
            "No durable close-started/stopped proof exists",
            str(prior["predecessor_target"]),
            str(prior["protected_target"]),
        ),
    )
    recovery_replay, recovery_commitment = validate_terminal_report(
        recovery_report,
        args.recovery_report_sha256,
        executor_target="config:4",
        executor_task=executor_task,
        required_text=(
            str(prior["predecessor_target"]),
            *RECOVERY_REPORT_REQUIRED_TEXT,
            str(prior["protected_target"]),
        ),
    )
    authorization_path = args.input_authorization.resolve(strict=True)
    authorization_data, authorization_identity, _ = absolute_file_binding(authorization_path, "input authorization")
    if sha256(authorization_data) != args.input_authorization_sha256 or authorization_data not in {AUTHORIZED_INPUT.encode(), f"{AUTHORIZED_INPUT}\n".encode()}:
        raise TaskFrontmatterError("input authorization is not exact /status text.")
    helper = Path(__file__).resolve(strict=True)
    _helper_data, helper_identity, _ = absolute_file_binding(helper, "input disposition helper")
    _task_data, task_identity, _ = absolute_file_binding(task, "protected task")
    _todo_data, todo_identity, _ = absolute_file_binding(todo, "TODO")
    _manager_data, manager_identity, _ = absolute_file_binding(manager_task, "manager task")
    predecessor = parse_pin(prior["predecessor_pane"], "predecessor pane")
    protected = parse_pin(prior["protected_pane"], "protected pane")
    output = args.output.resolve(strict=False)
    audit = args.audit.resolve(strict=False)
    prepared_close_audit = args.prepared_close_audit.resolve(strict=True)
    reserved = {
        args.prior_packet.resolve(strict=True),
        args.prior_review.resolve(strict=True),
        execution_report,
        execution_commitment,
        recovery_report,
        recovery_commitment,
        authorization_path,
        Path(str(prior["helper"])).resolve(strict=True),
        task,
        todo,
        manager_task,
        helper,
    } | prior_close_control_paths(Path(str(prior["audit"])), prepared_close_audit)
    validate_disposition_output_paths(output, audit, reserved)
    provisional: dict[str, object] = {
        "schema": SCHEMA,
        "operation": OPERATION,
        "root": str(root),
        "prior_packet": str(args.prior_packet.resolve(strict=True)),
        "prior_packet_sha256": args.prior_packet_sha256,
        "prior_review": str(args.prior_review.resolve(strict=True)),
        "prior_review_sha256": args.prior_review_sha256,
        "prepared_close_audit": str(prepared_close_audit),
        "prepared_close_audit_sha256": args.prepared_close_audit_sha256,
        "execution_report": str(execution_report),
        "execution_report_sha256": args.execution_report_sha256,
        "execution_report_replay_id": execution_replay,
        "recovery_report": str(recovery_report),
        "recovery_report_sha256": args.recovery_report_sha256,
        "recovery_report_replay_id": recovery_replay,
        "input_authorization": str(authorization_path),
        "input_authorization_sha256": authorization_identity.sha256,
        "authorized_input": AUTHORIZED_INPUT,
        "task": str(task),
        "task_sha256": task_identity.sha256,
        "todo": str(todo),
        "todo_sha256": todo_identity.sha256,
        "manager_task": str(manager_task),
        "manager_task_sha256": manager_identity.sha256,
        "manager_target": prior["manager_target"],
        "predecessor_target": prior["predecessor_target"],
        "predecessor_pane": prior["predecessor_pane"],
        "predecessor_session_id": prior["predecessor_session_id"],
        "protected_target": prior["protected_target"],
        "protected_pane": prior["protected_pane"],
        "protected_session_id": prior["protected_session_id"],
        "menu_capture_base64": "",
        "menu_capture_sha256": "",
        "helper": str(helper),
        "helper_sha256": helper_identity.sha256,
        "audit": str(audit),
        "inputs": [
            file_input(path, label, private=private)
            for path, label, private in (
                (args.prior_packet.resolve(strict=True), "prior close packet", True),
                (args.prior_review.resolve(strict=True), "prior close review", True),
                (args.prepared_close_audit.resolve(strict=True), "prepared close audit", True),
                (execution_report, "execution report", True),
                (execution_commitment, "execution report commitment", True),
                (recovery_report, "recovery report", True),
                (recovery_commitment, "recovery report commitment", True),
                (authorization_path, "input authorization", False),
                (Path(str(prior["helper"])), "prior close helper", False),
                (task, "protected task", False),
                (todo, "TODO", False),
                (manager_task, "manager task", False),
                (helper, "input disposition helper", False),
            )
        ],
    }
    validate_incident_digests(provisional)
    validate_lifecycle(provisional)
    if not current_pin(predecessor) or not current_pin(protected):
        raise TaskFrontmatterError("predecessor or protected pane identity changed.")
    if session_from_process(predecessor) != prior["predecessor_session_id"] or session_from_process(protected) != prior["protected_session_id"]:
        raise TaskFrontmatterError("predecessor or protected Codex session changed.")
    menu_capture = capture_pinned(predecessor)
    if not exact_status_menu(capture_lines(menu_capture)):
        raise TaskFrontmatterError("predecessor does not show the exact /status completion menu.")
    record = {
        **provisional,
        "menu_capture_base64": base64.b64encode(menu_capture).decode(),
        "menu_capture_sha256": sha256(menu_capture),
    }
    data = packet_bytes(record)
    publish_or_validate(output, data, "input disposition packet")
    print(sha256(data))


def review_record(packet: dict[str, object], packet_sha256: str) -> dict[str, object]:
    predecessor, protected, state, capture = live_state(packet, require_original_menu=True)
    if state != "status_menu":
        raise TaskFrontmatterError("independent review did not observe the exact status menu.")
    return {
        "schema": REVIEW_SCHEMA,
        "verdict": "PASS",
        "packet_sha256": packet_sha256,
        "menu_capture_sha256": sha256(capture),
        "predecessor_pane": pane_record(predecessor),
        "predecessor_session_id": packet["predecessor_session_id"],
        "protected_pane": pane_record(protected),
        "protected_session_id": packet["protected_session_id"],
    }


def review(args: argparse.Namespace) -> None:
    packet = validate_packet(read_bound(args.packet, args.packet_sha256, "input disposition packet", private=True), args.packet_sha256)
    print(json.dumps(review_record(packet, args.packet_sha256), sort_keys=True, separators=(",", ":")))


def validate_review(path: Path, expected_sha256: str, packet: dict[str, object], packet_sha256: str) -> None:
    record = canonical_object(read_bound(path, expected_sha256, "input disposition review", private=True), "input disposition review")
    expected = {
        "schema": REVIEW_SCHEMA,
        "verdict": "PASS",
        "packet_sha256": packet_sha256,
        "menu_capture_sha256": packet["menu_capture_sha256"],
        "predecessor_pane": packet["predecessor_pane"],
        "predecessor_session_id": packet["predecessor_session_id"],
        "protected_pane": packet["protected_pane"],
        "protected_session_id": packet["protected_session_id"],
    }
    if record != expected:
        raise TaskFrontmatterError("independent review does not PASS this exact menu disposition.")


def validate_todo_recovery_packet(
    recovery: dict[str, object],
    recovery_sha256: str,
    packet: dict[str, object],
    packet_path: Path,
    review_path: Path,
    prepared_path: Path,
) -> bytes:
    if sha256(todo_recovery_bytes(recovery)) != recovery_sha256:
        raise TaskFrontmatterError("TODO recovery packet hash is invalid.")
    expected = {
        "packet": str(packet_path),
        "packet_sha256": RECOVERABLE_PACKET_SHA256,
        "review_report": str(review_path),
        "review_report_sha256": RECOVERABLE_REVIEW_SHA256,
        "prepared_audit": str(prepared_path),
        "prepared_audit_sha256": RECOVERABLE_PREPARED_AUDIT_SHA256,
    }
    if any(recovery.get(key) != value for key, value in expected.items()) or not is_recoverable_prepared_packet(packet):
        raise TaskFrontmatterError("TODO recovery packet does not bind the exact failed disposition.")
    prepared_data = prepared_disposition_audit(packet, RECOVERABLE_PACKET_SHA256)
    if (
        sha256(prepared_data) != RECOVERABLE_PREPARED_AUDIT_SHA256
        or read_bound(
            prepared_path,
            RECOVERABLE_PREPARED_AUDIT_SHA256,
            "recoverable prepared disposition audit",
            private=True,
        )
        != prepared_data
    ):
        raise TaskFrontmatterError("recoverable prepared disposition audit changed.")
    validate_review(review_path, RECOVERABLE_REVIEW_SHA256, packet, RECOVERABLE_PACKET_SHA256)
    current_todo, _current_task, _manager_task, _manager_target, _manager_parent = validate_todo_recovery_current(packet, recovery)
    static_evidence(packet, rebind_recoverable_helper=True, todo_recovery=recovery)
    return current_todo


def todo_recovery_review_record(recovery: dict[str, object], recovery_sha256: str) -> dict[str, object]:
    todo_identity = file_identity_from(
        object_map(recovery["current_todo_input"], "current TODO recovery input").get("file"),
        "current TODO recovery input",
    )
    helper_identity = file_identity_from(
        object_map(recovery["recovery_helper_input"], "TODO recovery helper").get("file"),
        "TODO recovery helper",
    )
    task_identity = file_identity_from(
        object_map(recovery["current_task_input"], "current task recovery input").get("file"),
        "current task recovery input",
    )
    return {
        "schema": TODO_RECOVERY_REVIEW_SCHEMA,
        "verdict": "PASS",
        "recovery_packet_sha256": recovery_sha256,
        "packet_sha256": recovery["packet_sha256"],
        "prepared_audit_sha256": recovery["prepared_audit_sha256"],
        "original_todo_sha256": recovery["original_todo_sha256"],
        "current_todo_sha256": todo_identity.sha256,
        "original_task_sha256": recovery["original_task_sha256"],
        "current_task_sha256": task_identity.sha256,
        "source1485_root_audit_sha256": recovery["source1485_root_audit_sha256"],
        "current_manager_task": recovery["current_manager_task"],
        "current_manager_target": recovery["current_manager_target"],
        "unrelated_todo_sha256": recovery["unrelated_todo_sha256"],
        "recovery_helper_sha256": helper_identity.sha256,
    }


def validate_todo_recovery_review(path: Path, expected_sha256: str, recovery: dict[str, object], recovery_sha256: str) -> None:
    record = canonical_object(read_bound(path, expected_sha256, "TODO recovery review", private=True), "TODO recovery review")
    if record != todo_recovery_review_record(recovery, recovery_sha256):
        raise TaskFrontmatterError("independent review does not PASS this exact TODO recovery.")


def load_exact_failed_disposition(args: argparse.Namespace) -> tuple[dict[str, object], Path, Path, Path]:
    packet_path = args.packet.resolve(strict=True)
    review_path = args.review_report.resolve(strict=True)
    packet = validate_packet(read_bound(packet_path, args.packet_sha256, "input disposition packet", private=True), args.packet_sha256)
    if args.packet_sha256 != RECOVERABLE_PACKET_SHA256 or args.review_report_sha256 != RECOVERABLE_REVIEW_SHA256:
        raise TaskFrontmatterError("TODO recovery requires the exact failed disposition and review.")
    validate_review(review_path, args.review_report_sha256, packet, args.packet_sha256)
    prepared_path = Path(f"{packet['audit']}.prepared").resolve(strict=True)
    return packet, packet_path, review_path, prepared_path


def prepare_todo_recovery(args: argparse.Namespace) -> None:
    packet, packet_path, review_path, prepared_path = load_exact_failed_disposition(args)
    output = args.todo_recovery_output.resolve(strict=False)
    current_manager_task = Path(str(packet["root"])) / "dw_root_new.md"
    prior_prepared = Path(str(packet["prepared_close_audit"])).resolve(strict=True)
    if not str(prior_prepared).endswith(".prepared"):
        raise TaskFrontmatterError("prepared close audit path is malformed.")
    prior_complete = Path(str(prior_prepared)[: -len(".prepared")])
    raw_inputs = packet.get("inputs")
    if not isinstance(raw_inputs, list):
        raise TaskFrontmatterError("disposition packet input set is invalid.")
    reserved = {
        packet_path,
        review_path,
        prepared_path,
        Path(str(packet["audit"])).resolve(strict=False),
        prior_prepared,
        Path(str(packet["task"])).resolve(strict=True),
        Path(str(packet["todo"])).resolve(strict=True),
        Path(str(packet["manager_task"])).resolve(strict=True),
        args.source1485_root_audit.resolve(strict=True),
        current_manager_task.resolve(strict=True),
    } | prior_close_control_paths(prior_complete, prior_prepared)
    reserved |= {Path(file_identity_from(object_map(item, "disposition packet input").get("file"), "disposition packet input").path) for item in raw_inputs}
    if output in reserved:
        raise TaskFrontmatterError("TODO recovery output overlaps immutable or lifecycle evidence.")
    with ExitStack() as stack:
        for path in sorted({Path(str(packet[key])) for key in ("task", "todo", "manager_task")} | {current_manager_task}, key=str):
            stack.enter_context(task_file_lock(path))
        task = Path(str(packet["task"]))
        _task_data, task_identity, task_ancestors = absolute_file_binding(task, "rebound protected task")
        held_task = hold_absolute(task_identity, task_ancestors)
        stack.callback(os.close, held_task.descriptor)
        for descriptor in reversed(held_task.directories):
            stack.callback(os.close, descriptor)
        root_audit = args.source1485_root_audit.resolve(strict=True)
        _root_audit_data, root_audit_identity, root_audit_ancestors = absolute_file_binding(root_audit, "Source-1485 root audit", private=True)
        if root_audit_identity.sha256 != args.source1485_root_audit_sha256:
            raise TaskFrontmatterError("Source-1485 root audit changed.")
        held_root_audit = hold_absolute(root_audit_identity, root_audit_ancestors)
        stack.callback(os.close, held_root_audit.descriptor)
        for descriptor in reversed(held_root_audit.directories):
            stack.callback(os.close, descriptor)
        todo = Path(str(packet["todo"]))
        todo_data, todo_identity, todo_ancestors = absolute_file_binding(todo, "rebound TODO")
        held_todo = hold_absolute(todo_identity, todo_ancestors)
        stack.callback(os.close, held_todo.descriptor)
        for descriptor in reversed(held_todo.directories):
            stack.callback(os.close, descriptor)
        helper = Path(__file__).resolve(strict=True)
        _helper_data, helper_identity, helper_ancestors = absolute_file_binding(helper, "TODO recovery helper")
        held_helper = hold_absolute(helper_identity, helper_ancestors)
        stack.callback(os.close, held_helper.descriptor)
        for descriptor in reversed(held_helper.directories):
            stack.callback(os.close, descriptor)
        row, chunks = partition_todo(todo_data, owned_todo_row(packet))
        unsigned: dict[str, object] = {
            "schema": TODO_RECOVERY_SCHEMA,
            "operation": TODO_RECOVERY_OPERATION,
            "packet": str(packet_path),
            "packet_sha256": args.packet_sha256,
            "review_report": str(review_path),
            "review_report_sha256": args.review_report_sha256,
            "prepared_audit": str(prepared_path),
            "prepared_audit_sha256": RECOVERABLE_PREPARED_AUDIT_SHA256,
            "task": packet["task"],
            "original_task_sha256": packet["task_sha256"],
            "current_task_input": {
                "file": asdict(task_identity),
                "ancestors": [asdict(item) for item in task_ancestors],
            },
            "todo": packet["todo"],
            "original_todo_sha256": packet["todo_sha256"],
            "owned_rows_base64": [base64.b64encode(row).decode()],
            "unrelated_todo_base64": base64.b64encode(b"".join(chunks)).decode(),
            "unrelated_todo_sha256": sha256(b"".join(chunks)),
            "current_todo_input": file_input(todo, "rebound TODO"),
            "recovery_helper_input": {
                "file": asdict(helper_identity),
                "ancestors": [asdict(item) for item in helper_ancestors],
            },
            "source1485_root_audit": str(args.source1485_root_audit.resolve(strict=True)),
            "source1485_root_audit_sha256": args.source1485_root_audit_sha256,
            "current_manager_task": str(current_manager_task),
            "current_manager_target": "dw:15",
        }
        recovery = {**unsigned, "binding_id": bound_receipt_id(unsigned)}
        data = todo_recovery_bytes(recovery)
        validate_todo_recovery_packet(recovery, sha256(data), packet, packet_path, review_path, prepared_path)
        validate_held_absolute(held_todo)
        validate_held_absolute(held_task)
        validate_held_absolute(held_root_audit)
        validate_held_absolute(held_helper)
        publish_or_validate(output, data, "TODO recovery packet")
    print(sha256(data))


def review_todo_recovery(args: argparse.Namespace) -> None:
    packet, packet_path, review_path, prepared_path = load_exact_failed_disposition(args)
    recovery_path = args.todo_recovery_packet.resolve(strict=True)
    recovery = canonical_object(
        read_bound(recovery_path, args.todo_recovery_packet_sha256, "TODO recovery packet", private=True),
        "TODO recovery packet",
    )
    with ExitStack() as stack:
        manager_task = Path(str(recovery["current_manager_task"]))
        for path in sorted({Path(str(packet[key])) for key in ("task", "todo", "manager_task")} | {manager_task}, key=str):
            stack.enter_context(task_file_lock(path))
        root_audit = Path(str(recovery["source1485_root_audit"]))
        _audit_data, audit_identity, audit_ancestors = absolute_file_binding(root_audit, "Source-1485 root audit", private=True)
        if audit_identity.sha256 != recovery["source1485_root_audit_sha256"]:
            raise TaskFrontmatterError("Source-1485 root audit changed.")
        held_audit = hold_absolute(audit_identity, audit_ancestors)
        stack.callback(os.close, held_audit.descriptor)
        for descriptor in reversed(held_audit.directories):
            stack.callback(os.close, descriptor)
        validate_todo_recovery_packet(recovery, args.todo_recovery_packet_sha256, packet, packet_path, review_path, prepared_path)
        validate_held_absolute(held_audit)
    print(json.dumps(todo_recovery_review_record(recovery, args.todo_recovery_packet_sha256), sort_keys=True, separators=(",", ":")))


def prepared_disposition_audit(packet: dict[str, object], packet_sha256: str) -> bytes:
    fields = {key: packet[key] for key in AUDIT_KEYS if key in packet}
    fields.update({"schema": SCHEMA, "operation": OPERATION, "state": "prepared", "packet_sha256": packet_sha256})
    return canonical_json(fields)


def complete_disposition_audit(packet: dict[str, object], packet_sha256: str, prepared_sha256: str) -> bytes:
    prepared = canonical_object(prepared_disposition_audit(packet, packet_sha256), "prepared input disposition audit")
    return canonical_json(
        {
            **prepared,
            "state": "complete",
            "prepared_disposition_audit_sha256": prepared_sha256,
            "result": "stale status input cancelled; prior close packet superseded and must not be reused",
            "final_state": "ready",
        }
    )


def hold_inputs(
    packet: dict[str, object],
    stack: ExitStack,
    *,
    rebind_recoverable_helper: bool = False,
    todo_recovery: dict[str, object] | None = None,
) -> list[HeldAbsolute]:
    raw_inputs = packet.get("inputs")
    if not isinstance(raw_inputs, list):
        raise TaskFrontmatterError("disposition packet input set is invalid.")
    if rebind_recoverable_helper and not is_recoverable_prepared_packet(packet):
        raise TaskFrontmatterError("prepared disposition helper recovery binding is invalid.")
    held: list[HeldAbsolute] = []
    helper_rebound = False
    task_rebound = False
    todo_rebound = False
    manager_rebound = False
    for value in raw_inputs:
        item = object_map(value, "disposition packet input")
        identity = file_identity_from(item.get("file"), "disposition packet input")
        raw_ancestors = item.get("ancestors")
        if not isinstance(raw_ancestors, Iterable) or isinstance(raw_ancestors, (str, bytes, dict)):
            raise TaskFrontmatterError("disposition packet input ancestors are invalid.")
        ancestors = tuple(directory_identity_from(entry, "disposition packet input ancestor") for entry in cast(Iterable[object], raw_ancestors))
        if todo_recovery is not None and identity.path == str(packet["todo"]):
            if todo_rebound or identity.sha256 != packet["todo_sha256"]:
                raise TaskFrontmatterError("prepared disposition TODO recovery binding is invalid.")
            current_input = object_map(todo_recovery.get("current_todo_input"), "current TODO recovery input")
            current_identity = file_identity_from(current_input.get("file"), "current TODO recovery input")
            raw_current_ancestors = current_input.get("ancestors")
            if not isinstance(raw_current_ancestors, Iterable) or isinstance(raw_current_ancestors, (str, bytes, dict)):
                raise TaskFrontmatterError("current TODO recovery input ancestors are invalid.")
            current_ancestors = tuple(directory_identity_from(entry, "current TODO recovery input ancestor") for entry in cast(Iterable[object], raw_current_ancestors))
            current = hold_absolute(current_identity, current_ancestors)
            todo_rebound = True
        elif todo_recovery is not None and identity.path == str(packet["task"]):
            if task_rebound or identity.sha256 != packet["task_sha256"]:
                raise TaskFrontmatterError("prepared disposition task recovery binding is invalid.")
            current_input = object_map(todo_recovery.get("current_task_input"), "current task recovery input")
            current_identity = file_identity_from(current_input.get("file"), "current task recovery input")
            raw_current_ancestors = current_input.get("ancestors")
            if not isinstance(raw_current_ancestors, Iterable) or isinstance(raw_current_ancestors, (str, bytes, dict)):
                raise TaskFrontmatterError("current task recovery input ancestors are invalid.")
            current_ancestors = tuple(directory_identity_from(entry, "current task recovery input ancestor") for entry in cast(Iterable[object], raw_current_ancestors))
            current = hold_absolute(current_identity, current_ancestors)
            task_rebound = True
        elif todo_recovery is not None and identity.path == str(packet["manager_task"]):
            if manager_rebound or identity.sha256 != packet["manager_task_sha256"]:
                raise TaskFrontmatterError("prepared disposition manager recovery binding is invalid.")
            _data, current_identity, current_ancestors = absolute_file_binding(
                Path(str(todo_recovery["current_manager_task"])),
                "current manager recovery input",
            )
            current = hold_absolute(current_identity, current_ancestors)
            manager_rebound = True
        elif rebind_recoverable_helper and identity.path == str(packet["helper"]):
            if helper_rebound or identity.sha256 != RECOVERABLE_HELPER_SHA256:
                raise TaskFrontmatterError("prepared disposition helper recovery binding is invalid.")
            if todo_recovery is None:
                helper = Path(__file__).resolve(strict=True)
                _data, current_identity, current_ancestors = absolute_file_binding(
                    helper,
                    "current input disposition recovery helper",
                )
            else:
                current_input = object_map(todo_recovery.get("recovery_helper_input"), "TODO recovery helper")
                current_identity = file_identity_from(current_input.get("file"), "TODO recovery helper")
                raw_current_ancestors = current_input.get("ancestors")
                if not isinstance(raw_current_ancestors, Iterable) or isinstance(raw_current_ancestors, (str, bytes, dict)):
                    raise TaskFrontmatterError("TODO recovery helper ancestors are invalid.")
                current_ancestors = tuple(directory_identity_from(entry, "TODO recovery helper ancestor") for entry in cast(Iterable[object], raw_current_ancestors))
            current = hold_absolute(current_identity, current_ancestors)
            helper_rebound = True
        else:
            current = hold_absolute(identity, ancestors)
        held.append(current)
        stack.callback(os.close, current.descriptor)
        for descriptor in reversed(current.directories):
            stack.callback(os.close, descriptor)
    if rebind_recoverable_helper and not helper_rebound:
        raise TaskFrontmatterError("prepared disposition helper recovery binding is absent.")
    if todo_recovery is not None and not todo_rebound:
        raise TaskFrontmatterError("prepared disposition TODO recovery binding is absent.")
    if todo_recovery is not None and not task_rebound:
        raise TaskFrontmatterError("prepared disposition task recovery binding is absent.")
    if todo_recovery is not None and not manager_rebound:
        raise TaskFrontmatterError("prepared disposition manager recovery binding is absent.")
    if todo_recovery is not None:
        audit_path = Path(str(todo_recovery["source1485_root_audit"]))
        _audit_data, audit_identity, audit_ancestors = absolute_file_binding(audit_path, "Source-1485 root audit", private=True)
        if audit_identity.sha256 != todo_recovery["source1485_root_audit_sha256"]:
            raise TaskFrontmatterError("Source-1485 root audit changed.")
        current = hold_absolute(audit_identity, audit_ancestors)
        held.append(current)
        stack.callback(os.close, current.descriptor)
        for descriptor in reversed(current.directories):
            stack.callback(os.close, descriptor)
    return held


def authorize_prepared_helper_recovery(
    packet: dict[str, object],
    packet_sha256: str,
    review_sha256: str,
    prepared_path: Path,
    prepared_data: bytes,
) -> bool:
    """Rebind the helper only for the exact immutable failed execution."""

    if (
        packet_sha256 != RECOVERABLE_PACKET_SHA256
        or review_sha256 != RECOVERABLE_REVIEW_SHA256
        or sha256(prepared_data) != RECOVERABLE_PREPARED_AUDIT_SHA256
        or not is_recoverable_prepared_packet(packet)
    ):
        return False
    observed = read_bound(
        prepared_path,
        RECOVERABLE_PREPARED_AUDIT_SHA256,
        "recoverable prepared disposition audit",
        private=True,
    )
    if observed != prepared_data:
        raise TaskFrontmatterError("recoverable prepared disposition audit changed.")
    return True


def execute(args: argparse.Namespace) -> None:
    packet_path = args.packet.resolve(strict=False)
    packet = validate_packet(read_bound(packet_path, args.packet_sha256, "input disposition packet", private=True), args.packet_sha256)
    if args.review_report is None:
        raise TaskFrontmatterError("execute requires one independent PASS report.")
    review_path = args.review_report.resolve(strict=False)
    validate_review(review_path, args.review_report_sha256, packet, args.packet_sha256)
    root = Path(str(packet["root"]))
    predecessor = parse_pin(packet["predecessor_pane"], "predecessor pane")
    protected = parse_pin(packet["protected_pane"], "protected pane")
    prepared_path = Path(f"{packet['audit']}.prepared").resolve(strict=False)
    complete_path = Path(str(packet["audit"])).resolve(strict=False)
    raw_inputs = packet["inputs"]
    if not isinstance(raw_inputs, list):
        raise TaskFrontmatterError("disposition packet input set is invalid.")
    reserved = {Path(file_identity_from(object_map(item, "disposition packet input").get("file"), "disposition packet input").path) for item in raw_inputs}
    prior_prepared = Path(str(packet["prepared_close_audit"])).resolve(strict=False)
    if not str(prior_prepared).endswith(".prepared"):
        raise TaskFrontmatterError("prepared close audit path is malformed.")
    prior_complete = Path(str(prior_prepared)[: -len(".prepared")])
    reserved |= prior_close_control_paths(prior_complete, prior_prepared)
    if packet_path in {prepared_path, complete_path} or review_path in {prepared_path, complete_path} or {prepared_path, complete_path} & reserved:
        raise TaskFrontmatterError("disposition audit paths overlap immutable evidence.")
    prepared_data = prepared_disposition_audit(packet, args.packet_sha256)
    prepared_sha = sha256(prepared_data)
    complete_data = complete_disposition_audit(packet, args.packet_sha256, prepared_sha)
    recovery_values = tuple(getattr(args, name, None) for name in ("todo_recovery_packet", "todo_recovery_packet_sha256", "todo_recovery_review", "todo_recovery_review_sha256"))
    if any(recovery_values) and not all(recovery_values):
        raise TaskFrontmatterError("execute requires the complete TODO recovery packet and review binding.")
    todo_recovery: dict[str, object] | None = None
    recovery_path: Path | None = None
    recovery_review_path: Path | None = None
    if all(recovery_values):
        recovery_path = args.todo_recovery_packet.resolve(strict=True)
        recovery_review_path = args.todo_recovery_review.resolve(strict=True)
        if {recovery_path, recovery_review_path} & ({prepared_path, complete_path} | reserved) or recovery_path == recovery_review_path:
            raise TaskFrontmatterError("TODO recovery paths overlap immutable evidence or disposition outputs.")
        todo_recovery = canonical_object(
            read_bound(recovery_path, args.todo_recovery_packet_sha256, "TODO recovery packet", private=True),
            "TODO recovery packet",
        )
        if {
            Path(str(todo_recovery["source1485_root_audit"])).resolve(strict=True),
            Path(str(todo_recovery["current_manager_task"])).resolve(strict=True),
        } & ({prepared_path, complete_path, recovery_path, recovery_review_path} | reserved):
            raise TaskFrontmatterError("TODO recovery paths overlap immutable evidence or disposition outputs.")

    with (
        tmux_input_lock(predecessor.target),
        task_target_lock(root, predecessor.target),
        task_target_lock(root, protected.target),
        ExitStack() as stack,
    ):
        lock_paths = {Path(str(packet[key])) for key in ("task", "todo", "manager_task")}
        if todo_recovery is not None:
            lock_paths.add(Path(str(todo_recovery["current_manager_task"])))
        for path in sorted(lock_paths, key=str):
            stack.enter_context(task_file_lock(path))
        prepared_exists = path_entry_exists(prepared_path)
        if todo_recovery is not None:
            if not prepared_exists or recovery_path is None or recovery_review_path is None:
                raise TaskFrontmatterError("TODO recovery requires the exact prepared disposition.")
            validate_todo_recovery_packet(
                todo_recovery,
                args.todo_recovery_packet_sha256,
                packet,
                packet_path,
                review_path,
                prepared_path,
            )
            validate_todo_recovery_review(
                recovery_review_path,
                args.todo_recovery_review_sha256,
                todo_recovery,
                args.todo_recovery_packet_sha256,
            )
        rebind_recoverable_helper = prepared_exists and authorize_prepared_helper_recovery(
            packet,
            args.packet_sha256,
            args.review_report_sha256,
            prepared_path,
            prepared_data,
        )
        held = hold_inputs(
            packet,
            stack,
            rebind_recoverable_helper=rebind_recoverable_helper,
            todo_recovery=todo_recovery,
        )

        def current_state(*, allowed: set[str], require_original_menu: bool = False) -> tuple[str, bytes]:
            for item in held:
                validate_held_absolute(item)
            _predecessor, _protected, state, capture = live_state(
                packet,
                require_original_menu=require_original_menu,
                rebind_recoverable_helper=rebind_recoverable_helper,
                todo_recovery=todo_recovery,
            )
            for item in held:
                validate_held_absolute(item)
            if _predecessor != predecessor or _protected != protected:
                raise TaskFrontmatterError("live disposition pane binding changed.")
            if state not in allowed:
                raise TaskFrontmatterError("status-menu recovery entered an unsupported state.")
            return state, capture

        complete_exists = path_entry_exists(complete_path)
        if complete_exists:
            if not prepared_exists:
                raise TaskFrontmatterError("complete disposition exists without its prepared audit.")
            publish_or_validate(prepared_path, prepared_data, "prepared input disposition audit")
            publish_or_validate(complete_path, complete_data, "complete input disposition audit")
            current_state(allowed={"ready"})
            print(complete_path)
            return
        state, _capture = current_state(
            allowed={"status_menu", "status_input", "ready"} if prepared_exists else {"status_menu"},
            require_original_menu=not prepared_exists,
        )
        publish_or_validate(prepared_path, prepared_data, "prepared input disposition audit")

        if state == "status_menu":
            state, _capture = guarded_tmux_command_for_capture(
                predecessor,
                ["send-keys", "-t", predecessor.pane_id, "Escape"],
                validate_before=lambda: current_state(allowed={"status_menu"}, require_original_menu=True),
                validate_after=lambda: current_state(allowed={"status_input", "ready"}),
            )
        if state == "status_input":
            state, _capture = guarded_tmux_command_for_capture(
                predecessor,
                ["send-keys", "-t", predecessor.pane_id, "C-c"],
                validate_before=lambda: current_state(allowed={"status_input"}),
                validate_after=lambda: current_state(allowed={"status_input", "ready"}),
            )
            deadline = time.monotonic() + args.wait_s
            while state != "ready":
                state, _capture = current_state(allowed={"status_input", "ready"})
                if state != "status_input" or time.monotonic() >= deadline:
                    raise TaskFrontmatterError("stale status input cancellation did not reach a ready view.")
                time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))
        if state != "ready":
            raise TaskFrontmatterError("stale status input disposition lacks a ready result.")
        current_state(allowed={"ready"})
        publish_or_validate(complete_path, complete_data, "complete input disposition audit")
    print(complete_path)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--review", action="store_true")
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--prepare-todo-recovery", action="store_true")
    modes.add_argument("--review-todo-recovery", action="store_true")
    result.add_argument("--prior-packet", type=Path)
    result.add_argument("--prior-packet-sha256", default="")
    result.add_argument("--prior-review", type=Path)
    result.add_argument("--prior-review-sha256", default="")
    result.add_argument("--prepared-close-audit", type=Path)
    result.add_argument("--prepared-close-audit-sha256", default="")
    result.add_argument("--execution-report", type=Path)
    result.add_argument("--execution-report-sha256", default="")
    result.add_argument("--recovery-report", type=Path)
    result.add_argument("--recovery-report-sha256", default="")
    result.add_argument("--input-authorization", type=Path)
    result.add_argument("--input-authorization-sha256", default="")
    result.add_argument("--audit", type=Path)
    result.add_argument("--output", type=Path)
    result.add_argument("--packet", type=Path)
    result.add_argument("--packet-sha256", default="")
    result.add_argument("--review-report", type=Path)
    result.add_argument("--review-report-sha256", default="")
    result.add_argument("--todo-recovery-output", type=Path)
    result.add_argument("--source1485-root-audit", type=Path)
    result.add_argument("--source1485-root-audit-sha256", default="")
    result.add_argument("--todo-recovery-packet", type=Path)
    result.add_argument("--todo-recovery-packet-sha256", default="")
    result.add_argument("--todo-recovery-review", type=Path)
    result.add_argument("--todo-recovery-review-sha256", default="")
    result.add_argument("--wait-s", type=float, default=3.0)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.wait_s <= 0:
            raise TaskFrontmatterError("wait bound must be positive.")
        if args.prepare:
            required = (
                args.prior_packet,
                args.prior_review,
                args.prepared_close_audit,
                args.execution_report,
                args.recovery_report,
                args.input_authorization,
                args.audit,
                args.output,
            )
            if any(value is None for value in required) or not args.audit.is_absolute() or not args.output.is_absolute():
                raise TaskFrontmatterError("prepare requires every evidence path and absolute audit/output paths.")
            prepare(args)
        elif args.review:
            if args.packet is None:
                raise TaskFrontmatterError("review requires a packet.")
            review(args)
        elif args.prepare_todo_recovery:
            if (
                args.packet is None
                or args.review_report is None
                or args.todo_recovery_output is None
                or args.source1485_root_audit is None
                or not args.todo_recovery_output.is_absolute()
                or not args.source1485_root_audit.is_absolute()
            ):
                raise TaskFrontmatterError("prepare TODO recovery requires packet, review, root audit, and absolute output paths.")
            prepare_todo_recovery(args)
        elif args.review_todo_recovery:
            if args.packet is None or args.review_report is None or args.todo_recovery_packet is None:
                raise TaskFrontmatterError("review TODO recovery requires disposition packet/review and recovery packet.")
            review_todo_recovery(args)
        else:
            if args.packet is None:
                raise TaskFrontmatterError("execute requires a packet.")
            execute(args)
    except (CustodyError, OSError, TaskFrontmatterError, UnicodeError, ValueError) as exc:
        print(f"omo_stale_predecessor_input_disposition.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
