# digest and email helpers

`omo_digest_queue.py` is the durable non-urgent digest path. `submit` appends digest items to the configured queue file, records absolute `queued-at`, and records absolute `published-at` when the source provides it or a relative `Published N ago` value can be resolved at queue time.

`deliver-once` sends queued items immediately when requested and renders absolute queued/published times; idle timing and recent-contact checks belong to a separate watcher.

PB news digest:
- PB watcher queues digest items into the manager repo queue at `/ssd1/sichangheagent/work_logs/manager_digest.md`
- append one Markdown item from stdin with `scripts/manager-digest append`
- preview delivery with `scripts/manager-digest deliver --dry-run`
- when the human asks, run `scripts/manager-digest deliver` immediately from `/ssd1/sichangheagent/work_logs`
- the delivery script clears the manager queue only after a successful send

`email_me.py --manager-human` is the human email sender. New manager-human subjects use `[a] [TARGET]`; old `[omo_manager]` subjects remain accepted and canonicalized to `[a]`. Subjects must be passed with `--subject-file` or explicit `--subject`; positional subjects are refused so a body draft path cannot accidentally become the email subject.

Before sending, recent-thread lookup strips repeated `Re:` plus `[a]`, `[omo_manager]`, legacy `[omo]`, and leading tmux window/pane subject tags. When a match is found in the recent window, the outgoing subject becomes `Re: [a] [TARGET] SUBJECT` and the message includes `In-Reply-To` and `References` headers from the matched self-sent mail.

Reply subject preparation strips old tmux tags before prepending the selected current bracketed tag. Normally omit `--tmux-target` and `--sender-tmux-target`. Attribution resolves in this order: explicit target; valid `OMO_AGENT_TMUX_TARGET`; for manager-human mail, valid `OMO_MANAGER_TMUX_TARGET`; current tmux pane. Manager-human sending fails if no valid target is available. Use an explicit target only to preserve a different verified producer identity during forwarding or compression, never as a task owner or delivery destination. A zero-pane target such as `hcfg:1.0` is rendered as `hcfg:1`; nonzero panes keep their pane suffix.

`omo_manager_setup_watchers.sh` loads `local.env`, exports the manager environment for pending watcher helpers, and passes explicit manager flags only to helpers that still require them. Lookup failures are non-critical and fall back to a normal `[a] [TARGET] SUBJECT` send.

`email_me.py` sends a plain text fallback with Markdown links expanded to bare URLs, emits an email-compatible HTML alternative with escaped raw HTML, renders list-containing bodies as normal HTML instead of wrapping the whole email in `<pre>`, and appends a final `tmux: TARGET` footer when a target is known, otherwise `PWD: NAME`.
