purpose

- close one non-human Codex pane intentionally left live by `--complete-live-no-mail`
- preserve the already-done task and exact `TODO.md` previous-row custody
- send no Human mail and never restart or resume task work

command

- for a consumed report whose pane output was lost, first run `omo_task_status.py --describe-done-live-no-mail TASK.md`
  - bind `--active-target`, `--manager-target`, the absolute exported `--manager-consumed-report-receipt`, and its SHA-256
  - the helper requires a ready pane, revalidates the export and task/TODO ownership before every pane input, sends one guarded `/status`, and returns current pane id, process id/start ticks, session, report token, and task/TODO digests
- when the done task was moved into `YYYYMM/` and removed from TODO, or remains at its original path under one canonical `previous` row, first create the export with `omo_report.sh --export-archived-consumed PRIVATE_ENVELOPE --consumed-attestation-output ABSOLUTE_FILE`
  - validate it with `omo_report.sh --validate-consumed-export FILE --expected-sha256 SHA256`
  - pass the archived task path, its original manager target, the export, and its digest to the same describe command
- run `omo_task_status.py --close-done-live-no-mail TASK.md`
  - bind `--active-target` and `--manager-target`
  - bind `--expected-task-sha256` and `--expected-todo-sha256`
  - bind `--expected-pane-id`, `--expected-pane-pid`, and `--expected-pane-start-ticks`
  - bind `--expected-session-id` and the accepted report receipt token with `--terminal-evidence`
  - reserve a new absolute owner-private `--audit-output`
  - if the manager consumed the report before the pane recorded acceptance, bind either the exported canonical `omo-report-consumed-closure/v1` attestation or the reviewed manager-acceptance bundle with `--manager-consumed-report-receipt` and its SHA-256; use its attestation/report ID as `--terminal-evidence`

task custody

- require v1 `status: done`
- require one non-manager `tool: codex` record at the exact non-human target and manager
- require an empty ordered queue and no live `(pending)` marker
- require no active competing owner for the target
- require either one exact unannotated `TASK.md TARGET` row under canonical `previous:`, or an export that binds the current done task to its report-time snapshot
  - a monthly archive requires its Git-authenticated original path and exact TODO bytes containing no reference
  - a root-retained task requires the exact report-time bytes after only reversing `status: done` to `status: running`, plus exactly one literal `previous:` header and one exact unindented row
  - a root-retained task whose ordered queue was removed after report time may instead use the transcript-prefix custody contract in `root-retained-session-custody.md`; the described live Codex session must match that transcript
- keep the TODO bytes unchanged

pane closure

- require the exact symbolic target, numeric pane, pane process, process start ticks, and Codex session
- require the accepted report token in the terminal before any input
  - exception: an exported consumed-closure attestation may bind the receipt module's exact historical watcher transition, worker allocation, commitment, envelope, transfer, and report; the older manager-acceptance bundle remains supported
  - the exception waives only the missing visible acceptance token; every pane, process, session, exit, capture, close-proof, and lifecycle check remains
- exit Codex only after a fresh bound `/status` response identifies the expected session
- authenticate one unchanged exited-shell capture
- launch the close child only through the tmux server's exact target, pane, and process predicate
- pass the SHA-256 of the complete canonical `terminalized` audit to the child and reauthenticate those exact bytes and the pane identity inside the child
- durably write the audit-digest/commitment-bound `.owner-close-started` marker before killing the numeric pane
- after proving both pane names and the pinned process absent, hard-link that marker to `.owner-stopped`, sync it, and remove the started name
- retain the audit-digest/commitment-bound `.owner-stopped` proof beside the audit

recovery

- `reserved` means no pane input was authorized
- `prepared` means accepted report and lifecycle evidence were checked before terminal input
  - v2 also binds the exact manager-consumed receipt digest before terminal input
- `terminalized` binds the exact exited-shell capture and close-proof commitment
- `terminalized` plus only `.owner-close-started` is retryable: reuse its secret if the exact pane is live, or promote it after exact absence
- `terminalized` plus both marker names is the retryable link-before-unlink state; both names must identify one inode
- `owner-stopped` requires the bound pane and process absent plus the durable proof
- `note-prepared` binds the only task suffix the helper may append
- `complete` requires that exact note, unchanged TODO custody, durable proof, and continued pane/process absence
- retry the identical invocation and audit path after interruption
- reject missing, forged, replayed, wrong-task, or drifted report evidence; mismatched marker evidence; rebound identity; changed audit bytes or capture; changed task or TODO; competing ownership; malformed audit; or an out-of-order task suffix

boundaries

- do not use this mode for running, blocked, queued, manager, v2, Cursor, PCODX, or human-owned tasks
- do not delete the completed audit or its sibling `.owner-stopped` proof; successful completion leaves no `.owner-close-started` name
- the helper never calls the completion-email path

source-bound WebConf recovery

- `omo_webconf_exited_shell_close.py` is the incident-scoped path for the exact
  `webconf_list_email.md` worker at `dw:19` left as an exited shell after its
  private report was routed, while the task remains `blocked` only on
  `done_close_in_progress`
- it binds the complete task and TODO digests, the authoritative Source-1524
  file and embedded Human envelope, the registered report commitment and
  report bytes, the empty queue and sole ownership, the fixed resume UUID, and
  the pane id/PID/start-ticks/capture digest
- the helper sends no pane input, never resumes Codex, and never sends Human
  mail; its only pane mutation is a tmux-server-guarded close of the exact
  already-exited shell
- use a fresh absolute owner-private audit path; retries accept only the exact
  prepared, shell-closed, or completed transition associated with the same
  immutable invocation, and conflicting task, TODO, pane, process, report,
  source, ownership, or audit state fails closed
- if only unrelated `TODO.md` bytes or the validated exited-shell capture have
  changed since the incident release, first run
  `omo_webconf_exited_shell_recovery.py --prepare` from one immutable helper
  tree, then have an independent owner run its `--review` mode
- the recovery packet binds the exact current WebConf row, every unrelated
  TODO byte, task/source/report/session/pane/process identity, the validated
  capture, the close-audit destination, and every helper input; pass the
  packet and review paths and digests to `omo_webconf_exited_shell_close.py`
- preparation, review, and close each recheck the exact packet inputs; any
  intervening task, TODO, capture, identity, helper, packet, review, or output
  drift fails before close
