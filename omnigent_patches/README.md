# native message submission

(authored by agents unless marked 🧑)

- 🧑 "don’t need to check if the pasted message renders for any harness and just send them"
- `omnigent-no-message-render-gate.patch`
  - applies to upstream stock `v0.16.0` package sources
  - removes pasted-draft render polling from Antigravity, Claude, Cursor, Goose, Hermes, Kimi, Kiro, and Devin message submission
  - each normal paste settles briefly and sends one Enter
  - Codex native uses the app-server rather than rendered input
  - pending user decisions, cancellation, process liveness, and explicit backend rejection remain separate checks
  - Antigravity retries an explicit account-verification rejection; Claude retries an explicit unknown-command rejection as escaped text
  - Hermes retains its separate database-record confirmation and single retry if no new record appears; this is not draft-render verification
  - Devin's normal message path does not flush a visible queue with additional Enters
  - apply from the installed package's parent directory: `patch -p1 --forward < PATCH_FILE`
  - an already applied patch passes `patch -p1 --reverse --dry-run < PATCH_FILE`
- activation
  - disk changes apply to fresh imported bridge modules, not already running inner harness subprocesses
  - the zygote refuses source graphs changed after import; fresh launches fall back to a fresh interpreter
  - existing user sessions are not restarted by the patch
  - Wix's owner decides whether to replace that session; config must not reload or restart it
- current helper
  - `amh/amh/agents.py` submits tmux messages once without post-paste capture polling
  - OmniGent events must receive `queued: true`; that receipt means queued, not agent response completed
- validation
  - run the active host virtual environment's Python with `-m unittest discover -s omnigent_patches -p test_no_render_delivery.py`
  - real disposable private tmux TUIs consume input without echoing it; all eight public bridge entry points and the current `amh` sender must submit exactly once
  - Hermes confirmation uses a real SQLite message row; Devin keeps its visible queue without additional submits
  - the log prints installed package version and every imported bridge file path
  - private tmux sockets prevent interference with user sessions
