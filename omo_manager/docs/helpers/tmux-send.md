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

This recovery is synchronous and target-scoped: it resolves one canonical pane ID, captures it with joined, trailing-space-preserving output, then compares the complete visible input exactly before Enter and before each retry. Unicode line separators remain distinct from LF. Post-submit verification stays on that pinned pane ID. Codex renders one exact empty row as the composer's bottom spacer immediately before a recognized footer, so the helper removes that row. Some terminal widths render that same spacer as ASCII-space padding; the helper removes such padding only when its bounded delivery ledger proves that it recently sent the reconstructed exact text to this same target. For submit-only file authorization, exactly two adjacent ASCII-space rows may instead represent one final empty input row plus that renderer spacer; the helper tries both byte interpretations and accepts only one exact file match. Digest-only submission and cancellation do not use this exception. Any preceding row remains authorized input: extra empty rows preserve trailing LF, and whitespace preceding the exact spacer remains input. Other whitespace-only rows remain ambiguous and are rejected. It also accepts exact input whose final row directly precedes a strictly anchored model or `tab to queue message` footer. Footer-like prompt substrings and ambiguous prompt markers are rejected, as are human-owned `h*` targets, collapsed or partial input, overlays, whitespace-normalized differences, changed input, and mismatched file or digest authorization.

Digest-authorized submission also accepts one complete Cursor composer: exactly one upper border and arrow prompt, one lower border, an optional task-count row, one anchored Cursor model/percentage footer, and an optional workspace row at the bottom of the capture. It still pins and rechecks the pane, complete extracted input, digest, process-backed Cursor state, and error transition before Enter. Ambiguous, duplicated, partial, collapsed-paste, overlay, or displaced layout elements fail closed. This exception is submit-only; `--cancel-existing-*` continues to reject Cursor input.

For a manager-authorized blocked repair that must discard stale duplicate input without submitting it or resuming work, use the corresponding cancellation interface:

```sh
omo_tmux_send.py --target cfg:1.0 --cancel-existing-file "$prompt_file"
omo_tmux_send.py --target cfg:1.0 --cancel-existing-sha256 "$prompt_sha256"
```

Cancellation applies the same exact-text, exact empty spacer, and pinned-pane checks immediately before sending one `Ctrl+C`. It then requires the same live Codex pane to show no real input. It never sends Enter, retries `Ctrl+C`, submits the stale input, stops Codex, or performs normal dispatch. Target rebinding, changed input, unsupported state, overlays, human-owned `h*` targets, and unverifiable clearing fail the command.

When a failed Cursor paste leaves only a transport suffix in the composer, first obtain its current raw-rendering proof without mutation, then use that exact digest once:

```sh
omo_tmux_send.py --target cfg:1.0 --describe-partial-cursor
omo_tmux_send.py --target cfg:1.0 --clear-partial-cursor-sha256 "$rendered_sha256" --submit-verify-timeout-s 20
```

This incident-specific path requires the exact `cursor-agent` launcher and a suffix-only transport whose first non-whitespace text begins `Await its terminal result` and ends in `</agent_message>` within the same strict bottom-anchored Cursor layout. Any opening `<agent_message>` tag is rejected. It pins the exact raw rendering, pane, PID, and command, rejects complete messages and human-owned targets, revalidates immediately before Backspace, and requires the same ready pane to reach a complete empty composer. During a bounded redraw it accepts only unchanged or strictly shrinking prefixes of the authenticated text; growth, unrelated input, identity drift, overlays, errors, ambiguity, and timeout fail closed. It never pastes, submits, retries the original message, sends Enter, or uses `Ctrl+C`.

Use direct file-based helpers for manager-authored files:

```sh
subject_file=$(mktemp "${TMPDIR:-/tmp}/omo-email-subject.XXXXXX")
body_file=$(mktemp "${TMPDIR:-/tmp}/omo-email-body.XXXXXX")
prompt_file=$(mktemp "${TMPDIR:-/tmp}/omo-worker-prompt.XXXXXX")
chmod 600 "$subject_file" "$body_file" "$prompt_file"
email_me.py --manager-human --subject-file "$subject_file" --message-file "$body_file"
omo_tmux_send.py --target cfg:1.0 --message-file "$prompt_file"
omo_task.py --task-file x.md --tmux-session cfg --workdir /repo --model cursor-grok-4.6 --reasoning-effort xhigh --prompt-file "$prompt_file"
```

For worker prompts, keep task-file paths out of the prompt body. Workers report with `omo_report.sh` from their tmux pane and do not need `--task-file`, `--root`, `--manager-target`, or other manual route flags.

For prompts, it reads `--message-file`, writes the payload to a private `0600` temp file, loads that file into a tmux buffer, pastes the buffer to the target, and uses `send-keys` only for final Enter keys.

Before mutating the target, normal and system-message sends atomically claim the canonical target and unwrapped payload in the owner-only manager state directory. Generated pending-watcher manager delegations use their HTML-decoded inner instruction as the identity, so the same direct instruction and watcher-routed instruction share one claim despite different transport text. One terminal newline is transport-insignificant; otherwise, messages retain byte-exact identities. A matching retry within five minutes is an idempotent success and does not clear input, run a pre-paste callback, load or paste a buffer, or send Enter, including when the first process could not verify whether Codex queued the paste. A failure known to occur before delivery releases the claim; an ambiguous or successful paste retains it. A different target or payload remains deliverable. Set `OMO_MANAGER_TMUX_DELIVERY_DEDUPE_S=0` to disable the bounded claim; raw `/compact` control sends are not deduplicated.

Before normal paste, the helper refuses any real existing input. A strict exception recognizes a completed Cursor transport message still rendered in the composer. From one raw visible-pane capture, it requires one bottom-anchored arrow region, blank spacer rows, one exact Cursor footer, and one exact workspace row; border glyphs and off-screen history are not required. It binds the raw rendering digest plus the exact pane, pane PID, and accepted Cursor command, then requires an identical second proof. It clears that value with Backspace keys behind one atomic tmux predicate (never Enter or `Ctrl+C`), proves the same ready pane now has a complete empty composer, and only then pastes the new message. During the bounded post-clear redraw it may accept only unchanged or strictly shrinking prefixes of the authenticated text, or wait through an incomplete layout, while the pane/process stay pinned and no real input growth, unrelated text, overlay, fatal text, or error appears. Paste and its one submit Enter use the same atomic pane/PID/command predicate; post-paste verification requires the exact new transport message rather than a matching substring. An appended/combined prompt, multiple arrow or footer candidates, rebinding, byte drift, non-ready state, error, timeout, or failed empty check fails closed. The accepted command is the exact command already recognized by the process validator (`agent` or the installed launcher name, commonly `cursor-agent`), and the proof pins the observed value. The helper supports Codex `running`, `ready`, `stuck_input`, and `error` states. Cursor delivery uses the follow-up composer (`→ Add a follow-up` is `ready`); follow-up text is `stuck_input` and is not scanned as a Codex error; collapsed Cursor pastes look like `[Pasted text #N +M lines]`. A Cursor `follow-ups` overlay with `enter send now` is flushed with Enter on the ordinary path; the retained-composer recovery instead fails closed on overlays. It still rejects other non-agent panes. Error panes are sendable so recovery prompts can use the normal helper. During verified delivery, the preexisting error may remain visible for the bounded submit transition, but a different error fails the send. Shared routing for this helper is in `../routing/supported-delivery.md`.

Codex panes started inside an existing interactive shell are authenticated through that exact pane's terminal foreground process group when tmux has no usable launch command. The helper rechecks the target, pane PID, process identities, terminal session, command line, and supported Codex package before paste.

`--enter-count N` supports repeated submit keys. Codex sends always submit after paste. Verification requires the submitted prompt to leave the input and the pane to become `running` or `ready`; if the prompt remains in the input, the helper retries Enter until the submit verification timeout expires. `omo_dispatch.sh --tmux-target TARGET` uses this helper for normal prompt dispatch and defaults tmux dispatches to two Enter keys; override Enter count with `OMO_DISPATCH_TMUX_ENTER_COUNT`.

Use `--async` when the caller must continue immediately while the helper performs verified send. Async mode creates a private `0700` result directory under `${TMPDIR:-/tmp}` named `omo-tmux-send-async-ID`, copies `--message-file` to `payload.txt`, captures worker output in `stdout.log` and `stderr.log`, writes `status.txt`, `result.txt`, and `metadata.tsv`, and returns after printing `async_id: ID`, `result_dir: PATH`, and the worker pid.

Query the result with `omo_tmux_send.py --async-result ID` or `omo_tmux_send.py --async-result PATH`; statuses are `pending`, `running`, `succeeded`, `failed`, or `missing`. Add `--async-notify-target CALLER` when `CALLER` is a different pane from the target and should receive the legacy completion paste.
