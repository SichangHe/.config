#!/usr/bin/env python3
"""Send one owner-authenticated completion email for a new task mutation."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_email_config import guest_hees_target
from omo_manager.omo_task_context import current_active_task
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_file_lock_at_path

EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"
COMPLETION_ENTRYPOINT = Path(__file__).resolve()
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RECONCILIATION_VERSION = "v1"
NO_CONTACT_RE = re.compile(
    r"\bsource[- ]985\b|\bno[- ]contact\b|\b(?:do not|must not|never) (?:send )?(?:any )?(?:human(?:-facing)? )?(?:email|mail|message|report|outreach|contact)\b|\b(?:no|forbid(?:s|den)?) human-facing reports?\b|\bhuman reporting (?:is )?(?:suppressed|forbidden|prohibited|paused)\b|\bwithout human email\b|\breport only privately\b|\bprivate reports? only\b",
    re.IGNORECASE,
)
MANAGER_ONLY_RE = re.compile(r"\b(?:report|return) only\b[^.\n]{0,100}\b(?:manager|submanager)\b|\bmanager[- ]only reports?\b", re.IGNORECASE)
DIRECT_HUMAN_REPORT_RE = re.compile(r"\b(?:email|report|respond|write)\b[^.\n]{0,100}\b(?:directly to )?(?:the )?human\b", re.IGNORECASE)
SOURCE1241_REF = "manager_mail/85c5dff58359-1241.txt:1-7"
SOURCE1241_TASK = "hmanager_replace_fix.md"
SOURCE1241_HUMAN = """Subject: Re: Why recent agent replies were missing

I don't know why I should send transfer now. I already told that other
agent to talk to the agent they are in conflict with. They should sort it
out themselves. I also don't understand what needs my authorisation. I
asked for those things to be implemented and they should agents should go
ahead and do the job"""
SOURCE1241_ENVELOPE = f'<human_instruction authoritative="true" source="{SOURCE1241_REF}">\n{SOURCE1241_HUMAN}\n</human_instruction>'
SOURCE1241_META_SPAN = "Preserve exact-owner and no-contact safeguards."
SOURCE1241_META_LINE = (
    "This exact durable Human instruction resolves the stale request for another authorization step: agents must resolve the installed-entry-point "
    "overlap themselves and complete the already requested implementation/closure. Read-only deployed-contract verification shows "
    f"`{COMPLETION_ENTRYPOINT}` is mode 755 and self-binds `COMPLETION_ENTRYPOINT = Path(__file__).resolve()`. "
    "Use that supported deployed entry point for the owner-authenticated completion notice with the existing task/root/outcome arguments. "
    f"{SOURCE1241_META_SPAN} Do not perform production replacement or other task work."
)
SOURCE1241_CONTEXT = f"(from manager omo_task_edit delegate-message)\n{SOURCE1241_ENVELOPE}\n\n{SOURCE1241_META_LINE}"
SOURCE1241_LOCATOR_ENVELOPE_RE = re.compile(
    rf'(?ms)^<human_instruction[ \t]+authoritative="true"[ \t]+source="{re.escape(SOURCE1241_REF)}">\r?\n(?P<body>.*?)\r?\n</human_instruction>[ \t]*$'
)
ROUTING_OPEN_TAG_RE = re.compile(r"<(?P<tag>[A-Za-z][A-Za-z0-9_:-]*)(?:[ \t][^>]*)?>")
ROUTING_CLOSE_TAG_RE = re.compile(r"</(?P<tag>[A-Za-z][A-Za-z0-9_:-]*)>")
ROUTING_TAG_TOKEN_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9_:-]*(?:[ \t][^>]*)?>")
MARKDOWN_FENCE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})")


@dataclass(frozen=True)
class ContactPolicyBinding:
    task_sha256: str
    source: Path
    source_sha256: str


@dataclass(frozen=True)
class CompletionEmail:
    root: Path
    task: Path
    target: str
    manager_target: str
    task_sha256: str
    outcome: str
    subject: str
    body: str
    key: str
    contact_policy: ContactPolicyBinding | None = None


def completion_email_state_dir() -> Path:
    return Path(os.environ.get("OMO_MANAGER_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"))


def require_completion_entrypoint() -> None:
    """Fail closed unless the installed owner-authenticated entry point is safe to execute."""

    try:
        metadata = COMPLETION_ENTRYPOINT.lstat()
    except OSError as exc:
        raise OSError(f"completion entry point is unavailable: {COMPLETION_ENTRYPOINT}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise OSError(f"completion entry point has unsafe type or ownership: {COMPLETION_ENTRYPOINT}")
    if metadata.st_mode & 0o022 or not metadata.st_mode & 0o111:
        raise OSError(f"completion entry point is not safely executable: {COMPLETION_ENTRYPOINT}")


def stable_owned_file(path: Path, maximum_bytes: int) -> bytes:
    """Read one owner-controlled regular file without following or racing a path rebind."""

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022:
            raise OSError(f"contact-policy source is unsafe: {path}")
        payload = b""
        while chunk := os.read(fd, min(65_536, maximum_bytes + 1 - len(payload))):
            payload += chunk
            if len(payload) > maximum_bytes:
                break
        after = os.fstat(fd)
    finally:
        os.close(fd)
    bound = path.lstat()
    if len(payload) > maximum_bytes:
        raise OSError(f"contact-policy source exceeds {maximum_bytes} bytes: {path}")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or (after.st_dev, after.st_ino) != (bound.st_dev, bound.st_ino):
        raise OSError(f"contact-policy source changed while read: {path}")
    return payload


def is_top_level_record(text: str, offset: int) -> bool:
    """Reject a record nested in routing markup or a Markdown code fence."""

    tags: list[str] = []
    fence = ""
    comment = False
    for line in text[:offset].splitlines():
        if comment:
            if "<!--" in line:
                return False
            if "-->" not in line:
                continue
            comment = False
            line = line.partition("-->")[2]
        if "-->" in line and "<!--" not in line:
            return False
        if "<!--" in line:
            before, _opening, after = line.partition("<!--")
            if "-->" not in after:
                comment = True
                line = before
            else:
                if "<!--" in after.partition("-->")[2]:
                    return False
                line = before + after.partition("-->")[2]
        if len(ROUTING_TAG_TOKEN_RE.findall(line)) > 1:
            return False
        fence_match = MARKDOWN_FENCE_RE.match(line)
        if not fence and fence_match is not None:
            fence = fence_match.group("fence")
            continue
        if fence:
            closing = line.strip()
            if closing and set(closing) == {fence[0]} and len(closing) >= len(fence):
                fence = ""
            continue
        close_match = ROUTING_CLOSE_TAG_RE.search(line)
        if close_match is not None:
            tag = close_match.group("tag")
            if not tags or tags.pop() != tag:
                return False
            continue
        open_match = ROUTING_OPEN_TAG_RE.search(line)
        if open_match is not None:
            tags.append(open_match.group("tag"))
    return not tags and not fence and not comment


# 🧑 Human source `manager_mail/85c5dff58359-1241.txt:1-7`: "They should sort it out themselves. ... they should go ahead and do the job"
def source1241_contact_clarification(root: Path, task: Path, text: str) -> ContactPolicyBinding | None:
    """Bind the sole exact Source-1241 meta-reference that is not a no-contact directive."""

    context_offset = text.find(SOURCE1241_CONTEXT)
    context_end = context_offset + len(SOURCE1241_CONTEXT)
    source1241_envelopes = SOURCE1241_LOCATOR_ENVELOPE_RE.findall(text)
    if (
        task.resolve() != (root / SOURCE1241_TASK).resolve()
        or text.count(SOURCE1241_CONTEXT) != 1
        or text.count(SOURCE1241_ENVELOPE) != 1
        or source1241_envelopes != [SOURCE1241_HUMAN]
        or text.count(SOURCE1241_META_LINE) != 1
        or text.count(SOURCE1241_META_SPAN) != 1
        or (context_offset > 0 and text[context_offset - 1] != "\n")
        or (context_end < len(text) and text[context_end] != "\n")
        or not is_top_level_record(text, context_offset)
    ):
        return None
    try:
        task_payload = stable_owned_file(task, 8_000_000)
        source = root / SOURCE1241_REF.partition(":")[0]
        source_payload = stable_owned_file(source, 8_000_000)
        source_text = source_payload.decode()
    except (OSError, UnicodeDecodeError):
        return None
    if task_payload != text.encode() or "\n".join(source_text.splitlines()[:7]) != SOURCE1241_HUMAN:
        return None
    return ContactPolicyBinding(hashlib.sha256(task_payload).hexdigest(), source, hashlib.sha256(source_payload).hexdigest())


def build_completion_email(root: Path, task: Path, text: str, outcome: str, *, items: tuple[str, ...] = (), evidence: str = "") -> CompletionEmail | None:
    """Build the canonical notice without assigning reporter authority."""

    metadata = parse_task_metadata(text, root)
    policy_text = text
    contact_policy = None
    if SOURCE1241_META_SPAN in text:
        contact_policy = source1241_contact_clarification(root, task, text)
        if contact_policy is not None:
            policy_text = text.replace(SOURCE1241_META_SPAN, "", 1)
    contact_forbidden = NO_CONTACT_RE.search(policy_text) is not None or (
        MANAGER_ONLY_RE.search(policy_text) is not None and DIRECT_HUMAN_REPORT_RE.search(policy_text) is None
    )
    if (
        metadata is None
        or metadata.runat == "retired"
        or metadata.runat.partition(":")[0].startswith("h")
        or guest_hees_target(metadata.runat)
        or contact_forbidden
    ):
        return None
    relative = task.resolve().relative_to(root.resolve()).as_posix()
    details = [f"Task: {relative}", f"Outcome: {outcome}"]
    if items:
        details.append("Items:")
        details.extend(f"- {item}" for item in items)
    if evidence:
        details.append(f"Evidence: {evidence}")
    subject = f"{task.name}: {outcome}"
    body = "\n".join(details) + "\n"
    task_sha256 = hashlib.sha256(text.encode()).hexdigest()
    identity_parts = (
        str(root.resolve()),
        relative,
        metadata.runat,
        metadata.managerat,
        task_sha256,
        outcome,
        "\n".join(items),
        evidence,
        subject,
        body,
    )
    if contact_policy is not None:
        identity_parts += (contact_policy.task_sha256, str(contact_policy.source), contact_policy.source_sha256)
    identity = "\0".join(identity_parts)
    return CompletionEmail(
        root.resolve(),
        task.resolve(),
        metadata.runat,
        metadata.managerat,
        task_sha256,
        outcome,
        subject,
        body,
        hashlib.sha256(identity.encode()).hexdigest(),
        contact_policy,
    )


# 🧑 Human source `202607/manager_mail/85c5dff58359-1090.txt:1-3`: "You should let the responsible agent report and discuss with me directly instead of duplicating messages to me"
def plan_completion_email(
    root: Path,
    task: Path,
    text: str,
    outcome: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
    human_subject: str = "",
    human_body: str = "",
) -> CompletionEmail | None:
    """Return mail only when the caller is the exact task owner and contact is allowed."""

    canonical = build_completion_email(root, task, text, outcome, items=items, evidence=evidence)
    if canonical is None:
        return None
    try:
        caller_path = current_active_task(root).resolve()
    except (OSError, TaskFrontmatterError):
        return None
    if caller_path != task.resolve():
        return None
    require_completion_entrypoint()
    relative = task.resolve().relative_to(root.resolve()).as_posix()
    if bool(human_subject) != bool(human_body):
        raise ValueError("human answer requires both subject and body")
    if human_subject:
        if human_subject.strip() != human_subject or "\n" in human_subject or "\r" in human_subject:
            raise ValueError("human answer subject must be one non-empty trimmed line")
        subject = human_subject
        body = f"{human_body.rstrip()}\n\nCompletion record:\n{canonical.body}"
    else:
        return canonical
    identity_parts = (
        str(root.resolve()),
        relative,
        canonical.target,
        canonical.manager_target,
        canonical.task_sha256,
        outcome,
        "\n".join(items),
        evidence,
        subject,
        body,
    )
    if canonical.contact_policy is not None:
        identity_parts += (canonical.contact_policy.task_sha256, str(canonical.contact_policy.source), canonical.contact_policy.source_sha256)
    identity = "\0".join(identity_parts)
    return CompletionEmail(
        canonical.root,
        canonical.task,
        canonical.target,
        canonical.manager_target,
        canonical.task_sha256,
        outcome,
        subject,
        body,
        hashlib.sha256(identity.encode()).hexdigest(),
        canonical.contact_policy,
    )


def reconciliation_record(
    plan: CompletionEmail,
    task_sha256: str,
    receipt: Path,
    receipt_sha256: str,
    target_state: Path,
) -> str:
    relative = plan.task.relative_to(plan.root).as_posix()
    message_sha256 = hashlib.sha256(f"{plan.subject}\0{plan.body}".encode()).hexdigest()
    outcome_sha256 = hashlib.sha256(plan.outcome.encode()).hexdigest()
    values = (
        ("version", RECONCILIATION_VERSION),
        ("completion_key", plan.key),
        ("root", str(plan.root)),
        ("task", relative),
        ("owner", plan.target),
        ("outcome_sha256", outcome_sha256),
        ("task_sha256", task_sha256),
        ("message_sha256", message_sha256),
        ("source_receipt", str(receipt)),
        ("source_receipt_sha256", receipt_sha256),
        ("target_state", str(target_state)),
    )
    if any(any(character in value for character in "\r\n") for _, value in values):
        raise ValueError("completion reconciliation values must be single-line")
    return "".join(f"{key}={value}\n" for key, value in values)


def reconciled_completion_is_delivered(plan: CompletionEmail) -> bool:
    marker = completion_email_state_dir() / "completion-email-reconciled" / plan.key
    try:
        payload = owned_private_file(marker, "reconciled completion", 16_384).decode()
    except FileNotFoundError:
        return False
    except UnicodeDecodeError as exc:
        raise OSError("reconciled completion is not UTF-8") from exc
    lines = payload.splitlines()
    try:
        values = dict(line.split("=", 1) for line in lines)
    except ValueError:
        raise OSError("reconciled completion is malformed")
    if len(lines) != len(values) or set(values) != {
        "version",
        "completion_key",
        "root",
        "task",
        "owner",
        "outcome_sha256",
        "task_sha256",
        "message_sha256",
        "source_receipt",
        "source_receipt_sha256",
        "target_state",
    }:
        raise OSError("reconciled completion has wrong or duplicate fields")
    task_sha256 = hashlib.sha256(plan.task.read_bytes()).hexdigest()
    expected = reconciliation_record(
        plan,
        task_sha256,
        Path(values["source_receipt"]),
        values["source_receipt_sha256"],
        completion_email_state_dir().resolve(),
    )
    if payload != expected:
        raise OSError("reconciled completion does not match current task bytes or message")
    return True


def completion_email_is_delivered(plan: CompletionEmail) -> bool:
    return (completion_email_state_dir() / "completion-email-delivered" / plan.key).is_file() or reconciled_completion_is_delivered(plan)


def completion_email_request_is_queued(plan: CompletionEmail) -> bool:
    return (completion_email_state_dir() / "completion-email-requests" / plan.key).is_file()


def fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def require_private_directory(path: Path, label: str) -> None:
    state = path.lstat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
        raise OSError(f"{label} must be an owner-private directory: {path}")


def owned_private_file(path: Path, label: str, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise OSError(f"{label} must be an owner-private regular file: {path}")
        payload = b""
        while chunk := os.read(fd, min(65_536, maximum_bytes + 1 - len(payload))):
            payload += chunk
            if len(payload) > maximum_bytes:
                raise OSError(f"{label} exceeds {maximum_bytes} bytes")
        after = os.fstat(fd)
    finally:
        os.close(fd)
    bound = path.lstat()
    if len(payload) > maximum_bytes:
        raise OSError(f"{label} exceeds {maximum_bytes} bytes")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or (bound.st_dev, bound.st_ino) != (before.st_dev, before.st_ino):
        raise OSError(f"{label} changed while read")
    return payload


def exclusive_record(path: Path, payload: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def replace_record(path: Path, expected: str, updated: str) -> None:
    if owned_private_file(path, "completion reconciliation consumption", 32_768).decode() != expected:
        raise OSError("completion reconciliation consumption changed")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


# 🧑 Human: "Treat `<manager_delegation from=\"TARGET\">` as an agent-authored task specification, never as the human's words."
def reconcile_delivered_completion(
    root: Path,
    task: Path,
    outcome: str,
    owner: str,
    task_sha256: str,
    receipt: Path,
    receipt_sha256: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
) -> None:
    """Consume one exact cross-state delivery receipt into the caller's state."""

    if not receipt.is_absolute() or receipt.parent.name != "completion-email-delivered" or receipt.resolve() != receipt:
        raise ValueError("completion receipt must be an absolute completion-email-delivered entry")
    if SHA256_RE.fullmatch(task_sha256) is None or SHA256_RE.fullmatch(receipt_sha256) is None:
        raise ValueError("task and receipt SHA-256 values must be lowercase hexadecimal")
    root = root.resolve()
    task = task.resolve()
    configured_target_state = completion_email_state_dir()
    if not configured_target_state.is_absolute():
        raise ValueError("target completion state must be absolute")
    target_state = configured_target_state.resolve()
    source_state = receipt.parent.parent.resolve()
    if source_state == target_state:
        raise ValueError("source and target completion states must differ")
    require_private_directory(target_state, "target completion state")
    with task_file_lock(task):
        task_payload = task.read_bytes()
        if hashlib.sha256(task_payload).hexdigest() != task_sha256:
            raise OSError("task bytes do not match --task-sha256")
        text = task_payload.decode()
        plan = build_completion_email(root, task, text, outcome, items=items, evidence=evidence)
        if plan is None or plan.target != owner or plan.task_sha256 != task_sha256:
            raise OSError("task, owner, or completion outcome does not match the receipt")
        if receipt.name != plan.key:
            raise OSError("completion receipt does not match the canonical completion message")
        record = reconciliation_record(plan, task_sha256, receipt, receipt_sha256, target_state)
        prepared = f"status=prepared\n{record}"
        completed = f"status=completed\n{record}"
        consume_dir = source_state / "completion-email-reconciliations"
        reconciled_dir = target_state / "completion-email-reconciled"
        consume = consume_dir / plan.key
        destination = reconciled_dir / plan.key
        lock_paths = sorted(
            {source_state / "completion-email-reconcile.lock", target_state / "completion-email-reconcile.lock"},
            key=str,
        )
        with ExitStack() as locks:
            for lock_path in lock_paths:
                _ = locks.enter_context(task_file_lock_at_path(lock_path))
            require_private_directory(source_state, "source completion state")
            require_private_directory(receipt.parent, "source delivery directory")
            require_private_directory(target_state, "target completion state")
            receipt_payload = owned_private_file(receipt, "completion receipt", 4096)
            if hashlib.sha256(receipt_payload).hexdigest() != receipt_sha256:
                raise OSError("completion receipt does not match --receipt-sha256")
            if receipt_payload.decode() != f"{owner}\t{task.name}\n":
                raise OSError("completion receipt task or owner is wrong")
            claims = owned_private_file(source_state / "completion-email-claims.tsv", "completion claims ledger", 8_000_000).decode()
            matching_claims = [line for line in claims.splitlines() if line.split("\t", 1)[0] == plan.key]
            if matching_claims != [f"{plan.key}\t{owner}\t{task.name}\t{plan.manager_target}\t{task_sha256}"]:
                raise OSError("completion receipt has missing or ambiguous claim evidence")
            if hashlib.sha256(task.read_bytes()).hexdigest() != task_sha256:
                raise OSError("task bytes changed during completion reconciliation")
            consume_dir.mkdir(mode=0o700, exist_ok=True)
            reconciled_dir.mkdir(mode=0o700, exist_ok=True)
            require_private_directory(consume_dir, "source reconciliation directory")
            require_private_directory(reconciled_dir, "target reconciliation directory")
            try:
                consumption = owned_private_file(consume, "completion reconciliation consumption", 32_768).decode()
            except FileNotFoundError:
                if destination.exists():
                    raise OSError("completion reconciliation destination is ambiguous")
                exclusive_record(consume, prepared)
                consumption = prepared
            if consumption == completed:
                raise OSError("completion receipt was already consumed")
            if consumption != prepared:
                raise OSError("completion receipt has an ambiguous prior consumption")
            try:
                destination_payload = owned_private_file(destination, "reconciled completion", 16_384).decode()
            except FileNotFoundError:
                exclusive_record(destination, record)
            else:
                if destination_payload != record:
                    raise OSError("completion reconciliation destination is ambiguous")
            replace_record(consume, prepared, completed)


def mark_completion_email_request_queued(plan: CompletionEmail) -> None:
    directory = completion_email_state_dir() / "completion-email-requests"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = directory / plan.key
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        _ = handle.write(f"{plan.target}\t{plan.task}\t{plan.subject}\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(directory)


def mark_completion_email_delivered(plan: CompletionEmail) -> None:
    directory = completion_email_state_dir() / "completion-email-delivered"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = directory / plan.key
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        _ = handle.write(f"{plan.target}\t{plan.task.name}\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(directory)


def claim_completion_email(plan: CompletionEmail) -> bool:
    """Durably reserve one exact message before invoking the mail helper."""

    state_dir = completion_email_state_dir()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    ledger = state_dir / "completion-email-claims.tsv"
    lock_path = state_dir / "completion-email-claims.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            previous = ledger.read_text(encoding="utf-8")
        except FileNotFoundError:
            previous = ""
        keys = {line.partition("\t")[0] for line in previous.splitlines()}
        if plan.key in keys:
            return False
        temporary = ledger.with_name(f".{ledger.name}.{os.getpid()}.tmp")
        temporary_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            _ = handle.write(f"{previous}{plan.key}\t{plan.target}\t{plan.task.name}\t{plan.manager_target}\t{plan.task_sha256}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ledger)
        fsync_directory(state_dir)
    return True


def send_completion_email(plan: CompletionEmail | None) -> bool:
    """Send a claimed message once; an uncertain outcome remains claimed."""

    if plan is None:
        return False
    try:
        require_completion_entrypoint()
        task_payload = stable_owned_file(plan.task, 8_000_000)
        if hashlib.sha256(task_payload).hexdigest() != plan.task_sha256:
            raise OSError("task bytes changed before delivery")
        if plan.contact_policy is not None:
            source_payload = stable_owned_file(plan.contact_policy.source, 8_000_000)
            if hashlib.sha256(task_payload).hexdigest() != plan.contact_policy.task_sha256 or (
                hashlib.sha256(source_payload).hexdigest() != plan.contact_policy.source_sha256
            ):
                raise OSError("contact-policy authority changed before delivery")
    except OSError as exc:
        print(f"automatic completion email blocked before claim: {exc}", file=sys.stderr)
        return False
    if not claim_completion_email(plan):
        return False
    with tempfile.TemporaryDirectory(prefix="omo-completion-email-") as tmp:
        subject = Path(tmp) / "subject.txt"
        body = Path(tmp) / "body.txt"
        subject.write_text(plan.subject + "\n", encoding="utf-8")
        body.write_text(plan.body, encoding="utf-8")
        command = [str(EMAIL_HELPER), "--manager-human"]
        command.extend(("--subject-file", str(subject), "--message-file", str(body)))
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"automatic completion email failed or is uncertain; replay suppressed: {exc}", file=sys.stderr)
            return False
    mark_completion_email_delivered(plan)
    return True


def require_owner_completion(
    root: Path,
    task: Path,
    text: str,
    outcome: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
    human_subject: str = "",
    human_body: str = "",
    owner_may_mutate_after_delivery: bool = False,
) -> bool:
    """Require exact-owner delivery before a manager-driven mutation proceeds."""

    canonical = build_completion_email(root, task, text, outcome, items=items, evidence=evidence)
    if canonical is None:
        return True
    owner_plan = plan_completion_email(
        root,
        task,
        text,
        outcome,
        items=items,
        evidence=evidence,
        human_subject=human_subject,
        human_body=human_body,
    )
    effective = owner_plan or canonical
    require_completion_entrypoint()
    if completion_email_is_delivered(effective):
        return True
    if owner_plan is not None:
        if not send_completion_email(owner_plan):
            raise OSError("responsible-owner completion email was not confirmed delivered")
        return owner_may_mutate_after_delivery
    if completion_email_request_is_queued(canonical):
        return False
    command = [str(COMPLETION_ENTRYPOINT), "--root", str(root), "--task", str(task), "--outcome", outcome]
    for item in items:
        command.extend(("--item", item))
    if evidence:
        command.extend(("--evidence", evidence))
    message = (
        "Before this mutation can complete, send its single owner-authenticated completion notice. "
        "Run this exact command, then report completion to your manager:\n"
        f"{shlex.join(command)}"
    )
    from omo_manager.omo_tmux_send import send_system_to_codex

    send_system_to_codex(canonical.target, message)
    mark_completion_email_request_queued(canonical)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one owner-authenticated completion notice.")
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task", type=Path, required=True)
    _ = parser.add_argument("--outcome", required=True)
    _ = parser.add_argument("--item", action="append", default=[])
    _ = parser.add_argument("--evidence", default="")
    _ = parser.add_argument(
        "--reconcile-delivered",
        action="store_true",
        help="Consume one exact delivered receipt from another owner-private completion state.",
    )
    _ = parser.add_argument("--owner", default="", help="Exact task owner required by --reconcile-delivered.")
    _ = parser.add_argument("--task-sha256", default="", help="Exact current task digest required by --reconcile-delivered.")
    _ = parser.add_argument("--receipt", type=Path, help="Exact delivered marker required by --reconcile-delivered.")
    _ = parser.add_argument("--receipt-sha256", default="", help="Exact delivered-marker digest required by --reconcile-delivered.")
    parsed = parser.parse_args(argv)
    root = parsed.root.resolve()
    task = parsed.task if parsed.task.is_absolute() else root / parsed.task
    try:
        reconciliation_values = (parsed.owner, parsed.task_sha256, parsed.receipt, parsed.receipt_sha256)
        if parsed.reconcile_delivered:
            if not all(reconciliation_values):
                parser.error("--reconcile-delivered requires owner, task digest, receipt, and receipt digest.")
            reconcile_delivered_completion(
                root,
                task,
                parsed.outcome,
                parsed.owner,
                parsed.task_sha256,
                parsed.receipt,
                parsed.receipt_sha256,
                items=tuple(parsed.item),
                evidence=parsed.evidence,
            )
            print(f"Reconciled delivered completion receipt for {task.name} into {completion_email_state_dir()}.")
            return 0
        if any(reconciliation_values):
            parser.error("owner, task digest, and receipt options require --reconcile-delivered.")
        text = task.read_text(encoding="utf-8")
        plan = plan_completion_email(root, task, text, parsed.outcome, items=tuple(parsed.item), evidence=parsed.evidence)
    except (OSError, TaskFrontmatterError, ValueError) as exc:
        print(f"omo_completion_email.py: {exc}", file=sys.stderr)
        return 2
    if plan is None:
        print("omo_completion_email.py: caller is not the exact owner or reporting is suppressed", file=sys.stderr)
        return 2
    if completion_email_is_delivered(plan):
        return 0
    return 0 if send_completion_email(plan) else 2


if __name__ == "__main__":
    raise SystemExit(main())
