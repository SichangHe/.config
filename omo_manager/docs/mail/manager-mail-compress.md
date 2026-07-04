# manager mail compression helper

`omo_manager_mail_compress.py` is the repeatable helper for compressing unread manager-sent mail. Use `snapshot` to list the current unread manager UID/header set, `export --out-dir PRIVATE_DIR` to write body files plus `manifest.tsv` and `uids.txt` into an owner-only local directory, classify full-read memos as retained source mail, then after replacement summaries are sent with `email_me.py --manager-human --sender-tmux-target OWNER_TARGET --subject-file SUBJECT --message-file BODY`, use `trash-superseded --uid-file PRIVATE_DIR/superseded-uids.txt --yes` to move only explicitly superseded source UIDs from `INBOX` to `[Gmail]/Trash`.

The helper reuses `email_idle_watcher.py`'s Gmail/Himalaya config, searches only `INBOX` unread self-addressed `[a]` mail plus old `[omo_manager]` mail, revalidates the boundary before moving mail, and never expunges or permanently deletes.

The watcher queues this path when unread manager mail is more than `16`; it does not summarize or move mail itself. Full workflow doc: `compression.md`.
