"""The one background watcher: takes in the human's email, delivers `(pending)` blocks, and nudges managers and idle agents."""

from __future__ import annotations

import imaplib
import random
import sys
import time
from pathlib import Path

from amh import agents, config as configuration, mail, taskfile, work
from amh.config import Config
from amh.taskfile import MARKER, Task

MAIL_SOURCE = "(record and delegate manager_mail/"
# 🧑 "Why so slow ... Improve it"
MAIL_POLL_S = 5
RECONNECT_S = 30
RETRY_S = 600
REMINDER_S = 1800
ACCEPT = "Immediately email the Human to acknowledge acceptance. Record every open item with `amh todo add --from-human`, quoting the human's words:"
ADD_REMINDER = "\nAdd every still-open request to your open items."


def log(text: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}", flush=True)


def for_manager(body: str) -> bool:
    """Whether the human's own text starts or ends with `for manager`, or starts by asking to terminate or replace the agent."""
    # 🧑 “We should only dispatch “for manager” type and replace/terminate type messages to managers.”
    own = [line.strip(" \t.,:;!()[]*_").lower() for line in body.splitlines() if line.strip() and not line.lstrip().startswith(">")]
    return bool(own) and ("for manager" in (own[0], own[-1]) or own[0].startswith(("terminate this agent", "replace this agent")))


def route(config: Config, subject: str, body: str) -> Task:
    """Pick the task a human email belongs to: its `[tag]`, else that task's manager, else the main manager."""
    # 🧑 "it did not get routed to the correct Omnigent agent or even its task file"
    tasks = {task.name: task for task in taskfile.active_tasks(config) if task.address != "retired"}
    by_address = {task.address: task for task in tasks.values()}
    tag = mail.tag_of(subject)
    if tag and f"{tag}.md" in tasks:
        tagged = tasks[f"{tag}.md"]
        return by_address.get(tagged.fields.get("managerat", ""), tagged) if for_manager(body) else tagged
    if tag and (config.root / f"{tag}.md").is_file():
        try:
            manager = taskfile.load(config, f"{tag}.md").fields.get("managerat", "")
        except ValueError:
            manager = ""
        if manager in by_address:
            return by_address[manager]
    if work.main_manager(config) not in by_address:
        raise LookupError("no active task file runs at the main manager address")
    return by_address[work.main_manager(config)]


def take_in_mail(config: Config, box: imaplib.IMAP4_SSL) -> None:
    """Store each new human email and append a `(pending)` block naming it to the right task file."""
    for incoming in mail.fetch_new(box, config):
        name = f"{config.get('AMH_MAIL_PREFIX', 'mail')}-{incoming.uid}.txt"
        stored = config.mail_dir / name
        if not stored.exists():
            try:
                with taskfile.locked(config):
                    task = route(config, incoming.subject, incoming.body)
                    stored.write_text(f"Subject: {incoming.subject}\n\n{incoming.body}", encoding="utf-8")
                    stored.chmod(0o600)
                    path = config.root / task.name
                    taskfile.write(path, path.read_text(encoding="utf-8").rstrip("\n") + f"\n\n{MARKER}\n{MAIL_SOURCE}{name})\n")
            except (LookupError, ValueError, OSError) as error:
                stored.unlink(missing_ok=True)
                log(f"mail {name} {incoming.subject!r} left unread, not taken in: {error!r}")
                continue
            log(f"mail {name} {incoming.subject!r} -> {task.name}")
        mail.mark_read(box, incoming.uid)


def delivery_text(config: Config, block: list[str]) -> str:
    """Turn a pending block into the message for its agent; a stored human email is inlined verbatim."""
    # 🧑 "Pending blocks in task files should not be `manager_delegation`, they should be dispatched naked, randomly followed by a reminder to add to pending task items"
    # 🧑 "human requests should be sent verbatim"
    reminder = ADD_REMINDER if random.random() < 1 / 8 else ""
    if block[0].startswith(MAIL_SOURCE):
        name = block[0].removeprefix(MAIL_SOURCE).rstrip(")")
        content = (config.mail_dir / Path(name).name).read_text(encoding="utf-8").strip()
        return f'{ACCEPT}\n<human_instruction authoritative="true" source="manager_mail/{name}">\n{content}\n</human_instruction>{reminder}'
    return "\n".join(block).strip() + reminder


def deliver_pending(config: Config, failed_at: dict[str, float]) -> None:
    """Send every pending block to its task's agent, then delete the marker line."""
    for task in taskfile.active_tasks(config):
        if MARKER not in task.body or task.address == "retired" or time.monotonic() - failed_at.get(task.name, -RETRY_S) < RETRY_S:
            continue
        for _, block in taskfile.pending_blocks(task.body):
            if not block:
                continue
            try:
                state, evidence = agents.status(config, task.address)
                if state == "missing":
                    raise agents.AgentError(f"the agent is missing: {evidence}")
                agents.send(config, task.address, delivery_text(config, block))
            except (agents.AgentError, OSError) as error:
                failed_at[task.name] = time.monotonic()
                log(f"delivery to {task.name} at {task.address} failed: {error}")
                break
            log(f"delivered {block[0][:80]!r} to {task.name} at {task.address}")
            with taskfile.locked(config):
                fresh = taskfile.load(config, task.name)
                lines = fresh.body.split("\n")
                at = next((i for i, found in taskfile.pending_blocks(fresh.body) if found[:1] == block[:1]), None)
                if at is None:
                    failed_at[task.name] = time.monotonic()
                    log(f"delivered block is no longer in {task.name}; its marker was not deleted")
                    break
                del lines[at]
                fresh.body = "\n".join(lines)
                taskfile.save(config, fresh)


def nudge(config: Config, told_at: dict[tuple[str, str], float]) -> None:
    """Remind idle agents of their open items and tell managers about agents that are gone or failed."""
    by_manager: dict[str, list[str]] = {}
    for task, problem, evidence in work.problems(config):
        if "unreachable" in evidence or time.monotonic() - told_at.get((task.name, problem), -REMINDER_S) < REMINDER_S:
            continue
        told_at[task.name, problem] = time.monotonic()
        try:
            if problem == "idle with open items":
                agents.send(config, task.address, f"You have {len(task.items)} open items. See them with `amh todo list`. Continue until each is finished or cancelled.")
            else:
                by_manager.setdefault(task.fields.get("managerat", ""), []).append(f"{task.name} at {task.address}: {problem} ({evidence})")
        except agents.AgentError as error:
            log(f"reminder to {task.name} failed: {error}")
    for manager, rows in by_manager.items():
        try:
            agents.send(config, manager, "Handle these agent problems; only email the human if you cannot:\n" + "\n".join(rows))
        except agents.AgentError as error:
            log(f"problem notice to {manager} failed: {error}; rows: {rows}")


def run() -> int:
    """Loop forever; the service manager restarts the process if it dies."""
    failed_at: dict[str, float] = {}
    told_at: dict[tuple[str, str], float] = {}
    last_nudge = 0.0
    box = None
    retry_login_at = 0.0
    log("watcher started")
    while True:
        config = configuration.load()
        try:
            if box is None and time.monotonic() >= retry_login_at:
                box = imaplib.IMAP4_SSL(mail.IMAP_HOST, timeout=60)
                _ = box.login(config.get("OMO_AGENT_GMAIL_ADDRESS"), config.get("OMO_AGENT_GMAIL_APP_PASSWORD"))
            if box is not None:
                take_in_mail(config, box)
        except (imaplib.IMAP4.error, OSError) as error:
            log(f"mail connection failed, reconnecting in {RECONNECT_S} s: {error!r}")
            box = None
            retry_login_at = time.monotonic() + RECONNECT_S
        except Exception as error:
            log(f"mail intake failed: {error!r}")
        try:
            deliver_pending(config, failed_at)
            if time.monotonic() - last_nudge > 60:
                last_nudge = time.monotonic()
                nudge(config, told_at)
        except (Exception, SystemExit) as error:
            log(f"task scan failed: {error!r}")
        time.sleep(MAIL_POLL_S)


if __name__ == "__main__":
    sys.exit(run())
