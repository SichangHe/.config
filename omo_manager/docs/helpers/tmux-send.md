# tmux send helper

`omo_tmux_send.py` is the safe tmux paste primitive. Use its file path input for arbitrary prompt text.

Create a private prompt file, write it through an editor, `apply_patch`, or another non-shell text channel, then run:

```sh
prompt_file=$(mktemp "${TMPDIR:-/tmp}/omo-worker-prompt.XXXXXX")
chmod 600 "$prompt_file"
omo_tmux_send.py --target cfg:1.0 --message-file "$prompt_file" --enter
```

Use direct file-based helpers for manager-authored files:

```sh
subject_file=$(mktemp "${TMPDIR:-/tmp}/omo-email-subject.XXXXXX")
body_file=$(mktemp "${TMPDIR:-/tmp}/omo-email-body.XXXXXX")
prompt_file=$(mktemp "${TMPDIR:-/tmp}/omo-worker-prompt.XXXXXX")
chmod 600 "$subject_file" "$body_file" "$prompt_file"
email_me.py --manager-human --subject-file "$subject_file" --message-file "$body_file"
omo_tmux_send.py --target cfg:1.0 --message-file "$prompt_file" --enter
omo_task.py --task-file x.md --tmux-session cfg --workdir /repo --prompt-file "$prompt_file"
```

For prompts, it reads `--message-file`, writes the payload to a private `0600` temp file, loads that file into a tmux buffer, pastes the buffer to the target, and only uses `send-keys` for optional final Enter keys.

Before submitted paste, it sends a separate Enter when the target is already classified as `stuck_input`, then continues with the normal send path. If the target tail shows Codex compacting, it polls tmux output before clearing stuck input, before paste, and before each Enter; a compaction timeout aborts paste. `--ready-timeout-s N` waits for a Codex idle input box before submitted paste, preventing Codex from queueing dispatch text as `Messages to be submitted after next tool call`.

`--enter-count N` supports repeated submit keys. Submitted Codex sends verify that the active input line is empty after Enter; if it still contains text, the helper sends one fallback Enter and then fails if any non-placeholder input text remains. `omo_dispatch.sh --tmux-target TARGET` uses this helper for normal prompt dispatch, defaults submitted tmux dispatches to two Enter keys, and waits up to `OMO_DISPATCH_TMUX_READY_TIMEOUT_S` seconds, default `300`; override Enter count with `OMO_DISPATCH_TMUX_ENTER_COUNT`.

Use `--async` when the caller must continue immediately while the helper waits for the target to become ready. Async mode creates a private `0700` result directory under `${TMPDIR:-/tmp}` named `omo-tmux-send-async-ID`, copies `--message-file` to `payload.txt`, captures worker output in `stdout.log` and `stderr.log`, writes `status.txt`, `result.txt`, and `metadata.tsv`, and returns after printing `async_id: ID`, `result_dir: PATH`, and the worker pid.

Query the result with `omo_tmux_send.py --async-result ID` or `omo_tmux_send.py --async-result PATH`; statuses are `pending`, `running`, `succeeded`, `failed`, or `missing`. Add `--async-notify-target CALLER` when `CALLER` is a different pane from the target and should receive the legacy completion paste; use `--async-notify-enter-count 0` to paste the completion notice without submitting it.
