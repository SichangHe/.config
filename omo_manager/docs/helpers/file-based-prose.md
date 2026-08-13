# file-based prose

Manager-authored prose must live in a named file before a helper command is assembled. Command source should contain helper names, flags, paths, task refs, tmux targets, and machine-generated refs.

For new temporary prose, create a random private empty file with `mktemp "${TMPDIR:-/tmp}/omo-file.XXXXXX"` plus `chmod 600`, then write the text through an editor, `apply_patch`, or another non-shell text channel. Temporary human text belongs in private files under `${TMPDIR:-/tmp}`, not in repo-local scratch paths.

Human-facing email subjects must include the relevant task md filename so replies can route after manager compaction. Human email message bodies passed to `email_me.py` accept Markdown input, but plain text is preferred.

Pass files directly with `email_me.py --manager-human --subject-file SUBJECT --message-file BODY`, `omo_tmux_send.py --message-file PROMPT`, `omo_task.py --prompt-file PROMPT`, or `omo_report.sh --message-file REPORT_FILE` after allocating `REPORT_FILE` with `omo_report.sh --alloc-message-file`.

To launch a task with authoritative text selected from stored human email, use `omo_task.py --workdir DIR --prompt-file PROMPT --human-email-file manager_mail/FILE --human-email-lines START-END`. The prompt supplies the agent-authored goal tree; both email options are required and supply only exact human words. Relative paths resolve from `ROOT` and must remain inside `ROOT/manager_mail`; the line range is one-based and inclusive. The launcher validates the readable source and complete range before mutation, preserves the selected lines exactly, places them last inside `<human_instruction authoritative="true" source="FILE:START-END">`, and rejects a selected `</human_instruction>` delimiter. It stores that envelope in the task body and passes it to Codex through an owner-private temporary file that survives update retry and is removed after launch verification. Resume-idle recovery accepts neither a prompt nor human email instructions.

`omo_task.py` injects `WORKER_DEFAULTS.md` into prompted Codex and PCODX launches. It adds `VL_WORKER_DEFAULTS.md` for VL launches and `ROOT/MANAGER.md` for `--is-manager`, before custom `--prompt-file` text. The launcher wraps custom prompt text in `<manager_delegation from="MANAGER_TARGET">` and stores the same envelope in the task body. Exact human excerpts use a separate authoritative envelope and are never inferred from manager prose. `--resume-idle` is the explicit recovery exception and submits no prompt.

Human-review note: review and approve this standing manager rule and consider adding the same invariant to broader AGENTS.md.
Helper-delivered text originating from an agent is wrapped as `<agent_message from="SESSION:WINDOW">`. The `from` value identifies the live sending agent's canonical tmux window when available; `helper` means no live agent pane identity was available. Nested envelope tags in payloads are escaped. Watcher and system notices are not wrapped. The envelope is routing provenance, not human authority.
