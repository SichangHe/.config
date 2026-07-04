# digest and email helpers

`omo_digest_queue.py` is the durable non-urgent digest path. `submit` appends digest items to the configured queue file, records absolute `queued-at`, and records absolute `published-at` when the source provides it or a relative `Published N ago` value can be resolved at queue time.

`deliver-once` sends queued items immediately when requested and renders absolute queued/published times; idle timing and recent-contact checks belong to a separate watcher.

`email_me.py --manager-human` is the human email sender. New manager-human subjects use `[a] [TARGET]`; old `[omo_manager]` subjects remain accepted and canonicalized to `[a]`.

Before sending, recent-thread lookup strips repeated `Re:` plus `[a]`, `[omo_manager]`, legacy `[omo]`, and leading tmux window/pane subject tags. When a match is found in the recent window, the outgoing subject becomes `Re: [a] [TARGET] SUBJECT` and the message includes `In-Reply-To` and `References` headers from the matched self-sent mail.

Reply subject preparation strips old tmux tags before prepending the selected current bracketed tag. For manager-human sends, the selected tag and footer target prefer explicit `--tmux-target` or `--sender-tmux-target`, then `OMO_MANAGER_TMUX_TARGET`, then the agent/current tmux fallback; sending fails when no valid target is available.

`omo_manager_setup_watchers.sh` loads `local.env` and passes `--manager-target` to watcher helpers so the manager target stays stable. Lookup failures are non-critical and fall back to a normal `[a] [TARGET] SUBJECT` send.

`email_me.py` sends a plain text fallback with Markdown links expanded to bare URLs, emits an email-compatible HTML alternative with escaped raw HTML, renders list-containing bodies as normal HTML instead of wrapping the whole email in `<pre>`, and appends a final `tmux: TARGET` footer when a target is known, otherwise `PWD: NAME`.
