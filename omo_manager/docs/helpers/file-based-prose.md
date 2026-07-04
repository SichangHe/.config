# file-based prose

Manager-authored prose must live in a named file before a helper command is assembled. Command source should contain helper names, flags, paths, task refs, tmux targets, and machine-generated refs.

For new temporary prose, create a random private empty file with `mktemp "${TMPDIR:-/tmp}/omo-file.XXXXXX"` plus `chmod 600`, then write the text through an editor, `apply_patch`, or another non-shell text channel. Temporary human text belongs in private files under `${TMPDIR:-/tmp}`, not in repo-local scratch paths.

Human-facing email subjects must include the relevant task md filename so replies can route after manager compaction. Human email message bodies passed to `email_me.py` accept Markdown input, but plain text is preferred.

Pass files directly with `email_me.py --manager-human --subject-file SUBJECT --message-file BODY`, `omo_tmux_send.py --message-file PROMPT`, `omo_task.py --prompt-file PROMPT`, or `omo_report.sh --message-file REPORT_FILE` after allocating `REPORT_FILE` with `omo_report.sh --task-file TASK --alloc-message-file`.

Human-review note: review and approve this standing manager rule and consider adding the same invariant to broader AGENTS.md.
