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


@dataclass(frozen=True)
class CompletionEmail:
    root: Path
    task: Path
    target: str
    outcome: str
    subject: str
    body: str
    key: str


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


def build_completion_email(root: Path, task: Path, text: str, outcome: str, *, items: tuple[str, ...] = (), evidence: str = "") -> CompletionEmail | None:
    """Build the canonical notice without assigning reporter authority."""

    metadata = parse_task_metadata(text, root)
    contact_forbidden = NO_CONTACT_RE.search(text) is not None or (MANAGER_ONLY_RE.search(text) is not None and DIRECT_HUMAN_REPORT_RE.search(text) is None)
    if metadata is None or metadata.runat == "retired" or metadata.runat.partition(":")[0].startswith("h") or contact_forbidden:
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
    identity = "\0".join((str(root.resolve()), relative, metadata.runat, outcome, "\n".join(items), evidence, subject, body))
    return CompletionEmail(root.resolve(), task.resolve(), metadata.runat, outcome, subject, body, hashlib.sha256(identity.encode()).hexdigest())


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
    identity = "\0".join((str(root.resolve()), relative, canonical.target, outcome, "\n".join(items), evidence, subject, body))
    return CompletionEmail(canonical.root, canonical.task, canonical.target, outcome, subject, body, hashlib.sha256(identity.encode()).hexdigest())


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
        plan = build_completion_email(root, task, text, outcome)
        if plan is None or plan.target != owner:
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
            if matching_claims != [f"{plan.key}\t{owner}\t{task.name}"]:
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
            _ = handle.write(f"{previous}{plan.key}\t{plan.target}\t{plan.task.name}\n")
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
            if parsed.item or parsed.evidence or not all(reconciliation_values):
                parser.error("--reconcile-delivered requires owner, task digest, receipt, and receipt digest without item or evidence.")
            reconcile_delivered_completion(
                root,
                task,
                parsed.outcome,
                parsed.owner,
                parsed.task_sha256,
                parsed.receipt,
                parsed.receipt_sha256,
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
