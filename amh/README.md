# amh: agent manager helper

(authored by agents unless marked 🧑)

one program for agents and managers; usage lives in `amh --help`, not here
- 🧑 "Integrate them into a single program. Later we will rewrite it in Rust."
- Python 3.10 standard library only
- entry `bin/amh`; watcher `python3 -m amh.watch` under `amh-watch.service`
- `bin/use-amh`: stub that old command names link to
- test: `/usr/bin/python3 test_amh.py`; prints only failures
    - runs the real CLI entry and watcher steps on a temporary copy of the root's `*.md`
    - fakes: Omnigent API, tmux, IMAP, SMTP; settings from a temporary `local.env`, so no real agent or mailbox is touched
    - run it after every change; the program is live as soon as a file is saved

records, all under the work-log root (`OMO_WORK_LOGS_ROOT`)
- task file `NAME.md`
    - frontmatter: `status` `runat` `managerat` `is_manager` `tool` `pending_task_items`
        - `runat`: agent address, `omnigent://ID` or tmux `SESSION:WINDOW`, or `retired`
        - `status`: `running` `long_running` `blocked` (+ `blocked_on`) `done`
        - item starting `🧑 ` = the human's request
    - body: free text; whole-line `(notes)`; `(pending)` blocks
- `TODO.md`: sections `current:` `human pending:` `low priority:` `previous:`, rows `NAME.md ADDRESS`
- `manager_mail/PREFIX-UID.txt`: stored human email, `Subject:` line then body
- name of a task file without `.md` = tag in email subjects `[tag]`

modules
- `config`: reads `local.env` (`export K="V"`, `$VAR` expanded); the environment overrides it; `OMO_MANAGER_LOCAL_ENV` names another file
- `taskfile`: parse/render task files without a YAML library; one lock file serializes every writer; atomic replace
- `agents`: send, status, stop, launch for Omnigent (HTTP) and tmux
    - `own_address`: the caller's Omnigent session variable, else its tmux pane
    - multi-line message to Omnigent is followed by one typed line
        - Claude Code treats multi-line input as pasted text and does not act on it alone
    - tmux commands drop `TMUX` so they reach the user's default server
- `mail`: send on the tag's thread (agent Gmail account), list/trash the human's unread (human mailbox via himalaya config), fetch unread mail from one sender
    - `compose` + `deliver` build and send every outgoing email, to the human and to the guest
- `work`: what each action does; `problems`, `check`, `tree`, `rotate`
    - `rotate` of the main manager rewrites its address in `local.env`
- `watch`: loop every 5 s
    - mail intake: store, route, append `(pending)` + source line, mark read
        - route: `[tag]` task; `for manager` at body edge -> its manager; done task -> its manager; else main manager
        - stored file exists = already taken in
    - delivery: each `(pending)` block -> the task's agent; then the marker line is deleted, the source line stays
        - a block in a task with no agent (done or retired) goes to that task's manager, else the main manager
        - an idle Omnigent session whose runner is offline is reachable: the next message brings the runner back
        - stored email is inlined verbatim in `<human_instruction>`
        - failed target retried after 10 min
    - nudges: idle agent with open items is reminded at most every 30 min
    - agent problems (`missing`, `error`): each is reported once, naming the task file, tool, address, manager, and the harness error
        - to the task's manager; for `error` also by email to the human on the task's thread
        - reported keys live in `amh-problem-notices.txt`; a problem that goes away is forgotten, so a recurrence is reported again
        - nothing is reported while Omnigent is unreachable
- `guest`: guest mailbox; mail from `AMH_GUEST_ADDRESS` with SPF pass is stored under `guest_hees_manager_mail/`, a dedicated agent (task `guest_hees.md`) is started or reused, and it answers with `amh tell guest`
- `cli`: argparse tree; `amh help tmux` prints `amh/tmux.md`

dropped on purpose from the old scripts
- report envelopes/receipts: `tell manager` posts straight to the manager
- recovery modes, v2 task format, audit watcher, mail compression
- `task record`, `mail trash-replaced`: `task give --from-human`, `tell human --replaces`
