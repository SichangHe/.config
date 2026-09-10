#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Close the exact transferred DeepWiki manager and its stale same-target record."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_exported_agent_close import (
    closed_todo,
    read_regular,
    read_regular_unbound,
    replace_held,
)
from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_codex_stop import Args as CodexStopArgs
from omo_manager.omo_codex_stop import (
    bound_guarded_read,
    codex_status,
    done_live_close_started_path,
    has_bound_close_proof,
    path_entry_exists,
    promote_done_live_close_started,
    query_status_session_id,
    stop as guarded_codex_stop,
)
from omo_manager.omo_repository_custody import (
    CustodyError,
    absolute_file_binding,
    authenticated_report,
    directory_identity_from,
    existing_exact,
    file_identity_from,
    hold_absolute,
    publish_or_validate,
    validate_held_absolute,
)
from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, TaskMetadata, parse_task_metadata
from omo_manager.omo_task_status import (
    active_child_task_refs,
    authoritative_active_target_task_paths,
    cleared_pending_task_text,
    has_pending_marker,
    root_membership_lock,
    todo_row_task_paths,
    update_frontmatter_status,
)

SCHEMA = "omo-transferred-manager-close/v1"
REVIEW_SCHEMA = "omo-transferred-manager-close-review/v1"
TRUSTED_ROOT = Path("/ssd1/sichangheagent/work_logs")
SOURCE_TASK = "dw_tree_replace.md"
REPLACEMENT_TASK = "cleanup_dw_tree.md"
STALE_TASK = "dw_rotate_repair.md"
EXPECTED_HISTORICAL_CHILDREN = frozenset({"dw_lpair.md", "dw_rotate_exec.md"})
SOURCE_TARGET = "config:2"
REPLACEMENT_TARGET = "config:1"
AUTHORITY_REF = "manager_mail/85c5dff58359-1492.txt"
AUTHORITY_LINES = (5, 5)
AUTHORITY_SHA256 = "399b09775312d638e3b57b35bdce85b9e8e1b6487528d35e3c0f2f5b80dbf4ec"
AUTHORITY_TEXT = "Also close config:7 and config:2 and config:3. I don’t hear anything from them, so they are not doing their jobs"
CLOSURE_ITEM = (
    "Under Human closure authority manager_mail/85c5dff58359-1492.txt:1-4, send the single "
    "owner-authenticated completion notice requested by the supported closure helper for semantic key "
    "4cc8d9ef32a41174a9ee36aec881b321c0919a797ff612088e39833d77c2c308, report its Message-ID "
    "through private omo_report.sh, then remain idle for supported closure; all unresolved work and child "
    "reporting ownership have transferred to config:1, so do not resume project work."
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
PANE_RE = re.compile(r"^%[0-9]+$")
PACKET_KEYS = {
    "schema",
    "root",
    "source_task",
    "replacement_task",
    "stale_task",
    "source_target",
    "replacement_target",
    "executor_target",
    "close_proof_secret",
    "close_proof_commitment",
    "source_sha256",
    "replacement_sha256",
    "stale_sha256",
    "todo_sha256",
    "historical_commit",
    "historical_source_sha256",
    "historical_queue_sha256",
    "authority",
    "authority_sha256",
    "authority_lines",
    "source_pane",
    "session_id",
    "protected_targets",
    "child_transfers",
    "source_before_base64",
    "source_after_base64",
    "source_after_sha256",
    "stale_before_base64",
    "stale_after_base64",
    "stale_after_sha256",
    "todo_before_base64",
    "todo_after_base64",
    "todo_after_sha256",
    "audit",
    "inputs",
    "binding_id",
}


@dataclass(frozen=True)
class PanePin:
    target: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def decoded(value: object) -> bytes:
    if not isinstance(value, str):
        raise TaskFrontmatterError("closure packet replacement bytes are malformed.")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise TaskFrontmatterError("closure packet replacement bytes are not canonical base64.") from exc


def parse_pin(value: str) -> PanePin:
    fields = value.split("=")
    if len(fields) != 4:
        raise argparse.ArgumentTypeError("pane pin must be TARGET=PANE_ID=PANE_PID=START_TICKS")
    target, pane_id, raw_pid, raw_ticks = fields
    try:
        pid, ticks = int(raw_pid), int(raw_ticks)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pane pid and start ticks must be integers") from exc
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?", target) or not PANE_RE.fullmatch(pane_id) or pid <= 1 or ticks <= 0:
        raise argparse.ArgumentTypeError("pane pin identity is invalid")
    return PanePin(target, pane_id, pid, ticks)


def git_blob(root: Path, commit: str, task: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise TaskFrontmatterError("historical commit must be one full lowercase Git commit.")
    try:
        repository = Path(
            subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        ).resolve(strict=True)
        resolved = subprocess.run(
            ["git", "-C", str(root), "rev-parse", f"{commit}^{{commit}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        data = subprocess.run(
            ["git", "-C", str(root), "show", f"{commit}:{task}"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise TaskFrontmatterError("historical task identity could not be read from Git.") from exc
    if repository != root or resolved != commit:
        raise TaskFrontmatterError("historical task identity is not bound to the exact work-log repository and commit.")
    return data


def authority_text(path: Path, expected_sha256: str, lines: tuple[int, int]) -> str:
    data, state = read_regular(path, expected_sha256)
    if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o600:
        raise TaskFrontmatterError("closure authority must remain owner-private.")
    text = data.decode()
    start, end = lines
    selected = "\n".join(text.splitlines()[start - 1 : end])
    # 🧑 Source `manager_mail/85c5dff58359-1492.txt:5`: "Also close config:7 and config:2 and config:3. I don’t hear anything from them, so they are not doing their jobs"
    if path.name != Path(AUTHORITY_REF).name or expected_sha256 != AUTHORITY_SHA256 or lines != AUTHORITY_LINES or selected != AUTHORITY_TEXT:
        raise TaskFrontmatterError("Human authority does not exactly name config:2 for closure.")
    return selected


def metadata(data: bytes, root: Path, label: str) -> TaskMetadata:
    try:
        result = parse_task_metadata(data.decode(), root)
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError(f"{label} is not UTF-8.") from exc
    if result is None:
        raise TaskFrontmatterError(f"{label} has no task metadata.")
    return result


def task_done(root: Path, data: bytes, note: str) -> bytes:
    text = data.decode()
    updated = update_frontmatter_status(cleared_pending_task_text(text, root), "done", "", root).rstrip()
    return f"{updated}\n\n({note})\n".encode()


def rows_for(root: Path, task: Path, text: str) -> list[tuple[str, str]]:
    section = ""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        if line.strip().endswith(":"):
            section = line.strip()[:-1].casefold()
        elif task in todo_row_task_paths(root, line):
            rows.append((section, line))
    return rows


def historical_children(root: Path, commit: str, manager_target: str) -> list[dict[str, str]]:
    try:
        found = subprocess.run(
            ["git", "-C", str(root), "grep", "-l", f"^managerat: {manager_target}$", commit, "--", "*.md"],
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise TaskFrontmatterError("historical child inventory could not be read from Git.") from exc
    if found.returncode not in {0, 1}:
        raise TaskFrontmatterError("historical child inventory could not be read from Git.")
    names = [line.removeprefix(f"{commit}:") for line in found.stdout.splitlines()]
    result: list[dict[str, str]] = []
    for name in names:
        if not name.endswith(".md") or "manager_mail/" in name:
            continue
        data = git_blob(root, commit, name)
        try:
            item = parse_task_metadata(data.decode(), root)
        except (UnicodeDecodeError, TaskFrontmatterError):
            continue
        if item is not None and item.status != "done" and item.managerat == manager_target:
            result.append({"task": name, "sha256": sha256(data)})
    return sorted(result, key=lambda item: item["task"])


def validate_transfer(
    root: Path,
    source_data: bytes,
    replacement_data: bytes,
    stale_data: bytes,
    todo_data: bytes,
    historical_data: bytes,
    commit: str,
) -> list[dict[str, str]]:
    source = metadata(source_data, root, "source task")
    replacement = metadata(replacement_data, root, "replacement task")
    stale = metadata(stale_data, root, "stale same-target task")
    historical = metadata(historical_data, root, "historical source task")
    if (
        source.status != "blocked"
        or source.blocked_on != REPLACEMENT_TASK
        or source.runat != SOURCE_TARGET
        or source.managerat != REPLACEMENT_TARGET
        or not source.is_manager
        or source.pending_task_items != (CLOSURE_ITEM,)
        or has_pending_marker(source_data.decode())
    ):
        raise TaskFrontmatterError("source is not the exact transferred blocked manager record.")
    if (
        replacement.status not in {"running", "long_running"}
        or replacement.runat != REPLACEMENT_TARGET
        or replacement.status == "done"
        or not replacement.is_manager
        or has_pending_marker(replacement_data.decode())
    ):
        raise TaskFrontmatterError("replacement is not the exact active manager record.")
    if stale.status != "blocked" or stale.runat != SOURCE_TARGET or stale.is_manager or stale.pending_task_items or has_pending_marker(stale_data.decode()):
        raise TaskFrontmatterError("stale task is not the exact empty blocked same-target record.")
    if "manager closed Codex agent" not in stale_data.decode() or f"tmux target `{SOURCE_TARGET}`" not in stale_data.decode():
        raise TaskFrontmatterError("stale task lacks its prior exact target-close evidence.")
    if historical.status == "done" or not historical.is_manager or historical.runat != "config:3" or not historical.pending_task_items:
        raise TaskFrontmatterError("historical source does not preserve the pre-transfer manager queue.")
    destination_queue = list(replacement.pending_task_items)
    prior_queue = list(historical.pending_task_items)
    matches = [index for index in range(len(destination_queue) - len(prior_queue) + 1) if destination_queue[index : index + len(prior_queue)] == prior_queue]
    if len(matches) != 1:
        raise TaskFrontmatterError("replacement does not preserve the complete historical queue exactly once and in order.")
    todo_text = todo_data.decode()
    source_path, stale_path = root / SOURCE_TASK, root / STALE_TASK
    if rows_for(root, source_path, todo_text) != [("human pending", f"{SOURCE_TASK} {SOURCE_TARGET}")]:
        raise TaskFrontmatterError("source TODO custody is not the exact human-pending target row.")
    if rows_for(root, stale_path, todo_text) != [("previous", f"{STALE_TASK} {SOURCE_TARGET}")]:
        raise TaskFrontmatterError("stale TODO custody is not the exact previous target row.")
    if set(authoritative_active_target_task_paths(root, SOURCE_TARGET)) != {source_path, stale_path}:
        raise TaskFrontmatterError("config:2 does not have exactly the two bound active task records.")
    if active_child_task_refs(root, source_path, SOURCE_TARGET):
        raise TaskFrontmatterError("transferred source still has active direct children.")
    if authoritative_active_target_task_paths(root, REPLACEMENT_TARGET) != (root / REPLACEMENT_TASK,):
        raise TaskFrontmatterError("config:1 is not the sole active replacement owner.")
    children = historical_children(root, commit, historical.runat)
    if {child["task"] for child in children} != EXPECTED_HISTORICAL_CHILDREN:
        raise TaskFrontmatterError("historical source child-reporting set is not the exact transferred config:2 set.")
    for child in children:
        current_data, _ = read_regular_unbound(root / child["task"])
        current = metadata(current_data, root, f"transferred child {child['task']}")
        if current.managerat != REPLACEMENT_TARGET:
            raise TaskFrontmatterError("historical child reporting ownership did not transfer to config:1.")
        child["current_sha256"] = sha256(current_data)
    return children


def current_pin(pin: PanePin) -> bool:
    try:
        return target_identity(pin.target) == (pin.pane_id, pin.pane_pid, pin.pane_start_ticks)
    except (OSError, RuntimeError, TaskFrontmatterError):
        return False


def target_identity(target: str) -> tuple[str, int, int]:
    pane = exact_pane_id(target)
    if not pane:
        return "", 0, 0
    raw = bound_guarded_read(
        target,
        pane,
        ["display-message", "-p", "-t", target, "#{pane_id}|#{pane_pid}"],
    ).strip()
    resolved, separator, raw_pid = raw.partition("|")
    if not separator or resolved != pane or not raw_pid.isdigit():
        raise TaskFrontmatterError("live target process identity is malformed.")
    pid = int(raw_pid)
    ticks = process_start_ticks(pid)
    if ticks is None or exact_pane_id(target) != pane:
        raise TaskFrontmatterError("live target process identity changed.")
    final = bound_guarded_read(
        target,
        pane,
        ["display-message", "-p", "-t", target, "#{pane_id}|#{pane_pid}"],
        pid,
    ).strip()
    if final != raw or process_start_ticks(pid) != ticks:
        raise TaskFrontmatterError("live target process identity changed.")
    return pane, pid, ticks


def inspect_target(target: str) -> str:
    return codex_status(target)


def current_session_id(pin: PanePin, protected: tuple[PanePin, ...] = ()) -> str:
    """Resolve one exact live Codex session without trusting prior capture text."""

    def stable_targets() -> bool:
        return current_pin(pin) and all(current_pin(item) for item in protected)

    def pre_input_check() -> None:
        if not stable_targets():
            raise TaskFrontmatterError("a protected target changed before guarded session query.")

    session_id, _response = query_status_session_id(
        pin.target,
        2000,
        10.0,
        identity_is_current=stable_targets,
        tmux_guard=(pin.target, pin.pane_id),
        strict_status_response=True,
        expected_pane_pid=pin.pane_pid,
        pre_input_check=pre_input_check,
    )
    if SESSION_RE.fullmatch(session_id) is None:
        raise TaskFrontmatterError("live config:2 Codex session id could not be resolved.")
    return session_id.lower()


def stop_target(
    target: str,
    pane_id: str,
    pane_pid: int,
    pane_start_ticks: int,
    session_id: str,
    proof_path: Path,
    audit_path: Path,
    proof_secret: str,
    proof_commitment: str,
    proof_audit_sha256: str,
    pre_input_check: Callable[[], None] | None = None,
) -> None:
    observed = guarded_codex_stop(
        CodexStopArgs(
            target=target,
            wait_s=10.0,
            lines=2000,
            dry_run=False,
            allow_self=False,
            no_feedback=True,
            bound_symbolic_target=target,
            bound_pane_id=pane_id,
            bound_pane_pid=pane_pid,
            bound_pane_start_ticks=pane_start_ticks,
            bound_expected_session_id=session_id,
            bound_pre_input_check=pre_input_check,
            bound_close_proof_path=str(proof_path),
            bound_close_audit_path=str(audit_path),
            bound_close_proof_secret=proof_secret,
            bound_close_proof_commitment=proof_commitment,
            bound_close_operation="transferred-manager-close",
            bound_close_audit_sha256=proof_audit_sha256,
        )
    )
    if observed.lower() != session_id.lower():
        raise TaskFrontmatterError("guarded closure did not return the exact bound Codex session.")


def canonical_packet(record: dict[str, object]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def trusted_root() -> Path:
    return TRUSTED_ROOT.resolve(strict=True)


def validate_exact_scope(packet: dict[str, object]) -> Path:
    root = Path(str(packet["root"])).resolve(strict=True)
    fixed = (
        packet["source_task"],
        packet["replacement_task"],
        packet["stale_task"],
        packet["source_target"],
        packet["replacement_target"],
        packet["executor_target"],
    )
    if root != trusted_root() or fixed != (
        SOURCE_TASK,
        REPLACEMENT_TASK,
        STALE_TASK,
        SOURCE_TARGET,
        REPLACEMENT_TARGET,
        "config:4",
    ):
        raise TaskFrontmatterError("closure packet is outside the exact config:2 transfer scope.")
    if Path(str(packet["authority"])).resolve(strict=True) != (root / AUTHORITY_REF).resolve(strict=True):
        raise TaskFrontmatterError("closure packet authority is outside the exact config:2 transfer scope.")
    source_pin = packet_pin(packet["source_pane"])
    protected = [packet_pin(item) for item in packet_list(packet["protected_targets"], "protected targets")]
    if source_pin.target != SOURCE_TARGET or {pin.target for pin in protected} != {REPLACEMENT_TARGET, "config:4"}:
        raise TaskFrontmatterError("closure packet pane set is outside the exact config:2 transfer scope.")
    return root


def prepare(ns: argparse.Namespace) -> None:
    root = ns.root.resolve(strict=True)
    if root != trusted_root():
        raise TaskFrontmatterError("closure helper requires the exact trusted work-log root.")
    if (ns.source_task, ns.replacement_task, ns.stale_task, ns.source_target, ns.replacement_target) != (
        Path(SOURCE_TASK),
        Path(REPLACEMENT_TASK),
        Path(STALE_TASK),
        SOURCE_TARGET,
        REPLACEMENT_TARGET,
    ):
        raise TaskFrontmatterError("closure helper is restricted to the exact config:2 transfer.")
    if ns.authority.resolve(strict=True) != (root / AUTHORITY_REF).resolve(strict=True):
        raise TaskFrontmatterError("closure authority is not the exact Source-1492 file.")
    _ = authority_text(ns.authority.resolve(), ns.authority_sha256, ns.authority_lines)
    source_path, replacement_path, stale_path, todo_path = (
        root / SOURCE_TASK,
        root / REPLACEMENT_TASK,
        root / STALE_TASK,
        root / "TODO.md",
    )
    paths = {source_path, replacement_path, stale_path, todo_path, ns.authority.resolve()}
    if ns.packet.resolve() in paths or ns.audit.resolve() in paths or ns.packet.resolve() == ns.audit.resolve():
        raise TaskFrontmatterError("packet and audit must be distinct private outputs.")
    for output in (ns.packet.resolve(), ns.audit.resolve()):
        state = output.parent.resolve(strict=True).stat()
        if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) & 0o077:
            raise TaskFrontmatterError("packet and audit parents must be owner-private.")
    if (
        ns.executor_target != "config:4"
        or ns.source_pin.target != SOURCE_TARGET
        or len(ns.protected_target) != 2
        or {pin.target for pin in ns.protected_target} != {SOURCE_TARGET.replace(":2", ":1"), ns.executor_target}
    ):
        raise TaskFrontmatterError("closure requires exact config:2 source and config:1/config:4 protected targets.")
    if not SESSION_RE.fullmatch(ns.session_id):
        raise TaskFrontmatterError("source session id is invalid.")
    with root_membership_lock(root), task_target_lock(root, SOURCE_TARGET), task_target_lock(root, REPLACEMENT_TARGET), ExitStack() as locks:
        for path in sorted(paths, key=str):
            locks.enter_context(task_file_lock(path))
        source_data, _ = read_regular(source_path, ns.source_sha256)
        replacement_data, _ = read_regular(replacement_path, ns.replacement_sha256)
        stale_data, _ = read_regular(stale_path, ns.stale_sha256)
        todo_data, _ = read_regular(todo_path, ns.todo_sha256)
        historical_data = git_blob(root, ns.historical_commit, SOURCE_TASK)
        if sha256(historical_data) != ns.historical_source_sha256:
            raise TaskFrontmatterError("historical source task digest changed.")
        children = validate_transfer(root, source_data, replacement_data, stale_data, todo_data, historical_data, ns.historical_commit)
        if exact_pane_id(SOURCE_TARGET) != ns.source_pin.pane_id or not current_pin(ns.source_pin):
            raise TaskFrontmatterError("live config:2 pane identity changed.")
        if inspect_target(SOURCE_TARGET) != "ready":
            raise TaskFrontmatterError("live config:2 manager is not idle and ready for guarded closure.")
        if any(not current_pin(pin) for pin in ns.protected_target):
            raise TaskFrontmatterError("a protected target identity changed.")
        if current_session_id(ns.source_pin, tuple(ns.protected_target)) != ns.session_id.lower():
            raise TaskFrontmatterError("live config:2 Codex session id differs from the prepared binding.")
        source_after = task_done(root, source_data, f"Human-authorized transferred-manager closure from {AUTHORITY_REF}:5; exact bound pane closed")
        stale_after = task_done(root, stale_data, f"stale same-target record retired with {SOURCE_TASK}; pane already bound to the transferred manager")
        todo_after = closed_todo(root, source_path, todo_data.decode(), "shared-live-worker", SOURCE_TARGET)
        todo_after = closed_todo(root, stale_path, todo_after, "absent-manager-previous", SOURCE_TARGET)
        inputs = []
        input_paths = [*paths, *(root / child["task"] for child in children)]
        for path in sorted(set(input_paths), key=str):
            _, identity, ancestors = absolute_file_binding(path.resolve(), f"transferred-manager input {path}")
            inputs.append({"file": asdict(identity), "ancestors": [asdict(item) for item in ancestors]})
        close_proof_secret = secrets.token_hex(32)
        packet: dict[str, object] = {
            "schema": SCHEMA,
            "root": str(root),
            "source_task": SOURCE_TASK,
            "replacement_task": REPLACEMENT_TASK,
            "stale_task": STALE_TASK,
            "source_target": SOURCE_TARGET,
            "replacement_target": REPLACEMENT_TARGET,
            "executor_target": ns.executor_target,
            "close_proof_secret": close_proof_secret,
            "close_proof_commitment": sha256(close_proof_secret.encode()),
            "source_sha256": ns.source_sha256,
            "replacement_sha256": ns.replacement_sha256,
            "stale_sha256": ns.stale_sha256,
            "todo_sha256": ns.todo_sha256,
            "historical_commit": ns.historical_commit,
            "historical_source_sha256": ns.historical_source_sha256,
            "historical_queue_sha256": sha256(json.dumps(list(metadata(historical_data, root, "historical source").pending_task_items), ensure_ascii=False, separators=(",", ":")).encode()),
            "authority": str(ns.authority.resolve()),
            "authority_sha256": ns.authority_sha256,
            "authority_lines": list(ns.authority_lines),
            "source_pane": asdict(ns.source_pin),
            "session_id": ns.session_id,
            "protected_targets": [asdict(pin) for pin in sorted(ns.protected_target, key=lambda pin: pin.target)],
            "child_transfers": children,
            "source_before_base64": encoded(source_data),
            "source_after_base64": encoded(source_after),
            "source_after_sha256": sha256(source_after),
            "stale_before_base64": encoded(stale_data),
            "stale_after_base64": encoded(stale_after),
            "stale_after_sha256": sha256(stale_after),
            "todo_before_base64": encoded(todo_data),
            "todo_after_base64": encoded(todo_after.encode()),
            "todo_after_sha256": sha256(todo_after.encode()),
            "audit": str(ns.audit.resolve()),
            "inputs": inputs,
        }
        packet["binding_id"] = sha256(canonical_packet(packet))
        publish_or_validate(ns.packet.resolve(), canonical_packet(packet), "transferred-manager closure packet")
    print(ns.packet.resolve())


def validate_packet(packet: object, data: bytes) -> dict[str, object]:
    if not isinstance(packet, dict) or set(packet) != PACKET_KEYS or packet.get("schema") != SCHEMA:
        raise TaskFrontmatterError("closure packet is malformed.")
    record = dict(packet)
    binding = record.pop("binding_id", None)
    if binding != sha256(canonical_packet(record)) or data != canonical_packet(packet):
        raise TaskFrontmatterError("closure packet binding identity changed.")
    return packet


def packet_pin(value: object) -> PanePin:
    if not isinstance(value, dict) or set(value) != {"target", "pane_id", "pane_pid", "pane_start_ticks"}:
        raise TaskFrontmatterError("closure packet pane identity is malformed.")
    target, pane_id, pane_pid, ticks = (value[key] for key in ("target", "pane_id", "pane_pid", "pane_start_ticks"))
    if not isinstance(target, str) or not isinstance(pane_id, str) or type(pane_pid) is not int or type(ticks) is not int:
        raise TaskFrontmatterError("closure packet pane identity has invalid types.")
    try:
        return parse_pin(f"{target}={pane_id}={pane_pid}={ticks}")
    except argparse.ArgumentTypeError as exc:
        raise TaskFrontmatterError(str(exc)) from exc


def packet_list(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TaskFrontmatterError(f"closure packet {label} is malformed.")
    return [dict(item) for item in value]


def prepared_audit_data(packet: dict[str, object], packet_sha256: str) -> bytes:
    audit_fields = {
        key: packet[key]
        for key in packet
        if key
        not in {
            "inputs",
            "source_before_base64",
            "source_after_base64",
            "stale_before_base64",
            "stale_after_base64",
            "todo_before_base64",
            "todo_after_base64",
            "close_proof_secret",
        }
    }
    return canonical_packet(
        {
            **audit_fields,
            "packet_sha256": packet_sha256,
            "operation": "transferred-manager-close",
            "state": "prepared",
        }
    )


def abandon_prepared(ns: argparse.Namespace) -> None:
    """Permanently disposition one pre-interrupt prepared transaction."""
    packet_data, _ = read_regular(ns.packet.resolve(strict=True), ns.packet_sha256)
    packet = validate_packet(json.loads(packet_data), packet_data)
    root = validate_exact_scope(packet)
    source_path, replacement_path, stale_path, todo_path = (
        root / SOURCE_TASK,
        root / REPLACEMENT_TASK,
        root / STALE_TASK,
        root / "TODO.md",
    )
    source_pin = packet_pin(packet["source_pane"])
    protected = [packet_pin(item) for item in packet_list(packet["protected_targets"], "protected targets")]
    prepared_path = Path(f"{packet['audit']}.prepared")
    expected_prepared = prepared_audit_data(packet, ns.packet_sha256)
    prepared_data, _ = read_regular(prepared_path.resolve(strict=True), ns.prepared_sha256)
    if prepared_data != expected_prepared:
        raise TaskFrontmatterError("prepared transaction does not match the exact packet.")
    committed_path = Path(str(packet["audit"]))
    proof_path = prepared_path.with_name(f".{prepared_path.name}.owner-stopped")
    started_path = done_live_close_started_path(prepared_path)
    abandoned_path = Path(f"{prepared_path}.abandoned")
    with root_membership_lock(root), task_target_lock(root, SOURCE_TARGET), task_target_lock(root, REPLACEMENT_TARGET), ExitStack() as locks:
        for path in sorted((source_path, replacement_path, stale_path, todo_path), key=str):
            locks.enter_context(task_file_lock(path))
        if any(path_entry_exists(path) for path in (committed_path, proof_path, started_path)):
            raise TaskFrontmatterError("prepared transaction advanced beyond the pre-interrupt state.")
        expected = {
            source_path: str(packet["source_sha256"]),
            replacement_path: str(packet["replacement_sha256"]),
            stale_path: str(packet["stale_sha256"]),
        }
        for path, expected_sha256 in expected.items():
            _ = read_regular(path, expected_sha256)
        todo_data = read_regular_unbound(todo_path)[0]
        todo_text = todo_data.decode()
        if rows_for(root, source_path, todo_text) != [("human pending", f"{SOURCE_TASK} {SOURCE_TARGET}")]:
            raise TaskFrontmatterError("source TODO custody changed before prepared disposition.")
        if rows_for(root, stale_path, todo_text) != [("previous", f"{STALE_TASK} {SOURCE_TARGET}")]:
            raise TaskFrontmatterError("stale TODO custody changed before prepared disposition.")
        if set(authoritative_active_target_task_paths(root, SOURCE_TARGET)) != {source_path, stale_path}:
            raise TaskFrontmatterError("config:2 task ownership changed before prepared disposition.")
        if authoritative_active_target_task_paths(root, REPLACEMENT_TARGET) != (replacement_path,):
            raise TaskFrontmatterError("config:1 replacement ownership changed before prepared disposition.")
        if exact_pane_id(SOURCE_TARGET) != source_pin.pane_id or not current_pin(source_pin):
            raise TaskFrontmatterError("live config:2 pane identity changed before prepared disposition.")
        if inspect_target(SOURCE_TARGET) != "ready" or any(not current_pin(pin) for pin in protected):
            raise TaskFrontmatterError("a live target is not stable for prepared disposition.")
        observed_session_id = current_session_id(source_pin, tuple(protected))
        if observed_session_id == str(packet["session_id"]).lower():
            raise TaskFrontmatterError("prepared transaction session binding is not stale.")
        if (
            exact_pane_id(SOURCE_TARGET) != source_pin.pane_id
            or inspect_target(SOURCE_TARGET) != "ready"
            or not current_pin(source_pin)
            or any(not current_pin(pin) for pin in protected)
        ):
            raise TaskFrontmatterError("a live target changed before prepared disposition.")
        record: dict[str, object] = {
            "schema": "omo-transferred-manager-close-abandonment/v1",
            "operation": "transferred-manager-close",
            "state": "abandoned-before-interrupt",
            "packet": str(ns.packet.resolve()),
            "packet_sha256": ns.packet_sha256,
            "prepared_audit": str(prepared_path.resolve()),
            "prepared_audit_sha256": ns.prepared_sha256,
            "source_pane": asdict(source_pin),
            "packet_session_id": str(packet["session_id"]),
            "observed_session_id": observed_session_id,
            "packet_todo_sha256": str(packet["todo_sha256"]),
            "observed_todo_sha256": sha256(todo_data),
            "committed_audit_absent": True,
            "owner_stopped_proof_absent": True,
            "close_started_marker_absent": True,
        }
        record["binding_id"] = sha256(canonical_packet(record))
        publish_or_validate(abandoned_path, canonical_packet(record), "abandoned transferred-manager transaction")
    print(abandoned_path)


def verify_live_session(ns: argparse.Namespace) -> None:
    """Authenticate the packet's live session binding for independent review."""
    packet_data, _ = read_regular(ns.packet.resolve(strict=True), ns.packet_sha256)
    packet = validate_packet(json.loads(packet_data), packet_data)
    _ = validate_exact_scope(packet)
    source_pin = packet_pin(packet["source_pane"])
    protected = [packet_pin(item) for item in packet_list(packet["protected_targets"], "protected targets")]
    if exact_pane_id(SOURCE_TARGET) != source_pin.pane_id or not current_pin(source_pin):
        raise TaskFrontmatterError("live config:2 pane identity differs from the packet.")
    if inspect_target(SOURCE_TARGET) != "ready" or any(not current_pin(pin) for pin in protected):
        raise TaskFrontmatterError("a live target is not stable for session review.")
    session_id = current_session_id(source_pin, tuple(protected))
    if session_id != str(packet["session_id"]).lower():
        raise TaskFrontmatterError("live config:2 Codex session id differs from the packet.")
    if any(not current_pin(pin) for pin in (source_pin, *protected)):
        raise TaskFrontmatterError("a live target changed during session review.")
    print(session_id)


def execute(ns: argparse.Namespace) -> None:
    packet_data, _ = read_regular(ns.packet.resolve(strict=True), ns.packet_sha256)
    review_data, _ = read_regular(ns.review.resolve(strict=True), ns.review_sha256)
    packet = validate_packet(json.loads(packet_data), packet_data)
    root = validate_exact_scope(packet)
    review = authenticated_report(review_data, "transferred-manager closure review")
    try:
        verdict = json.loads(review_data.split(b"message:\n", 1)[1])
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("authenticated closure review body is invalid.") from exc
    if verdict != {
        "schema": REVIEW_SCHEMA,
        "verdict": "PASS",
        "packet_sha256": ns.packet_sha256,
        "session_id": packet["session_id"],
    }:
        raise TaskFrontmatterError("independent review does not PASS this exact packet.")
    if review["producer_target"] in {SOURCE_TARGET, REPLACEMENT_TARGET, packet["executor_target"]}:
        raise TaskFrontmatterError("closure review is not independent of source, replacement, and executor.")
    source_path, replacement_path, stale_path, todo_path, authority_path = (
        root / SOURCE_TASK,
        root / REPLACEMENT_TASK,
        root / STALE_TASK,
        root / "TODO.md",
        Path(str(packet["authority"])),
    )
    source_pin = packet_pin(packet["source_pane"])
    protected = [packet_pin(item) for item in packet_list(packet["protected_targets"], "protected targets")]
    source_before = decoded(packet["source_before_base64"])
    stale_before = decoded(packet["stale_before_base64"])
    todo_before = decoded(packet["todo_before_base64"])
    expected_source_after = task_done(
        root,
        source_before,
        f"Human-authorized transferred-manager closure from {AUTHORITY_REF}:5; exact bound pane closed",
    )
    expected_stale_after = task_done(
        root,
        stale_before,
        f"stale same-target record retired with {SOURCE_TASK}; pane already bound to the transferred manager",
    )
    expected_todo_after = closed_todo(root, source_path, todo_before.decode(), "shared-live-worker", SOURCE_TARGET)
    expected_todo_after = closed_todo(root, stale_path, expected_todo_after, "absent-manager-previous", SOURCE_TARGET).encode()
    image_bindings = (
        (source_before, "source_sha256", expected_source_after, "source_after_base64", "source_after_sha256"),
        (stale_before, "stale_sha256", expected_stale_after, "stale_after_base64", "stale_after_sha256"),
        (todo_before, "todo_sha256", expected_todo_after, "todo_after_base64", "todo_after_sha256"),
    )
    if any(
        sha256(before_data) != packet[before_sha_key] or decoded(packet[after_payload_key]) != expected_after or sha256(expected_after) != packet[after_sha_key]
        for before_data, before_sha_key, expected_after, after_payload_key, after_sha_key in image_bindings
    ):
        raise TaskFrontmatterError("closure packet before/after images do not match the exact lifecycle transformation.")
    after = {
        source_path: (str(packet["source_sha256"]), str(packet["source_after_sha256"]), decoded(packet["source_after_base64"])),
        stale_path: (str(packet["stale_sha256"]), str(packet["stale_after_sha256"]), decoded(packet["stale_after_base64"])),
        todo_path: (str(packet["todo_sha256"]), str(packet["todo_after_sha256"]), decoded(packet["todo_after_base64"])),
    }
    prepared_data = prepared_audit_data(packet, ns.packet_sha256)
    prepared_record = json.loads(prepared_data)
    audit_fields = {key: value for key, value in prepared_record.items() if key not in {"packet_sha256", "operation", "state"}}
    committed_data = canonical_packet({**audit_fields, "packet_sha256": ns.packet_sha256, "operation": "transferred-manager-close", "state": "committed"})
    prepared_sha256 = sha256(prepared_data)
    prepared_path = Path(f"{packet['audit']}.prepared")
    proof_path = prepared_path.with_name(f".{prepared_path.name}.owner-stopped")
    abandoned_path = Path(f"{prepared_path}.abandoned")
    if path_entry_exists(abandoned_path):
        raise TaskFrontmatterError("prepared transaction was permanently abandoned before interrupt.")
    prepared_exists = existing_exact(prepared_path, prepared_data, "prepared transferred-manager audit") if prepared_path.exists() else False
    committed_path = Path(str(packet["audit"]))
    committed_exists = existing_exact(committed_path, committed_data, "committed transferred-manager audit") if committed_path.exists() else False
    child_transfers = packet_list(packet["child_transfers"], "child transfers")
    child_paths: set[Path] = set()
    for child in child_transfers:
        task = child.get("task")
        if not isinstance(task, str):
            raise TaskFrontmatterError("closure packet child task identity is malformed.")
        child_paths.add(root / task)
    input_paths = {source_path, replacement_path, stale_path, todo_path, authority_path, *child_paths}
    raw_inputs = packet.get("inputs")
    if not isinstance(raw_inputs, list) or len(raw_inputs) != len(input_paths):
        raise TaskFrontmatterError("closure packet input identity set is incomplete.")
    with root_membership_lock(root), task_target_lock(root, SOURCE_TARGET), task_target_lock(root, REPLACEMENT_TARGET), ExitStack() as locks:
        for path in sorted(input_paths, key=str):
            locks.enter_context(task_file_lock(path))
        if path_entry_exists(abandoned_path):
            raise TaskFrontmatterError("prepared transaction was permanently abandoned before interrupt.")
        held_by_path = {}
        for item in raw_inputs:
            identity = file_identity_from(item["file"], "transferred-manager input")
            ancestors = tuple(directory_identity_from(value, "transferred-manager ancestor") for value in item["ancestors"])
            path = Path(identity.path)
            if prepared_exists and path in after:
                current_data, current_identity, current_ancestors = absolute_file_binding(path, "recoverable transferred-manager input")
                if sha256(current_data) in after[path][:2]:
                    identity, ancestors = current_identity, current_ancestors
            held = hold_absolute(identity, ancestors)
            held_by_path[path] = held
            locks.callback(os.close, held.descriptor)
            for descriptor in reversed(held.directories):
                locks.callback(os.close, descriptor)
        if set(held_by_path) != {path.resolve() for path in input_paths}:
            raise TaskFrontmatterError("closure packet input identity set changed.")
        for held in held_by_path.values():
            validate_held_absolute(held)
        current = {path: read_regular_unbound(path)[0] for path in after}
        if any(sha256(current[path]) not in identities[:2] for path, identities in after.items()):
            raise TaskFrontmatterError("task/TODO state is neither initial nor recoverable committed state.")
        if committed_exists and any(sha256(current[path]) != identities[1] for path, identities in after.items()):
            raise TaskFrontmatterError("committed closure audit cannot mutate restored or reopened initial state.")
        raw_lines = packet["authority_lines"]
        if not isinstance(raw_lines, list) or len(raw_lines) != 2 or not all(type(value) is int for value in raw_lines):
            raise TaskFrontmatterError("closure packet authority line range is malformed.")
        lines = (int(raw_lines[0]), int(raw_lines[1]))
        _ = authority_text(authority_path, str(packet["authority_sha256"]), lines)
        historical_data = git_blob(root, str(packet["historical_commit"]), SOURCE_TASK)
        if sha256(historical_data) != packet["historical_source_sha256"]:
            raise TaskFrontmatterError("historical transfer evidence changed.")
        if all(sha256(current[path]) == identities[0] for path, identities in after.items()):
            source_data = current[source_path]
            replacement_data = read_regular_unbound(replacement_path)[0]
            stale_data = current[stale_path]
            children = validate_transfer(root, source_data, replacement_data, stale_data, current[todo_path], historical_data, str(packet["historical_commit"]))
            if children != child_transfers:
                raise TaskFrontmatterError("child reporting transfer evidence changed.")
        elif not prepared_exists:
            raise TaskFrontmatterError("partial closure state lacks its durable prepared audit.")
        replacement_data = read_regular(replacement_path, str(packet["replacement_sha256"]))[0]
        if sha256(replacement_data) != packet["replacement_sha256"]:
            raise TaskFrontmatterError("replacement manager changed.")
        if any(not current_pin(pin) for pin in protected):
            raise TaskFrontmatterError("a protected target identity changed before closure.")
        if committed_exists:
            if exact_pane_id(SOURCE_TARGET) != "" or authoritative_active_target_task_paths(root, SOURCE_TARGET):
                raise TaskFrontmatterError("committed closure audit no longer matches absent config:2 ownership.")
            if authoritative_active_target_task_paths(root, REPLACEMENT_TARGET) != (replacement_path,):
                raise TaskFrontmatterError("committed closure audit no longer matches the sole replacement owner.")
            if path_entry_exists(proof_path):
                if not has_bound_close_proof(
                    proof_path,
                    str(packet["close_proof_commitment"]),
                    prepared_sha256,
                    "transferred-manager-close",
                ):
                    raise TaskFrontmatterError("committed closure retains malformed close-proof evidence.")
                proof_path.unlink(missing_ok=True)
            print(packet["audit"])
            return
        if not prepared_exists:
            if exact_pane_id(SOURCE_TARGET) != source_pin.pane_id or inspect_target(SOURCE_TARGET) != "ready" or not current_pin(source_pin):
                raise TaskFrontmatterError("source pane changed before live session validation.")
            if current_session_id(source_pin, tuple(protected)) != str(packet["session_id"]).lower():
                raise TaskFrontmatterError("live config:2 Codex session id differs before prepared audit.")
            if any(not current_pin(pin) for pin in (source_pin, *protected)):
                raise TaskFrontmatterError("a live target changed after session validation.")
        publish_or_validate(prepared_path, prepared_data, "prepared transferred-manager audit")
        close_proven = has_bound_close_proof(
            proof_path,
            str(packet["close_proof_commitment"]),
            prepared_sha256,
            "transferred-manager-close",
        )
        pane = exact_pane_id(SOURCE_TARGET)
        if prepared_exists and not committed_exists and pane == "" and not close_proven and path_entry_exists(done_live_close_started_path(prepared_path)):
            promote_done_live_close_started(
                proof_path,
                prepared_path,
                str(packet["close_proof_commitment"]),
                prepared_sha256,
                SOURCE_TARGET,
                source_pin.pane_id,
                source_pin.pane_pid,
                source_pin.pane_start_ticks,
                "transferred-manager-close",
            )
            close_proven = has_bound_close_proof(
                proof_path,
                str(packet["close_proof_commitment"]),
                prepared_sha256,
                "transferred-manager-close",
            )
        if pane == source_pin.pane_id:
            if inspect_target(SOURCE_TARGET) != "ready" or not current_pin(source_pin):
                raise TaskFrontmatterError("source pane changed before guarded closure.")
            if any(not current_pin(pin) for pin in protected):
                raise TaskFrontmatterError("a protected target changed immediately before closure.")

            def pre_input_check() -> None:
                for held in held_by_path.values():
                    validate_held_absolute(held)
                if authoritative_active_target_task_paths(root, REPLACEMENT_TARGET) != (replacement_path,):
                    raise TaskFrontmatterError("config:1 is not the sole replacement owner before guarded pane input.")
                if any(not current_pin(pin) for pin in protected):
                    raise TaskFrontmatterError("a protected target changed before guarded pane input.")

            stop_target(
                SOURCE_TARGET,
                source_pin.pane_id,
                source_pin.pane_pid,
                source_pin.pane_start_ticks,
                str(packet["session_id"]),
                proof_path,
                prepared_path,
                str(packet["close_proof_secret"]),
                str(packet["close_proof_commitment"]),
                prepared_sha256,
                pre_input_check,
            )
            close_proven = has_bound_close_proof(
                proof_path,
                str(packet["close_proof_commitment"]),
                prepared_sha256,
                "transferred-manager-close",
            )
            if not close_proven:
                raise TaskFrontmatterError("guarded closure returned without its durable close proof.")
        elif not (prepared_exists and pane == "" and (close_proven or committed_exists)):
            raise TaskFrontmatterError("source pane identity changed before reviewed closure.")
        if exact_pane_id(SOURCE_TARGET) != "":
            raise TaskFrontmatterError("source pane remains after guarded closure.")
        for path in (todo_path, stale_path, source_path):
            before_sha, after_sha, replacement = after[path]
            if sha256(current[path]) == before_sha:
                replace_held(held_by_path[path.resolve()], replacement.decode())
            elif sha256(current[path]) != after_sha:
                raise TaskFrontmatterError("closure input changed during recovery.")
        if authoritative_active_target_task_paths(root, SOURCE_TARGET):
            raise TaskFrontmatterError("config:2 retains active task ownership after closure.")
        if authoritative_active_target_task_paths(root, REPLACEMENT_TARGET) != (replacement_path,):
            raise TaskFrontmatterError("config:1 is no longer the sole replacement owner.")
        if any(not current_pin(pin) for pin in protected):
            raise TaskFrontmatterError("a protected target changed during closure.")
        publish_or_validate(committed_path, committed_data, "committed transferred-manager audit")
        if has_bound_close_proof(
            proof_path,
            str(packet["close_proof_commitment"]),
            prepared_sha256,
            "transferred-manager-close",
        ):
            proof_path.unlink(missing_ok=True)
    print(packet["audit"])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = result.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--root", required=True, type=Path)
    prepare_parser.add_argument("--source-task", required=True, type=Path)
    prepare_parser.add_argument("--replacement-task", required=True, type=Path)
    prepare_parser.add_argument("--stale-task", required=True, type=Path)
    prepare_parser.add_argument("--source-target", required=True)
    prepare_parser.add_argument("--replacement-target", required=True)
    prepare_parser.add_argument("--executor-target", required=True)
    prepare_parser.add_argument("--source-sha256", required=True)
    prepare_parser.add_argument("--replacement-sha256", required=True)
    prepare_parser.add_argument("--stale-sha256", required=True)
    prepare_parser.add_argument("--todo-sha256", required=True)
    prepare_parser.add_argument("--historical-commit", required=True)
    prepare_parser.add_argument("--historical-source-sha256", required=True)
    prepare_parser.add_argument("--authority", required=True, type=Path)
    prepare_parser.add_argument("--authority-sha256", required=True)
    prepare_parser.add_argument("--authority-lines", required=True, type=lambda value: tuple(map(int, value.split(":"))))
    prepare_parser.add_argument("--source-pin", required=True, type=parse_pin)
    prepare_parser.add_argument("--session-id", required=True)
    prepare_parser.add_argument("--protected-target", required=True, action="append", type=parse_pin)
    prepare_parser.add_argument("--packet", required=True, type=Path)
    prepare_parser.add_argument("--audit", required=True, type=Path)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--packet", required=True, type=Path)
    execute_parser.add_argument("--packet-sha256", required=True)
    execute_parser.add_argument("--review", required=True, type=Path)
    execute_parser.add_argument("--review-sha256", required=True)
    abandon_parser = sub.add_parser("abandon-prepared")
    abandon_parser.add_argument("--packet", required=True, type=Path)
    abandon_parser.add_argument("--packet-sha256", required=True)
    abandon_parser.add_argument("--prepared-sha256", required=True)
    verify_parser = sub.add_parser("verify-live-session")
    verify_parser.add_argument("--packet", required=True, type=Path)
    verify_parser.add_argument("--packet-sha256", required=True)
    return result


def main() -> int:
    try:
        ns = parser().parse_args()
        for key, value in vars(ns).items():
            if key.endswith("sha256") and not SHA256_RE.fullmatch(str(value)):
                raise TaskFrontmatterError(f"--{key.replace('_', '-')} must be one lowercase SHA-256.")
        {
            "prepare": prepare,
            "execute": execute,
            "abandon-prepared": abandon_prepared,
            "verify-live-session": verify_live_session,
        }[ns.command](ns)
    except (TaskFrontmatterError, CustodyError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"omo_transferred_manager_close.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
