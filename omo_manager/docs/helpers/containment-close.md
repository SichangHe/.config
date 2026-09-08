# Contained sentinel close

`omo_manager_containment_close.py` is the close-only successor to a completed `omo_manager_rotation_contain.py` operation. It removes one exact receipt-bound `sleep infinity` sentinel when the Human ordered closure and no successor should be launched. It never starts or resumes an agent and never edits task or TODO records.

Use the helper only while all of these bindings are available:

- the complete owner-private containment receipt and all of its explicit historical assertions
- the live sentinel's exact non-`h*` target, pane, window, PID, start ticks, and argv digest
- a one-pane window with no child of the sentinel and no survivor from the failed successor's process session or group
- the current empty-queue manager task and TODO digests, either blocked in `current` with one active owner or done in `previous` with no active owner
- the current manager parent, the original protected-target set, and no successor-ownership receipt
- one direct `manager_mail` source whose selected Human-written lines contain an unconditional close instruction that literally names the exact tmux target, plus the exact authoritative-Human envelope for those bytes

A generic instruction such as `Close them all`, a request to report the helper defect, or an exact target appearing only later or in forwarded/quoted text does not authorize pane closure. The direct Human imperative must name the exact target as the close verb's object, so one authority source cannot be replayed against a different containment receipt or used to close a target the Human excluded.

Read the command's help for its explicit assertions. Run the exact invocation with `--dry-run` first, have that immutable invocation reviewed, then remove only `--dry-run`. The output path is fixed beside the containment receipt as `CONTAINMENT-STEM-sentinel-close.receipt`.

The mutating path takes the manager-rotation, task-membership, task-target, task-file, and TODO locks. It writes a private `prepared-or-committed` receipt before one tmux-server-guarded numeric-pane close. The guard requires the same target, window, pane, PID, `sleep` command, and a one-pane window. Acceptance is emitted before `kill-pane`, because closing the final pane can remove its window. The helper then proves that the symbolic target, pane, window, and sentinel process session/group are absent, rechecks task/TODO bytes, and finalizes the receipt as `complete` with `successor_launched: false`.

If execution stops after the guarded close, rerun the identical command. An exact prepared receipt may become complete only when the bound symbolic target, pane/window, and process session/group are all absent. Reuse of that target by a different pane, a changed receipt, changed authority, task drift, successor receipt, malformed tmux inventory, or ambiguous process state fails closed.

Do not change the task lifecycle until the close receipt is complete. Afterward, use the applicable task lifecycle helper to close or retain the record. Do not retroactively mint a close receipt for a pane that another operation already removed without this helper's prepared receipt.
