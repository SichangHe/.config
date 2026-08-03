# tmux send helper

`omo_tmux_send.py` is the safe tmux paste primitive. Use its file path input for arbitrary prompt text.

Create a private prompt file, write it through an editor, `apply_patch`, or another non-shell text channel, then run:

```sh
prompt_file=$(mktemp "${TMPDIR:-/tmp}/omo-worker-prompt.XXXXXX")
chmod 600 "$prompt_file"
omo_tmux_send.py --target cfg:1.0 --message-file "$prompt_file"
```

To submit a prompt already visible in one non-human Codex target, authorize its exact UTF-8 text, including whitespace and line endings, with either a file or lowercase SHA-256 digest:

```sh
omo_tmux_send.py --target cfg:1.0 --submit-existing-file "$prompt_file"
omo_tmux_send.py --target cfg:1.0 --submit-existing-sha256 "$prompt_sha256"
```

This recovery is synchronous and target-scoped: it resolves one canonical pane ID, captures it with joined, trailing-space-preserving output, then compares the complete visible input exactly before Enter and before each retry. Unicode line separators remain distinct from LF. Post-submit verification stays on that pinned pane ID. The capture has no marker that distinguishes a layout spacer immediately before a recognized footer from an input-ending LF, so the helper rejects any whitespace-only row there, including one empty row. It accepts exact input whose final row directly precedes a strictly anchored model or `tab to queue message` footer. Footer-like prompt substrings and ambiguous prompt markers are rejected, as are human-owned `h*` targets, collapsed or partial input, overlays, whitespace-normalized differences, changed input, and mismatched file or digest authorization.

For a manager-authorized blocked repair that must discard stale duplicate input without submitting it or resuming work, use the corresponding cancellation interface:

```sh
omo_tmux_send.py --target cfg:1.0 --cancel-existing-file "$prompt_file"
omo_tmux_send.py --target cfg:1.0 --cancel-existing-sha256 "$prompt_sha256"
```

Cancellation applies the same exact-text and pinned-pane checks immediately before sending one `Ctrl+C`. It then requires the same live Codex pane to show no real input. It never sends Enter, retries `Ctrl+C`, submits the stale input, stops Codex, or performs normal dispatch. Target rebinding, changed input, unsupported state, overlays, human-owned `h*` targets, and unverifiable clearing fail the command.

Use direct file-based helpers for manager-authored files:

```sh
subject_file=$(mktemp "${TMPDIR:-/tmp}/omo-email-subject.XXXXXX")
body_file=$(mktemp "${TMPDIR:-/tmp}/omo-email-body.XXXXXX")
prompt_file=$(mktemp "${TMPDIR:-/tmp}/omo-worker-prompt.XXXXXX")
chmod 600 "$subject_file" "$body_file" "$prompt_file"
email_me.py --manager-human --subject-file "$subject_file" --message-file "$body_file"
omo_tmux_send.py --target cfg:1.0 --message-file "$prompt_file"
omo_task.py --task-file x.md --tmux-session cfg --workdir /repo --model gpt-5.6-terra --reasoning-effort medium --prompt-file "$prompt_file"
```

For worker prompts, keep task-file paths out of the prompt body. Workers report with `omo_report.sh` from their tmux pane and do not need `--task-file`, `--root`, `--manager-target`, or other manual route flags.

For prompts, it reads `--message-file`, writes the payload to a private `0600` temp file, loads that file into a tmux buffer, pastes the buffer to the target, and uses `send-keys` only for final Enter keys.

Before normal paste, the helper refuses any real existing input and never uses Enter to clear or submit it. The helper supports Codex `running`, `ready`, `stuck_input`, and `error` states; it still rejects non-Codex panes. Error panes are sendable so recovery prompts can use the normal helper. During verified delivery, the preexisting error may remain visible for the bounded submit transition, but a different error fails the send. The error state is rechecked after any pre-paste callback and immediately before each submit Enter, including prompts verified through the stock-placeholder probe.

`--enter-count N` supports repeated submit keys. Codex sends always submit after paste. Verification requires the submitted prompt to leave the input and the pane to become `running` or `ready`; if the prompt remains in the input, the helper retries Enter until the submit verification timeout expires. `omo_dispatch.sh --tmux-target TARGET` uses this helper for normal prompt dispatch and defaults tmux dispatches to two Enter keys; override Enter count with `OMO_DISPATCH_TMUX_ENTER_COUNT`.

Use `--async` when the caller must continue immediately while the helper performs verified send. Async mode creates a private `0700` result directory under `${TMPDIR:-/tmp}` named `omo-tmux-send-async-ID`, copies `--message-file` to `payload.txt`, captures worker output in `stdout.log` and `stderr.log`, writes `status.txt`, `result.txt`, and `metadata.tsv`, and returns after printing `async_id: ID`, `result_dir: PATH`, and the worker pid.

Query the result with `omo_tmux_send.py --async-result ID` or `omo_tmux_send.py --async-result PATH`; statuses are `pending`, `running`, `succeeded`, `failed`, or `missing`. Add `--async-notify-target CALLER` when `CALLER` is a different pane from the target and should receive the legacy completion paste.
