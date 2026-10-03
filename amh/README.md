# amh: agent manager helper

(authored by agents unless marked 🧑)

one program for agents and managers; usage lives in `amh --help`, not here
- 🧑 "Integrate them into a single program. Later we will rewrite it in Rust."
- Python 3.10 standard library only
- entry `bin/amh`; watcher `python3 -m amh.watch` under `amh-watch.service`
- `bin/use-amh`: stub that old command names link to

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
- `config`: reads `local.env` (`export K="V"`); `own_address` from the Omnigent session variable, else the tmux pane
- `taskfile`: parse/render task files without a YAML library; one lock file serializes every writer; atomic replace
- `agents`: send, status, stop, launch for Omnigent (HTTP) and tmux
    - multi-line message to Omnigent is followed by one typed line
        - Claude Code treats multi-line input as pasted text and does not act on it alone
    - tmux commands drop `TMUX` so they reach the user's default server
- `mail`: send on the tag's thread (agent Gmail account), list/trash the human's unread (human mailbox via himalaya config), fetch the human's new mail
- `work`: what each action does; `problems`, `check`, `tree`, `rotate`
- `watch`: loop every 5 s
    - mail intake: store, route, append `(pending)` + source line, mark read
        - route: `[tag]` task; `for manager` at body edge -> its manager; done task -> its manager; else main manager
        - stored file exists = already taken in
    - delivery: each `(pending)` block -> the task's agent; then the marker line is deleted, the source line stays
        - stored email is inlined verbatim in `<human_instruction>`
        - failed target retried after 10 min
    - nudges, each at most every 30 min: idle agent with open items; managers told of missing/failed agents
- `cli`: argparse tree; `amh help tmux` prints `amh/tmux.md`

dropped on purpose from the old scripts
- report envelopes/receipts: `tell manager` posts straight to the manager
- recovery modes, v2 task format, guest mailbox, audit watcher, mail compression
- `task record`, `mail trash-replaced`: `task give --from-human`, `tell human --replaces`
