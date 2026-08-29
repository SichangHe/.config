# per-agent sent mail

`omo_manager_mail_compress.py agent-unread` reads the Human mailbox and lists only mail still unread after the final metadata fetch. It prints no bodies. Gmail's Human-side unread flag is authoritative; sender-side Sent flags are not used.

New mail carries both its tmux target and immutable Codex session UUID. A current session sees its own mail plus legacy same-target mail without a session header. The JSON marks only current-session mail `trashable`; bound mail from an earlier agent that reused the pane is hidden, and legacy mail remains read-only.

To replace stale unread mail, send the new message with `email_me.py`, passing each source `message_id` back as `--supersedes-message-id`, and retain the printed replacement `Message-ID`. Then run `agent-trash-replaced` with each selected UID, the listed `source_uidvalidity`, that replacement identity, and `--yes`.

The mutation requires every source to remain unread in Inbox, belong to the current exact tmux pane and Codex session, retain its Gmail identity, and be named exactly by the replacement's `X-OMO-Supersedes` headers. The replacement must be newer, exist inside the configured agent-to-Human boundary, and carry the same tmux and session identities. Sources move only to recoverable Gmail Trash; the command never expunges. Explicit mail from another agent or session, legacy mail, read mail, changed mail, ambiguous or unrelated replacements, and stale UID identities are refused.

Before `MOVE`, the helper persists and directory-syncs an owner-only intent containing source identities but no bodies. A repeated identical command reconciles an interrupted move against Inbox and Trash and records a directory-synced terminal outcome. Mixed or missing outcomes refuse further mutation for manual inspection.
