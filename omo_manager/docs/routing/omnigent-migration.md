# gradual OmniGent migration

authority

- `manager_mail/85c5dff58359-1513.txt:3-8`
- “move to Omnigent bit by bit”
- “add Omnigent session as a valid runat while keeping backwards compatibility for tmux somehow”

goal

- keep current task records as lifecycle authority
- replace one runtime-facing capability at a time
- retain tmux until each OmniGent adapter has equivalent evidence and recovery

target contract

- tmux `SESSION:WINDOW[.PANE]`
  - unchanged
- OmniGent `omnigent://SESSION_ID`
  - session id uses ASCII letters, digits, `.`, `_`, or `-`
  - URI-like syntax cannot overlap tmux syntax
  - `tool` names the real harness, currently `codex` or `cursor`
  - example: `omnigent:123` remains tmux
  - example: `omnigent://019f0000-0000-7000-8000-000000000123` is OmniGent
- `managerat`
  - remains tmux-only in this slice

first migration step

- opt in one task at launch with `omo_task.py --omnigent --tool codex ...`
- create its session through OmniGent's `/v1/sessions` API on the selected online host
- store the returned durable session as `runat: omnigent://SESSION_ID`
- send prompts and later messages through `/v1/sessions/SESSION_ID/events`
- inspect the session snapshot through `/v1/sessions/SESSION_ID`
- stop one session with the `stop_session` event when its task is completed
- update normal task statuses with the existing task-file and TODO transactions
- do not auto-unstick OmniGent sessions
- leave every tmux launch, delivery, status, and stop path unchanged

current boundary

- `managerat` remains tmux-only
- launch supports the installed native `codex` and `cursor` harnesses
- the server URL, bearer token, and explicit host may be configured with `OMO_MANAGER_OMNIGENT_URL`, `OMO_MANAGER_OMNIGENT_TOKEN`, and `OMO_MANAGER_OMNIGENT_HOST_ID`
- task completion stops but does not delete OmniGent history
