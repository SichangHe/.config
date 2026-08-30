# Atomic manager replacement

`omo_manager_replace.py` closes one exact live failed non-human Codex manager, migrates every active child to one new target, and publishes one blocked successor. It never starts the successor. A later normal `omo_task.py` launch is deliberately separate, after this helper has proved that the successor is the sole active owner and that its target is absent.

The invocation binds:

- old manager, TODO, and every active child by SHA-256;
- the complete active-child set and each child's unchanged pending queue;
- old and new manager targets, parent manager, Codex session UUID, pane id, pane PID, and process start ticks;
- an owner-private Human source excerpt plus the digest of the one existing `<human_instruction authoritative="true">` block in its task envelope;
- an owner-private audit path, distinct preparer/reviewer identities, and any explicitly protected targets.

The selected authenticated Human lines must explicitly establish manager failure, non-execution, and replacement. A merely `running` record or arbitrary manager-mail text is insufficient. The successor stores only a digest-bound private source locator in its queue; it does not duplicate the private Human excerpt into ordinary task or TODO artifacts.

The helper takes the root-membership lock, both target locks, every current Markdown task lock, and locks for its audit/proof paths. It rejects stale membership or bytes, malformed ownership, duplicate owners, omitted or extra children, any live or owned successor target, and old/new targets in `h*` or the protected set. The old pane is stopped only through the guarded exact-pane close capability; every read and mutation is server-guarded by symbolic target, pane id, and pane PID, while process start ticks are rechecked around the interaction. A durable close proof is written before lifecycle bytes change.

Write order is old-manager closure, all child migrations, TODO migration, then successor publication. Existing files are replaced with Linux inode exchange, and successor publication uses atomic no-replace linking; a concurrent path rebind is preserved instead of overwritten or unlinked. The successor stays `blocked` with its inherited queue and no inherited session ID. Final proof requires unchanged Markdown membership apart from the successor, no active old-target owner or child, exactly the successor at the new target, the exact migrated child set, unchanged child queues, unchanged successor queue, and both panes absent.

The private audit embeds all before/after images and the exact close capability, and advances through `prepared`, `owner_stopped`, `mutating`, `proving`, then `committed`. An interruption can be retried with the identical invocation and audit path: the helper authenticates the audit, reconstructs canonical bytes from the bound Human source, proves the close, commits an already-complete state, or rolls a partial state back before continuing. Unknown bytes and incomplete rollback fail closed with durable evidence. Ordinary failures reverse transaction-owned writes in reverse order and record `rolled_back`; any incomplete reversal records `rollback_failed`. Concurrent unsupported edits to a not-yet-written task or TODO are preserved and cause fail-closed rollback. The old pane remains closed after rollback, as recorded by the immutable close proof.

Use `--child TASK.md=SHA256` once for every active child, in any order. Bind authority with `--authority-file`, `--authority-lines`, `--authority-sha256`, `--authority-envelope-task`, and the selected block's `--authority-envelope-sha256`; select the replacement directive with one or more contained `--successor-item-lines`. On the first run, `--audit-output` must name a nonexistent file in an owner-private directory. The same path is the recovery key after an interruption. The helper prints a success message only after the singular ownership proof; it does not print or run a launch command.

Focused verification:

```sh
uv run --project omo_manager --group dev python -m unittest \
  omo_manager.tests.test_manager_replace \
  omo_manager.tests.test_codex_stop
uv run --project omo_manager --group dev ruff check \
  omo_manager/omo_manager_replace.py \
  omo_manager/omo_codex_stop.py \
  omo_manager/tests/test_manager_replace.py \
  omo_manager/tests/test_codex_stop.py
```
