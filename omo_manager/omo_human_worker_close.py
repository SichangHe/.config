#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Prepare and execute one Human-authorized blocked live-worker closure."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_status import exact_pane_id, exact_tail
from omo_manager.omo_codex_stop import bound_guarded_read, codex_status, guarded_tmux_command
from omo_manager.omo_tmux_send import (
    capture_complete_input_lines,
    exact_codex_runtime_binding,
    source_bound_wrapped_candidates,
    wrap_agent_message,
)
from omo_manager.omo_exported_agent_close import (
    IndeterminateClose,
    read_regular,
    read_regular_unbound,
    replace_held,
    require_private_output,
    restore_exact_after,
)
from omo_manager.omo_repository_custody import (
    FileIdentity,
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
from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import (
    authoritative_active_target_task_paths,
    has_pending_marker,
    relative_task_ref,
    root_membership_lock,
    todo_row_task_paths,
    update_frontmatter_status,
)

SCHEMA = "omo-human-worker-continuity-close/v1"
REVIEW_SCHEMA = "omo-human-worker-continuity-close-review/v1"
TARGET = "config:16"
TASK = "dw2_input_clear.md"
BLOCKER = "config16_close.md"
AUTHORITY = "manager_mail/85c5dff58359-1570.txt"
AUTHORITY_SHA256 = "2b1e6ed1b653cf79f459a8ff33113df6c262b665368ffb010fbdbb7888625e0f"
MANAGER_TASK = "transport_closure_mgr.md"
ORIGINAL_MANAGER = "wl:12"
CURRENT_MANAGER = "wl:21"
REPLAY_ID = "205eec6dff48bc1feedd27d3521f86580d86d9943dad469056c63a33dec9d9dc"
REPORT_MESSAGE_SHA256 = "51629a0a165aca98a7b7d482a82df3dd9856dc55b8c8064632be78362c6eef27"
TERMINAL_REPORT_SHA256 = "784dfcd424f9a385dbec8098d305a61c72bfe4e48de7e0724ccaa922aab46ddc"
TERMINAL_COMMITMENT_SHA256 = "d25ae8dc76d9da546fc1b6f5a4767be0fefb8eb35f152c19a2a05ba917395a84"
DW2_TARGET = "dw2:0"
DW2_PANE_ID = "%432"
DW2_PANE_PID = 388967
PROTECTED_TARGETS = ("config:18", "config:19", "config:20", DW2_TARGET)
HISTORICAL_SESSION_ID = "01a084f5-35e6-7492-af01-7eccf2a21bb3"
HISTORICAL_ROLLOUT_NAME = "rollout-2026-09-08T23-57-37-01a084f5-35e6-7492-af01-7eccf2a21bb3.jsonl"
COMPOSER_SOURCE_TARGET = "wl:1"
SESSION_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACKET_KEYS = {
    "schema",
    "root",
    "task",
    "todo",
    "task_before_sha256",
    "todo_before_sha256",
    "task_after_sha256",
    "todo_after_sha256",
    "task_after_base64",
    "todo_after_base64",
    "authority",
    "authority_sha256",
    "authority_lines",
    "terminal_report",
    "terminal_report_sha256",
    "terminal_commitment",
    "terminal_commitment_sha256",
    "replay_id",
    "original_manager",
    "manager_task",
    "manager_task_sha256",
    "manager_target",
    "pane",
    "terminal_tail_sha256",
    "protected_targets",
    "protected_panes",
    "dw2_target",
    "dw2_pane_id",
    "dw2_pane_pid",
    "historical_rollout",
    "historical_rollout_sha256",
    "composer_source",
    "composer_source_sha256",
    "composer_capture_sha256",
    "composer_rendered_sha256s",
    "composer_runtime",
    "audit",
    "destination_target",
    "inputs",
    "binding_id",
}


@dataclass(frozen=True)
class PanePin:
    target: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int
    session_id: str


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(record: dict[str, object]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


# 🧑 "You're saying they're completed, then just send a bunch of control C and kill the Tmux window, no?"
def validate_authority(data: bytes, lines: tuple[int, int]) -> None:
    """Require the exact Source-1570 closure authorization line."""

    source = data.decode("utf-8").splitlines()
    if lines != (3, 4) or source[2:4] != ["You're saying they're completed, then just send a bunch of control C and", "kill the Tmux window, no?"]:
        raise TaskFrontmatterError("Human Source 1570 authorization text or line selection changed.")


def proc_fields(pid: int, proc_root: Path = Path("/proc")) -> tuple[int, int]:
    try:
        fields = (proc_root / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
        return int(fields[1]), int(fields[19])
    except (OSError, IndexError, ValueError) as exc:
        raise TaskFrontmatterError("Codex process identity is unavailable.") from exc


def descendant_pids(root_pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    parents: dict[int, int] = {}
    for child in proc_root.iterdir():
        if child.name.isdecimal():
            try:
                parents[int(child.name)] = proc_fields(int(child.name), proc_root)[0]
            except TaskFrontmatterError:
                continue
    result: set[int] = set()
    while True:
        added = {child for child, parent in parents.items() if child not in result and (parent == root_pid or parent in result)}
        if not added:
            return result
        result.update(added)


def target_identity(target: str) -> tuple[str, int, int]:
    """Capture one target through the current tmux server, with final revalidation."""

    pane = exact_pane_id(target)
    if not pane:
        return "", 0, 0
    raw = bound_guarded_read(target, pane, ["display-message", "-p", "-t", target, "#{pane_id}|#{pane_pid}"]).strip()
    resolved, separator, raw_pid = raw.partition("|")
    if not separator or resolved != pane or not raw_pid.isdecimal():
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


def validate_historical_rollout(data: bytes, path: Path) -> None:
    if path.name != HISTORICAL_ROLLOUT_NAME:
        raise TaskFrontmatterError("historical rollout does not bind protected dw2:0, session, and replay.")
    events: dict[int, dict[str, object]] = {}
    try:
        for line in data.splitlines():
            record = json.loads(line)
            if isinstance(record, dict) and isinstance(record.get("ordinal"), int):
                ordinal = int(record["ordinal"])
                if ordinal in events:
                    raise TaskFrontmatterError("historical rollout contains a duplicate event ordinal.")
                events[ordinal] = record
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("historical rollout is not canonical JSONL.") from exc

    def command_stdout(ordinal: int) -> str:
        record = events.get(ordinal)
        payload = record.get("payload") if isinstance(record, dict) else None
        item = payload.get("item") if isinstance(payload, dict) else None
        if (
            record is None
            or record.get("type") != "event_msg"
            or not isinstance(payload, dict)
            or payload.get("type") != "item_completed"
            or payload.get("thread_id") != HISTORICAL_SESSION_ID
            or not isinstance(item, dict)
            or item.get("type") != "CommandExecution"
            or not isinstance(item.get("stdout"), str)
        ):
            raise TaskFrontmatterError("historical rollout event shape changed.")
        return str(item["stdout"])

    if (
        "session=dw2 window=0 pane=0 pid=388967 command=bunx dead=0" not in command_stdout(39)
        or f"pane={DW2_PANE_ID} pid={DW2_PANE_PID} command=bunx dead=0" not in command_stdout(127)
        or '"replay_id":"205eec6dff48bc1feedd27d3521f86580d86d9943dad469056c63a33dec9d9dc"' not in command_stdout(263)
    ):
        raise TaskFrontmatterError("historical rollout does not bind protected dw2:0, session, and replay.")


def validate_owned_source(identity: FileIdentity) -> None:
    if identity.uid != os.getuid() or identity.mode & 0o022:
        raise TaskFrontmatterError("historical rollout must be current-user-owned and not group/other writable.")


def validate_bound_source(identity: FileIdentity, historical_rollout: Path) -> None:
    if Path(identity.path) == historical_rollout:
        validate_owned_source(identity)


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


def validate_protected_dw2(snapshots: object) -> None:
    if not isinstance(snapshots, list):
        raise TaskFrontmatterError("protected target snapshots are malformed.")
    dw2 = [snapshot for snapshot in snapshots if isinstance(snapshot, dict) and snapshot.get("target") == DW2_TARGET]
    if len(dw2) != 1 or dw2[0].get("pane_id") != DW2_PANE_ID or dw2[0].get("pane_pid") != DW2_PANE_PID:
        raise TaskFrontmatterError("protected dw2:0 does not match the authenticated historical pane and process.")


def composer_snapshot(pin: PanePin, source: bytes) -> dict[str, object]:
    if target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        raise TaskFrontmatterError("config:16 changed before composer capture.")
    source_text = source.decode("utf-8")
    rendering = f"{wrap_agent_message(source_text, source_target=COMPOSER_SOURCE_TARGET, include_authority_reminder=True)}\n"
    lines = capture_complete_input_lines(pin.pane_id, full_history=True)
    ordinary, trailing = source_bound_wrapped_candidates(lines, rendering, True)
    runtime = exact_codex_runtime_binding(pin.target, allow_shell=True)
    if (runtime.pane_id, runtime.pane_pid) != (pin.pane_id, pin.pane_pid):
        raise TaskFrontmatterError("config:16 runtime changed during composer capture.")
    if target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        raise TaskFrontmatterError("config:16 changed during composer capture.")
    return {
        "capture_sha256": sha256("\n".join(lines).encode()),
        "rendered_sha256s": sorted((sha256(ordinary.encode()), sha256(trailing.encode()))),
        "runtime": asdict(runtime),
    }


def close_bound_target(pin: PanePin) -> None:
    if target_identity(TARGET) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks) or session_from_process(pin.pane_pid) != pin.session_id:
        raise TaskFrontmatterError("config:16 identity changed before its guarded pane close.")
    guarded_tmux_command(TARGET, pin.pane_id, ["kill-pane", "-t", pin.pane_id], pin.pane_pid)


def session_from_process(pane_pid: int, proc_root: Path = Path("/proc")) -> str:
    """Bind one primary Codex process to its launch-time rollout UUID."""

    codex: list[int] = []
    for pid in descendant_pids(pane_pid, proc_root):
        try:
            executable = os.readlink(proc_root / str(pid) / "exe")
            argv = (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if Path(executable.removesuffix(" (deleted)")).name == "codex" and b"--dangerously-bypass-approvals-and-sandbox" in argv:
            codex.append(pid)
    if len(codex) != 1:
        raise TaskFrontmatterError("live pane does not contain one exact primary Codex process.")
    pid = codex[0]
    _, start_ticks = proc_fields(pid, proc_root)
    try:
        boot_s = next(int(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines() if line.startswith("btime "))
        started_s = boot_s + start_ticks / os.sysconf("SC_CLK_TCK")
        descriptors = (proc_root / str(pid) / "fd").iterdir()
    except (OSError, StopIteration, ValueError) as exc:
        raise TaskFrontmatterError("Codex process start time is unavailable.") from exc
    sessions: list[tuple[float, str]] = []
    for descriptor in descriptors:
        try:
            destination = descriptor.readlink()
        except OSError:
            continue
        match = re.fullmatch(r".*/rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-([0-9a-f-]{36})\.jsonl", str(destination))
        if match is None or SESSION_RE.fullmatch(match.group(2)) is None:
            continue
        created_s = datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S").astimezone().timestamp()
        if abs(created_s - started_s) <= 120:
            sessions.append((abs(created_s - started_s), match.group(2).lower()))
    sessions.sort()
    if not sessions or (len(sessions) > 1 and sessions[0][0] == sessions[1][0]):
        raise TaskFrontmatterError("primary Codex rollout identity is unavailable or ambiguous.")
    return sessions[0][1]


def terminal_transfer(data: bytes) -> dict[str, object]:
    line = next((line for line in data.splitlines() if line.startswith(b"[omo-transfer: ")), b"")
    try:
        transfer = json.loads(line.removeprefix(b"[omo-transfer: ").removesuffix(b"]"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("terminal report transfer is malformed.") from exc
    if not isinstance(transfer, dict):
        raise TaskFrontmatterError("terminal report transfer is malformed.")
    return transfer


def validate_terminal_report(data: bytes, path: Path, root: Path) -> Path:
    report = authenticated_report(data, "Source-1570 terminal replay")
    transfer = terminal_transfer(data)
    routing = transfer.get("routing")
    queue = transfer.get("queue_item")
    commitment = Path(str(report["commitment_path"]))
    if (
        report["message_sha256"] != REPORT_MESSAGE_SHA256
        or canonical_target(str(report["producer_target"])) != canonical_target(TARGET)
        or report["source_task"] != str(root / TASK)
        or not isinstance(routing, dict)
        or routing.get("requested_manager_target") != ORIGINAL_MANAGER
        or routing.get("resolved_manager_target") != ORIGINAL_MANAGER
        or not isinstance(queue, dict)
        or queue.get("replay_id") != REPLAY_ID
        or commitment.name != f"{REPLAY_ID}.commitment"
        or b"Completion and no-mail closure:" not in data
        or b"subsequent `omo_pending.py list` returned empty output." not in data
        or b"No Human email was sent." not in data
        or path.name not in str(transfer)
    ):
        raise TaskFrontmatterError("terminal replay does not prove this completed no-mail worker outcome.")
    return commitment.resolve(strict=True)


def todo_after(root: Path, task: Path, data: bytes) -> bytes:
    lines = data.decode("utf-8").splitlines(keepends=True)
    section = ""
    rows: list[tuple[int, str]] = []
    previous = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
        if task in todo_row_task_paths(root, line):
            rows.append((index, section))
        if stripped == "previous:":
            previous.append(index)
    ref = relative_task_ref(root, task)
    if len(rows) != 1 or rows[0][1] != "current" or lines[rows[0][0]].strip() != f"{ref} {TARGET}" or len(previous) != 1:
        raise TaskFrontmatterError("task must have one exact canonical current TODO row.")
    lines.pop(rows[0][0])
    previous_index = next(index for index, line in enumerate(lines) if line.strip() == "previous:")
    lines.insert(previous_index + 1, f"{ref}\n")
    return "".join(lines).encode()


def task_after(root: Path, text: str, authority: Path) -> bytes:
    updated = update_frontmatter_status(text, "done", "", root).rstrip("\n")
    return f"{updated}\n\n(Source-1570 Human-authorized no-mail close; authority {authority}; terminal replay {REPLAY_ID})\n".encode()


def parse_pin(ns: argparse.Namespace) -> PanePin:
    pin = PanePin(ns.target, ns.pane_id, ns.pane_pid, ns.pane_start_ticks, ns.session_id.lower())
    if pin.target != TARGET or not re.fullmatch(r"%[0-9]+", pin.pane_id) or pin.pane_pid <= 1 or pin.pane_start_ticks <= 0 or SESSION_RE.fullmatch(pin.session_id) is None:
        raise TaskFrontmatterError("live pane binding is malformed.")
    return pin


def validate_live(pin: PanePin, expected_tail_sha256: str = "") -> str:
    state = inspect_target(pin.target)
    if state != "stuck_input" or target_identity(pin.target) != (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
        raise TaskFrontmatterError("config:16 is not the exact unchanged stuck-input pane.")
    if session_from_process(pin.pane_pid) != pin.session_id:
        raise TaskFrontmatterError("config:16 Codex session identity changed.")
    captured, lines = exact_tail(pin.target, 80)
    tail_sha256 = sha256("\n".join(lines).encode()) if captured else ""
    if not tail_sha256 or (expected_tail_sha256 and tail_sha256 != expected_tail_sha256):
        raise TaskFrontmatterError("config:16 terminal/composer tail changed.")
    return tail_sha256


def prepare(ns: argparse.Namespace) -> None:
    root = ns.root.resolve(strict=True)
    task = (root / ns.task).resolve(strict=True)
    todo = root / "TODO.md"
    authority = ns.authority.resolve(strict=True)
    report_path = ns.terminal_report.resolve(strict=True)
    manager_task = (root / ns.manager_task).resolve(strict=True)
    historical_rollout = ns.historical_rollout.resolve(strict=True)
    composer_source = ns.composer_source.resolve(strict=True)
    if (
        relative_task_ref(root, task) != TASK
        or authority != root / AUTHORITY
        or ns.authority_sha256 != AUTHORITY_SHA256
        or relative_task_ref(root, manager_task) != MANAGER_TASK
        or ns.terminal_report_sha256 != TERMINAL_REPORT_SHA256
        or ns.replay_id != REPLAY_ID
        or ns.original_manager != ORIGINAL_MANAGER
        or ns.manager_target != CURRENT_MANAGER
        or ns.destination_target != CURRENT_MANAGER
        or tuple(sorted(ns.protected_target)) != PROTECTED_TARGETS
        or historical_rollout.name != HISTORICAL_ROLLOUT_NAME
    ):
        raise TaskFrontmatterError("Source-1570 closure scope is not exact.")
    pin = parse_pin(ns)
    if pin.session_id != HISTORICAL_SESSION_ID or pin.pane_id == DW2_PANE_ID or pin.pane_pid == DW2_PANE_PID:
        raise TaskFrontmatterError("current config:16 is not the exact same-session continuity target.")
    input_paths = {task, todo, authority, report_path, manager_task, historical_rollout, composer_source}
    ns.packet = require_private_output(ns.packet, input_paths)
    ns.audit = require_private_output(ns.audit, input_paths | {ns.packet})
    with root_membership_lock(root), task_target_lock(root, TARGET), ExitStack() as locks:
        for path in sorted(input_paths, key=str):
            locks.enter_context(task_file_lock(path))
        task_data, _ = read_regular(task, ns.task_sha256)
        todo_data, _ = read_regular(todo, ns.todo_sha256)
        authority_data, _ = read_regular(authority, ns.authority_sha256)
        report_data, _ = read_regular(report_path, ns.terminal_report_sha256)
        manager_data, _ = read_regular(manager_task, ns.manager_task_sha256)
        historical_data, _ = read_regular(historical_rollout, ns.historical_rollout_sha256)
        composer_source_data, _ = read_regular(composer_source, ns.composer_source_sha256)
        validate_authority(authority_data, ns.authority_lines)
        validate_historical_rollout(historical_data, historical_rollout)
        commitment = validate_terminal_report(report_data, report_path, root)
        commitment_data, _ = read_regular_unbound(commitment)
        if sha256(commitment_data) != TERMINAL_COMMITMENT_SHA256:
            raise TaskFrontmatterError("terminal replay commitment is not the committed Source-1570 replay.")
        metadata = parse_task_metadata(task_data.decode(), root)
        manager = parse_task_metadata(manager_data.decode(), root)
        if (
            metadata is None
            or metadata.status != "blocked"
            or metadata.blocked_on != BLOCKER
            or metadata.runat != TARGET
            or metadata.managerat != CURRENT_MANAGER
            or metadata.is_manager
            or metadata.pending_task_items
            or has_pending_marker(task_data.decode())
            or manager is None
            or not manager.is_manager
            or manager.runat != CURRENT_MANAGER
            or manager.managerat != ORIGINAL_MANAGER
            or manager.status == "done"
            or authoritative_active_target_task_paths(root, TARGET) != (task,)
        ):
            raise TaskFrontmatterError("current task or manager custody is not the exact Source-1570 closure shape.")
        after_task = task_after(root, task_data.decode(), authority)
        after_todo = todo_after(root, task, todo_data)
        tail_sha256 = validate_live(pin)
        composer = composer_snapshot(pin, composer_source_data)
        protected = protected_snapshots()
        validate_protected_dw2(protected)
        if validate_live(pin, tail_sha256) != tail_sha256:
            raise TaskFrontmatterError("config:16 changed across rebind preparation.")
        inputs = []
        for path in sorted({*input_paths, commitment}, key=str):
            _, identity, ancestors = absolute_file_binding(
                path,
                f"Source-1570 closure input {path}",
                private=path in {report_path, commitment},
            )
            validate_bound_source(identity, historical_rollout)
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
            "authority": str(authority),
            "authority_sha256": ns.authority_sha256,
            "authority_lines": list(ns.authority_lines),
            "terminal_report": str(report_path),
            "terminal_report_sha256": ns.terminal_report_sha256,
            "terminal_commitment": str(commitment),
            "terminal_commitment_sha256": sha256(commitment_data),
            "replay_id": REPLAY_ID,
            "original_manager": ORIGINAL_MANAGER,
            "manager_task": str(manager_task),
            "manager_task_sha256": ns.manager_task_sha256,
            "manager_target": CURRENT_MANAGER,
            "pane": asdict(pin),
            "terminal_tail_sha256": tail_sha256,
            "protected_targets": list(PROTECTED_TARGETS),
            "protected_panes": protected,
            "dw2_target": DW2_TARGET,
            "dw2_pane_id": DW2_PANE_ID,
            "dw2_pane_pid": DW2_PANE_PID,
            "historical_rollout": str(historical_rollout),
            "historical_rollout_sha256": ns.historical_rollout_sha256,
            "composer_source": str(composer_source),
            "composer_source_sha256": ns.composer_source_sha256,
            "composer_capture_sha256": composer["capture_sha256"],
            "composer_rendered_sha256s": composer["rendered_sha256s"],
            "composer_runtime": composer["runtime"],
            "audit": str(ns.audit),
            "destination_target": ns.destination_target,
            "inputs": inputs,
        }
        packet["binding_id"] = sha256(canonical(packet))
        publish_or_validate(ns.packet, canonical(packet), "Source-1570 closure packet")
    print(ns.packet)


def decode_after(packet: dict[str, object], field: str, digest_field: str) -> bytes:
    try:
        data = base64.b64decode(str(packet[field]), validate=True)
    except ValueError as exc:
        raise TaskFrontmatterError("closure packet after-image is invalid.") from exc
    if sha256(data) != packet[digest_field]:
        raise TaskFrontmatterError("closure packet after-image digest is invalid.")
    return data


def lifecycle_state(packet: dict[str, object], task_data: bytes, todo_data: bytes) -> tuple[str, str]:
    """Accept only initial, TODO-first partial, or fully committed byte pairs."""

    current = (sha256(task_data), sha256(todo_data))
    before = (packet["task_before_sha256"], packet["todo_before_sha256"])
    after = (packet["task_after_sha256"], packet["todo_after_sha256"])
    if current not in {before, (before[0], after[1]), after}:
        raise TaskFrontmatterError("task/TODO state is neither initial, recoverable partial, nor committed.")
    return current


def validate_executor() -> None:
    caller = os.environ.get("TMUX_PANE", "")
    if re.fullmatch(r"%[0-9]+", caller) is None or exact_pane_id(CURRENT_MANAGER) != caller:
        raise TaskFrontmatterError("Source-1570 closure execution is restricted to the current wl:21 pane.")


def execute(ns: argparse.Namespace) -> None:
    validate_executor()
    packet_data, _ = read_regular(ns.packet.resolve(strict=True), ns.packet_sha256)
    review_data, _ = read_regular(ns.review.resolve(strict=True), ns.review_sha256)
    packet = json.loads(packet_data)
    if not isinstance(packet, dict) or set(packet) != PACKET_KEYS or packet.get("schema") != SCHEMA:
        raise TaskFrontmatterError("Source-1570 closure packet is malformed.")
    unsigned = dict(packet)
    binding_id = unsigned.pop("binding_id")
    if binding_id != sha256(canonical(unsigned)):
        raise TaskFrontmatterError("Source-1570 closure packet binding is invalid.")
    review_report = authenticated_report(review_data, "Source-1570 independent review")
    try:
        review = json.loads(review_data.split(b"message:\n", 1)[1])
    except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("Source-1570 independent review body is malformed.") from exc
    if review != {"schema": REVIEW_SCHEMA, "verdict": "PASS", "packet_sha256": ns.packet_sha256} or canonical_target(str(review_report["producer_target"])) in {
        canonical_target(TARGET),
        canonical_target(CURRENT_MANAGER),
    }:
        raise TaskFrontmatterError("independent review does not PASS this exact closure packet.")
    root = Path(str(packet["root"])).resolve(strict=True)
    task = root / str(packet["task"])
    todo = Path(str(packet["todo"]))
    raw_pin = packet["pane"]
    raw_inputs = packet["inputs"]
    if not isinstance(raw_pin, dict) or set(raw_pin) != {"target", "pane_id", "pane_pid", "pane_start_ticks", "session_id"}:
        raise TaskFrontmatterError("Source-1570 closure packet pane binding is malformed.")
    pin = PanePin(**raw_pin)
    if not isinstance(raw_inputs, list) or any(not isinstance(item, dict) or set(item) != {"file", "ancestors"} or not isinstance(item["ancestors"], list) for item in raw_inputs):
        raise TaskFrontmatterError("Source-1570 closure packet input binding is malformed.")
    lock_paths = {Path(file_identity_from(item["file"], "closure input").path) for item in raw_inputs}
    expected_paths = {
        task,
        todo,
        Path(str(packet["authority"])),
        Path(str(packet["terminal_report"])),
        Path(str(packet["terminal_commitment"])),
        Path(str(packet["manager_task"])),
        Path(str(packet["historical_rollout"])),
        Path(str(packet["composer_source"])),
    }
    if (
        str(root) != packet["root"]
        or task != root / TASK
        or todo != root / "TODO.md"
        or Path(str(packet["authority"])) != root / AUTHORITY
        or packet["authority_sha256"] != AUTHORITY_SHA256
        or Path(str(packet["manager_task"])) != root / MANAGER_TASK
        or packet["terminal_report_sha256"] != TERMINAL_REPORT_SHA256
        or packet["terminal_commitment_sha256"] != TERMINAL_COMMITMENT_SHA256
        or packet["replay_id"] != REPLAY_ID
        or packet["original_manager"] != ORIGINAL_MANAGER
        or packet["authority_lines"] != [3, 4]
        or packet["destination_target"] != CURRENT_MANAGER
        or pin.target != TARGET
        or pin.session_id != HISTORICAL_SESSION_ID
        or pin.pane_id == DW2_PANE_ID
        or pin.pane_pid == DW2_PANE_PID
        or packet["dw2_target"] != DW2_TARGET
        or packet["dw2_pane_id"] != DW2_PANE_ID
        or packet["dw2_pane_pid"] != DW2_PANE_PID
        or Path(str(packet["historical_rollout"])).name != HISTORICAL_ROLLOUT_NAME
        or lock_paths != expected_paths
        or packet["protected_targets"] != list(PROTECTED_TARGETS)
        or packet["manager_target"] != CURRENT_MANAGER
    ):
        raise TaskFrontmatterError("Source-1570 closure packet input or protection set is incomplete.")
    validate_protected_dw2(packet["protected_panes"])
    audit = {key: packet[key] for key in PACKET_KEYS - {"task_after_base64", "todo_after_base64", "inputs"}}
    prepared = canonical({**audit, "state": "prepared"})
    committed = canonical({**audit, "state": "committed"})
    prepared_path = Path(f"{packet['audit']}.prepared")
    prepared_exists = existing_exact(prepared_path, prepared, "Source-1570 prepared close audit") if prepared_path.exists() else False
    with root_membership_lock(root), task_target_lock(root, TARGET), ExitStack() as locks:
        for path in sorted(lock_paths, key=str):
            locks.enter_context(task_file_lock(path))
        held = {}
        for item in raw_inputs:
            identity = file_identity_from(item["file"], "closure input")
            validate_bound_source(identity, Path(str(packet["historical_rollout"])))
            ancestors = tuple(directory_identity_from(value, "closure input ancestor") for value in item["ancestors"])
            if prepared_exists and Path(identity.path) in {task, todo}:
                current_data, current_identity, current_ancestors = absolute_file_binding(
                    Path(identity.path),
                    "recoverable Source-1570 closure input",
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
        report_data, _ = read_regular(Path(str(packet["terminal_report"])), str(packet["terminal_report_sha256"]))
        manager_data, _ = read_regular(Path(str(packet["manager_task"])), str(packet["manager_task_sha256"]))
        historical_data, _ = read_regular(Path(str(packet["historical_rollout"])), str(packet["historical_rollout_sha256"]))
        composer_source_data, _ = read_regular(Path(str(packet["composer_source"])), str(packet["composer_source_sha256"]))
        validate_authority(authority_data, tuple(packet["authority_lines"]))
        validate_historical_rollout(historical_data, Path(str(packet["historical_rollout"])))
        if validate_terminal_report(report_data, Path(str(packet["terminal_report"])), root) != Path(str(packet["terminal_commitment"])):
            raise TaskFrontmatterError("terminal replay commitment changed.")
        read_regular(Path(str(packet["terminal_commitment"])), str(packet["terminal_commitment_sha256"]))
        metadata = parse_task_metadata(task_data.decode(), root)
        manager = parse_task_metadata(manager_data.decode(), root)
        before_state = task_sha256 == packet["task_before_sha256"]
        if (
            metadata is None
            or metadata.pending_task_items
            or manager is None
            or not manager.is_manager
            or manager.runat != CURRENT_MANAGER
            or manager.managerat != ORIGINAL_MANAGER
            or manager.status == "done"
        ):
            raise TaskFrontmatterError("task or manager lifecycle drifted before closure.")
        if before_state and (metadata.status != "blocked" or metadata.blocked_on != BLOCKER or metadata.managerat != CURRENT_MANAGER):
            raise TaskFrontmatterError("task lifecycle drifted before closure.")
        if not before_state and metadata.status != "done":
            raise TaskFrontmatterError("recovered task is not terminal.")
        owners = authoritative_active_target_task_paths(root, TARGET)
        expected_owners = (task,) if before_state else ()
        after_todo = decode_after(packet, "todo_after_base64", "todo_after_sha256")
        after_task = decode_after(packet, "task_after_base64", "task_after_sha256")
        if owners != expected_owners or (todo_sha256 == packet["todo_before_sha256"] and todo_after(root, task, todo_data) != after_todo):
            raise TaskFrontmatterError("task ownership or TODO custody drifted before closure.")
        if protected_snapshots() != packet["protected_panes"]:
            raise TaskFrontmatterError("a protected target changed before closure.")
        live_identity = target_identity(TARGET)
        if live_identity == (pin.pane_id, pin.pane_pid, pin.pane_start_ticks):
            validate_live(pin, str(packet["terminal_tail_sha256"]))
            composer = composer_snapshot(pin, composer_source_data)
            if (
                composer["capture_sha256"] != packet["composer_capture_sha256"]
                or composer["rendered_sha256s"] != packet["composer_rendered_sha256s"]
                or composer["runtime"] != packet["composer_runtime"]
            ):
                raise TaskFrontmatterError("config:16 composer bytes changed before closure.")
        elif not (prepared_exists and live_identity == ("", 0, 0)):
            raise TaskFrontmatterError("config:16 pane identity drifted before closure.")
        publish_or_validate(prepared_path, prepared, "Source-1570 prepared close audit")
        if Path(str(packet["audit"])).exists():
            read_regular(Path(str(packet["audit"])), sha256(committed))
            return
        if live_identity != ("", 0, 0):
            if protected_snapshots() != packet["protected_panes"]:
                raise IndeterminateClose("a protected target changed before the config:16 pane close.")
            current = composer_snapshot(pin, composer_source_data)
            if current["capture_sha256"] != packet["composer_capture_sha256"] or current["rendered_sha256s"] != packet["composer_rendered_sha256s"] or current["runtime"] != packet["composer_runtime"]:
                raise IndeterminateClose("config:16 composer or runtime changed before its guarded pane close.")
            close_bound_target(pin)
        if target_identity(TARGET) != ("", 0, 0):
            raise IndeterminateClose("config:16 remains after its exact guarded close.")
        if protected_snapshots() != packet["protected_panes"]:
            raise IndeterminateClose("a protected target changed across the config:16 close.")
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
            raise TaskFrontmatterError("post-close lifecycle verification failed; task/TODO bytes restored.")
        publish_or_validate(Path(str(packet["audit"])), committed, "Source-1570 committed close audit")
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
    prepare_parser.add_argument("--terminal-report", type=Path, required=True)
    prepare_parser.add_argument("--terminal-report-sha256", required=True)
    prepare_parser.add_argument("--replay-id", required=True)
    prepare_parser.add_argument("--original-manager", required=True)
    prepare_parser.add_argument("--manager-task", type=Path, required=True)
    prepare_parser.add_argument("--manager-task-sha256", required=True)
    prepare_parser.add_argument("--manager-target", required=True)
    prepare_parser.add_argument("--historical-rollout", type=Path, required=True)
    prepare_parser.add_argument("--historical-rollout-sha256", required=True)
    prepare_parser.add_argument("--composer-source", type=Path, required=True)
    prepare_parser.add_argument("--composer-source-sha256", required=True)
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
        print(f"omo_human_worker_close.py: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
