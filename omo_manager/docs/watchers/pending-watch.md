# pending watcher

- purpose
  - deliver new Markdown `(pending)` markers to the manager with file-line refs
  - keep seen-state durable so watcher restarts do not redeliver the same marker
  - run agent-problem and digest maintenance without delaying marker scans

- file-change path
  - Linux uses recursive inotify watches on the work-log root
  - `.git`, `.venv`, and `__pycache__` dirs are ignored
  - Markdown file events enqueue only changed files for marker parsing
  - directory create, move, watch removal, unmount, or queue overflow forces a full Markdown scan
  - a filesystem notification resets the mtime-poll backstop even when no Markdown file changed
  - platforms without inotify use the older mtime scan fallback

- safety scans
  - startup scans all Markdown files
  - periodic full scans remain controlled by `--full-scan-interval-s`
  - full scans refresh the fallback mtime snapshot
  - while inotify is active, `--poll-backstop-interval-s` runs an mtime scan after 30 seconds without a filesystem notification, full scan, or previous backstop poll

- subprocess isolation
  - pending dispatch uses the existing `omo_push_to_manager.py` path
  - agent-problem checks run as background child processes and are polled
  - digest delivery runs as a background child process and is polled
  - timeout handling kills overdue maintenance children and logs stderr

- assumptions
  - production manager hosts are Linux and support inotify
  - a 30-second mtime backstop is cheap enough for normal work-log roots
  - a periodic full scan is still required as broader recovery
  - maintenance command output remains small enough for captured pipes

- pending ref semantics
  - scans Markdown for literal `(pending)` markers outside fenced code
  - inspects each pending block for explicit source markers
  - sends manager refs as `pending: file=... line=... origin=human|agent source=email|manual|agent action=ack-human|no-human-ack`
  - email source markers are `origin=human source=email`
  - explicit agent source markers anywhere in the same pending block are `origin=agent source=agent`
  - unmarked pending blocks are `origin=human source=manual` because prompts appended to `work_manager*.md` are human-origin unless explicitly marked otherwise
  - human-origin refs require manager email acknowledgement

- agent-problem routing
  - runs `omo_agent_status.py --problems-only` every `--agent-problem-interval-s` seconds, default `300`
  - detects task files still marked `(running)` whose pane is `error`, `not_codex`, `ready`, or `stuck_input`
  - detects blocked persistent-role task files whose pane is `error`, `not_codex`, or `stuck_input`
  - detects manager pane problems when `--manager-target` is set
  - detects completed task files that still have stale registry rows
  - agent-problem prompts include an `origin=agent` source marker so any manager-written pending follow-up block is `action=no-human-ack`
  - email pending refs remain `origin=human source=email action=ack-human`

- scoped maintenance
  - when a current `vl_submanager_current_*` or `vl_supervisor_current_*` task exists, the root watcher also runs a VL-owned problem pass and sends only VL-scoped problem rows to that submanager target
  - panes classified as `stuck_input` are submitted with Enter, including the manager target and visible Codex turns
  - manager self-problem rows and matching `unstuck:` rows are logged and filtered by the watcher so they are not pasted back into the manager prompt
  - identical problem output is keyed by SHA-256 in the pending seen state and is repeated at most once per `--agent-problem-repeat-s` seconds, default `1800`
  - digest idle delivery uses a separate human-contact clock: if `manager_digest.md` has content and the newest `manager_mail/*.txt` is at least `--digest-idle-after-s` seconds old, default `3600`, it runs `scripts/manager-digest deliver`

Human-review note: this one-hour no-human-email digest policy was added after mail `manager_mail/5125.txt`; review whether future manager instructions should make this a standing obligation or keep it as helper behavior.

Human-review note: running-agent reminders are now problem-only; review standing manager instructions if they still expect periodic healthy running-agent reminders.
