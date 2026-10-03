# digest and email helpers

(authored by agents unless marked 🧑)

`omo_digest_queue.py` is the durable non-urgent digest path. `submit` appends digest items to the configured queue file, records absolute `queued-at`, and records absolute `published-at` when the source provides it or a relative `Published N ago` value can be resolved at queue time.

`deliver-once` sends queued items immediately when requested and renders absolute queued/published times; idle timing and recent-contact checks belong to a separate watcher.

PB news digest:
- PB watcher queues digest items into the manager repo queue at `/ssd1/sichangheagent/work_logs/manager_digest.md`
- append one Markdown item from stdin with `scripts/manager-digest append`
- preview delivery with `scripts/manager-digest deliver --dry-run`
- when the human asks, run `scripts/manager-digest deliver` immediately from `/ssd1/sichangheagent/work_logs`
- the delivery script clears the manager queue only after a successful send

`email_me.py --manager-human` sends from the configured agent Gmail account only to the configured human address. New manager-human subjects use `[TARGET]` when a target is known; legacy `[a]` and `[omo_manager]` tags are accepted only to preserve old threads. Pass a new subject with `--subject-file` or `--subject`. When both are omitted, the helper reuses the newest recent inbound or outbound thread tagged with the exact inferred tmux target, including reply headers; it fails when no matching thread exists. Positional subjects are refused so a body draft path cannot accidentally become the email subject.

Before sending, recent-thread lookup strips repeated `Re:` plus legacy `[a]`, `[omo_manager]`, `[omo]`, and leading tmux window/pane subject tags. When a match is found in the recent window, the outgoing subject becomes `Re: [TARGET] SUBJECT` and the message includes `In-Reply-To` and `References` headers from the matched self-sent mail. The first target in the authenticated thread fixes its route; a different agent cannot retag later replies. A thread with no target keeps the explicitly authenticated sender as its fallback.

Reply subject preparation strips old tmux or OmniGent producer tags before prepending the selected current bracketed tag. Normally omit `--tmux-target` and `--sender-tmux-target`. Attribution first authenticates OmniGent identity or resolves the tmux pane: an exact `TMUX_PANE` if available, otherwise the unique tmux pane whose process is an ancestor of the helper. A matching inherited agent target does not override the pane; an inherited target can only serve as a fallback outside detached Codex app-server calls. Detached Codex CLI tool processes often have no pane ancestor, even if a Codex TUI is visible in tmux: a copied `OMO_AGENT_TMUX_TARGET` or untargeted `tmux display-message` is not proof of their owner. Those calls must supply a separately verified `--tmux-target` for ordinary tagged mail; manager-human and completion mail additionally require authenticated owner evidence and fail closed. The visible subject tag prefers the unique active task filename without `.md` for both tmux and OmniGent producers; tmux senders without a known active task retain their authenticated target as a fallback. A zero-pane target such as `hcfg:1.0` is rendered as `hcfg:1`; nonzero panes keep their pane suffix. OmniGent producers keep durable identity `omnigent://SESSION_ID`, including a `.0` suffix that is part of the session id. Agent session identity for thread lookup is `CODEX_SESSION_ID`, else `CURSOR_CONVERSATION_ID`, else `CODEX_THREAD_ID`.

An old leading `[TASK.md]` tag is also stripped before retagging; it does not appear as a second tag beside the current task name.

For a sender with a valid agent-session ID, repeating a subject without `Re:` replies to the most recent same-subject mail sent by that session. This applies to tmux and OmniGent agents. The first use of a subject starts a new thread; lookup failure refuses delivery rather than silently splitting a conversation. Reply headers retain the parent Message-ID and References; the authenticated route and original task tag still govern delivery. Explicit digest authorization remains a fresh-thread request.

An explicit authenticated primary-route `Re:` continues that session's latest verified outbound thread, retaining its subject text even when the supplied reply title adds status wording. Ordinary workers with a current session, producer target, and split agent/human configuration use the same primary route without `--manager-human`; its exact addresses govern both lookup and SMTP delivery, without granting manager permissions. Exact-subject fallback remains bound to the current session: no prior current-session Sent mail means the first explicit reply fails rather than joining another session's thread. A nonreply new subject remains a fresh-thread request. Mail without an authenticated primary route, guest replies, and explicitly preserved recovery threads retain exact-subject selection; route mismatch, ambiguity, and lookup failure refuse delivery.

Omitting the subject also selects the current session's latest Sent thread for tmux and OmniGent agents. It does not fall back to an earlier agent's legacy window-tagged mail when current-session mail uses a task tag.

`omo_manager_setup_watchers.sh` loads `local.env`, exports the manager environment for pending watcher helpers, and passes explicit manager flags only to helpers that still require them. When the caller supplied a subject, lookup failures are non-critical and fall back to a normal `[TARGET] SUBJECT` send. An omitted subject fails closed because no safe fallback subject exists.

`email_me.py` sends a plain text fallback with Markdown links expanded to bare URLs, emits an email-compatible HTML alternative with escaped raw HTML, and renders list-containing bodies as normal HTML instead of wrapping the whole email in `<pre>`. Split agent-to-human mail omits the old `PWD` footer because the subject tmux tag identifies the sender and separate accounts prevent self-mail loops.
