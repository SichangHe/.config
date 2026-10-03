"""Agent manager helper: one command for agents and managers.

Start at `amh --help` and follow the groups down; every level has its own `--help`.
"""
# 🧑 "Consolidate the manager helpers. Make them into one command `amh` ... Make the subcommands nested and with sane names ... anyone can start from the root --help page and work their way naturally"
# 🧑 "Don't keep calling the old helper commands. Integrate them into a single program. Later we will rewrite it in Rust."

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from amh import agents, config as configuration, guest, mail, taskfile, work
from amh.config import Config

Run = Callable[[Config, argparse.Namespace], int]
UNIT = "amh-watch.service"


def todo_list(config: Config, args: argparse.Namespace) -> int:
    print("\n".join(taskfile.own_task(config, args.task_file).items))
    return 0


def todo_add(config: Config, args: argparse.Namespace) -> int:
    with taskfile.locked(config):
        task = taskfile.own_task(config, args.task_file)
        new = work.add_items(config, task, [args.item], args.from_human)
    work.notify_human(config, task, "created", new)
    print(f"added {len(new)} open item(s)")
    return 0


def todo_done(config: Config, args: argparse.Namespace) -> int:
    with taskfile.locked(config):
        task = taskfile.own_task(config, args.task_file)
        work.remove_item(config, task, args.item, args.evidence)
    work.notify_human(config, task, "deleted", [args.item])
    print("removed 1 open item")
    return 0


def todo_replace(config: Config, args: argparse.Namespace) -> int:
    with taskfile.locked(config):
        task = taskfile.own_task(config, args.task_file)
        if args.old not in task.items:
            raise SystemExit("amh: no such open item; copy it exactly from `amh todo list`")
        task.items[task.items.index(args.old)] = taskfile.mark(args.new, args.old.startswith(taskfile.HUMAN_MARK))
        taskfile.save(config, task)
    print("replaced 1 open item")
    return 0


def tell_manager(config: Config, args: argparse.Namespace) -> int:
    work.tell_manager(config, taskfile.own_task(config, args.task_file), args.text, "blocked" if args.blocked else "done" if args.done else "")
    print("message sent to your manager; do not send it again")
    return 0


def tell_human(config: Config, args: argparse.Namespace) -> int:
    body = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    work.tell_human(config, taskfile.own_task(config, args.task_file), args.subject, body, args.replaces)
    return 0


def tell_agent(config: Config, args: argparse.Namespace) -> int:
    if bool(args.text) == bool(args.file):
        raise SystemExit("amh: give the message as TEXT or as --file, not both")
    text = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
    agents.send(config, address(config, args.who), work.envelope(config.own_address(), text))
    return 0


def tell_guest(config: Config, args: argparse.Namespace) -> int:
    print(f"Email sent to the guest.\nMessage-ID: {guest.reply(config, args.mail, Path(args.file).read_text(encoding='utf-8'), [Path(image) for image in args.image])}")
    return 0


def address(config: Config, who: str) -> str:
    """Resolve a task file name to where its agent runs; pass an address through."""
    return taskfile.load(config, who).address if who.endswith(".md") else who


def task_start(config: Config, args: argparse.Namespace) -> int:
    if bool(args.email) != bool(args.lines):
        raise SystemExit("amh: --email and --lines go together")
    print(work.start_task(config, args.task, Path(args.dir), Path(args.prompt), args.tool, args.model, args.effort, args.manager, args.as_manager, args.tmux, args.email, args.lines, args.proxy))
    return 0


def task_show(config: Config, args: argparse.Namespace) -> int:
    for name in args.task:
        task = taskfile.load(config, name)
        print(f"{task.name}\n" + "".join(f"  {key}: {value}\n" for key, value in task.fields.items()) + "".join(f"  - {item}\n" for item in task.items), end="")
    return 0


def task_close(config: Config, args: argparse.Namespace) -> int:
    print(f"closed {args.task} at {work.close_task(config, args.task, args.agent_gone, not args.no_email)}")
    return 0


def task_status(config: Config, args: argparse.Namespace) -> int:
    work.set_status(config, args.task, args.status, args.on)
    return 0


def task_note(config: Config, args: argparse.Namespace) -> int:
    with taskfile.locked(config):
        task = taskfile.load(config, args.task)
        taskfile.note(task, args.text)
        taskfile.save(config, task)
    return 0


def task_give(config: Config, args: argparse.Namespace) -> int:
    with taskfile.locked(config):
        task = taskfile.load(config, args.task)
        new = work.add_items(config, task, [args.item], args.from_human)
    work.notify_human(config, task, "created", new)
    print(f"added {len(new)} open item(s) to {args.task}")
    return 0


def task_move(config: Config, args: argparse.Namespace) -> int:
    with taskfile.locked(config):
        source, to = taskfile.load(config, args.source), taskfile.load(config, args.to)
        if args.item not in source.items or to.fields["status"] == "done" or args.source == args.to:
            raise SystemExit(f"amh: {args.source} has no such open item, or {args.to} is done or the same task")
        source.items.remove(args.item)
        to.items += [args.item] if args.item not in to.items else []
        taskfile.save(config, to)
        taskfile.save(config, source)
    return 0


def task_check(config: Config, _args: argparse.Namespace) -> int:
    findings = work.check(config)
    print("\n".join(findings), end="\n" if findings else "")
    return 1 if findings else 0


def agent_problems(config: Config, _args: argparse.Namespace) -> int:
    found = work.problems(config)
    for task, problem, evidence in found:
        print(f"{task.name} at {task.address}: {problem} ({evidence})")
    return 3 if found else 0


def agent_list(config: Config, _args: argparse.Namespace) -> int:
    for task in taskfile.active_tasks(config):
        state, evidence = ("retired", "") if task.address == "retired" else agents.status(config, task.address)
        print(f"{state}: {task.name} [{task.fields['status']}] {task.address} {evidence}")
    return 0


def agent_tree(config: Config, _args: argparse.Namespace) -> int:
    print(work.tree(config))
    return 0


def agent_status(config: Config, args: argparse.Namespace) -> int:
    print(": ".join(agents.status(config, address(config, args.who))))
    return 0


def agent_stop(config: Config, args: argparse.Namespace) -> int:
    agents.stop(config, address(config, args.who))
    return 0


def agent_compact(config: Config, args: argparse.Namespace) -> int:
    agents.send(config, address(config, args.who), "/compact")
    return 0


def manager_watchers(_config: Config, _args: argparse.Namespace) -> int:
    restarted = subprocess.run(["systemctl", "--user", "restart", UNIT], check=False).returncode
    return restarted or subprocess.run(["systemctl", "--user", "--no-pager", "--lines=3", "status", UNIT], check=False).returncode


def manager_rotate(config: Config, args: argparse.Namespace) -> int:
    print(work.rotate(config, args.who, args.tool, args.model, args.effort))
    return 0


def manager_worktree(config: Config, _args: argparse.Namespace) -> int:
    for repo in (Path.home() / ".config", config.root):
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=False).stdout
        print(f"{repo}:\n{dirty}" if dirty else f"{repo}: clean")
    return 0


def mail_unread(config: Config, args: argparse.Namespace) -> int:
    for header in mail.unread(config, Path(taskfile.own_task(config, args.task_file).name).stem, []):
        print(f"{header.message_id} {header.subject!r} {header.date}")
    return 0


# 🧑 "Move those to the relevant and optional help pages for working with tmux in the CLI that does not show normally but do show when given specific command/flag when agents want to use tmux"
TOPICS = {"tmux": Path(__file__).with_name("tmux.md")}


def build_parser() -> argparse.ArgumentParser:
    plain = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(prog="amh", description=__doc__, formatter_class=plain, epilog="Agents in tmux instead of Omnigent: `amh help tmux`.")
    groups = parser.add_subparsers(dest="group", required=True, metavar="GROUP")

    def group(name: str, summary: str) -> argparse._SubParsersAction[argparse.ArgumentParser]:
        sub = groups.add_parser(name, help=summary, description=summary, formatter_class=plain)
        return sub.add_subparsers(dest="action", required=True, metavar="ACTION")

    def action(parent: argparse._SubParsersAction[argparse.ArgumentParser], name: str, summary: str, run: Run, more: str = "", own: bool = False) -> argparse.ArgumentParser:
        p = parent.add_parser(name, help=summary, description=f"{summary}\n\n{more}".strip(), formatter_class=plain)
        p.set_defaults(run=run)
        if own:
            _ = p.add_argument("--task-file", metavar="NAME.md", help="your task file, when `amh` cannot tell who you are")
        return p

    tell = group("tell", "Send a message to your manager, the human, an agent you manage, or the guest.")
    p = action(tell, "manager", "Message your manager, like a chat message.", tell_manager, f"Use it only to ask for help or coordination, or to say you are done or blocked.\nAt most {work.MANAGER_MESSAGE_MAX_CHARS} characters. For details, write a file and name its path in the message.", own=True)
    _ = p.add_argument("text", metavar="TEXT", help="what you need from the manager, or what changed, in one or two sentences")
    state = p.add_mutually_exclusive_group()
    _ = state.add_argument("--blocked", action="store_true", help="you cannot continue without the manager")
    _ = state.add_argument("--done", action="store_true", help="your task is finished")
    p = action(tell, "human", "Email the human.", tell_human, "The body comes from --file, or from standard input when --file is omitted.\nReuse the subject of the thread you are answering; the tag of your task is added for you.", own=True)
    _ = p.add_argument("--subject", required=True, help="plain English, under 60 characters")
    _ = p.add_argument("--file", help="file holding the email body")
    _ = p.add_argument("--replaces", metavar="MESSAGE_ID", action="append", default=[], help="an earlier email of yours that this one replaces; if the human has not read it, it goes to the trash; repeat as needed")
    p = action(tell, "agent", "Message an agent you manage.", tell_agent)
    _ = p.add_argument("who", metavar="WHO", help="its task file name, or its address")
    _ = p.add_argument("text", metavar="TEXT", nargs="?", help="the message")
    _ = p.add_argument("--file", help="file holding the message, instead of TEXT")

    p = action(tell, "guest", "The guest agent only: answer a guest email; it goes to the guest alone.", tell_guest)
    _ = p.add_argument("--mail", required=True, metavar="FILE.txt", help="name of the stored guest email being answered")
    _ = p.add_argument("--file", required=True, help="file holding the answer")
    _ = p.add_argument("--image", action="append", default=[], metavar="PATH", help="png, jpeg, gif, or webp image to attach; repeat as needed")

    todo = group("todo", "Read and update your own list of open work.")
    _ = action(todo, "list", "Show your open work.", todo_list, own=True)
    p = action(todo, "add", "Add one open item.", todo_add, "Adding the human's request emails the human that it was created.", own=True)
    _ = p.add_argument("item", metavar="ITEM")
    _ = p.add_argument("--from-human", action="store_true", help="the item is the human's request, in the human's words")
    p = action(todo, "done", "Remove one finished or cancelled item.", todo_done, "Removing a 🧑 item emails the human that it was deleted.", own=True)
    _ = p.add_argument("item", metavar="ITEM", help="the exact item text as `amh todo list` shows it, including a leading 🧑 mark")
    _ = p.add_argument("--evidence", required=True, help="one sentence saying how you know it is finished, or why it was cancelled")
    p = action(todo, "replace", "Reword one open item.", todo_replace, own=True)
    _ = p.add_argument("old", metavar="OLD")
    _ = p.add_argument("new", metavar="NEW")

    task = group("task", "Managers: create, inspect, and close the tasks of agents you manage.")
    p = action(task, "start", "Create a task and start an agent on it.", task_start, "Prints the new agent's address. The agent gets the standing instructions, then your prompt, then the quoted human email lines.")
    _ = p.add_argument("task", metavar="TASK.md", help="new task file name; also the agent's tag in email subjects")
    _ = p.add_argument("--dir", required=True, help="directory the agent works in")
    _ = p.add_argument("--prompt", required=True, metavar="FILE", help="file holding the goal: one goal line, then sub-goals as bullets")
    _ = p.add_argument("--tool", choices=agents.TOOLS, help="default: claude for managers, codex for workers")
    _ = p.add_argument("--model", help="default: the tool's usual model")
    _ = p.add_argument("--effort", help="reasoning effort; default: the tool's usual effort")
    _ = p.add_argument("--manager", metavar="ADDRESS", help="who the agent reports to; default: you")
    _ = p.add_argument("--as-manager", action="store_true", help="the new agent is itself a manager")
    _ = p.add_argument("--tmux", metavar="SESSION", help="run Codex in a new window of this tmux session instead of on Omnigent")
    _ = p.add_argument("--proxy", metavar="URL", help="with --tmux: make Codex talk to this local proxy, e.g. http://localhost:18181/backend-api/codex, instead of using its saved login")
    _ = p.add_argument("--email", metavar="FILE", help="stored human email that caused this task; needs --lines")
    _ = p.add_argument("--lines", metavar="START-END", help="the relevant lines of --email")
    p = action(task, "show", "Show tasks' status, agent address, and open items.", task_show)
    _ = p.add_argument("task", metavar="TASK.md", nargs="+")
    p = action(task, "close", "Mark a task done, stop its agent, and move it to `previous` in the task list.", task_close, "Refuses while the task has open items. Emails the human one line saying the task is closed.")
    _ = p.add_argument("task", metavar="TASK.md")
    _ = p.add_argument("--agent-gone", action="store_true", help="close even though the agent cannot be stopped because it no longer exists")
    _ = p.add_argument("--no-email", action="store_true", help="do not email the human; for test tasks only")
    p = action(task, "status", "Set a task's status.", task_status)
    _ = p.add_argument("task", metavar="TASK.md")
    _ = p.add_argument("status", choices=("running", "long_running", "blocked"))
    _ = p.add_argument("--on", metavar="WHAT", help="what a blocked task waits for: `human`, or another task file")
    p = action(task, "note", "Append a manager note to a task file.", task_note)
    _ = p.add_argument("task", metavar="TASK.md")
    _ = p.add_argument("text", metavar="TEXT")
    p = action(task, "give", "Add one open item to a task's list.", task_give, "Giving the human's request emails the human that it was created.")
    _ = p.add_argument("task", metavar="TASK.md")
    _ = p.add_argument("item", metavar="ITEM")
    _ = p.add_argument("--from-human", action="store_true", help="the item is the human's request, in the human's words")
    p = action(task, "move", "Move one open item from one task to another.", task_move)
    _ = p.add_argument("source", metavar="FROM.md")
    _ = p.add_argument("to", metavar="TO.md")
    _ = p.add_argument("item", metavar="ITEM")
    _ = action(task, "check", "Check that task files and the task list agree.", task_check)

    agent = group("agent", "Managers: look at and control running agents.")
    _ = action(agent, "problems", "List agents that are gone, failed, or idle with open items.", agent_problems)
    _ = action(agent, "list", "List active tasks and what their agents are doing.", agent_list)
    _ = action(agent, "tree", "Show who reports to whom, with each agent's open items.", agent_tree)
    for name, summary, run in (
        ("status", "Show whether one agent is running, idle (`ready`), failed, or missing.", agent_status),
        ("stop", "Stop one agent without closing its task.", agent_stop),
        ("compact", "Tell an agent to compact its context by sending it `/compact`.", agent_compact),
    ):
        p = action(agent, name, summary, run)
        _ = p.add_argument("who", metavar="WHO", help="its task file name, or its address")

    mail_group = group("mail", "Look at the emails you sent the human.")
    _ = action(mail_group, "unread", "List your emails the human has not read yet.", mail_unread, "To drop one, send its replacement with `amh tell human --replaces MESSAGE_ID`.", own=True)

    manager = group("manager", "Main manager: the watcher, replacing a manager, and repository checks.")
    _ = action(manager, "watchers", "Restart the background watcher that takes in email and delivers pending work.", manager_watchers)
    p = action(manager, "rotate", "Replace a manager with a fresh one that takes over its task and reports.", manager_rotate)
    _ = p.add_argument("who", metavar="TASK.md", help="the manager's task file")
    _ = p.add_argument("--tool", choices=agents.TOOLS, help="default: the same tool")
    _ = p.add_argument("--model", help="default: the same model")
    _ = p.add_argument("--effort", help="default: the same effort")
    _ = action(manager, "worktree", "List uncommitted changes in manager-owned repositories.", manager_worktree)
    return parser


def main() -> int:
    if sys.argv[1:2] == ["help"] and sys.argv[2:] in ([topic] for topic in TOPICS):
        print(TOPICS[sys.argv[2]].read_text(encoding="utf-8"), end="")
        return 0
    args = build_parser().parse_args()
    try:
        return args.run(configuration.load(), args)
    except agents.AgentError as error:
        raise SystemExit(f"amh: {error}") from error


if __name__ == "__main__":
    sys.exit(main())
