# hees final-artifact owner replacement

`omo_hees_final_artifact_replace.py` is pinned to replacing the absent stale owner `hees_1170_policy.md` at `guest_hees:5` with an unlaunched `hees_final_artifact.md`. It does not launch, deliver, signal a pane, or change another guest record.

The invocation binds the exact stale-task bytes, TODO bytes, and sole pending-item text by SHA-256. Under the global task-membership lock, the target lock, and all three file locks, the helper requires:

- one blocked v1 Codex worker owned by `guest_hees:0`, with exactly the bound pending item
- one exact `hees_1170_policy.md guest_hees:5` row under `current` or `human pending`, no successor file or TODO row, and no other active target claimant
- an absent `guest_hees:5` pane before and throughout the transaction
- unchanged task and TODO snapshots immediately before their writes
- a descriptor-bound owner-owned work-log tree with no world-writable directory or extended access/default ACL; each group-writable directory must be setgid to the exact `sichanghe` group, whose only principals remain the human `sichanghe` and the current agent account

It first proves the complete candidate state has exactly one active owner. Before changing lifecycle files, it durably publishes an owner-private, invocation-bound transaction journal containing the exact before/after bytes and an integrity commitment. Recovery independently reconstructs all canonical after-state bytes from the hash-bound before-state instead of trusting the journal's after-state. It then marks the stale record done with an empty queue, replaces the TODO row in its existing section, records the stale row under `previous`, and finally publishes the complete blocked successor from an anonymous inode through an exclusive hard link. Each public namespace mutation is directory-synced. A catchable pre-publication failure reverses completed writes. Once the successor exists, the helper never deletes it: a later invocation validates and finalizes the complete transaction, while any foreign or partial successor is preserved. The journal is retained as the durable one-shot transaction receipt, so no cleanup path can delete a rebound journal or successor. Each exchange also retains its owner-owned displaced inode under a `.TASK.omo-stage-HEX` name. Success and recovery bound their count and revalidate every receipt's identity, mode, and bytes against the canonical transaction states; malformed or changed receipts stop recovery and remain available for manual inspection. Unrecognized recovery state, ownership drift, or rollback failure stops with those receipts intact.

Final normal and recovery success use the same executable proof sequence while the global membership, canonical target, four lifecycle-file locks, and retained root descriptor remain held. The last mutation is successor publication in a normal transaction; recovery performs no success-path mutation. The proof captures the root-directory generation, then checks the exact journal, stale task, TODO, successor, and every retained receipt by descriptor-bound inode and bytes; scans the complete descriptor-bound task tree for the sole successor owner; reproves stale-pane absence under the target lock; validates the public root binding; and requires the directory generation to remain unchanged. The function returns from inside those locks immediately after this sequence.

This atomicity and verified-success contract covers authenticated supported lifecycle writers that honor the canonical alias-equivalent target lock and descriptor/identity protocol. The helper fails closed on unsupported filesystem mutation wherever its descriptor, inode, generation, byte, owner, pane, rollback, or recovery checks observe it. It does not claim that a finite final check can exclude a later mutation by an uncooperative writer that ignores the canonical lock.

Run the hash-bound command only once the responsible manager is ready to perform the replacement:

```bash
~/.config/omo_manager/omo_hees_final_artifact_replace.py \
  --root "$OMO_WORK_LOGS_ROOT" \
  --stale-task hees_1170_policy.md \
  --successor-task hees_final_artifact.md \
  --stale-target guest_hees:5 \
  --manager-target guest_hees:0 \
  --stale-sha256 STALE_TASK_SHA256 \
  --todo-sha256 TODO_SHA256 \
  --pending-item-sha256 PENDING_ITEM_SHA256
```

After success, use `omo_task.py` separately with `--tool codex`, `--workdir`, `--task-file hees_final_artifact.md`, `--tmux-session guest_hees`, `--tmux-window 5`, `--manager-target guest_hees:0`, and the approved model/reasoning/prompt inputs. The normal launcher changes the successor from blocked to running while preserving its queue and Codex tool, then ordinary verified delivery may proceed.

Focused verification from `~/.config` is `uv run --python 3.13 --with pyyaml -m unittest omo_manager.tests.test_hees_final_artifact_replace omo_manager.tests.test_task_target_lock`; PyYAML is explicit because the helper's imported task metadata parser requires it.
