#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Close one pinned manager and publish one unlaunched successor transactionally."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import has_bound_close_proof, stop
from omo_manager.omo_codex_start import PCODX_ENV_KEYS as START_PCODX_ENV_KEYS
from omo_manager.omo_codex_start import Pane as StartPane
from omo_manager.omo_codex_start import pcodx_state
from omo_manager.omo_task_edit import render_pending_items
from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1, TaskFrontmatterError, TaskMetadata, frontmatter_parts, parse_task_metadata
from omo_manager.omo_task_status import (
    TODO_ROW_RE,
    active_child_task_refs,
    authoritative_active_target_task_paths,
    root_membership_lock,
    update_frontmatter_status,
)

AUDIT_VERSION = "v1.0.0"
AUDIT_OPERATION = "manager-replace"
SUCCESSOR_BLOCKER = "awaiting separate supported launch after atomic manager replacement ownership proof"
PCODX_ENV_KEYS = ("PCODX_POC_ROOT", "PCODX_RUN_DIR", "PCODX_LEDGER_PATH", "PCODX_SESSION_ID")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# tmux allocates pane ids from zero; `%0` is a valid first pane.
PANE_ID_RE = re.compile(r"^%[0-9]+$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
TASK_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+\.md$")
AUTHORITY_REF_RE = re.compile(r"^(?:[0-9]{6}/)?manager_mail/[A-Za-z0-9_.-]+\.txt$")
HUMAN_ENVELOPE_RE = re.compile(
    r'(?ms)^<human_instruction[ \t]+authoritative="true"[ \t]+source="(?P<source>[^"\r\n]+)">\r?\n'
    r"(?P<body>.*?)\r?\n</human_instruction>[ \t]*(?:\r?\n|$)"
)
FAILED_MANAGER_EVIDENCE_RE = re.compile(r"(?is)\b(?:agent|manager)\s+(?:has\s+)?failed\b.*\b(?:did\s+not|didn't|has\s+not|hasn't)\b.*\breplace\b")
# 🧑 Source `manager_mail/85c5dff58359-1269.txt:3-9`: "The guest has reported that they do not receive response ... previous responsible agents ... completely failed. Replace them."
GUEST1269_REPLACEMENT = (
    "manager_mail/85c5dff58359-1269.txt",
    (3, 9),
    "guest_hees_mail_mgr.md",
    "guest_hees:0.0",
    "\n".join(
        (
            "The guest has reported that they do not receive response for emails sent to",
            "you guys. Whatever the previous responsible agents were doing, they",
            "completely failed. Replace them. The new agent should be skeptical of",
            "anything done previously and make sure that in the future replies get sent",
            "to the guest also It was not like the guest received nothing. They report",
            "receiving empty emails. Investigate this with the new agents. Completing",
            "overhaul any garbage that's left.",
        )
    ),
)
SOURCE1289_FILE = "manager_mail/85c5dff58359-1289.txt"
SOURCE1289_TASK = "vl_repo_split_mgr.md"
SOURCE1289_SHA256 = "33829151fabb3c1502aceb87dbc837b7e2186160f8f1f34ca60bcd034c083770"
SOURCE1289_CARRIER_LINES = (1, 13)
SOURCE1289_TREE_LINES = (3, 10)
SOURCE1292_FILE = "manager_mail/85c5dff58359-1292.txt"
SOURCE1292_SHA256 = "508a7f94ec934e3dda36712201d9d3260a1fe673efc51b24c6c1d51fc34e6669"
SOURCE1292_LINES = (1, 4)
SOURCE1443_FILE = "manager_mail/85c5dff58359-1443.txt"
SOURCE1443_TASK = "personal_browser_mgr.md"
SOURCE1443_OLD_TARGET = "wl:8"
SOURCE1443_NEW_SESSION = "pb"
SOURCE1443_SHA256 = "71b17a8828fcbdf7e26e60c1960b81a51e570858a68c730f34d32379e84b1bca"
SOURCE1443_CARRIER_LINES = (1, 3)
SOURCE1443_SUCCESSOR_LINES = (3, 3)
SOURCE1477_FILE = "manager_mail/85c5dff58359-1477.txt"
SOURCE1477_SHA256 = "8c5f7388677758a65c98779a4b8bcccdf6a628fca8d5a02376bbc10817949aaa"
SOURCE1477_CARRIER_LINES = (1, 6)
SOURCE1477_SUCCESSOR_LINES = (3, 6)
SOURCE1477_REPLACEMENTS = (
    ("personal_browser_mgr_pb.md", "pb:13.0", "pb"),
    ("dw_fpr_mgr.md", "dw:5.0", "dw"),
)
SOURCE1485_FILE = "manager_mail/85c5dff58359-1485.txt"
SOURCE1485_SHA256 = "daea29bb96d21b1e4f205c98f6905d444a3eb95dbcd4120c7854259e1c7c386f"
SOURCE1485_CARRIER_LINES = (1, 12)
SOURCE1485_SUCCESSOR_LINES = (3, 11)
SOURCE1485_ENVELOPE_TASKS = frozenset({"dw_rotate_exec.md", "dw_rotate_repair.md"})
SOURCE1485_ENVELOPE_SHA256 = "48d449159866c4d0c28ea7455347516dd1fca01f2ec71f31cf7a7faa90673036"
SOURCE1485_ENVELOPE_SUBJECT = "Subject: Re: Wix read-only check — 202608/pb_wix_inventory_041.md"
SOURCE1485_REPLACEMENTS = (
    ("dw_present_mgr.md", "dw11:0.0", "dw_present_mgr_replacement.md", "dw11:1.0", "dw:13.0"),
    (
        "dw_fpr_mgr_replacement.md",
        "dw:13.0",
        "dw_fpr_mgr_replacement2.md",
        "dw:14.0",
        "wl:7.0",
    ),
    ("dw_manager.md", "dw:0.0", "dw_manager_replacement.md", "dw:15.0", "config:1.0"),
)
SOURCE1485_ROOT_TASK = "dw_manager.md"
SOURCE1485_UMBRELLA_TASK = "resume_dw_work.md"
SOURCE1485_UMBRELLA_TARGET = "wl:7.0"
SOURCE1485_ROOT_REQUIRED_CHILDREN = frozenset({"dw_cc_sampling.md", "dw_fpr_mgr_replacement2.md", SOURCE1485_UMBRELLA_TASK})
SOURCE1485_ROOT_OPTIONAL_CHILDREN = frozenset({"dw_bodyswap_pr.md"})
SOURCE1597_FILE = "manager_mail/85c5dff58359-1597.txt"
SOURCE1597_SHA256 = "aa4034035121e5fa75c8c2e412bccb1731b626982934403e00fce33506380b43"
SOURCE1597_LINES = (3, 3)
SOURCE1597_TASK = "dw_fpr_new.md"
SOURCE1597_OLD_TARGET = "dw:14"
SOURCE1597_SUCCESSOR_TASK = "dw_fpr_new_source1597.md"
SOURCE1597_SUCCESSOR_TARGET = "dw:16"
SOURCE1597_PARENT_TARGET = "dw:15"
SOURCE1597_DIRECTIVE = "For manager: replace this agent immediately. Tell to new agent to obey my order to try ephemeral AWS proxies for the 20 texts or face termination"
SOURCE1597_OLD_QUEUE = (
    "🧑 Use the Human-signed-up :6082 browser, which gives 20 free checks a day, for the Pangram evaluation. Source: manager_mail/85c5dff58359-1584.txt.",
    "🧑 Automate Pangram checks and run all hard samples; if automation is too complicated, delegate direct checks to cheaper agents. Source: manager_mail/85c5dff58359-1592.txt.",
)
SOURCE1601_FILE = "manager_mail/85c5dff58359-1601.txt"
SOURCE1601_SHA256 = "dc78cd34a4fd6becb0a20a1ea3143d43cf8f45ae1e6a58c6e7b3c6fa9f830e01"
SOURCE1601_LINES = (3, 4)
SOURCE1601_DIRECTIVE = "For a manager, replace this manager and DW for team, only tell them their\noriginal goals from the human, not any of the ones they set themselves."
SOURCE1601_QUEUE_GOAL = "For a manager, replace this manager and DW for team, only tell them their original goals from the human, not any of the ones they set themselves."
SOURCE1611_FILE = "manager_mail/85c5dff58359-1611.txt"
SOURCE1611_SHA256 = "b136968572ef02cd85811fbc89f171b332cd9d24768ca6c5718dfb175b4ff648"
SOURCE1611_LINES = (3, 3)
SOURCE1611_TASK = "cleanup_dw_tree.md"
SOURCE1611_OLD_TARGET = "config:1"
SOURCE1611_SUCCESSOR_TASK = "cleanup_dw_tree_new.md"
SOURCE1611_SUCCESSOR_TARGET = "config:23"
SOURCE1611_PARENT_TARGET = "wl:1"
SOURCE1611_DIRECTIVE = "Replace the manager and let the new manager immediately replace their worker"
SOURCE1611_OLD_QUEUE = (
    "🧑 Human Source manager_mail/85c5dff58359-1601.txt: replace this manager and the entire DW team through supported atomic lifecycle transfers, preserving exactly one successor per responsibility.",
    "🧑 Human Source manager_mail/85c5dff58359-1601.txt: give every successor only the original goals stated by the Human; exclude goals or constraints invented by prior agents.",
    "🧑 Replace the manager, then have the new manager immediately replace their worker. Source: manager_mail/85c5dff58359-1611.txt.",
    "🧑 Replace the manager that took tasks outside its ownership, and require the successor manager to hand off all tasks completely to workers. Source: manager_mail/85c5dff58359-1612.txt.",
)
# Source-1611's second, serial action is deliberately separate from the
# config:1 manager transfer above.  It may replace only that fresh manager's
# direct DW manager once; its authority is consumed by the transition and is
# not propagated to the successor (which would otherwise authorize recursion).
SOURCE1611_DIRECT_WORKER_TASK = "dw_root_new.md"
SOURCE1611_DIRECT_WORKER_OLD_TARGET = "dw:15"
SOURCE1611_DIRECT_WORKER_SUCCESSOR_TASK = "dw_root_source1611.md"
SOURCE1611_DIRECT_WORKER_SUCCESSOR_TARGET = "dw:16"
SOURCE1611_DIRECT_WORKER_PARENT_TARGET = "config:23"
SOURCE1611_DIRECT_WORKER_OLD_QUEUE = (
    "🧑 Replace the current Pangram manager immediately. Tell the new owner to obey the Human order to try ephemeral AWS proxies for the 20 texts or face termination. Source: manager_mail/85c5dff58359-1597.txt.",
)
SOURCE1611_SESSION_ROOT = Path("/home/sichanghe/.codex/sessions")
SOURCE1612_FILE = "manager_mail/85c5dff58359-1612.txt"
SOURCE_ONLY_AUTHORITY_MODE = "source-only-old-task-before-image"
PCODX_REPLACE_EVIDENCE_RE = re.compile(
    r"(?m)^Replace the failed PCODX manager (?P<task>[A-Za-z0-9_./-]+\.md) at "
    r"(?P<target>[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) with one fresh plain-Codex manager "
    r"inheriting all tasks and comments\.[ \t]*$"
)
PCODX_REPLACE_DIRECTIVE_RE = re.compile(r"(?m)^Replace the failed PCODX manager\b.*$")
MAX_AUDIT_BYTES = 8 * 1024 * 1024
ROLLOUT_METADATA_MAX_BYTES = 256 * 1024
POSIX_ACL_XATTRS = {"system.posix_acl_access", "system.posix_acl_default"}
AT_FDCWD = -100
AT_SYMLINK_FOLLOW = 0x400
RENAME_EXCHANGE = 2
RENAME_NOREPLACE = 1
LIBC = ctypes.CDLL(None, use_errno=True)


class ReplaceError(RuntimeError):
    """The manager replacement failed closed."""


class CommittedMutationError(ReplaceError):
    """A namespace mutation committed, but its durability sync failed."""

    def __init__(self, label: str, snapshot: Snapshot, error: OSError) -> None:
        super().__init__(f"{label} committed but directory sync failed: {error}")
        self.label = label
        self.snapshot = snapshot


class NamespaceMutationError(ReplaceError):
    """An atomic namespace operation could not be resolved safely."""


@dataclass(frozen=True)
class ChildPin:
    task: str
    sha256: str
    queue_sha256: str = ""


@dataclass(frozen=True)
class DescendantPin:
    task: str
    sha256: str
    target: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int
    session_id: str
    queue_sha256: str


@dataclass(frozen=True, order=True)
class LineRange:
    start: int
    end: int


@dataclass(frozen=True)
class Args:
    root: Path
    old_task: str
    successor_task: str
    old_target: str
    new_target: str
    parent_target: str
    old_sha256: str
    todo_sha256: str
    children: tuple[ChildPin, ...]
    old_pane_id: str
    old_pane_pid: int
    old_pane_start_ticks: int
    old_session_id: str
    authority_file: str
    authority_lines: LineRange
    authority_sha256: str
    authority_envelope_task: str
    authority_envelope_sha256: str
    successor_item_lines: tuple[LineRange, ...]
    protected_targets: tuple[str, ...]
    audit_output: Path
    preparer: str
    reviewer: str
    closed_owner_audit: Path | None = None
    closed_owner_audit_sha256: str = ""
    old_queue_sha256: str = ""
    old_pcodx_state_sha256: str = ""
    old_pcodx_ledger_sha256: str = ""
    old_pcodx_wrapper_sha256: str = ""
    protected_targets_sha256: str = ""
    authority_envelope_file_sha256: str = ""
    descendants: tuple[DescendantPin, ...] = ()
    empty_tree_envelope_sha256: str = ""
    descendant_authority_envelope_sha256: str = ""
    source1611_parent_sha256: str = ""
    source1611_rollout: Path | None = None
    source1611_session_root: Path | None = None
    source1611_rollout_device: int = 0
    source1611_rollout_inode: int = 0
    source1611_rollout_holder_pid: int = 0
    source1611_rollout_holder_start_ticks: int = 0
    source1611_rollout_fd: int = -1
    source1611_rollout_session_meta_sha256: str = ""
    source1611_rollout_holder_exe: Path | None = None
    source1611_rollout_holder_exe_link: str = ""
    source1611_rollout_holder_exe_device: int = 0
    source1611_rollout_holder_exe_inode: int = 0
    source1611_rollout_holder_exe_mode: int = 0
    source1611_rollout_holder_exe_uid: int = -1
    source1611_rollout_holder_exe_nlink: int = -1
    source1611_rollout_holder_exe_size: int = 0
    source1611_rollout_holder_exe_sha256: str = ""
    source1611_rollout_holder_argv_sha256: str = ""


@dataclass(frozen=True)
class Snapshot:
    path: Path
    data: bytes
    state: os.stat_result


@dataclass(frozen=True)
class PaneIdentity:
    target: str
    pane_id: str
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class Source1611RolloutCustody:
    """One direct worker's live Codex UUID, proven from its held rollout FD."""

    rollout: Path
    device: int
    inode: int
    holder_pid: int
    holder_start_ticks: int
    descriptor: int
    pane_start_ticks: int
    session_meta_sha256: str
    session_id: str


@dataclass(frozen=True)
class Source1611Process:
    pid: int
    ppid: int
    state: str
    process_group: int
    session: int
    tty: int
    start_ticks: int
    argv_sha256: str


@dataclass(frozen=True)
class ActiveGraphRow:
    task: str
    sha256: str
    status: str
    runat: str
    managerat: str
    tool: str
    is_manager: bool
    session_id: str
    queue_sha256: str


@dataclass(frozen=True)
class Plan:
    old: Snapshot
    todo: Snapshot
    authority: Snapshot
    authority_envelope: Snapshot
    children: tuple[Snapshot, ...]
    successor_path: Path
    old_after: bytes
    child_after: tuple[bytes, ...]
    todo_after: bytes
    successor_data: bytes
    successor_queue: tuple[str, ...]
    child_queues: tuple[tuple[str, ...], ...]
    initial_markdown_paths: tuple[Path, ...]
    protected_identities: tuple[PaneIdentity, ...]
    descendant_identities: tuple[PaneIdentity, ...] = ()
    empty_tree_authority: Snapshot | None = None
    source1485_topology: dict[str, object] | None = None
    source1601_authority: Snapshot | None = None
    source1597_authority: Snapshot | None = None
    source1611_parent: Snapshot | None = None


@dataclass(frozen=True)
class AuditEntry:
    task: str
    before: bytes | None
    after: bytes
    mode: int
    gid: int


@dataclass(frozen=True)
class Recovery:
    plan: Plan
    record: dict[str, object]
    audit_bytes: bytes
    entries: tuple[AuditEntry, ...]
    owner_stopped: bool
    result: str = ""


class ParsedArgs(argparse.Namespace):
    root: Path = Path()
    old_task: str = ""
    successor_task: str = ""
    old_target: str = ""
    new_target: str = ""
    parent_target: str = ""
    old_sha256: str = ""
    todo_sha256: str = ""
    child: list[ChildPin] = []
    old_pane_id: str = ""
    old_pane_pid: int = 0
    old_pane_start_ticks: int = 0
    old_session_id: str = ""
    authority_file: str = ""
    authority_lines: LineRange = LineRange(0, 0)
    authority_sha256: str = ""
    authority_envelope_task: str = ""
    authority_envelope_sha256: str = ""
    successor_item_lines: list[LineRange] = []
    protected_target: list[str] = []
    audit_output: Path = Path()
    preparer: str = ""
    reviewer: str = ""
    closed_owner_audit: Path | None = None
    closed_owner_audit_sha256: str = ""
    old_queue_sha256: str = ""
    old_pcodx_state_sha256: str = ""
    old_pcodx_ledger_sha256: str = ""
    old_pcodx_wrapper_sha256: str = ""
    protected_targets_sha256: str = ""
    authority_envelope_file_sha256: str = ""
    descendant: list[DescendantPin] = []
    empty_tree_envelope_sha256: str = ""
    descendant_authority_envelope_sha256: str = ""
    source1611_parent_sha256: str = ""
    source1611_rollout: Path | None = None
    source1611_session_root: Path | None = None
    source1611_rollout_device: int = 0
    source1611_rollout_inode: int = 0
    source1611_rollout_holder_pid: int = 0
    source1611_rollout_holder_start_ticks: int = 0
    source1611_rollout_fd: int | None = None
    source1611_rollout_session_meta_sha256: str = ""
    source1611_rollout_holder_exe: Path | None = None
    source1611_rollout_holder_exe_link: str = ""
    source1611_rollout_holder_exe_device: int = 0
    source1611_rollout_holder_exe_inode: int = 0
    source1611_rollout_holder_exe_mode: int = 0
    source1611_rollout_holder_exe_uid: int = -1
    source1611_rollout_holder_exe_nlink: int = -1
    source1611_rollout_holder_exe_size: int = 0
    source1611_rollout_holder_exe_sha256: str = ""
    source1611_rollout_holder_argv_sha256: str = ""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_digest(value: object) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def is_pcodx_replacement(args: Args) -> bool:
    return bool(args.old_pcodx_state_sha256)


def is_guest1269_replacement(args: Args) -> bool:
    source, lines, task, target, _evidence = GUEST1269_REPLACEMENT
    return (
        args.authority_file == source
        and args.authority_lines == LineRange(*lines)
        and args.successor_item_lines == (args.authority_lines,)
        and args.old_task == task
        and canonical_target(args.old_target) == target
    )


def is_source1289_whole_tree(args: Args) -> bool:
    return args.authority_file == SOURCE1289_FILE and args.old_task == SOURCE1289_TASK


# 🧑 Source `manager_mail/85c5dff58359-1289.txt:3-10`: "replace the entire agent tree for this task ... Previous agents completely failed ... repository has not been split"
def is_source1289_semantic_exception(args: Args) -> bool:
    return (
        args.old_task == SOURCE1289_TASK
        and args.authority_file == SOURCE1289_FILE
        and args.authority_sha256 == SOURCE1289_SHA256
        and args.authority_lines == LineRange(*SOURCE1289_CARRIER_LINES)
        and args.successor_item_lines == (LineRange(*SOURCE1289_TREE_LINES),)
    )


# 🧑 Source `manager_mail/85c5dff58359-1443.txt:3`: "close this agent and replace them with correct setup"
def is_source1443_semantic_exception(args: Args) -> bool:
    return (
        args.old_task == SOURCE1443_TASK
        and canonical_target(args.old_target) == canonical_target(SOURCE1443_OLD_TARGET)
        and target_session(args.new_target) == SOURCE1443_NEW_SESSION
        and args.authority_file == SOURCE1443_FILE
        and args.authority_sha256 == SOURCE1443_SHA256
        and args.authority_lines == LineRange(*SOURCE1443_CARRIER_LINES)
        and args.successor_item_lines == (LineRange(*SOURCE1443_SUCCESSOR_LINES),)
    )


# 🧑 Source `manager_mail/85c5dff58359-1477.txt:3-6`: "Replace all the managers involved ... agents should be in the correct T mark session that correspond to their tasks."
def is_source1477_semantic_exception(args: Args) -> bool:
    replacement = (args.old_task, canonical_target(args.old_target), target_session(args.new_target))
    return (
        replacement in SOURCE1477_REPLACEMENTS
        and args.authority_file == SOURCE1477_FILE
        and args.authority_sha256 == SOURCE1477_SHA256
        and args.authority_lines == LineRange(*SOURCE1477_CARRIER_LINES)
        and args.successor_item_lines == (LineRange(*SOURCE1477_SUCCESSOR_LINES),)
    )


def is_source1485_replacement(args: Args) -> bool:
    """Return whether the invocation names one exact Source-1485 manager transition."""

    replacement = (
        args.old_task,
        canonical_target(args.old_target),
        args.successor_task,
        canonical_target(args.new_target),
        canonical_target(args.parent_target),
    )
    return replacement in SOURCE1485_REPLACEMENTS


# 🧑 Source `manager_mail/85c5dff58359-1485.txt:3-11`: "the agent has drifted. Replace them and every agent they manage. New agents should be maximally responsive"
def is_source1485_semantic_exception(args: Args) -> bool:
    return (
        is_source1485_replacement(args)
        and args.authority_file == SOURCE1485_FILE
        and args.authority_sha256 == SOURCE1485_SHA256
        and args.authority_lines == LineRange(*SOURCE1485_CARRIER_LINES)
        and args.successor_item_lines == (LineRange(*SOURCE1485_SUCCESSOR_LINES),)
        and args.authority_envelope_task in SOURCE1485_ENVELOPE_TASKS
        and args.authority_envelope_sha256 == SOURCE1485_ENVELOPE_SHA256
    )


# 🧑 Source `manager_mail/85c5dff58359-1597.txt:3`: exact replacement of the Pangram manager.
def is_source1597_semantic_exception(args: Args) -> bool:
    return (
        args.old_task == SOURCE1597_TASK
        and args.successor_task == SOURCE1597_SUCCESSOR_TASK
        and canonical_target(args.old_target) == canonical_target(SOURCE1597_OLD_TARGET)
        and canonical_target(args.new_target) == canonical_target(SOURCE1597_SUCCESSOR_TARGET)
        and canonical_target(args.parent_target) == canonical_target(SOURCE1597_PARENT_TARGET)
        and args.authority_file == SOURCE1597_FILE
        and args.authority_sha256 == SOURCE1597_SHA256
        and args.authority_lines == LineRange(*SOURCE1597_LINES)
        and args.successor_item_lines == (LineRange(*SOURCE1597_LINES),)
        and args.authority_envelope_task == args.old_task
        and args.authority_envelope_sha256 == args.old_sha256
    )


def is_source1611_mapping(
    args: Args,
    *,
    old_task: str,
    successor_task: str,
    old_target: str,
    successor_target: str,
    parent_target: str,
) -> bool:
    return (
        args.old_task == old_task
        and args.successor_task == successor_task
        and canonical_target(args.old_target) == canonical_target(old_target)
        and canonical_target(args.new_target) == canonical_target(successor_target)
        and canonical_target(args.parent_target) == canonical_target(parent_target)
        and args.authority_file == SOURCE1611_FILE
        and args.authority_sha256 == SOURCE1611_SHA256
        and args.authority_lines == LineRange(*SOURCE1611_LINES)
        and args.successor_item_lines == (LineRange(*SOURCE1611_LINES),)
        and args.authority_envelope_task == args.old_task
        and args.authority_envelope_sha256 == args.old_sha256
    )


def is_source1611_manager_semantic_exception(args: Args) -> bool:
    return is_source1611_mapping(
        args,
        old_task=SOURCE1611_TASK,
        successor_task=SOURCE1611_SUCCESSOR_TASK,
        old_target=SOURCE1611_OLD_TARGET,
        successor_target=SOURCE1611_SUCCESSOR_TARGET,
        parent_target=SOURCE1611_PARENT_TARGET,
    )


def is_source1611_direct_worker_semantic_exception(args: Args) -> bool:
    return is_source1611_mapping(
        args,
        old_task=SOURCE1611_DIRECT_WORKER_TASK,
        successor_task=SOURCE1611_DIRECT_WORKER_SUCCESSOR_TASK,
        old_target=SOURCE1611_DIRECT_WORKER_OLD_TARGET,
        successor_target=SOURCE1611_DIRECT_WORKER_SUCCESSOR_TARGET,
        parent_target=SOURCE1611_DIRECT_WORKER_PARENT_TARGET,
    )


def is_source1611_semantic_exception(args: Args) -> bool:
    return is_source1611_manager_semantic_exception(args) or is_source1611_direct_worker_semantic_exception(args)


def is_source_only_semantic_exception(args: Args) -> bool:
    return is_source1597_semantic_exception(args) or is_source1611_semantic_exception(args)


def source_only_directive(args: Args) -> str:
    if is_source1597_semantic_exception(args):
        return SOURCE1597_DIRECTIVE
    if is_source1611_semantic_exception(args):
        return SOURCE1611_DIRECTIVE
    raise ReplaceError("source-only authority is unavailable outside an exact replacement program")


def source1601_required(args: Args) -> bool:
    return is_source1597_semantic_exception(args) or is_source1611_semantic_exception(args)


def source1597_direct_worker_required(args: Args) -> bool:
    """Whether Source-1611's serial worker handoff preserves Source-1597 work."""

    return is_source1611_direct_worker_semantic_exception(args)


def source1611_rollout_custody_requested(args: Args) -> bool:
    return args.source1611_rollout is not None


def source1611_task_session_is_absent(data: bytes) -> bool:
    """Distinguish a missing legacy field from an explicitly blank one."""

    try:
        parts = frontmatter_parts(data.decode("utf-8"))
    except (UnicodeDecodeError, TaskFrontmatterError):
        return False
    return parts is not None and not any(line.partition(":")[0].strip() == "session_id" for line in parts[0])


def old_session_matches(args: Args, session_id: str, data: bytes | None = None) -> bool:
    """Keep normal task-session custody mandatory outside the one legacy path."""

    if is_pcodx_replacement(args):
        return True
    if source1611_rollout_custody_requested(args):
        return not session_id and data is not None and source1611_task_session_is_absent(data)
    return session_id.lower() == args.old_session_id


def source_only_expected_old_queue(args: Args) -> tuple[str, ...]:
    if is_source1597_semantic_exception(args):
        return SOURCE1597_OLD_QUEUE
    if is_source1611_manager_semantic_exception(args):
        return SOURCE1611_OLD_QUEUE
    if is_source1611_direct_worker_semantic_exception(args):
        return SOURCE1611_DIRECT_WORKER_OLD_QUEUE
    raise ReplaceError("source-only queue is unavailable outside an exact replacement program")


def source_only_added_goals(args: Args, queue: tuple[str, ...]) -> tuple[str, ...]:
    # This is the terminal Source-1611 direct-worker transition.  Its exact
    # authority has been consumed, so preserve only the old manager's original
    # Human queue; do not grant a successor authority to replace another child.
    if is_source1611_direct_worker_semantic_exception(args):
        return ()
    added: list[str] = []
    if not any(args.authority_file in item for item in queue):
        added.append(f"🧑 Source {args.authority_file}: {source_only_directive(args)}")
    if source1601_required(args) and not any(SOURCE1601_FILE in item for item in queue):
        added.append(f"🧑 Source {SOURCE1601_FILE}: {SOURCE1601_QUEUE_GOAL}")
    return tuple(added)


def uses_ordered_queue_binding(args: Args) -> bool:
    return is_pcodx_replacement(args) or is_guest1269_replacement(args) or is_source1485_replacement(args) or is_source_only_semantic_exception(args)


def uses_protected_inventory(args: Args) -> bool:
    return is_pcodx_replacement(args) or is_source1485_replacement(args) or is_source_only_semantic_exception(args)


def is_source1292_empty_tree(args: Args) -> bool:
    # 🧑 Source `manager_mail/85c5dff58359-1292.txt:3`: "Close either way. Directly use tmux if needed"
    return bool(args.empty_tree_envelope_sha256)


def is_source1292_descendant_tree(args: Args) -> bool:
    return bool(args.descendant_authority_envelope_sha256)


def source1292_envelope_sha256(args: Args) -> str:
    return args.empty_tree_envelope_sha256 or args.descendant_authority_envelope_sha256


def canonical_target(target: str) -> str:
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):(\d+)(?:\.(\d+))?", target)
    if match is None:
        raise ReplaceError(f"invalid tmux target: {target}")
    session, window, pane = match.groups()
    return f"{session}:{int(window)}.{int(pane or '0')}"


def target_session(target: str) -> str:
    return canonical_target(target).partition(":")[0]


def parse_child(value: str) -> ChildPin:
    fields = value.split("=")
    if len(fields) not in {2, 3}:
        raise argparse.ArgumentTypeError("--child must be TASK.md=SHA256 or TASK.md=SHA256=QUEUE_SHA256")
    task, sha256, *queue = fields
    queue_sha256 = queue[0] if queue else ""
    if TASK_REF_RE.fullmatch(task) is None or SHA256_RE.fullmatch(sha256) is None or (queue_sha256 and SHA256_RE.fullmatch(queue_sha256) is None):
        raise argparse.ArgumentTypeError("--child must contain canonical task and lowercase SHA-256 fields")
    return ChildPin(task, sha256, queue_sha256)


def parse_descendant(value: str) -> DescendantPin:
    fields = value.split("=")
    if len(fields) != 8:
        raise argparse.ArgumentTypeError("--descendant must be TASK.md=SHA256=TARGET=PANE_ID=PANE_PID=START_TICKS=SESSION_UUID=QUEUE_SHA256")
    task, sha256, target, pane_id, pane_pid, start_ticks, session_id, queue_sha256 = fields
    try:
        canonical_target(target)
        pid = int(pane_pid)
        ticks = int(start_ticks)
    except (ReplaceError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if (
        TASK_REF_RE.fullmatch(task) is None
        or SHA256_RE.fullmatch(sha256) is None
        or PANE_ID_RE.fullmatch(pane_id) is None
        or pid <= 0
        or ticks <= 0
        or UUID_RE.fullmatch(session_id) is None
        or SHA256_RE.fullmatch(queue_sha256) is None
    ):
        raise argparse.ArgumentTypeError("--descendant contains an invalid pinned identity")
    return DescendantPin(task, sha256, target, pane_id, pid, ticks, session_id.lower(), queue_sha256)


def parse_line_range(value: str) -> LineRange:
    start, separator, end = value.partition("-")
    if not separator or not start.isdigit() or not end.isdigit() or int(start) <= 0 or int(end) < int(start):
        raise argparse.ArgumentTypeError("line range must be positive START-END")
    return LineRange(int(start), int(end))


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--old-task", required=True)
    _ = parser.add_argument("--successor-task", required=True)
    _ = parser.add_argument("--old-target", required=True)
    _ = parser.add_argument("--new-target", required=True)
    _ = parser.add_argument("--parent-target", required=True)
    _ = parser.add_argument("--old-sha256", required=True)
    _ = parser.add_argument("--todo-sha256", required=True)
    _ = parser.add_argument("--child", action="append", default=[], type=parse_child)
    _ = parser.add_argument("--descendant", action="append", default=[], type=parse_descendant)
    _ = parser.add_argument("--empty-tree-envelope-sha256", default="")
    _ = parser.add_argument("--descendant-authority-envelope-sha256", default="")
    _ = parser.add_argument("--source1611-parent-sha256", default="")
    _ = parser.add_argument("--source1611-rollout", type=Path)
    _ = parser.add_argument("--source1611-session-root", type=Path)
    _ = parser.add_argument("--source1611-rollout-device", type=int, default=0)
    _ = parser.add_argument("--source1611-rollout-inode", type=int, default=0)
    _ = parser.add_argument("--source1611-rollout-holder-pid", type=int, default=0)
    _ = parser.add_argument("--source1611-rollout-holder-start-ticks", type=int, default=0)
    _ = parser.add_argument("--source1611-rollout-fd", type=int)
    _ = parser.add_argument("--source1611-rollout-session-meta-sha256", default="")
    _ = parser.add_argument("--source1611-rollout-holder-exe", type=Path)
    _ = parser.add_argument("--source1611-rollout-holder-exe-link", default="")
    _ = parser.add_argument("--source1611-rollout-holder-exe-device", type=int, default=0)
    _ = parser.add_argument("--source1611-rollout-holder-exe-inode", type=int, default=0)
    _ = parser.add_argument("--source1611-rollout-holder-exe-mode", type=int, default=0)
    _ = parser.add_argument("--source1611-rollout-holder-exe-uid", type=int, default=-1)
    _ = parser.add_argument("--source1611-rollout-holder-exe-nlink", type=int, default=-1)
    _ = parser.add_argument("--source1611-rollout-holder-exe-size", type=int, default=0)
    _ = parser.add_argument("--source1611-rollout-holder-exe-sha256", default="")
    _ = parser.add_argument("--source1611-rollout-holder-argv-sha256", default="")
    _ = parser.add_argument("--old-pane-id", required=True)
    _ = parser.add_argument("--old-pane-pid", required=True, type=int)
    _ = parser.add_argument("--old-pane-start-ticks", required=True, type=int)
    _ = parser.add_argument("--old-session-id", required=True)
    _ = parser.add_argument("--authority-file", required=True)
    _ = parser.add_argument("--authority-lines", required=True, type=parse_line_range)
    _ = parser.add_argument("--authority-sha256", required=True)
    _ = parser.add_argument("--authority-envelope-task", required=True)
    _ = parser.add_argument("--authority-envelope-sha256", required=True)
    _ = parser.add_argument("--successor-item-lines", action="append", required=True, type=parse_line_range)
    _ = parser.add_argument("--protected-target", action="append", default=[])
    _ = parser.add_argument("--audit-output", required=True, type=Path)
    _ = parser.add_argument("--preparer", required=True)
    _ = parser.add_argument("--reviewer", required=True)
    _ = parser.add_argument("--closed-owner-audit", type=Path)
    _ = parser.add_argument("--closed-owner-audit-sha256", default="")
    _ = parser.add_argument("--old-queue-sha256", default="")
    _ = parser.add_argument("--old-pcodx-state-sha256", default="")
    _ = parser.add_argument("--old-pcodx-ledger-sha256", default="")
    _ = parser.add_argument("--old-pcodx-wrapper-sha256", default="")
    _ = parser.add_argument("--protected-targets-sha256", default="")
    _ = parser.add_argument("--authority-envelope-file-sha256", default="")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    for value, label in (
        (parsed.old_sha256, "old task"),
        (parsed.todo_sha256, "TODO"),
        (parsed.authority_sha256, "authority"),
        (parsed.authority_envelope_sha256, "authority envelope"),
    ):
        if SHA256_RE.fullmatch(value) is None:
            parser.error(f"{label} SHA-256 must be 64 lowercase hexadecimal characters")
    if any(TASK_REF_RE.fullmatch(task) is None for task in (parsed.old_task, parsed.successor_task, parsed.authority_envelope_task)):
        parser.error("task arguments must be canonical relative Markdown paths")
    if parsed.old_task == parsed.successor_task:
        parser.error("old and successor tasks must differ")
    if PANE_ID_RE.fullmatch(parsed.old_pane_id) is None or parsed.old_pane_pid <= 0 or parsed.old_pane_start_ticks <= 0:
        parser.error("old pane identity must contain a positive pane id, pid, and process start tick")
    if UUID_RE.fullmatch(parsed.old_session_id) is None:
        parser.error("--old-session-id must be one exact UUID")
    if AUTHORITY_REF_RE.fullmatch(parsed.authority_file) is None:
        parser.error("--authority-file must be one canonical manager_mail/*.txt reference")
    item_ranges = tuple(sorted(parsed.successor_item_lines))
    if len(set(item_ranges)) != len(item_ranges) or any(item.start < parsed.authority_lines.start or item.end > parsed.authority_lines.end for item in item_ranges):
        parser.error("successor item line ranges must be unique and contained in --authority-lines")
    if not parsed.audit_output.is_absolute():
        parser.error("--audit-output must be absolute")
    if not parsed.preparer.strip() or not parsed.reviewer.strip() or parsed.preparer.strip() == parsed.reviewer.strip():
        parser.error("preparer and independent reviewer must be distinct nonempty identities")
    if bool(parsed.closed_owner_audit) != bool(parsed.closed_owner_audit_sha256):
        parser.error("closed-owner audit path and SHA-256 must be supplied together")
    if parsed.closed_owner_audit is not None and not parsed.closed_owner_audit.is_absolute():
        parser.error("--closed-owner-audit must be absolute")
    if parsed.closed_owner_audit_sha256 and SHA256_RE.fullmatch(parsed.closed_owner_audit_sha256) is None:
        parser.error("closed-owner audit SHA-256 must be 64 lowercase hexadecimal characters")
    if parsed.source1611_parent_sha256 and SHA256_RE.fullmatch(parsed.source1611_parent_sha256) is None:
        parser.error("Source-1611 parent SHA-256 must be 64 lowercase hexadecimal characters")
    source1611_rollout_values = (
        parsed.source1611_rollout,
        parsed.source1611_session_root,
        parsed.source1611_rollout_device,
        parsed.source1611_rollout_inode,
        parsed.source1611_rollout_holder_pid,
        parsed.source1611_rollout_holder_start_ticks,
        parsed.source1611_rollout_fd is not None,
        parsed.source1611_rollout_session_meta_sha256,
        parsed.source1611_rollout_holder_exe,
        parsed.source1611_rollout_holder_exe_link,
        parsed.source1611_rollout_holder_exe_device,
        parsed.source1611_rollout_holder_exe_inode,
        parsed.source1611_rollout_holder_exe_mode,
        parsed.source1611_rollout_holder_exe_uid >= 0,
        parsed.source1611_rollout_holder_exe_nlink >= 0,
        parsed.source1611_rollout_holder_exe_size,
        parsed.source1611_rollout_holder_exe_sha256,
        parsed.source1611_rollout_holder_argv_sha256,
    )
    if any(bool(value) for value in source1611_rollout_values):
        if (
            parsed.source1611_rollout is None
            or parsed.source1611_session_root is None
            or not parsed.source1611_rollout.is_absolute()
            or not parsed.source1611_session_root.is_absolute()
            or parsed.source1611_rollout_holder_exe is None
            or not parsed.source1611_rollout_holder_exe.is_absolute()
            or not parsed.source1611_rollout_holder_exe_link.startswith("/")
            or min(
                parsed.source1611_rollout_device,
                parsed.source1611_rollout_inode,
                parsed.source1611_rollout_holder_pid,
                parsed.source1611_rollout_holder_start_ticks,
                parsed.source1611_rollout_holder_exe_device,
                parsed.source1611_rollout_holder_exe_inode,
                parsed.source1611_rollout_holder_exe_mode,
                parsed.source1611_rollout_holder_exe_size,
            )
            <= 1
            or parsed.source1611_rollout_holder_exe_uid < 0
            or parsed.source1611_rollout_holder_exe_nlink < 0
            or parsed.source1611_rollout_fd is None
            or parsed.source1611_rollout_fd < 0
            or SHA256_RE.fullmatch(parsed.source1611_rollout_session_meta_sha256) is None
            or SHA256_RE.fullmatch(parsed.source1611_rollout_holder_exe_sha256) is None
            or SHA256_RE.fullmatch(parsed.source1611_rollout_holder_argv_sha256) is None
        ):
            parser.error("Source-1611 rollout custody requires every exact absolute rollout, holder, FD, and metadata binding")
    children = tuple(sorted(parsed.child, key=lambda child: child.task))
    if len({child.task for child in children}) != len(children):
        parser.error("--child task references must be unique")
    descendants = tuple(sorted(parsed.descendant, key=lambda child: child.task))
    if len({child.task for child in descendants}) != len(descendants):
        parser.error("--descendant task references must be unique")
    if descendants and tuple((item.task, item.sha256) for item in descendants) != tuple((item.task, item.sha256) for item in children):
        parser.error("whole-tree replacement requires every --child to have one identical --descendant pin")
    try:
        targets = (parsed.old_target, parsed.new_target, parsed.parent_target, *parsed.protected_target)
        _ = tuple(canonical_target(target) for target in targets)
    except ReplaceError as exc:
        parser.error(str(exc))
    result = Args(
        root=parsed.root.expanduser().resolve(strict=False),
        old_task=parsed.old_task,
        successor_task=parsed.successor_task,
        old_target=parsed.old_target,
        new_target=parsed.new_target,
        parent_target=parsed.parent_target,
        old_sha256=parsed.old_sha256,
        todo_sha256=parsed.todo_sha256,
        children=children,
        old_pane_id=parsed.old_pane_id,
        old_pane_pid=parsed.old_pane_pid,
        old_pane_start_ticks=parsed.old_pane_start_ticks,
        old_session_id=parsed.old_session_id.lower(),
        authority_file=parsed.authority_file,
        authority_lines=parsed.authority_lines,
        authority_sha256=parsed.authority_sha256,
        authority_envelope_task=parsed.authority_envelope_task,
        authority_envelope_sha256=parsed.authority_envelope_sha256,
        successor_item_lines=item_ranges,
        protected_targets=tuple(parsed.protected_target),
        audit_output=parsed.audit_output.resolve(strict=False),
        preparer=parsed.preparer.strip(),
        reviewer=parsed.reviewer.strip(),
        closed_owner_audit=(parsed.closed_owner_audit.resolve(strict=False) if parsed.closed_owner_audit is not None else None),
        closed_owner_audit_sha256=parsed.closed_owner_audit_sha256,
        old_queue_sha256=parsed.old_queue_sha256,
        old_pcodx_state_sha256=parsed.old_pcodx_state_sha256,
        old_pcodx_ledger_sha256=parsed.old_pcodx_ledger_sha256,
        old_pcodx_wrapper_sha256=parsed.old_pcodx_wrapper_sha256,
        protected_targets_sha256=parsed.protected_targets_sha256,
        authority_envelope_file_sha256=parsed.authority_envelope_file_sha256,
        descendants=descendants,
        empty_tree_envelope_sha256=parsed.empty_tree_envelope_sha256,
        descendant_authority_envelope_sha256=parsed.descendant_authority_envelope_sha256,
        source1611_parent_sha256=parsed.source1611_parent_sha256,
        source1611_rollout=(parsed.source1611_rollout.resolve(strict=False) if parsed.source1611_rollout is not None else None),
        source1611_session_root=(parsed.source1611_session_root.resolve(strict=False) if parsed.source1611_session_root is not None else None),
        source1611_rollout_device=parsed.source1611_rollout_device,
        source1611_rollout_inode=parsed.source1611_rollout_inode,
        source1611_rollout_holder_pid=parsed.source1611_rollout_holder_pid,
        source1611_rollout_holder_start_ticks=parsed.source1611_rollout_holder_start_ticks,
        source1611_rollout_fd=(parsed.source1611_rollout_fd if parsed.source1611_rollout_fd is not None else -1),
        source1611_rollout_session_meta_sha256=parsed.source1611_rollout_session_meta_sha256,
        source1611_rollout_holder_exe=(parsed.source1611_rollout_holder_exe.resolve(strict=False) if parsed.source1611_rollout_holder_exe is not None else None),
        source1611_rollout_holder_exe_link=parsed.source1611_rollout_holder_exe_link,
        source1611_rollout_holder_exe_device=parsed.source1611_rollout_holder_exe_device,
        source1611_rollout_holder_exe_inode=parsed.source1611_rollout_holder_exe_inode,
        source1611_rollout_holder_exe_mode=parsed.source1611_rollout_holder_exe_mode,
        source1611_rollout_holder_exe_uid=parsed.source1611_rollout_holder_exe_uid,
        source1611_rollout_holder_exe_nlink=parsed.source1611_rollout_holder_exe_nlink,
        source1611_rollout_holder_exe_size=parsed.source1611_rollout_holder_exe_size,
        source1611_rollout_holder_exe_sha256=parsed.source1611_rollout_holder_exe_sha256,
        source1611_rollout_holder_argv_sha256=parsed.source1611_rollout_holder_argv_sha256,
    )
    try:
        validate_targets(result)
    except ReplaceError as exc:
        parser.error(str(exc))
    return result


def task_path(root: Path, task: str) -> Path:
    lexical = root.joinpath(*Path(task).parts)
    candidate = lexical.resolve(strict=False)
    if candidate != lexical or candidate == root or root not in candidate.parents:
        raise ReplaceError(f"task escapes work-log root: {task}")
    return candidate


def file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def read_snapshot(path: Path, label: str) -> Snapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReplaceError(f"{label} is unavailable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or not before.st_mode & stat.S_IWUSR or before.st_mode & 0o022:
            raise ReplaceError(f"{label} must be one owner-owned, owner-writable regular file")
        if set(os.listxattr(fd)) & POSIX_ACL_XATTRS:
            raise ReplaceError(f"{label} has a POSIX ACL that this transaction cannot preserve")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if file_identity(before) != file_identity(after):
        raise ReplaceError(f"{label} changed while it was read")
    return Snapshot(path, b"".join(chunks), after)


def require_snapshot(expected: Snapshot, label: str) -> None:
    current = read_snapshot(expected.path, label)
    if file_identity(current.state) != file_identity(expected.state) or current.data != expected.data:
        raise ReplaceError(f"{label} changed during manager replacement")


def directory_fd(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )


def sync_directory(path: Path) -> None:
    fd = directory_fd(path)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_snapshot_at(parent_fd: int, name: str, path: Path, label: str) -> Snapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ReplaceError(f"{label} is unavailable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or not before.st_mode & stat.S_IWUSR or before.st_mode & 0o022:
            raise ReplaceError(f"{label} must be one owner-owned, owner-writable regular file")
        if set(os.listxattr(fd)) & POSIX_ACL_XATTRS:
            raise ReplaceError(f"{label} has a POSIX ACL that this transaction cannot preserve")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if file_identity(before) != file_identity(after):
        raise ReplaceError(f"{label} changed while it was read")
    return Snapshot(path, b"".join(chunks), after)


def require_snapshot_at(expected: Snapshot, label: str, parent_fd: int) -> None:
    current = read_snapshot_at(parent_fd, expected.path.name, expected.path, label)
    if file_identity(current.state) != file_identity(expected.state) or current.data != expected.data:
        raise ReplaceError(f"{label} changed during manager replacement")


def anonymous_file(parent_fd: int, data: bytes, mode: int, gid: int) -> tuple[int, os.stat_result]:
    if not getattr(os, "O_TMPFILE", 0):
        raise ReplaceError("atomic manager replacement requires Linux O_TMPFILE support")
    flags = os.O_RDWR | os.O_TMPFILE | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(".", flags, mode, dir_fd=parent_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short anonymous-file write")
            view = view[written:]
        os.fchown(fd, -1, gid)
        os.fchmod(fd, mode)
        os.fsync(fd)
        return fd, os.fstat(fd)
    except Exception:
        os.close(fd)
        raise


def link_fd(fd: int, parent_fd: int, name: str) -> None:
    source = os.fsencode(f"/proc/self/fd/{fd}")
    result = LIBC.linkat(
        AT_FDCWD,
        ctypes.c_char_p(source),
        parent_fd,
        ctypes.c_char_p(os.fsencode(name)),
        AT_SYMLINK_FOLLOW,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def rename_at2(parent_fd: int, source: str, target: str, flags: int) -> None:
    result = LIBC.renameat2(
        parent_fd,
        ctypes.c_char_p(os.fsencode(source)),
        parent_fd,
        ctypes.c_char_p(os.fsencode(target)),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{source}->{target}")


def replace_snapshot(expected: Snapshot, data: bytes, label: str) -> Snapshot:
    """Exchange a prepared inode with the exact expected inode; never clobber a rebound path."""

    parent_fd = directory_fd(expected.path.parent)
    fd = -1
    stage_name = f".{expected.path.name}.omo-manager-replace-stage-{os.urandom(16).hex()}"
    try:
        require_snapshot_at(expected, label, parent_fd)
        fd, _ = anonymous_file(parent_fd, data, stat.S_IMODE(expected.state.st_mode), expected.state.st_gid)
        link_fd(fd, parent_fd, stage_name)
        prepared = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        os.close(fd)
        fd = -1
        require_snapshot_at(expected, label, parent_fd)
        rename_at2(parent_fd, stage_name, expected.path.name, RENAME_EXCHANGE)
        try:
            target_state = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
            receipt_state = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise NamespaceMutationError(f"{label} exchange committed but its namespace cannot be inspected") from exc
        target_identity = (target_state.st_dev, target_state.st_ino)
        receipt_identity = (receipt_state.st_dev, receipt_state.st_ino)
        if target_identity != (prepared.st_dev, prepared.st_ino) or receipt_identity != (expected.state.st_dev, expected.state.st_ino):
            try:
                current_target = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
                current_receipt = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
                if (current_target.st_dev, current_target.st_ino) != target_identity or (
                    current_receipt.st_dev,
                    current_receipt.st_ino,
                ) != receipt_identity:
                    raise NamespaceMutationError(f"{label} exchange paths changed before safe restoration")
                rename_at2(parent_fd, stage_name, expected.path.name, RENAME_EXCHANGE)
                restored = os.stat(expected.path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (restored.st_dev, restored.st_ino) != (expected.state.st_dev, expected.state.st_ino):
                    raise NamespaceMutationError(f"{label} original inode was not restored")
                os.fsync(parent_fd)
            except (OSError, NamespaceMutationError) as exc:
                raise NamespaceMutationError(f"{label} was concurrently rebound; foreign state was preserved for recovery") from exc
            raise ReplaceError(f"{label} was concurrently rebound; its original inode was restored")
        try:
            receipt = read_snapshot_at(parent_fd, stage_name, expected.path.parent / stage_name, f"{label} displaced-inode receipt")
            if file_identity(receipt.state) != file_identity(receipt_state) or receipt.data != expected.data:
                raise NamespaceMutationError(f"{label} displaced-inode receipt changed")
            snapshot = read_snapshot_at(parent_fd, expected.path.name, expected.path, f"updated {label}")
            if file_identity(snapshot.state) != file_identity(target_state) or snapshot.data != data:
                raise NamespaceMutationError(f"{label} was rebound after its committed exchange")
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise CommittedMutationError(label, snapshot, exc) from exc
            require_snapshot_at(receipt, f"{label} displaced-inode receipt", parent_fd)
            return snapshot
        except (CommittedMutationError, NamespaceMutationError):
            raise
        except Exception as exc:
            raise NamespaceMutationError(f"{label} committed but verification failed; receipts were preserved") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def create_snapshot(path: Path, data: bytes, mode: int, gid: int | None = None) -> Snapshot:
    """Publish one prepared inode with linkat's atomic no-replace semantics."""

    parent_fd = directory_fd(path.parent)
    fd = -1
    try:
        fd, prepared = anonymous_file(parent_fd, data, mode, os.getgid() if gid is None else gid)
        link_fd(fd, parent_fd, path.name)
        os.close(fd)
        fd = -1
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        snapshot = read_snapshot_at(parent_fd, path.name, path, f"published {path.name}")
        if (current.st_dev, current.st_ino) != (prepared.st_dev, prepared.st_ino) or snapshot.data != data:
            raise NamespaceMutationError(f"published {path.name} was rebound; foreign state was preserved")
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise CommittedMutationError(path.name, snapshot, exc) from exc
        return snapshot
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def remove_created(expected: Snapshot) -> None:
    """Quarantine the exact created inode atomically; never unlink a rebound path."""

    parent_fd = directory_fd(expected.path.parent)
    receipt_name = f".{expected.path.name}.omo-manager-replace-removed-{os.urandom(16).hex()}"
    try:
        require_snapshot_at(expected, "transaction-created successor", parent_fd)
        rename_at2(parent_fd, expected.path.name, receipt_name, RENAME_NOREPLACE)
        receipt_state = os.stat(receipt_name, dir_fd=parent_fd, follow_symlinks=False)
        if (receipt_state.st_dev, receipt_state.st_ino) != (expected.state.st_dev, expected.state.st_ino):
            try:
                rename_at2(parent_fd, receipt_name, expected.path.name, RENAME_NOREPLACE)
                os.fsync(parent_fd)
            except OSError as exc:
                raise NamespaceMutationError("successor path was rebound and the foreign inode could not be restored") from exc
            raise ReplaceError("successor path was rebound; the foreign inode was restored")
        receipt = read_snapshot_at(parent_fd, receipt_name, expected.path.parent / receipt_name, "removed-successor receipt")
        if receipt.data != expected.data:
            raise NamespaceMutationError("removed-successor receipt changed; state preserved for recovery")
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def metadata(data: bytes, root: Path, label: str) -> TaskMetadata:
    try:
        value = parse_task_metadata(data.decode(), root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise ReplaceError(f"{label} has invalid task frontmatter: {exc}") from exc
    if value is None:
        raise ReplaceError(f"{label} has no task frontmatter")
    return value


def migrate_manager_owner(data: bytes, old_owner: str, new_owner: str, root: Path) -> bytes:
    """Call the shared migration transform lazily to keep helper imports acyclic."""

    manager_owner_migration_text = cast(
        Callable[[str, str, str, Path], str],
        getattr(importlib.import_module("omo_manager.omo_task"), "manager_owner_migration_text"),
    )

    try:
        return manager_owner_migration_text(data.decode(), old_owner, new_owner, root).encode()
    except (UnicodeDecodeError, ValueError, TaskFrontmatterError) as exc:
        raise ReplaceError(f"cannot prepare manager-owner migration: {exc}") from exc


def replace_v1_fields(
    text: str,
    *,
    status: str,
    runat: str,
    blocked_on: str,
    remove_session: bool,
    tool: str | None = None,
) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ReplaceError("manager task has no frontmatter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ReplaceError("manager task frontmatter is unterminated") from exc
    indexes: dict[str, list[int]] = {}
    for index, line in enumerate(lines[1:closing], start=1):
        key, separator, _value = line.rstrip("\r\n").partition(":")
        if separator:
            indexes.setdefault(key, []).append(index)
    required_fields = ("status", "runat", "tool") if tool is not None else ("status", "runat")
    for key in required_fields:
        if len(indexes.get(key, [])) != 1:
            raise ReplaceError(f"manager task requires exactly one {key} field")
    ending = "\r\n" if lines[0].endswith("\r\n") else "\n"
    lines[indexes["status"][0]] = f"status: {status}{ending}"
    lines[indexes["runat"][0]] = f"runat: {runat}{ending}"
    if tool is not None:
        lines[indexes["tool"][0]] = f"tool: {tool}{ending}"
    removed = [*indexes.get("blocked_on", []), *(indexes.get("session_id", []) if remove_session else [])]
    for index in sorted(removed, reverse=True):
        del lines[index]
        closing -= 1
    status_index = next(index for index, line in enumerate(lines[1:closing], start=1) if line.rstrip("\r\n").partition(":")[0] == "status")
    if blocked_on:
        lines.insert(status_index + 1, f"blocked_on: {blocked_on}{ending}")
    return "".join(lines)


def frontmatter_only(text: str) -> str:
    """Retain task metadata while excluding inherited agent-authored body text."""

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ReplaceError("manager task has no frontmatter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ReplaceError("manager task frontmatter is unterminated") from exc
    ending = "\r\n" if lines[0].endswith("\r\n") else "\n"
    return "".join(lines[: closing + 1]) + ending


def authority_material(args: Args, snapshot: Snapshot, envelope: Snapshot) -> tuple[str, ...]:
    """Authenticate failed-manager evidence without copying private mail into task records."""

    try:
        authority_parent = snapshot.path.parent.stat()
    except OSError as exc:
        raise ReplaceError(f"replacement authority directory is unavailable: {exc}") from exc
    if (
        snapshot.path != args.root.joinpath(*Path(args.authority_file).parts)
        or not stat.S_ISDIR(authority_parent.st_mode)
        or authority_parent.st_uid != os.getuid()
        or stat.S_IMODE(authority_parent.st_mode) & 0o077
        or stat.S_IMODE(snapshot.state.st_mode) & 0o077
    ):
        raise ReplaceError("replacement authority source and directory must be owner-private without symlink traversal")
    if digest(snapshot.data) != args.authority_sha256:
        raise ReplaceError("authority digest changed")
    if (is_pcodx_replacement(args) or is_source1485_replacement(args)) and digest(envelope.data) != args.authority_envelope_file_sha256:
        raise ReplaceError("authority envelope file bytes changed")
    try:
        lines = snapshot.data.decode().splitlines()
        envelope_text = envelope.data.decode()
    except UnicodeDecodeError as exc:
        raise ReplaceError(f"authority source or envelope is not UTF-8: {exc}") from exc
    if args.authority_lines.end > len(lines):
        raise ReplaceError("authority line range exceeds the bound source")
    excerpt = "\n".join(lines[args.authority_lines.start - 1 : args.authority_lines.end])
    canonical_excerpt = "\n".join(excerpt.splitlines())
    if not excerpt.strip():
        raise ReplaceError("authority excerpt must not be empty")
    if is_source_only_semantic_exception(args):
        if envelope.path != task_path(args.root, args.old_task) or digest(envelope.data) != args.old_sha256 or canonical_excerpt != source_only_directive(args):
            raise ReplaceError("source-only authority or old-task before image changed")
        return ()
    locator = f"{args.authority_file}:{args.authority_lines.start}-{args.authority_lines.end}"
    matches = list(HUMAN_ENVELOPE_RE.finditer(envelope_text))
    expected_matches = 2 if is_source1292_empty_tree(args) or is_source1292_descendant_tree(args) else 1
    selected = [candidate for candidate in matches if candidate.group("source") == locator]
    source1485_carrier = is_source1485_semantic_exception(args) and args.authority_envelope_task in SOURCE1485_ENVELOPE_TASKS and args.authority_envelope_sha256 == SOURCE1485_ENVELOPE_SHA256
    if (not source1485_carrier and len(matches) != expected_matches) or len(selected) != 1:
        raise ReplaceError("authority envelope must contain exactly the expected blocks and bound source locator")
    match = selected[0]
    if digest(match.group(0).encode()) != args.authority_envelope_sha256:
        raise ReplaceError("authority envelope block digest changed")
    canonical_envelope_body = "\n".join(match.group("body").splitlines())
    source1485_expected_body = "\n".join((SOURCE1485_ENVELOPE_SUBJECT, *canonical_excerpt.splitlines()[1:]))
    if canonical_envelope_body != canonical_excerpt and not (source1485_carrier and canonical_envelope_body == source1485_expected_body):
        raise ReplaceError("authority envelope does not contain exactly the bound source excerpt")
    items: list[str] = []
    for line_range in args.successor_item_lines:
        if line_range.start < args.authority_lines.start or line_range.end > args.authority_lines.end:
            raise ReplaceError("successor item line range is outside the authenticated authority excerpt")
        evidence = "\n".join(lines[line_range.start - 1 : line_range.end])
        if not evidence.strip():
            raise ReplaceError("successor queue item source lines must not be empty")
        item_locator = f"{args.authority_file}:{line_range.start}-{line_range.end}"
        items.append(
            "Read and execute the private authenticated Human instruction at "
            f"{item_locator} (source-sha256={args.authority_sha256}; "
            f"envelope={args.authority_envelope_task}; envelope-block-sha256={args.authority_envelope_sha256})."
        )
    selected_evidence = "\n".join("\n".join(lines[line_range.start - 1 : line_range.end]) for line_range in args.successor_item_lines)
    pcodx_replacements = list(PCODX_REPLACE_EVIDENCE_RE.finditer(selected_evidence))
    pcodx_directives = PCODX_REPLACE_DIRECTIVE_RE.findall(selected_evidence)
    selected_nonempty_lines = [line.strip() for line in selected_evidence.splitlines() if line.strip()]
    pcodx_replacement = pcodx_replacements[0] if len(pcodx_replacements) == 1 else None
    exact_pcodx_replacement = (
        len(pcodx_directives) == 1
        and pcodx_replacement is not None
        and selected_nonempty_lines == [pcodx_replacement.group(0).strip(), "Just do it"]
        and pcodx_replacement.group("task") == args.old_task
        and canonical_target(pcodx_replacement.group("target")) == canonical_target(args.old_target)
    )
    exact_guest1269_replacement = is_guest1269_replacement(args) and selected_evidence == GUEST1269_REPLACEMENT[4]
    if (
        FAILED_MANAGER_EVIDENCE_RE.search(selected_evidence) is None
        and not exact_guest1269_replacement
        and not exact_pcodx_replacement
        and not is_source1289_semantic_exception(args)
        and not is_source1443_semantic_exception(args)
        and not is_source1477_semantic_exception(args)
        and not is_source1485_semantic_exception(args)
        and not is_source_only_semantic_exception(args)
    ):
        raise ReplaceError("authenticated authority does not explicitly prove failure, non-execution, and replacement")
    if is_pcodx_replacement(args) and not all(value in selected_evidence for value in (args.old_task, args.old_target)):
        raise ReplaceError("authenticated PCODX replacement authority must name the exact old task and protected target")
    if is_pcodx_replacement(args) and "pcodx" not in selected_evidence.lower():
        raise ReplaceError("authenticated PCODX replacement authority must explicitly identify PCODX")
    if is_pcodx_replacement(args):
        subject_token = re.compile(rf"(?im)^Subject:.*(?<![A-Za-z0-9_./-]){re.escape(args.old_task)}(?![A-Za-z0-9_./-])")
        close_directive = re.compile(rf"(?im)^\s*close\s+{re.escape(args.old_target)}(?=$|[\s,.;:])")
        source_text = snapshot.data.decode()
        if (len(subject_token.findall(source_text)) != 1 and not exact_pcodx_replacement) or (len(close_directive.findall(selected_evidence)) != 1 and not exact_pcodx_replacement):
            raise ReplaceError("authenticated PCODX authority must directly close the exact named task and protected target")
    return tuple(items)


def source1601_material(args: Args) -> Snapshot | None:
    if not source1601_required(args):
        return None
    path = task_path(args.root, SOURCE1601_FILE)
    snapshot = read_snapshot(path, "Source-1601 replacement authority")
    try:
        parent = path.parent.stat()
    except OSError as exc:
        raise ReplaceError(f"Source-1601 authority directory is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
        or stat.S_IMODE(snapshot.state.st_mode) & 0o077
        or digest(snapshot.data) != SOURCE1601_SHA256
    ):
        raise ReplaceError("Source-1601 authority source or digest changed")
    try:
        lines = snapshot.data.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise ReplaceError(f"Source-1601 authority is not UTF-8: {exc}") from exc
    excerpt = "\n".join(lines[SOURCE1601_LINES[0] - 1 : SOURCE1601_LINES[1]])
    if excerpt != SOURCE1601_DIRECTIVE:
        raise ReplaceError("Source-1601 original-Human-goals directive changed")
    return snapshot


def source1597_direct_worker_material(args: Args) -> Snapshot | None:
    """Bind the original Human source of the sole retained direct-worker goal."""

    if not source1597_direct_worker_required(args):
        return None
    path = task_path(args.root, SOURCE1597_FILE)
    snapshot = read_snapshot(path, "Source-1597 direct-worker authority")
    try:
        parent = path.parent.stat()
    except OSError as exc:
        raise ReplaceError(f"Source-1597 authority directory is unavailable: {exc}") from exc
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
        or stat.S_IMODE(snapshot.state.st_mode) & 0o077
        or digest(snapshot.data) != SOURCE1597_SHA256
    ):
        raise ReplaceError("Source-1597 direct-worker authority source or digest changed")
    try:
        lines = snapshot.data.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise ReplaceError(f"Source-1597 direct-worker authority is not UTF-8: {exc}") from exc
    excerpt = "\n".join(lines[SOURCE1597_LINES[0] - 1 : SOURCE1597_LINES[1]])
    if excerpt != SOURCE1597_DIRECTIVE:
        raise ReplaceError("Source-1597 direct-worker original-Human directive changed")
    return snapshot


def validate_source1611_direct_worker_parent(args: Args, snapshot: Snapshot, *, active_child: str | None = None) -> None:
    """Prove the serial parent is the exact active first Source-1611 successor."""

    expected_path = task_path(args.root, SOURCE1611_SUCCESSOR_TASK)
    if snapshot.path != expected_path or digest(snapshot.data) != args.source1611_parent_sha256:
        raise ReplaceError("Source-1611 direct-worker parent task or digest changed")
    parent = metadata(snapshot.data, args.root, "Source-1611 direct-worker parent")
    if (
        parent.status != "long_running"
        or canonical_target(parent.runat) != canonical_target(SOURCE1611_SUCCESSOR_TARGET)
        or canonical_target(parent.managerat) != canonical_target(SOURCE1611_PARENT_TARGET)
        or parent.tool != "codex"
        or not parent.is_manager
    ):
        raise ReplaceError("Source-1611 direct-worker parent is not the exact active successor owner")
    if authoritative_active_target_task_paths(args.root, args.parent_target) != (expected_path.resolve(),):
        raise ReplaceError("Source-1611 direct-worker parent is not the sole active config:23 owner")
    if active_child is not None and active_child not in active_child_task_refs(args.root, expected_path, args.parent_target):
        raise ReplaceError("Source-1611 direct-worker is no longer an active child of its exact parent")


def source1611_direct_worker_parent_material(args: Args, *, active_child: str | None = None) -> Snapshot | None:
    if not source1597_direct_worker_required(args):
        return None
    snapshot = read_snapshot(task_path(args.root, SOURCE1611_SUCCESSOR_TASK), "Source-1611 direct-worker parent")
    validate_source1611_direct_worker_parent(args, snapshot, active_child=active_child)
    return snapshot


def require_source1611_direct_worker_parent(args: Args, snapshot: Snapshot | None, label: str, *, active_child: str | None = None) -> None:
    if snapshot is None:
        return
    require_snapshot(snapshot, label)
    validate_source1611_direct_worker_parent(args, snapshot, active_child=active_child)


def empty_tree_authority_material(args: Args, envelope: Snapshot) -> tuple[Snapshot, str]:
    path = task_path(args.root, SOURCE1292_FILE)
    snapshot = read_snapshot(path, "Source-1292 empty-tree authority")
    try:
        parent = path.parent.stat()
    except OSError as exc:
        raise ReplaceError(f"Source-1292 authority directory is unavailable: {exc}") from exc
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077 or stat.S_IMODE(snapshot.state.st_mode) & 0o077:
        raise ReplaceError("Source-1292 authority source and directory must be owner-private")
    if digest(snapshot.data) != SOURCE1292_SHA256:
        raise ReplaceError("Source-1292 empty-tree authority digest changed")
    try:
        source_lines = snapshot.data.decode().splitlines()
        envelope_text = envelope.data.decode()
    except UnicodeDecodeError as exc:
        raise ReplaceError(f"Source-1292 authority is not UTF-8: {exc}") from exc
    excerpt = "\n".join(source_lines[SOURCE1292_LINES[0] - 1 : SOURCE1292_LINES[1]])
    locator = f"{SOURCE1292_FILE}:{SOURCE1292_LINES[0]}-{SOURCE1292_LINES[1]}"
    matches = [candidate for candidate in HUMAN_ENVELOPE_RE.finditer(envelope_text) if candidate.group("source") == locator]
    if (
        len(matches) != 1
        or digest(matches[0].group(0).encode()) != source1292_envelope_sha256(args)
        or "\n".join(matches[0].group("body").splitlines()) != "\n".join(excerpt.splitlines())
        or "Close either way. Directly use tmux if needed" not in excerpt
    ):
        raise ReplaceError("Source-1292 empty-tree envelope binding changed")
    item = (
        "Read and execute the private authenticated Human close instruction at "
        f"{locator} (source-sha256={SOURCE1292_SHA256}; envelope={args.authority_envelope_task}; "
        f"envelope-block-sha256={source1292_envelope_sha256(args)})."
    )
    return snapshot, item


def protected_inventory(args: Args, inventory: dict[str, PaneIdentity]) -> tuple[PaneIdentity, ...]:
    protected = tuple(canonical_target(target) for target in args.protected_targets)
    if len(set(protected)) != len(protected):
        raise ReplaceError("protected targets must be unique")
    identities = tuple(inventory.get(target) for target in protected)
    if any(identity is None for identity in identities):
        raise ReplaceError("every protected target must retain one exact live pane/process identity")
    return tuple(identity for identity in identities if identity is not None)


def protected_inventory_digest(args: Args, inventory: dict[str, PaneIdentity]) -> str:
    return json_digest(
        [
            {
                "target": identity.target,
                "pane_id": identity.pane_id,
                "pid": identity.pid,
                "start_ticks": identity.start_ticks,
            }
            for identity in protected_inventory(args, inventory)
        ]
    )


def validate_protected_bindings(args: Args, inventory: dict[str, PaneIdentity]) -> None:
    if not uses_protected_inventory(args):
        return
    protected = tuple(canonical_target(target) for target in args.protected_targets)
    if is_source_only_semantic_exception(args):
        expected = tuple(sorted(target for target in inventory if target != canonical_target(args.old_target)))
        if protected != expected:
            raise ReplaceError("source-only replacement protected targets are not the complete canonical live inventory")
    if protected_inventory_digest(args, inventory) != args.protected_targets_sha256:
        raise ReplaceError("protected pane/process inventory changed")


def pcodx_binding(args: Args, pane: PaneIdentity) -> dict[str, str]:
    if START_PCODX_ENV_KEYS != PCODX_ENV_KEYS:
        raise ReplaceError("installed PCODX custody schema changed")
    state = pcodx_state(StartPane(pane.target, pane.pane_id, "", "", Path(), pane.pid))
    if tuple(state) != PCODX_ENV_KEYS or json_digest(state) != args.old_pcodx_state_sha256:
        raise ReplaceError("live PCODX identity, session, or custody changed")
    ledger = read_snapshot(Path(state["PCODX_LEDGER_PATH"]), "live PCODX ledger")
    if digest(ledger.data) != args.old_pcodx_ledger_sha256:
        raise ReplaceError("live PCODX ledger bytes changed")
    wrapper = read_snapshot(Path(__file__).resolve().with_name("pcodx"), "installed PCODX wrapper")
    if digest(wrapper.data) != args.old_pcodx_wrapper_sha256:
        raise ReplaceError("installed PCODX wrapper bytes changed")
    return state


def validate_live_bindings(args: Args, inventory: dict[str, PaneIdentity], *, require_descendants: bool = True) -> None:
    old = inventory.get(canonical_target(args.old_target))
    expected = PaneIdentity(canonical_target(args.old_target), args.old_pane_id, args.old_pane_pid, args.old_pane_start_ticks)
    if old != expected:
        raise ReplaceError(f"old manager pane identity changed: expected {expected}, found {old}")
    if canonical_target(args.new_target) in inventory:
        raise ReplaceError("successor target is already live; launch-before-singular-proof is rejected")
    for item in args.descendants if require_descendants else ():
        expected_child = PaneIdentity(canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks)
        if inventory.get(expected_child.target) != expected_child:
            raise ReplaceError(f"active descendant pane identity changed: {item.task}")
    validate_protected_bindings(args, inventory)
    if source1611_rollout_custody_requested(args):
        custody = source1611_rollout_custody(args)
        if custody is None or custody.session_id != args.old_session_id:
            raise ReplaceError("Source-1611 process-held rollout does not prove the exact old Codex session")
    if is_pcodx_replacement(args):
        _ = pcodx_binding(args, expected)


def validate_source1485_protected_set(
    args: Args,
    inventory: dict[str, PaneIdentity],
    child_metadata: tuple[TaskMetadata, ...],
) -> None:
    if not is_source1485_replacement(args):
        return
    protected = {canonical_target(target) for target in args.protected_targets}
    parent = canonical_target(args.parent_target)
    if parent not in inventory or parent not in protected:
        raise ReplaceError("Source-1485 replacement must protect its live reporting parent")
    live_children = {canonical_target(value.runat) for value in child_metadata if canonical_target(value.runat) in inventory}
    if not live_children.issubset(protected):
        raise ReplaceError("Source-1485 replacement must protect every live retained child pane")
    if any((value.is_manager or value.status in {"running", "long_running"}) and canonical_target(value.runat) not in inventory for value in child_metadata):
        raise ReplaceError("Source-1485 active manager or running child lacks its bound live pane")
    if any(target_session(target).startswith("h") for target in protected):
        raise ReplaceError("Source-1485 protected inventory cannot include a human-owned target")


def old_and_successor_text(
    old_data: bytes,
    root: Path,
    old_target: str,
    new_target: str,
    authority_items: tuple[str, ...],
    *,
    human_goals_only: bool = False,
) -> tuple[bytes, bytes, tuple[str, ...]]:
    old_text = old_data.decode()
    old_metadata = metadata(old_data, root, "old manager task")
    queue = (*old_metadata.pending_task_items, *authority_items)
    if len(set(queue)) != len(queue):
        raise ReplaceError("successor queue would contain duplicate open items")
    cleared = render_pending_items(old_text, ())
    old_after = update_frontmatter_status(cleared, "done", "", root)
    successor = replace_v1_fields(
        old_text,
        status="blocked",
        runat=new_target,
        blocked_on=SUCCESSOR_BLOCKER,
        remove_session=True,
        tool="codex",
    )
    if human_goals_only:
        successor = frontmatter_only(successor)
    successor = render_pending_items(successor, queue)
    successor_metadata = parse_task_metadata(successor, root)
    if successor_metadata is None or successor_metadata.pending_task_items != queue:
        raise ReplaceError("successor construction did not preserve the manager queue")
    if successor_metadata.runat != new_target or successor_metadata.status != "blocked" or not successor_metadata.is_manager:
        raise ReplaceError("successor construction did not produce one blocked manager")
    old_after_metadata = parse_task_metadata(old_after, root)
    if old_after_metadata is None or old_after_metadata.status != "done" or old_after_metadata.pending_task_items:
        raise ReplaceError("old-manager construction did not produce one empty done record")
    if old_metadata.runat != old_target:
        raise ReplaceError("old manager target drifted")
    return old_after.encode(), successor.encode(), queue


def todo_task_path(root: Path, value: str) -> Path | None:
    candidate = Path(value)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve(strict=False)
    return resolved if root in resolved.parents else None


def todo_replacement(data: bytes, root: Path, old_path: Path, successor_path: Path, old_target: str, new_target: str) -> bytes:
    try:
        text = data.decode()
    except UnicodeDecodeError as exc:
        raise ReplaceError(f"TODO is not UTF-8: {exc}") from exc
    lines = text.splitlines(keepends=True)
    contents = [line.rstrip("\r\n") for line in lines]
    headings = {name: [index for index, value in enumerate(contents) if value == f"{name}:"] for name in ("current", "human pending", "low priority", "previous")}
    if any(len(indexes) != 1 for indexes in headings.values()):
        raise ReplaceError("TODO must contain one canonical lifecycle section of each kind")
    order = tuple(headings[name][0] for name in ("current", "human pending", "low priority", "previous"))
    if order != tuple(sorted(order)):
        raise ReplaceError("TODO lifecycle sections are out of order")
    old_rows: list[tuple[int, re.Match[str]]] = []
    successor_rows = 0
    for index, content in enumerate(contents):
        match = TODO_ROW_RE.fullmatch(content)
        if match is None:
            if old_path.name in content or successor_path.name in content:
                raise ReplaceError("TODO contains a malformed old or successor row")
            continue
        resolved = todo_task_path(root, match.group(1))
        if resolved == old_path:
            old_rows.append((index, match))
        elif resolved == successor_path:
            successor_rows += 1
    if len(old_rows) != 1 or successor_rows:
        raise ReplaceError("TODO must contain exactly one old-manager row and no successor row")
    index, match = old_rows[0]
    if not (headings["current"][0] < index < headings["human pending"][0] or headings["human pending"][0] < index < headings["low priority"][0]):
        raise ReplaceError("old-manager TODO row must be current or human pending")
    suffix = (match.group(2) or "").strip()
    if suffix != old_target:
        raise ReplaceError("old-manager TODO row must name only its exact target")
    old_ref = match.group(1)
    if contents[index].strip().split(maxsplit=1)[0] != old_ref:
        raise ReplaceError("old-manager TODO row must use one canonical unquoted task reference")
    successor_ref = successor_path.relative_to(root).as_posix()
    ending = lines[index][len(contents[index]) :]
    prefix = contents[index][: match.start(1)]
    lines[index] = f"{prefix}{successor_ref} {new_target}{ending}"
    previous_index = headings["previous"][0]
    default_ending = "\r\n" if "\r\n" in text else "\n"
    previous_ending = lines[previous_index][len(contents[previous_index]) :] or default_ending
    if not lines[previous_index].endswith(("\n", "\r")):
        lines[previous_index] += previous_ending
    lines.insert(previous_index + 1, f"{old_ref} {old_target}{previous_ending}")
    return "".join(lines).encode()


def require_source1485_umbrella_previous(root: Path, data: bytes) -> None:
    """Require the stale umbrella's one historical row before root replacement."""

    try:
        lines = data.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise ReplaceError(f"TODO is not UTF-8: {exc}") from exc
    section = ""
    matches: list[tuple[str, str]] = []
    for line in lines:
        if line in {"current:", "human pending:", "low priority:", "previous:"}:
            section = line.removesuffix(":")
            continue
        match = TODO_ROW_RE.fullmatch(line)
        if match is None:
            continue
        path = todo_task_path(root, match.group(1))
        if path == task_path(root, SOURCE1485_UMBRELLA_TASK):
            matches.append((section, (match.group(2) or "").strip()))
    if matches != [("previous", SOURCE1485_UMBRELLA_TARGET.removesuffix(".0"))]:
        raise ReplaceError("Source-1485 root replacement requires one exact wl:7 umbrella row in TODO previous")


def pane_inventory() -> dict[str, PaneIdentity]:
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReplaceError(f"cannot inspect tmux pane inventory: {exc}") from exc
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())
        raise ReplaceError(f"tmux pane inventory failed: {detail or result.returncode}")
    inventory: dict[str, PaneIdentity] = {}
    for row in result.stdout.splitlines():
        fields = row.split("\t")
        if len(fields) != 4 or PANE_ID_RE.fullmatch(fields[1]) is None or not fields[2].isdigit() or fields[3] not in {"0", "1"}:
            raise ReplaceError("tmux pane inventory contains a malformed row")
        target = canonical_target(fields[0])
        if fields[3] == "1":
            continue
        pid = int(fields[2])
        ticks = process_start_ticks(pid)
        if ticks is None or target in inventory:
            raise ReplaceError("tmux pane inventory cannot prove one process identity per target")
        inventory[target] = PaneIdentity(target, fields[1], pid, ticks)
    return inventory


def source1611_process_stat(pid: int, proc_root: Path = Path("/proc")) -> Source1611Process:
    """Read only the stable kernel identity needed for legacy rollout custody."""

    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        fields = raw.rsplit(")", 1)[1].split()
        argv = (proc_root / str(pid) / "cmdline").read_bytes()
        return Source1611Process(
            pid,
            int(fields[1]),
            fields[0],
            int(fields[2]),
            int(fields[3]),
            int(fields[4]),
            int(fields[19]),
            digest(argv),
        )
    except (IndexError, OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReplaceError(f"cannot bind Source-1611 rollout process {pid}: {exc}") from exc


def source1611_process_tree(root_pid: int, proc_root: Path = Path("/proc")) -> tuple[Source1611Process, ...]:
    """Bind the complete descendant tree without inspecting unrelated child FDs."""

    pending = [root_pid]
    seen: set[int] = set()
    result: dict[int, Source1611Process] = {}
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        identity = source1611_process_stat(pid, proc_root)
        if identity.state == "Z":
            raise ReplaceError(f"Source-1611 rollout process became a zombie: {pid}")
        task_root = proc_root / str(pid) / "task"
        try:
            threads = tuple(task_root.iterdir())
        except OSError as exc:
            raise ReplaceError(f"cannot enumerate Source-1611 rollout process threads: {pid}: {exc}") from exc
        children: set[int] = set()
        for thread in threads:
            if not thread.name.isdigit():
                continue
            try:
                children.update(int(value) for value in (thread / "children").read_text(encoding="ascii").split())
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise ReplaceError(f"cannot enumerate Source-1611 rollout process children: {thread}: {exc}") from exc
        result[pid] = identity
        pending.extend(sorted(children - seen, reverse=True))
    return tuple(result[pid] for pid in sorted(result))


def source1611_process_comm(pid: int, proc_root: Path = Path("/proc")) -> str:
    try:
        return (proc_root / str(pid) / "comm").read_text(encoding="utf-8").removesuffix("\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReplaceError(f"cannot classify Source-1611 rollout process {pid}: {exc}") from exc


def source1611_process_environment(pid: int, proc_root: Path = Path("/proc")) -> dict[str, str]:
    try:
        values = (proc_root / str(pid) / "environ").read_bytes().split(b"\0")
        decoded = [value.decode("utf-8", errors="strict") for value in values if value]
    except (OSError, UnicodeDecodeError) as exc:
        raise ReplaceError(f"cannot inspect Source-1611 native Codex environment: {exc}") from exc
    result: dict[str, str] = {}
    for value in decoded:
        key, separator, item = value.partition("=")
        if not separator:
            raise ReplaceError("Source-1611 native Codex environment contains a malformed value")
        if key in result:
            raise ReplaceError("Source-1611 native Codex environment contains a duplicate key")
        result[key] = item
    return result


def source1611_native_codex_executable(
    args: Args,
    holder: Source1611Process,
    proc_root: Path,
) -> None:
    """Pin the process-held Codex executable and its exact launch argv.

    ``comm`` is useful only as a short kernel label.  The executable is read
    through the holder's procfs handle and hashed from that opened inode, so a
    same-named non-Codex child cannot satisfy the legacy custody exception.
    """

    asserted = args.source1611_rollout_holder_exe
    if asserted is None:
        raise ReplaceError("Source-1611 native Codex executable binding is missing")
    deleted_suffix = " (deleted)"
    try:
        proc_link = os.readlink(proc_root / str(holder.pid) / "exe")
        # npm may unlink a still-running Codex binary during its own cache
        # rotation. This incident-only fallback therefore accepts only that
        # exact kernel marker; a live substituted pathname fails closed.
        if not proc_link.endswith(deleted_suffix):
            raise ReplaceError("Source-1611 native Codex executable is not the expected deleted process image")
        proc_executable = Path(proc_link.removesuffix(deleted_suffix))
        executable = asserted.resolve(strict=False)
        descriptor = os.open(proc_root / str(holder.pid) / "exe", os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ReplaceError(f"cannot inspect Source-1611 native Codex executable: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        executable_hash = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            executable_hash.update(chunk)
    except OSError as exc:
        raise ReplaceError(f"cannot hash Source-1611 native Codex executable: {exc}") from exc
    finally:
        os.close(descriptor)
    if (
        asserted != executable
        or executable.name != "codex"
        or proc_link != args.source1611_rollout_holder_exe_link
        or proc_executable != executable
        or not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (args.source1611_rollout_holder_exe_device, args.source1611_rollout_holder_exe_inode)
        or stat.S_IMODE(opened.st_mode) != args.source1611_rollout_holder_exe_mode
        or opened.st_uid != args.source1611_rollout_holder_exe_uid
        or opened.st_nlink != args.source1611_rollout_holder_exe_nlink
        or opened.st_size != args.source1611_rollout_holder_exe_size
        or executable_hash.hexdigest() != args.source1611_rollout_holder_exe_sha256
        or holder.argv_sha256 != args.source1611_rollout_holder_argv_sha256
    ):
        raise ReplaceError("Source-1611 native Codex executable or argv provenance changed")


def source1611_rollout_metadata(path: Path, identity: tuple[int, int]) -> tuple[bytes, dict[str, object]]:
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity:
            raise ReplaceError("Source-1611 rollout path does not retain its asserted regular-file identity")
        payload = os.read(fd, ROLLOUT_METADATA_MAX_BYTES + 1)
    except OSError as exc:
        raise ReplaceError(f"cannot read Source-1611 rollout metadata: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if b"\n" not in payload:
        raise ReplaceError("Source-1611 rollout metadata is missing or exceeds its bounded first record")
    line = payload.split(b"\n", 1)[0] + b"\n"
    try:
        value = json.loads(line, object_pairs_hook=lambda pairs: _no_duplicate_json_object(pairs, "Source-1611 rollout metadata"))
    except (UnicodeDecodeError, json.JSONDecodeError, ReplaceError) as exc:
        raise ReplaceError(f"Source-1611 rollout metadata is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplaceError("Source-1611 rollout metadata must be one object")
    return line, value


def _no_duplicate_json_object(pairs: list[tuple[str, object]], label: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReplaceError(f"{label} contains duplicate key {key!r}")
        result[key] = value
    return result


def source1611_rollout_descriptor(
    holder: Source1611Process,
    rollout: Path,
    identity: tuple[int, int],
    proc_root: Path,
) -> int:
    """Require one writable FD on the exact native Codex process, twice."""

    matching: list[int] = []
    try:
        descriptors = tuple((proc_root / str(holder.pid) / "fd").iterdir())
    except OSError as exc:
        raise ReplaceError(f"cannot enumerate Source-1611 native Codex descriptors: {exc}") from exc
    for descriptor_path in descriptors:
        try:
            descriptor = int(descriptor_path.name)
            descriptor_stat = descriptor_path.stat()
            destination = Path(os.readlink(descriptor_path)).resolve(strict=True)
        except (OSError, ValueError):
            continue
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != identity or destination != rollout:
            continue
        try:
            fields = (proc_root / str(holder.pid) / "fdinfo" / descriptor_path.name).read_text(encoding="ascii").splitlines()
            flags = next(line.partition(":")[2].strip() for line in fields if line.startswith("flags:"))
            writable = int(flags, 8) & os.O_ACCMODE in {os.O_WRONLY, os.O_RDWR}
        except (OSError, StopIteration, ValueError):
            raise ReplaceError("cannot prove Source-1611 native Codex rollout descriptor mode") from None
        if not writable:
            raise ReplaceError("Source-1611 native Codex rollout descriptor is not writable")
        matching.append(descriptor)
    if len(matching) != 1:
        raise ReplaceError("Source-1611 rollout custody does not have one exact writable native Codex descriptor")
    return matching[0]


def source1611_rollout_custody(args: Args, proc_root: Path = Path("/proc")) -> Source1611RolloutCustody | None:
    """Prove the sole Source-1611 incumbent UUID from its process-held journal.

    The journal is append-only while Codex runs, so only its immutable first
    metadata record is digested.  Its live identity is the exact native-Codex
    FD, inode, descendant chain, pane TTY, and manager target environment.
    """

    if not source1611_rollout_custody_requested(args):
        return None
    asserted_rollout = args.source1611_rollout
    asserted_root = args.source1611_session_root
    if asserted_rollout is None or asserted_root is None:
        raise ReplaceError("Source-1611 rollout custody is incomplete")
    try:
        rollout = asserted_rollout.resolve(strict=True)
        session_root = asserted_root.resolve(strict=True)
    except OSError as exc:
        raise ReplaceError(f"cannot resolve Source-1611 rollout custody paths: {exc}") from exc
    if (
        asserted_rollout != rollout
        or asserted_root != session_root
        or session_root != SOURCE1611_SESSION_ROOT.resolve(strict=True)
        or not session_root.is_dir()
        or rollout.parent.parent.parent.parent != session_root
    ):
        raise ReplaceError("Source-1611 rollout custody paths are not one canonical dated session record")
    if not (re.fullmatch(r"[0-9]{4}", rollout.parent.parent.parent.name) and re.fullmatch(r"[0-9]{2}", rollout.parent.parent.name) and re.fullmatch(r"[0-9]{2}", rollout.parent.name)):
        raise ReplaceError("Source-1611 rollout custody date hierarchy is malformed")
    identity = (args.source1611_rollout_device, args.source1611_rollout_inode)
    try:
        path_stat = rollout.stat(follow_symlinks=False)
    except OSError as exc:
        raise ReplaceError(f"cannot stat Source-1611 rollout custody record: {exc}") from exc
    if not stat.S_ISREG(path_stat.st_mode) or (path_stat.st_dev, path_stat.st_ino) != identity:
        raise ReplaceError("Source-1611 rollout custody record changed identity")
    before = source1611_process_tree(args.old_pane_pid, proc_root)
    root = next((item for item in before if item.pid == args.old_pane_pid), None)
    if root is None or root.start_ticks != args.old_pane_start_ticks:
        raise ReplaceError("Source-1611 rollout pane process changed identity")
    native = tuple(item for item in before if source1611_process_comm(item.pid, proc_root) == "codex")
    if len(native) != 1:
        raise ReplaceError("Source-1611 rollout custody requires exactly one native Codex descendant")
    holder = native[0]
    if holder.pid != args.source1611_rollout_holder_pid or holder.start_ticks != args.source1611_rollout_holder_start_ticks or root.tty == 0 or holder.tty != root.tty:
        raise ReplaceError("Source-1611 native Codex holder changed identity or pane TTY")
    source1611_native_codex_executable(args, holder, proc_root)
    environment = source1611_process_environment(holder.pid, proc_root)
    if environment.get("OMO_AGENT_TMUX_TARGET") != args.old_target or environment.get("TMUX_PANE") != args.old_pane_id or environment.get("PWD") != str(args.root.parent / "dw"):
        raise ReplaceError("Source-1611 native Codex environment does not bind the exact manager pane")
    try:
        cwd = (proc_root / str(holder.pid) / "cwd").resolve(strict=True)
    except OSError as exc:
        raise ReplaceError(f"cannot resolve Source-1611 native Codex working directory: {exc}") from exc
    descriptor = source1611_rollout_descriptor(holder, rollout, identity, proc_root)
    if descriptor != args.source1611_rollout_fd:
        raise ReplaceError("Source-1611 rollout custody does not have one exact writable native Codex descriptor")
    metadata_bytes, metadata = source1611_rollout_metadata(rollout, identity)
    if digest(metadata_bytes) != args.source1611_rollout_session_meta_sha256:
        raise ReplaceError("Source-1611 rollout session metadata digest changed")
    payload = metadata.get("payload")
    if not isinstance(payload, dict):
        raise ReplaceError("Source-1611 rollout metadata payload is malformed")
    session_id = payload.get("session_id")
    started_at = payload.get("timestamp")
    record_time = metadata.get("timestamp")
    record_cwd = payload.get("cwd")
    if (
        metadata.get("type") != "session_meta"
        or metadata.get("ordinal") != 0
        or payload.get("id") != session_id
        or not isinstance(session_id, str)
        or UUID_RE.fullmatch(session_id) is None
        or session_id.lower() != args.old_session_id
        or not isinstance(record_time, str)
        or not isinstance(started_at, str)
        or payload.get("originator") != "codex-tui"
        or payload.get("source") != "cli"
        or payload.get("thread_source") != "user"
        or not isinstance(record_cwd, str)
        or not rollout.name.endswith(f"-{session_id}.jsonl")
        or any(key in metadata or key in payload for key in ("parent", "parent_id", "parent_session_id", "parent_thread_id", "fork", "fork_id", "forked_from", "forked_from_id"))
    ):
        raise ReplaceError("Source-1611 rollout metadata does not prove one fresh native Codex session")
    try:
        parsed_record_time = datetime.fromisoformat(record_time.replace("Z", "+00:00"))
        parsed_started_at = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        record_cwd_path = Path(record_cwd).resolve(strict=True)
        boot_seconds = int(next(line.partition(" ")[2] for line in (proc_root / "stat").read_text(encoding="ascii").splitlines() if line.startswith("btime ")))
        pane_started = datetime.fromtimestamp(boot_seconds + root.start_ticks / os.sysconf("SC_CLK_TCK"), timezone.utc)
    except (OSError, StopIteration, ValueError) as exc:
        raise ReplaceError(f"Source-1611 rollout metadata timestamp is invalid: {exc}") from exc
    if (
        parsed_record_time.tzinfo is None
        or parsed_started_at.tzinfo is None
        or not (parsed_started_at <= parsed_record_time <= parsed_started_at + timedelta(seconds=5))
        or record_cwd_path != cwd
        or cwd != (args.root.parent / "dw").resolve(strict=True)
        or not (pane_started <= parsed_started_at <= pane_started + timedelta(seconds=300))
    ):
        raise ReplaceError("Source-1611 rollout metadata time or working-directory identity changed")
    after = source1611_process_tree(args.old_pane_pid, proc_root)
    if before != after or source1611_rollout_descriptor(holder, rollout, identity, proc_root) != descriptor:
        raise ReplaceError("Source-1611 descendant process tree changed during rollout custody binding")
    source1611_native_codex_executable(args, holder, proc_root)
    if before != source1611_process_tree(args.old_pane_pid, proc_root):
        raise ReplaceError("Source-1611 descendant process tree changed after executable provenance binding")
    return Source1611RolloutCustody(
        rollout,
        identity[0],
        identity[1],
        holder.pid,
        holder.start_ticks,
        descriptor,
        root.start_ticks,
        digest(metadata_bytes),
        session_id.lower(),
    )


def validate_targets(args: Args) -> None:
    child_pairs = tuple((item.task, item.sha256) for item in args.children)
    descendant_pairs = tuple((item.task, item.sha256) for item in args.descendants)
    if args.authority_file == SOURCE1612_FILE:
        raise ReplaceError("Source-1612 replacement requires a separate authenticated handoff-complete proof")
    if len({item.task for item in args.children}) != len(args.children):
        raise ReplaceError("child task references must be unique")
    if len({item.task for item in args.descendants}) != len(args.descendants):
        raise ReplaceError("whole-tree descendant task references must be unique")
    if args.descendants and child_pairs != descendant_pairs:
        raise ReplaceError("whole-tree replacement requires one identical descendant pin for every child")
    pcodx_only_digests = (
        args.old_pcodx_state_sha256,
        args.old_pcodx_ledger_sha256,
        args.old_pcodx_wrapper_sha256,
    )
    if is_pcodx_replacement(args) and not all(
        SHA256_RE.fullmatch(value)
        for value in (
            args.old_queue_sha256,
            *pcodx_only_digests,
            args.protected_targets_sha256,
            args.authority_envelope_file_sha256,
        )
    ):
        raise ReplaceError("PCODX replacement requires all six exact SHA-256 bindings")
    if not is_pcodx_replacement(args) and any(pcodx_only_digests):
        raise ReplaceError("PCODX custody bindings are accepted only for a PCODX replacement")
    if is_guest1269_replacement(args):
        if SHA256_RE.fullmatch(args.old_queue_sha256) is None:
            raise ReplaceError("Source-1269 guest replacement requires the full ordered queue SHA-256 binding")
    elif is_source1485_replacement(args):
        if not is_source1485_semantic_exception(args) or SHA256_RE.fullmatch(args.old_queue_sha256) is None:
            raise ReplaceError("Source-1485 replacement requires exact authority and the full ordered queue SHA-256 binding")
        if any(SHA256_RE.fullmatch(child.queue_sha256) is None for child in args.children):
            raise ReplaceError("Source-1485 replacement requires an ordered queue SHA-256 for every active child")
        if not args.protected_targets or SHA256_RE.fullmatch(args.protected_targets_sha256) is None:
            raise ReplaceError("Source-1485 replacement requires a nonempty digest-bound protected target inventory")
        if SHA256_RE.fullmatch(args.authority_envelope_file_sha256) is None:
            raise ReplaceError("Source-1485 replacement requires the complete authority-envelope file SHA-256 binding")
    elif is_source_only_semantic_exception(args):
        if SHA256_RE.fullmatch(args.old_queue_sha256) is None or not args.protected_targets or SHA256_RE.fullmatch(args.protected_targets_sha256) is None:
            raise ReplaceError("source-only replacement requires ordered-queue and protected-inventory SHA-256 bindings")
    elif not is_pcodx_replacement(args) and args.old_queue_sha256:
        raise ReplaceError("ordered queue binding is accepted only for an exact PCODX, Source-1269, Source-1485, or source-only replacement")
    if not is_source1485_replacement(args) and any(child.queue_sha256 for child in args.children):
        raise ReplaceError("explicit child queue bindings are accepted only for an exact Source-1485 replacement")
    if source1597_direct_worker_required(args):
        if SHA256_RE.fullmatch(args.source1611_parent_sha256) is None:
            raise ReplaceError("Source-1611 direct-worker replacement requires the exact active parent SHA-256 binding")
    elif args.source1611_parent_sha256:
        raise ReplaceError("Source-1611 parent binding is accepted only for the exact direct-worker replacement")
    rollout_values = (
        args.source1611_rollout,
        args.source1611_session_root,
        args.source1611_rollout_device,
        args.source1611_rollout_inode,
        args.source1611_rollout_holder_pid,
        args.source1611_rollout_holder_start_ticks,
        args.source1611_rollout_fd >= 0,
        args.source1611_rollout_session_meta_sha256,
        args.source1611_rollout_holder_exe,
        args.source1611_rollout_holder_exe_link,
        args.source1611_rollout_holder_exe_device,
        args.source1611_rollout_holder_exe_inode,
        args.source1611_rollout_holder_exe_mode,
        args.source1611_rollout_holder_exe_uid >= 0,
        args.source1611_rollout_holder_exe_nlink >= 0,
        args.source1611_rollout_holder_exe_size,
        args.source1611_rollout_holder_exe_sha256,
        args.source1611_rollout_holder_argv_sha256,
    )
    if source1611_rollout_custody_requested(args):
        if (
            not is_source1611_direct_worker_semantic_exception(args)
            or args.source1611_session_root is None
            or args.source1611_rollout_holder_exe is None
            or not args.source1611_rollout_holder_exe_link.startswith("/")
            or min(
                args.source1611_rollout_device,
                args.source1611_rollout_inode,
                args.source1611_rollout_holder_pid,
                args.source1611_rollout_holder_start_ticks,
                args.source1611_rollout_holder_exe_device,
                args.source1611_rollout_holder_exe_inode,
                args.source1611_rollout_holder_exe_mode,
                args.source1611_rollout_holder_exe_size,
            )
            <= 1
            or args.source1611_rollout_holder_exe_uid < 0
            or args.source1611_rollout_holder_exe_nlink < 0
            or args.source1611_rollout_fd < 0
            or SHA256_RE.fullmatch(args.source1611_rollout_session_meta_sha256) is None
            or SHA256_RE.fullmatch(args.source1611_rollout_holder_exe_sha256) is None
            or SHA256_RE.fullmatch(args.source1611_rollout_holder_argv_sha256) is None
        ):
            raise ReplaceError("process-held rollout custody is accepted only for the exact Source-1611 direct-worker transition")
    elif any(bool(value) for value in rollout_values):
        raise ReplaceError("incomplete Source-1611 rollout custody binding")
    if not uses_protected_inventory(args) and args.protected_targets_sha256:
        raise ReplaceError("protected inventory digest is accepted only for PCODX, exact Source-1485, or an exact source-only replacement")
    if not (is_pcodx_replacement(args) or is_source1485_replacement(args)) and args.authority_envelope_file_sha256:
        raise ReplaceError("authority-envelope file digest is accepted only for PCODX or exact Source-1485 replacement")
    if args.closed_owner_audit is not None:
        if not is_guest1269_replacement(args):
            raise ReplaceError("closed-owner audit recovery is accepted only for the exact Source-1269 replacement")
        if args.closed_owner_audit == args.audit_output:
            raise ReplaceError("closed-owner evidence and the fresh replacement audit must use distinct paths")
    if is_source1289_whole_tree(args) and not args.descendants:
        if not is_source1292_empty_tree(args):
            raise ReplaceError("Source-1289 replacement requires descendants or exact Source-1292 empty-tree authority")
    if args.descendants and not is_source1289_whole_tree(args):
        raise ReplaceError("whole-tree descendant closure is authorized only by exact Source-1289")
    if is_source1292_empty_tree(args) and (
        not is_source1289_whole_tree(args) or args.children or args.descendants or SHA256_RE.fullmatch(args.empty_tree_envelope_sha256) is None or args.descendant_authority_envelope_sha256
    ):
        raise ReplaceError("Source-1292 empty-tree mode requires exact Source-1289 with no child or descendant pins")
    if is_source1292_descendant_tree(args) and (
        not is_source1289_whole_tree(args) or not args.descendants or bool(args.empty_tree_envelope_sha256) or SHA256_RE.fullmatch(args.descendant_authority_envelope_sha256) is None
    ):
        raise ReplaceError("Source-1292 descendant mode requires exact Source-1289 with nonempty descendant pins")
    if any(child.task == args.authority_envelope_task for child in args.children) and not is_source1292_descendant_tree(args):
        raise ReplaceError("authority-envelope child alias is restricted to exact Source-1292 descendant mode")
    if args.authority_envelope_task == args.old_task and not is_source_only_semantic_exception(args):
        raise ReplaceError("authority-envelope old-task alias is restricted to an exact source-only mode")
    if is_source_only_semantic_exception(args) and len(Path(args.successor_task).name) >= 25:
        raise ReplaceError("source-only successor task filename must be shorter than 25 characters")
    if is_source1289_whole_tree(args) and (
        args.authority_sha256 != SOURCE1289_SHA256 or args.authority_lines != LineRange(*SOURCE1289_CARRIER_LINES) or args.successor_item_lines != (LineRange(*SOURCE1289_TREE_LINES),)
    ):
        raise ReplaceError("Source-1289 whole-tree mode requires its exact authenticated tree-replacement directive")
    if args.authority_file == SOURCE1485_FILE and not is_source1485_semantic_exception(args):
        raise ReplaceError("Source-1485 authority is restricted to its three exact manager transitions")
    if args.authority_file == SOURCE1597_FILE and not is_source1597_semantic_exception(args):
        raise ReplaceError("Source-1597 authority is restricted to its exact manager transition")
    if args.authority_file == SOURCE1611_FILE and not is_source1611_semantic_exception(args):
        raise ReplaceError("Source-1611 authority is restricted to its exact manager transition")
    old = canonical_target(args.old_target)
    new = canonical_target(args.new_target)
    if old == new:
        raise ReplaceError("old and new manager targets must differ")
    # 🧑 "Treat tmux sessions whose names begin with `h` as human-owned. Change one only when authoritative human text explicitly requests that exact action and session."
    pcodx_human = is_pcodx_replacement(args) and target_session(old).startswith("h")
    if target_session(new).startswith("h") or (target_session(old).startswith("h") and not pcodx_human):
        raise ReplaceError("only an exactly bound Human-authorized PCODX old manager may use a human-owned h* target")
    protected = {canonical_target(target) for target in args.protected_targets}
    if new in protected or (old in protected and not pcodx_human):
        raise ReplaceError("old or new target aliases an explicitly protected pane")
    if pcodx_human and old not in protected:
        raise ReplaceError("Human-owned PCODX old target must be included in the exact protected target set")
    if is_pcodx_replacement(args) != pcodx_human:
        raise ReplaceError("PCODX replacement bindings are accepted only for one protected human-owned old target")
    descendant_targets = tuple(canonical_target(item.target) for item in args.descendants)
    if len(set(descendant_targets)) != len(descendant_targets):
        raise ReplaceError("whole-tree descendant targets must be unique")
    if any(target_session(target).startswith("h") for target in descendant_targets):
        raise ReplaceError("whole-tree replacement cannot close a human-owned descendant target")
    if {old, new, canonical_target(args.parent_target)} & set(descendant_targets) or protected & set(descendant_targets):
        raise ReplaceError("whole-tree descendant target aliases an old, successor, parent, or protected target")
    if is_source1485_replacement(args):
        tasks = {child.task for child in args.children}
        if args.old_task == SOURCE1485_ROOT_TASK and (
            not SOURCE1485_ROOT_REQUIRED_CHILDREN.issubset(tasks) or not tasks.issubset(SOURCE1485_ROOT_REQUIRED_CHILDREN | SOURCE1485_ROOT_OPTIONAL_CHILDREN)
        ):
            raise ReplaceError("Source-1485 root replacement requires the exact retained root-child task set")


def markdown_paths(root: Path) -> tuple[Path, ...]:
    try:
        discovered = tuple(root.rglob("*.md"))
    except OSError as exc:
        raise ReplaceError(f"cannot enumerate work-log task records: {exc}") from exc
    if any(path.is_symlink() for path in discovered):
        raise ReplaceError("work-log Markdown inventory contains an unsafe path")
    paths = tuple(sorted((path.resolve(strict=False) for path in discovered), key=str))
    if any(path == root or root not in path.parents for path in paths) or len(set(paths)) != len(paths):
        raise ReplaceError("work-log Markdown inventory contains an unsafe or duplicate path")
    return paths


def active_graph_row(root: Path, task: str, data: bytes) -> ActiveGraphRow:
    value = metadata(data, root, f"Source-1485 simulated task {task}")
    if value.status == "done":
        raise ReplaceError(f"Source-1485 simulated active graph contains a done task: {task}")
    return ActiveGraphRow(
        task=task,
        sha256=digest(data),
        status=value.status,
        runat=canonical_target(value.runat),
        managerat=canonical_target(value.managerat),
        tool=value.tool,
        is_manager=value.is_manager,
        session_id=value.session_id.lower(),
        queue_sha256=json_digest(list(value.pending_task_items)),
    )


def active_manager_owner_row(root: Path, target: str) -> ActiveGraphRow | None:
    """Return the unique active manager task owning ``target``, if one exists."""

    canonical = canonical_target(target)
    owners: list[ActiveGraphRow] = []
    for path in authoritative_active_target_task_paths(root, canonical):
        task = path.relative_to(root).as_posix()
        row = active_graph_row(
            root,
            task,
            read_snapshot(path, f"Source-1485 reporting-parent owner {task}").data,
        )
        if row.is_manager:
            owners.append(row)
    if len(owners) > 1:
        raise ReplaceError(f"Source-1485 post-graph target {canonical} has multiple active manager owners")
    return owners[0] if owners else None


def source1485_topology_binding(args: Args, plan: Plan) -> dict[str, object]:
    """Build and validate the exact affected post-replacement ownership tree."""

    if not is_source1485_replacement(args):
        raise ReplaceError("Source-1485 topology binding is unavailable outside its exact replacement program")
    direct_data = {pin.task: after for pin, after in zip(args.children, plan.child_after, strict=True)}
    rows: list[ActiveGraphRow] = [active_graph_row(args.root, args.successor_task, plan.successor_data)]
    seen_tasks = {args.successor_task}
    seen_targets = {rows[0].runat}

    def visit(task: str, data: bytes, expected_manager: str) -> None:
        if task in seen_tasks:
            raise ReplaceError(f"Source-1485 simulated post-graph repeats task {task}")
        row = active_graph_row(args.root, task, data)
        if row.managerat != expected_manager:
            raise ReplaceError(f"Source-1485 simulated post-graph ownership changed for {task}")
        if row.runat in seen_targets:
            raise ReplaceError(f"Source-1485 simulated post-graph repeats active target {row.runat}")
        if target_session(row.runat).startswith("h"):
            raise ReplaceError("Source-1485 replacement cannot mutate ownership of a human-owned child target")
        seen_tasks.add(task)
        seen_targets.add(row.runat)
        rows.append(row)
        if not row.is_manager:
            return
        path = task_path(args.root, task)
        for child_task in active_child_task_refs(args.root, path, row.runat):
            visit(
                child_task,
                read_snapshot(task_path(args.root, child_task), f"Source-1485 nested child {child_task}").data,
                row.runat,
            )

    for pin in args.children:
        visit(pin.task, direct_data[pin.task], canonical_target(args.new_target))
    if canonical_target(args.parent_target) in seen_targets:
        raise ReplaceError("Source-1485 simulated post-graph would retain or create a reporting cycle")
    root = rows[0]
    if root.managerat != canonical_target(args.parent_target):
        raise ReplaceError("Source-1485 successor reporting parent changed during construction")
    inventory = pane_inventory()
    protected = {canonical_target(target) for target in args.protected_targets}
    required_live = {row.runat for row in rows[1:] if row.is_manager or row.status in {"running", "long_running"} or row.runat in inventory}
    if canonical_target(args.parent_target) not in inventory or canonical_target(args.parent_target) not in protected:
        raise ReplaceError("Source-1485 simulated graph requires its live reporting parent in the protected set")
    if any(target not in inventory for target in required_live) or not required_live.issubset(protected):
        raise ReplaceError("Source-1485 simulated graph requires every retained live manager or worker in the protected set")
    if args.old_task == SOURCE1485_ROOT_TASK:
        umbrella = next((row for row in rows if row.task == SOURCE1485_UMBRELLA_TASK), None)
        if (
            umbrella is None
            or umbrella.runat != SOURCE1485_UMBRELLA_TARGET
            or not umbrella.is_manager
            or umbrella.queue_sha256 != json_digest([])
            or active_child_task_refs(
                args.root,
                task_path(args.root, SOURCE1485_UMBRELLA_TASK),
                SOURCE1485_UMBRELLA_TARGET,
            )
        ):
            raise ReplaceError("Source-1485 root replacement requires the queue-empty, childless wl:7 umbrella retained for later supported retirement")
    ancestor_rows: list[ActiveGraphRow] = []
    parent_target = canonical_target(args.parent_target)
    current_target = parent_target
    visited_ancestor_targets: set[str] = set()
    while True:
        if current_target in seen_targets or current_target in visited_ancestor_targets:
            raise ReplaceError("Source-1485 post-graph reporting ancestry would retain or create a cycle")
        parent_row = active_manager_owner_row(args.root, current_target)
        if parent_row is None:
            if current_target == parent_target:
                raise ReplaceError("Source-1485 post-graph requires exactly one active reporting-parent manager owner")
            break
        if parent_row.runat != current_target:
            raise ReplaceError("Source-1485 post-graph reporting-parent identity changed")
        if parent_row.task in seen_tasks:
            raise ReplaceError("Source-1485 post-graph reporting ancestry repeats a retained task")
        if current_target not in inventory or current_target not in protected:
            raise ReplaceError("Source-1485 post-graph requires every live reporting ancestor in the protected set")
        visited_ancestor_targets.add(current_target)
        ancestor_rows.append(parent_row)
        if args.old_task != SOURCE1485_ROOT_TASK:
            if parent_row.managerat in seen_targets:
                raise ReplaceError("Source-1485 post-graph reporting edge would retain or create a cycle")
            break
        current_target = parent_row.managerat
    serialized_rows = [
        {
            "task": row.task,
            "sha256": row.sha256,
            "status": row.status,
            "runat": row.runat,
            "managerat": row.managerat,
            "tool": row.tool,
            "is_manager": row.is_manager,
            "session_id": row.session_id,
            "queue_sha256": row.queue_sha256,
        }
        for row in rows
    ]
    serialized_ancestors = [
        {
            "task": row.task,
            "sha256": row.sha256,
            "status": row.status,
            "runat": row.runat,
            "managerat": row.managerat,
            "tool": row.tool,
            "is_manager": row.is_manager,
            "session_id": row.session_id,
            "queue_sha256": row.queue_sha256,
        }
        for row in ancestor_rows
    ]
    return {
        "root_task": args.successor_task,
        "root_target": canonical_target(args.new_target),
        "parent_target": canonical_target(args.parent_target),
        "acyclic": True,
        "acyclic_scope": ("replacement-subtree-plus-complete-active-manager-parent-ancestry" if args.old_task == SOURCE1485_ROOT_TASK else "replacement-subtree-plus-immediate-parent-owner"),
        "rows": serialized_rows,
        "rows_sha256": json_digest(serialized_rows),
        "ancestor_rows": serialized_ancestors,
        "ancestor_rows_sha256": json_digest(serialized_ancestors),
    }


def prepare(args: Args, paths: tuple[Path, ...]) -> Plan:
    old_path = task_path(args.root, args.old_task)
    successor_path = task_path(args.root, args.successor_task)
    todo_path = args.root / "TODO.md"
    authority_path = task_path(args.root, args.authority_file)
    authority_envelope_path = task_path(args.root, args.authority_envelope_task)
    if successor_path.exists() or successor_path.is_symlink():
        raise ReplaceError("successor task already exists; launch-before-proof is rejected")
    path_set = set(paths)
    if old_path not in path_set or todo_path not in path_set or authority_envelope_path not in path_set:
        raise ReplaceError("old manager, TODO, or authority envelope is absent from the locked Markdown inventory")
    old = read_snapshot(old_path, "old manager task")
    todo = read_snapshot(todo_path, "TODO")
    authority = read_snapshot(authority_path, "replacement authority")
    authority_envelope = read_snapshot(authority_envelope_path, "replacement authority envelope")
    source1601_authority = source1601_material(args)
    source1597_authority = source1597_direct_worker_material(args)
    source1611_parent = source1611_direct_worker_parent_material(args, active_child=args.old_task)
    if digest(old.data) != args.old_sha256 or digest(todo.data) != args.todo_sha256:
        raise ReplaceError("old manager or TODO digest changed")
    if is_source1485_replacement(args) and args.old_task == SOURCE1485_ROOT_TASK:
        require_source1485_umbrella_previous(args.root, todo.data)
    old_metadata = metadata(old.data, args.root, "old manager task")
    if (
        old_metadata.version != TASK_FRONTMATTER_V1
        or old_metadata.status != "long_running"
        or old_metadata.runat != args.old_target
        or old_metadata.managerat != args.parent_target
        or old_metadata.tool != ("pcodx" if is_pcodx_replacement(args) else "codex")
        or not old_metadata.is_manager
        or not old_session_matches(args, old_metadata.session_id, old.data)
    ):
        raise ReplaceError("old manager must be the exact live long-running failed-manager record bound by the invocation")
    if uses_ordered_queue_binding(args) and json_digest(list(old_metadata.pending_task_items)) != args.old_queue_sha256:
        raise ReplaceError("old manager full ordered queue changed")
    if is_source_only_semantic_exception(args) and old_metadata.pending_task_items != source_only_expected_old_queue(args):
        raise ReplaceError("source-only replacement exact Human-provenance queue changed")
    old_owners = authoritative_active_target_task_paths(args.root, args.old_target)
    if old_owners != (old_path.resolve(),):
        raise ReplaceError("old target does not have exactly one authoritative active owner")
    if authoritative_active_target_task_paths(args.root, args.new_target):
        raise ReplaceError("new target already has an authoritative active owner; launch-before-proof is rejected")
    expected_children = tuple(sorted(child.task for child in args.children))
    actual_children = active_child_task_refs(args.root, old_path, args.old_target)
    if actual_children != expected_children:
        raise ReplaceError(f"active child set changed: expected {expected_children}, found {actual_children}")
    if is_pcodx_replacement(args) and len(expected_children) != 4:
        raise ReplaceError("Human-owned PCODX manager replacement requires the exact four-child Source-1228 set")
    pins = {child.task: child.sha256 for child in args.children}
    children: list[Snapshot] = []
    child_after: list[bytes] = []
    child_queues: list[tuple[str, ...]] = []
    child_metadata_values: list[TaskMetadata] = []
    for task in expected_children:
        path = task_path(args.root, task)
        if path not in path_set:
            raise ReplaceError(f"active child disappeared: {task}")
        snapshot = read_snapshot(path, f"active child {task}")
        if digest(snapshot.data) != pins[task]:
            raise ReplaceError(f"active child digest changed: {task}")
        child_metadata = metadata(snapshot.data, args.root, f"active child {task}")
        if child_metadata.status == "done" or child_metadata.managerat != args.old_target:
            raise ReplaceError(f"active child ownership changed: {task}")
        if pin := next((candidate for candidate in args.children if candidate.task == task), None):
            if pin.queue_sha256 and json_digest(list(child_metadata.pending_task_items)) != pin.queue_sha256:
                raise ReplaceError(f"active child ordered queue changed: {task}")
        if args.descendants:
            runtime = next(item for item in args.descendants if item.task == task)
            if canonical_target(child_metadata.runat) != canonical_target(runtime.target):
                raise ReplaceError(f"active descendant run target changed: {task}")
            if child_metadata.session_id.lower() != runtime.session_id:
                raise ReplaceError(f"active descendant session changed: {task}")
            if json_digest(list(child_metadata.pending_task_items)) != runtime.queue_sha256:
                raise ReplaceError(f"active descendant queue changed: {task}")
            nested = active_child_task_refs(args.root, path, child_metadata.runat)
            if nested:
                raise ReplaceError(f"whole-tree replacement does not omit nested active descendants under {task}: {nested}")
        updated = migrate_manager_owner(snapshot.data, args.old_target, args.new_target, args.root)
        children.append(snapshot)
        child_after.append(updated)
        child_queues.append(child_metadata.pending_task_items)
        child_metadata_values.append(child_metadata)
    authority_items = authority_material(args, authority, authority_envelope)
    if is_source_only_semantic_exception(args):
        authority_items = (*authority_items, *source_only_added_goals(args, old_metadata.pending_task_items))
    empty_tree_authority: Snapshot | None = None
    if is_source1292_empty_tree(args) or is_source1292_descendant_tree(args):
        empty_tree_authority, empty_tree_item = empty_tree_authority_material(args, authority_envelope)
        authority_items = (*authority_items, empty_tree_item)
    old_after, successor_data, successor_queue = old_and_successor_text(
        old.data,
        args.root,
        args.old_target,
        args.new_target,
        authority_items,
        human_goals_only=is_source_only_semantic_exception(args),
    )
    todo_after = todo_replacement(todo.data, args.root, old_path, successor_path, args.old_target, args.new_target)
    protected_identities: tuple[PaneIdentity, ...] = ()
    if uses_protected_inventory(args):
        inventory = pane_inventory()
        validate_live_bindings(args, inventory)
        validate_source1485_protected_set(args, inventory, tuple(child_metadata_values))
        protected_identities = protected_inventory(args, inventory)
    descendant_identities = tuple(PaneIdentity(canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks) for item in args.descendants)
    plan = Plan(
        old,
        todo,
        authority,
        authority_envelope,
        tuple(children),
        successor_path,
        old_after,
        tuple(child_after),
        todo_after,
        successor_data,
        successor_queue,
        tuple(child_queues),
        paths,
        protected_identities,
        descendant_identities,
        empty_tree_authority,
    )
    if source1601_authority is not None:
        plan = replace(plan, source1601_authority=source1601_authority)
    if source1597_authority is not None:
        plan = replace(plan, source1597_authority=source1597_authority)
    if source1611_parent is not None:
        plan = replace(plan, source1611_parent=source1611_parent)
    if is_source1485_replacement(args):
        topology = source1485_topology_binding(args, plan)
        plan = replace(plan, source1485_topology=topology)
    return plan


def authority_envelope_child_index(args: Args) -> int | None:
    """Return the migrated child index when the authority carrier is that child."""

    return next(
        (index for index, child in enumerate(args.children) if child.task == args.authority_envelope_task),
        None,
    )


def authenticate_committed_authority_envelope(
    args: Args,
    plan: Plan,
    child_after: tuple[Snapshot, ...],
) -> None:
    """Authenticate unchanged authority blocks in a migrated child carrier."""

    if is_source_only_semantic_exception(args):
        _ = authority_material(args, plan.authority, plan.old)
        return

    index = authority_envelope_child_index(args)
    if index is None:
        require_snapshot(plan.authority_envelope, "replacement authority envelope")
        return
    envelope = child_after[index]
    _ = authority_material(args, plan.authority, envelope)
    if is_source1292_empty_tree(args) or is_source1292_descendant_tree(args):
        _ = empty_tree_authority_material(args, envelope)


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def child_binding(args: Args) -> list[dict[str, str]]:
    return [
        {
            "task": child.task,
            "sha256": child.sha256,
            **({"queue_sha256": child.queue_sha256} if child.queue_sha256 else {}),
        }
        for child in args.children
    ]


def audit_record(args: Args, plan: Plan, secret: str, commitment: str) -> dict[str, object]:
    files = [
        {
            "task": args.old_task,
            "before": encoded(plan.old.data),
            "after": encoded(plan.old_after),
            "mode": stat.S_IMODE(plan.old.state.st_mode),
            "gid": plan.old.state.st_gid,
        },
        *(
            {
                "task": pin.task,
                "before": encoded(snapshot.data),
                "after": encoded(after),
                "mode": stat.S_IMODE(snapshot.state.st_mode),
                "gid": snapshot.state.st_gid,
            }
            for pin, snapshot, after in zip(args.children, plan.children, plan.child_after, strict=True)
        ),
        {
            "task": "TODO.md",
            "before": encoded(plan.todo.data),
            "after": encoded(plan.todo_after),
            "mode": stat.S_IMODE(plan.todo.state.st_mode),
            "gid": plan.todo.state.st_gid,
        },
        {
            "task": args.successor_task,
            "before": None,
            "after": encoded(plan.successor_data),
            "mode": stat.S_IMODE(plan.old.state.st_mode),
            "gid": plan.old.state.st_gid,
        },
    ]
    record: dict[str, object] = {
        "version": AUDIT_VERSION,
        "operation": AUDIT_OPERATION,
        "state": "prepared",
        "root": str(args.root),
        "old_task": args.old_task,
        "successor_task": args.successor_task,
        "old_target": args.old_target,
        "new_target": args.new_target,
        "parent_target": args.parent_target,
        "old_sha256": args.old_sha256,
        "todo_sha256": args.todo_sha256,
        "children": child_binding(args),
        "old_pane": {"id": args.old_pane_id, "pid": args.old_pane_pid, "start_ticks": args.old_pane_start_ticks},
        "old_session_id": args.old_session_id,
        "authority_file": args.authority_file,
        "authority_lines": [args.authority_lines.start, args.authority_lines.end],
        "authority_sha256": args.authority_sha256,
        "authority_envelope_task": args.authority_envelope_task,
        "authority_envelope_sha256": args.authority_envelope_sha256,
        "successor_item_lines": [[item.start, item.end] for item in args.successor_item_lines],
        "protected_targets": list(args.protected_targets),
        "preparer": args.preparer,
        "reviewer": args.reviewer,
        "close_proof_secret": secret,
        "close_proof_commitment": commitment,
        "completed_writes": [],
        "markdown_membership": [path.relative_to(args.root).as_posix() for path in plan.initial_markdown_paths],
        "files": files,
    }
    if args.descendants:
        record["descendants"] = descendant_binding(args)
        record["descendant_close_commitments"] = [descendant_commitment(secret, child) for child in args.descendants]
    if is_source1292_empty_tree(args):
        record["empty_tree_envelope_sha256"] = args.empty_tree_envelope_sha256
    if is_source1292_descendant_tree(args):
        record["descendant_authority_envelope_sha256"] = args.descendant_authority_envelope_sha256
    if is_source1485_replacement(args):
        if plan.source1485_topology is None:
            raise ReplaceError("Source-1485 audit lost its simulated post-topology")
        record["old_queue_sha256"] = args.old_queue_sha256
        record["protected_targets_sha256"] = args.protected_targets_sha256
        record["authority_envelope_file_sha256"] = args.authority_envelope_file_sha256
        record["source1485_topology"] = plan.source1485_topology
        record["source1485_topology_sha256"] = json_digest(plan.source1485_topology)
    if is_source_only_semantic_exception(args):
        record.update(source_only_audit_binding(args))
    if source1611_rollout_custody_requested(args):
        record.update(source1611_rollout_audit_binding(args))
    if args.closed_owner_audit is not None:
        record.update(
            {
                "closed_owner_audit": str(args.closed_owner_audit),
                "closed_owner_audit_sha256": args.closed_owner_audit_sha256,
            }
        )
    if is_pcodx_replacement(args):
        record.update(pcodx_audit_binding(args))
    if uses_protected_inventory(args):
        record["protected_inventory"] = [
            {
                "target": identity.target,
                "pane_id": identity.pane_id,
                "pid": identity.pid,
                "start_ticks": identity.start_ticks,
            }
            for identity in plan.protected_identities
        ]
    return record


def serialized_audit(record: dict[str, object]) -> bytes:
    value = dict(record)
    value.pop("record_sha256", None)
    value["record_sha256"] = digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_AUDIT_BYTES:
        raise ReplaceError("private replacement audit exceeds the size bound")
    return data


def reserve_audit(path: Path, record: dict[str, object]) -> bytes:
    try:
        parent = path.parent.stat()
    except OSError as exc:
        raise ReplaceError(f"audit directory is unavailable: {exc}") from exc
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ReplaceError("audit directory must be owner-private")
    data = serialized_audit(record)
    try:
        published = create_snapshot(path, data, 0o600)
    except (OSError, ReplaceError) as exc:
        raise ReplaceError(f"cannot reserve private replacement audit: {exc}") from exc
    if published.data != data or stat.S_IMODE(published.state.st_mode) != 0o600:
        raise ReplaceError("private replacement audit publication could not be proved")
    return data


def close_authority_record(args: Args, audit_bytes: bytes, commitment: str) -> dict[str, object]:
    """Return the bounded capability record consumed inside the guarded tmux close."""

    return {
        "version": AUDIT_VERSION,
        "operation": AUDIT_OPERATION,
        "state": "prepared",
        "audit_path": str(args.audit_output),
        "replacement_audit_sha256": digest(audit_bytes),
        "old_target": args.old_target,
        "old_pane_id": args.old_pane_id,
        "old_pane_pid": args.old_pane_pid,
        "old_pane_start_ticks": args.old_pane_start_ticks,
        "close_proof_commitment": commitment,
    }


def descendant_secret(secret: str, child: DescendantPin) -> str:
    return digest(f"{secret}\0{child.task}\0{child.target}".encode())


def descendant_commitment(secret: str, child: DescendantPin) -> str:
    return digest(descendant_secret(secret, child).encode())


def descendant_evidence_paths(audit_path: Path, child: DescendantPin) -> tuple[Path, Path]:
    token = digest(child.task.encode())[:16]
    authority = audit_path.with_name(f".{audit_path.name}.descendant-{token}-close-authority")
    return authority, authority.with_name(f".{authority.name}.owner-stopped")


def descendant_close_authority_record(args: Args, child: DescendantPin, audit_bytes: bytes, commitment: str) -> dict[str, object]:
    return {
        "version": AUDIT_VERSION,
        "operation": AUDIT_OPERATION,
        "state": "prepared",
        "audit_path": str(args.audit_output),
        "replacement_audit_sha256": digest(audit_bytes),
        "old_target": child.target,
        "old_pane_id": child.pane_id,
        "old_pane_pid": child.pane_pid,
        "old_pane_start_ticks": child.pane_start_ticks,
        "close_proof_commitment": commitment,
    }


def transition_audit(
    path: Path, expected: bytes, record: dict[str, object], state: str, *, completed: tuple[str, ...], error: str = "", rollback_failures: tuple[str, ...] = ()
) -> tuple[dict[str, object], bytes]:
    current = read_snapshot(path, "private replacement audit")
    if current.data != expected or stat.S_IMODE(current.state.st_mode) != 0o600:
        raise ReplaceError("private replacement audit changed during transaction")
    updated = dict(record)
    updated["state"] = state
    updated["completed_writes"] = list(completed)
    updated.pop("error", None)
    updated.pop("rollback_failures", None)
    if error:
        updated["error"] = error
    if rollback_failures:
        updated["rollback_failures"] = list(rollback_failures)
    data = serialized_audit(updated)
    result = replace_snapshot(current, data, "private replacement audit")
    return updated, result.data


def decoded(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise ReplaceError(f"private replacement audit {label} is not text")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ReplaceError(f"private replacement audit {label} is not canonical base64") from exc


def pcodx_audit_binding(args: Args) -> dict[str, object]:
    return {
        "old_queue_sha256": args.old_queue_sha256,
        "old_pcodx_state_sha256": args.old_pcodx_state_sha256,
        "old_pcodx_ledger_sha256": args.old_pcodx_ledger_sha256,
        "old_pcodx_wrapper_sha256": args.old_pcodx_wrapper_sha256,
        "protected_targets_sha256": args.protected_targets_sha256,
        "authority_envelope_file_sha256": args.authority_envelope_file_sha256,
    }


def source_only_audit_binding(args: Args) -> dict[str, object]:
    binding: dict[str, object] = {
        "authority_mode": SOURCE_ONLY_AUTHORITY_MODE,
        "old_queue_sha256": args.old_queue_sha256,
        "protected_targets_sha256": args.protected_targets_sha256,
    }
    if source1601_required(args):
        binding.update(
            {
                "source1601_file": SOURCE1601_FILE,
                "source1601_lines": list(SOURCE1601_LINES),
                "source1601_sha256": SOURCE1601_SHA256,
            }
        )
    if source1597_direct_worker_required(args):
        binding.update(
            {
                "source1597_file": SOURCE1597_FILE,
                "source1597_lines": list(SOURCE1597_LINES),
                "source1597_sha256": SOURCE1597_SHA256,
            }
        )
        binding.update(
            {
                "source1611_parent_task": SOURCE1611_SUCCESSOR_TASK,
                "source1611_parent_sha256": args.source1611_parent_sha256,
            }
        )
    return binding


def source1611_rollout_audit_binding(args: Args) -> dict[str, object]:
    if not source1611_rollout_custody_requested(args) or args.source1611_rollout is None or args.source1611_session_root is None:
        return {}
    return {
        "source1611_rollout": str(args.source1611_rollout),
        "source1611_session_root": str(args.source1611_session_root),
        "source1611_rollout_device": args.source1611_rollout_device,
        "source1611_rollout_inode": args.source1611_rollout_inode,
        "source1611_rollout_holder_pid": args.source1611_rollout_holder_pid,
        "source1611_rollout_holder_start_ticks": args.source1611_rollout_holder_start_ticks,
        "source1611_rollout_fd": args.source1611_rollout_fd,
        "source1611_rollout_session_meta_sha256": args.source1611_rollout_session_meta_sha256,
        "source1611_rollout_holder_exe": str(args.source1611_rollout_holder_exe),
        "source1611_rollout_holder_exe_link": args.source1611_rollout_holder_exe_link,
        "source1611_rollout_holder_exe_device": args.source1611_rollout_holder_exe_device,
        "source1611_rollout_holder_exe_inode": args.source1611_rollout_holder_exe_inode,
        "source1611_rollout_holder_exe_mode": args.source1611_rollout_holder_exe_mode,
        "source1611_rollout_holder_exe_uid": args.source1611_rollout_holder_exe_uid,
        "source1611_rollout_holder_exe_nlink": args.source1611_rollout_holder_exe_nlink,
        "source1611_rollout_holder_exe_size": args.source1611_rollout_holder_exe_size,
        "source1611_rollout_holder_exe_sha256": args.source1611_rollout_holder_exe_sha256,
        "source1611_rollout_holder_argv_sha256": args.source1611_rollout_holder_argv_sha256,
    }


def descendant_binding(args: Args) -> list[dict[str, object]]:
    return [
        {
            "task": child.task,
            "sha256": child.sha256,
            "target": child.target,
            "pane_id": child.pane_id,
            "pane_pid": child.pane_pid,
            "pane_start_ticks": child.pane_start_ticks,
            "session_id": child.session_id,
            "queue_sha256": child.queue_sha256,
        }
        for child in args.descendants
    ]


def audit_binding(args: Args) -> dict[str, object]:
    binding: dict[str, object] = {
        "version": AUDIT_VERSION,
        "operation": AUDIT_OPERATION,
        "root": str(args.root),
        "old_task": args.old_task,
        "successor_task": args.successor_task,
        "old_target": args.old_target,
        "new_target": args.new_target,
        "parent_target": args.parent_target,
        "old_sha256": args.old_sha256,
        "todo_sha256": args.todo_sha256,
        "children": child_binding(args),
        "old_pane": {"id": args.old_pane_id, "pid": args.old_pane_pid, "start_ticks": args.old_pane_start_ticks},
        "old_session_id": args.old_session_id,
        "authority_file": args.authority_file,
        "authority_lines": [args.authority_lines.start, args.authority_lines.end],
        "authority_sha256": args.authority_sha256,
        "authority_envelope_task": args.authority_envelope_task,
        "authority_envelope_sha256": args.authority_envelope_sha256,
        "successor_item_lines": [[item.start, item.end] for item in args.successor_item_lines],
        "protected_targets": list(args.protected_targets),
        "preparer": args.preparer,
        "reviewer": args.reviewer,
    }
    if args.descendants:
        binding["descendants"] = descendant_binding(args)
    if is_source1292_empty_tree(args):
        binding["empty_tree_envelope_sha256"] = args.empty_tree_envelope_sha256
    if is_source1292_descendant_tree(args):
        binding["descendant_authority_envelope_sha256"] = args.descendant_authority_envelope_sha256
    if is_pcodx_replacement(args):
        binding.update(pcodx_audit_binding(args))
    if is_source1485_replacement(args):
        binding.update(
            {
                "old_queue_sha256": args.old_queue_sha256,
                "protected_targets_sha256": args.protected_targets_sha256,
                "authority_envelope_file_sha256": args.authority_envelope_file_sha256,
            }
        )
    if is_source_only_semantic_exception(args):
        binding.update(source_only_audit_binding(args))
    if source1611_rollout_custody_requested(args):
        binding.update(source1611_rollout_audit_binding(args))
    if args.closed_owner_audit is not None:
        binding.update(
            {
                "closed_owner_audit": str(args.closed_owner_audit),
                "closed_owner_audit_sha256": args.closed_owner_audit_sha256,
            }
        )
    return binding


def read_audit(args: Args) -> tuple[dict[str, object], bytes, tuple[AuditEntry, ...], tuple[Path, ...]]:
    snapshot = read_snapshot(args.audit_output, "private replacement audit")
    if stat.S_IMODE(snapshot.state.st_mode) != 0o600 or len(snapshot.data) > MAX_AUDIT_BYTES:
        raise ReplaceError("private replacement audit must remain owner-private and bounded")
    try:
        loaded: object = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplaceError(f"private replacement audit is invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ReplaceError("private replacement audit must be one object")
    record = dict(loaded)
    commitment = record.pop("record_sha256", None)
    if commitment != digest(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()):
        raise ReplaceError("private replacement audit integrity commitment changed")
    allowed = {
        *audit_binding(args),
        "state",
        "close_proof_secret",
        "close_proof_commitment",
        "completed_writes",
        "markdown_membership",
        "files",
        "error",
        "rollback_failures",
        "owner_close_evidence",
        "closed_owner_prepared_sha256",
        "closed_owner_authority_sha256",
    }
    if is_pcodx_replacement(args):
        allowed.add("protected_inventory")
    if is_source1485_replacement(args):
        allowed.update({"protected_inventory", "source1485_topology", "source1485_topology_sha256"})
    if is_source_only_semantic_exception(args):
        allowed.add("protected_inventory")
    if args.descendants:
        allowed.add("descendant_close_commitments")
        commitments = record.get("descendant_close_commitments")
        if not isinstance(commitments, list) or commitments != [descendant_commitment(str(record.get("close_proof_secret", "")), child) for child in args.descendants]:
            raise ReplaceError("private replacement audit descendant close capabilities changed")
    required = allowed - {"error", "rollback_failures", "owner_close_evidence"}
    if args.closed_owner_audit is None:
        required -= {"closed_owner_prepared_sha256", "closed_owner_authority_sha256"}
        allowed -= {"closed_owner_prepared_sha256", "closed_owner_authority_sha256"}
    if not required.issubset(record) or not set(record).issubset(allowed):
        raise ReplaceError("private replacement audit fields are incomplete or unrecognized")
    if any(record.get(key) != value for key, value in audit_binding(args).items()):
        raise ReplaceError("private replacement audit is bound to a different invocation")
    state = record.get("state")
    completed = record.get("completed_writes")
    close_commitment = record.get("close_proof_commitment")
    close_secret = record.get("close_proof_secret")
    if not isinstance(state, str) or not isinstance(completed, list) or not all(isinstance(item, str) for item in completed):
        raise ReplaceError("private replacement audit state is malformed")
    if state not in {"prepared", "owner_stopped", "owner_absent", "mutating", "proving", "committed", "stop_failed", "rolled_back", "rollback_failed"}:
        raise ReplaceError("private replacement audit lifecycle state is unrecognized")
    if record.get("owner_close_evidence") not in {None, "authorized-absence"}:
        raise ReplaceError("private replacement audit owner-close evidence is unrecognized")
    if record.get("owner_close_evidence") == "authorized-absence" and state in {"prepared", "owner_stopped", "stop_failed"}:
        raise ReplaceError("private replacement audit owner-close evidence contradicts its lifecycle state")
    if args.closed_owner_audit is not None and any(SHA256_RE.fullmatch(str(record.get(key, ""))) is None for key in ("closed_owner_prepared_sha256", "closed_owner_authority_sha256")):
        raise ReplaceError("private replacement audit closed-owner evidence binding is malformed")
    if not isinstance(close_commitment, str) or SHA256_RE.fullmatch(close_commitment) is None:
        raise ReplaceError("private replacement audit close commitment is malformed")
    if not isinstance(close_secret, str) or SHA256_RE.fullmatch(close_secret) is None or digest(close_secret.encode()) != close_commitment:
        raise ReplaceError("private replacement audit close capability is malformed")
    membership_value = record.get("markdown_membership")
    if not isinstance(membership_value, list) or not membership_value or not all(isinstance(item, str) for item in membership_value):
        raise ReplaceError("private replacement audit Markdown membership is malformed")
    membership = tuple(task_path(args.root, item) for item in membership_value)
    if len(set(membership)) != len(membership) or tuple(sorted(membership, key=str)) != membership:
        raise ReplaceError("private replacement audit Markdown membership is not canonical")
    file_values = record.get("files")
    if not isinstance(file_values, list):
        raise ReplaceError("private replacement audit file images are malformed")
    expected_tasks = (args.old_task, *(child.task for child in args.children), "TODO.md", args.successor_task)
    entries: list[AuditEntry] = []
    for index, value in enumerate(file_values):
        if not isinstance(value, dict) or set(value) != {"task", "before", "after", "mode", "gid"}:
            raise ReplaceError("private replacement audit file entry is malformed")
        task = value.get("task")
        mode = value.get("mode")
        gid = value.get("gid")
        if not isinstance(task, str) or index >= len(expected_tasks) or task != expected_tasks[index]:
            raise ReplaceError("private replacement audit file order changed")
        if not isinstance(mode, int) or mode < 0 or mode & ~0o7777 or not isinstance(gid, int) or gid < 0:
            raise ReplaceError("private replacement audit file ownership metadata is malformed")
        before_value = value.get("before")
        before = None if before_value is None else decoded(before_value, f"{task} before")
        after = decoded(value.get("after"), f"{task} after")
        if (task == args.successor_task) != (before is None):
            raise ReplaceError("private replacement audit successor image is malformed")
        entries.append(AuditEntry(task, before, after, mode, gid))
    if len(entries) != len(expected_tasks):
        raise ReplaceError("private replacement audit file set changed")
    if digest(entries[0].before or b"") != args.old_sha256 or digest(entries[-2].before or b"") != args.todo_sha256:
        raise ReplaceError("private replacement audit before-image digest changed")
    for pin, entry in zip(args.children, entries[1:-2], strict=True):
        if digest(entry.before or b"") != pin.sha256:
            raise ReplaceError(f"private replacement audit child before-image changed: {pin.task}")
    if uses_protected_inventory(args):
        protected_value = record.get("protected_inventory")
        if not isinstance(protected_value, list) or json_digest(protected_value) != args.protected_targets_sha256:
            raise ReplaceError("private replacement audit protected inventory binding changed")
    if is_source1485_replacement(args):
        topology = record.get("source1485_topology")
        topology_sha256 = record.get("source1485_topology_sha256")
        if not isinstance(topology, dict) or not isinstance(topology_sha256, str) or json_digest(topology) != topology_sha256:
            raise ReplaceError("private replacement audit Source-1485 topology binding changed")
    return dict(loaded), snapshot.data, tuple(entries), membership


def closed_owner_evidence_paths(audit_path: Path) -> tuple[Path, Path]:
    authority = audit_path.with_name(f".{audit_path.name}.close-authority")
    return authority, authority.with_name(f".{authority.name}.owner-stopped")


def validate_closed_owner_absence(args: Args) -> None:
    inventory = pane_inventory()
    if (
        canonical_target(args.old_target) in inventory
        or canonical_target(args.new_target) in inventory
        or any(identity.pane_id == args.old_pane_id for identity in inventory.values())
        or process_start_ticks(args.old_pane_pid) is not None
    ):
        raise ReplaceError("closed-owner source pane or process identity is no longer absent")


# 🧑 "Bind the exact failed manager, current TODO ... pane/process/session identity ... and protected targets."
def authenticate_closed_owner_source(
    args: Args,
) -> tuple[str, str, Args, tuple[AuditEntry, ...]]:
    source_path = args.closed_owner_audit
    if source_path is None:
        raise ReplaceError("closed-owner recovery source is absent")
    source_snapshot = read_snapshot(source_path, "closed-owner replacement audit")
    if digest(source_snapshot.data) != args.closed_owner_audit_sha256:
        raise ReplaceError("closed-owner replacement audit digest changed")
    try:
        loaded: object = json.loads(source_snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplaceError(f"closed-owner replacement audit is invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict) or SHA256_RE.fullmatch(str(loaded.get("todo_sha256", ""))) is None:
        raise ReplaceError("closed-owner replacement audit lacks its original TODO binding")
    source_preparer = loaded.get("preparer")
    source_reviewer = loaded.get("reviewer")
    if not isinstance(source_preparer, str) or not source_preparer.strip() or not isinstance(source_reviewer, str) or not source_reviewer.strip() or source_preparer.strip() == source_reviewer.strip():
        raise ReplaceError("closed-owner replacement audit has invalid review identities")
    source_args = replace(
        args,
        todo_sha256=str(loaded["todo_sha256"]),
        audit_output=source_path,
        preparer=source_preparer,
        reviewer=source_reviewer,
        closed_owner_audit=None,
        closed_owner_audit_sha256="",
    )
    record, audit_bytes, entries, _membership = read_audit(source_args)
    if audit_bytes != source_snapshot.data:
        raise ReplaceError("closed-owner replacement audit changed during authentication")
    if (
        record.get("state") != "stop_failed"
        or record.get("error") != "tmux symbolic target no longer owns the exact pane at command execution"
        or record.get("completed_writes") != []
        or record.get("owner_close_evidence") is not None
    ):
        raise ReplaceError("closed-owner source is not the exact proofless Source-1269 stop failure")
    commitment = record.get("close_proof_commitment")
    if not isinstance(commitment, str):
        raise ReplaceError("closed-owner source close commitment is malformed")
    authority_path, proof_path = closed_owner_evidence_paths(source_path)
    if has_bound_close_proof(proof_path, commitment):
        raise ReplaceError("closed-owner source has a bound close proof and is not the proofless Source-1269 incident")
    prepared = dict(record)
    prepared["state"] = "prepared"
    prepared["completed_writes"] = []
    prepared.pop("error", None)
    prepared.pop("rollback_failures", None)
    prepared_bytes = serialized_audit(prepared)
    authority = read_snapshot(authority_path, "closed-owner close authority")
    if authority.data != serialized_audit(close_authority_record(source_args, prepared_bytes, commitment)):
        raise ReplaceError("closed-owner Human-bound close authority changed")
    return digest(prepared_bytes), digest(authority.data), source_args, entries


def validate_closed_owner_before_state(args: Args, source_args: Args, entries: tuple[AuditEntry, ...]) -> None:
    states = tuple(current_entry_state(source_args, entry)[0] for entry in entries)
    if any(state != "before" for state in (*states[:-2], states[-1])):
        raise ReplaceError("closed-owner source lifecycle bytes changed outside the current TODO")
    validate_closed_owner_absence(args)


def validate_closed_owner_recovery(args: Args, record: dict[str, object]) -> None:
    prepared, authority, _source_args, _entries = authenticate_closed_owner_source(args)
    if (prepared, authority) != (
        record.get("closed_owner_prepared_sha256"),
        record.get("closed_owner_authority_sha256"),
    ):
        raise ReplaceError("closed-owner evidence changed before lifecycle recovery")
    validate_closed_owner_absence(args)


def recovery_plan(
    args: Args,
    entries: tuple[AuditEntry, ...],
    membership: tuple[Path, ...],
    record: dict[str, object],
) -> Plan:
    old_entry = entries[0]
    child_entries = entries[1:-2]
    todo_entry = entries[-2]
    old_path = task_path(args.root, args.old_task)
    todo_path = args.root / "TODO.md"
    snapshots = [read_snapshot(old_path, "recovery old manager")]
    snapshots.extend(read_snapshot(task_path(args.root, pin.task), f"recovery child {pin.task}") for pin in args.children)
    snapshots.append(read_snapshot(todo_path, "recovery TODO"))
    if any(stat.S_IMODE(snapshot.state.st_mode) != entry.mode or snapshot.state.st_gid != entry.gid for snapshot, entry in zip(snapshots, entries[:-1], strict=True)):
        raise ReplaceError("recovery found changed lifecycle file mode or group")
    authority = read_snapshot(task_path(args.root, args.authority_file), "recovery replacement authority")
    envelope = read_snapshot(task_path(args.root, args.authority_envelope_task), "recovery authority envelope")
    source1601_authority = source1601_material(args)
    source1597_authority = source1597_direct_worker_material(args)
    source1611_parent = source1611_direct_worker_parent_material(args)
    envelope_child_index = authority_envelope_child_index(args)
    if envelope_child_index is not None:
        envelope_entry = child_entries[envelope_child_index]
        if envelope_entry.before is None:
            raise ReplaceError("private replacement audit lost the authority-envelope child before image")
        envelope = Snapshot(envelope.path, envelope_entry.before, envelope.state)
    if is_source_only_semantic_exception(args):
        if old_entry.before is None:
            raise ReplaceError("private replacement audit lost the source-only old-task authority before image")
        envelope = Snapshot(envelope.path, old_entry.before, envelope.state)
    authority_items = authority_material(args, authority, envelope)
    empty_tree_authority: Snapshot | None = None
    if is_source1292_empty_tree(args) or is_source1292_descendant_tree(args):
        empty_tree_authority, empty_tree_item = empty_tree_authority_material(args, envelope)
        authority_items = (*authority_items, empty_tree_item)
    if old_entry.before is None or todo_entry.before is None or any(entry.before is None for entry in child_entries):
        raise ReplaceError("private replacement audit lost a required before image")
    old_before_metadata = metadata(old_entry.before, args.root, "recovery old manager before image")
    if is_source_only_semantic_exception(args):
        authority_items = (*authority_items, *source_only_added_goals(args, old_before_metadata.pending_task_items))
    old_before = Snapshot(old_path, old_entry.before, snapshots[0].state)
    old_after, successor_after, successor_queue = old_and_successor_text(
        old_entry.before,
        args.root,
        args.old_target,
        args.new_target,
        authority_items,
        human_goals_only=is_source_only_semantic_exception(args),
    )
    child_before: list[Snapshot] = []
    child_after: list[bytes] = []
    child_queues: list[tuple[str, ...]] = []
    for pin, entry, current in zip(args.children, child_entries, snapshots[1:-1], strict=True):
        assert entry.before is not None
        before = Snapshot(task_path(args.root, pin.task), entry.before, current.state)
        before_metadata = metadata(entry.before, args.root, f"recovery child {pin.task}")
        if before_metadata.status == "done" or before_metadata.managerat != args.old_target:
            raise ReplaceError(f"recovery child before image has invalid ownership: {pin.task}")
        if pin.queue_sha256 and json_digest(list(before_metadata.pending_task_items)) != pin.queue_sha256:
            raise ReplaceError(f"recovery child ordered queue changed: {pin.task}")
        try:
            migrated = migrate_manager_owner(entry.before, args.old_target, args.new_target, args.root)
        except ReplaceError as exc:
            raise ReplaceError(f"recovery cannot reconstruct child migration: {pin.task}: {exc}") from exc
        child_before.append(before)
        child_after.append(migrated)
        child_queues.append(before_metadata.pending_task_items)
    successor_path = task_path(args.root, args.successor_task)
    todo_after = todo_replacement(todo_entry.before, args.root, old_path, successor_path, args.old_target, args.new_target)
    canonical_after = (old_after, *child_after, todo_after, successor_after)
    if tuple(entry.after for entry in entries) != canonical_after:
        raise ReplaceError("private replacement audit after images are not the canonical reconstruction")
    old_metadata = metadata(old_entry.before, args.root, "recovery old manager before image")
    if (
        old_metadata.version != TASK_FRONTMATTER_V1
        or old_metadata.status != "long_running"
        or old_metadata.runat != args.old_target
        or old_metadata.managerat != args.parent_target
        or old_metadata.tool != ("pcodx" if is_pcodx_replacement(args) else "codex")
        or not old_metadata.is_manager
        or not old_session_matches(args, old_metadata.session_id, old_entry.before)
    ):
        raise ReplaceError("private replacement audit does not describe the exact failed manager")
    if uses_ordered_queue_binding(args) and json_digest(list(old_metadata.pending_task_items)) != args.old_queue_sha256:
        raise ReplaceError("private replacement audit old manager ordered queue changed")
    if is_source_only_semantic_exception(args) and old_metadata.pending_task_items != source_only_expected_old_queue(args):
        raise ReplaceError("private replacement audit source-only Human-provenance queue changed")
    protected: list[PaneIdentity] = []
    protected_value = record.get("protected_inventory", [])
    if not isinstance(protected_value, list):
        raise ReplaceError("private replacement audit protected pane inventory is malformed")
    for value in protected_value:
        if not isinstance(value, dict) or set(value) != {"target", "pane_id", "pid", "start_ticks"}:
            raise ReplaceError("private replacement audit protected pane identity is malformed")
        try:
            identity = PaneIdentity(value["target"], value["pane_id"], value["pid"], value["start_ticks"])
        except TypeError as exc:
            raise ReplaceError("private replacement audit protected pane identity has invalid types") from exc
        if (
            canonical_target(identity.target) != identity.target
            or PANE_ID_RE.fullmatch(identity.pane_id) is None
            or not isinstance(identity.pid, int)
            or not isinstance(identity.start_ticks, int)
            or identity.pid <= 0
            or identity.start_ticks <= 0
        ):
            raise ReplaceError("private replacement audit protected pane identity is invalid")
        protected.append(identity)
    plan = Plan(
        old_before,
        Snapshot(todo_path, todo_entry.before, snapshots[-1].state),
        authority,
        envelope,
        tuple(child_before),
        successor_path,
        old_after,
        tuple(child_after),
        todo_after,
        successor_after,
        successor_queue,
        tuple(child_queues),
        membership,
        tuple(protected),
        tuple(PaneIdentity(canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks) for item in args.descendants),
        empty_tree_authority,
    )
    if source1601_authority is not None:
        plan = replace(plan, source1601_authority=source1601_authority)
    if source1597_authority is not None:
        plan = replace(plan, source1597_authority=source1597_authority)
    if source1611_parent is not None:
        plan = replace(plan, source1611_parent=source1611_parent)
    if is_source1485_replacement(args):
        topology = source1485_topology_binding(args, plan)
        if topology != record.get("source1485_topology") or json_digest(topology) != record.get("source1485_topology_sha256"):
            raise ReplaceError("private replacement audit Source-1485 topology is not canonical")
        plan = replace(plan, source1485_topology=topology)
    return plan


def current_entry_state(args: Args, entry: AuditEntry) -> tuple[str, Snapshot | None]:
    path = task_path(args.root, entry.task)
    if entry.before is None and not path.exists() and not path.is_symlink():
        return "before", None
    try:
        current = read_snapshot(path, f"current transaction state {entry.task}")
    except ReplaceError:
        return "unknown", None
    if stat.S_IMODE(current.state.st_mode) != entry.mode or current.state.st_gid != entry.gid:
        return "unknown", current
    if entry.before is not None and current.data == entry.before:
        return "before", current
    if current.data == entry.after:
        return "after", current
    return "unknown", current


def rollback_record(args: Args, entries: tuple[AuditEntry, ...], completed: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    failures: list[str] = []
    preserved: list[str] = []
    completed_set = set(completed)
    for entry in reversed(entries):
        state, current = current_entry_state(args, entry)
        try:
            if state == "after" and current is not None:
                if entry.before is None:
                    remove_created(current)
                else:
                    _ = replace_snapshot(current, entry.before, f"rollback {entry.task}")
            elif state == "unknown":
                if entry.task in completed_set or entry.before is None:
                    failures.append(f"{entry.task}: unrecognized current state preserved")
                else:
                    preserved.append(entry.task)
        except Exception as exc:  # rollback must retain every exact failure in durable evidence
            failures.append(f"{entry.task}: {exc}")
    for entry in entries:
        state, _current = current_entry_state(args, entry)
        if state == "before" or entry.task in preserved:
            continue
        if not any(failure.startswith(f"{entry.task}:") for failure in failures):
            failures.append(f"{entry.task}: rollback verification failed")
    return tuple(failures), tuple(preserved)


def after_snapshots(args: Args, entries: tuple[AuditEntry, ...]) -> tuple[Snapshot, ...]:
    snapshots: list[Snapshot] = []
    for entry in entries:
        state, current = current_entry_state(args, entry)
        if state != "after" or current is None:
            raise ReplaceError(f"committed recovery state is incomplete at {entry.task}")
        snapshots.append(current)
    return tuple(snapshots)


def recover_existing(
    args: Args,
    proof_path: Path,
    authority_path: Path,
) -> Recovery:
    record, audit_bytes, entries, membership = read_audit(args)
    plan = recovery_plan(args, entries, membership, record)
    states = tuple(current_entry_state(args, entry)[0] for entry in entries)
    if "unknown" in states:
        completed_value = record.get("completed_writes")
        completed = tuple(completed_value) if isinstance(completed_value, list) else ()
        failures, preserved = rollback_record(args, entries, completed)
        state = "rollback_failed" if failures else "rolled_back"
        record, audit_bytes = transition_audit(
            args.audit_output,
            audit_bytes,
            record,
            state,
            completed=completed,
            error="crash recovery found unrecognized concurrent lifecycle bytes",
            rollback_failures=(*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)),
        )
        detail = "; ".join((*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)))
        raise ReplaceError(f"crash recovery preserved unrecognized concurrent state; replacement remains closed: {detail}")
    state = record.get("state")
    commitment = record.get("close_proof_commitment")
    if not isinstance(state, str) or not isinstance(commitment, str):
        raise ReplaceError("private replacement audit state is malformed")
    inventory = pane_inventory()
    old = inventory.get(canonical_target(args.old_target))
    new = inventory.get(canonical_target(args.new_target))
    expected_old = PaneIdentity(canonical_target(args.old_target), args.old_pane_id, args.old_pane_pid, args.old_pane_start_ticks)
    if new is not None:
        raise ReplaceError("successor target is live during crash recovery; launch-before-singular-proof is rejected")
    proof = has_bound_close_proof(proof_path, commitment)
    authorized_absence = record.get("owner_close_evidence") == "authorized-absence"
    all_after = all(value == "after" for value in states)
    all_before = all(value == "before" for value in states)
    completed_value = record.get("completed_writes")
    completed = tuple(completed_value) if isinstance(completed_value, list) else ()
    if source1611_rollout_custody_requested(args) and old == expected_old and not proof:
        _ = source1611_rollout_custody(args)
    if state == "committed":
        if not all_after or old is not None or not (proof or authorized_absence):
            raise ReplaceError("committed replacement audit no longer has its exact committed state and close outcome evidence")
        snapshots = after_snapshots(args, entries)
        prove_committed(args, plan, snapshots[0], snapshots[1:-2], snapshots[-2], snapshots[-1])
        return Recovery(
            plan,
            record,
            audit_bytes,
            entries,
            True,
            f"recovered committed manager replacement; sole blocked successor ownership remains proved; audit={args.audit_output}",
        )
    if state == "stop_failed":
        if is_source1289_whole_tree(args):
            secret = record.get("close_proof_secret")
            if not isinstance(secret, str) or not all_before:
                raise ReplaceError("Source-1289 failed-stop recovery lost its exact before state")
            descendant_closed = all(
                has_bound_close_proof(
                    descendant_evidence_paths(args.audit_output, child)[1],
                    descendant_commitment(secret, child),
                )
                and canonical_target(child.target) not in inventory
                and process_start_ticks(child.pane_pid) is None
                for child in args.descendants
            )
            if not descendant_closed or descendant_progress(args, audit_bytes):
                raise ReplaceError("Source-1289 failed-stop recovery cannot prove every descendant closure")
            if old == expected_old and not proof:
                prepared = dict(record)
                prepared["state"] = "prepared"
                prepared["completed_writes"] = []
                prepared.pop("error", None)
                prepared.pop("rollback_failures", None)
                prepared_bytes = serialized_audit(prepared)
                authority = read_snapshot(authority_path, "bound close authority")
                if authority.data != serialized_audit(close_authority_record(args, prepared_bytes, commitment)):
                    raise ReplaceError("Source-1289 failed-stop recovery close authority changed")
                record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "prepared", completed=())
                return Recovery(plan, record, audit_bytes, entries, False)
            if old is None and proof:
                record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "owner_stopped", completed=())
                return Recovery(plan, record, audit_bytes, entries, True)
            raise ReplaceError("Source-1289 old-manager close outcome is ambiguous after descendant closure")
        if (
            not is_guest1269_replacement(args)
            or record.get("error") != "tmux symbolic target no longer owns the exact pane at command execution"
            or not all_before
            or old is not None
            or any(identity.pane_id == args.old_pane_id for identity in inventory.values())
            or process_start_ticks(args.old_pane_pid) is not None
        ):
            raise ReplaceError("prior guarded manager stop failed; its exact closed-owner recovery state cannot be proved")
        prepared_record = dict(record)
        prepared_record["state"] = "prepared"
        prepared_record["completed_writes"] = []
        prepared_record.pop("error", None)
        prepared_record.pop("rollback_failures", None)
        prepared_bytes = serialized_audit(prepared_record)
        authority = read_snapshot(authority_path, "bound close authority")
        expected_authority = serialized_audit(close_authority_record(args, prepared_bytes, commitment))
        if authority.data != expected_authority:
            raise ReplaceError("failed-stop recovery close-authority record changed")
        # 🧑 "Atomically close only exact failed `guest_hees:0` ... Verify old owner absent and exactly one successor owns all work"
        record["owner_close_evidence"] = "authorized-absence"
        record, audit_bytes = transition_audit(
            args.audit_output,
            audit_bytes,
            record,
            "owner_absent",
            completed=(),
        )
        return Recovery(plan, record, audit_bytes, entries, True)
    if state == "prepared" and all_before and old == expected_old and not proof:
        if not authority_path.exists():
            _ = reserve_audit(authority_path, close_authority_record(args, audit_bytes, commitment))
        authority = read_snapshot(authority_path, "bound close authority")
        if authority.data != serialized_audit(close_authority_record(args, audit_bytes, commitment)):
            raise ReplaceError("prepared recovery close-authority record changed")
        return Recovery(plan, record, audit_bytes, entries, False)
    if old is not None or not (proof or authorized_absence):
        raise ReplaceError("crash recovery cannot prove the exact old-manager close outcome")
    if all_after:
        snapshots = after_snapshots(args, entries)
        try:
            prove_committed(args, plan, snapshots[0], snapshots[1:-2], snapshots[-2], snapshots[-1])
        except Exception as exc:
            failures, preserved = rollback_record(args, entries, completed)
            rollback_state = "rollback_failed" if failures else "rolled_back"
            record, audit_bytes = transition_audit(
                args.audit_output,
                audit_bytes,
                record,
                rollback_state,
                completed=completed,
                error=f"commit recovery proof failed: {exc}",
                rollback_failures=(*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)),
            )
            if failures or preserved:
                raise ReplaceError("commit recovery proof failed and exact rollback could not be completed") from exc
        else:
            record, audit_bytes = transition_audit(
                args.audit_output,
                audit_bytes,
                record,
                "committed",
                completed=tuple(entry.task for entry in entries),
            )
            return Recovery(
                plan,
                record,
                audit_bytes,
                entries,
                True,
                f"recovered committed manager replacement; sole blocked successor ownership proved; audit={args.audit_output}",
            )
    if not all_before:
        failures, preserved = rollback_record(args, entries, completed)
        rollback_state = "rollback_failed" if failures else "rolled_back"
        record, audit_bytes = transition_audit(
            args.audit_output,
            audit_bytes,
            record,
            rollback_state,
            completed=completed,
            error="recovered interrupted lifecycle transaction",
            rollback_failures=(*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)),
        )
        if failures or preserved:
            raise ReplaceError("interrupted transaction could not be rolled back to its exact before state")
    if markdown_paths(args.root) != membership:
        raise ReplaceError("Markdown membership changed during crash recovery; exact before state retained")
    restored = recovery_plan(args, entries, membership, record)
    if authoritative_active_target_task_paths(args.root, args.old_target) != (restored.old.path.resolve(),):
        raise ReplaceError("crash recovery cannot prove the restored old manager as sole before-state owner")
    if authoritative_active_target_task_paths(args.root, args.new_target):
        raise ReplaceError("crash recovery found an unexpected successor owner")
    if active_child_task_refs(args.root, restored.old.path, args.old_target) != tuple(child.task for child in args.children):
        raise ReplaceError("crash recovery cannot prove the exact restored child set")
    recovered_state = "owner_absent" if authorized_absence else "owner_stopped"
    record, audit_bytes = transition_audit(
        args.audit_output,
        audit_bytes,
        record,
        recovered_state,
        completed=(),
    )
    return Recovery(restored, record, audit_bytes, entries, True)


def validate_panes_before_close(args: Args) -> None:
    inventory = pane_inventory()
    validate_live_bindings(args, inventory)


def descendant_progress_path(audit_path: Path) -> Path:
    return audit_path.with_name(f".{audit_path.name}.descendant-progress")


def descendant_progress(args: Args, _audit_bytes: bytes) -> str:
    path = descendant_progress_path(args.audit_output)
    if not path.exists():
        return ""
    snapshot = read_snapshot(path, "descendant close progress")
    try:
        value = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplaceError("descendant close progress is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("operation") != "manager-replace-descendant-progress"
        or value.get("replacement_binding_sha256") != json_digest(audit_binding(args))
        or value.get("task") not in {"", *(child.task for child in args.descendants)}
    ):
        raise ReplaceError("descendant close progress changed")
    return str(value["task"])


def set_descendant_progress(args: Args, _audit_bytes: bytes, task: str) -> None:
    path = descendant_progress_path(args.audit_output)
    data = (
        json.dumps(
            {
                "operation": "manager-replace-descendant-progress",
                "replacement_binding_sha256": json_digest(audit_binding(args)),
                "task": task,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if path.exists():
        current = read_snapshot(path, "descendant close progress")
        _ = replace_snapshot(current, data, "descendant close progress")
    else:
        _ = create_snapshot(path, data, 0o600)


def stop_descendants(
    args: Args,
    plan: Plan,
    record: dict[str, object],
    audit_bytes: bytes,
    secret: str,
) -> tuple[dict[str, object], bytes]:
    for child, expected in zip(args.descendants, plan.descendant_identities, strict=True):
        authority_path, proof_path = descendant_evidence_paths(args.audit_output, child)
        child_secret = descendant_secret(secret, child)
        commitment = descendant_commitment(secret, child)
        inventory = pane_inventory()
        if has_bound_close_proof(proof_path, commitment):
            if expected.target in inventory or process_start_ticks(expected.pid) is not None:
                raise ReplaceError(f"closed descendant identity reappeared: {child.task}")
            if descendant_progress(args, audit_bytes) == child.task:
                set_descendant_progress(args, audit_bytes, "")
            continue
        if descendant_progress(args, audit_bytes) == child.task and expected.target not in inventory:
            raise ReplaceError(f"descendant close outcome is durably ambiguous and requires exact external reconciliation: {child.task}")
        if inventory.get(expected.target) != expected:
            raise ReplaceError(f"active descendant pane identity changed before close: {child.task}")
        require_preclose_eligibility(args, plan)
        if descendant_progress(args, audit_bytes) != child.task:
            set_descendant_progress(args, audit_bytes, child.task)
        if not authority_path.exists():
            _ = reserve_audit(
                authority_path,
                descendant_close_authority_record(args, child, audit_bytes, commitment),
            )
        authority = read_snapshot(authority_path, f"descendant close authority {child.task}")
        if authority.data != serialized_audit(descendant_close_authority_record(args, child, audit_bytes, commitment)):
            raise ReplaceError(f"descendant close authority changed: {child.task}")

        def pre_input_check() -> None:
            require_preclose_eligibility(args, plan)
            current = pane_inventory()
            if current.get(expected.target) != expected:
                raise ReplaceError(f"active descendant pane identity changed before input: {child.task}")

        session_id = stop(
            StopArgs(
                target=child.target,
                wait_s=30.0,
                lines=2000,
                dry_run=False,
                allow_self=False,
                root=args.root,
                no_feedback=True,
                bound_symbolic_target=child.target,
                bound_pane_id=child.pane_id,
                bound_pane_pid=child.pane_pid,
                bound_pane_start_ticks=child.pane_start_ticks,
                bound_expected_session_id=child.session_id,
                bound_close_proof_path=str(proof_path),
                bound_close_audit_path=str(authority_path),
                bound_close_proof_secret=child_secret,
                bound_close_proof_commitment=commitment,
                bound_pre_input_check=pre_input_check,
            )
        )
        if session_id.lower() != child.session_id or not has_bound_close_proof(proof_path, commitment):
            raise ReplaceError(f"descendant close could not be proved: {child.task}")
        current = pane_inventory()
        if expected.target in current or process_start_ticks(expected.pid) is not None:
            raise ReplaceError(f"descendant remains live after guarded close: {child.task}")
        set_descendant_progress(args, audit_bytes, "")
    return record, audit_bytes


def require_preclose_eligibility(args: Args, plan: Plan) -> None:
    """Recheck every prepared lifecycle and authority binding before pane input."""

    if markdown_paths(args.root) != plan.initial_markdown_paths:
        raise ReplaceError("Markdown membership changed before guarded manager close")
    if plan.successor_path.exists() or plan.successor_path.is_symlink():
        raise ReplaceError("successor appeared before guarded manager close")
    if source1611_rollout_custody_requested(args):
        custody = source1611_rollout_custody(args)
        if custody is None or custody.session_id != args.old_session_id:
            raise ReplaceError("Source-1611 process-held rollout changed before guarded manager close")
    require_snapshot(plan.old, "pre-close old manager")
    require_snapshot(plan.todo, "pre-close TODO")
    require_snapshot(plan.authority, "pre-close replacement authority")
    require_snapshot(plan.authority_envelope, "pre-close replacement authority envelope")
    if plan.source1601_authority is not None:
        require_snapshot(plan.source1601_authority, "pre-close Source-1601 authority")
    if plan.source1597_authority is not None:
        require_snapshot(plan.source1597_authority, "pre-close Source-1597 direct-worker authority")
    require_source1611_direct_worker_parent(
        args,
        plan.source1611_parent,
        "pre-close Source-1611 direct-worker parent",
        active_child=args.old_task,
    )
    if plan.empty_tree_authority is not None:
        require_snapshot(plan.empty_tree_authority, "pre-close Source-1292 empty-tree authority")
    for child in plan.children:
        require_snapshot(child, f"pre-close active child {child.path.name}")
    if authoritative_active_target_task_paths(args.root, args.old_target) != (plan.old.path.resolve(),):
        raise ReplaceError("old target ownership changed before guarded manager close")
    if authoritative_active_target_task_paths(args.root, args.new_target):
        raise ReplaceError("successor target ownership appeared before guarded manager close")
    if active_child_task_refs(args.root, plan.old.path, args.old_target) != tuple(child.task for child in args.children):
        raise ReplaceError("active child set changed before guarded manager close")
    for child, snapshot in zip(args.descendants, plan.children):
        child_metadata = metadata(snapshot.data, args.root, f"pre-close active descendant {child.task}")
        if active_child_task_refs(args.root, snapshot.path, child_metadata.runat):
            raise ReplaceError(f"nested active descendant appeared before guarded close: {child.task}")
    if is_source1485_replacement(args):
        validate_source1485_protected_set(
            args,
            pane_inventory(),
            tuple(metadata(snapshot.data, args.root, f"Source-1485 retained child {snapshot.path.name}") for snapshot in plan.children),
        )
        if plan.source1485_topology is None or source1485_topology_binding(args, plan) != plan.source1485_topology:
            raise ReplaceError("Source-1485 simulated post-graph changed before guarded manager close")


def stop_old_manager(
    args: Args,
    plan: Plan,
    proof_path: Path,
    authority_path: Path,
    secret: str,
    commitment: str,
) -> None:
    protected_before: tuple[PaneIdentity, ...] = ()
    require_preclose_eligibility(args, plan)
    require_descendants_closed(args, plan, secret)
    if uses_protected_inventory(args):
        before_inventory = pane_inventory()
        validate_live_bindings(args, before_inventory, require_descendants=False)
        protected_before = tuple(identity for identity in protected_inventory(args, before_inventory) if identity.target != canonical_target(args.old_target))
    require_preclose_eligibility(args, plan)
    require_descendants_closed(args, plan, secret)

    def pre_input_check() -> None:
        require_preclose_eligibility(args, plan)
        validate_live_bindings(args, pane_inventory(), require_descendants=False)
        require_descendants_closed(args, plan, secret)

    session_id = stop(
        StopArgs(
            target=args.old_target,
            wait_s=30.0,
            lines=2000,
            dry_run=False,
            allow_self=False,
            root=args.root,
            task_file=args.old_task if is_pcodx_replacement(args) else "",
            no_feedback=True,
            bound_symbolic_target=args.old_target,
            bound_pane_id=args.old_pane_id,
            bound_pane_pid=args.old_pane_pid,
            bound_pane_start_ticks=args.old_pane_start_ticks,
            bound_expected_session_id=args.old_session_id,
            bound_close_proof_path=str(proof_path),
            bound_close_audit_path=str(authority_path),
            bound_close_proof_secret=secret,
            bound_close_proof_commitment=commitment,
            human_close_authorization_source=args.authority_file if is_pcodx_replacement(args) else "",
            human_close_authorization_sha256=args.authority_sha256 if is_pcodx_replacement(args) else "",
            human_close_authorized_target=args.old_target if is_pcodx_replacement(args) else "",
            bound_pre_input_check=(pre_input_check if uses_protected_inventory(args) or is_source1289_whole_tree(args) else None),
            bound_custody_status_bypass=source1611_rollout_custody_requested(args),
        )
    )
    if session_id.lower() != args.old_session_id:
        raise ReplaceError(f"stopped manager session id mismatch: expected {args.old_session_id}, found {session_id or '<missing>'}")
    if not has_bound_close_proof(proof_path, commitment):
        raise ReplaceError("old manager close did not produce its bound durable proof")
    inventory = pane_inventory()
    if canonical_target(args.old_target) in inventory:
        raise ReplaceError("old manager target remains live after guarded close")
    if canonical_target(args.new_target) in inventory:
        raise ReplaceError("successor target launched before singular ownership proof")
    if any(identity.target in inventory or process_start_ticks(identity.pid) is not None for identity in plan.descendant_identities):
        raise ReplaceError("a bound descendant remains live after guarded close")
    if uses_protected_inventory(args):
        if is_source_only_semantic_exception(args):
            validate_protected_bindings(args, inventory)
        if tuple(inventory.get(identity.target) for identity in protected_before) != protected_before:
            raise ReplaceError("non-replaced protected pane/process inventory changed during close")


def require_descendants_closed(args: Args, plan: Plan, secret: str) -> None:
    if not args.descendants:
        return
    inventory = pane_inventory()
    if descendant_progress(args, read_snapshot(args.audit_output, "private replacement audit").data):
        raise ReplaceError("descendant close progress is incomplete before old-manager close")
    for child, identity in zip(args.descendants, plan.descendant_identities, strict=True):
        proof = descendant_evidence_paths(args.audit_output, child)[1]
        if not has_bound_close_proof(proof, descendant_commitment(secret, child)) or identity.target in inventory or process_start_ticks(identity.pid) is not None:
            raise ReplaceError(f"descendant closure changed before old-manager close: {child.task}")


def prove_committed(args: Args, plan: Plan, old_after: Snapshot, child_after: tuple[Snapshot, ...], todo_after: Snapshot, successor: Snapshot) -> None:
    if args.closed_owner_audit is not None:
        _prepared, _authority, _source_args, _entries = authenticate_closed_owner_source(args)
        validate_closed_owner_absence(args)
    require_snapshot(plan.authority, "replacement authority")
    if plan.source1601_authority is not None:
        require_snapshot(plan.source1601_authority, "committed Source-1601 authority")
    if plan.source1597_authority is not None:
        require_snapshot(plan.source1597_authority, "committed Source-1597 direct-worker authority")
    require_source1611_direct_worker_parent(args, plan.source1611_parent, "committed Source-1611 direct-worker parent")
    authenticate_committed_authority_envelope(args, plan, child_after)
    if plan.empty_tree_authority is not None:
        require_snapshot(plan.empty_tree_authority, "Source-1292 empty-tree authority")
    require_snapshot(old_after, "committed old manager")
    require_snapshot(todo_after, "committed TODO")
    require_snapshot(successor, "committed successor")
    for snapshot in child_after:
        require_snapshot(snapshot, f"committed child {snapshot.path.name}")
    if authoritative_active_target_task_paths(args.root, args.old_target):
        raise ReplaceError("old target retains an active owner after replacement")
    if authoritative_active_target_task_paths(args.root, args.new_target) != (plan.successor_path.resolve(),):
        raise ReplaceError("new target does not have exactly one successor owner")
    if active_child_task_refs(args.root, plan.successor_path, args.new_target) != tuple(child.task for child in args.children):
        raise ReplaceError("successor does not own the exact migrated active-child set")
    if active_child_task_refs(args.root, plan.old.path, args.old_target):
        raise ReplaceError("old manager retains an active child after replacement")
    if plan.source1611_parent is not None and args.successor_task not in active_child_task_refs(args.root, plan.source1611_parent.path, args.parent_target):
        raise ReplaceError("Source-1611 direct-worker successor is not an active child of its exact parent")
    if is_source1485_replacement(args):
        committed_plan = replace(
            plan,
            child_after=tuple(snapshot.data for snapshot in child_after),
            successor_data=successor.data,
        )
        if plan.source1485_topology is None or source1485_topology_binding(args, committed_plan) != plan.source1485_topology:
            raise ReplaceError("Source-1485 committed ownership graph differs from its reviewed acyclic simulation")
    for snapshot, queue in zip(child_after, plan.child_queues, strict=True):
        if metadata(snapshot.data, args.root, snapshot.path.name).pending_task_items != queue:
            raise ReplaceError(f"active child queue changed during migration: {snapshot.path.name}")
    successor_metadata = metadata(successor.data, args.root, "successor task")
    if successor_metadata.pending_task_items != plan.successor_queue or successor_metadata.status != "blocked":
        raise ReplaceError("successor queue or launch gate changed")
    inventory = pane_inventory()
    if canonical_target(args.old_target) in inventory or canonical_target(args.new_target) in inventory:
        raise ReplaceError("old or successor pane is live at the singular ownership proof boundary")
    if any(identity.target in inventory or process_start_ticks(identity.pid) is not None for identity in plan.descendant_identities):
        raise ReplaceError("a replaced-tree descendant remains live at the singular ownership proof boundary")
    audit = read_snapshot(args.audit_output, "committed replacement audit")
    record = json.loads(audit.data)
    if source1611_rollout_custody_requested(args) and (not isinstance(record, dict) or any(record.get(key) != value for key, value in source1611_rollout_audit_binding(args).items())):
        raise ReplaceError("committed audit lost the exact Source-1611 process-held rollout custody binding")
    secret = record.get("close_proof_secret") if isinstance(record, dict) else None
    if not isinstance(secret, str) or any(not has_bound_close_proof(descendant_evidence_paths(args.audit_output, child)[1], descendant_commitment(secret, child)) for child in args.descendants):
        raise ReplaceError("a replaced-tree descendant lacks its durable guarded-close proof")
    if args.descendants and descendant_progress(args, audit.data) != "":
        raise ReplaceError("descendant close progress is incomplete at the singular ownership proof boundary")
    remaining_protected = tuple(identity for identity in plan.protected_identities if identity.target != canonical_target(args.old_target))
    if is_source_only_semantic_exception(args):
        validate_protected_bindings(args, inventory)
    if tuple(inventory.get(identity.target) for identity in remaining_protected) != remaining_protected:
        raise ReplaceError("protected pane/process inventory changed before singular ownership proof")
    expected_membership = tuple(sorted((*plan.initial_markdown_paths, plan.successor_path.resolve(strict=False)), key=str))
    if markdown_paths(args.root) != expected_membership:
        raise ReplaceError("Markdown membership changed before singular ownership proof")


def replace_manager(args: Args) -> str:
    validate_targets(args)
    if not args.root.is_dir():
        raise ReplaceError("work-log root is unavailable")
    initial_paths = markdown_paths(args.root)
    old_path = task_path(args.root, args.old_task)
    successor_path = task_path(args.root, args.successor_task)
    authority_source_path = task_path(args.root, args.authority_file)
    authority_envelope_path = task_path(args.root, args.authority_envelope_task)
    source1601_path = task_path(args.root, SOURCE1601_FILE)
    source1597_path = task_path(args.root, SOURCE1597_FILE)
    source1611_parent_path = task_path(args.root, SOURCE1611_SUCCESSOR_TASK)
    empty_tree_authority_path = task_path(args.root, SOURCE1292_FILE)
    close_authority_path, proof_path = closed_owner_evidence_paths(args.audit_output)
    source_evidence_paths = () if args.closed_owner_audit is None else (args.closed_owner_audit, *closed_owner_evidence_paths(args.closed_owner_audit))
    descendant_evidence = tuple(path for child in args.descendants for path in descendant_evidence_paths(args.audit_output, child))
    lock_paths = tuple(
        sorted(
            {
                *initial_paths,
                old_path,
                successor_path,
                authority_source_path,
                authority_envelope_path,
                *((source1601_path,) if source1601_required(args) else ()),
                *((source1597_path,) if source1597_direct_worker_required(args) else ()),
                *((source1611_parent_path,) if source1597_direct_worker_required(args) else ()),
                *((empty_tree_authority_path,) if is_source1292_empty_tree(args) or is_source1292_descendant_tree(args) else ()),
                args.audit_output,
                close_authority_path,
                proof_path,
                descendant_progress_path(args.audit_output),
                *source_evidence_paths,
                *descendant_evidence,
            },
            key=str,
        )
    )
    with ExitStack() as locks:
        locks.enter_context(root_membership_lock(args.root))
        for target in sorted(
            {
                canonical_target(args.old_target),
                canonical_target(args.new_target),
                *(canonical_target(child.target) for child in args.descendants),
                *((canonical_target(target) for target in args.protected_targets) if is_source1485_replacement(args) or is_source_only_semantic_exception(args) else ()),
            }
        ):
            locks.enter_context(task_target_lock(args.root, target))
        for path in lock_paths:
            locks.enter_context(task_file_lock(path))
        if markdown_paths(args.root) != initial_paths:
            raise ReplaceError("Markdown membership changed while replacement locks were acquired")
        owner_stopped = False
        if args.audit_output.exists() or args.audit_output.is_symlink():
            if args.closed_owner_audit is not None:
                current_record, _current_bytes, _current_entries, _current_membership = read_audit(args)
                validate_closed_owner_recovery(args, current_record)
            recovery = recover_existing(args, proof_path, close_authority_path)
            if recovery.result:
                return recovery.result
            plan = recovery.plan
            record = recovery.record
            audit_bytes = recovery.audit_bytes
            entries = recovery.entries
            owner_stopped = recovery.owner_stopped
            secret = record.get("close_proof_secret")
            commitment = record.get("close_proof_commitment")
            if not isinstance(secret, str) or not isinstance(commitment, str):
                raise ReplaceError("private replacement audit lost its close capability")
        else:
            if close_authority_path.exists() or proof_path.exists():
                raise ReplaceError("bound close authority or proof exists without its private replacement audit")
            if args.closed_owner_audit is None:
                validate_panes_before_close(args)
                plan = prepare(args, initial_paths)
                validate_panes_before_close(args)
                secret = os.urandom(32).hex()
                commitment = digest(secret.encode())
                record = audit_record(args, plan, secret, commitment)
                audit_bytes = reserve_audit(args.audit_output, record)
                _ = reserve_audit(close_authority_path, close_authority_record(args, audit_bytes, commitment))
            else:
                prepared_sha256, authority_sha256, source_args, source_entries = authenticate_closed_owner_source(args)
                validate_closed_owner_before_state(args, source_args, source_entries)
                plan = prepare(args, initial_paths)
                prepared_after, authority_after, source_args, source_entries = authenticate_closed_owner_source(args)
                if (prepared_after, authority_after) != (prepared_sha256, authority_sha256):
                    raise ReplaceError("closed-owner evidence changed while the fresh plan was prepared")
                validate_closed_owner_before_state(args, source_args, source_entries)
                secret = os.urandom(32).hex()
                commitment = digest(secret.encode())
                record = audit_record(args, plan, secret, commitment)
                record.update(
                    {
                        "state": "owner_absent",
                        "owner_close_evidence": "authorized-absence",
                        "closed_owner_prepared_sha256": prepared_sha256,
                        "closed_owner_authority_sha256": authority_sha256,
                    }
                )
                audit_bytes = reserve_audit(args.audit_output, record)
                owner_stopped = True
            record, audit_bytes, entries, membership = read_audit(args)
            if membership != plan.initial_markdown_paths:
                raise ReplaceError("private replacement audit did not preserve prepared Markdown membership")
        if args.closed_owner_audit is not None and owner_stopped:
            validate_closed_owner_recovery(args, record)
        if not owner_stopped:
            validate_live_bindings(args, pane_inventory(), require_descendants=False)
            # 🧑 Source `manager_mail/85c5dff58359-1289.txt`: "replace the entire agent tree for this task. Get completing new agents to do them."
            record, audit_bytes = stop_descendants(args, plan, record, audit_bytes, secret)
            try:
                stop_old_manager(args, plan, proof_path, close_authority_path, secret, commitment)
            except Exception as exc:
                try:
                    record, audit_bytes = transition_audit(
                        args.audit_output,
                        audit_bytes,
                        record,
                        "stop_failed",
                        completed=(),
                        error=str(exc),
                    )
                except Exception as audit_exc:
                    raise ReplaceError(f"manager stop failed and audit finalization failed: {exc}; audit: {audit_exc}") from exc
                raise ReplaceError(f"manager stop failed before lifecycle mutation: {exc}") from exc
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "owner_stopped", completed=())
        if is_source_only_semantic_exception(args):
            validate_protected_bindings(args, pane_inventory())
        completed: list[str] = []
        try:
            require_snapshot(plan.authority, "replacement authority")
            require_snapshot(plan.authority_envelope, "replacement authority envelope")
            if plan.source1601_authority is not None:
                require_snapshot(plan.source1601_authority, "Source-1601 replacement authority")
            if plan.source1597_authority is not None:
                require_snapshot(plan.source1597_authority, "Source-1597 direct-worker authority")
            require_source1611_direct_worker_parent(
                args,
                plan.source1611_parent,
                "Source-1611 direct-worker parent",
                active_child=args.old_task,
            )
            if plan.empty_tree_authority is not None:
                require_snapshot(plan.empty_tree_authority, "Source-1292 replacement authority")
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "mutating", completed=())
            # 🧑 "The agent failed. They did not run the experiment. Replace them. The replacement agent should finish the task."
            updated_old = replace_snapshot(plan.old, plan.old_after, "old manager")
            completed.append(args.old_task)
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "mutating", completed=tuple(completed))
            updated_children: list[Snapshot] = []
            for pin, before, after_data in zip(args.children, plan.children, plan.child_after, strict=True):
                after = replace_snapshot(before, after_data, f"active child {pin.task}")
                updated_children.append(after)
                completed.append(pin.task)
                record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "mutating", completed=tuple(completed))
            updated_todo = replace_snapshot(plan.todo, plan.todo_after, "TODO")
            completed.append("TODO.md")
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "mutating", completed=tuple(completed))
            successor = create_snapshot(
                plan.successor_path,
                plan.successor_data,
                stat.S_IMODE(plan.old.state.st_mode),
                plan.old.state.st_gid,
            )
            completed.append(args.successor_task)
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "proving", completed=tuple(completed))
            prove_committed(args, plan, updated_old, tuple(updated_children), updated_todo, successor)
            record, audit_bytes = transition_audit(args.audit_output, audit_bytes, record, "committed", completed=tuple(completed))
        except Exception as exc:
            failures, preserved = rollback_record(args, entries, tuple(completed))
            state = "rollback_failed" if failures else "rolled_back"
            try:
                record, audit_bytes = transition_audit(
                    args.audit_output,
                    audit_bytes,
                    record,
                    state,
                    completed=tuple(completed),
                    error=str(exc),
                    rollback_failures=(*failures, *(f"{task}: concurrent bytes preserved" for task in preserved)),
                )
            except Exception as audit_exc:
                raise ReplaceError(f"replacement failed; rollback failures={failures or 'none'}; audit finalization failed: {audit_exc}") from exc
            if failures:
                raise ReplaceError(f"replacement failed and rollback was incomplete: {'; '.join(failures)}") from exc
            if preserved:
                raise ReplaceError(f"replacement failed; all owned lifecycle writes rolled back and concurrent bytes were preserved: {', '.join(preserved)}") from exc
            raise ReplaceError(f"replacement failed; all lifecycle writes rolled back: {exc}") from exc
        return (
            f"closed {args.old_task}, migrated {len(args.children)} active child task(s), and created blocked unlaunched "
            f"{args.successor_task}; sole ownership at {args.new_target} proved; launch remains a separate supported operation; audit={args.audit_output}"
        )


def main(argv: list[str] | None = None) -> int:
    try:
        print(replace_manager(parse_args(sys.argv[1:] if argv is None else argv)))
    except (OSError, ReplaceError, TaskFrontmatterError, ValueError) as exc:
        print(f"omo_manager_replace.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
