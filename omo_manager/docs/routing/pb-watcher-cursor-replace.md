# PB watcher Cursor replacement

`omo_pb_watcher_cursor_replace.py` replaces only the Cursor process for
`202607/pbw_interpreter_live.md` at `pb-newswatcher-agent:0.0`. It does not
create a pane, alter task/TODO bytes, send the Gmail instruction, read mail, or
touch browser pages.

The helper authenticates these facts as one binding:

- exact Human authorization is read from owner-private
  `manager_mail/85c5dff58359-1468.txt:3-7`, hashed, and revalidated immediately
  before replacement;
- the task is the sole active owner of the pane, remains `long_running`, reports
  to `pb:13`, and retains its complete ordered queue;
- the task contains exactly one live Gmail pending marker and TODO contains
  exactly one matching owner row;
- the target's detached tmux session, window, pane, pane PID/start time,
  workdir, and one Cursor process are exact; the entire pane process subtree
  must be only the shell-to-Cursor ancestry, with no sibling or child work;
- the retained submitted composer defect is still present and there is no
  follow-up overlay;
- Cursor, tmux, shell, model, and sanitized process-environment bytes are
  unchanged, and the launch mode is pinned to fresh with no startup prompt;
- `pb_watcher.env`, the SQLite database and sidecar presence, the four browser
  tmux panes, the local CDP browser identity and page inventory, and the
  existing watch-loop presence are unchanged. Mail is preserved by
  non-interference: the helper has no mailbox or mail-sender operation.

First obtain fresh evidence without changing runtime state:

```bash
omo_pb_watcher_cursor_replace.py \
  --root /ssd1/sichangheagent/work_logs \
  --task-file 202607/pbw_interpreter_live.md \
  --target pb-newswatcher-agent:0.0 \
  --describe
```

`--describe` emits one parseable JSON object. Inspect it, create a new
owner-private audit directory, and run once with
the printed `binding_sha256`:

```bash
omo_pb_watcher_cursor_replace.py \
  --root /ssd1/sichangheagent/work_logs \
  --task-file 202607/pbw_interpreter_live.md \
  --target pb-newswatcher-agent:0.0 \
  --expected-binding-sha256 SHA256_FROM_DESCRIBE \
  --state-dir OWNER_PRIVATE_STATE_DIR \
  --audit-output OWNER_PRIVATE_STATE_DIR/pb-watcher-replacement.audit
```

The execution revalidates the full binding while holding the lifecycle locks
and the same per-target input lock used by every supported tmux sender. It also
rejects a legacy sender that started before the lock was available. It then
reserves its audit, and asks tmux to respawn only the bound pane. The fresh
Cursor receives no startup prompt at all, so replacement cannot replay work or
initiate browser, database, Gmail, or mail activity. Success requires the same
tmux session/window/pane, a distinct authenticated Cursor process, exactly one
task owner, unchanged protected evidence, and a ready empty composer. The audit
is then committed. A failure after audit reservation is retained as `prepared`,
`respawn-attempted`, or `completion-unknown`, according to the last durable
step. Do not retry or create another worker. Reconcile the preserved audit
without respawning by hashing it and running:

```bash
omo_pb_watcher_cursor_replace.py \
  --root /ssd1/sichangheagent/work_logs \
  --task-file 202607/pbw_interpreter_live.md \
  --target pb-newswatcher-agent:0.0 \
  --state-dir OWNER_PRIVATE_STATE_DIR \
  --audit-output OWNER_PRIVATE_STATE_DIR/pb-watcher-replacement.audit \
  --reconcile-audit \
  --expected-audit-sha256 EXACT_AUDIT_SHA256
```

Reconciliation never respawns or sends input. If the pane still has the exact
authenticated old binding, it records `aborted-no-respawn`; this proves that no
replacement occurred and permits a fresh describe and transaction. Otherwise,
it commits only if the same pane contains one distinct exact replacement
process, its composer is empty, the old process is gone, and every
lifecycle/runtime/protected digest still matches. Any ambiguous or mismatched
state fails closed.

After a committed result, the existing `pb:15` recovery owner—not this helper—
may deliver its already-reviewed Gmail instruction with the normal exact
`omo_tmux_send.py --message-file ...` path. It must use a fresh empty-composer
description and preserve the task's one marker until delivery is accepted.
