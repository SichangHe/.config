#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Prepare and execute the satisfied config:18 child closure."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_status import exact_pane_id, exact_tail
from omo_manager.omo_codex_stop import guarded_tmux_command
from omo_manager.omo_exported_agent_close import (
    IndeterminateClose,
    read_regular,
    read_regular_unbound,
    replace_held,
    require_private_output,
    restore_exact_after,
)
from omo_manager.omo_human_worker_close import (
    PanePin,
    inspect_target,
    session_from_process,
    target_identity,
    validate_authority,
)
from omo_manager.omo_repository_custody import (
    absolute_file_binding,
    authenticated_report,
    canonical_target,
    directory_identity_from,
    existing_exact,
    file_identity_from,
    hold_absolute,
    publish_or_validate,
    validate_held_absolute,
)
from omo_manager.omo_task_edit import append_comment, pending_remove_evidence_comment, remove_pending_items
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import (
    authoritative_active_target_task_paths,
    has_pending_marker,
    reconcile_done_todo_text,
    relative_task_ref,
    root_membership_lock,
    todo_row_task_paths,
    update_frontmatter_status,
)
from omo_manager.omo_tmux_send import capture_complete_input_lines, exact_codex_runtime_binding

SCHEMA = "omo-config18-satisfied-close/v1"
REVIEW_SCHEMA = "omo-config18-satisfied-close-review/v1"
TARGET = "config:18"
TASK = "config16_close.md"
CURRENT_MANAGER = "wl:21"
MANAGER_TASK = "transport_closure_mgr.md"
AUTHORITY = "manager_mail/85c5dff58359-1570.txt"
AUTHORITY_SHA256 = "2b1e6ed1b653cf79f459a8ff33113df6c262b665368ffb010fbdbb7888625e0f"
UPSTREAM_AUDIT = Path("/tmp/config16-continuity-close.fZOJoJ/audit.json")
UPSTREAM_AUDIT_SHA256 = "e0aaa1395af521f92fb6fd2b7b8207b622edf3751aa272710a21bd9120893e27"
UPSTREAM_SCHEMA = "omo-human-worker-continuity-close/v1"
REPLAY_ID = "205eec6dff48bc1feedd27d3521f86580d86d9943dad469056c63a33dec9d9dc"
PENDING_ITEM = (
    "Repair only the consumed-report export/closure gap for completed queue-empty dw2_input_clear.md/config:16: "
    "bind committed delivered replay 205eec6dff48bc1feedd27d3521f86580d86d9943dad469056c63a33dec9d9dc, "
    "determine exact custody classification, add focused tests and independent review if code changes are needed, "
    "and release a source-safe supported no-mail closure invocation without touching config:16, dw2:0, task/TODO, "
    "composer, mailbox, repository, artifacts, or domain state; send no Human mail and fabricate no attestation."
)
PROTECTED_TARGETS = ("config:19", "config:20", "dw2:0")
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACKET_KEYS = {
    "schema", "root", "task", "todo", "task_before_sha256", "todo_before_sha256",
    "task_after_sha256", "todo_after_sha256", "task_after_base64", "todo_after_base64",
    "pending_item", "authority", "authority_sha256", "authority_lines", "upstream_audit",
    "upstream_audit_sha256", "replay_id", "manager_task", "manager_task_sha256", "manager_target",
    "pane", "terminal_tail_sha256", "composer_capture_sha256", "composer_runtime",
    "protected_targets", "protected_panes", "audit", "destination_target", "inputs", "binding_id",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(record: dict[str, object]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def validate_upstream(data: bytes) -> None:
    try:
        audit = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("config:16 committed audit is malformed.") from exc
    required = {
        "schema": UPSTREAM_SCHEMA,
        "state": "committed",
        "audit": str(UPSTREAM_AUDIT),
        "task": "dw2_input_clear.md",
        "replay_id": REPLAY_ID,
        "authority_sha256": AUTHORITY_SHA256,
        "manager_target": CURRENT_MANAGER,
    }
    if not isinstance(audit, dict) or any(audit.get(key) != value for key, value in required.items()):
        raise TaskFrontmatterError("config:16 audit does not prove the exact committed upstream closure.")


def pane_snapshot(target: str) -> dict[str, object]:
    pane_id, pane_pid, pane_start_ticks = target_identity(target)
    if not pane_id:
        raise TaskFrontmatterError(f"protected target {target} is absent.")
    state = inspect_target(target)
    captured, lines = exact_tail(target, 80)
    if not captured or target_identity(target) != (pane_id, pane_pid, pane_start_ticks):
        raise TaskFrontmatterError(f"protected target {target} changed during capture.")
    return {
        "target": target,
        "state": state,
        "pane_id": pane_id,
        "pane_pid": pane_pid,
        "pane_start_ticks": pane_start_ticks,
        "tail_sha256": sha256("\n".join(lines).encode()),
    }


def protected_snapshots() -> list[dict[str, object]]:
    return [pane_snapshot(target) for target in PROTECTED_TARGETS]


def composer_snapshot(pin: PanePin) -> dict[str, object]:
    if target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        raise TaskFrontmatterError("config:18 changed before composer capture.")
    lines = capture_complete_input_lines(pin.pane_id, full_history=True)
    runtime = exact_codex_runtime_binding(pin.target, allow_shell=True)
    if (runtime.pane_id, runtime.pane_pid) != (pin.pane_id, pin.pane_pid):
        raise TaskFrontmatterError("config:18 runtime changed during composer capture.")
    if target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        raise TaskFrontmatterError("config:18 changed during composer capture.")
    return {"capture_sha256": sha256("\n".join(lines).encode()), "runtime": asdict(runtime)}


def validate_live(pin: PanePin, expected_tail_sha256: str = "", expected_composer: Mapping[str, object] | None = None) -> dict[str, object]:
    if (
        inspect_target(pin.target) != "stuck_input"
        or target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks)
        or session_from_process(pin.pane_pid) != pin.session_id
    ):
        raise TaskFrontmatterError("config:18 is not the exact bound stuck-input Codex session.")
    captured, lines = exact_tail(pin.target, 80)
    tail_sha256 = sha256("\n".join(lines).encode()) if captured else ""
    if not tail_sha256 or (expected_tail_sha256 and tail_sha256 != expected_tail_sha256):
        raise TaskFrontmatterError("config:18 terminal tail changed.")
    composer = composer_snapshot(pin)
    if expected_composer is not None and composer != expected_composer:
        raise TaskFrontmatterError("config:18 full composer or runtime changed.")
    return {"tail_sha256": tail_sha256, **composer}


def close_bound_target(pin: PanePin) -> None:
    if (
        target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks)
        or session_from_process(pin.pane_pid) != pin.session_id
    ):
        raise TaskFrontmatterError("config:18 identity changed before its guarded pane close.")
    guarded_tmux_command(pin.target, pin.pane_id, ["kill-pane", "-t", pin.pane_id], pin.pane_pid)


def require_open_todo(root: Path, task: Path, text: str) -> None:
    section = ""
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
        if task in todo_row_task_paths(root, line):
            rows.append((section, stripped))
    canonical = f"{relative_task_ref(root, task)} {TARGET}"
    if len(rows) != 1 or rows[0][0] not in {"current", "human pending"} or rows[0][1] != canonical:
        raise TaskFrontmatterError("config:18 task must have one exact active TODO row.")


def task_after(root: Path, text: str) -> bytes:
    cleared, n_removed = remove_pending_items(text, (PENDING_ITEM,))
    if n_removed != 1:
        raise TaskFrontmatterError("config:18 closure did not remove exactly its satisfied pending item.")
    updated = update_frontmatter_status(cleared, "done", "", root)
    evidence = f"config:16 committed audit {UPSTREAM_AUDIT}; SHA-256 {UPSTREAM_AUDIT_SHA256}"
    result = append_comment(updated, pending_remove_evidence_comment(1, evidence))
    metadata = parse_task_metadata(result, root)
    if metadata is None or metadata.status != "done" or metadata.pending_task_items:
        raise TaskFrontmatterError("config:18 after-image is not terminal and queue-empty.")
    return result.encode()


def todo_after(root: Path, task: Path, text: str) -> bytes:
    require_open_todo(root, task, text)
    return reconcile_done_todo_text(root, task, text, TARGET).encode()


def decode_after(packet: dict[str, object], field: str, digest_field: str) -> bytes:
    try:
        data = base64.b64decode(str(packet[field]), validate=True)
    except ValueError as exc:
        raise TaskFrontmatterError("closure packet after-image is invalid.") from exc
    if sha256(data) != packet[digest_field]:
        raise TaskFrontmatterError("closure packet after-image digest is invalid.")
    return data


def lifecycle_state(packet: dict[str, object], task_data: bytes, todo_data: bytes) -> tuple[str, str]:
    current = (sha256(task_data), sha256(todo_data))
    before = (str(packet["task_before_sha256"]), str(packet["todo_before_sha256"]))
    after = (str(packet["task_after_sha256"]), str(packet["todo_after_sha256"]))
    if current not in {before, (before[0], after[1]), after}:
        raise TaskFrontmatterError("task/TODO state is neither initial, recoverable partial, nor committed.")
    return current


def parse_pin(ns: argparse.Namespace) -> PanePin:
    pin = PanePin(ns.target, ns.pane_id, ns.pane_pid, ns.pane_start_ticks, ns.session_id.lower())
    if (
        pin.target != TARGET
        or re.fullmatch(r"%[0-9]+", pin.pane_id) is None
        or pin.pane_pid <= 1
        or pin.pane_start_ticks <= 0
        or SESSION_RE.fullmatch(pin.session_id) is None
    ):
        raise TaskFrontmatterError("config:18 live pane binding is malformed.")
    return pin


def validate_task_before(root: Path, task: Path, task_data: bytes, todo_data: bytes | None) -> None:
    metadata = parse_task_metadata(task_data.decode(), root)
    if (
        metadata is None
        or metadata.status != "blocked"
        or metadata.blocked_on != "human"
        or metadata.runat != TARGET
        or metadata.managerat != CURRENT_MANAGER
        or metadata.is_manager
        or metadata.pending_task_items != (PENDING_ITEM,)
        or has_pending_marker(task_data.decode())
        or authoritative_active_target_task_paths(root, TARGET) != (task,)
    ):
        raise TaskFrontmatterError("config:18 task is not the exact satisfied-child closure shape.")
    if todo_data is not None:
        require_open_todo(root, task, todo_data.decode())


# 🧑 "You're saying they're completed, then just send a bunch of control C and kill the Tmux window, no?"
def prepare(ns: argparse.Namespace) -> None:
    root = ns.root.resolve(strict=True)
    task = (root / ns.task).resolve(strict=True)
    todo = root / "TODO.md"
    authority = ns.authority.resolve(strict=True)
    manager_task = (root / ns.manager_task).resolve(strict=True)
    upstream = ns.upstream_audit.resolve(strict=True)
    pin = parse_pin(ns)
    if (
        relative_task_ref(root, task) != TASK
        or authority != root / AUTHORITY
        or ns.authority_sha256 != AUTHORITY_SHA256
        or upstream != UPSTREAM_AUDIT
        or ns.upstream_audit_sha256 != UPSTREAM_AUDIT_SHA256
        or relative_task_ref(root, manager_task) != MANAGER_TASK
        or ns.manager_target != CURRENT_MANAGER
        or ns.destination_target != CURRENT_MANAGER
        or tuple(sorted(ns.protected_target)) != PROTECTED_TARGETS
    ):
        raise TaskFrontmatterError("config:18 closure scope is not exact.")
    input_paths = {task, todo, authority, upstream, manager_task}
    ns.packet = require_private_output(ns.packet, input_paths)
    ns.audit = require_private_output(ns.audit, input_paths | {ns.packet})
    with root_membership_lock(root), task_target_lock(root, TARGET), ExitStack() as locks:
        for path in sorted(input_paths, key=str):
            locks.enter_context(task_file_lock(path))
        task_data, _ = read_regular(task, ns.task_sha256)
        todo_data, _ = read_regular(todo, ns.todo_sha256)
        authority_data, _ = read_regular(authority, ns.authority_sha256)
        upstream_data, _ = read_regular(upstream, ns.upstream_audit_sha256)
        manager_data, _ = read_regular(manager_task, ns.manager_task_sha256)
        validate_authority(authority_data, ns.authority_lines)
        validate_upstream(upstream_data)
        validate_task_before(root, task, task_data, todo_data)
        manager = parse_task_metadata(manager_data.decode(), root)
        if manager is None or not manager.is_manager or manager.runat != CURRENT_MANAGER or manager.status == "done":
            raise TaskFrontmatterError("wl:21 manager custody changed.")
        after_task = task_after(root, task_data.decode())
        after_todo = todo_after(root, task, todo_data.decode())
        live = validate_live(pin)
        protected = protected_snapshots()
        validate_live(
            pin,
            str(live["tail_sha256"]),
            {"capture_sha256": live["capture_sha256"], "runtime": live["runtime"]},
        )
        inputs = []
        for path in sorted(input_paths, key=str):
            _, identity, ancestors = absolute_file_binding(path, f"config:18 satisfied-close input {path}")
            inputs.append({"file": asdict(identity), "ancestors": [asdict(item) for item in ancestors]})
        packet: dict[str, object] = {
            "schema": SCHEMA,
            "root": str(root),
            "task": TASK,
            "todo": str(todo),
            "task_before_sha256": ns.task_sha256,
            "todo_before_sha256": ns.todo_sha256,
            "task_after_sha256": sha256(after_task),
            "todo_after_sha256": sha256(after_todo),
            "task_after_base64": base64.b64encode(after_task).decode(),
            "todo_after_base64": base64.b64encode(after_todo).decode(),
            "pending_item": PENDING_ITEM,
            "authority": str(authority),
            "authority_sha256": ns.authority_sha256,
            "authority_lines": list(ns.authority_lines),
            "upstream_audit": str(upstream),
            "upstream_audit_sha256": ns.upstream_audit_sha256,
            "replay_id": REPLAY_ID,
            "manager_task": str(manager_task),
            "manager_task_sha256": ns.manager_task_sha256,
            "manager_target": CURRENT_MANAGER,
            "pane": asdict(pin),
            "terminal_tail_sha256": live["tail_sha256"],
            "composer_capture_sha256": live["capture_sha256"],
            "composer_runtime": live["runtime"],
            "protected_targets": list(PROTECTED_TARGETS),
            "protected_panes": protected,
            "audit": str(ns.audit),
            "destination_target": ns.destination_target,
            "inputs": inputs,
        }
        packet["binding_id"] = sha256(canonical(packet))
        publish_or_validate(ns.packet, canonical(packet), "config:18 satisfied-close packet")
    print(ns.packet)


def validate_executor() -> None:
    caller = os.environ.get("TMUX_PANE", "")
    if re.fullmatch(r"%[0-9]+", caller) is None or exact_pane_id(CURRENT_MANAGER) != caller:
        raise TaskFrontmatterError("config:18 closure execution is restricted to the current wl:21 pane.")


def execute(ns: argparse.Namespace) -> None:
    validate_executor()
    packet_data, _ = read_regular(ns.packet.resolve(strict=True), ns.packet_sha256)
    review_data, _ = read_regular(ns.review.resolve(strict=True), ns.review_sha256)
    packet = json.loads(packet_data)
    if not isinstance(packet, dict) or set(packet) != PACKET_KEYS or packet.get("schema") != SCHEMA:
        raise TaskFrontmatterError("config:18 closure packet is malformed.")
    unsigned = dict(packet)
    binding_id = unsigned.pop("binding_id")
    if binding_id != sha256(canonical(unsigned)):
        raise TaskFrontmatterError("config:18 closure packet binding is invalid.")
    review_report = authenticated_report(review_data, "config:18 independent review")
    try:
        review = json.loads(review_data.split(b"message:\n", 1)[1])
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("config:18 independent review body is malformed.") from exc
    if review != {"schema": REVIEW_SCHEMA, "verdict": "PASS", "packet_sha256": ns.packet_sha256} or canonical_target(
        str(review_report["producer_target"])
    ) in {canonical_target(TARGET), canonical_target(CURRENT_MANAGER)}:
        raise TaskFrontmatterError("independent review does not PASS this exact config:18 packet.")
    root = Path(str(packet["root"])).resolve(strict=True)
    task = root / str(packet["task"])
    todo = Path(str(packet["todo"]))
    raw_pin = packet["pane"]
    raw_inputs = packet["inputs"]
    if not isinstance(raw_pin, dict) or set(raw_pin) != {"target", "pane_id", "pane_pid", "pane_start_ticks", "session_id"}:
        raise TaskFrontmatterError("config:18 packet pane binding is malformed.")
    pin = PanePin(**raw_pin)
    if not isinstance(raw_inputs, list) or any(
        not isinstance(item, dict) or set(item) != {"file", "ancestors"} or not isinstance(item["ancestors"], list)
        for item in raw_inputs
    ):
        raise TaskFrontmatterError("config:18 packet input bindings are malformed.")
    lock_paths = {Path(file_identity_from(item["file"], "closure input").path) for item in raw_inputs}
    expected_paths = {
        task,
        todo,
        Path(str(packet["authority"])),
        Path(str(packet["upstream_audit"])),
        Path(str(packet["manager_task"])),
    }
    if (
        str(root) != packet["root"]
        or task != root / TASK
        or todo != root / "TODO.md"
        or packet["pending_item"] != PENDING_ITEM
        or Path(str(packet["authority"])) != root / AUTHORITY
        or packet["authority_sha256"] != AUTHORITY_SHA256
        or packet["authority_lines"] != [3, 4]
        or Path(str(packet["upstream_audit"])) != UPSTREAM_AUDIT
        or packet["upstream_audit_sha256"] != UPSTREAM_AUDIT_SHA256
        or packet["replay_id"] != REPLAY_ID
        or Path(str(packet["manager_task"])) != root / MANAGER_TASK
        or packet["manager_target"] != CURRENT_MANAGER
        or packet["destination_target"] != CURRENT_MANAGER
        or pin.target != TARGET
        or lock_paths != expected_paths
        or packet["protected_targets"] != list(PROTECTED_TARGETS)
    ):
        raise TaskFrontmatterError("config:18 packet input or protection set is incomplete.")
    audit = {key: packet[key] for key in PACKET_KEYS - {"task_after_base64", "todo_after_base64", "inputs"}}
    prepared = canonical({**audit, "state": "prepared"})
    committed = canonical({**audit, "state": "committed"})
    prepared_path = Path(f"{packet['audit']}.prepared")
    prepared_exists = existing_exact(prepared_path, prepared, "config:18 prepared close audit") if prepared_path.exists() else False
    with root_membership_lock(root), task_target_lock(root, TARGET), ExitStack() as locks:
        for path in sorted(lock_paths, key=str):
            locks.enter_context(task_file_lock(path))
        held = {}
        for item in raw_inputs:
            identity = file_identity_from(item["file"], "closure input")
            ancestors = tuple(directory_identity_from(value, "closure input ancestor") for value in item["ancestors"])
            if prepared_exists and Path(identity.path) in {task, todo}:
                current_data, current_identity, current_ancestors = absolute_file_binding(
                    Path(identity.path), "recoverable config:18 closure input"
                )
                after_sha256 = packet["task_after_sha256"] if Path(identity.path) == task else packet["todo_after_sha256"]
                if sha256(current_data) in {identity.sha256, after_sha256}:
                    identity, ancestors = current_identity, current_ancestors
            handle = hold_absolute(identity, ancestors)
            held[Path(identity.path)] = handle
            locks.callback(os.close, handle.descriptor)
            for descriptor in reversed(handle.directories):
                locks.callback(os.close, descriptor)
            validate_held_absolute(handle)
        task_data, _ = read_regular_unbound(task)
        todo_data, _ = read_regular_unbound(todo)
        task_sha256, todo_sha256 = lifecycle_state(packet, task_data, todo_data)
        authority_data, _ = read_regular(Path(str(packet["authority"])), str(packet["authority_sha256"]))
        upstream_data, _ = read_regular(Path(str(packet["upstream_audit"])), str(packet["upstream_audit_sha256"]))
        manager_data, _ = read_regular(Path(str(packet["manager_task"])), str(packet["manager_task_sha256"]))
        validate_authority(authority_data, tuple(packet["authority_lines"]))
        validate_upstream(upstream_data)
        manager = parse_task_metadata(manager_data.decode(), root)
        before_state = task_sha256 == packet["task_before_sha256"]
        metadata = parse_task_metadata(task_data.decode(), root)
        if manager is None or not manager.is_manager or manager.runat != CURRENT_MANAGER or manager.status == "done":
            raise TaskFrontmatterError("wl:21 manager custody changed before closure.")
        if before_state:
            validate_task_before(root, task, task_data, todo_data if todo_sha256 == packet["todo_before_sha256"] else None)
        elif metadata is None or metadata.status != "done" or metadata.pending_task_items:
            raise TaskFrontmatterError("recovered config:18 task is not terminal.")
        after_task = decode_after(packet, "task_after_base64", "task_after_sha256")
        after_todo = decode_after(packet, "todo_after_base64", "todo_after_sha256")
        if before_state and task_after(root, task_data.decode()) != after_task:
            raise TaskFrontmatterError("config:18 task after-image changed.")
        if todo_sha256 == packet["todo_before_sha256"] and todo_after(root, task, todo_data.decode()) != after_todo:
            raise TaskFrontmatterError("config:18 TODO after-image changed.")
        if protected_snapshots() != packet["protected_panes"]:
            raise TaskFrontmatterError("a protected target changed before config:18 closure.")
        live_identity = target_identity(TARGET)
        expected_composer = {"capture_sha256": packet["composer_capture_sha256"], "runtime": packet["composer_runtime"]}
        if live_identity == (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
            validate_live(pin, str(packet["terminal_tail_sha256"]), expected_composer)
        elif not (prepared_exists and live_identity == ("", 0, 0)):
            raise TaskFrontmatterError("config:18 pane identity drifted before closure.")
        publish_or_validate(prepared_path, prepared, "config:18 prepared close audit")
        if Path(str(packet["audit"])).exists():
            read_regular(Path(str(packet["audit"])), sha256(committed))
            return
        if live_identity != ("", 0, 0):
            if protected_snapshots() != packet["protected_panes"]:
                raise IndeterminateClose("a protected target changed before the config:18 pane close.")
            validate_live(pin, str(packet["terminal_tail_sha256"]), expected_composer)
            close_bound_target(pin)
        if target_identity(TARGET) != ("", 0, 0):
            raise IndeterminateClose("config:18 remains after its exact guarded close.")
        if protected_snapshots() != packet["protected_panes"]:
            raise IndeterminateClose("a protected target changed across the config:18 close.")
        if todo_sha256 == packet["todo_before_sha256"]:
            replace_held(held[todo], after_todo.decode())
        try:
            if task_sha256 == packet["task_before_sha256"]:
                replace_held(held[task], after_task.decode())
        except Exception as exc:
            if todo_sha256 == packet["todo_before_sha256"] and not isinstance(exc, IndeterminateClose):
                restore_exact_after(todo, str(packet["todo_after_sha256"]), todo_data.decode())
            raise
        if authoritative_active_target_task_paths(root, TARGET) or target_identity(TARGET) != ("", 0, 0) or protected_snapshots() != packet["protected_panes"]:
            restore_exact_after(task, str(packet["task_after_sha256"]), task_data.decode())
            restore_exact_after(todo, str(packet["todo_after_sha256"]), todo_data.decode())
            raise TaskFrontmatterError("post-close config:18 verification failed; task/TODO bytes restored.")
        publish_or_validate(Path(str(packet["audit"])), committed, "config:18 committed close audit")
    print(packet["audit"])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--root", type=Path, required=True)
    prepare_parser.add_argument("--task", type=Path, required=True)
    prepare_parser.add_argument("--target", required=True)
    prepare_parser.add_argument("--task-sha256", required=True)
    prepare_parser.add_argument("--todo-sha256", required=True)
    prepare_parser.add_argument("--authority", type=Path, required=True)
    prepare_parser.add_argument("--authority-sha256", required=True)
    prepare_parser.add_argument("--authority-lines", type=lambda value: tuple(map(int, value.split(":"))), required=True)
    prepare_parser.add_argument("--upstream-audit", type=Path, required=True)
    prepare_parser.add_argument("--upstream-audit-sha256", required=True)
    prepare_parser.add_argument("--manager-task", type=Path, required=True)
    prepare_parser.add_argument("--manager-task-sha256", required=True)
    prepare_parser.add_argument("--manager-target", required=True)
    prepare_parser.add_argument("--pane-id", required=True)
    prepare_parser.add_argument("--pane-pid", type=int, required=True)
    prepare_parser.add_argument("--pane-start-ticks", type=int, required=True)
    prepare_parser.add_argument("--session-id", required=True)
    prepare_parser.add_argument("--protected-target", action="append", default=[])
    prepare_parser.add_argument("--destination-target", required=True)
    prepare_parser.add_argument("--audit", type=Path, required=True)
    prepare_parser.add_argument("--packet", type=Path, required=True)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--packet", type=Path, required=True)
    execute_parser.add_argument("--packet-sha256", required=True)
    execute_parser.add_argument("--review", type=Path, required=True)
    execute_parser.add_argument("--review-sha256", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    ns = parser().parse_args(argv)
    try:
        for key, value in vars(ns).items():
            if key.endswith("sha256") and value and SHA256_RE.fullmatch(value) is None:
                raise TaskFrontmatterError(f"--{key.replace('_', '-')} must be lowercase SHA-256.")
        {"prepare": prepare, "execute": execute}[ns.command](ns)
    except (OSError, ValueError, json.JSONDecodeError, TaskFrontmatterError, RuntimeError) as exc:
        print(f"omo_config18_satisfied_close.py: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
