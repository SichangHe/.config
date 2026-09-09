# Root-retained session custody

Use this exceptional consumed-export input only when a root-level task changed after its accepted report by emptying `pending_task_items`, adding lifecycle removal notes, and completing in place. The ordinary status-only and monthly-rename custody paths remain preferred.

```bash
omo_report.sh \
  --export-archived-consumed PRIVATE_ENVELOPE \
  --consumed-attestation-output OWNER_PRIVATE_EXPORT \
  --root-retained-session-transcript OWNER_CODEX_JSONL \
  --root-retained-lifecycle-transcript REVIEWER_CODEX_JSONL \
  --ownership-acknowledgment-message-id '<MESSAGE_ID>' \
  --published-result-commit COMMIT
```

The helper derives all routing and report identity from the envelope and commitment. The four exceptional arguments are all-or-none and bind:

- an owned, single-link Codex JSONL prefix whose session metadata names the exact work-log root;
- an owned, single-link reviewer-child JSONL prefix with its imported parent header, exact parent spawn, and first completion event;
- exactly one commitment-digest task image in a tool output;
- exactly one successful `email_me.py` call, command event, and matching output for the supplied Message-ID in that session and before the task image;
- exactly one canonical `omo_report.sh --status done --message-file ...` execution for the replay and its same-turn successful `omo_pending.py remove --item ... --evidence ... --no-email` execution in the linked reviewer child;
- a result commit descended from the session's Git commit and contained by a stable local `refs/remotes/origin/main` snapshot;
- a current root task made from the report-time image only by `running` to `done`, replacing its one-item pending queue with `[]`, and appending complete `(verified removed pending item: ...)` lines, exactly one of which reproduces the recorded removal evidence and binds the report replay;
- the exact sole `previous:` TODO row for the producer target.

The export records both complete-line transcript prefix sizes and SHA-256 digests. Later append-only JSONL growth and later interactions with the same reviewer child are allowed; prefix edits, truncation, replacement, hard links, a mismatched imported parent header, task/TODO drift, or an `origin/main` race fail closed. `--describe-done-live-no-mail` additionally requires the current pane's Codex session ID to equal the owner transcript before it emits a close invocation.

For a top-level task whose contract explicitly forbids Human email, use the narrower no-mail form:

```bash
omo_report.sh \
  --export-archived-consumed PRIVATE_ENVELOPE \
  --consumed-attestation-output OWNER_PRIVATE_EXPORT \
  --root-retained-no-mail-transcript OWNER_CODEX_JSONL
```

This form requires one top-level Codex transcript to prove the private report, the matching queue removal, and the same-turn terminal completion. It reconstructs and verifies the committed report-time task from the final task and exact removal evidence. Two report/removal pairs are supported:

- an acknowledged terminal `done` report followed by `--no-email`, with exactly one explicit no-Human-email contract and no Human-email command anywhere in the captured prefix;
- an initially unacknowledged `in-progress` report, its exact manager consumption acknowledgment, then the normal completion-email removal, with one Human-owned queue item, completion key, and Message-ID. The complete authoritative Human-instruction block must quote exact lines from a safe private `manager_mail` source whose digest is bound in the export.

An earlier unrelated queue removal in the same top-level session is ignored only when it cannot reconstruct the commitment-bound report-time task. Two removals that both reconstruct those bytes remain ambiguous and fail closed.

The same form also supports a completed root task subsequently moved into one monthly archive by a clean committed R100 rename. The helper binds the report-time transition first, then the unchanged source/destination/head Git blob and current zero-reference TODO state. A non-R100 rename, task-path dirt, Git snapshot race, or archive bytes changed after the transcript transition fails closed.

Any mixed pair, missing authority, ambiguous matching report or removal, subagent transcript, or provenance shared between the two exceptional forms fails closed. The eventual pane close remains no-mail in both cases.

A strictly revalidated export may preserve a historical `main-manager-fallback` route when the requested manager had no active task. Closure accepts that route only when the export, transfer receipt, consumed-pointer transition, requested and resolved targets, and root-level dated manager log all agree. The receiver date records the historical delivery file; the later replay-validation date may differ. Ordinary manager-task routes still require the requested target, resolved target, and manager frontmatter target to match exactly.
