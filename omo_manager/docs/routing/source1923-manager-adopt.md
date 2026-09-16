# Source-1923 live manager adoption

(authored by agents unless marked 🧑)

`omo_source1923_manager_adopt.py` is a non-reusable transaction for the exact Source-1923 DeepWiki state. It retains the existing plain-Codex `dw_cleanup_mgr.md` process at `dw:33`; it never launches or resumes a successor.

Set `OMO_SOURCE1923_WORK_LOGS_ROOT` to the same exact absolute work-log root passed to `--root`; the helper refuses an unset, relative, different, or copied directory by checking the fixed Source-1923 repository device/inode identity.

The `prepare` command authenticates the fixed Source-1923 mail file, its exact lines 1–10, the authoritative envelope, both manager records, the historical shared-target record, root and archived TODO indexes, the complete active child/descendant tree, every ordered queue, and every supplied live pane/PID/start-tick binding. The stale `dw:0`, retained `dw:33`, and executor Codex sessions are bound separately. Preparation produces an owner-private packet and makes no lifecycle-file change.

The packet requires an independent canonical PASS review that binds the packet and helper digests. `execute` reacquires the root, target, and file locks; revalidates every byte, tree edge, queue, pane, process, and session; writes a private prepared audit; then closes only the exact `dw:0` process through the guarded Codex stop capability.

After the close proof is durable, one descriptor-bound exchange transaction:

- marks `new_dw_manager.md` done and moves its root TODO row to `previous`
- reparents `dw_cleanup_mgr.md` to `wl:1`
- preserves the successor queue and predecessor queue as ordered blocks, prepended by the authenticated Source-1923 delivery item
- reparents every other direct `dw:0` child to `dw:33` without changing nested ownership or any child queue
- changes only the historical record's `runat` to `retired`, preserving its blocked Human hold and body evidence byte-for-byte
- changes the matching archived index row from `dw:33` to `retired`

All mutations use precomputed before/after images. A failed exchange or final proof reverses every completed exchange before returning. Recorded parent-directory identities and retained descriptors bind interrupted recovery and committed-stage cleanup to the reviewed paths. A durable prepared audit and deterministic staging names permit an interrupted run to restore the exact before state before retrying. Unknown bytes, authority drift, extra or missing descendants, queue drift, pane/process/status/session drift, a third `dw:33` claimant, or concurrent path replacement fails closed.

Success requires `dw:0` and its pinned process absent, exactly one active `dw:33` owner, the complete direct-child union under that owner, every nested queue unchanged, every retained live pane unchanged, and a committed private audit. Human email delivery remains successor work and must be verified separately after the transaction.

Focused verification:

```sh
uv run --project omo_manager python -m unittest \
  omo_manager.tests.test_source1923_manager_adopt \
  omo_manager.tests.test_codex_stop
uv run --project omo_manager --group dev ruff check \
  omo_manager/omo_source1923_manager_adopt.py \
  omo_manager/omo_codex_stop.py \
  omo_manager/tests/test_source1923_manager_adopt.py
```
