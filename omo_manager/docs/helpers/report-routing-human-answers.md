# report routing human answers

- `seen`
  - process-local dict in `omo_pending_watch.py`
  - not global across watcher restarts
  - entries expire by time using `OMO_MANAGER_SEEN_TTL_S`, default 24 hours
  - no state file is written

- pending watcher restart
  - `omo_manager_setup_watchers.sh` starts a supervisor loop
  - the watcher continuously watches while healthy
  - the 5 second restart delay only happens after the watcher process exits

- unstick attempts
  - status checks may send Enter for non-blocked stuck input when `omo_agent_status.py --problems-only` runs
  - the pending watcher remembers per-target Enter attempts in `seen`
  - it suppresses reports until 3 failed attempts
  - it clears those attempt records when the target is no longer reported stuck or when status returns clean

- digest messages
  - digest keys are SHA-256 prefixes used to throttle duplicate watcher reports
  - pending delivery also includes readable content snippets
  - `manager_digest.md` is a separate queued digest delivered after idle mail time

- referenced status helper code
  - the old line-number reference has drifted
  - the referenced block is `session_records()`
  - it builds `SessionRecord` values from registry fields
  - `tmux_target` becomes the tmux pane target used for later inspection
  - `port` remains only the optional server port
  - this registry parsing does not choose a manager route

- `--manager-target`
  - unnecessary for `omo_report.sh`
  - report routing reads the worker task file, uses `managerat`, and writes the pending block into the manager task file
  - still used by pending/status delivery as a live pane routing filter
  - broader pruning, common-module extraction, and uv wrapper work should be separate from the urgent report route fix
