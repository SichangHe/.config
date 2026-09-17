# gradual OmniGent migration

(authored by agents unless marked 🧑)

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
  - installed OmniGent intentionally excludes this internal ingestion route from OpenAPI
  - the supported route returns `202` and a queued acknowledgement
- inspect the session snapshot through `/v1/sessions/SESSION_ID`
- report an idle session ready only when `runner_online` is exactly true; host liveness alone does not make it reachable
- stop one session with the `stop_session` event when its task is completed
- update normal task statuses with the existing task-file and TODO transactions
- do not auto-unstick OmniGent sessions
- leave every tmux launch, delivery, status, and stop path unchanged

Codex access

- an OmniGent Codex launch accepts the exact singleton `--codex-flag=--dangerously-bypass-approvals-and-sandbox`
  - the session request records OmniGent's native per-session bypass label
  - every other raw OmniGent Codex flag remains rejected
- this launch option does not change an already-running thread
  - the installed Codex settings API has no conditional-update or exclusive-owner operation
  - a live repair requiring concurrent-change rejection must remain blocked until that capability exists

private reports

- `omo_report.sh` supports both tmux and native OmniGent producers
- OmniGent authentication binds the current `CODEX_HOME` bridge state, native app-server ancestor, socket, workspace, thread, and online session snapshot
- submission rechecks that identity before acceptance
- archived consumed-report verification trusts the immutable commitment identity and does not require the retired producer process
- tmux producer inference and routing are unchanged

current boundary

- `managerat` remains tmux-only
- native producer authentication currently requires Linux process metadata under `/proc`
- launch supports the installed native `codex` and `cursor` harnesses
- the server URL, bearer token, and explicit host may be configured with `OMO_MANAGER_OMNIGENT_URL`, `OMO_MANAGER_OMNIGENT_TOKEN`, and `OMO_MANAGER_OMNIGENT_HOST_ID`
- task completion stops but does not delete OmniGent history
