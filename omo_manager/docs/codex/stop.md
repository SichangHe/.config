# codex stop helper

`omo_codex_stop.py --target SESSION:WINDOW[.PANE] --root ROOT --task-file TASK.md` captures the pane tail, sends `/status` with one Enter plus one fallback Enter only if `/status` remains in the Codex input, records only the newly emitted `Session: UUID` status value or post-close `codex resume UUID`, and ignores transcript UUIDs.

Before closing an idle task-backed worker, it asks for concise process feedback and waits up to `--feedback-wait-s`, default `180`; use `--no-feedback` for trivial, already-reviewed, or urgent closes. The feedback prompt asks whether the worker had partial-compaction access, whether it used it, why or why not, and what should change in the PCODX instructions/tools/triggers.

It exits Codex with repeated Ctrl-C inputs and short delays until the pane reaches a shell or the bounded retry loop is exhausted. After Codex reaches a shell, it kills the single-pane tmux window or only the target pane in a multi-pane window.

The task-file close note uses `MM-DD HH:MM TZ`, target, optional `session_id`, no year, and no resume command. With `--task-file`, it also moves the task reference from `TODO.md` `current` to the top of `previous`; the task md status tag remains authoritative. Without `--task-file`, it only prints the captured ID. It refuses to stop the current pane unless `--allow-self` is passed.

Substantial partial-compaction feedback from a worker is routed as product evidence, not only as close-time process notes. This includes feedback collected by the close-time prompt.

The manager should preserve the worker report plus task file, tmux target, session id, session JSONL path, and PCODX ledger path when available; email the human with a short subject naming the task file; then forward the same evidence to active OPC partial-compaction work. If no active OPC worker exists, create a new PCODX task in the `opc` tmux session rooted at `/ssd1/sichangheagent/opencode_partial_compact/experiments/codex-wrapper` and include the evidence bundle in the prompt.
