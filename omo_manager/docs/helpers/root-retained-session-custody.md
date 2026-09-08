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
