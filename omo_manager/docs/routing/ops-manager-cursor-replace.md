# ops manager Codex-to-Cursor replacement

`omo_ops_manager_cursor_replace.py` is the only supported helper that may replace the operations manager from Codex to Cursor. It is pinned to `ops_manager.md` at `wl:3` and does not launch a replacement pane.

It acts only when all of these remain true through the replacement boundary:

- caller confirmation matches the pinned task, target, and human source: `--task-file ops_manager.md`, `--target wl:3` or `wl:3.0`, `--authority-file manager_mail/85c5dff58359-741.txt`, `--authority-lines 17-17`
- those selected source lines strip to exactly `Replace wl:3 with Cursor`; the source is one owner-private file under owner-private `ROOT/manager_mail`
- `ops_manager.md` is the unique active `runat` claimant for `wl:3`, is a Codex manager, and still has `managerat: wl:18`
- child tasks that already report to `wl:3` are snapshotted and never written
- the pending queue is unchanged; a live `(pending)` marker fails closed
- `wl:3` is an exact live non-`h*` Codex pane whose process, window, and work-log working directory have not drifted
- the helper is invoked from a different pane than `wl:3`
- tracked dirty work-log files either are `ops_manager.md`, have parseable ownership that is not a competing `wl:3` runat, or the helper fails on dirty unknown state

The helper then respawns that same pane into Cursor Agent with `cursor-grok-4.6-xhigh`, worker defaults, `MANAGER.md`, and a continuation prompt. After Cursor is `running` or `ready` with a new pane PID, it changes only frontmatter `tool` to `cursor`.

`--dry-run` reruns the non-mutating gates, including the fresh pre-action revalidation. It does not respawn `wl:3` or edit the task record.

```bash
~/.config/omo_manager/omo_ops_manager_cursor_replace.py \
  --root "$OMO_WORK_LOGS_ROOT" \
  --task-file ops_manager.md \
  --target wl:3 \
  --authority-file manager_mail/85c5dff58359-741.txt \
  --authority-lines 17-17 \
  --dry-run
```

Omit `--dry-run` only when a manager is actually performing this one replacement. A post-respawn failure is reported as `completion-unknown` and is not retried by the helper.
