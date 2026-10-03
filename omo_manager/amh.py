#!/usr/bin/env python3
"""Agent manager helper: one command for agents and managers.

Start at `amh --help` and follow the groups down; every level has its own `--help`.
"""
# 🧑 "Consolidate the manager helpers. Make them into one command `amh` ... Make the subcommands nested and with sane names ... anyone can start from the root --help page and work their way naturally"
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

HELPER_DIR = Path(__file__).resolve().parent
LOCAL_ENV = Path(os.environ.get("OMO_MANAGER_LOCAL_ENV", HELPER_DIR / "local.env"))
OMNIGENT_PREFIX = "omnigent://"
# 🧑 "make the manager reporting sound more like messaging the manager, to discourage agents to dump info to manager"
MANAGER_MESSAGE_MAX_CHARS = 600
Run = Callable[[argparse.Namespace, list[str]], int]


def helper(name: str) -> str:
    return str(HELPER_DIR / name)


def call(*command: str) -> int:
    runner = [sys.executable] if command[0].endswith(".py") and not os.access(command[0], os.X_OK) else []
    return subprocess.run([*runner, *command], check=False).returncode


def work_log_root() -> Path:
    """Return the task-list directory, read from `local.env` because agent shells do not export it."""
    configured = os.environ.get("OMO_WORK_LOGS_ROOT", "")
    if not configured and LOCAL_ENV.is_file():
        match = re.search(r'^export OMO_WORK_LOGS_ROOT="([^"$]+)"$', LOCAL_ENV.read_text(encoding="utf-8"), re.MULTILINE)
        configured = match.group(1) if match else ""
    root = Path(configured) if configured else Path.home() / "work_logs"
    os.environ["OMO_WORK_LOGS_ROOT"] = str(root)
    return root


def address(who: str) -> str:
    """Resolve a task file name to where its agent runs; pass an address through."""
    if not who.endswith(".md"):
        return who
    match = re.search(r"^runat: (\S+)$", (work_log_root() / who).read_text(encoding="utf-8")[:4000], re.MULTILINE)
    if match is None:
        raise SystemExit(f"amh: {who} records no agent address")
    return match.group(1)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def message_file(args: argparse.Namespace, label: str) -> str:
    """Return a file holding the message, writing inline text to a private file."""
    if args.file:
        return args.file
    if not args.text:
        raise SystemExit("amh: give the message as TEXT or with --file")
    directory = Path(f"/tmp/amh-messages-{os.getuid()}")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{label}-{os.getpid()}.md"
    _ = path.write_text(args.text.rstrip() + "\n", encoding="utf-8")
    return str(path)


def tell_manager(args: argparse.Namespace, extra: list[str]) -> int:
    if len(args.text) > MANAGER_MESSAGE_MAX_CHARS:
        raise SystemExit(
            f"amh: {len(args.text)} characters is too long; your manager wants at most {MANAGER_MESSAGE_MAX_CHARS}. "
            + "Say only what you need from them or what changed. Put details in a file and name its path."
        )
    allocated = subprocess.run([helper("omo_report.sh"), "--alloc-message-file"], capture_output=True, text=True, check=False)
    if allocated.returncode != 0:
        sys.stderr.write(allocated.stderr)
        return allocated.returncode
    path = allocated.stdout.strip().splitlines()[-1]
    _ = Path(path).write_text(args.text.rstrip() + "\n", encoding="utf-8")
    status = "blocked" if args.blocked else "done" if args.done else "in-progress"
    sent = subprocess.run([helper("omo_report.sh"), "--status", status, "--message-file", path, *extra], capture_output=True, text=True, check=False)
    try:
        receipt = json.loads(sent.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        sys.stdout.write(sent.stdout)
        sys.stderr.write(sent.stderr)
        return sent.returncode
    print(f"message sent to your manager at {receipt.get('requested_manager_target')}; do not send it again ({receipt.get('reason')})")
    return sent.returncode


def tell_human(args: argparse.Namespace, extra: list[str]) -> int:
    body = ["--message-file", args.file] if args.file else []
    replaced = [x for message_id in args.replaces for x in ("--supersedes-message-id", message_id)]
    return call(str(HELPER_DIR.parent / "helper.sh" / "email_me.py"), "--subject", args.subject, *body, *replaced, *extra)


def tell_agent(args: argparse.Namespace, extra: list[str]) -> int:
    target, path = address(args.who), message_file(args, "agent")
    if target.startswith(OMNIGENT_PREFIX):
        return call(helper("omo_omnigent.py"), "--target", target, "send", "--message-file", path, *extra)
    return call(helper("omo_tmux_send.py"), "--target", target, "--message-file", path, *extra)


def authorship(args: argparse.Namespace) -> str:
    return "--human-authored" if args.from_human else "--agent-authored"


def task_record(args: argparse.Namespace, extra: list[str]) -> int:
    if args.from_human and not args.email_file:
        raise SystemExit("amh: --from-human requires --email-file MAIL.txt, the stored manager_mail/*.txt file with the human's request")
    ack = ["--ack-human", "--email-file", args.email_file] if args.from_human else []
    items = [x for item in args.item for x in ("--item", item)]
    return call(helper("omo_record_pending.py"), "--pending-file", args.file, "--line", args.line, *items, authorship(args), *ack, *extra)


def todo_done(args: argparse.Namespace, extra: list[str]) -> int:
    outcome = "cancelled" if args.cancelled else "completed"
    return call(
        helper("omo_pending.py"), "remove", "--item", args.item, "--outcome", outcome, "--evidence", args.evidence, "--completion-key", sha256(args.item), *extra
    )


def task_start(args: argparse.Namespace, extra: list[str]) -> int:
    command = [helper("omo_task.py"), "--task-file", args.task, "--workdir", args.dir, "--prompt-file", args.prompt]
    for flag, value in (("--tool", args.tool), ("--model", args.model), ("--reasoning-effort", args.effort), ("--manager-target", args.manager)):
        command += [flag, value] if value else []
    command += ["--is-manager"] if args.as_manager else []
    command += ["--tmux", "--tmux-session", args.tmux] if args.tmux else []
    command += ["--human-email-file", args.email, "--human-email-lines", args.lines] if args.email else []
    command += ["--dry-run"] if args.dry_run else []
    return call(*command, *extra)


# 🧑 "the task reference moves from `TODO.md` `current` to the top of `previous`"
# 🧑 "when closing an agent ... automatically email the Human `Closed xx:n`, including the agent window"
def task_close(args: argparse.Namespace, extra: list[str]) -> int:
    """Close one task directly: refuse open items, stop the agent, mark done, reindex, email the human."""
    sys.path.insert(0, str(HELPER_DIR))
    from omo_task_lock import task_file_lock  # pyright: ignore[reportMissingImports]

    root = work_log_root()
    path, todo = root / args.task, root / "TODO.md"
    head = path.read_text(encoding="utf-8").partition("\n---\n")[0]
    if re.search(r"^pending_task_items: \[\]$", head, re.MULTILINE) is None:
        raise SystemExit(f"amh: {args.task} still has open items; finish, move, or cancel them first (`amh task show {args.task}`)")
    target = address(args.task)
    stopped = agent_stop(argparse.Namespace(who=target), extra)
    if stopped != 0 and not args.agent_gone:
        raise SystemExit(f"amh: could not stop the agent at {target}; if it no longer exists, repeat with --agent-gone")
    with task_file_lock(path):
        text = path.read_text(encoding="utf-8")
        head, separator, body = text.partition("\n---\n")
        lines = [re.sub(r"^status: .*$", "status: done", line) for line in head.split("\n") if not line.startswith("blocked_on:")]
        _ = path.write_text("\n".join(lines) + separator + body, encoding="utf-8")
    with task_file_lock(todo):
        rows = todo.read_text(encoding="utf-8").split("\n")
        own = [row for row in rows if row.split(" ")[0] == args.task]
        rows = [row for row in rows if row not in own]
        rows.insert(rows.index("previous:") + 1, own[0] if own else f"{args.task} {target}")
        _ = todo.write_text("\n".join(rows), encoding="utf-8")
    print(f"closed {args.task} at {target}")
    if args.no_email:
        return 0
    email = subprocess.run(
        [str(HELPER_DIR.parent / "helper.sh" / "email_me.py"), "--subject", f"Closed {args.task.removesuffix('.md')}"],
        input=f"Closed the task {args.task}; its agent at {target} is stopped.\n",
        text=True,
        check=False,
    )
    return email.returncode


def task_status(args: argparse.Namespace, extra: list[str]) -> int:
    blocked = ["--blocked-on", args.on] if args.on else []
    return call(helper("omo_task_status.py"), args.task, args.status, *blocked, *extra)


def agent_status(args: argparse.Namespace, extra: list[str]) -> int:
    target = address(args.who)
    if target.startswith(OMNIGENT_PREFIX):
        return call(helper("omo_omnigent.py"), "--target", target, "status", *extra)
    return call(helper("omo_codex_status.py"), target, *extra)


def agent_stop(args: argparse.Namespace, extra: list[str]) -> int:
    target = address(args.who)
    if target.startswith(OMNIGENT_PREFIX):
        return call(helper("omo_omnigent.py"), "--target", target, "stop", *extra)
    return call(helper("omo_codex_stop.py"), "--target", target, *extra)


def manager_rotate(args: argparse.Namespace, extra: list[str]) -> int:
    command = [helper("omo_manager_rotate.py"), "--target", address(args.who)]
    for flag, value in (("--tool", args.tool), ("--model", args.model), ("--reasoning-effort", args.effort)):
        command += [flag, value] if value else []
    return call(*command, *extra)


def passthrough(*command: str) -> Run:
    return lambda _args, extra: call(*command, *extra)


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="amh", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    groups = root.add_subparsers(dest="group", metavar="GROUP", required=True)

    def group(name: str, summary: str) -> argparse._SubParsersAction:  # pyright: ignore[reportPrivateUsage]
        parser = groups.add_parser(name, help=summary, description=summary)
        return parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    def action(parent: argparse._SubParsersAction, name: str, summary: str, run: Run, detail: str = "") -> argparse.ArgumentParser:  # pyright: ignore[reportPrivateUsage]
        parser = parent.add_parser(name, help=summary, description=f"{summary}\n\n{detail}".strip(), formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.set_defaults(run=run)
        return parser

    tell = group("tell", "Send a message to your manager, the human, or an agent you manage.")
    p = action(
        tell,
        "manager",
        "Message your manager, like a chat message.",
        tell_manager,
        f"Use it only to ask for help or coordination, or to say you are done or blocked.\nAt most {MANAGER_MESSAGE_MAX_CHARS} characters. For details, write a file and name its path in the message.",
    )
    p.add_argument("text", metavar="TEXT", help="what you need from the manager, or what changed, in one or two sentences")
    state = p.add_mutually_exclusive_group()
    state.add_argument("--blocked", action="store_true", help="you cannot continue without the manager")
    state.add_argument("--done", action="store_true", help="your task is finished")
    p = action(tell, "human", "Email the human.", tell_human, "The body comes from --file, or from standard input when --file is omitted.\nReuse the subject of the thread you are answering.")
    p.add_argument("--subject", required=True, help="plain English, under 60 characters")
    p.add_argument("--file", help="file holding the email body")
    p.add_argument("--replaces", metavar="MESSAGE_ID", action="append", default=[], help="an earlier unread email of yours that this one replaces; repeat as needed, then run `amh mail trash-replaced`")
    p = action(tell, "agent", "Message an agent you manage.", tell_agent)
    p.add_argument("who", metavar="WHO", help="the agent's task file name, such as `x.md`, or its address")
    p.add_argument("text", metavar="TEXT", nargs="?", help="the message; or use --file")
    p.add_argument("--file", help="file holding the message")

    todo = group("todo", "Read and update your own list of open work.")
    _ = action(todo, "list", "Show your open work.", passthrough(helper("omo_pending.py"), "list"))
    p = action(todo, "add", "Add one open item.", lambda a, e: call(helper("omo_pending.py"), "add", "--item", a.item, authorship(a), *e))
    p.add_argument("item", metavar="ITEM", help="the work, quoting the human's words when it is their request")
    p.add_argument("--from-human", action="store_true", help="the human asked for this; omit for work you or another agent added")
    p = action(todo, "done", "Remove one finished or cancelled item.", todo_done)
    p.add_argument("item", metavar="ITEM", help="the exact item text as `amh todo list` shows it, including a leading 🧑 mark; removing a 🧑 item emails the human")
    p.add_argument("--evidence", required=True, help="one sentence saying how you know it is finished")
    p.add_argument("--cancelled", action="store_true", help="the item was cancelled, not finished")
    p = action(todo, "replace", "Reword one open item.", lambda a, e: call(helper("omo_pending.py"), "replace", "--old-item", a.old, "--new-item", a.new, *e))
    p.add_argument("old", metavar="OLD")
    p.add_argument("new", metavar="NEW")

    task = group("task", "Managers: create, inspect, and close the tasks of agents you manage.")
    p = action(
        task,
        "start",
        "Create a task and start an agent on it.",
        task_start,
        "The prompt file holds one goal line, a blank line, then at least one `- ` bullet subgoal.\nDefaults: workers run Codex gpt-6.1-sol low; managers run Claude claude-opus-5-5 low.",
    )
    p.add_argument("task", metavar="TASK.md", help="new task file name; also the agent's tag in email subjects")
    p.add_argument("--dir", required=True, help="directory the agent works in")
    p.add_argument("--prompt", required=True, help="file holding the goal and bullet subgoals")
    p.add_argument("--tool", choices=("claude", "codex", "cursor", "antigravity"), help="agent program")
    p.add_argument("--model", help="model id, such as claude-opus-5-5")
    p.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"), help="reasoning effort")
    p.add_argument("--as-manager", action="store_true", help="start a manager that will run its own workers")
    p.add_argument("--manager", help="address of the manager the agent reports to, when it is not you")
    p.add_argument("--tmux", metavar="SESSION", help="start in this tmux session; Codex and Cursor only")
    p.add_argument("--email", metavar="FILE", help="stored human email that caused this task; needs --lines")
    p.add_argument("--lines", metavar="START-END", help="the relevant lines of --email")
    p.add_argument("--dry-run", action="store_true", help="print what would happen")
    p = action(task, "show", "Show a task's status, agent address, and open items.", lambda a, e: call(helper("omo_task_edit.py"), "summary", *a.task, *e))
    p.add_argument("task", metavar="TASK.md", nargs="+")
    p = action(
        task,
        "close",
        "Mark a task done, stop its agent, and move it to `previous` in the task list.",
        task_close,
        "Refuses while the task has open items. Emails the human one line saying the task is closed.",
    )
    p.add_argument("task", metavar="TASK.md")
    p.add_argument("--agent-gone", action="store_true", help="close even though the agent cannot be stopped because it no longer exists")
    p.add_argument("--no-email", action="store_true", help="do not email the human; for test tasks only")
    p = action(task, "status", "Set a task's status.", task_status)
    p.add_argument("task", metavar="TASK.md")
    p.add_argument("status", choices=("running", "long_running", "blocked"))
    p.add_argument("--on", help="with `blocked`: the task file or human review it waits on")
    p = action(task, "note", "Append a manager note to a task file.", lambda a, e: call(helper("omo_task_edit.py"), "comment-add", "--message", a.text, a.task, *e))
    p.add_argument("task", metavar="TASK.md")
    p.add_argument("text", metavar="TEXT")
    p = action(task, "give", "Add one open item to a task's list.", lambda a, e: call(helper("omo_task_edit.py"), "pending-add", "--item", a.item, authorship(a), a.task, *e))
    p.add_argument("task", metavar="TASK.md")
    p.add_argument("item", metavar="ITEM")
    p.add_argument("--from-human", action="store_true", help="the human asked for this")
    p = action(
        task, "move", "Move one open item from one task to another.", lambda a, e: call(helper("omo_task_edit.py"), "pending-move", "--from", a.source, "--to", a.to, "--item", a.item, *e)
    )
    p.add_argument("source", metavar="FROM.md")
    p.add_argument("to", metavar="TO.md")
    p.add_argument("item", metavar="ITEM")
    p = action(
        task,
        "record",
        "Turn a `(pending)` marker in a task file into open items and clear the marker.",
        task_record,
    )
    p.add_argument("file", metavar="FILE.md", help="file holding the marker")
    p.add_argument("line", metavar="LINE", help="line number of the marker")
    p.add_argument("item", metavar="ITEM", nargs="+", help="one open item per argument, quoting the source's words")
    p.add_argument("--from-human", action="store_true", help="the items are the human's requests; emails the human an acknowledgement, so needs --email-file")
    p.add_argument("--email-file", metavar="MAIL.txt", help="with --from-human: the stored `manager_mail/*.txt` file holding the human's request")
    _ = action(task, "check", "Check that task files and the task list agree.", passthrough(helper("omo_task_audit.py"), "--check"))

    agent = group("agent", "Managers: look at and control running agents.")
    _ = action(agent, "problems", "List agents that are stuck, failed, or missing.", passthrough(helper("omo_agent_status.py"), "--problems-only", "--no-auto-unstick"))
    _ = action(agent, "list", "List active tasks and their agents.", passthrough(helper("omo_agent_status.py"), "--no-auto-unstick"))
    _ = action(agent, "tree", "Show who reports to whom.", passthrough(helper("omo_agent_tree.py"), "--full-tree"))
    for name, summary, run in (
        ("status", "Show whether one agent is running, idle, or broken.", agent_status),
        ("stop", "Stop one agent without closing its task.", agent_stop),
    ):
        p = action(agent, name, summary, run)
        p.add_argument("who", metavar="WHO", help="the agent's task file name, such as `x.md`, or its address")
    p = action(
        agent, "compact", "Shrink a tmux Codex agent's context once it is idle.", lambda a, e: call(helper("omo_codex_compact_when_idle.py"), "--target", address(a.who), *e)
    )
    p.add_argument("who", metavar="WHO")

    mail = group("mail", "Tidy the emails you sent the human.")
    _ = action(mail, "unread", "List your emails the human has not read yet.", passthrough(helper("omo_manager_mail_compress.py"), "agent-unread"))
    _ = action(
        mail,
        "trash-replaced",
        "Trash your unread emails that a newer email of yours replaced.",
        passthrough(helper("omo_manager_mail_compress.py"), "agent-trash-replaced"),
        "Pass the values `amh mail unread` printed: --uid, --source-uidvalidity, --replacement-message-id, then --yes.",
    )

    manager = group("manager", "Main manager: watchers, replacing a manager, and repository checks.")
    _ = action(manager, "watchers", "Start or refresh the email and pending-work watchers.", passthrough(helper("omo_manager_setup_watchers.sh")))
    p = action(
        manager,
        "rotate",
        "Replace a manager with a fresh one that takes over its tasks.",
        manager_rotate,
        "Write open context to files linked from the task list first; the fresh manager starts without your memory.",
    )
    p.add_argument("who", metavar="WHO", help="the manager's task file name or address")
    p.add_argument("--tool", choices=("claude", "codex", "cursor", "antigravity"), help="change the agent program; needs --model and --effort")
    p.add_argument("--model")
    p.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"))
    _ = action(manager, "worktree", "List uncommitted changes in manager-owned repositories.", passthrough(helper("omo_worktree_check.py")))
    return root


def main(argv: list[str]) -> int:
    args, extra = build_parser().parse_known_args(argv)
    _ = work_log_root()
    run: Run = args.run
    return run(args, extra)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
