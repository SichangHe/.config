# codex stop helper

Normal manager task closure goes through `omo_task_status.py TASK.md done`, which owns task frontmatter, TODO movement, and worker shutdown. The lower-level `omo_codex_stop.py --target SESSION:WINDOW[.PANE]` captures the pane tail, sends `/status` with one Enter plus one fallback Enter only if `/status` remains in the Codex input, records only the newly emitted `Session: UUID` status value or post-close `codex resume UUID`, and ignores transcript UUIDs.

Before closing an idle task-backed worker, it asks for concise process feedback and waits up to `--feedback-wait-s`, default `180`; use `--no-feedback` for trivial, already-reviewed, or urgent closes. The feedback prompt asks whether the worker had partial-compaction access, whether it used it, why or why not, and what should change in the PCODX instructions/tools/triggers.

It exits Codex with repeated Ctrl-C inputs and short delays until the pane reaches a shell or the bounded retry loop is exhausted. After Codex reaches a shell, normal closure kills the single-pane tmux window or only the target pane in a multi-pane window.

For selected-model-capacity recovery or another deliberate live restart, run `omo_codex_start.py --task-file TASK.md --target SESSION:WINDOW[.PANE] --model MODEL --reasoning-effort EFFORT --restart-running` from another pane. It captures the live Codex session id before atomically replacing the process with `tmux respawn-pane -k`; the exact tmux pane and window stay in place. Do not call the stop helper first. Use `omo_task_status.py TASK.md done` for normal closure. The helpers refuse `h*` human-owned session targets except for task-status closure with a digest-bound private human authority record. That record must either name the exact task file in its subject and directly close the exact target, or be a reply with one unquoted `cancel this task` directive and exactly one quoted mailbox-compression provenance statement that names the task's frontmatter target as the responsible EDA/C++ owner and says task ownership is unchanged. Both forms retain the exact symbolic-target, pinned-pane, session, and pre-close authority revalidation safeguards.

When invoked by the task-status helper, the task-file close note uses `MM-DD HH:MM TZ`, target, optional `session_id`, no year, and no resume command; task file frontmatter remains authoritative. The lower-level stop helper without a task file only prints the captured ID. It refuses to stop the current pane unless `--allow-self` is passed.

Managers should not tell workers to run stop/status helpers or provide task-file paths for closure; task-file edits stay manager-owned through helper scripts.

Substantial partial-compaction feedback from a worker is routed as product evidence, not only as close-time process notes. This includes feedback collected by the close-time prompt.

The manager should preserve the worker report plus task file, tmux target, session id, session JSONL path, and PCODX ledger path when available; email the human with a short subject naming the task file; then forward the same evidence to active OPC partial-compaction work. If no active OPC worker exists, create a new PCODX task in the `opc` tmux session rooted at `/ssd1/sichangheagent/opencode_partial_compact/experiments/codex-wrapper` and include the evidence bundle in the prompt.
