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
  - `tool` names the real harness, currently `antigravity`, `codex`, or `cursor`
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

- `omo_report.sh` supports tmux producers and native Codex, Antigravity, or Cursor OmniGent producers
- OmniGent authentication binds the current native bridge state, live native process, workspace, thread, and online session snapshot
- submission rechecks that identity before acceptance
- archived consumed-report verification trusts the immutable commitment identity and does not require the retired producer process
- tmux producer inference and routing are unchanged

current boundary

- `managerat` remains tmux-only
- launch supports the installed native `antigravity`, `codex`, and `cursor` harnesses
  - Antigravity launch looks up the registered `antigravity-native-ui` agent with `antigravity-native` harness
  - Antigravity reasoning effort is `low`, `medium`, or `high`
  - Antigravity has no Codex bypass flag
  - local Antigravity launches pass `--dangerously-skip-permissions`; the native agent spec uses `caller_process`, `cwd: .`, and `sandbox.type: none`
  - the global Antigravity permission settings are copied into each per-session Gemini directory before launch, preserving OmniGent's session-specific MCP configuration
  - Cursor launch looks up the registered `cursor-native-ui` agent with `cursor-native` harness
  - Cursor `model_override` is the same concatenated CLI id tmux uses (`MODEL-EFFORT` when effort is set)
  - Cursor `terminal_launch_args` are the same as tmux Cursor Agent: `--force --sandbox disabled --trust`
  - model ids are passed through; they are not checked per harness
- native producer authentication currently requires Linux process metadata under `/proc` and the matching OmniGent native bridge (`codex-native`, `antigravity-native`, or `cursor-native`)
  - Antigravity may bind the unique on-disk conversation UUID while OmniGent still has a placeholder `external_session_id`
  - Cursor binds `tmux.json` plus the unique chat id from `cursor_forwarder.json` or `external_session_id`; the live TUI bridge lives under `/tmp/omnigent-<uid>/cursor-native/`
- `--tool antigravity` selects OmniGent even without `--omnigent`; Cursor and Codex remain tmux unless `--omnigent` is set
- pending-watch readiness for OmniGent `runat` uses the session snapshot, not tmux inspect; send still uses the shared helper
- ready-report for OmniGent uses the newest completed assistant session item instead of a tmux transcript
  - same-host Antigravity falls back to its OmniGent-advertised terminal when its RPC mirror cannot read the current CLI version
  - the fallback binds the first TUI-created conversation, detects the ready prompt, and hashes only the last completed turn
- pending-item reminders use that same ready-report guard as tmux, so an unreported OmniGent turn is not also nagged about its queue
- Human mail treats a live OmniGent producer like a tmux producer: `email_me.py` infers `omnigent://SESSION_ID` from native identity when there is no pane, and Cursor conversation UUID is a valid agent session id
- the loopback server and host are the long-lived Source-1957 replacement (`omo_omnigent_server` on `127.0.0.1:6767`) plus `omnigent.host._daemon_entry`
  - OmniGent's `_ensure_host_daemon` starts a host, and starts a local server only when no `--server` is given
  - `omo_task.py --omnigent` reuses that existing loopback owner and does not bind a second `6767`
  - host tunnels multiplex runner keepalive `ping`/`pong` with `host.*`; the fence admits those keepalives
  - launch waits briefly for the sole host through a tunnel drop instead of failing on the first offline snapshot
  - user unit `omo-omnigent-server.service` starts that fenced argv from `~/.omnigent/source1957-packet` with linger and `Restart=always`; it does not bind a second vanilla `6767`
  - `omo-omnigent-host.service` waits on a live `host.pid` `_daemon_entry` or starts one `--server http://127.0.0.1:6767`; it does not start a second host
  - `omo-omnigent-packet-keep.timer` touches the durable packet directory
- the server URL, bearer token, and explicit host may be configured with `OMO_MANAGER_OMNIGENT_URL`, `OMO_MANAGER_OMNIGENT_TOKEN`, and `OMO_MANAGER_OMNIGENT_HOST_ID`
- task completion stops but does not delete OmniGent history
