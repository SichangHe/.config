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
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_task_context import current_active_task

EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"
COMPLETION_ENTRYPOINT = Path(__file__).resolve()
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
    return CompletionEmail(root.resolve(), task.resolve(), metadata.runat, subject, body, hashlib.sha256(identity.encode()).hexdigest())


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
    return CompletionEmail(canonical.root, canonical.task, canonical.target, subject, body, hashlib.sha256(identity.encode()).hexdigest())


def completion_email_is_delivered(plan: CompletionEmail) -> bool:
    return (completion_email_state_dir() / "completion-email-delivered" / plan.key).is_file()


def completion_email_request_is_queued(plan: CompletionEmail) -> bool:
    return (completion_email_state_dir() / "completion-email-requests" / plan.key).is_file()


def fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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
    parsed = parser.parse_args(argv)
    root = parsed.root.resolve()
    task = parsed.task if parsed.task.is_absolute() else root / parsed.task
    try:
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
