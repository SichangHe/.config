#!/usr/bin/env python3
"""Apply the immutable Source-1376 AMH queue-transfer and closure plan."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import TaskMetadata
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import close_exited_codex_shell_with_task_receipt
from omo_manager.omo_codex_stop import close_note
from omo_manager.omo_codex_stop import stop
from omo_manager.omo_task_edit import append_comment
from omo_manager.omo_task_edit import fsync_task_directories
from omo_manager.omo_task_edit import markdown_paths
from omo_manager.omo_task_edit import raw_target_claims
from omo_manager.omo_task_edit import render_pending_items
from omo_manager.omo_task_edit import require_v1_metadata
from omo_manager.omo_task_edit import task_bytes
from omo_manager.omo_task_edit import task_snapshot
from omo_manager.omo_task_edit import validate_closure_authority
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_status import DONE_CLOSE_IN_PROGRESS
from omo_manager.omo_task_status import authoritative_active_target_task_paths
from omo_manager.omo_task_status import ensure_manager_has_no_active_children
from omo_manager.omo_task_status import exact_pane_id
from omo_manager.omo_task_status import has_pending_marker
from omo_manager.omo_task_status import reconcile_todo_text
from omo_manager.omo_task_status import relative_task_ref
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_status import root_membership_lock
from omo_manager.omo_task_status import same_file_state
from omo_manager.omo_task_status import todo_row_task_paths
from omo_manager.omo_task_status import update_frontmatter_status

PLAN_SHA256 = "c0d7e98972312347fb1a727bafe8822d115814a98ba0ab7334f7d84942238070"
PLAN_PATH = Path("/ssd1/sichangheagent/amh1376-transfer-plan-20260902.md")
EXECUTION_BINDING_SHA256 = "d308101dbf82684f2cded53a9238dad13992f889eee185d5eec040af7d2693a6"
EXECUTION_BINDING_PATH = Path("/ssd1/sichangheagent/amh1376-execution-binding-20260903T082429Z.json")
SUPERSEDED_EXECUTION_BINDING_SHA256 = "9087a3f02b73fb9706545ad23080d4de7d737bf2f8ae066a74fc695214bbd648"
SUPERSEDED_EXECUTION_BINDING_PATH = Path("/ssd1/sichangheagent/amh1376-execution-binding-20260903T075526Z.json")
WORK_LOG_ROOT = Path("/ssd1/sichangheagent/work_logs")
AUTHORITY_SHA256 = "7cfcd4e7e776db227db5b8fcd92679051f7983a35c47576817d7c28b0a8b2a7f"
DESTINATION_REF = "amh1376_close.md"
BASE_DESTINATION_INITIAL_SHA256 = "9ebd2401b7649bc35c70be79cb479a28cb25d40bb05e0e35130c6d105dea9bdc"
DESTINATION_INITIAL_SHA256 = "ebd60e85d87ae6c7fca6280ee8988c990becbb8027c7b22b5ac5f64a5f02619b"
ROW29_CURRENT_SHA256 = "33b58625e87d3c70dd19fd6ed0a45fb864d23697cb7b07a47f98b452f3ef00f3"
AUTHORITY_REF = "manager_mail/85c5dff58359-1376.txt"
AUTHORITY_TEXT = "tell those agents to document anything worth keeping long-term, move out their pending task items, then close them all"
SHUTDOWN_BLOCKER = "Source-1376 shutdown: queue custody transfer and bottom-up closure by amh1376_close.md"
COMPLETED_SHELL_ROW = "17"
COMPLETED_SHELL_PANE = "%1855"
COMPLETED_SHELL_EVIDENCE = "eff8705ab015ee3d6436f29f5212cb6760e47c4fc14f24d222092f1207560a4e"
COMPLETED_SHELL_MESSAGE_ID = "178811521612.3360518.2912986841064040014@gmail.com"
COMPLETED_SHELL_SESSION_ID = "01a053cc-a419-72c3-812e-47024971a25a"
COMPLETED_SHELL_SESSION_PATH = Path("/home/sichangheagent/.codex/sessions/2026/08/30/rollout-2026-08-30T10-51-55-01a053cc-a419-72c3-812e-47024971a25a.jsonl")
COMPLETED_SHELL_SESSION_SHA256 = "9a3b599bd26ed853017c4b86995a28c843a3ac0469cc71ca859f48c417eb8c04"
COMPLETED_SHELL_SESSION_SIZE = 759427
COMPLETED_SHELL_COMPLETION_COMMAND = (
    "/home/sichangheagent/.config/omo_manager/omo_completion_email.py --root /ssd1/sichangheagent/work_logs --task /ssd1/sichangheagent/work_logs/amh1232_term_eval.md --outcome 'task done'"
)
TRANSFER_JOURNAL = ".omo-source1376-transfer.json"
LEGACY_TRANSFER_JOURNAL = ".omo-pending-closure-transfer.json"
PREPARED_BINDING_SCHEMA = "omo-source1376-prepared-binding/v1"
REVIEW_APPROVAL_SCHEMA = "omo-source1376-review-approval/v1"
REVIEWER_IDENTITY = "/root/source1376_review"
REVIEWER_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAJEGR0M4KgG5uA9N1uYoTS55nG3dlmxsHQMgE/DpxeuA=
-----END PUBLIC KEY-----
"""
REVIEWER_PUBLIC_KEY_SHA256 = "a90757d37ad9ab547a0209fd5db5bdc80e2485240623273d444c7df3a5e25fdf"
REVIEW_APPROVAL_SCOPE = "one reviewed Source-1376 binding and its continuously locked first queue-transfer commit"
PREPARED_BINDING_PURPOSE = (
    "Root-wide locked Source-1376 snapshot for independent review and exact approval while the complete lock set stays "
    "held through the first committed queue transfer; the immutable base plan and prior execution binding remain unchanged inputs."
)
PREPARED_BINDING_EXECUTION_RULE = (
    "Initial execution must use --reviewed-handoff: publish this exact snapshot while holding the root-membership lock and complete "
    "prepared task-file lock set, wait for independent approval of its exact SHA-256, revalidate it, and commit the first eligible "
    "transfer receipt before releasing any lock. Abort on any mismatch; never alter row 15 or substitute a stale digest."
)
RECEIPT_DIRECTORY_MODE_CONTRACT = (
    "An inherited setgid bit may be cleared only with an atomic creation-to-open guarantee proving exact custody of a "
    "just-created owner-owned empty directory. Because mkdirat plus openat has a replacement window, this helper fails "
    "closed on 02700 and never normalizes an existing directory."
)
PROTECTED_ROWS = frozenset({"29", "59", "69", "84"})
EXTERNAL_SHARED_ROWS = {"28": "29", "58": "59"}
INTERNAL_SHARED_PAIR = ("71", "72")
DEFERRED_TRANSFER_ROW = "36"
EARLY_CLOSE_ROWS = ("06", "39")
PRE_DEFERRED_CLOSE_ROWS = ("38", "40", "37")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

CLOSE_ORDER: tuple[str | tuple[str, str], ...] = (
    "01",
    "02",
    "11",
    "28",
    "50",
    "70",
    "68",
    "03",
    "18",
    "41",
    "07",
    "08",
    "23",
    "19",
    "21",
    "22",
    "31",
    "32",
    "33",
    "12",
    "13",
    "30",
    "55",
    "56",
    "54",
    "09",
    "17",
    "25",
    "26",
    "24",
    INTERNAL_SHARED_PAIR,
    "10",
    "78",
    "80",
    "81",
    "82",
    "83",
    "74",
    "14",
    "20",
    "34",
    "36",
    "42",
    "35",
    "43",
    "49",
    "51",
    "62",
    "63",
    "52",
    "44",
    "46",
    "47",
    "48",
    "64",
    "53",
    "60",
    "61",
    "66",
    "67",
    "65",
    "45",
    "15",
    "57",
    "76",
    "77",
    "75",
    "04",
    "05",
    "16",
    "73",
    "27",
    "58",
    "79",
)


@dataclass(frozen=True)
class PlanRow:
    row_id: str
    task_ref: str
    status: str
    is_manager: bool
    runat: str
    managerat: str
    n_items: int
    task_sha256: str
    queue_sha256: str
    transfer: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class Args:
    root: Path
    plan: Path
    binding: Path
    authority: Path
    receipt_dir: Path
    packet_output: Path
    prepared_binding: Path | None = None
    prepared_binding_sha256: str = ""
    prepare_binding: bool = False
    reviewed_handoff: bool = False
    review_approval: Path | None = None


_HELD_PREPARED_LOCKS_KEY = object()


class _HeldPreparedLocks:
    """Unforgeable-in-normal-use capability for the live prepared-handoff lock scope."""

    __slots__ = ("_active", "_key", "locked_paths", "markdown", "root")

    def __init__(
        self,
        key: object,
        root: Path,
        markdown: tuple[Path, ...],
        locked_paths: tuple[Path, ...],
    ) -> None:
        if key is not _HELD_PREPARED_LOCKS_KEY:
            raise RuntimeError("Source-1376 held-lock capability cannot be constructed externally.")
        self._key = key
        self._active = True
        self.root = root
        self.markdown = markdown
        self.locked_paths = frozenset(path.resolve() for path in locked_paths)

    def require(self, root: Path, required_paths: tuple[Path, ...]) -> tuple[Path, ...]:
        if not self._active or self._key is not _HELD_PREPARED_LOCKS_KEY or self.root != root or not {path.resolve() for path in required_paths}.issubset(self.locked_paths):
            raise TaskFrontmatterError("Source-1376 transfer lacks the live complete prepared-lock capability.")
        return self.markdown

    def invalidate(self) -> None:
        self._active = False


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def path_entry_exists(path: Path) -> bool:
    """Return whether a directory entry exists, including a dangling symlink."""

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode()


def queue_sha256(items: tuple[str, ...]) -> str:
    return sha256(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode())


def review_gate(args: Args, challenge: str) -> dict[str, object]:
    if args.review_approval is None or SHA256_RE.fullmatch(challenge) is None:
        raise TaskFrontmatterError("Source-1376 review gate is incomplete.")
    return {
        "schema": REVIEW_APPROVAL_SCHEMA,
        "reviewer": REVIEWER_IDENTITY,
        "signature_algorithm": "Ed25519",
        "public_key_sha256": REVIEWER_PUBLIC_KEY_SHA256,
        "challenge": challenge,
        "approval_output": str(args.review_approval),
        "scope": REVIEW_APPROVAL_SCOPE,
    }


def review_approval_unsigned(
    args: Args,
    prepared_path: Path,
    prepared_digest: str,
    prepared_document: dict[str, object],
) -> dict[str, object]:
    if args.prepared_binding is None or prepared_path.resolve() != args.prepared_binding.resolve() or SHA256_RE.fullmatch(prepared_digest) is None:
        raise TaskFrontmatterError("Source-1376 review approval names an invalid prepared binding.")
    gate = prepared_document.get("review_gate")
    implementation = prepared_document.get("implementation")
    if not isinstance(gate, dict) or not isinstance(implementation, dict) or gate != review_gate(args, str(gate.get("challenge", ""))):
        raise TaskFrontmatterError("Source-1376 prepared review gate is malformed.")
    implementation_digest = implementation.get("sha256")
    if SHA256_RE.fullmatch(str(implementation_digest or "")) is None:
        raise TaskFrontmatterError("Source-1376 prepared implementation digest is malformed.")
    return {
        "schema": REVIEW_APPROVAL_SCHEMA,
        "prepared_binding": str(prepared_path),
        "prepared_binding_sha256": prepared_digest,
        "implementation_sha256": str(implementation_digest),
        "challenge": str(gate["challenge"]),
        "reviewer": REVIEWER_IDENTITY,
        "verdict": "PASS",
        "signature_algorithm": "Ed25519",
        "public_key_sha256": REVIEWER_PUBLIC_KEY_SHA256,
        "scope": REVIEW_APPROVAL_SCOPE,
    }


def verify_review_signature(message: bytes, signature: bytes) -> None:
    if sha256(REVIEWER_PUBLIC_KEY_PEM) != REVIEWER_PUBLIC_KEY_SHA256 or len(signature) != 64:
        raise TaskFrontmatterError("Source-1376 reviewer key or signature is invalid.")
    with tempfile.TemporaryDirectory(prefix="omo-source1376-review-") as temporary:
        directory = Path(temporary)
        public_key = directory / "reviewer-public.pem"
        message_path = directory / "approval.json"
        signature_path = directory / "approval.sig"
        public_key.write_bytes(REVIEWER_PUBLIC_KEY_PEM)
        message_path.write_bytes(message)
        signature_path.write_bytes(signature)
        os.chmod(public_key, 0o600)
        os.chmod(message_path, 0o600)
        os.chmod(signature_path, 0o600)
        result = subprocess.run(
            (
                "/usr/bin/openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_key),
                "-rawin",
                "-in",
                str(message_path),
                "-sigfile",
                str(signature_path),
            ),
            check=False,
            capture_output=True,
        )
    if result.returncode != 0:
        raise TaskFrontmatterError("Source-1376 review approval signature is invalid.")


def validate_review_approval(
    args: Args,
    prepared_path: Path,
    prepared_digest: str,
    prepared_document: dict[str, object],
) -> tuple[dict[str, object], bytes]:
    if args.review_approval is None:
        raise TaskFrontmatterError("Source-1376 review-approval path is missing.")
    loaded = read_private_json(args.review_approval, required=True)
    assert loaded is not None
    receipt, payload = loaded
    unsigned = review_approval_unsigned(args, prepared_path, prepared_digest, prepared_document)
    signature_value = receipt.get("signature")
    if set(receipt) != {*unsigned, "signature"} or any(receipt.get(key) != value for key, value in unsigned.items()) or not isinstance(signature_value, str):
        raise TaskFrontmatterError("Source-1376 review approval does not match the exact prepared binding.")
    try:
        signature = base64.b64decode(signature_value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TaskFrontmatterError("Source-1376 review approval signature encoding is invalid.") from exc
    verify_review_signature(canonical_json(unsigned), signature)
    return receipt, payload


def stable_owned_read(path: Path, *, modes: frozenset[int], label: str) -> tuple[bytes, os.stat_result]:
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    chunks: list[bytes] = []
    try:
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        opened = os.fstat(fd)
    finally:
        os.close(fd)
    after = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) not in modes or not same_file_state(before, opened) or not same_file_state(before, after):
        raise TaskFrontmatterError(f"{label} changed during its stable read or has unsafe custody.")
    return b"".join(chunks), before


def safe_read(path: Path, *, expected_sha256: str, mode: int, label: str) -> bytes:
    payload, _state = stable_owned_read(path, modes=frozenset({mode}), label=label)
    if sha256(payload) != expected_sha256:
        raise TaskFrontmatterError(f"{label} is unsafe or does not match its reviewed identity.")
    return payload


# 🧑 Human Source-1376: “tell those agents to document anything worth keeping long-term, move out their pending task items, then close them all”.
def load_plan(path: Path) -> dict[str, PlanRow]:
    if path.resolve() != PLAN_PATH:
        raise TaskFrontmatterError("Source-1376 plan path changed.")
    payload = safe_read(path, expected_sha256=PLAN_SHA256, mode=0o444, label="Source-1376 plan")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError("Source-1376 plan is not UTF-8.") from exc
    rows: dict[str, PlanRow] = {}
    for line in text.splitlines():
        if re.match(r"^[0-9]{2}\|", line) is None:
            continue
        fields = line.split("|")
        if len(fields) != 11:
            raise TaskFrontmatterError("Source-1376 plan table is malformed.")
        row_id, task_ref, status_value, role, runat, managerat, count, task_digest, queue_digest, transfer, flags = fields
        if (
            row_id in rows
            or role not in {"M", "W"}
            or status_value not in {"blocked", "long_running", "running"}
            or not count.isdecimal()
            or SHA256_RE.fullmatch(task_digest) is None
            or SHA256_RE.fullmatch(queue_digest) is None
        ):
            raise TaskFrontmatterError("Source-1376 plan row is invalid.")
        rows[row_id] = PlanRow(
            row_id,
            task_ref,
            status_value,
            role == "M",
            runat,
            managerat,
            int(count),
            task_digest,
            queue_digest,
            transfer,
            tuple(value for value in flags.split(",") if value != "-"),
        )
    if set(rows) != {f"{value:02d}" for value in range(1, 85)}:
        raise TaskFrontmatterError("Source-1376 plan must contain exactly rows 01 through 84.")
    scheduled: set[str] = set(EARLY_CLOSE_ROWS) | set(PRE_DEFERRED_CLOSE_ROWS)
    for entry in CLOSE_ORDER:
        scheduled.update(entry if isinstance(entry, tuple) else (entry,))
    if scheduled != set(rows) - PROTECTED_ROWS:
        raise TaskFrontmatterError("Source-1376 closure schedule does not cover every and only eligible row.")
    return rows


def load_execution_binding(path: Path) -> dict[str, object]:
    if path.resolve() != EXECUTION_BINDING_PATH:
        raise TaskFrontmatterError("Source-1376 execution-binding path changed.")
    payload = safe_read(
        path,
        expected_sha256=EXECUTION_BINDING_SHA256,
        mode=0o444,
        label="Source-1376 execution binding",
    )
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError("Source-1376 execution binding is malformed.") from exc
    if not isinstance(value, dict):
        raise TaskFrontmatterError("Source-1376 execution binding is not a JSON object.")
    base = value.get("base_plan")
    authority = value.get("authority")
    destination = value.get("initial_destination")
    verification = value.get("task_hash_verification")
    preserved = value.get("preserved_contract")
    supersedes = value.get("supersedes")
    if (
        value.get("schema") != "omo-source1376-execution-binding/v1"
        or value.get("created_at") != "2026-09-03T08:24:29Z"
        or value.get("purpose")
        != "Final supplemental immutable pre-execution binding after verified coordinator-only drift; the base plan remains authoritative for every mapping, order, source, blocker, ownership rule, and boundary not replaced below."
        or not isinstance(base, dict)
        or base.get("path") != str(PLAN_PATH)
        or base.get("sha256") != PLAN_SHA256
        or base.get("replacement_scope")
        != [
            "row 29 task byte digest and status",
            "initial destination byte digest, queue, and evidence comments",
        ]
        or not isinstance(authority, dict)
        or authority.get("path") != str(WORK_LOG_ROOT / AUTHORITY_REF)
        or authority.get("sha256") != AUTHORITY_SHA256
        or authority.get("instruction") != AUTHORITY_TEXT
        or not isinstance(destination, dict)
        or destination.get("path") != str(WORK_LOG_ROOT / DESTINATION_REF)
        or destination.get("sha256") != DESTINATION_INITIAL_SHA256
        or destination.get("pending_items") != [AUTHORITY_TEXT]
        or destination.get("status") != "long_running"
        or destination.get("target") != "cedit:15"
        or destination.get("unique_raw_target_owner") is not True
        or not isinstance(verification, dict)
        or verification.get("total_rows") != 84
        or verification.get("unchanged_count") != 83
        or verification.get("changed_rows")
        != [
            {
                "current_sha256": ROW29_CURRENT_SHA256,
                "current_status": "done",
                "disposition": "Protected external survivor; preserve its record and target disposition. Its completed lifecycle is not a Source-1376 transfer or closure mutation.",
                "is_manager": False,
                "manager_target": "hwl:3",
                "mode": "0644",
                "pending_items": [],
                "planned_sha256": "2aff849d7975700031f3b5d6d889c0f4ed67c902dd4e6c90ea422c15b2b566bb",
                "planned_status": "blocked",
                "queue_sha256": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
                "row": "29",
                "size": 1560,
                "target": "agent_managers:5",
                "task": "active_task_tree.md",
            }
        ]
        or not isinstance(preserved, dict)
        or not isinstance(preserved.get("protected_human_target"), dict)
        or preserved["protected_human_target"].get("sha256") != "c6d74ffbfbdf3aec67bf19dfc9e71d451a9894d843a5336dd2f21abc8bb81aa5"
        or not isinstance(preserved.get("root_last"), dict)
        or preserved["root_last"].get("sha256") != "8ef839e6c9c22051eaeb7cf3010b7646add4760ed9240aa449b8c6a4b810efea"
        or supersedes
        != {
            "path": str(SUPERSEDED_EXECUTION_BINDING_PATH),
            "mode": "0444",
            "size": 7584,
            "sha256": SUPERSEDED_EXECUTION_BINDING_SHA256,
            "initial_destination_sha256": "ef5b8d60ca2c9039b2f335e6aaf957b9271304d8f547d36b2229c826337f7e25",
        }
        or value.get("execution_rule")
        != "Treat this file and the immutable base plan as one fail-closed contract. Substitute only the row-29 current binding and initial-destination binding above. Abort on any later mismatch; never replace another digest merely to bypass drift."
        or value.get("remaining_blocker")
        != "Exact authoritative Human permission naming closure of hamh:1 has not been established by this binding, so row 84 and root row 69 remain protected and open. This does not prevent eligible non-human execution under the base plan."
    ):
        raise TaskFrontmatterError("Source-1376 execution binding does not preserve the reviewed replacement scope.")
    return value


def load_execution_rows(plan: Path, binding: Path) -> dict[str, PlanRow]:
    rows = load_plan(plan)
    _ = load_execution_binding(binding)
    row29 = rows["29"]
    rows["29"] = replace(row29, status="done", task_sha256=ROW29_CURRENT_SHA256)
    return rows


def prepared_binding_digest(args: Args) -> str:
    return args.prepared_binding_sha256


def prepared_binding_value(args: Args) -> dict[str, object] | None:
    path = args.prepared_binding
    if path is None:
        if args.prepared_binding_sha256 or args.prepare_binding or args.reviewed_handoff:
            raise TaskFrontmatterError("Source-1376 prepared-binding arguments are incomplete.")
        return None
    if args.prepare_binding or args.reviewed_handoff:
        raise TaskFrontmatterError("a Source-1376 binding being prepared cannot be used for execution.")
    if SHA256_RE.fullmatch(args.prepared_binding_sha256) is None:
        raise TaskFrontmatterError("Source-1376 prepared-binding digest is invalid.")
    value, payload = read_immutable_json(path)
    if sha256(payload) != args.prepared_binding_sha256:
        raise TaskFrontmatterError("Source-1376 prepared binding does not match its independently reviewed digest.")
    validate_prepared_binding_document(args, value)
    validate_review_approval(args, path, args.prepared_binding_sha256, value)
    return value


def prepared_binding_rows(value: dict[str, object], prior_rows: dict[str, PlanRow]) -> dict[str, PlanRow]:
    row_values = value.get("rows")
    if not isinstance(row_values, list) or len(row_values) != len(prior_rows):
        raise TaskFrontmatterError("Source-1376 prepared binding has an invalid row manifest.")
    effective: dict[str, PlanRow] = {}
    for expected_id, row_value in zip(sorted(prior_rows), row_values, strict=True):
        if not isinstance(row_value, dict):
            raise TaskFrontmatterError("Source-1376 prepared binding row is malformed.")
        prior = prior_rows[expected_id]
        pending_items = row_value.get("pending_items")
        current_sha256 = row_value.get("current_sha256")
        if (
            set(row_value)
            != {
                "row",
                "task",
                "planned_sha256",
                "prior_bound_sha256",
                "current_sha256",
                "drifted_after_prior_binding",
                "status",
                "blocked_on",
                "target",
                "manager_target",
                "is_manager",
                "pending_items",
                "pending_items_count",
                "queue_sha256",
                "mode",
                "size",
                "protected",
                "disposition",
            }
            or row_value.get("row") != expected_id
            or row_value.get("task") != prior.task_ref
            or row_value.get("prior_bound_sha256") != prior.task_sha256
            or not isinstance(current_sha256, str)
            or SHA256_RE.fullmatch(current_sha256) is None
            or row_value.get("drifted_after_prior_binding") is not (current_sha256 != prior.task_sha256)
            or row_value.get("status") != prior.status
            or not isinstance(row_value.get("blocked_on"), str)
            or row_value.get("target") != prior.runat
            or row_value.get("manager_target") != prior.managerat
            or row_value.get("is_manager") is not prior.is_manager
            or not isinstance(pending_items, list)
            or any(not isinstance(item, str) for item in pending_items)
            or row_value.get("pending_items_count") != prior.n_items
            or row_value.get("queue_sha256") != prior.queue_sha256
            or len(pending_items) != prior.n_items
            or queue_sha256(tuple(pending_items)) != prior.queue_sha256
            or not isinstance(row_value.get("mode"), str)
            or not isinstance(row_value.get("size"), int)
            or row_value.get("protected") is not (expected_id in PROTECTED_ROWS)
            or not isinstance(row_value.get("disposition"), str)
        ):
            raise TaskFrontmatterError(f"Source-1376 prepared binding row {expected_id} breaks prior plan semantics.")
        effective[expected_id] = replace(prior, task_sha256=current_sha256)
    return effective


def prepared_binding_inventory(value: dict[str, object], root: Path) -> dict[Path, dict[str, object]]:
    manifest = value.get("markdown_inventory")
    if not isinstance(manifest, list) or not manifest:
        raise TaskFrontmatterError("Source-1376 prepared binding has no Markdown inventory.")
    result: dict[Path, dict[str, object]] = {}
    for entry in manifest:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"task", "sha256", "mode", "size"}
            or not isinstance(entry.get("task"), str)
            or not isinstance(entry.get("sha256"), str)
            or SHA256_RE.fullmatch(str(entry["sha256"])) is None
            or not isinstance(entry.get("mode"), str)
            or not isinstance(entry.get("size"), int)
        ):
            raise TaskFrontmatterError("Source-1376 prepared binding Markdown inventory is malformed.")
        path = (root / str(entry["task"])).resolve()
        if path == root or root not in path.parents or path in result:
            raise TaskFrontmatterError("Source-1376 prepared binding Markdown path is invalid or duplicated.")
        result[path] = entry
    if tuple(result) != tuple(sorted(result)):
        raise TaskFrontmatterError("Source-1376 prepared binding Markdown inventory is not canonical.")
    return result


def validate_prepared_binding_document(args: Args, value: dict[str, object]) -> None:
    prior_rows = load_execution_rows(args.plan, args.binding)
    effective_rows = prepared_binding_rows(value, prior_rows)
    plan_value = value.get("base_plan")
    prior_value = value.get("prior_execution_binding")
    authority_value = value.get("authority")
    receipt_value = value.get("receipt_directory")
    destination = value.get("destination")
    todo = value.get("todo")
    implementation = value.get("implementation")
    gate = value.get("review_gate")
    protected = value.get("protected")
    boundaries = value.get("boundaries")
    source1352 = value.get("source1352")
    inventory = prepared_binding_inventory(value, args.root)
    prepared_schedule = transfer_schedule(effective_rows, include_deferred=False)
    if not prepared_schedule:
        raise TaskFrontmatterError("Source-1376 prepared binding has no eligible first transfer.")
    expected_first = list(prepared_schedule[0])
    if (
        set(value)
        != {
            "schema",
            "created_at",
            "purpose",
            "root",
            "base_plan",
            "prior_execution_binding",
            "authority",
            "implementation",
            "review_gate",
            "receipt_directory",
            "packet_output",
            "todo",
            "markdown_inventory",
            "rows",
            "drifts",
            "destination",
            "first_transfer_rows",
            "protected",
            "source1352",
            "boundaries",
            "recovery_records_absent",
            "execution_rule",
        }
        or value.get("schema") != PREPARED_BINDING_SCHEMA
        or not isinstance(value.get("created_at"), str)
        or value.get("purpose") != PREPARED_BINDING_PURPOSE
        or value.get("root") != str(args.root)
        or plan_value != {"path": str(args.plan), "mode": "0444", "sha256": PLAN_SHA256}
        or prior_value != {"path": str(args.binding), "mode": "0444", "sha256": EXECUTION_BINDING_SHA256}
        or authority_value
        != {
            "path": str(args.authority),
            "source": f"{AUTHORITY_REF}:3-3",
            "sha256": AUTHORITY_SHA256,
            "text": AUTHORITY_TEXT,
        }
        or not isinstance(implementation, dict)
        or set(implementation) != {"path", "sha256", "mode", "size"}
        or implementation.get("path") != str(Path(__file__).resolve())
        or SHA256_RE.fullmatch(str(implementation.get("sha256", ""))) is None
        or not isinstance(implementation.get("mode"), str)
        or not isinstance(implementation.get("size"), int)
        or not isinstance(gate, dict)
        or gate != review_gate(args, str(gate.get("challenge", "")))
        or not isinstance(receipt_value, dict)
        or set(receipt_value) != {"path", "mode", "uid", "device", "inode", "initial_entries", "normalization_contract"}
        or receipt_value.get("path") != str(args.receipt_dir)
        or receipt_value.get("mode") != "0700"
        or receipt_value.get("uid") != os.getuid()
        or not isinstance(receipt_value.get("device"), int)
        or not isinstance(receipt_value.get("inode"), int)
        or receipt_value.get("initial_entries") != []
        or receipt_value.get("normalization_contract") != RECEIPT_DIRECTORY_MODE_CONTRACT
        or value.get("packet_output") != str(args.packet_output)
        or not isinstance(todo, dict)
        or set(todo) != {"path", "sha256", "mode", "size"}
        or todo.get("path") != str((args.root / "TODO.md").resolve())
        or SHA256_RE.fullmatch(str(todo.get("sha256", ""))) is None
        or not isinstance(todo.get("mode"), str)
        or not isinstance(todo.get("size"), int)
        or value.get("first_transfer_rows") != expected_first
        or not isinstance(destination, dict)
        or set(destination)
        != {
            "task",
            "path",
            "prior_bound_sha256",
            "current_sha256",
            "drifted_after_prior_binding",
            "status",
            "blocked_on",
            "target",
            "manager_target",
            "is_manager",
            "pending_items",
            "pending_items_count",
            "queue_sha256",
            "mode",
            "size",
            "unique_active_target_owner",
            "disposition",
        }
        or destination.get("task") != DESTINATION_REF
        or destination.get("path") != str((args.root / DESTINATION_REF).resolve())
        or destination.get("prior_bound_sha256") != DESTINATION_INITIAL_SHA256
        or destination.get("target") != "cedit:15"
        or destination.get("manager_target") != "wl:4"
        or destination.get("status") != "long_running"
        or destination.get("is_manager") is not True
        or destination.get("pending_items") != [AUTHORITY_TEXT]
        or destination.get("pending_items_count") != 1
        or destination.get("queue_sha256") != queue_sha256((AUTHORITY_TEXT,))
        or SHA256_RE.fullmatch(str(destination.get("prior_bound_sha256", ""))) is None
        or SHA256_RE.fullmatch(str(destination.get("current_sha256", ""))) is None
        or destination.get("drifted_after_prior_binding") is not (destination.get("current_sha256") != destination.get("prior_bound_sha256"))
        or destination.get("unique_active_target_owner") is not True
        or not isinstance(destination.get("blocked_on"), str)
        or not isinstance(destination.get("mode"), str)
        or not isinstance(destination.get("size"), int)
        or not isinstance(destination.get("disposition"), str)
        or protected
        != {
            "rows": sorted(PROTECTED_ROWS),
            "human_target": "hamh:1",
            "human_rule": "Preserve row 84 and do not mutate any human-owned session without exact authoritative Human text naming that action and session.",
            "root_target": "amh:1",
            "root_rule": "Preserve row 69 and leave the root open while the protected hamh:1 dependency lacks exact authoritative Human resolution.",
        }
        or source1352 != source1352_anchor()
        or boundaries
        != {
            "human_contact": "forbidden",
            "human_owned_sessions": "no mutation",
            "mailbox": "no access or mutation",
            "pcodx": "unused",
            "production": "no access or mutation",
            "source1352": "preserve accepted receipt and do not resend",
        }
        or value.get("recovery_records_absent") != [TRANSFER_JOURNAL, LEGACY_TRANSFER_JOURNAL]
        or value.get("execution_rule") != PREPARED_BINDING_EXECUTION_RULE
    ):
        raise TaskFrontmatterError("Source-1376 prepared binding breaks its reviewed frozen-snapshot contract.")
    implementation_path = Path(str(implementation["path"])).resolve()
    implementation_payload, implementation_state = stable_owned_read(
        implementation_path,
        modes=frozenset({int(str(implementation["mode"]), 8)}),
        label="Source-1376 prepared implementation",
    )
    if len(implementation_payload) != implementation["size"] or sha256(implementation_payload) != implementation["sha256"]:
        raise TaskFrontmatterError("Source-1376 prepared implementation bytes changed.")
    if implementation_state.st_uid != os.getuid():
        raise TaskFrontmatterError("Source-1376 prepared implementation custody changed.")
    planned_rows = load_plan(args.plan)
    row_manifest = value["rows"]
    assert isinstance(row_manifest, list)
    drift_values = [
        {
            "kind": "row",
            "row": str(entry["row"]),
            "task": str(entry["task"]),
            "planned_sha256": planned_rows[str(entry["row"])].task_sha256,
            "prior_bound_sha256": str(entry["prior_bound_sha256"]),
            "current_sha256": str(entry["current_sha256"]),
            "disposition": str(entry["disposition"]),
        }
        for entry in row_manifest
        if isinstance(entry, dict) and entry.get("drifted_after_prior_binding") is True
    ]
    if destination["drifted_after_prior_binding"] is True:
        drift_values.append(
            {
                "kind": "destination",
                "task": DESTINATION_REF,
                "prior_bound_sha256": str(destination["prior_bound_sha256"]),
                "current_sha256": str(destination["current_sha256"]),
                "disposition": str(destination.get("disposition", "")),
            }
        )
    if value.get("drifts") != drift_values:
        raise TaskFrontmatterError("Source-1376 prepared binding drift manifest does not rederive.")
    planned_sha_by_row = {row_id: row.task_sha256 for row_id, row in planned_rows.items()}
    required_inventory = {
        *(plan_path(args.root, row) for row in prior_rows.values()),
        (args.root / DESTINATION_REF).resolve(),
        (args.root / "TODO.md").resolve(),
    }
    if not required_inventory.issubset(inventory):
        raise TaskFrontmatterError("Source-1376 prepared inventory omits required task custody.")
    for entry in row_manifest:
        assert isinstance(entry, dict)
        if entry.get("planned_sha256") != planned_sha_by_row[str(entry["row"])]:
            raise TaskFrontmatterError("Source-1376 prepared binding rewrites a stale base-plan digest.")
        inventory_entry = inventory[plan_path(args.root, prior_rows[str(entry["row"])])]
        if entry.get("current_sha256") != inventory_entry["sha256"] or entry.get("mode") != inventory_entry["mode"] or entry.get("size") != inventory_entry["size"]:
            raise TaskFrontmatterError("Source-1376 prepared row does not match the frozen root inventory.")
    destination_inventory = inventory[(args.root / DESTINATION_REF).resolve()]
    todo_inventory = inventory[(args.root / "TODO.md").resolve()]
    if (
        destination["current_sha256"] != destination_inventory["sha256"]
        or destination["mode"] != destination_inventory["mode"]
        or destination["size"] != destination_inventory["size"]
        or todo["sha256"] != todo_inventory["sha256"]
        or todo["mode"] != todo_inventory["mode"]
        or todo["size"] != todo_inventory["size"]
    ):
        raise TaskFrontmatterError("Source-1376 prepared destination or TODO does not match the frozen inventory.")
    if set(inventory) != set(markdown_paths(args.root)):
        raise TaskFrontmatterError("Source-1376 prepared binding inventory no longer names the current root inventory.")


def effective_execution_rows(args: Args) -> dict[str, PlanRow]:
    prior = load_execution_rows(args.plan, args.binding)
    value = prepared_binding_value(args)
    return prior if value is None else prepared_binding_rows(value, prior)


def initial_destination_sha256(args: Args) -> str:
    value = prepared_binding_value(args)
    if value is None:
        return DESTINATION_INITIAL_SHA256
    destination = value.get("destination")
    if not isinstance(destination, dict) or SHA256_RE.fullmatch(str(destination.get("current_sha256", ""))) is None:
        raise TaskFrontmatterError("Source-1376 prepared destination digest is invalid.")
    return str(destination["current_sha256"])


def snapshot_manifest_entry(root: Path, path: Path, state: os.stat_result, payload: bytes) -> dict[str, object]:
    if not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid():
        raise TaskFrontmatterError(f"Source-1376 snapshot path has unsafe custody: {path}")
    return {
        "task": relative_task_ref(root, path),
        "sha256": sha256(payload),
        "mode": f"{stat.S_IMODE(state.st_mode):04o}",
        "size": len(payload),
    }


def prepared_binding_lock_paths(args: Args, rows: dict[str, PlanRow], markdown: tuple[Path, ...]) -> tuple[Path, ...]:
    if args.prepared_binding is None or args.review_approval is None:
        raise TaskFrontmatterError("Source-1376 prepared-binding or review-approval path is missing.")
    schedule = transfer_schedule(rows, include_deferred=False)
    if not schedule:
        raise TaskFrontmatterError("Source-1376 prepared handoff has no eligible first transfer.")
    first_receipt = transfer_receipt_path(args.receipt_dir, schedule[0])
    return tuple(
        sorted(
            {
                *markdown,
                args.root / "TODO.md",
                args.root / TRANSFER_JOURNAL,
                args.root / LEGACY_TRANSFER_JOURNAL,
                args.plan,
                args.binding,
                args.authority,
                args.receipt_dir,
                first_receipt,
                args.packet_output,
                args.prepared_binding,
                args.review_approval,
                Path(__file__).resolve(),
                *(plan_path(args.root, row) for row in rows.values()),
            }
        )
    )


def receipt_directory_binding(path: Path) -> dict[str, object]:
    before = path.lstat()
    entries = sorted(candidate.name for candidate in path.iterdir())
    after = path.lstat()
    if not same_file_state(before, after) or not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o700:
        raise TaskFrontmatterError("Source-1376 receipt directory has unsafe custody.")
    return {
        "path": str(path),
        "mode": "0700",
        "uid": before.st_uid,
        "device": before.st_dev,
        "inode": before.st_ino,
        "initial_entries": entries,
        "normalization_contract": RECEIPT_DIRECTORY_MODE_CONTRACT,
    }


def validate_empty_receipt_directory(args: Args, expected: dict[str, object]) -> None:
    actual = receipt_directory_binding(args.receipt_dir)
    if actual != expected or actual["initial_entries"] != []:
        raise TaskFrontmatterError("Source-1376 receipt directory changed after the reviewed snapshot.")


def wait_for_review_approval(
    args: Args,
    path: Path,
    digest: str,
    document: dict[str, object],
) -> str:
    """Verify and preserve one reviewer-signed approval while the caller retains every lock."""

    if args.review_approval is None:
        raise TaskFrontmatterError("Source-1376 review-approval output is missing.")
    unsigned = review_approval_unsigned(args, path, digest, document)
    signed_payload = canonical_json(unsigned)
    encoded_payload = base64.b64encode(signed_payload).decode("ascii")
    print(f"prepared binding ready under locks: {path}", flush=True)
    print(f"prepared binding sha256: {digest}", flush=True)
    print(f"review approval unsigned payload base64: {encoded_payload}", flush=True)
    print("awaiting reviewer Ed25519 signature on stdin: SIGNATURE <base64>", flush=True)
    approval = sys.stdin.readline()
    prefix = "SIGNATURE "
    if not approval.startswith(prefix) or not approval.endswith("\n"):
        raise TaskFrontmatterError("Source-1376 locked handoff did not receive a reviewer signature.")
    signature_value = approval[len(prefix) : -1]
    try:
        signature = base64.b64decode(signature_value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TaskFrontmatterError("Source-1376 review approval signature encoding is invalid.") from exc
    verify_review_signature(signed_payload, signature)
    receipt = {**unsigned, "signature": signature_value}
    payload = write_private_json(args.review_approval, receipt, final=True)
    validated, validated_payload = validate_review_approval(args, path, digest, document)
    if validated != receipt or validated_payload != payload:
        raise TaskFrontmatterError("Source-1376 immutable review approval changed after publication.")
    return sha256(payload)


def publish_prepared_binding(args: Args) -> tuple[Path, str]:
    if args.prepared_binding is None or args.review_approval is None or args.prepare_binding is args.reviewed_handoff or args.prepared_binding_sha256:
        raise TaskFrontmatterError("Source-1376 preparation requires exactly one unbound prepare or reviewed-handoff mode.")
    output = args.prepared_binding.resolve()
    approval_output = args.review_approval.resolve()
    packet_output = args.packet_output.resolve()
    if (
        output.parent == args.root
        or args.root in output.parents
        or approval_output.parent == args.root
        or args.root in approval_output.parents
        or packet_output.parent == args.root
        or args.root in packet_output.parents
        or output == approval_output
        or output == packet_output
        or approval_output == packet_output
        or not output.parent.is_dir()
        or not approval_output.parent.is_dir()
        or not packet_output.parent.is_dir()
    ):
        raise TaskFrontmatterError("Source-1376 binding, review approval, and packet must be distinct external paths in existing directories.")
    prior_rows = load_execution_rows(args.plan, args.binding)
    with root_membership_lock(args.root):
        initial_paths = markdown_paths(args.root)
        with ExitStack() as locks:
            locked_paths = prepared_binding_lock_paths(args, prior_rows, initial_paths)
            for locked_path in locked_paths:
                locks.enter_context(task_file_lock(locked_path))
            held_locks = _HeldPreparedLocks(_HELD_PREPARED_LOCKS_KEY, args.root, initial_paths, locked_paths)
            locks.callback(held_locks.invalidate)
            if markdown_paths(args.root) != initial_paths:
                raise TaskFrontmatterError("Source-1376 task inventory changed while the prepared binding was locked.")
            if path_entry_exists(output):
                raise TaskFrontmatterError("Source-1376 prepared-binding output already exists.")
            if path_entry_exists(approval_output):
                raise TaskFrontmatterError("Source-1376 review-approval output already exists.")
            if path_entry_exists(args.packet_output):
                raise TaskFrontmatterError("Source-1376 execution packet exists before the prepared handoff.")
            if path_entry_exists(args.root / TRANSFER_JOURNAL):
                raise TaskFrontmatterError("Source-1376 transfer journal exists before the prepared handoff.")
            if path_entry_exists(args.root / LEGACY_TRANSFER_JOURNAL):
                raise TaskFrontmatterError("legacy closure-transfer recovery state exists before the prepared handoff.")
            receipt_binding = receipt_directory_binding(args.receipt_dir)
            if receipt_binding["initial_entries"] != []:
                raise TaskFrontmatterError("Source-1376 prepared handoff requires a new empty receipt directory.")
            if validate_authority(args.root, args.authority) != f"{AUTHORITY_REF}:3-3":
                raise TaskFrontmatterError("Source-1376 authority locator drifted during snapshot preparation.")
            if load_execution_rows(args.plan, args.binding) != prior_rows:
                raise TaskFrontmatterError("Source-1376 base plan or prior execution binding changed during snapshot preparation.")
            snapshots = {path: task_snapshot(path) for path in initial_paths}
            payloads = {path: snapshot[1] for path, snapshot in snapshots.items()}
            planned_rows = load_plan(args.plan)
            planned_paths = {plan_path(args.root, row) for row in prior_rows.values()}
            destination_path = (args.root / DESTINATION_REF).resolve()
            todo_path = (args.root / "TODO.md").resolve()
            if not planned_paths.issubset(payloads) or destination_path not in payloads or todo_path not in payloads:
                raise TaskFrontmatterError("Source-1376 prepared snapshot is missing planned task custody.")
            row_manifest: list[dict[str, object]] = []
            for row_id in sorted(prior_rows):
                prior = prior_rows[row_id]
                task = plan_path(args.root, prior)
                state, payload = snapshots[task]
                text = payload.decode("utf-8")
                metadata = require_v1_metadata(text)
                validate_plan_metadata(prior, metadata, require_original_status=True)
                if len(metadata.pending_task_items) != prior.n_items or queue_sha256(metadata.pending_task_items) != prior.queue_sha256:
                    raise TaskFrontmatterError(f"plan row {row_id} queue semantics drifted and cannot be rebound.")
                current_digest = sha256(payload)
                drifted = current_digest != prior.task_sha256
                if row_id in PROTECTED_ROWS:
                    disposition = "Preserve the exact current bytes and lifecycle state; this protected row is outside every transfer and closure mutation."
                elif drifted:
                    disposition = (
                        "Bind the exact current complete bytes while retaining the immutable base-plan mapping, order, queue, ownership, and current blocker; "
                        "preserve all appended evidence through transfer and closure."
                    )
                else:
                    disposition = "The prior execution-binding byte preimage remains exact."
                row_manifest.append(
                    {
                        "row": row_id,
                        "task": prior.task_ref,
                        "planned_sha256": planned_rows[row_id].task_sha256,
                        "prior_bound_sha256": prior.task_sha256,
                        "current_sha256": current_digest,
                        "drifted_after_prior_binding": drifted,
                        "status": metadata.status,
                        "blocked_on": metadata.blocked_on,
                        "target": metadata.runat,
                        "manager_target": metadata.managerat,
                        "is_manager": metadata.is_manager,
                        "pending_items": list(metadata.pending_task_items),
                        "pending_items_count": len(metadata.pending_task_items),
                        "queue_sha256": queue_sha256(metadata.pending_task_items),
                        "mode": f"{stat.S_IMODE(state.st_mode):04o}",
                        "size": len(payload),
                        "protected": row_id in PROTECTED_ROWS,
                        "disposition": disposition,
                    }
                )
            destination_state, destination_payload = snapshots[destination_path]
            destination_metadata = require_v1_metadata(destination_payload.decode("utf-8"))
            parsed, malformed = active_metadata(args.root, initial_paths)
            destination_owners = tuple(path for path, metadata in parsed.items() if metadata.runat == "cedit:15")
            if (
                destination_metadata.status != "long_running"
                or not destination_metadata.is_manager
                or destination_metadata.runat != "cedit:15"
                or destination_metadata.managerat != "wl:4"
                or destination_metadata.pending_task_items != (AUTHORITY_TEXT,)
                or destination_owners != (destination_path,)
                or any(raw_target_claims(text, "cedit:15") for text in malformed.values())
                or task_row_sections(args.root, destination_path) != ("current",)
            ):
                raise TaskFrontmatterError("Source-1376 prepared destination does not retain singular escrow custody.")
            destination_digest = sha256(destination_payload)
            destination: dict[str, object] = {
                "task": DESTINATION_REF,
                "path": str(destination_path),
                "prior_bound_sha256": DESTINATION_INITIAL_SHA256,
                "current_sha256": destination_digest,
                "drifted_after_prior_binding": destination_digest != DESTINATION_INITIAL_SHA256,
                "status": destination_metadata.status,
                "blocked_on": destination_metadata.blocked_on,
                "target": destination_metadata.runat,
                "manager_target": destination_metadata.managerat,
                "is_manager": destination_metadata.is_manager,
                "pending_items": list(destination_metadata.pending_task_items),
                "pending_items_count": len(destination_metadata.pending_task_items),
                "queue_sha256": queue_sha256(destination_metadata.pending_task_items),
                "mode": f"{stat.S_IMODE(destination_state.st_mode):04o}",
                "size": len(destination_payload),
                "unique_active_target_owner": True,
                "disposition": "Bind and preserve the exact current evidence-bearing escrow bytes as the first transfer destination preimage.",
            }
            drifts: list[dict[str, object]] = [
                {
                    "kind": "row",
                    "row": str(entry["row"]),
                    "task": str(entry["task"]),
                    "planned_sha256": str(entry["planned_sha256"]),
                    "prior_bound_sha256": str(entry["prior_bound_sha256"]),
                    "current_sha256": str(entry["current_sha256"]),
                    "disposition": str(entry["disposition"]),
                }
                for entry in row_manifest
                if entry["drifted_after_prior_binding"] is True
            ]
            if destination["drifted_after_prior_binding"] is True:
                drifts.append(
                    {
                        "kind": "destination",
                        "task": DESTINATION_REF,
                        "prior_bound_sha256": DESTINATION_INITIAL_SHA256,
                        "current_sha256": destination_digest,
                        "disposition": str(destination["disposition"]),
                    }
                )
            implementation_path = Path(__file__).resolve()
            implementation_payload, implementation_state = stable_owned_read(
                implementation_path,
                modes=frozenset({stat.S_IMODE(implementation_path.lstat().st_mode)}),
                label="Source-1376 prepared implementation",
            )
            challenge = secrets.token_hex(32)
            effective_rows = {row_id: replace(prior_rows[row_id], task_sha256=str(row_manifest[int(row_id) - 1]["current_sha256"])) for row_id in prior_rows}
            first_schedule = transfer_schedule(effective_rows, include_deferred=False)
            if not first_schedule:
                raise TaskFrontmatterError("Source-1376 prepared snapshot has no eligible first transfer.")
            todo_state, todo_payload = snapshots[todo_path]
            manifest = [snapshot_manifest_entry(args.root, path, *snapshots[path]) for path in initial_paths]
            document: dict[str, object] = {
                "schema": PREPARED_BINDING_SCHEMA,
                "created_at": datetime.now().astimezone().isoformat(),
                "purpose": PREPARED_BINDING_PURPOSE,
                "root": str(args.root),
                "base_plan": {"path": str(args.plan), "mode": "0444", "sha256": PLAN_SHA256},
                "prior_execution_binding": {"path": str(args.binding), "mode": "0444", "sha256": EXECUTION_BINDING_SHA256},
                "authority": {
                    "path": str(args.authority),
                    "source": f"{AUTHORITY_REF}:3-3",
                    "sha256": AUTHORITY_SHA256,
                    "text": AUTHORITY_TEXT,
                },
                "implementation": {
                    "path": str(implementation_path),
                    "sha256": sha256(implementation_payload),
                    "mode": f"{stat.S_IMODE(implementation_state.st_mode):04o}",
                    "size": len(implementation_payload),
                },
                "review_gate": review_gate(args, challenge),
                "receipt_directory": receipt_binding,
                "packet_output": str(args.packet_output),
                "todo": {
                    "path": str(todo_path),
                    "sha256": sha256(todo_payload),
                    "mode": f"{stat.S_IMODE(todo_state.st_mode):04o}",
                    "size": len(todo_payload),
                },
                "markdown_inventory": manifest,
                "rows": row_manifest,
                "drifts": drifts,
                "destination": destination,
                "first_transfer_rows": list(first_schedule[0]),
                "protected": {
                    "rows": sorted(PROTECTED_ROWS),
                    "human_target": "hamh:1",
                    "human_rule": "Preserve row 84 and do not mutate any human-owned session without exact authoritative Human text naming that action and session.",
                    "root_target": "amh:1",
                    "root_rule": "Preserve row 69 and leave the root open while the protected hamh:1 dependency lacks exact authoritative Human resolution.",
                },
                "source1352": source1352_anchor(),
                "boundaries": {
                    "human_contact": "forbidden",
                    "human_owned_sessions": "no mutation",
                    "mailbox": "no access or mutation",
                    "pcodx": "unused",
                    "production": "no access or mutation",
                    "source1352": "preserve accepted receipt and do not resend",
                },
                "recovery_records_absent": [TRANSFER_JOURNAL, LEGACY_TRANSFER_JOURNAL],
                "execution_rule": PREPARED_BINDING_EXECUTION_RULE,
            }
            payload = write_private_json(output, document, final=True)
            os.chmod(output, 0o444)
            output_fd = os.open(output, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(output_fd)
            finally:
                os.close(output_fd)
            digest = sha256(payload)
            if args.reviewed_handoff:
                approval_digest = wait_for_review_approval(args, output, digest, document)
                execution_args = replace(
                    args,
                    prepared_binding_sha256=digest,
                    prepare_binding=False,
                    reviewed_handoff=False,
                )
                execution_rows = effective_execution_rows(execution_args)
                if execution_rows != effective_rows:
                    raise TaskFrontmatterError("Source-1376 reviewed handoff rows changed after publication.")
                first_group = first_schedule[0]
                receipt = transfer_rows(
                    execution_args,
                    execution_rows,
                    first_group,
                    initial_destination_sha256(execution_args),
                    require_prepared_snapshot=True,
                    _held_prepared_locks=held_locks,
                )
                if receipt.get("prepared_binding_sha256") != digest:
                    raise TaskFrontmatterError("Source-1376 first transfer did not retain the approved binding digest.")
                print(f"review approval receipt sha256: {approval_digest}", flush=True)
                print(f"committed reviewed first handoff row(s) {','.join(first_group)}", flush=True)
            return output, digest


def validate_prepared_snapshot_locked(
    args: Args,
    rows: dict[str, PlanRow],
    initial_paths: tuple[Path, ...],
) -> None:
    value = prepared_binding_value(args)
    if value is None or prepared_binding_rows(value, load_execution_rows(args.plan, args.binding)) != rows:
        raise TaskFrontmatterError("Source-1376 first transfer is not bound to the reviewed prepared rows.")
    inventory = prepared_binding_inventory(value, args.root)
    if set(initial_paths) != set(inventory) or markdown_paths(args.root) != initial_paths:
        raise TaskFrontmatterError("Source-1376 root inventory drifted after prepared-binding review.")
    for path in initial_paths:
        state, payload = task_snapshot(path)
        expected = inventory[path]
        if sha256(payload) != expected["sha256"] or len(payload) != expected["size"] or f"{stat.S_IMODE(state.st_mode):04o}" != expected["mode"] or state.st_uid != os.getuid():
            raise TaskFrontmatterError(f"Source-1376 prepared snapshot drifted before first transfer: {relative_task_ref(args.root, path)}")
    receipt = value.get("receipt_directory")
    if not isinstance(receipt, dict):
        raise TaskFrontmatterError("Source-1376 prepared receipt-directory binding is malformed.")
    validate_empty_receipt_directory(args, receipt)
    if path_entry_exists(args.packet_output) or path_entry_exists(args.root / TRANSFER_JOURNAL) or path_entry_exists(args.root / LEGACY_TRANSFER_JOURNAL):
        raise TaskFrontmatterError("Source-1376 transaction state appeared after prepared-binding review.")
    destination = value.get("destination")
    if not isinstance(destination, dict) or sha256(task_bytes(args.root / DESTINATION_REF)) != destination.get("current_sha256"):
        raise TaskFrontmatterError("Source-1376 prepared destination drifted before first transfer.")


def validate_authority(root: Path, authority: Path) -> str:
    if authority.resolve() != (root / AUTHORITY_REF).resolve():
        raise TaskFrontmatterError("Source-1376 authority path changed.")
    payload, locator = validate_closure_authority(root, authority.resolve(), AUTHORITY_SHA256)
    if sha256(payload) != AUTHORITY_SHA256 or AUTHORITY_TEXT.encode() not in payload:
        raise TaskFrontmatterError("Source-1376 authority bytes changed.")
    return locator


def plan_path(root: Path, row: PlanRow) -> Path:
    path = (root / row.task_ref).resolve()
    if path == root or root not in path.parents:
        raise TaskFrontmatterError(f"plan row {row.row_id} escapes the task root.")
    return path


def operation_binding_paths(args: Args, rows: dict[str, PlanRow]) -> tuple[Path, ...]:
    return tuple(
        sorted(
            {
                args.plan.resolve(),
                args.binding.resolve(),
                args.authority.resolve(),
                Path(__file__).resolve(),
                *(() if args.prepared_binding is None else (args.prepared_binding.resolve(),)),
                *(() if args.review_approval is None else (args.review_approval.resolve(),)),
                *(plan_path(args.root, rows[row_id]) for row_id in PROTECTED_ROWS if row_id in rows),
            }
        )
    )


def validate_operation_bindings_locked(args: Args, rows: dict[str, PlanRow]) -> None:
    if effective_execution_rows(args) != rows or validate_authority(args.root, args.authority) != f"{AUTHORITY_REF}:3-3":
        raise TaskFrontmatterError("Source-1376 immutable plan or authority binding changed.")
    for row_id in PROTECTED_ROWS:
        if row_id in rows:
            row = rows[row_id]
            _ = validate_original_row(args.root, row, task_bytes(plan_path(args.root, row)))


def validate_operation_bindings(args: Args, rows: dict[str, PlanRow]) -> None:
    with root_membership_lock(args.root):
        with ExitStack() as locks:
            for path in operation_binding_paths(args, rows):
                locks.enter_context(task_file_lock(path))
            validate_operation_bindings_locked(args, rows)


def validate_plan_metadata(row: PlanRow, metadata: TaskMetadata, *, require_original_status: bool) -> None:
    expected_status = row.status if require_original_status else metadata.status
    if metadata.status != expected_status or metadata.runat != row.runat or metadata.managerat != row.managerat or metadata.is_manager != row.is_manager:
        raise TaskFrontmatterError(f"plan row {row.row_id} task frontmatter drifted.")


def validate_original_row(root: Path, row: PlanRow, payload: bytes) -> tuple[str, TaskMetadata]:
    if sha256(payload) != row.task_sha256:
        raise TaskFrontmatterError(f"plan row {row.row_id} task bytes drifted.")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TaskFrontmatterError(f"plan row {row.row_id} task is not UTF-8.") from exc
    metadata = require_v1_metadata(text)
    validate_plan_metadata(row, metadata, require_original_status=True)
    if len(metadata.pending_task_items) != row.n_items or queue_sha256(metadata.pending_task_items) != row.queue_sha256:
        raise TaskFrontmatterError(f"plan row {row.row_id} queue drifted.")
    return text, metadata


def ensure_private_dir(path: Path) -> Path:
    path = path.resolve()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, directory_flags)
    created_fd = -1
    try:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        else:
            created_fd = os.open(path.name, directory_flags, dir_fd=parent_fd)
            created = os.fstat(created_fd)
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(created.st_mode) or created.st_uid != os.getuid() or stat.S_IMODE(created.st_mode) != 0o700:
                raise TaskFrontmatterError("new receipt directory is not exact owner-private 0700; inherited setgid is not normalized.")
            if not same_file_state(created, named) or os.listdir(created_fd):
                raise TaskFrontmatterError("new receipt directory changed before exact custody was verified.")
            verified = os.fstat(created_fd)
            named_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not same_file_state(created, verified) or not same_file_state(verified, named_after) or stat.S_IMODE(verified.st_mode) != 0o700 or os.listdir(created_fd):
                raise TaskFrontmatterError("new receipt directory changed before exact custody was verified.")
            return path
    finally:
        if created_fd >= 0:
            os.close(created_fd)
        os.close(parent_fd)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise TaskFrontmatterError("receipt directory must be an owner-private 0700 directory.")
    return path


def read_private_json(path: Path, *, required: bool = False) -> tuple[dict[str, object], bytes] | None:
    try:
        payload, _state = stable_owned_read(path, modes=frozenset({0o400, 0o600}), label=f"private record {path.name}")
    except FileNotFoundError:
        if required:
            raise TaskFrontmatterError(f"required private record is missing: {path.name}")
        return None
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError(f"private record is malformed: {path.name}") from exc
    if not isinstance(value, dict) or payload != canonical_json(value):
        raise TaskFrontmatterError(f"private record is unsafe or noncanonical: {path.name}")
    return value, payload


def read_immutable_json(path: Path) -> tuple[dict[str, object], bytes]:
    payload, _state = stable_owned_read(path, modes=frozenset({0o444}), label=f"immutable JSON {path.name}")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskFrontmatterError(f"immutable JSON is malformed: {path.name}") from exc
    if not isinstance(value, dict) or payload != canonical_json(value):
        raise TaskFrontmatterError(f"immutable JSON is unsafe or noncanonical: {path.name}")
    return value, payload


def write_private_json(path: Path, value: object, *, final: bool) -> bytes:
    payload = canonical_json(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("private record write made no progress")
            offset += written
        os.fsync(fd)
        if final:
            os.fchmod(fd, 0o400)
            os.fsync(fd)
    except Exception:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return payload


def replace_private_json(path: Path, expected: bytes, value: object, *, final: bool) -> bytes:
    current = read_private_json(path, required=True)
    assert current is not None
    if current[1] != expected:
        raise TaskFrontmatterError(f"private record changed before replacement: {path.name}")
    payload = canonical_json(value)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o400 if final else 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("private record replacement made no progress")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        latest = read_private_json(path, required=True)
        assert latest is not None
        if latest[1] != expected:
            raise TaskFrontmatterError(f"private record changed during replacement: {path.name}")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)
    return payload


def remove_private_json(path: Path, expected: bytes) -> None:
    current = read_private_json(path, required=True)
    assert current is not None
    if current[1] != expected:
        raise TaskFrontmatterError(f"private record changed before removal: {path.name}")
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def transfer_receipt_path(receipt_dir: Path, row_ids: tuple[str, ...]) -> Path:
    return receipt_dir / f"transfer-{'-'.join(row_ids)}.json"


def close_receipt_path(receipt_dir: Path, row_ids: tuple[str, ...]) -> Path:
    return receipt_dir / f"close-{'-'.join(row_ids)}.json"


def source1376_record(args: Args, operation: str, row: PlanRow, **values: object) -> str:
    record: dict[str, object] = {
        "operation": operation,
        "row": row.row_id,
        "plan_sha256": PLAN_SHA256,
        "execution_binding_sha256": EXECUTION_BINDING_SHA256,
        "prepared_binding_sha256": prepared_binding_digest(args),
        "authority": f"{AUTHORITY_REF}:3-3",
        "authority_sha256": AUTHORITY_SHA256,
        **values,
    }
    return f"Source-1376 {json.dumps(record, ensure_ascii=True, separators=(',', ':'), sort_keys=True)}"


def active_metadata(root: Path, paths: tuple[Path, ...]) -> tuple[dict[Path, TaskMetadata], dict[Path, str]]:
    parsed: dict[Path, TaskMetadata] = {}
    malformed: dict[Path, str] = {}
    for path in paths:
        text = task_bytes(path).decode("utf-8")
        try:
            metadata = parse_task_metadata(text, root)
        except TaskFrontmatterError:
            malformed[path] = text
            continue
        if metadata is not None and metadata.status != "done":
            parsed[path] = metadata
    return parsed, malformed


def row_ids_for_paths(root: Path, rows: dict[str, PlanRow]) -> dict[Path, str]:
    return {plan_path(root, row): row_id for row_id, row in rows.items()}


def validate_source_target_owners(
    root: Path,
    rows: dict[str, PlanRow],
    row: PlanRow,
    source: Path,
    parsed: dict[Path, TaskMetadata],
    malformed: dict[Path, str],
) -> None:
    for path, text in malformed.items():
        if raw_target_claims(text, row.runat):
            raise TaskFrontmatterError(f"cannot verify row {row.row_id} target because {relative_task_ref(root, path)} is malformed.")
    owners = tuple(path for path, metadata in parsed.items() if metadata.runat == row.runat)
    owner_ids = {row_ids_for_paths(root, rows).get(path, "") for path in owners}
    if row.row_id == "28":
        expected_shared = {"28"} if rows["29"].status == "done" else {"28", "29"}
    elif row.row_id == "72":
        expected_shared = {"71", "72"}
    else:
        expected_shared = None
    if expected_shared is not None:
        if owner_ids != expected_shared:
            raise TaskFrontmatterError(f"row {row.row_id} shared-target ownership drifted.")
    elif owners != (source,):
        refs = ", ".join(relative_task_ref(root, path) for path in owners) or "none"
        raise TaskFrontmatterError(f"row {row.row_id} is not the sole active target owner: {refs}.")


def string_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise TaskFrontmatterError(f"Source-1376 {label} is malformed.")
    return value


def finish_transfer_journal_locked(
    args: Args,
    rows: dict[str, PlanRow],
    selected: tuple[PlanRow, ...],
    initial_paths: tuple[Path, ...],
    expected_destination_sha256: str,
    journal_path: Path,
    journal_value: dict[str, object],
    journal_payload: bytes,
) -> dict[str, object]:
    row_ids = tuple(row.row_id for row in selected)
    receipt_path = transfer_receipt_path(args.receipt_dir, row_ids)
    destination = (args.root / DESTINATION_REF).resolve()
    sources = tuple(plan_path(args.root, row) for row in selected)
    receipt = journal_value.get("receipt")
    if (
        journal_value.get("schema") != "omo-source1376-transfer-journal/v1"
        or journal_value.get("rows") != list(row_ids)
        or journal_value.get("destination") != DESTINATION_REF
        or journal_value.get("receipt_path") != str(receipt_path)
        or not isinstance(receipt, dict)
        or receipt.get("schema") != "omo-source1376-transfer/v1"
        or receipt.get("rows") != list(row_ids)
        or receipt.get("prepared_binding_sha256") != prepared_binding_digest(args)
        or receipt.get("legacy_recovery_absent") is not True
        or receipt.get("destination_before_sha256") != expected_destination_sha256
        or path_entry_exists(args.root / LEGACY_TRANSFER_JOURNAL)
    ):
        raise TaskFrontmatterError("Source-1376 transfer recovery record does not match this operation.")
    destination_before = journal_value.get("destination_before")
    destination_after = journal_value.get("destination_after")
    if not isinstance(destination_before, str) or not isinstance(destination_after, str):
        raise TaskFrontmatterError("Source-1376 transfer recovery destination state is malformed.")
    sources_before = string_mapping(journal_value.get("sources_before"), "transfer recovery source-before map")
    sources_after = string_mapping(journal_value.get("sources_after"), "transfer recovery source-after map")
    unchanged = string_mapping(journal_value.get("unchanged_sha256"), "transfer recovery unchanged map")
    markdown_values = journal_value.get("markdown_paths")
    if (
        set(sources_before) != {row.task_ref for row in selected}
        or set(sources_after) != set(sources_before)
        or sha256(destination_before.encode()) != expected_destination_sha256
        or sha256(destination_after.encode()) != receipt.get("destination_after_sha256")
        or not isinstance(markdown_values, list)
        or any(not isinstance(value, str) for value in markdown_values)
        or set(initial_paths) != {Path(value).resolve() for value in markdown_values}
        or sha256(task_bytes(args.root / "TODO.md")) != receipt.get("todo_sha256")
    ):
        raise TaskFrontmatterError("Source-1376 transfer recovery bindings changed.")
    source_receipts = receipt.get("sources")
    if not isinstance(source_receipts, list) or len(source_receipts) != len(selected):
        raise TaskFrontmatterError("Source-1376 transfer recovery source receipts are malformed.")
    moving: list[str] = []
    for row, source_receipt in zip(selected, source_receipts, strict=True):
        if not isinstance(source_receipt, dict) or source_receipt.get("row") != row.row_id:
            raise TaskFrontmatterError("Source-1376 transfer recovery source order changed.")
        source_text = sources_before[row.task_ref]
        _text, metadata = validate_original_row(args.root, row, source_text.encode())
        after_text = sources_after[row.task_ref]
        after_metadata = require_v1_metadata(after_text)
        sent_record = source_receipt.get("sent_record")
        received_record = source_receipt.get("received_record")
        if (
            sha256(after_text.encode()) != source_receipt.get("after_sha256")
            or after_metadata.status != "blocked"
            or after_metadata.pending_task_items
            or source_receipt.get("items") != list(metadata.pending_task_items)
            or not isinstance(sent_record, str)
            or sent_record not in after_text
            or not isinstance(received_record, str)
            or received_record not in destination_after
        ):
            raise TaskFrontmatterError("Source-1376 transfer recovery derivation changed.")
        moving.extend(metadata.pending_task_items)
    destination_before_metadata = require_v1_metadata(destination_before)
    destination_after_metadata = require_v1_metadata(destination_after)
    if destination_after_metadata.pending_task_items != (*destination_before_metadata.pending_task_items, *moving):
        raise TaskFrontmatterError("Source-1376 transfer recovery queue order changed.")
    affected = {destination, *sources}
    expected_unchanged = {relative_task_ref(args.root, path): sha256(task_bytes(path)) for path in initial_paths if path not in affected}
    if unchanged != expected_unchanged:
        raise TaskFrontmatterError("an unrelated task changed while a Source-1376 transfer awaited recovery.")
    before_after = {
        destination: (destination_before, destination_after),
        **{plan_path(args.root, row): (sources_before[row.task_ref], sources_after[row.task_ref]) for row in selected},
    }
    for path, (before_text, after_text) in before_after.items():
        state, current = task_snapshot(path)
        if current == before_text.encode():
            replace_if_unchanged_locked(path, after_text, state)
        elif current != after_text.encode():
            raise TaskFrontmatterError(f"{path.name} conflicts with the Source-1376 transfer recovery record.")
    fsync_task_directories(destination, *sources)
    final_parsed, _malformed = active_metadata(args.root, initial_paths)
    final_destination_metadata = require_v1_metadata(task_bytes(destination).decode("utf-8"))
    if tuple(path for path, metadata in final_parsed.items() if metadata.runat == final_destination_metadata.runat) != (destination,):
        raise TaskFrontmatterError("Source-1376 escrow target ownership is not singular after transfer.")
    for item in moving:
        owners = {path for path, metadata in final_parsed.items() if item in metadata.pending_task_items}
        if owners != {destination}:
            raise TaskFrontmatterError(f"transferred item does not have exactly one active escrow owner: {item}")
    existing_receipt = read_private_json(receipt_path)
    if existing_receipt is None:
        write_private_json(receipt_path, receipt, final=True)
    elif existing_receipt[0] != receipt:
        raise TaskFrontmatterError("Source-1376 transfer receipt conflicts with its recovery record.")
    remove_private_json(journal_path, journal_payload)
    return receipt


def transfer_rows(
    args: Args,
    rows: dict[str, PlanRow],
    row_ids: tuple[str, ...],
    expected_destination_sha256: str,
    *,
    require_prepared_snapshot: bool = False,
    _held_prepared_locks: _HeldPreparedLocks | None = None,
) -> dict[str, object]:
    if not row_ids or any(row_id in PROTECTED_ROWS for row_id in row_ids):
        raise TaskFrontmatterError("transfer requires eligible non-protected Source-1376 rows.")
    if len(row_ids) > 1 and row_ids != ("44", "45"):
        raise TaskFrontmatterError("only the reviewed duplicate pair may transfer as a group.")
    selected = tuple(rows[row_id] for row_id in row_ids)
    if any(row.n_items == 0 for row in selected):
        raise TaskFrontmatterError("transfer source queue is empty.")
    receipt_path = transfer_receipt_path(args.receipt_dir, row_ids)
    journal_path = args.root / TRANSFER_JOURNAL
    legacy_journal_path = args.root / LEGACY_TRANSFER_JOURNAL
    prior_receipt = read_private_json(receipt_path)
    if prior_receipt is not None and not path_entry_exists(journal_path):
        if require_prepared_snapshot:
            raise TaskFrontmatterError("Source-1376 first-transfer handoff cannot reuse a preexisting receipt.")
        validate_initial_state(args, rows)
        latest = read_private_json(receipt_path, required=True)
        assert latest is not None
        if latest[1] != prior_receipt[1]:
            raise TaskFrontmatterError("existing Source-1376 transfer receipt changed during validation.")
        value = prior_receipt[0]
        validated_rows, _moving = validate_transfer_receipt_against_plan(value, rows, args)
        if validated_rows != row_ids or value.get("destination_before_sha256") != expected_destination_sha256:
            raise TaskFrontmatterError("existing transfer receipt does not match the requested rows.")
        return value
    destination = (args.root / DESTINATION_REF).resolve()
    sources = tuple(plan_path(args.root, row) for row in selected)
    with ExitStack() as membership:
        if _held_prepared_locks is None:
            membership.enter_context(root_membership_lock(args.root))
            initial_paths = markdown_paths(args.root)
        else:
            initial_paths = _held_prepared_locks.markdown
        locked_paths = tuple(
            sorted(
                {
                    *initial_paths,
                    (args.root / "TODO.md").resolve(),
                    *operation_binding_paths(args, rows),
                    journal_path,
                    legacy_journal_path,
                    receipt_path,
                    args.receipt_dir,
                    *(prepared_binding_lock_paths(args, rows, initial_paths) if require_prepared_snapshot else ()),
                }
            )
        )
        with ExitStack() as locks:
            if _held_prepared_locks is None:
                for locked_path in locked_paths:
                    locks.enter_context(task_file_lock(locked_path))
            else:
                initial_paths = _held_prepared_locks.require(args.root, locked_paths)
            validate_operation_bindings_locked(args, rows)
            if path_entry_exists(legacy_journal_path):
                raise TaskFrontmatterError("legacy closure-transfer recovery state is present.")
            todo_before = task_bytes(args.root / "TODO.md")
            snapshots = {path: task_snapshot(path) for path in initial_paths}
            payloads = {path: snapshot[1] for path, snapshot in snapshots.items()}
            if destination not in payloads or any(source not in payloads for source in sources):
                raise TaskFrontmatterError("transfer source or destination is not a current Markdown record.")
            existing_journal = read_private_json(journal_path)
            if existing_journal is not None:
                return finish_transfer_journal_locked(
                    args,
                    rows,
                    selected,
                    initial_paths,
                    expected_destination_sha256,
                    journal_path,
                    existing_journal[0],
                    existing_journal[1],
                )
            if require_prepared_snapshot:
                value = prepared_binding_value(args)
                if value is None or value.get("first_transfer_rows") != list(row_ids):
                    raise TaskFrontmatterError("Source-1376 first transfer differs from the reviewed prepared handoff.")
                validate_prepared_snapshot_locked(args, rows, initial_paths)
            if sha256(payloads[destination]) != expected_destination_sha256:
                raise TaskFrontmatterError("destination digest does not match the rolling transfer chain.")
            destination_text = payloads[destination].decode("utf-8")
            destination_metadata = require_v1_metadata(destination_text)
            if destination_metadata.status == "done" or not destination_metadata.is_manager or destination_metadata.runat != "cedit:15":
                raise TaskFrontmatterError("Source-1376 destination is not the exact active escrow manager.")
            parsed, malformed = active_metadata(args.root, initial_paths)
            destination_owners = tuple(path for path, metadata in parsed.items() if metadata.runat == destination_metadata.runat)
            if destination_owners != (destination,):
                raise TaskFrontmatterError("Source-1376 destination no longer has unique active target ownership.")
            source_values: list[tuple[PlanRow, Path, str, TaskMetadata]] = []
            moving: list[str] = []
            expected_item_owners: dict[str, Counter[Path]] = {}
            for row, source in zip(selected, sources, strict=True):
                source_text, source_metadata = validate_original_row(args.root, row, payloads[source])
                if source_metadata.runat.partition(":")[0].startswith("h"):
                    raise TaskFrontmatterError("Source-1376 cannot transfer a human-owned source.")
                validate_source_target_owners(args.root, rows, row, source, parsed, malformed)
                source_values.append((row, source, source_text, source_metadata))
                moving.extend(source_metadata.pending_task_items)
                for item in source_metadata.pending_task_items:
                    expected_item_owners.setdefault(item, Counter())[source] += 1
            for item, expected in expected_item_owners.items():
                actual = Counter(path for path, metadata in parsed.items() for candidate in metadata.pending_task_items if candidate == item)
                if actual != expected:
                    raise TaskFrontmatterError(f"pending item does not have exactly the reviewed source ownership: {item}")
            destination_after = render_pending_items(destination_text, (*destination_metadata.pending_task_items, *moving))
            source_after: dict[Path, str] = {}
            source_receipts: list[dict[str, object]] = []
            received_records: list[str] = []
            for row, source, source_text, source_metadata in source_values:
                prepared = source_text
                if source_metadata.status != "blocked":
                    prepared = update_frontmatter_status(prepared, "blocked", SHUTDOWN_BLOCKER, args.root)
                sent_record = source1376_record(
                    args,
                    "pending-closure-transfer-sent",
                    row,
                    destination=DESTINATION_REF,
                    source_sha256=row.task_sha256,
                    source_status=source_metadata.status,
                    source_blocked_on=source_metadata.blocked_on,
                    queue_sha256=row.queue_sha256,
                    count=row.n_items,
                )
                received_record = source1376_record(
                    args,
                    "pending-closure-transfer-received",
                    row,
                    source=row.task_ref,
                    source_sha256=row.task_sha256,
                    source_status=source_metadata.status,
                    source_blocked_on=source_metadata.blocked_on,
                    queue_sha256=row.queue_sha256,
                    count=row.n_items,
                )
                updated_source = append_comment(render_pending_items(prepared, ()), sent_record)
                destination_after = append_comment(destination_after, received_record)
                source_after[source] = updated_source
                received_records.append(received_record)
                source_receipts.append(
                    {
                        "row": row.row_id,
                        "task": row.task_ref,
                        "before_sha256": row.task_sha256,
                        "after_sha256": sha256(updated_source.encode()),
                        "status": source_metadata.status,
                        "blocked_on": source_metadata.blocked_on,
                        "items": list(source_metadata.pending_task_items),
                        "count": row.n_items,
                        "queue_sha256": row.queue_sha256,
                        "sent_record": sent_record,
                        "received_record": received_record,
                    }
                )
            destination_after_payload = destination_after.encode()
            receipt: dict[str, object] = {
                "schema": "omo-source1376-transfer/v1",
                "rows": list(row_ids),
                "plan_sha256": PLAN_SHA256,
                "execution_binding_sha256": EXECUTION_BINDING_SHA256,
                "prepared_binding_sha256": prepared_binding_digest(args),
                "authority": f"{AUTHORITY_REF}:3-3",
                "authority_sha256": AUTHORITY_SHA256,
                "todo_sha256": sha256(todo_before),
                "destination": DESTINATION_REF,
                "destination_before_sha256": expected_destination_sha256,
                "destination_after_sha256": sha256(destination_after_payload),
                "sources": source_receipts,
                "received_records": received_records,
                "recovery_record": TRANSFER_JOURNAL,
                "legacy_recovery_absent": True,
            }
            journal: dict[str, object] = {
                "schema": "omo-source1376-transfer-journal/v1",
                "rows": list(row_ids),
                "destination": DESTINATION_REF,
                "destination_before": destination_text,
                "destination_after": destination_after,
                "sources_before": {row.task_ref: source_text for row, _source, source_text, _metadata in source_values},
                "sources_after": {row.task_ref: source_after[source] for row, source, _text, _metadata in source_values},
                "markdown_paths": [str(path) for path in initial_paths],
                "unchanged_sha256": {relative_task_ref(args.root, path): sha256(payload) for path, payload in payloads.items() if path not in {*sources, destination}},
                "receipt_path": str(receipt_path),
                "receipt": receipt,
            }
            journal_payload = write_private_json(journal_path, journal, final=False)
            return finish_transfer_journal_locked(
                args,
                rows,
                selected,
                initial_paths,
                expected_destination_sha256,
                journal_path,
                journal,
                journal_payload,
            )


def transfer_receipts(receipt_dir: Path, initial_destination: str = DESTINATION_INITIAL_SHA256) -> list[dict[str, object]]:
    by_before: dict[str, dict[str, object]] = {}
    for path in sorted(receipt_dir.glob("transfer-*.json")):
        loaded = read_private_json(path, required=True)
        assert loaded is not None
        receipt = loaded[0]
        before = receipt.get("destination_before_sha256")
        after = receipt.get("destination_after_sha256")
        if (
            receipt.get("schema") != "omo-source1376-transfer/v1"
            or not isinstance(before, str)
            or not isinstance(after, str)
            or SHA256_RE.fullmatch(before) is None
            or SHA256_RE.fullmatch(after) is None
            or before in by_before
        ):
            raise TaskFrontmatterError(f"unexpected transfer receipt schema: {path.name}")
        by_before[before] = receipt
    ordered: list[dict[str, object]] = []
    current = initial_destination
    while current in by_before:
        receipt = by_before.pop(current)
        ordered.append(receipt)
        current = str(receipt["destination_after_sha256"])
    if by_before:
        raise TaskFrontmatterError("Source-1376 transfer receipts do not form one contiguous destination chain.")
    return ordered


def validate_transfer_receipt_against_plan(
    receipt: dict[str, object],
    rows: dict[str, PlanRow],
    args: Args | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    expected_prepared_binding = "" if args is None else prepared_binding_digest(args)
    row_values = receipt.get("rows")
    if (
        set(receipt)
        != {
            "schema",
            "rows",
            "plan_sha256",
            "execution_binding_sha256",
            "prepared_binding_sha256",
            "authority",
            "authority_sha256",
            "todo_sha256",
            "destination",
            "destination_before_sha256",
            "destination_after_sha256",
            "sources",
            "received_records",
            "recovery_record",
            "legacy_recovery_absent",
        }
        or receipt.get("schema") != "omo-source1376-transfer/v1"
        or receipt.get("plan_sha256") != PLAN_SHA256
        or receipt.get("execution_binding_sha256") != EXECUTION_BINDING_SHA256
        or receipt.get("prepared_binding_sha256") != expected_prepared_binding
        or receipt.get("authority") != f"{AUTHORITY_REF}:3-3"
        or receipt.get("authority_sha256") != AUTHORITY_SHA256
        or receipt.get("destination") != DESTINATION_REF
        or receipt.get("recovery_record") != TRANSFER_JOURNAL
        or receipt.get("legacy_recovery_absent") is not True
        or (args is not None and path_entry_exists(args.root / LEGACY_TRANSFER_JOURNAL))
        or not isinstance(row_values, list)
        or not row_values
        or any(not isinstance(row_id, str) or row_id not in rows or row_id in PROTECTED_ROWS for row_id in row_values)
        or (len(row_values) > 1 and tuple(row_values) != ("44", "45"))
        or any(rows[str(row_id)].n_items == 0 for row_id in row_values)
        or any(SHA256_RE.fullmatch(str(receipt.get(field, ""))) is None for field in ("todo_sha256", "destination_before_sha256", "destination_after_sha256"))
    ):
        raise TaskFrontmatterError("Source-1376 transfer receipt is not bound to the reviewed plan.")
    row_ids = tuple(str(row_id) for row_id in row_values)
    sources = receipt.get("sources")
    received_records = receipt.get("received_records")
    if not isinstance(sources, list) or len(sources) != len(row_ids) or not isinstance(received_records, list):
        raise TaskFrontmatterError("Source-1376 transfer receipt source evidence is malformed.")
    moving: list[str] = []
    expected_received: list[str] = []
    for row_id, source_value in zip(row_ids, sources, strict=True):
        row = rows[row_id]
        if not isinstance(source_value, dict):
            raise TaskFrontmatterError(f"row {row_id} transfer receipt source evidence is malformed.")
        items_value = source_value.get("items")
        blocked_on = source_value.get("blocked_on")
        after_sha256 = source_value.get("after_sha256")
        if (
            set(source_value)
            != {
                "row",
                "task",
                "before_sha256",
                "after_sha256",
                "status",
                "blocked_on",
                "items",
                "count",
                "queue_sha256",
                "sent_record",
                "received_record",
            }
            or not isinstance(items_value, list)
            or any(not isinstance(item, str) for item in items_value)
            or not isinstance(blocked_on, str)
            or not isinstance(after_sha256, str)
            or SHA256_RE.fullmatch(after_sha256) is None
        ):
            raise TaskFrontmatterError(f"row {row_id} transfer receipt source evidence is malformed.")
        items = tuple(items_value)
        if args is None:
            raise TaskFrontmatterError("Source-1376 transfer receipt lacks prepared-binding validation context.")
        sent_record = source1376_record(
            args,
            "pending-closure-transfer-sent",
            row,
            destination=DESTINATION_REF,
            source_sha256=row.task_sha256,
            source_status=row.status,
            source_blocked_on=blocked_on,
            queue_sha256=row.queue_sha256,
            count=row.n_items,
        )
        received_record = source1376_record(
            args,
            "pending-closure-transfer-received",
            row,
            source=row.task_ref,
            source_sha256=row.task_sha256,
            source_status=row.status,
            source_blocked_on=blocked_on,
            queue_sha256=row.queue_sha256,
            count=row.n_items,
        )
        if (
            source_value.get("row") != row_id
            or source_value.get("task") != row.task_ref
            or source_value.get("before_sha256") != row.task_sha256
            or source_value.get("status") != row.status
            or (row.status != "blocked" and blocked_on)
            or source_value.get("count") != row.n_items
            or source_value.get("queue_sha256") != row.queue_sha256
            or len(items) != row.n_items
            or queue_sha256(items) != row.queue_sha256
            or source_value.get("sent_record") != sent_record
            or source_value.get("received_record") != received_record
        ):
            raise TaskFrontmatterError(f"row {row_id} transfer receipt does not rederive from the reviewed plan.")
        moving.extend(items)
        expected_received.append(received_record)
    if received_records != expected_received:
        raise TaskFrontmatterError("Source-1376 transfer receipt received-record order changed.")
    return row_ids, tuple(moving)


def validated_transfer_receipts(args: Args, rows: dict[str, PlanRow]) -> list[dict[str, object]]:
    receipts = transfer_receipts(args.receipt_dir, initial_destination_sha256(args))
    seen: Counter[str] = Counter()
    expected_names: set[str] = set()
    for receipt in receipts:
        row_ids, _moving = validate_transfer_receipt_against_plan(receipt, rows, args)
        seen.update(row_ids)
        expected_names.add(transfer_receipt_path(args.receipt_dir, row_ids).name)
    actual_names = {path.name for path in args.receipt_dir.glob("transfer-*.json")}
    if seen and any(count != 1 for count in seen.values()) or actual_names != expected_names:
        raise TaskFrontmatterError("Source-1376 transfer receipts have duplicate rows or unexpected filenames.")
    return receipts


def expected_current_task_sha256(args: Args, rows: dict[str, PlanRow], row: PlanRow) -> str:
    if row.n_items == 0:
        return row.task_sha256
    matches: list[str] = []
    for receipt in validated_transfer_receipts(args, rows):
        sources = receipt.get("sources")
        if not isinstance(sources, list):
            raise TaskFrontmatterError("transfer receipt lacks source records.")
        for source in sources:
            if isinstance(source, dict) and source.get("row") == row.row_id and isinstance(source.get("after_sha256"), str):
                matches.append(str(source["after_sha256"]))
    if len(matches) != 1:
        raise TaskFrontmatterError(f"row {row.row_id} requires exactly one transfer receipt before closure.")
    return matches[0]


def close_record(args: Args, row: PlanRow, mode: str, receipt_path: Path, **values: object) -> str:
    return source1376_record(
        args,
        "lifecycle-close",
        row,
        mode=mode,
        receipt=str(receipt_path),
        original_status=row.status,
        original_task_sha256=row.task_sha256,
        **values,
    )


def todo_done_text(root: Path, path: Path, todo_text: str, target: str) -> str:
    return reconcile_todo_text(root, path, todo_text, target, "previous", ("current", "human pending", "low priority", "previous"))


def todo_sections_in_text(root: Path, task: Path, todo_text: str) -> tuple[str, ...]:
    sections: list[str] = []
    section = ""
    for line in todo_text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
        elif task in todo_row_task_paths(root, line):
            sections.append(section)
    return tuple(sections)


def prepare_todo_close(root: Path, task: Path, todo_text: str, target: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    before = todo_sections_in_text(root, task, todo_text)
    if not before:
        return todo_text, (), ()
    if len(before) != 1:
        raise TaskFrontmatterError(f"TODO custody for {relative_task_ref(root, task)} is ambiguous.")
    updated = todo_done_text(root, task, todo_text, target)
    after = todo_sections_in_text(root, task, updated)
    if after != ("previous",):
        raise TaskFrontmatterError(f"TODO custody for {relative_task_ref(root, task)} did not become terminal.")
    return updated, before, after


def close_receipt_todo_sections(
    receipt: dict[str, object],
    selected: tuple[PlanRow, ...],
    field: str,
) -> dict[str, tuple[str, ...]]:
    raw = receipt.get(field)
    if len(selected) == 1 and isinstance(raw, list):
        values: dict[str, object] = {selected[0].row_id: raw}
    elif isinstance(raw, dict) and set(raw) == {row.row_id for row in selected}:
        values = raw
    else:
        raise TaskFrontmatterError(f"Source-1376 close receipt has invalid {field} evidence.")
    result: dict[str, tuple[str, ...]] = {}
    for row in selected:
        sections = values[row.row_id]
        if not isinstance(sections, list) or any(not isinstance(section, str) for section in sections):
            raise TaskFrontmatterError(f"Source-1376 close receipt has malformed {field} evidence.")
        result[row.row_id] = tuple(sections)
    return result


def validate_close_todo_evidence(
    root: Path,
    selected: tuple[PlanRow, ...],
    receipt: dict[str, object],
    todo_before: str,
    todo_after: str,
) -> dict[str, tuple[str, ...]]:
    expected_before = close_receipt_todo_sections(receipt, selected, "todo_before_sections")
    expected_after = close_receipt_todo_sections(receipt, selected, "todo_after_sections")
    for row in selected:
        task = plan_path(root, row)
        before = todo_sections_in_text(root, task, todo_before)
        after = todo_sections_in_text(root, task, todo_after)
        if before != expected_before[row.row_id] or after != expected_after[row.row_id]:
            raise TaskFrontmatterError(f"row {row.row_id} TODO evidence does not bind the close recovery states.")
        if len(before) > 1 or (not before and after != ()) or (before and after != ("previous",)):
            raise TaskFrontmatterError(f"row {row.row_id} TODO evidence is not a supported terminal transition.")
    return expected_after


def close_journal_path(receipt_dir: Path, row_ids: tuple[str, ...]) -> Path:
    return receipt_dir / f".close-{'-'.join(row_ids)}.journal.json"


def close_journal_rows(value: dict[str, object], rows: dict[str, PlanRow]) -> tuple[PlanRow, ...]:
    row_values = value.get("rows")
    if (
        value.get("schema") != "omo-source1376-close-journal/v1"
        or not isinstance(row_values, list)
        or not row_values
        or any(not isinstance(row_id, str) or row_id not in rows or row_id in PROTECTED_ROWS for row_id in row_values)
    ):
        raise TaskFrontmatterError("Source-1376 close recovery record has invalid rows.")
    return tuple(rows[str(row_id)] for row_id in row_values)


def validate_close_receipt(
    args: Args,
    rows: dict[str, PlanRow],
    row_ids: tuple[str, ...],
    receipt: dict[str, object],
) -> None:
    selected = tuple(rows[row_id] for row_id in row_ids)
    if not selected:
        raise TaskFrontmatterError("existing Source-1376 close receipt has no rows.")
    first = next(iter(selected))
    if (
        receipt.get("schema") != "omo-source1376-close/v1"
        or receipt.get("rows") != list(row_ids)
        or receipt.get("plan_sha256") != PLAN_SHA256
        or receipt.get("execution_binding_sha256") != EXECUTION_BINDING_SHA256
        or receipt.get("prepared_binding_sha256") != prepared_binding_digest(args)
        or receipt.get("authority_sha256") != AUTHORITY_SHA256
        or receipt.get("mail") != "suppressed"
        or any(row_id in PROTECTED_ROWS for row_id in row_ids)
        or (len(row_ids) > 1 and row_ids != INTERNAL_SHARED_PAIR)
        or any(row.runat != first.runat for row in selected)
        or receipt.get("target") != first.runat
    ):
        raise TaskFrontmatterError("existing Source-1376 close receipt has invalid identity.")
    before_value = receipt.get("task_before_sha256")
    before_digests = [before_value] if isinstance(before_value, str) else before_value
    digest_value = receipt.get("task_after_sha256")
    digests = [digest_value] if isinstance(digest_value, str) else digest_value
    if (
        not isinstance(before_digests, list)
        or len(before_digests) != len(row_ids)
        or any(not isinstance(value, str) or SHA256_RE.fullmatch(value) is None for value in before_digests)
        or not isinstance(digests, list)
        or len(digests) != len(row_ids)
        or any(not isinstance(value, str) or SHA256_RE.fullmatch(value) is None for value in digests)
    ):
        raise TaskFrontmatterError("existing Source-1376 close receipt has invalid task digests.")
    expected_before = [expected_current_task_sha256(args, rows, row) for row in selected]
    if before_digests != expected_before:
        raise TaskFrontmatterError("existing Source-1376 close receipt does not continue reviewed task custody.")
    mode = receipt.get("mode")
    receipt_path = close_receipt_path(args.receipt_dir, row_ids)
    receipt_notes: list[str]
    if len(selected) > 1:
        if mode not in {"coordinated-shared-target", "coordinated-shared-missing-target"} or receipt.get("tasks") != [row.task_ref for row in selected]:
            raise TaskFrontmatterError("existing Source-1376 shared close receipt has invalid disposition.")
        expected_notes = [close_record(args, row, str(mode), receipt_path, target=row.runat, paired_rows=list(row_ids)) for row in selected]
        notes = receipt.get("notes")
        if notes != expected_notes:
            raise TaskFrontmatterError("existing Source-1376 shared close receipt provenance changed.")
        receipt_notes = expected_notes
    else:
        row = first
        if row.row_id in EXTERNAL_SHARED_ROWS:
            protected_row = rows[EXTERNAL_SHARED_ROWS[row.row_id]]
            allowed_modes = {"preserved-shared-target", "protected-shared-missing-target"} if protected_row.status == "done" else {"preserved-shared-target"}
        elif row.row_id == COMPLETED_SHELL_ROW:
            allowed_modes = {"exited-shell"}
        else:
            allowed_modes = {"live", "missing-target"}
        if mode not in allowed_modes:
            raise TaskFrontmatterError("existing Source-1376 close receipt has an invalid disposition.")
        if mode in {"missing-target", "preserved-shared-target", "protected-shared-missing-target"}:
            expected_note = close_record(
                args,
                row,
                str(mode),
                receipt_path,
                target=row.runat,
                protected_row=EXTERNAL_SHARED_ROWS.get(row.row_id, ""),
            )
            if receipt.get("task") != row.task_ref or receipt.get("note") != expected_note:
                raise TaskFrontmatterError("existing Source-1376 metadata-close provenance changed.")
        else:
            expected_note = close_record(
                args,
                row,
                str(mode),
                receipt_path,
                target=row.runat,
                pane_id=receipt.get("pane_before"),
                paired_rows=[],
            )
            if receipt.get("tasks") != [row.task_ref] or receipt.get("notes") != [expected_note]:
                raise TaskFrontmatterError("existing Source-1376 live-close provenance changed.")
        receipt_notes = [expected_note]
    for row, expected, note in zip(selected, digests, receipt_notes, strict=True):
        task = plan_path(args.root, row)
        payload = task_bytes(task)
        metadata = require_v1_metadata(payload.decode("utf-8"))
        if sha256(payload) != expected or metadata.status != "done" or metadata.pending_task_items or note not in payload.decode("utf-8"):
            raise TaskFrontmatterError(f"closed row {row.row_id} drifted after its Source-1376 receipt.")
    target = receipt.get("target")
    pane_before = receipt.get("pane_before")
    pane_after = receipt.get("pane_after")
    session_id = receipt.get("session_id")
    if not isinstance(target, str) or not isinstance(pane_before, str) or not isinstance(pane_after, str) or not isinstance(session_id, str):
        raise TaskFrontmatterError("existing Source-1376 close receipt has invalid target evidence.")
    if mode == "preserved-shared-target":
        if not pane_after or exact_pane_id(target) != pane_after:
            raise TaskFrontmatterError("protected shared target drifted after Source-1376 metadata closure.")
    else:
        if pane_after or exact_pane_id(target):
            raise TaskFrontmatterError("a Source-1376 closed target became live again.")
        if mode in {"missing-target", "protected-shared-missing-target", "coordinated-shared-missing-target"} and (pane_before or session_id):
            raise TaskFrontmatterError("missing-target receipt contains live-pane evidence.")
        if mode in {"live", "exited-shell", "coordinated-shared-target"} and not pane_before:
            raise TaskFrontmatterError("live closure receipt lacks its bound pane.")
    expected_todo = close_receipt_todo_sections(receipt, selected, "todo_after_sections")
    for row in selected:
        if task_row_sections(args.root, plan_path(args.root, row)) != expected_todo[row.row_id]:
            raise TaskFrontmatterError(f"closed row {row.row_id} TODO custody drifted after its receipt.")


def validate_close_bookkeeping_derivation(
    args: Args,
    rows: dict[str, PlanRow],
    selected: tuple[PlanRow, ...],
    journal_value: dict[str, object],
    receipt: dict[str, object],
    tasks_before: dict[str, str],
    tasks_after: dict[str, str],
) -> None:
    if not selected:
        raise TaskFrontmatterError("Source-1376 close bookkeeping has no rows.")
    first = next(iter(selected))
    row_ids = tuple(row.row_id for row in selected)
    target = first.runat
    mode = receipt.get("mode")
    receipt_path = close_receipt_path(args.receipt_dir, row_ids)
    if (
        receipt.get("schema") != "omo-source1376-close/v1"
        or receipt.get("rows") != list(row_ids)
        or receipt.get("plan_sha256") != PLAN_SHA256
        or receipt.get("execution_binding_sha256") != EXECUTION_BINDING_SHA256
        or receipt.get("prepared_binding_sha256") != prepared_binding_digest(args)
        or receipt.get("authority_sha256") != AUTHORITY_SHA256
        or receipt.get("target") != target
        or receipt.get("mail") != "suppressed"
    ):
        raise TaskFrontmatterError("Source-1376 close bookkeeping receipt identity changed.")
    before_value = receipt.get("task_before_sha256")
    before_digests = [before_value] if isinstance(before_value, str) else before_value
    after_value = receipt.get("task_after_sha256")
    after_digests = [after_value] if isinstance(after_value, str) else after_value
    if (
        before_digests != [expected_current_task_sha256(args, rows, row) for row in selected]
        or not isinstance(after_digests, list)
        or len(after_digests) != len(selected)
        or any(not isinstance(expected, str) or sha256(tasks_after[row.task_ref].encode()) != expected for row, expected in zip(selected, after_digests, strict=True))
    ):
        raise TaskFrontmatterError("Source-1376 close bookkeeping receipt breaks task digest custody.")
    metadata_modes = {"missing-target", "preserved-shared-target", "protected-shared-missing-target"}
    pane_modes = {"live", "exited-shell", "coordinated-shared-target"}
    if mode in metadata_modes and len(selected) == 1:
        row = selected[0]
        if row.row_id in EXTERNAL_SHARED_ROWS:
            allowed = {"preserved-shared-target", "protected-shared-missing-target"} if rows[EXTERNAL_SHARED_ROWS[row.row_id]].status == "done" else {"preserved-shared-target"}
        else:
            allowed = {"missing-target"}
        note = close_record(
            args,
            row,
            str(mode),
            receipt_path,
            target=target,
            protected_row=EXTERNAL_SHARED_ROWS.get(row.row_id, ""),
        )
        expected_after = update_frontmatter_status(append_comment(tasks_before[row.task_ref], note), "done", "", args.root)
        pane_before = receipt.get("pane_before")
        pane_after = receipt.get("pane_after")
        if (
            mode not in allowed
            or receipt.get("task") != row.task_ref
            or receipt.get("note") != note
            or tasks_after[row.task_ref] != expected_after
            or not isinstance(pane_before, str)
            or not isinstance(pane_after, str)
            or receipt.get("session_id") != ""
            or (mode == "preserved-shared-target" and (not pane_before or pane_after != pane_before))
            or (mode != "preserved-shared-target" and (pane_before or pane_after))
        ):
            raise TaskFrontmatterError("Source-1376 metadata-close bookkeeping no longer rederives.")
        return
    if mode == "coordinated-shared-missing-target" and row_ids == INTERNAL_SHARED_PAIR:
        notes = [close_record(args, row, str(mode), receipt_path, target=target, paired_rows=list(row_ids)) for row in selected]
        expected_after = {row.task_ref: update_frontmatter_status(append_comment(tasks_before[row.task_ref], note), "done", "", args.root) for row, note in zip(selected, notes, strict=True)}
        if (
            receipt.get("tasks") != [row.task_ref for row in selected]
            or receipt.get("notes") != notes
            or receipt.get("pane_before") != ""
            or receipt.get("pane_after") != ""
            or receipt.get("session_id") != ""
            or tasks_after != expected_after
        ):
            raise TaskFrontmatterError("Source-1376 missing shared-target bookkeeping no longer rederives.")
        return
    if mode in pane_modes:
        originals, in_progress = pane_phase_task_maps(journal_value)
        validate_pane_journal_derivation(args, rows, selected, journal_value, originals, in_progress)
        closed_at = journal_value.get("closed_at")
        session_id = receipt.get("session_id")
        expected_mode = "coordinated-shared-target" if len(selected) > 1 else "exited-shell" if first.row_id == COMPLETED_SHELL_ROW else "live"
        if (
            mode != expected_mode
            or not isinstance(closed_at, str)
            or not isinstance(session_id, str)
            or receipt.get("tasks") != [row.task_ref for row in selected]
            or receipt.get("notes") != journal_value.get("notes")
            or receipt.get("pane_before") != journal_value.get("pane_before")
            or receipt.get("pane_after") != ""
            or tasks_before != in_progress
        ):
            raise TaskFrontmatterError("Source-1376 pane-close bookkeeping evidence is malformed.")
        try:
            close_time = datetime.fromisoformat(closed_at)
        except ValueError as exc:
            raise TaskFrontmatterError("Source-1376 pane-close bookkeeping timestamp changed.") from exc
        expected_after = {
            row.task_ref: update_frontmatter_status(
                in_progress[row.task_ref].rstrip("\n") + close_note(target, session_id, close_time),
                "done",
                "",
                args.root,
            )
            for row in selected
        }
        if tasks_after != expected_after:
            raise TaskFrontmatterError("Source-1376 pane-close terminal bookkeeping no longer rederives.")
        return
    raise TaskFrontmatterError("Source-1376 close bookkeeping mode is not supported for these rows.")


def finish_close_bookkeeping_locked(
    args: Args,
    rows: dict[str, PlanRow],
    journal_path: Path,
    journal_value: dict[str, object],
    journal_payload: bytes,
) -> dict[str, object]:
    validate_operation_bindings_locked(args, rows)
    validate_escrow_custody_locked(args, rows)
    selected = close_journal_rows(journal_value, rows)
    row_ids = tuple(row.row_id for row in selected)
    receipt_path = close_receipt_path(args.receipt_dir, row_ids)
    receipt = journal_value.get("receipt")
    target = journal_value.get("target")
    if (
        journal_value.get("phase") != "bookkeeping"
        or journal_value.get("receipt_path") != str(receipt_path)
        or not isinstance(target, str)
        or any(row.runat != target for row in selected)
        or not isinstance(receipt, dict)
        or receipt.get("schema") != "omo-source1376-close/v1"
        or receipt.get("rows") != list(row_ids)
        or receipt.get("mail") != "suppressed"
    ):
        raise TaskFrontmatterError("Source-1376 close recovery bookkeeping binding changed.")
    tasks_before = string_mapping(journal_value.get("tasks_before"), "close task-before map")
    tasks_after = string_mapping(journal_value.get("tasks_after"), "close task-after map")
    todo_before = journal_value.get("todo_before")
    todo_after = journal_value.get("todo_after")
    expected_refs = {row.task_ref for row in selected}
    if set(tasks_before) != expected_refs or set(tasks_after) != expected_refs or not isinstance(todo_before, str) or not isinstance(todo_after, str):
        raise TaskFrontmatterError("Source-1376 close recovery public states are malformed.")
    expected_task_digests = receipt.get("task_after_sha256")
    digest_values = [expected_task_digests] if isinstance(expected_task_digests, str) else expected_task_digests
    if (
        not isinstance(digest_values, list)
        or len(digest_values) != len(selected)
        or any(not isinstance(expected, str) or sha256(tasks_after[row.task_ref].encode()) != expected for row, expected in zip(selected, digest_values, strict=True))
    ):
        raise TaskFrontmatterError("Source-1376 close recovery receipt does not bind its terminal task bytes.")
    before_digest_value = receipt.get("task_before_sha256")
    before_digest_values = [before_digest_value] if isinstance(before_digest_value, str) else before_digest_value
    if before_digest_values != [expected_current_task_sha256(args, rows, row) for row in selected]:
        raise TaskFrontmatterError("Source-1376 close recovery does not continue reviewed task custody.")
    validate_close_bookkeeping_derivation(args, rows, selected, journal_value, receipt, tasks_before, tasks_after)
    if "tasks_original" in journal_value:
        originals, in_progress = pane_phase_task_maps(journal_value)
        validate_pane_journal_derivation(args, rows, selected, journal_value, originals, in_progress)
        if tasks_before != in_progress:
            raise TaskFrontmatterError("Source-1376 pane-close bookkeeping lost its durable intent state.")
    _ = validate_close_todo_evidence(args.root, selected, receipt, todo_before, todo_after)
    protected = string_mapping(journal_value.get("protected_sha256", {}), "close protected-task map")
    for task_ref, expected in protected.items():
        if SHA256_RE.fullmatch(expected) is None or sha256(task_bytes((args.root / task_ref).resolve())) != expected:
            raise TaskFrontmatterError("a protected shared-target record changed during Source-1376 closure.")
    preserved_pane = journal_value.get("preserved_pane", "")
    if not isinstance(preserved_pane, str) or (preserved_pane and exact_pane_id(target) != preserved_pane):
        raise TaskFrontmatterError("a protected shared-target pane changed during Source-1376 closure.")
    todo = args.root / "TODO.md"
    todo_state, todo_current = task_snapshot(todo)
    if todo_current == todo_before.encode():
        replace_if_unchanged_locked(todo, todo_after, todo_state)
    elif todo_current != todo_after.encode():
        raise TaskFrontmatterError("TODO conflicts with the Source-1376 close recovery record.")
    paths: list[Path] = []
    for row in selected:
        path = plan_path(args.root, row)
        paths.append(path)
        state, current = task_snapshot(path)
        before_payload = tasks_before[row.task_ref].encode()
        after_payload = tasks_after[row.task_ref].encode()
        if current == before_payload:
            replace_if_unchanged_locked(path, tasks_after[row.task_ref], state)
        elif current != after_payload:
            raise TaskFrontmatterError(f"row {row.row_id} conflicts with the Source-1376 close recovery record.")
        final_metadata = require_v1_metadata(task_bytes(path).decode("utf-8"))
        if final_metadata.status != "done" or final_metadata.pending_task_items:
            raise TaskFrontmatterError(f"row {row.row_id} did not reach terminal queue-empty metadata.")
    fsync_task_directories(todo, *paths)
    if preserved_pane and exact_pane_id(target) != preserved_pane:
        raise TaskFrontmatterError("protected shared-target pane changed after metadata closure.")
    existing_receipt = read_private_json(receipt_path)
    if existing_receipt is None:
        write_private_json(receipt_path, receipt, final=True)
    elif existing_receipt[0] != receipt:
        raise TaskFrontmatterError("Source-1376 close receipt conflicts with its recovery record.")
    remove_private_json(journal_path, journal_payload)
    return receipt


def finish_close_bookkeeping(
    args: Args,
    rows: dict[str, PlanRow],
    journal_path: Path,
    journal_value: dict[str, object],
    journal_payload: bytes,
) -> dict[str, object]:
    selected = close_journal_rows(journal_value, rows)
    target = selected[0].runat
    receipt_path = close_receipt_path(args.receipt_dir, tuple(row.row_id for row in selected))
    task_paths = {plan_path(args.root, row) for row in selected}
    protected = string_mapping(journal_value.get("protected_sha256", {}), "close protected-task map")
    protected_paths = {(args.root / task_ref).resolve() for task_ref in protected}
    with root_membership_lock(args.root), task_target_lock(args.root, target):
        with ExitStack() as locks:
            for locked_path in sorted(
                {
                    *task_paths,
                    *protected_paths,
                    *closure_custody_paths(args, rows),
                    args.root / "TODO.md",
                    journal_path,
                    receipt_path,
                }
            ):
                locks.enter_context(task_file_lock(locked_path))
            validate_operation_bindings_locked(args, rows)
            return finish_close_bookkeeping_locked(args, rows, journal_path, journal_value, journal_payload)


def prepare_metadata_close(
    args: Args,
    rows: dict[str, PlanRow],
    row: PlanRow,
    current_text: str,
    current_before: os.stat_result,
    mode: str,
    protected_row: PlanRow | None,
    pane_before: str,
) -> dict[str, object]:
    task = plan_path(args.root, row)
    row_ids = (row.row_id,)
    receipt_path = close_receipt_path(args.receipt_dir, row_ids)
    journal_path = close_journal_path(args.receipt_dir, row_ids)
    note = close_record(args, row, mode, receipt_path, target=row.runat, protected_row=protected_row.row_id if protected_row else "")
    updated_task = update_frontmatter_status(append_comment(current_text, note), "done", "", args.root)
    todo = args.root / "TODO.md"
    protected_sha256: dict[str, str] = {}
    with root_membership_lock(args.root), task_target_lock(args.root, row.runat):
        with ExitStack() as locks:
            lock_paths = {
                task,
                todo,
                journal_path,
                receipt_path,
                *closure_custody_paths(args, rows),
            }
            protected_path: Path | None = None
            if protected_row is not None:
                protected_path = plan_path(args.root, protected_row)
                lock_paths.add(protected_path)
            for locked_path in sorted(lock_paths):
                locks.enter_context(task_file_lock(locked_path))
            validate_operation_bindings_locked(args, rows)
            validate_escrow_custody_locked(args, rows)
            if task.read_text(encoding="utf-8") != current_text or not same_file_state(current_before, task.stat()):
                raise TaskFrontmatterError(f"row {row.row_id} changed during metadata closure preparation.")
            if sha256(current_text.encode()) != expected_current_task_sha256(args, rows, row):
                raise TaskFrontmatterError(f"row {row.row_id} no longer matches reviewed custody before metadata closure.")
            if protected_path is not None and protected_row is not None:
                protected_payload = task_bytes(protected_path)
                _ = validate_original_row(args.root, protected_row, protected_payload)
                expected_owners = {task} if protected_row.status == "done" else {task, protected_path}
                if set(authoritative_active_target_task_paths(args.root, row.runat)) != expected_owners:
                    raise TaskFrontmatterError(f"row {row.row_id} shared-target ownership drifted.")
                if mode == "preserved-shared-target":
                    if not pane_before or exact_pane_id(row.runat) != pane_before:
                        raise TaskFrontmatterError(f"row {row.row_id} external survivor pane is not stable and live.")
                elif pane_before or exact_pane_id(row.runat):
                    raise TaskFrontmatterError(f"row {row.row_id} protected shared target became live before metadata closure.")
                protected_sha256[protected_row.task_ref] = sha256(protected_payload)
            elif exact_pane_id(row.runat):
                raise TaskFrontmatterError(f"row {row.row_id} target became live before metadata-only closure.")
            todo_text = todo.read_text(encoding="utf-8")
            updated_todo, todo_before_sections, todo_after_sections = prepare_todo_close(
                args.root,
                task,
                todo_text,
                row.runat,
            )
            receipt: dict[str, object] = {
                "schema": "omo-source1376-close/v1",
                "rows": [row.row_id],
                "plan_sha256": PLAN_SHA256,
                "execution_binding_sha256": EXECUTION_BINDING_SHA256,
                "prepared_binding_sha256": prepared_binding_digest(args),
                "authority_sha256": AUTHORITY_SHA256,
                "mode": mode,
                "task": row.task_ref,
                "task_before_sha256": sha256(current_text.encode()),
                "task_after_sha256": sha256(updated_task.encode()),
                "target": row.runat,
                "pane_before": pane_before,
                "pane_after": pane_before,
                "session_id": "",
                "mail": "suppressed",
                "note": note,
                "todo_before_sections": list(todo_before_sections),
                "todo_after_sections": list(todo_after_sections),
            }
            journal: dict[str, object] = {
                "schema": "omo-source1376-close-journal/v1",
                "phase": "bookkeeping",
                "rows": [row.row_id],
                "target": row.runat,
                "receipt_path": str(receipt_path),
                "tasks_before": {row.task_ref: current_text},
                "tasks_after": {row.task_ref: updated_task},
                "todo_before": todo_text,
                "todo_after": updated_todo,
                "protected_sha256": protected_sha256,
                "preserved_pane": pane_before if protected_row is not None else "",
                "receipt": receipt,
            }
            journal_payload = write_private_json(journal_path, journal, final=False)
            return finish_close_bookkeeping_locked(args, rows, journal_path, journal, journal_payload)


def prepare_missing_shared_close(
    args: Args,
    rows: dict[str, PlanRow],
    selected: tuple[PlanRow, ...],
    values: tuple[tuple[Path, os.stat_result, str], ...],
) -> dict[str, object]:
    row_ids = tuple(row.row_id for row in selected)
    target = selected[0].runat
    receipt_path = close_receipt_path(args.receipt_dir, row_ids)
    journal_path = close_journal_path(args.receipt_dir, row_ids)
    notes = [close_record(args, row, "coordinated-shared-missing-target", receipt_path, target=target, paired_rows=list(row_ids)) for row in selected]
    originals = {row.task_ref: text for row, (_path, _before, text) in zip(selected, values, strict=True)}
    tasks_after = {row.task_ref: update_frontmatter_status(append_comment(originals[row.task_ref], note), "done", "", args.root) for row, note in zip(selected, notes, strict=True)}
    todo = args.root / "TODO.md"
    paths = {value[0] for value in values}
    with root_membership_lock(args.root), task_target_lock(args.root, target):
        with ExitStack() as locks:
            for locked_path in sorted(
                {
                    *paths,
                    todo,
                    journal_path,
                    receipt_path,
                    *closure_custody_paths(args, rows),
                }
            ):
                locks.enter_context(task_file_lock(locked_path))
            validate_operation_bindings_locked(args, rows)
            validate_escrow_custody_locked(args, rows)
            if exact_pane_id(target):
                raise TaskFrontmatterError("shared target became live before metadata-only closure.")
            if set(authoritative_active_target_task_paths(args.root, target)) != paths:
                raise TaskFrontmatterError("shared missing-target ownership changed before closure.")
            for row, (path, before, text) in zip(selected, values, strict=True):
                if path.read_text(encoding="utf-8") != text or not same_file_state(before, path.stat()):
                    raise TaskFrontmatterError(f"row {row.row_id} changed before shared missing-target closure.")
                if sha256(text.encode()) != expected_current_task_sha256(args, rows, row):
                    raise TaskFrontmatterError(f"row {row.row_id} no longer matches reviewed custody before shared closure.")
            todo_text = todo.read_text(encoding="utf-8")
            updated_todo = todo_text
            todo_before_sections: dict[str, list[str]] = {}
            todo_after_sections: dict[str, list[str]] = {}
            for row in selected:
                updated_todo, before_sections, after_sections = prepare_todo_close(
                    args.root,
                    plan_path(args.root, row),
                    updated_todo,
                    target,
                )
                todo_before_sections[row.row_id] = list(before_sections)
                todo_after_sections[row.row_id] = list(after_sections)
            receipt: dict[str, object] = {
                "schema": "omo-source1376-close/v1",
                "rows": list(row_ids),
                "plan_sha256": PLAN_SHA256,
                "execution_binding_sha256": EXECUTION_BINDING_SHA256,
                "prepared_binding_sha256": prepared_binding_digest(args),
                "authority_sha256": AUTHORITY_SHA256,
                "mode": "coordinated-shared-missing-target",
                "tasks": [row.task_ref for row in selected],
                "task_before_sha256": [sha256(originals[row.task_ref].encode()) for row in selected],
                "task_after_sha256": [sha256(tasks_after[row.task_ref].encode()) for row in selected],
                "target": target,
                "pane_before": "",
                "pane_after": "",
                "session_id": "",
                "mail": "suppressed",
                "notes": notes,
                "todo_before_sections": todo_before_sections,
                "todo_after_sections": todo_after_sections,
            }
            journal: dict[str, object] = {
                "schema": "omo-source1376-close-journal/v1",
                "phase": "bookkeeping",
                "rows": list(row_ids),
                "target": target,
                "receipt_path": str(receipt_path),
                "tasks_before": originals,
                "tasks_after": tasks_after,
                "todo_before": todo_text,
                "todo_after": updated_todo,
                "protected_sha256": {},
                "preserved_pane": "",
                "receipt": receipt,
            }
            journal_payload = write_private_json(journal_path, journal, final=False)
            return finish_close_bookkeeping_locked(args, rows, journal_path, journal, journal_payload)


def pane_phase_task_maps(value: dict[str, object]) -> tuple[dict[str, str], dict[str, str]]:
    originals = string_mapping(value.get("tasks_original"), "pane-close original-task map")
    in_progress = string_mapping(value.get("tasks_in_progress"), "pane-close in-progress-task map")
    if set(originals) != set(in_progress):
        raise TaskFrontmatterError("Source-1376 pane-close task maps disagree.")
    return originals, in_progress


def validate_pane_journal_derivation(
    args: Args,
    rows: dict[str, PlanRow],
    selected: tuple[PlanRow, ...],
    journal_value: dict[str, object],
    originals: dict[str, str],
    in_progress: dict[str, str],
) -> None:
    row_ids = tuple(row.row_id for row in selected)
    target = selected[0].runat
    pane_before = journal_value.get("pane_before")
    notes = journal_value.get("notes")
    receipt_path = close_receipt_path(args.receipt_dir, row_ids)
    if (
        not isinstance(pane_before, str)
        or not pane_before
        or journal_value.get("target") != target
        or journal_value.get("receipt_path") != str(receipt_path)
        or not isinstance(notes, list)
        or len(notes) != len(selected)
        or any(not isinstance(note, str) for note in notes)
    ):
        raise TaskFrontmatterError("Source-1376 pane-close journal identity is malformed.")
    if row_ids == (COMPLETED_SHELL_ROW,) and pane_before != COMPLETED_SHELL_PANE:
        raise TaskFrontmatterError("Source-1232 exited shell is not the reviewed exact pane.")
    expected_notes = [
        close_record(
            args,
            row,
            "coordinated-shared-target" if len(selected) > 1 else "exited-shell" if row.row_id == COMPLETED_SHELL_ROW else "live",
            receipt_path,
            target=target,
            pane_id=pane_before,
            paired_rows=list(row_ids) if len(selected) > 1 else [],
        )
        for row in selected
    ]
    if notes != expected_notes:
        raise TaskFrontmatterError("Source-1376 pane-close journal provenance changed.")
    for row, note in zip(selected, expected_notes, strict=True):
        original = originals.get(row.task_ref)
        prepared = in_progress.get(row.task_ref)
        if (
            original is None
            or prepared is None
            or sha256(original.encode()) != expected_current_task_sha256(args, rows, row)
            or prepared
            != update_frontmatter_status(
                append_comment(original, note),
                "blocked",
                DONE_CLOSE_IN_PROGRESS,
                args.root,
            )
        ):
            raise TaskFrontmatterError(f"row {row.row_id} pane-close journal does not rederive from reviewed custody.")


def closure_custody_paths(args: Args, rows: dict[str, PlanRow]) -> tuple[Path, ...]:
    """Return every path that must stay locked while a closure is authorized."""

    return tuple(
        sorted(
            {
                *markdown_paths(args.root),
                *operation_binding_paths(args, rows),
                *args.receipt_dir.glob("transfer-*.json"),
                args.root / TRANSFER_JOURNAL,
                args.root / LEGACY_TRANSFER_JOURNAL,
            }
        )
    )


def validate_escrow_custody_locked(args: Args, rows: dict[str, PlanRow]) -> None:
    """Re-derive the complete escrow queue and its only active target owner."""

    if path_entry_exists(args.root / TRANSFER_JOURNAL):
        raise TaskFrontmatterError("Source-1376 cannot close a row while a transfer journal is active.")
    if path_entry_exists(args.root / LEGACY_TRANSFER_JOURNAL):
        raise TaskFrontmatterError("Source-1376 cannot close a row while legacy transfer recovery state exists.")
    paths = markdown_paths(args.root)
    destination = (args.root / DESTINATION_REF).resolve()
    if destination not in paths:
        raise TaskFrontmatterError("Source-1376 escrow owner is absent from the locked task inventory.")
    _custody, moved, rolling_destination = transfer_custody_by_row(args, validated_transfer_receipts(args, rows), rows)
    destination_payload = task_bytes(destination)
    destination_metadata = require_v1_metadata(destination_payload.decode("utf-8"))
    if (
        sha256(destination_payload) != rolling_destination
        or destination_metadata.status != "long_running"
        or not destination_metadata.is_manager
        or destination_metadata.runat != "cedit:15"
        or destination_metadata.managerat != "wl:4"
        or destination_metadata.pending_task_items != (AUTHORITY_TEXT, *moved)
        or task_row_sections(args.root, destination) != ("current",)
    ):
        raise TaskFrontmatterError("Source-1376 escrow owner drifted from reviewed rolling custody.")
    parsed, malformed = active_metadata(args.root, paths)
    if tuple(path for path, metadata in parsed.items() if metadata.runat == "cedit:15") != (destination,):
        raise TaskFrontmatterError("Source-1376 escrow target no longer has exactly one active owner.")
    if any(raw_target_claims(text, "cedit:15") for text in malformed.values()):
        raise TaskFrontmatterError("a malformed record claims the Source-1376 escrow target.")


def stop_bound_target(args: Args, target: str, pane_before: str, task_ref: str = "") -> str:
    stop_args = StopArgs(
        target=target,
        wait_s=10.0,
        lines=2000,
        dry_run=False,
        allow_self=False,
        root=args.root,
        task_file=task_ref,
        no_feedback=True,
        feedback_wait_s=0.0,
        bound_symbolic_target=target,
        bound_pane_id=pane_before,
    )
    return stop(stop_args)


def finish_pane_phase(
    args: Args,
    rows: dict[str, PlanRow],
    journal_path: Path,
    journal_value: dict[str, object],
    journal_payload: bytes,
    session_id: str,
) -> dict[str, object]:
    selected = close_journal_rows(journal_value, rows)
    row_ids = tuple(row.row_id for row in selected)
    target = journal_value.get("target")
    pane_before = journal_value.get("pane_before")
    closed_at = journal_value.get("closed_at")
    if journal_value.get("phase") != "pane" or not isinstance(target, str) or not isinstance(pane_before, str) or not isinstance(closed_at, str) or any(row.runat != target for row in selected):
        raise TaskFrontmatterError("Source-1376 pane-close recovery binding changed.")
    originals, in_progress = pane_phase_task_maps(journal_value)
    if set(originals) != {row.task_ref for row in selected}:
        raise TaskFrontmatterError("Source-1376 pane-close rows do not match their task records.")
    todo = args.root / "TODO.md"
    paths = {plan_path(args.root, row) for row in selected}
    with root_membership_lock(args.root), task_target_lock(args.root, target):
        with ExitStack() as locks:
            for locked_path in sorted(
                {
                    *paths,
                    *closure_custody_paths(args, rows),
                    todo,
                    journal_path,
                }
            ):
                locks.enter_context(task_file_lock(locked_path))
            validate_operation_bindings_locked(args, rows)
            validate_escrow_custody_locked(args, rows)
            validate_pane_journal_derivation(args, rows, selected, journal_value, originals, in_progress)
            for row in selected:
                if task_bytes(plan_path(args.root, row)) != in_progress[row.task_ref].encode():
                    raise TaskFrontmatterError(f"row {row.row_id} changed after its durable close intent.")
            if exact_pane_id(target) or exact_pane_id(pane_before):
                raise TaskFrontmatterError("Source-1376 target remains live before terminal bookkeeping.")
            try:
                close_time = datetime.fromisoformat(closed_at)
            except ValueError as exc:
                raise TaskFrontmatterError("Source-1376 pane-close timestamp is invalid.") from exc
            tasks_after: dict[str, str] = {}
            for row in selected:
                tasks_after[row.task_ref] = update_frontmatter_status(
                    in_progress[row.task_ref].rstrip("\n") + close_note(target, session_id, close_time),
                    "done",
                    "",
                    args.root,
                )
            todo_text = todo.read_text(encoding="utf-8")
            updated_todo = todo_text
            todo_before_sections: dict[str, list[str]] = {}
            todo_after_sections: dict[str, list[str]] = {}
            for row in selected:
                updated_todo, before_sections, after_sections = prepare_todo_close(
                    args.root,
                    plan_path(args.root, row),
                    updated_todo,
                    target,
                )
                todo_before_sections[row.row_id] = list(before_sections)
                todo_after_sections[row.row_id] = list(after_sections)
            notes = journal_value.get("notes")
            if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
                raise TaskFrontmatterError("Source-1376 pane-close notes are malformed.")
            mode = "exited-shell" if row_ids == (COMPLETED_SHELL_ROW,) else "coordinated-shared-target" if len(row_ids) > 1 else "live"
            receipt: dict[str, object] = {
                "schema": "omo-source1376-close/v1",
                "rows": list(row_ids),
                "plan_sha256": PLAN_SHA256,
                "execution_binding_sha256": EXECUTION_BINDING_SHA256,
                "prepared_binding_sha256": prepared_binding_digest(args),
                "authority_sha256": AUTHORITY_SHA256,
                "mode": mode,
                "tasks": [row.task_ref for row in selected],
                "task_before_sha256": [sha256(originals[row.task_ref].encode()) for row in selected],
                "task_after_sha256": [sha256(tasks_after[row.task_ref].encode()) for row in selected],
                "target": target,
                "pane_before": pane_before,
                "pane_after": "",
                "session_id": session_id,
                "mail": "suppressed",
                "notes": notes,
                "todo_before_sections": todo_before_sections,
                "todo_after_sections": todo_after_sections,
            }
            bookkeeping = {
                **journal_value,
                "phase": "bookkeeping",
                "tasks_before": in_progress,
                "tasks_after": tasks_after,
                "todo_before": todo_text,
                "todo_after": updated_todo,
                "protected_sha256": {},
                "preserved_pane": "",
                "receipt": receipt,
            }
            bookkeeping_payload = replace_private_json(journal_path, journal_payload, bookkeeping, final=False)
    return finish_close_bookkeeping(args, rows, journal_path, bookkeeping, bookkeeping_payload)


def resume_pane_close(
    args: Args,
    rows: dict[str, PlanRow],
    journal_path: Path,
    journal_value: dict[str, object],
    journal_payload: bytes,
) -> dict[str, object]:
    selected = close_journal_rows(journal_value, rows)
    target = journal_value.get("target")
    pane_before = journal_value.get("pane_before")
    if journal_value.get("phase") != "pane" or not isinstance(target, str) or not isinstance(pane_before, str):
        raise TaskFrontmatterError("Source-1376 pane-close recovery record is not resumable.")
    originals, in_progress = pane_phase_task_maps(journal_value)
    paths = {plan_path(args.root, row) for row in selected}
    with root_membership_lock(args.root), task_target_lock(args.root, target):
        with ExitStack() as locks:
            for locked_path in sorted(
                {
                    *paths,
                    *closure_custody_paths(args, rows),
                    journal_path,
                }
            ):
                locks.enter_context(task_file_lock(locked_path))
            validate_operation_bindings_locked(args, rows)
            validate_escrow_custody_locked(args, rows)
            validate_pane_journal_derivation(args, rows, selected, journal_value, originals, in_progress)
            symbolic_pane = exact_pane_id(target)
            numeric_pane = exact_pane_id(pane_before)
            if symbolic_pane not in {"", pane_before} or numeric_pane not in {"", pane_before} or bool(symbolic_pane) != bool(numeric_pane):
                raise TaskFrontmatterError("Source-1376 pane identity changed during close recovery.")
            if len(selected) == 1 and selected[0].row_id == COMPLETED_SHELL_ROW and (symbolic_pane != COMPLETED_SHELL_PANE or numeric_pane != COMPLETED_SHELL_PANE):
                raise TaskFrontmatterError("Source-1232 reviewed exited shell is absent or changed.")
            for row in selected:
                path = plan_path(args.root, row)
                state, current = task_snapshot(path)
                if current == originals[row.task_ref].encode():
                    replace_if_unchanged_locked(path, in_progress[row.task_ref], state)
                elif current != in_progress[row.task_ref].encode():
                    raise TaskFrontmatterError(f"row {row.row_id} conflicts with pane-close recovery state.")
            session_id = str(journal_value.get("session_id", ""))
            if symbolic_pane:
                validate_operation_bindings_locked(args, rows)
                validate_escrow_custody_locked(args, rows)
                validate_pane_journal_derivation(args, rows, selected, journal_value, originals, in_progress)
                if len(selected) == 1 and selected[0].row_id == COMPLETED_SHELL_ROW:
                    original = originals[selected[0].task_ref].encode()
                    session_payload = safe_read(
                        COMPLETED_SHELL_SESSION_PATH,
                        expected_sha256=COMPLETED_SHELL_SESSION_SHA256,
                        mode=0o644,
                        label="Source-1232 exited Codex session",
                    )
                    if len(session_payload) != COMPLETED_SHELL_SESSION_SIZE:
                        raise TaskFrontmatterError("Source-1232 exited Codex session size changed.")
                    close_exited_codex_shell_with_task_receipt(
                        target,
                        pane_before,
                        COMPLETED_SHELL_SESSION_ID,
                        original,
                        sha256(original),
                        COMPLETED_SHELL_EVIDENCE,
                        COMPLETED_SHELL_MESSAGE_ID,
                        session_payload=session_payload,
                        expected_session_sha256=COMPLETED_SHELL_SESSION_SHA256,
                        expected_completion_command=COMPLETED_SHELL_COMPLETION_COMMAND,
                    )
                    session_id = COMPLETED_SHELL_SESSION_ID
                else:
                    session_id = stop_bound_target(args, target, pane_before, selected[0].task_ref if len(selected) == 1 else "")
            if exact_pane_id(target) or exact_pane_id(pane_before):
                raise TaskFrontmatterError("Source-1376 target remained live after supported close.")
    return finish_pane_phase(args, rows, journal_path, journal_value, journal_payload, session_id)


def prepare_pane_close(
    args: Args,
    rows: dict[str, PlanRow],
    selected: tuple[PlanRow, ...],
    values: tuple[tuple[Path, os.stat_result, str], ...],
    pane_before: str,
) -> dict[str, object]:
    row_ids = tuple(row.row_id for row in selected)
    target = selected[0].runat
    if row_ids == (COMPLETED_SHELL_ROW,) and pane_before != COMPLETED_SHELL_PANE:
        raise TaskFrontmatterError("Source-1232 exited shell is not the reviewed exact pane.")
    receipt_path = close_receipt_path(args.receipt_dir, row_ids)
    journal_path = close_journal_path(args.receipt_dir, row_ids)
    notes = [
        close_record(
            args,
            row,
            "coordinated-shared-target" if len(selected) > 1 else "exited-shell" if row.row_id == COMPLETED_SHELL_ROW else "live",
            receipt_path,
            target=target,
            pane_id=pane_before,
            paired_rows=list(row_ids) if len(selected) > 1 else [],
        )
        for row in selected
    ]
    originals = {row.task_ref: text for row, (_path, _before, text) in zip(selected, values, strict=True)}
    in_progress = {
        row.task_ref: update_frontmatter_status(append_comment(originals[row.task_ref], note), "blocked", DONE_CLOSE_IN_PROGRESS, args.root) for row, note in zip(selected, notes, strict=True)
    }
    journal: dict[str, object] = {
        "schema": "omo-source1376-close-journal/v1",
        "phase": "pane",
        "rows": list(row_ids),
        "target": target,
        "pane_before": pane_before,
        "closed_at": datetime.now().astimezone().isoformat(),
        "receipt_path": str(receipt_path),
        "tasks_original": originals,
        "tasks_in_progress": in_progress,
        "notes": notes,
        "session_id": "",
    }
    paths = {value[0] for value in values}
    with root_membership_lock(args.root), task_target_lock(args.root, target):
        with ExitStack() as locks:
            for locked_path in sorted(
                {
                    *paths,
                    *closure_custody_paths(args, rows),
                    journal_path,
                    receipt_path,
                }
            ):
                locks.enter_context(task_file_lock(locked_path))
            validate_operation_bindings_locked(args, rows)
            validate_escrow_custody_locked(args, rows)
            if exact_pane_id(target) != pane_before or exact_pane_id(pane_before) != pane_before:
                raise TaskFrontmatterError("Source-1376 pane changed before durable close intent.")
            if set(authoritative_active_target_task_paths(args.root, target)) != paths:
                raise TaskFrontmatterError("Source-1376 target ownership changed before durable close intent.")
            for row, (path, before, text) in zip(selected, values, strict=True):
                if path.read_text(encoding="utf-8") != text or not same_file_state(before, path.stat()):
                    raise TaskFrontmatterError(f"row {row.row_id} changed before durable close intent.")
                if sha256(text.encode()) != expected_current_task_sha256(args, rows, row):
                    raise TaskFrontmatterError(f"row {row.row_id} no longer matches reviewed custody before close intent.")
            validate_pane_journal_derivation(args, rows, selected, journal, originals, in_progress)
            journal_payload = write_private_json(journal_path, journal, final=False)
            for row, (path, before, _text) in zip(selected, values, strict=True):
                replace_if_unchanged_locked(path, in_progress[row.task_ref], before)
            fsync_task_directories(*paths)
    return resume_pane_close(args, rows, journal_path, journal, journal_payload)


def close_row(args: Args, rows: dict[str, PlanRow], row_id: str) -> dict[str, object]:
    validate_operation_bindings(args, rows)
    if row_id in PROTECTED_ROWS:
        raise TaskFrontmatterError(f"plan row {row_id} is outside Source-1376 closure authority.")
    row = rows[row_id]
    receipt_path = close_receipt_path(args.receipt_dir, (row_id,))
    journal_path = close_journal_path(args.receipt_dir, (row_id,))
    journal = read_private_json(journal_path)
    if journal is not None:
        phase = journal[0].get("phase")
        if phase == "bookkeeping":
            return finish_close_bookkeeping(args, rows, journal_path, journal[0], journal[1])
        if phase == "pane":
            return resume_pane_close(args, rows, journal_path, journal[0], journal[1])
        raise TaskFrontmatterError("Source-1376 close recovery record has an unknown phase.")
    prior = read_private_json(receipt_path)
    if prior is not None:
        validate_close_receipt(args, rows, (row_id,), prior[0])
        return prior[0]
    task = plan_path(args.root, row)
    before, payload = task_snapshot(task)
    expected_sha256 = expected_current_task_sha256(args, rows, row)
    if sha256(payload) != expected_sha256:
        raise TaskFrontmatterError(f"row {row_id} bytes do not match reviewed custody before closure.")
    text = payload.decode("utf-8")
    metadata = require_v1_metadata(text)
    validate_plan_metadata(row, metadata, require_original_status=row.n_items == 0)
    if metadata.pending_task_items or has_pending_marker(text):
        raise TaskFrontmatterError(f"row {row_id} is not queue-empty before closure.")
    ensure_manager_has_no_active_children(args.root, task, metadata)
    if metadata.is_manager and live_source1352_senders():
        raise TaskFrontmatterError("a Source-1352 sender remains live before manager closure.")
    if metadata.runat.partition(":")[0].startswith("h"):
        raise TaskFrontmatterError("Source-1376 cannot close a human-owned target.")
    protected_row = rows[EXTERNAL_SHARED_ROWS[row_id]] if row_id in EXTERNAL_SHARED_ROWS else None
    pane_before = exact_pane_id(row.runat)
    if protected_row is not None:
        mode = "protected-shared-missing-target" if protected_row.status == "done" and not pane_before else "preserved-shared-target"
        return prepare_metadata_close(args, rows, row, text, before, mode, protected_row, pane_before)
    owners = authoritative_active_target_task_paths(args.root, row.runat)
    if owners != (task,):
        refs = ", ".join(relative_task_ref(args.root, owner) for owner in owners) or "none"
        raise TaskFrontmatterError(f"row {row_id} is not the sole active target owner before closure: {refs}.")
    if not pane_before:
        return prepare_metadata_close(args, rows, row, text, before, "missing-target", None, "")
    return prepare_pane_close(args, rows, (row,), ((task, before, text),), pane_before)


def close_internal_shared_pair(args: Args, rows: dict[str, PlanRow]) -> dict[str, object]:
    validate_operation_bindings(args, rows)
    receipt_path = close_receipt_path(args.receipt_dir, INTERNAL_SHARED_PAIR)
    journal_path = close_journal_path(args.receipt_dir, INTERNAL_SHARED_PAIR)
    journal = read_private_json(journal_path)
    if journal is not None:
        phase = journal[0].get("phase")
        if phase == "bookkeeping":
            return finish_close_bookkeeping(args, rows, journal_path, journal[0], journal[1])
        if phase == "pane":
            return resume_pane_close(args, rows, journal_path, journal[0], journal[1])
        raise TaskFrontmatterError("Source-1376 paired close recovery record has an unknown phase.")
    prior = read_private_json(receipt_path)
    if prior is not None:
        validate_close_receipt(args, rows, INTERNAL_SHARED_PAIR, prior[0])
        return prior[0]
    pair = tuple(rows[row_id] for row_id in INTERNAL_SHARED_PAIR)
    paths = tuple(plan_path(args.root, row) for row in pair)
    values: list[tuple[Path, os.stat_result, str]] = []
    for row, path in zip(pair, paths, strict=True):
        before, payload = task_snapshot(path)
        if sha256(payload) != expected_current_task_sha256(args, rows, row):
            raise TaskFrontmatterError(f"paired row {row.row_id} bytes do not match reviewed custody.")
        text = payload.decode("utf-8")
        metadata = require_v1_metadata(text)
        validate_plan_metadata(row, metadata, require_original_status=row.n_items == 0)
        if metadata.pending_task_items or has_pending_marker(text):
            raise TaskFrontmatterError(f"paired row {row.row_id} is not queue-empty.")
        ensure_manager_has_no_active_children(args.root, path, metadata)
        if metadata.is_manager and live_source1352_senders():
            raise TaskFrontmatterError("a Source-1352 sender remains live before shared-manager closure.")
        values.append((path, before, text))
    target = pair[0].runat
    if target.partition(":")[0].startswith("h") or any(row.runat != target for row in pair):
        raise TaskFrontmatterError("internal shared pair target is invalid.")
    owners = authoritative_active_target_task_paths(args.root, target)
    if set(owners) != set(paths):
        raise TaskFrontmatterError("internal shared pair ownership drifted.")
    pane_before = exact_pane_id(target)
    if not pane_before:
        return prepare_missing_shared_close(args, rows, pair, tuple(values))
    return prepare_pane_close(args, rows, pair, tuple(values), pane_before)


def transfer_schedule(rows: dict[str, PlanRow], *, include_deferred: bool) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = []
    skipped = {DEFERRED_TRANSFER_ROW} if not include_deferred else set(rows) - {DEFERRED_TRANSFER_ROW}
    consumed: set[str] = set()
    for row_id in sorted(rows):
        row = rows[row_id]
        if row_id in PROTECTED_ROWS or row_id in skipped or row.n_items == 0 or row_id in consumed:
            continue
        if row_id in INTERNAL_SHARED_PAIR and row_id == "71":
            continue
        if row_id == "44":
            groups.append(("44", "45"))
            consumed.update({"44", "45"})
        else:
            groups.append((row_id,))
            consumed.add(row_id)
    return tuple(groups)


def transfer_custody_by_row(
    args: Args,
    receipts: list[dict[str, object]],
    rows: dict[str, PlanRow],
) -> tuple[dict[str, tuple[str, str]], tuple[str, ...], str]:
    custody: dict[str, tuple[str, str]] = {}
    moved: list[str] = []
    rolling = initial_destination_sha256(args)
    for receipt in receipts:
        row_ids, items = validate_transfer_receipt_against_plan(receipt, rows, args)
        if receipt.get("destination_before_sha256") != rolling:
            raise TaskFrontmatterError("Source-1376 transfer receipt chain has a discontinuous destination preimage.")
        sources = receipt.get("sources")
        assert isinstance(sources, list)
        for row_id, source in zip(row_ids, sources, strict=True):
            assert isinstance(source, dict)
            if row_id in custody:
                raise TaskFrontmatterError(f"row {row_id} appears in more than one Source-1376 transfer receipt.")
            custody[row_id] = (str(source["after_sha256"]), str(source["sent_record"]))
        moved.extend(items)
        rolling = str(receipt["destination_after_sha256"])
    return custody, tuple(moved), rolling


def validate_close_journal_state_locked(
    args: Args,
    rows: dict[str, PlanRow],
    path: Path,
    value: dict[str, object],
) -> tuple[str, ...]:
    selected = close_journal_rows(value, rows)
    row_ids = tuple(row.row_id for row in selected)
    if path != close_journal_path(args.receipt_dir, row_ids):
        raise TaskFrontmatterError("Source-1376 close journal filename does not match its rows.")
    phase = value.get("phase")
    if phase == "pane":
        originals, in_progress = pane_phase_task_maps(value)
        validate_pane_journal_derivation(args, rows, selected, value, originals, in_progress)
        for row in selected:
            current = task_bytes(plan_path(args.root, row))
            if current not in {originals[row.task_ref].encode(), in_progress[row.task_ref].encode()}:
                raise TaskFrontmatterError(f"row {row.row_id} conflicts with its pane-close journal.")
        return row_ids
    if phase != "bookkeeping":
        raise TaskFrontmatterError("Source-1376 close journal has an unknown phase.")
    receipt = value.get("receipt")
    tasks_before = string_mapping(value.get("tasks_before"), "close journal task-before map")
    tasks_after = string_mapping(value.get("tasks_after"), "close journal task-after map")
    todo_before = value.get("todo_before")
    todo_after = value.get("todo_after")
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema") != "omo-source1376-close/v1"
        or receipt.get("rows") != list(row_ids)
        or value.get("receipt_path") != str(close_receipt_path(args.receipt_dir, row_ids))
        or set(tasks_before) != {row.task_ref for row in selected}
        or set(tasks_after) != set(tasks_before)
        or not isinstance(todo_before, str)
        or not isinstance(todo_after, str)
    ):
        raise TaskFrontmatterError("Source-1376 close bookkeeping journal is malformed.")
    before_value = receipt.get("task_before_sha256")
    before_digests = [before_value] if isinstance(before_value, str) else before_value
    after_value = receipt.get("task_after_sha256")
    after_digests = [after_value] if isinstance(after_value, str) else after_value
    if (
        before_digests != [expected_current_task_sha256(args, rows, row) for row in selected]
        or not isinstance(after_digests, list)
        or len(after_digests) != len(selected)
        or any(not isinstance(expected, str) or sha256(tasks_after[row.task_ref].encode()) != expected for row, expected in zip(selected, after_digests, strict=True))
    ):
        raise TaskFrontmatterError("Source-1376 close bookkeeping journal breaks task custody.")
    validate_close_bookkeeping_derivation(args, rows, selected, value, receipt, tasks_before, tasks_after)
    _ = validate_close_todo_evidence(args.root, selected, receipt, todo_before, todo_after)
    current_todo = task_bytes(args.root / "TODO.md")
    if current_todo not in {todo_before.encode(), todo_after.encode()}:
        raise TaskFrontmatterError("TODO conflicts with a Source-1376 close bookkeeping journal.")
    for row in selected:
        current = task_bytes(plan_path(args.root, row))
        if current not in {tasks_before[row.task_ref].encode(), tasks_after[row.task_ref].encode()}:
            raise TaskFrontmatterError(f"row {row.row_id} conflicts with its close bookkeeping journal.")
    if "tasks_original" in value:
        originals, in_progress = pane_phase_task_maps(value)
        validate_pane_journal_derivation(args, rows, selected, value, originals, in_progress)
        if tasks_before != in_progress:
            raise TaskFrontmatterError("Source-1376 pane-close bookkeeping journal lost its in-progress state.")
    return row_ids


def validate_transfer_journal_state_locked(
    args: Args,
    rows: dict[str, PlanRow],
    initial_paths: tuple[Path, ...],
    rolling_destination: str,
    value: dict[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...], str, bool]:
    receipt = value.get("receipt")
    if value.get("schema") != "omo-source1376-transfer-journal/v1" or not isinstance(receipt, dict):
        raise TaskFrontmatterError("Source-1376 transfer journal is malformed.")
    row_ids, moving = validate_transfer_receipt_against_plan(receipt, rows, args)
    selected = tuple(rows[row_id] for row_id in row_ids)
    destination = (args.root / DESTINATION_REF).resolve()
    sources_before = string_mapping(value.get("sources_before"), "transfer journal source-before map")
    sources_after = string_mapping(value.get("sources_after"), "transfer journal source-after map")
    destination_before = value.get("destination_before")
    destination_after = value.get("destination_after")
    markdown_values = value.get("markdown_paths")
    unchanged = string_mapping(value.get("unchanged_sha256"), "transfer journal unchanged map")
    source_receipts = receipt.get("sources")
    if (
        value.get("rows") != list(row_ids)
        or value.get("destination") != DESTINATION_REF
        or value.get("receipt_path") != str(transfer_receipt_path(args.receipt_dir, row_ids))
        or receipt.get("destination_before_sha256") != rolling_destination
        or not isinstance(destination_before, str)
        or not isinstance(destination_after, str)
        or sha256(destination_before.encode()) != rolling_destination
        or sha256(destination_after.encode()) != receipt.get("destination_after_sha256")
        or set(sources_before) != {row.task_ref for row in selected}
        or set(sources_after) != set(sources_before)
        or not isinstance(source_receipts, list)
        or not isinstance(markdown_values, list)
        or any(not isinstance(item, str) for item in markdown_values)
        or {Path(str(item)).resolve() for item in markdown_values} != set(initial_paths)
        or sha256(task_bytes(args.root / "TODO.md")) != receipt.get("todo_sha256")
    ):
        raise TaskFrontmatterError("Source-1376 transfer journal does not match reviewed rolling custody.")
    for row, source_receipt in zip(selected, source_receipts, strict=True):
        assert isinstance(source_receipt, dict)
        _ = validate_original_row(args.root, row, sources_before[row.task_ref].encode())
        if sha256(sources_after[row.task_ref].encode()) != source_receipt.get("after_sha256"):
            raise TaskFrontmatterError(f"row {row.row_id} transfer journal postimage changed.")
        current = task_bytes(plan_path(args.root, row))
        if current not in {sources_before[row.task_ref].encode(), sources_after[row.task_ref].encode()}:
            raise TaskFrontmatterError(f"row {row.row_id} conflicts with its transfer journal.")
    current_destination = task_bytes(destination)
    if current_destination not in {destination_before.encode(), destination_after.encode()}:
        raise TaskFrontmatterError("Source-1376 destination conflicts with its transfer journal.")
    affected = {destination, *(plan_path(args.root, row) for row in selected)}
    expected_unchanged = {relative_task_ref(args.root, path): sha256(task_bytes(path)) for path in initial_paths if path not in affected}
    if unchanged != expected_unchanged:
        raise TaskFrontmatterError("an unrelated task changed after the Source-1376 transfer journal was written.")
    destination_is_after = current_destination == destination_after.encode()
    return row_ids, moving, str(receipt["destination_after_sha256"]), destination_is_after


def validate_initial_state(args: Args, rows: dict[str, PlanRow]) -> None:
    destination = (args.root / DESTINATION_REF).resolve()
    with root_membership_lock(args.root):
        paths = markdown_paths(args.root)
        receipt_paths = tuple(args.receipt_dir.glob("transfer-*.json")) + tuple(args.receipt_dir.glob("close-*.json")) + tuple(args.receipt_dir.glob(".close-*.journal.json"))
        transfer_journal_path = args.root / TRANSFER_JOURNAL
        legacy_transfer_journal_path = args.root / LEGACY_TRANSFER_JOURNAL
        with ExitStack() as locks:
            for locked_path in sorted(
                {
                    *paths,
                    *receipt_paths,
                    *operation_binding_paths(args, rows),
                    args.root / "TODO.md",
                    transfer_journal_path,
                    legacy_transfer_journal_path,
                }
            ):
                locks.enter_context(task_file_lock(locked_path))
            validate_operation_bindings_locked(args, rows)
            if path_entry_exists(legacy_transfer_journal_path):
                raise TaskFrontmatterError("legacy closure-transfer recovery state is present.")
            planned_paths = {plan_path(args.root, row) for row in rows.values()}
            if not planned_paths.issubset(paths) or destination not in paths:
                raise TaskFrontmatterError("Source-1376 initial task-record inventory drifted.")
            todo_text = (args.root / "TODO.md").read_text(encoding="utf-8")
            for row in rows.values():
                task = plan_path(args.root, row)
                if len(todo_sections_in_text(args.root, task, todo_text)) > 1:
                    raise TaskFrontmatterError(f"plan row {row.row_id} has ambiguous TODO custody.")
            transfers = validated_transfer_receipts(args, rows)
            transfer_custody, moved, rolling_destination = transfer_custody_by_row(args, transfers, rows)
            closed_rows: set[str] = set()
            for receipt_path in args.receipt_dir.glob("close-*.json"):
                loaded = read_private_json(receipt_path, required=True)
                assert loaded is not None
                receipt = loaded[0]
                row_values = receipt.get("rows")
                if not isinstance(row_values, list) or any(not isinstance(row_id, str) or row_id not in rows for row_id in row_values):
                    raise TaskFrontmatterError("Source-1376 close receipt rows are malformed.")
                row_ids = tuple(row_values)
                if receipt_path != close_receipt_path(args.receipt_dir, row_ids) or closed_rows.intersection(row_ids):
                    raise TaskFrontmatterError("Source-1376 close receipts have duplicate rows or unexpected filenames.")
                validate_close_receipt(args, rows, row_ids, receipt)
                closed_rows.update(row_ids)
            close_journal_paths = tuple(args.receipt_dir.glob(".close-*.journal.json"))
            if len(close_journal_paths) > 1:
                raise TaskFrontmatterError("more than one Source-1376 close journal is active.")
            journal_rows: set[str] = set()
            if close_journal_paths:
                loaded = read_private_json(close_journal_paths[0], required=True)
                assert loaded is not None
                journal_rows.update(validate_close_journal_state_locked(args, rows, close_journal_paths[0], loaded[0]))
                if journal_rows - closed_rows and journal_rows & closed_rows:
                    raise TaskFrontmatterError("Source-1376 close journal only partially overlaps completed receipts.")
            transfer_journal = read_private_json(transfer_journal_path)
            transfer_journal_rows: set[str] = set()
            journal_moved: tuple[str, ...] = ()
            journal_destination_after = False
            if transfer_journal is not None:
                if close_journal_paths:
                    raise TaskFrontmatterError("transfer and close recovery journals cannot coexist.")
                journal_row_ids, journal_moved, journal_after_sha256, journal_destination_after = validate_transfer_journal_state_locked(
                    args,
                    rows,
                    paths,
                    rolling_destination,
                    transfer_journal[0],
                )
                transfer_journal_rows.update(journal_row_ids)
                if transfer_journal_rows.intersection(transfer_custody):
                    raise TaskFrontmatterError("Source-1376 transfer journal repeats a completed transfer row.")
            else:
                journal_after_sha256 = rolling_destination
            for row_id, row in rows.items():
                if row_id in PROTECTED_ROWS or row_id in closed_rows or row_id in journal_rows or row_id in transfer_journal_rows:
                    continue
                payload = task_bytes(plan_path(args.root, row))
                if row_id in transfer_custody:
                    expected_sha256, sent_record = transfer_custody[row_id]
                    text = payload.decode("utf-8")
                    metadata = require_v1_metadata(text)
                    if sha256(payload) != expected_sha256 or metadata.status != "blocked" or metadata.pending_task_items or sent_record not in text:
                        raise TaskFrontmatterError(f"transferred row {row_id} drifted before closure.")
                else:
                    _ = validate_original_row(args.root, row, payload)
            destination_payload = task_bytes(destination)
            destination_text = destination_payload.decode("utf-8")
            destination_metadata = require_v1_metadata(destination_text)
            expected_destination = journal_after_sha256 if journal_destination_after else rolling_destination
            expected_items = (AUTHORITY_TEXT, *moved, *(journal_moved if journal_destination_after else ()))
            if (
                sha256(destination_payload) != expected_destination
                or destination_metadata.status == "done"
                or not destination_metadata.is_manager
                or destination_metadata.runat != "cedit:15"
                or destination_metadata.pending_task_items != expected_items
                or task_row_sections(args.root, destination) != ("current",)
            ):
                raise TaskFrontmatterError("Source-1376 escrow owner drifted from reviewed rolling custody.")
            parsed, malformed = active_metadata(args.root, paths)
            if tuple(path for path, metadata in parsed.items() if metadata.runat == "cedit:15") != (destination,):
                raise TaskFrontmatterError("Source-1376 initial escrow target ownership is not singular.")
            if any(raw_target_claims(text, "cedit:15") for text in malformed.values()):
                raise TaskFrontmatterError("a malformed record claims the Source-1376 escrow target.")


def apply(args: Args) -> None:
    rows = effective_execution_rows(args)
    if args.prepared_binding is not None:
        initial_schedule = transfer_schedule(rows, include_deferred=False)
        if not initial_schedule:
            raise TaskFrontmatterError("Source-1376 execution has no eligible first transfer.")
        first_receipt = transfer_receipt_path(args.receipt_dir, initial_schedule[0])
        if not path_entry_exists(first_receipt) and not path_entry_exists(args.root / TRANSFER_JOURNAL):
            raise TaskFrontmatterError("Source-1376 initial execution requires --reviewed-handoff so review and first transfer share one lock acquisition.")
    authority_locator = validate_authority(args.root, args.authority)
    if authority_locator != f"{AUTHORITY_REF}:3-3":
        raise TaskFrontmatterError("Source-1376 authority locator drifted.")
    validate_initial_state(args, rows)
    interrupted_transfer = read_private_json(args.root / TRANSFER_JOURNAL)
    if interrupted_transfer is not None:
        row_values = interrupted_transfer[0].get("rows")
        receipt = interrupted_transfer[0].get("receipt")
        if (
            not isinstance(row_values, list)
            or any(not isinstance(row_id, str) or row_id not in rows for row_id in row_values)
            or not isinstance(receipt, dict)
            or not isinstance(receipt.get("destination_before_sha256"), str)
        ):
            raise TaskFrontmatterError("Source-1376 interrupted transfer cannot be routed for recovery.")
        recovered = transfer_rows(
            args,
            rows,
            tuple(row_values),
            str(receipt["destination_before_sha256"]),
        )
        print(f"recovered transfer row(s) {','.join(row_values)}", flush=True)
        if recovered != receipt:
            raise TaskFrontmatterError("Source-1376 recovered transfer receipt changed.")
    destination = args.root / DESTINATION_REF
    receipts = validated_transfer_receipts(args, rows)
    rolling_destination = initial_destination_sha256(args)
    for receipt in receipts:
        if receipt.get("destination_before_sha256") != rolling_destination:
            raise TaskFrontmatterError("existing transfer receipt chain is not contiguous.")
        rolling_destination = str(receipt.get("destination_after_sha256", ""))
    if sha256(task_bytes(destination)) != rolling_destination:
        raise TaskFrontmatterError("current destination does not match the completed transfer receipt chain.")
    nondeferred_schedule = transfer_schedule(rows, include_deferred=False)
    if not nondeferred_schedule:
        raise TaskFrontmatterError("Source-1376 execution has no eligible first transfer.")
    deferred_schedule = transfer_schedule(rows, include_deferred=True)
    if deferred_schedule != ((DEFERRED_TRANSFER_ROW,),):
        raise TaskFrontmatterError("deferred Source-1376 transfer schedule is invalid.")
    complete_transfer_schedule = (*nondeferred_schedule, *deferred_schedule)
    completed_groups_values: list[tuple[str, ...]] = []
    for receipt in receipts:
        receipt_rows = receipt.get("rows")
        if not isinstance(receipt_rows, list) or any(not isinstance(row_id, str) for row_id in receipt_rows):
            raise TaskFrontmatterError("completed Source-1376 transfer has malformed rows.")
        completed_groups_values.append(tuple(receipt_rows))
    completed_groups = tuple(completed_groups_values)
    if completed_groups != complete_transfer_schedule[: len(completed_groups)]:
        raise TaskFrontmatterError("completed transfers are not a prefix of the reviewed Source-1376 schedule.")
    first_group = nondeferred_schedule[0]
    if not receipts:
        receipt = transfer_rows(
            args,
            rows,
            first_group,
            rolling_destination,
            require_prepared_snapshot=args.prepared_binding is not None,
        )
        rolling_destination = str(receipt["destination_after_sha256"])
        receipts = [receipt]
        print(f"transferred first handoff row(s) {','.join(first_group)}", flush=True)
    elif receipts[0].get("rows") != list(first_group):
        raise TaskFrontmatterError("the first completed transfer does not match the reviewed prepared handoff.")
    for row_id in EARLY_CLOSE_ROWS:
        receipt = close_row(args, rows, row_id)
        print(f"closed row {row_id} mode={receipt['mode']}", flush=True)
    completed_nondeferred = min(len(receipts), len(nondeferred_schedule))
    for group in nondeferred_schedule[completed_nondeferred:]:
        receipt = transfer_rows(args, rows, group, rolling_destination)
        rolling_destination = str(receipt["destination_after_sha256"])
        print(f"transferred row(s) {','.join(group)}", flush=True)
    for row_id in PRE_DEFERRED_CLOSE_ROWS:
        receipt = close_row(args, rows, row_id)
        print(f"closed row {row_id} mode={receipt['mode']}", flush=True)
    deferred = deferred_schedule
    deferred_path = transfer_receipt_path(args.receipt_dir, deferred[0])
    if read_private_json(deferred_path) is None:
        receipt = transfer_rows(args, rows, deferred[0], rolling_destination)
        rolling_destination = str(receipt["destination_after_sha256"])
        print(f"transferred row {DEFERRED_TRANSFER_ROW}", flush=True)
    else:
        loaded = read_private_json(deferred_path, required=True)
        assert loaded is not None
        receipt = loaded[0]
        if receipt.get("destination_after_sha256") != rolling_destination:
            raise TaskFrontmatterError("deferred transfer receipt does not match rolling custody.")
    for entry in CLOSE_ORDER:
        if isinstance(entry, tuple):
            receipt = close_internal_shared_pair(args, rows)
            print(f"closed rows {','.join(entry)} mode={receipt['mode']}", flush=True)
        else:
            receipt = close_row(args, rows, entry)
            print(f"closed row {entry} mode={receipt['mode']}", flush=True)
    build_packet(args, rows, rolling_destination)


def task_row_sections(root: Path, task: Path) -> tuple[str, ...]:
    sections: list[str] = []
    section = ""
    for line in (root / "TODO.md").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
        elif task in todo_row_task_paths(root, line):
            sections.append(section)
    return tuple(sections)


def source1352_anchor() -> dict[str, object]:
    path = Path("/ssd1/sichangheagent/amh/SOURCE_1352_TERMINAL_ATTACH_COMMAND_REPORT_2026-09-01.md")
    expected = "d92245aa544111da5c653dafe9715432145f55812fef7485da9503346facb096"
    payload = safe_read(path, expected_sha256=expected, mode=0o444, label="Source-1352 terminal report")
    return {"path": str(path), "mode": "0444", "size": len(payload), "sha256": expected, "resent": False}


def stable_file_identity(path: Path, *, expected_sha256: str, mode: int, expected_size: int) -> dict[str, object]:
    before = path.lstat()
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        opened = os.fstat(fd)
    finally:
        os.close(fd)
    after = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size != expected_size
        or digest.hexdigest() != expected_sha256
        or not same_file_state(before, opened)
        or not same_file_state(before, after)
    ):
        raise TaskFrontmatterError(f"retained anchor changed: {path}")
    return {
        "path": str(path),
        "mode": f"{mode:04o}",
        "size": expected_size,
        "sha256": expected_sha256,
    }


def retained_long_term_anchors() -> dict[str, object]:
    install_receipt = stable_file_identity(
        Path("/tmp/source1200-install-retry85.UAhkGp/receipts/run-20260903T012152Z-237456/receipt/immutable_receipt_67f69e5cd63798914c06bc9d361007eef96900b3b6da02acc485e8ee13f9a582.tar"),
        expected_sha256="67f69e5cd63798914c06bc9d361007eef96900b3b6da02acc485e8ee13f9a582",
        mode=0o444,
        expected_size=393052160,
    )
    alias_repository = Path("/tmp/source1200-alias-correction.d9U98J/repo")
    alias_head = subprocess.run(
        ["git", "-C", str(alias_repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    alias_dirty = subprocess.run(
        ["git", "-C", str(alias_repository), "status", "--porcelain=v1", "--untracked-files=no"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if alias_head != "63d96909faeba4608f29a51bc1b528705c9f55a4" or alias_dirty:
        raise TaskFrontmatterError("Source-1200 alias-correction custody changed.")
    proof_root = Path("/tmp/amh-slice-c-proof.75rc8Il6")
    proof_state = proof_root.lstat()
    if not stat.S_ISDIR(proof_state.st_mode) or proof_state.st_uid != os.getuid() or stat.S_IMODE(proof_state.st_mode) != 0o700:
        raise TaskFrontmatterError("Slice-C proof-root custody changed.")
    implementation_repository = Path("/ssd1/sichangheagent/agent_managers")
    implementation_commits = (
        "f69e1c3ec906da81350fce52f6b3a92cf26651ae",
        "a51e96fb290807676f1f11be0f7b7f034a160f67",
        "316d347969fb4f96f1e886bfdcb66c71d9fbea01",
    )
    for commit in implementation_commits:
        subprocess.run(
            ["git", "-C", str(implementation_repository), "cat-file", "-e", f"{commit}^{{commit}}"],
            check=True,
            capture_output=True,
        )
    target90_path = Path("/ssd1/sichangheagent/work_logs/amh1200_ptr_recover.md")
    target90_payload, _target90_state = stable_owned_read(
        target90_path,
        modes=frozenset({0o644}),
        label="Source-1200 target-90 record",
    )
    target90_sha256 = "aa524ae162c5be08bcc0ce6062843f81fe74fb35cef32cc49f579b5d6e1c6673"
    if len(target90_payload) != 2093 or sha256(target90_payload) != target90_sha256:
        raise TaskFrontmatterError("Source-1200 target-90 record custody changed.")
    target90 = {
        "path": str(target90_path),
        "mode": "0644",
        "size": len(target90_payload),
        "sha256": target90_sha256,
    }
    target90_metadata = require_v1_metadata(target90_payload.decode("utf-8"))
    if target90_metadata.status != "done" or target90_metadata.pending_task_items:
        raise TaskFrontmatterError("Source-1200 target-90 record is not terminal and queue-empty.")
    evaluator_source = Path("/tmp/omo-agent-messages-30033/agent_done_2f72b75fce4bafac92f0d99a5922ed1597454ea2296201cecd51c29dcf87a1fc.md")
    evaluator_payload, _evaluator_state = stable_owned_read(
        evaluator_source,
        modes=frozenset({0o600}),
        label="Source-1352 evaluator source",
    )
    return {
        "source1352_evaluator": {
            "source": str(evaluator_source),
            "source_sha256_observed": sha256(evaluator_payload),
            "replay": "a4da37b48f7647155b0a76a8fb8823042684d70198ff59e6bbea573aa32c2a98",
            "transfer": "8fe7285d9379b9167ae75c3cc000ad40af71b7919caa17f3ce7d6c33e9f8332e",
        },
        "source1352_mail": {
            "message_id": "178832249055.2399970.7011806172868320195@gmail.com",
            "resent": False,
            "mailbox_inspected": False,
            "automatic_queue_removal_message": {
                "uid": 19333,
                "uidvalidity": 1,
                "message_id": "178831982815.1637852.11284158796006105593@gmail.com",
                "content_equivalence": "unauthenticated",
            },
        },
        "source1232_row17": {
            "message_id": COMPLETED_SHELL_MESSAGE_ID,
            "receipt": COMPLETED_SHELL_EVIDENCE,
            "session_path": str(COMPLETED_SHELL_SESSION_PATH),
            "session_sha256": COMPLETED_SHELL_SESSION_SHA256,
            "session_size": COMPLETED_SHELL_SESSION_SIZE,
            "resent": False,
        },
        "source1200_install_receipt": install_receipt,
        "source1200_alias_correction": {
            "repository": str(alias_repository),
            "head": alias_head,
            "tracked_worktree": "clean",
        },
        "slice_c": {
            "proof_root": str(proof_root),
            "proof_root_mode": "0700",
            "repository": str(implementation_repository),
            "commits_verified": list(implementation_commits),
        },
        "source1200_target90": target90,
    }


def live_source1352_senders() -> list[int]:
    tokens = (
        "SOURCE_1352_TERMINAL_ATTACH_COMMAND_REPORT_2026-09-01.md",
        "178832249055.2399970.7011806172868320195",
        "a4da37b48f7647155b0a76a8fb8823042684d70198ff59e6bbea573aa32c2a98",
    )
    matches: list[int] = []
    own_pid = os.getpid()
    for candidate in Path("/proc").iterdir():
        if not candidate.name.isdecimal() or int(candidate.name) == own_pid:
            continue
        try:
            command = (candidate / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if any(token in command for token in tokens):
            matches.append(int(candidate.name))
    return sorted(matches)


def build_packet_locked(args: Args, rows: dict[str, PlanRow], destination_sha256: str) -> None:
    prepared = prepared_binding_value(args)
    if prepared is None or args.prepared_binding is None or args.review_approval is None:
        raise TaskFrontmatterError("Source-1376 packet requires the reviewed prepared binding and approval.")
    approval, approval_payload = validate_review_approval(
        args,
        args.prepared_binding,
        prepared_binding_digest(args),
        prepared,
    )
    transfer_values = validated_transfer_receipts(args, rows)
    close_values: list[dict[str, object]] = []
    for path in sorted(args.receipt_dir.glob("close-*.json")):
        loaded = read_private_json(path, required=True)
        assert loaded is not None
        if loaded[0].get("schema") != "omo-source1376-close/v1":
            raise TaskFrontmatterError(f"unexpected closure receipt schema: {path.name}")
        close_values.append(loaded[0])
    closed_counts: Counter[str] = Counter()
    final_todo_sections: dict[str, tuple[str, ...]] = {}
    for receipt in close_values:
        receipt_rows = receipt.get("rows")
        if not isinstance(receipt_rows, list) or any(not isinstance(row_id, str) for row_id in receipt_rows):
            raise TaskFrontmatterError("closure receipt row list is invalid.")
        row_ids = tuple(receipt_rows)
        if any(row_id not in rows or row_id in PROTECTED_ROWS for row_id in row_ids):
            raise TaskFrontmatterError("closure receipt names an invalid or protected row.")
        selected = tuple(rows[row_id] for row_id in row_ids)
        validate_close_receipt(args, rows, row_ids, receipt)
        after = close_receipt_todo_sections(receipt, selected, "todo_after_sections")
        for row_id, sections in after.items():
            if row_id in final_todo_sections:
                raise TaskFrontmatterError(f"row {row_id} has duplicate closure TODO evidence.")
            final_todo_sections[row_id] = sections
        closed_counts.update(receipt_rows)
    if closed_counts != Counter({row_id: 1 for row_id in set(rows) - PROTECTED_ROWS}):
        raise TaskFrontmatterError("closure receipts do not cover every eligible Source-1376 row exactly once.")
    moved_items: list[str] = []
    moved_counts: Counter[str] = Counter()
    for receipt in transfer_values:
        sources = receipt.get("sources")
        if not isinstance(sources, list):
            raise TaskFrontmatterError("transfer receipt source list is invalid.")
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("items"), list):
                raise TaskFrontmatterError("transfer receipt source item list is invalid.")
            row_id = str(source.get("row"))
            moved_counts[row_id] += 1
            moved_items.extend(str(item) for item in source["items"])
    expected_moved = {row_id for row_id, row in rows.items() if row.n_items and row_id not in PROTECTED_ROWS}
    if moved_counts != Counter({row_id: 1 for row_id in expected_moved}):
        raise TaskFrontmatterError("transfer receipts do not cover every eligible nonempty queue exactly once.")
    destination = args.root / DESTINATION_REF
    destination_metadata = require_v1_metadata(destination.read_text(encoding="utf-8"))
    if (
        sha256(task_bytes(destination)) != destination_sha256
        or destination_metadata.pending_task_items != (AUTHORITY_TEXT, *moved_items)
        or authoritative_active_target_task_paths(args.root, destination_metadata.runat) != (destination.resolve(),)
    ):
        raise TaskFrontmatterError("final escrow destination does not preserve exact transferred order and ownership.")
    task_custody: list[dict[str, object]] = []
    for row_id, row in rows.items():
        task = plan_path(args.root, row)
        final_payload = task_bytes(task)
        metadata = require_v1_metadata(final_payload.decode("utf-8"))
        if row_id in PROTECTED_ROWS:
            if sha256(final_payload) != row.task_sha256 or metadata.status != row.status:
                raise TaskFrontmatterError(f"protected row {row_id} changed.")
        elif metadata.status != "done" or metadata.pending_task_items:
            raise TaskFrontmatterError(f"eligible row {row_id} is not terminal and queue-empty.")
        sections = task_row_sections(args.root, task)
        if row_id in PROTECTED_ROWS:
            if len(sections) > 1:
                raise TaskFrontmatterError(f"protected row {row_id} TODO custody is ambiguous.")
        elif sections != final_todo_sections.get(row_id):
            raise TaskFrontmatterError(f"row {row_id} TODO custody does not match its closure receipt.")
        task_custody.append(
            {
                "row": row_id,
                "task": row.task_ref,
                "target": row.runat,
                "original_sha256": row.task_sha256,
                "final_sha256": sha256(final_payload),
                "final_status": metadata.status,
                "final_queue_items": len(metadata.pending_task_items),
                "todo_sections": list(sections),
                "todo_indexed": bool(sections),
                "protected": row_id in PROTECTED_ROWS,
            }
        )
    active = active_metadata(args.root, markdown_paths(args.root))[0]
    for item in moved_items:
        owners = {path for path, metadata in active.items() if item in metadata.pending_task_items}
        if owners != {destination.resolve()}:
            raise TaskFrontmatterError(f"final transferred item ownership is not singular: {item}")
    closed_targets = {row.runat for row_id, row in rows.items() if row_id not in PROTECTED_ROWS and row_id not in EXTERNAL_SHARED_ROWS}
    live_closed_targets = {target: exact_pane_id(target) for target in sorted(closed_targets) if exact_pane_id(target)}
    if live_closed_targets:
        raise TaskFrontmatterError(f"closed Source-1376 targets remain live: {live_closed_targets}")
    senders = live_source1352_senders()
    if senders:
        raise TaskFrontmatterError(f"Source-1352 sender processes remain live: {senders}")
    close_journals = sorted(path.name for path in args.receipt_dir.glob(".close-*.journal.json"))
    if path_entry_exists(args.root / TRANSFER_JOURNAL) or path_entry_exists(args.root / LEGACY_TRANSFER_JOURNAL) or close_journals:
        raise TaskFrontmatterError("a closure transfer recovery record remains present.")
    receipt_manifest: list[dict[str, object]] = []
    for path in sorted((*args.receipt_dir.glob("transfer-*.json"), *args.receipt_dir.glob("close-*.json"))):
        payload, _state = stable_owned_read(
            path,
            modes=frozenset({0o400}),
            label=f"execution receipt {path.name}",
        )
        receipt_manifest.append(
            {
                "path": str(path),
                "mode": "0400",
                "size": len(payload),
                "sha256": sha256(payload),
            }
        )
    packet_body: dict[str, object] = {
        "schema": "omo-source1376-execution-packet/v1",
        "plan": {"path": str(args.plan), "mode": "0444", "sha256": PLAN_SHA256},
        "execution_binding": {
            "path": str(args.binding),
            "mode": "0444",
            "sha256": EXECUTION_BINDING_SHA256,
        },
        "prepared_binding": {
            "path": str(args.prepared_binding),
            "mode": "0444",
            "sha256": prepared_binding_digest(args),
        },
        "review_approval": {
            "path": str(args.review_approval),
            "mode": "0400",
            "sha256": sha256(approval_payload),
            "schema": approval["schema"],
            "reviewer": approval["reviewer"],
            "verdict": approval["verdict"],
            "public_key_sha256": approval["public_key_sha256"],
        },
        "authority": {"source": f"{AUTHORITY_REF}:3-3", "sha256": AUTHORITY_SHA256, "text": AUTHORITY_TEXT},
        "destination": {
            "task": DESTINATION_REF,
            "target": destination_metadata.runat,
            "sha256": destination_sha256,
            "items": len(destination_metadata.pending_task_items),
            "transferred_items": len(moved_items),
        },
        "transfer_receipts": transfer_values,
        "closure_receipts": close_values,
        "receipt_manifest": receipt_manifest,
        "task_custody": task_custody,
        "protected": {
            "external_survivors": [asdict(rows["29"]), asdict(rows["59"])],
            "root": asdict(rows["69"]),
            "human": asdict(rows["84"]),
            "root_blocker": "amh_manager.md remains open because hamh:1 lacks exact Human authority naming that action and session.",
        },
        "source1352": source1352_anchor(),
        "long_term_anchors": retained_long_term_anchors(),
        "source1352_live_sender_pids": senders,
        "mail_policy": "No mailbox access and no Human mail; the accepted Source-1352 notice was not resent.",
        "production": "untouched",
        "pcodx": "unused",
        "recovery_records_absent": [TRANSFER_JOURNAL, LEGACY_TRANSFER_JOURNAL, ".close-*.journal.json"],
        "process_feedback": (
            "The static plan required a plan-bound batch helper for historical and shared lifecycle dispositions, then a root-wide locked "
            "prepared-binding handoff because evidence writers could otherwise invalidate a reviewed snapshot before its first transfer. " + RECEIPT_DIRECTORY_MODE_CONTRACT
        ),
    }
    output = args.packet_output.resolve()
    if output.parent == args.root or args.root in output.parents:
        raise TaskFrontmatterError("execution packet must remain outside the work-log repository.")
    if not output.parent.is_dir():
        raise TaskFrontmatterError("execution packet parent directory does not exist.")
    if path_entry_exists(output):
        existing, _payload = read_immutable_json(output)
        existing_body = dict(existing)
        created_at = existing_body.pop("created_at", None)
        if not isinstance(created_at, str) or existing_body != packet_body:
            raise TaskFrontmatterError("existing immutable execution packet does not match current custody state.")
        print(f"execution packet already verified: {output}", flush=True)
        return
    packet = {**packet_body, "created_at": datetime.now().astimezone().isoformat()}
    _ = write_private_json(output, packet, final=True)
    os.chmod(output, 0o444)
    output_fd = os.open(output, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(output_fd)
    finally:
        os.close(output_fd)
    print(f"execution packet: {output}", flush=True)


def build_packet(args: Args, rows: dict[str, PlanRow], destination_sha256: str) -> None:
    """Build one packet while every task, target, receipt, and binding stays locked."""

    target_values = {
        "cedit:15",
        *(row.runat for row_id, row in rows.items() if row_id not in PROTECTED_ROWS and not row.runat.partition(":")[0].startswith("h")),
    }
    with root_membership_lock(args.root):
        with ExitStack() as target_locks:
            for target in sorted(target_values):
                target_locks.enter_context(task_target_lock(args.root, target))
            with ExitStack() as locks:
                for locked_path in sorted(
                    {
                        *closure_custody_paths(args, rows),
                        *args.receipt_dir.glob("close-*.json"),
                        *args.receipt_dir.glob(".close-*.journal.json"),
                        args.receipt_dir,
                        args.packet_output,
                    }
                ):
                    locks.enter_context(task_file_lock(locked_path))
                validate_operation_bindings_locked(args, rows)
                validate_escrow_custody_locked(args, rows)
                build_packet_locked(args, rows, destination_sha256)


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "This helper accepts only the immutable reviewed Source-1376 plan and authority. "
            "It transfers every eligible queue to amh1376_close.md, closes eligible descendants bottom-up without mail, "
            "preserves external and human-owned targets, and emits one immutable custody packet."
        ),
    )
    _ = parser.add_argument("--root", type=Path, required=True, help="Absolute authoritative work-log root.")
    _ = parser.add_argument("--plan", type=Path, required=True, help=f"Exact immutable Source-1376 plan with SHA-256 {PLAN_SHA256}.")
    _ = parser.add_argument(
        "--binding",
        type=Path,
        required=True,
        help=f"Exact immutable current-state execution binding with SHA-256 {EXECUTION_BINDING_SHA256}.",
    )
    _ = parser.add_argument("--authority", type=Path, required=True, help=f"Exact private Source-1376 Human authority file {AUTHORITY_REF}.")
    _ = parser.add_argument("--receipt-dir", type=Path, required=True, help="New or existing owner-private 0700 execution-receipt directory.")
    _ = parser.add_argument("--packet-output", type=Path, required=True, help="New immutable execution/custody JSON packet outside the work-log root.")
    _ = parser.add_argument(
        "--prepared-binding",
        type=Path,
        required=True,
        help="Immutable root-wide prepared-binding path outside the work-log root.",
    )
    _ = parser.add_argument(
        "--prepared-binding-sha256",
        default="",
        help="Independent-review-approved SHA-256 of --prepared-binding; required for execution and forbidden while preparing.",
    )
    _ = parser.add_argument(
        "--review-approval-output",
        type=Path,
        required=True,
        help="External immutable reviewer-signed approval receipt path.",
    )
    preparation = parser.add_mutually_exclusive_group()
    _ = preparation.add_argument(
        "--prepare-binding",
        action="store_true",
        help="Publish an audit-only --prepared-binding under the complete root/task lock set without mutating lifecycle state, then exit.",
    )
    _ = preparation.add_argument(
        "--reviewed-handoff",
        action="store_true",
        help="Publish while retaining every lock, await exact-digest approval on stdin, commit the first transfer, then finish execution.",
    )
    parsed = parser.parse_args(argv)
    root = parsed.root.resolve()
    if (
        not root.is_dir()
        or not parsed.plan.is_absolute()
        or not parsed.binding.is_absolute()
        or not parsed.authority.is_absolute()
        or not parsed.receipt_dir.is_absolute()
        or not parsed.packet_output.is_absolute()
        or not parsed.prepared_binding.is_absolute()
        or not parsed.review_approval_output.is_absolute()
    ):
        parser.error("root, plan, binding, authority, receipt directory, packet, prepared-binding, and review-approval paths must be absolute.")
    if root != WORK_LOG_ROOT or parsed.plan.resolve() != PLAN_PATH or parsed.binding.resolve() != EXECUTION_BINDING_PATH:
        parser.error(f"this one-shot helper is bound to root {WORK_LOG_ROOT}, plan {PLAN_PATH}, and binding {EXECUTION_BINDING_PATH}.")
    if parsed.packet_output.suffix != ".json":
        parser.error("--packet-output must end with .json.")
    packet_output = parsed.packet_output.resolve()
    if packet_output.parent == root or root in packet_output.parents or not packet_output.parent.is_dir():
        parser.error("--packet-output must be external to the work-log root with an existing parent directory.")
    prepared_binding = parsed.prepared_binding.resolve()
    review_approval = parsed.review_approval_output.resolve()
    if prepared_binding.suffix != ".json" or prepared_binding.parent == root or root in prepared_binding.parents:
        parser.error("--prepared-binding must be an external .json path.")
    if (
        review_approval.suffix != ".json"
        or review_approval.parent == root
        or root in review_approval.parents
        or review_approval == prepared_binding
        or review_approval == packet_output
        or prepared_binding == packet_output
    ):
        parser.error("prepared binding, review approval, and packet output must be distinct external .json paths.")
    if parsed.prepare_binding or parsed.reviewed_handoff:
        if parsed.prepared_binding_sha256:
            parser.error("--prepared-binding-sha256 is forbidden while publishing a binding.")
    elif SHA256_RE.fullmatch(parsed.prepared_binding_sha256) is None:
        parser.error("execution requires --prepared-binding-sha256 as 64 lowercase hex characters.")
    return Args(
        root,
        parsed.plan.resolve(),
        parsed.binding.resolve(),
        parsed.authority.resolve(),
        ensure_private_dir(parsed.receipt_dir),
        packet_output,
        prepared_binding,
        parsed.prepared_binding_sha256,
        parsed.prepare_binding,
        parsed.reviewed_handoff,
        review_approval,
    )


def run(args: Args) -> int:
    try:
        if args.prepare_binding or args.reviewed_handoff:
            path, digest = publish_prepared_binding(args)
            if args.reviewed_handoff:
                execution_args = replace(
                    args,
                    prepared_binding_sha256=digest,
                    prepare_binding=False,
                    reviewed_handoff=False,
                )
                apply(execution_args)
            else:
                print(f"prepared binding: {path}", flush=True)
                print(f"prepared binding sha256: {digest}", flush=True)
        else:
            apply(args)
    except (OSError, TaskFrontmatterError, RuntimeError, ValueError) as exc:
        print(f"omo_source1376_shutdown.py: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
