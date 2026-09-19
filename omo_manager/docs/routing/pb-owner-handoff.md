# PB owner handoff

(authored by agents unless marked 🧑)

purpose:

- rebind the retained live PB service after fleet consolidation
  - predecessor: `pb_news_live.md`, `pb:0`, manager `pb:1`
  - successor: `news_service.md`, `pb:0`, manager `wl:1`
- do not signal or restart the worker, loop, browser, CDP, database, or queue
- implement only the exact Human-authorized Source-1982 consolidation
  - authority bytes: `manager_mail/85c5dff58359-1982.txt`
  - SHA-256: `7b57f157958a7fccb57c9b31351a5660b13cc920865a0b898f35ff5cca30c32e`
  - the exact authoritative envelope must occur once in `news_service.md`

helper:

- `omo_manager/omo_pb_owner_handoff.py`
- `describe`
  - proves exact pushed PB commit `f9893b5eeb4cefa74d147d0642915db251b3bc6d`
  - returns task, TODO, environment, worker, loop, and Codex-session bindings
- `execute`
  - requires every returned binding and an absolute owner-private audit path
  - accepts only the exact predecessor and successor records
  - accepts the successor only while exactly blocked on `watcher_repair.md`
  - restores it to `long_running` while preserving its body, ordered queue, session, manager, and target

serialization:

- acquire locks in this order
  1. PB `manager-report-handoff.lock`
  2. `pb:0` tmux-input lock
  3. work-log membership lock
  4. `pb:0` target lock
  5. sorted locks for both tasks, TODO, authority, environment, and audit
- the first lock excludes PB database claims and prompt handoffs
- the second excludes supported pane input and lifecycle replacement

transaction:

- reserve a canonical audit before mutation
- mark the empty blocked predecessor `done`
  - preserve its historical `pb:0` target
  - append the fleet-consolidation handoff note
- move its sole TODO row from `current` to `previous`
- change `news_service.md` from exact `blocked`/`watcher_repair.md` to `long_running`
- change only `PB_WATCHER_AGENT_TASK_FILE` to `news_service.md`
- verify the successor is the sole active `pb:0` owner
- durably record forward-only recovery intent
- run the project owner preflight
  - require exit zero, empty stderr, and exact stdout `agent_owner=pb:0.0`
- verify worker process, Codex session, and loop process again
- treat the validated final binding as the commit boundary
- durably record the validated commit, then complete the audit
- mark the audit complete
- fsync each published file and its parent directory before the next phase

session proof:

- bind the stable `pb:0` process tree twice
- inspect process-held rollout inodes under the Codex session root
- require exactly one top-level `codex-tui`/`cli`/`user` rollout
- require its metadata id, filename, and working directory to match the successor

failure behavior:

- every intermediate state makes the project owner preflight reject work
- before recovery intent, an in-process failure restores the exact input bytes in reverse order
- after recovery intent, validation or audit failure never rolls back ownership
  - rerun the same command to revalidate or finish its audit forward
- a crash before recovery intent leaves a prepared audit
  - rerunning the same digest-bound command accepts only a monotonic transaction prefix
- a crash after recovery intent leaves final images and a forward-only audit
- drift, another owner, a changed process, a changed session, or a noncanonical audit fails closed
