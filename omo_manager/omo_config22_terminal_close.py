#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Prepare and execute the terminal config:22 preflight closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_codex_stop import guarded_tmux_command
from omo_manager.omo_exported_agent_close import (
    IndeterminateClose,
    read_regular,
    require_private_output,
)
from omo_manager.omo_human_worker_close import (
    PanePin,
    inspect_target,
    session_from_process,
    target_identity,
    validate_authority,
)
from omo_manager.omo_repository_custody import (
    HeldAbsolute,
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
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import (
    authoritative_active_target_task_paths,
    has_pending_marker,
    relative_task_ref,
    root_membership_lock,
    todo_row_task_paths,
)

SCHEMA = "omo-config22-terminal-close/v1"
REVIEW_SCHEMA = "omo-config22-terminal-close-review/v1"
TARGET = "config:22"
TASK = "config16_preflight.md"
TASK_MANAGER = "wl:12"
TASK_MANAGER_FILE = "ops_submanager_sep7.md"
EXECUTOR = "wl:21"
EXECUTOR_TASK = "transport_closure_mgr.md"
AUTHORITY = "manager_mail/85c5dff58359-1570.txt"
AUTHORITY_SHA256 = "2b1e6ed1b653cf79f459a8ff33113df6c262b665368ffb010fbdbb7888625e0f"
AUDIT18 = Path("/tmp/config18-satisfied-close.q9Zzgv/audit.json")
AUDIT18_SHA256 = "0894057cd73b451d277d2731aafb9300b41e79a646394d230bcc61dcdd96106d"
AUDIT19 = Path("/tmp/config19-satisfied-close.8aWkkp/audit.json")
AUDIT19_SHA256 = "90609342bdfa539385af80a62eed46e07dafb1f9265149f723d030e2d69b92f0"
REPLAY_ID = "205eec6dff48bc1feedd27d3521f86580d86d9943dad469056c63a33dec9d9dc"
PROTECTED_TARGETS = ("config:20", "dw2:0")
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACKET_KEYS = {
    "schema", "root", "task", "todo", "task_sha256", "todo_sha256", "authority",
    "authority_sha256", "authority_lines", "audit18", "audit18_sha256", "audit19",
    "audit19_sha256", "replay_id", "task_manager_file", "task_manager_sha256",
    "task_manager", "executor_task", "executor_task_sha256", "executor", "pane",
    "protected_targets", "protected_panes", "helper", "helper_sha256", "audit",
    "destination_target", "inputs", "binding_id",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(record: dict[str, object]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def validate_review(data: bytes, packet_sha256: str) -> None:
    report = authenticated_report(data, "config:22 independent review")
    try:
        review = json.loads(data.split(b"message:\n", 1)[1])
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("config:22 independent review body is malformed.") from exc
    if (
        review != {"schema": REVIEW_SCHEMA, "verdict": "PASS", "packet_sha256": packet_sha256}
        or canonical_target(str(report["producer_target"])) in {canonical_target(TARGET), canonical_target(EXECUTOR)}
    ):
        raise TaskFrontmatterError("independent review does not PASS this exact config:22 packet.")


def validate_audits(audit18_data: bytes, audit19_data: bytes) -> None:
    try:
        audit18 = json.loads(audit18_data)
        audit19 = json.loads(audit19_data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("terminal upstream audit is malformed.") from exc
    expected18 = {
        "schema": "omo-config18-satisfied-close/v1",
        "state": "committed",
        "audit": str(AUDIT18),
        "task": "config16_close.md",
        "task_after_sha256": "0e70ff352e1d0ec200a71f97c677dffdbef905715783aba0ace8f7c1a1ac1652",
        "authority_sha256": AUTHORITY_SHA256,
        "replay_id": REPLAY_ID,
        "manager_target": EXECUTOR,
    }
    expected19 = {
        "schema": "omo-config19-satisfied-close/v1",
        "state": "committed",
        "audit": str(AUDIT19),
        "task": "dw2_wrap_cancel.md",
        "task_after_sha256": "4f82c1eb376a3d257b9409717e03974f48dea2f1bb5e9eaf20ccc8a2e37487c2",
        "authority_sha256": AUTHORITY_SHA256,
        "replay_id": REPLAY_ID,
        "manager_target": EXECUTOR,
        "upstream_audit": str(AUDIT18),
        "upstream_audit_sha256": AUDIT18_SHA256,
    }
    if (
        not isinstance(audit18, dict)
        or not isinstance(audit19, dict)
        or any(audit18.get(key) != value for key, value in expected18.items())
        or any(audit19.get(key) != value for key, value in expected19.items())
    ):
        raise TaskFrontmatterError("terminal upstream audits do not prove both exact committed closures.")


def validate_task(root: Path, task: Path, task_data: bytes, todo_data: bytes) -> None:
    metadata = parse_task_metadata(task_data.decode(), root)
    if (
        metadata is None
        or metadata.status != "done"
        or metadata.runat != TARGET
        or metadata.managerat != TASK_MANAGER
        or metadata.is_manager
        or metadata.pending_task_items
        or has_pending_marker(task_data.decode())
        or authoritative_active_target_task_paths(root, TARGET)
    ):
        raise TaskFrontmatterError("config:22 task is not the exact terminal queue-empty record.")
    section = ""
    rows: list[tuple[str, str]] = []
    for line in todo_data.decode().splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
        if task in todo_row_task_paths(root, line):
            rows.append((section, stripped))
    expected = f"{relative_task_ref(root, task)} {TARGET}"
    if rows != [("previous", expected)]:
        raise TaskFrontmatterError("config:22 task lacks one exact previous TODO row.")


def validate_manager(data: bytes, root: Path, target: str) -> None:
    metadata = parse_task_metadata(data.decode(), root)
    if metadata is None or not metadata.is_manager or metadata.runat != target or metadata.status == "done":
        raise TaskFrontmatterError(f"manager custody at {target} changed.")


def target_pin(target: str) -> PanePin:
    pane_id, pane_pid, pane_start_ticks = target_identity(target)
    if not pane_id:
        raise TaskFrontmatterError(f"required target {target} is absent.")
    session_id = session_from_process(pane_pid)
    if SESSION_RE.fullmatch(session_id) is None:
        raise TaskFrontmatterError(f"required target {target} has no exact Codex session.")
    return PanePin(target, pane_id, pane_pid, pane_start_ticks, session_id)


def validate_pin(pin: PanePin, *, require_ready: bool) -> None:
    if (
        target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks)
        or session_from_process(pin.pane_pid) != pin.session_id
        or (require_ready and inspect_target(pin.target) != "ready")
    ):
        raise TaskFrontmatterError(f"target {pin.target} identity or readiness changed.")


def protected_snapshots() -> list[dict[str, object]]:
    return [asdict(target_pin(target)) for target in PROTECTED_TARGETS]


def validate_protected(expected: object, message: str) -> None:
    if protected_snapshots() != expected:
        raise TaskFrontmatterError(message)


def validate_committed_recovery(live_identity: tuple[str, int, int], prepared_exists: bool) -> None:
    if live_identity != ("", 0, 0) or not prepared_exists:
        raise TaskFrontmatterError("committed config:22 audit conflicts with a live target or missing prepared proof.")


def prepared_proof_exists(path: Path, expected: bytes) -> bool:
    return existing_exact(path, expected, "config:22 prepared close audit") if path.exists() else False


def hold_prepared_proof(path: Path, expected: bytes) -> HeldAbsolute:
    data, identity, ancestors = absolute_file_binding(path, "config:22 prepared close audit")
    if data != expected:
        raise TaskFrontmatterError("config:22 prepared close audit changed.")
    handle = hold_absolute(identity, ancestors)
    validate_held_absolute(handle)
    return handle


def existing_committed_audit(
    audit_path: Path,
    committed: bytes,
    live_identity: tuple[str, int, int],
    prepared_path: Path,
    prepared: bytes,
) -> bool:
    if not audit_path.exists():
        return False
    read_regular(audit_path, sha256(committed))
    validate_committed_recovery(live_identity, prepared_proof_exists(prepared_path, prepared))
    return True


def classify_close_state(
    pin: PanePin,
    live_identity: tuple[str, int, int],
    prepared_path: Path,
    prepared: bytes,
) -> bool:
    """Return true only for a live ready target; require prior proof for recovery."""

    if live_identity == (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        validate_pin(pin, require_ready=True)
        return True
    if live_identity == ("", 0, 0) and prepared_proof_exists(prepared_path, prepared):
        return False
    raise TaskFrontmatterError("config:22 pane identity changed before closure.")


def parse_pin(ns: argparse.Namespace) -> PanePin:
    pin = PanePin(ns.target, ns.pane_id, ns.pane_pid, ns.pane_start_ticks, ns.session_id.lower())
    if (
        pin.target != TARGET
        or re.fullmatch(r"%[0-9]+", pin.pane_id) is None
        or pin.pane_pid <= 1
        or pin.pane_start_ticks <= 0
        or SESSION_RE.fullmatch(pin.session_id) is None
    ):
        raise TaskFrontmatterError("config:22 pane binding is malformed.")
    return pin


def prepare(ns: argparse.Namespace) -> None:
    root = ns.root.resolve(strict=True)
    task = (root / ns.task).resolve(strict=True)
    todo = root / "TODO.md"
    authority = ns.authority.resolve(strict=True)
    audit18 = ns.audit18.resolve(strict=True)
    audit19 = ns.audit19.resolve(strict=True)
    task_manager = (root / ns.task_manager_file).resolve(strict=True)
    executor_task = (root / ns.executor_task).resolve(strict=True)
    helper = Path(__file__).resolve(strict=True)
    pin = parse_pin(ns)
    if (
        relative_task_ref(root, task) != TASK
        or authority != root / AUTHORITY
        or ns.authority_sha256 != AUTHORITY_SHA256
        or audit18 != AUDIT18
        or ns.audit18_sha256 != AUDIT18_SHA256
        or audit19 != AUDIT19
        or ns.audit19_sha256 != AUDIT19_SHA256
        or relative_task_ref(root, task_manager) != TASK_MANAGER_FILE
        or ns.task_manager != TASK_MANAGER
        or relative_task_ref(root, executor_task) != EXECUTOR_TASK
        or ns.executor != EXECUTOR
        or ns.destination_target != EXECUTOR
        or tuple(sorted(ns.protected_target)) != PROTECTED_TARGETS
    ):
        raise TaskFrontmatterError("config:22 terminal closure scope is not exact.")
    input_paths = {task, todo, authority, audit18, audit19, task_manager, executor_task, helper}
    ns.packet = require_private_output(ns.packet, input_paths)
    ns.audit = require_private_output(ns.audit, input_paths | {ns.packet})
    with root_membership_lock(root), task_target_lock(root, TARGET), ExitStack() as locks:
        for path in sorted(input_paths, key=str):
            locks.enter_context(task_file_lock(path))
        task_data, _ = read_regular(task, ns.task_sha256)
        todo_data, _ = read_regular(todo, ns.todo_sha256)
        authority_data, _ = read_regular(authority, ns.authority_sha256)
        audit18_data, _ = read_regular(audit18, ns.audit18_sha256)
        audit19_data, _ = read_regular(audit19, ns.audit19_sha256)
        task_manager_data, _ = read_regular(task_manager, ns.task_manager_sha256)
        executor_data, _ = read_regular(executor_task, ns.executor_task_sha256)
        helper_data, _ = read_regular(helper, sha256(helper.read_bytes()))
        validate_authority(authority_data, (3, 4))
        validate_audits(audit18_data, audit19_data)
        validate_task(root, task, task_data, todo_data)
        validate_manager(task_manager_data, root, TASK_MANAGER)
        validate_manager(executor_data, root, EXECUTOR)
        validate_pin(pin, require_ready=False)
        protected = protected_snapshots()
        validate_pin(pin, require_ready=False)
        if protected_snapshots() != protected:
            raise TaskFrontmatterError("a protected target changed during packet preparation.")
        inputs = []
        for path in sorted(input_paths, key=str):
            _, identity, ancestors = absolute_file_binding(path, f"config:22 terminal-close input {path}")
            inputs.append({"file": asdict(identity), "ancestors": [asdict(item) for item in ancestors]})
        packet: dict[str, object] = {
            "schema": SCHEMA,
            "root": str(root),
            "task": TASK,
            "todo": str(todo),
            "task_sha256": ns.task_sha256,
            "todo_sha256": ns.todo_sha256,
            "authority": str(authority),
            "authority_sha256": ns.authority_sha256,
            "authority_lines": [3, 4],
            "audit18": str(audit18),
            "audit18_sha256": ns.audit18_sha256,
            "audit19": str(audit19),
            "audit19_sha256": ns.audit19_sha256,
            "replay_id": REPLAY_ID,
            "task_manager_file": TASK_MANAGER_FILE,
            "task_manager_sha256": ns.task_manager_sha256,
            "task_manager": TASK_MANAGER,
            "executor_task": EXECUTOR_TASK,
            "executor_task_sha256": ns.executor_task_sha256,
            "executor": EXECUTOR,
            "pane": asdict(pin),
            "protected_targets": list(PROTECTED_TARGETS),
            "protected_panes": protected,
            "helper": str(helper),
            "helper_sha256": sha256(helper_data),
            "audit": str(ns.audit),
            "destination_target": ns.destination_target,
            "inputs": inputs,
        }
        packet["binding_id"] = sha256(canonical(packet))
        publish_or_validate(ns.packet, canonical(packet), "config:22 terminal-close packet")
    print(ns.packet)


def process_is_under(pid: int, ancestor: int) -> bool:
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        if pid == ancestor:
            return True
        seen.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            return False
    return False


def validate_executor() -> None:
    caller = os.environ.get("TMUX_PANE", "")
    pane_id, pane_pid, _start_ticks = target_identity(EXECUTOR)
    if (
        re.fullmatch(r"%[0-9]+", caller) is None
        or pane_id != caller
        or exact_pane_id(EXECUTOR) != caller
        or not process_is_under(os.getpid(), pane_pid)
    ):
        raise TaskFrontmatterError("config:22 terminal closure is restricted to the current wl:21 pane.")


def execute(ns: argparse.Namespace) -> None:
    validate_executor()
    packet_data, _ = read_regular(ns.packet.resolve(strict=True), ns.packet_sha256)
    review_data, _ = read_regular(ns.review.resolve(strict=True), ns.review_sha256)
    packet = json.loads(packet_data)
    if not isinstance(packet, dict) or set(packet) != PACKET_KEYS or packet.get("schema") != SCHEMA:
        raise TaskFrontmatterError("config:22 terminal-close packet is malformed.")
    unsigned = dict(packet)
    binding_id = unsigned.pop("binding_id")
    if binding_id != sha256(canonical(unsigned)):
        raise TaskFrontmatterError("config:22 packet binding is invalid.")
    validate_review(review_data, ns.packet_sha256)
    root = Path(str(packet["root"])).resolve(strict=True)
    task = root / str(packet["task"])
    todo = Path(str(packet["todo"]))
    raw_pin = packet["pane"]
    raw_inputs = packet["inputs"]
    if not isinstance(raw_pin, dict) or set(raw_pin) != {"target", "pane_id", "pane_pid", "pane_start_ticks", "session_id"}:
        raise TaskFrontmatterError("config:22 packet pane binding is malformed.")
    pin = PanePin(**raw_pin)
    if not isinstance(raw_inputs, list) or any(
        not isinstance(item, dict) or set(item) != {"file", "ancestors"} or not isinstance(item["ancestors"], list)
        for item in raw_inputs
    ):
        raise TaskFrontmatterError("config:22 packet input bindings are malformed.")
    lock_paths = {Path(file_identity_from(item["file"], "closure input").path) for item in raw_inputs}
    expected_paths = {
        task, todo, Path(str(packet["authority"])), Path(str(packet["audit18"])),
        Path(str(packet["audit19"])), root / str(packet["task_manager_file"]),
        root / str(packet["executor_task"]), Path(str(packet["helper"])),
    }
    if (
        str(root) != packet["root"]
        or task != root / TASK
        or todo != root / "TODO.md"
        or Path(str(packet["authority"])) != root / AUTHORITY
        or packet["authority_sha256"] != AUTHORITY_SHA256
        or packet["authority_lines"] != [3, 4]
        or Path(str(packet["audit18"])) != AUDIT18
        or packet["audit18_sha256"] != AUDIT18_SHA256
        or Path(str(packet["audit19"])) != AUDIT19
        or packet["audit19_sha256"] != AUDIT19_SHA256
        or packet["replay_id"] != REPLAY_ID
        or packet["task_manager_file"] != TASK_MANAGER_FILE
        or packet["task_manager"] != TASK_MANAGER
        or packet["executor_task"] != EXECUTOR_TASK
        or packet["executor"] != EXECUTOR
        or packet["destination_target"] != EXECUTOR
        or packet["protected_targets"] != list(PROTECTED_TARGETS)
        or Path(str(packet["helper"])).resolve(strict=True) != Path(__file__).resolve(strict=True)
        or packet["helper_sha256"] != sha256(Path(__file__).read_bytes())
        or lock_paths != expected_paths
    ):
        raise TaskFrontmatterError("config:22 packet inputs or protection set changed.")
    audit = {key: packet[key] for key in PACKET_KEYS - {"inputs"}}
    prepared = canonical({**audit, "state": "prepared"})
    committed = canonical({**audit, "state": "committed"})
    prepared_path = Path(f"{packet['audit']}.prepared")
    with root_membership_lock(root), task_target_lock(root, TARGET), ExitStack() as locks:
        for path in sorted(lock_paths, key=str):
            locks.enter_context(task_file_lock(path))
        held = []
        for item in raw_inputs:
            identity = file_identity_from(item["file"], "closure input")
            ancestors = tuple(directory_identity_from(value, "closure input ancestor") for value in item["ancestors"])
            handle = hold_absolute(identity, ancestors)
            held.append(handle)
            locks.callback(os.close, handle.descriptor)
            for descriptor in reversed(handle.directories):
                locks.callback(os.close, descriptor)
            validate_held_absolute(handle)
        task_data, _ = read_regular(task, str(packet["task_sha256"]))
        todo_data, _ = read_regular(todo, str(packet["todo_sha256"]))
        authority_data, _ = read_regular(Path(str(packet["authority"])), AUTHORITY_SHA256)
        audit18_data, _ = read_regular(AUDIT18, AUDIT18_SHA256)
        audit19_data, _ = read_regular(AUDIT19, AUDIT19_SHA256)
        task_manager_data, _ = read_regular(root / TASK_MANAGER_FILE, str(packet["task_manager_sha256"]))
        executor_data, _ = read_regular(root / EXECUTOR_TASK, str(packet["executor_task_sha256"]))
        validate_authority(authority_data, (3, 4))
        validate_audits(audit18_data, audit19_data)
        validate_task(root, task, task_data, todo_data)
        validate_manager(task_manager_data, root, TASK_MANAGER)
        validate_manager(executor_data, root, EXECUTOR)
        validate_protected(packet["protected_panes"], "a protected target changed before config:22 closure.")
        live_identity = target_identity(TARGET)
        target_was_live = classify_close_state(pin, live_identity, prepared_path, prepared)
        if existing_committed_audit(Path(str(packet["audit"])), committed, live_identity, prepared_path, prepared):
            return
        if target_was_live:
            publish_or_validate(prepared_path, prepared, "config:22 prepared close audit")
        proof_handle = hold_prepared_proof(prepared_path, prepared)
        locks.callback(os.close, proof_handle.descriptor)
        for descriptor in reversed(proof_handle.directories):
            locks.callback(os.close, descriptor)
        validate_held_absolute(proof_handle)
        if target_was_live:
            try:
                validate_protected(packet["protected_panes"], "a protected target changed before the config:22 pane close.")
            except TaskFrontmatterError as exc:
                raise IndeterminateClose(str(exc)) from exc
            validate_pin(pin, require_ready=True)
            guarded_tmux_command(pin.target, pin.pane_id, ["kill-pane", "-t", pin.pane_id], pin.pane_pid)
        if target_identity(TARGET) != ("", 0, 0):
            raise IndeterminateClose("config:22 remains after its exact guarded close.")
        try:
            validate_protected(packet["protected_panes"], "a protected target changed across the config:22 close.")
        except TaskFrontmatterError as exc:
            raise IndeterminateClose(str(exc)) from exc
        for handle in held:
            validate_held_absolute(handle)
        validate_held_absolute(proof_handle)
        publish_or_validate(Path(str(packet["audit"])), committed, "config:22 committed close audit")
        validate_held_absolute(proof_handle)
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
    prepare_parser.add_argument("--audit18", type=Path, required=True)
    prepare_parser.add_argument("--audit18-sha256", required=True)
    prepare_parser.add_argument("--audit19", type=Path, required=True)
    prepare_parser.add_argument("--audit19-sha256", required=True)
    prepare_parser.add_argument("--task-manager-file", type=Path, required=True)
    prepare_parser.add_argument("--task-manager-sha256", required=True)
    prepare_parser.add_argument("--task-manager", required=True)
    prepare_parser.add_argument("--executor-task", type=Path, required=True)
    prepare_parser.add_argument("--executor-task-sha256", required=True)
    prepare_parser.add_argument("--executor", required=True)
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
        print(f"omo_config22_terminal_close.py: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
