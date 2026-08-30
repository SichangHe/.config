# Prepared ordinary-worker successor

> **Draft, inactive pending exact Human approval.** If approved, this procedure will govern managers and ordinary-worker replacement tasks that explicitly invoke these helpers. It must not be used before that approval. Any byte change requires renewed exact Human review before use.

`omo_worker_successor.py` prepares one stopped, non-manager worker's successor without launching it. Use it only for a supported lifecycle transfer; it serializes cooperating work-log writers and does not claim atomicity against arbitrary processes that ignore the locks.

The preparation invocation binds the canonical work-log root; old and successor task names; exact old-task and TODO bytes; the worker target, manager, tool, and sorted protected-target set; the complete ordered nonempty queue; a frozen prompt; and a canonical launch manifest. Both frozen files are opened without symlink following. Supply both the literal queue (`--expected-pending-item` in order) and its SHA-256 over NUL-joined UTF-8 items. Supply the prompt, protected-set, and launch-manifest SHA-256 values too.

The canonical launch manifest is JSON emitted by `launch_manifest_bytes()` for the same root, successor task, target, manager, and Cursor tool. It also freezes the exact existing workdir, tmux session/window, window name, model, reasoning effort, Codex-flag list, AMH caller ID, worker-default instruction digest, minimal child and tmux-client environments, absolute Bash and `env` identities, the installed tmux client identity, and the installed Cursor launcher, Node runtime, and program path/digests. It explicitly records no prelaunch source. Put those exact bytes in a distinct owner-controlled, owner-owned, regular direct child of the work-log root with exact mode `0600` before preparation. Prepared launch currently rejects non-Cursor tools, prelaunch source files, Cursor Codex flags, new-session creation, resume/session inputs, Human-email input, and manager roles.

The target must already be absent. The old task must be its sole active worker owner, and the successor path must not exist. Under the root-membership, target, task, TODO, prompt, and journal locks, the helper creates an owner-private no-replace journal, empties and completes the old task, moves the TODO row, and publishes exactly one blocked successor with the same queue plus the digest-bound manager prompt. It checks global ownership with both the authoritative resolver and the stricter raw-record scan. Each journal phase is fsynced. An identical retry recognizes only journaled before/after bytes and deterministically finishes the transaction; unknown bytes fail closed.

Example shape (all digests and queue values must be exact):

```sh
uv run --offline --python 3.13 -m omo_manager.omo_worker_successor \
  --root "$work_logs" \
  --old-task old_worker.md \
  --successor-task successor_worker.md \
  --target cfg:7.0 \
  --manager-target cfg:1.0 \
  --tool cursor \
  --old-sha256 "$old_sha" \
  --todo-sha256 "$todo_sha" \
  --expected-pending-item "$first_item" \
  --expected-pending-item "$second_item" \
  --queue-sha256 "$queue_sha" \
  --prompt-file "$frozen_prompt" \
  --prompt-sha256 "$prompt_sha" \
  --protected-target cfg:0.0 \
  --protected-sha256 "$protected_sha" \
  --journal "$work_logs/.omo-worker-successor-0123456789abcdef.transaction" \
  --launch-manifest "$work_logs/.omo-worker-successor-launch.json" \
  --launch-manifest-sha256 "$launch_manifest_sha"
```

Preparation does not start a process. Launch only through `omo_task.py`'s prepared-successor mode, passing the exact committed journal, successor-task, prompt, queue, and launch-manifest digests printed by preparation. The launch must use an existing non-Human tmux session, an explicit window, every launch option frozen by the manifest, `--no-link`, and `--require-existing-tmux-session`.

The launch requires the journal and manifest to remain separate direct children of the exact same canonical root and rechecks both files' owner and exact mode `0600`. It reopens and revalidates the journal, manifest, prompt, old task, TODO, successor, protected panes, target absence, and global sole ownership while holding the preparation root/target namespace and all relevant file locks. Every prepared tmux inventory, create, capture, delivery, identity, and cleanup operation uses the digest-bound absolute tmux client from a pinned root directory under its separate minimal environment; ambient `PATH`, loader variables, shell startup variables, and socket-directory overrides never reach that client or its server. The new pane starts only the digest-bound absolute Bash through pinned `env -i`, so its initial environment is exactly the manifest-bound set. Before creating a window the launcher authenticates the full tmux pane inventory and gives only the new pane an unguessable transaction token. On a client error or timeout, it inventories again and removes the exact pane only when exactly one new pane has the requested target and that token; no new pane is success, while multiple, malformed, or unauthenticated new panes are preserved and fail closed. Cleanup targets the authenticated pane ID rather than its window, so a concurrent or pre-existing sibling split remains intact. Malformed or swapped `new-window` output can therefore never authorize killing a pre-existing pane. It copies the exact bound worker defaults plus manager-delegated prompt to an owner-private temporary descriptor-backed file. It executes the digest-bound absolute Cursor launcher through pinned absolute `env` and Bash executables under `env -i`, with only the manifest-bound minimal environment plus the bound target/caller. Parent `PATH`, `BASH_ENV`, `ENV`, `NODE_OPTIONS`, loader injection, language-path, and compiler-wrapper variables do not cross that boundary.

After launch it proves one descendant with the exact bound Node executable, program path, complete argv, workdir, model/effort, captured prompt, required minimal environment, and absence of the rejected injection variables. Merely observing pane command `agent` is never sufficient. Only after that proof does it change the blocked task to `running`; the queue remains nonempty throughout. The owner-private, no-replace launch receipt advances through `prepared`, `window`, `started`, `published`, and `committed`, digest-binding the protected-pane inventory plus exact session/window/pane, pane process, and Cursor process identities. A retry of an incomplete receipt authenticates those identities, the exact blocked-or-running task bytes, both authoritative and raw sole-owner scans, and protected panes. It then either commits the already-valid state or restores the exact blocked task, removes only the journal-authenticated pane while preserving any sibling panes, and records a durable `failed` containment receipt. A target that appeared before its creation identity was durably recorded is preserved rather than killed unless its exact Cursor process proves the transaction. Arbitrary unsynchronized editors remain outside the atomicity claim and cause fail-closed containment.

Focused offline verification:

```sh
uv run --offline --python 3.13 python -m unittest \
  omo_manager.tests.test_worker_successor \
  omo_manager.tests.test_omo_task
uv run --offline --python 3.13 ruff check \
  omo_manager/omo_worker_successor.py \
  omo_manager/omo_task.py \
  omo_manager/tests/test_worker_successor.py \
  omo_manager/tests/test_omo_task.py
```
