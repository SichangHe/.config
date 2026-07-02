# manager mail compression workflow

Use this when unread manager-sent emails need to be compressed into a few topic emails.

Find this doc:
- manager helpers index: `~/.config/omo_manager/MANAGER_HELPERS.md`
- helper: `~/.config/omo_manager/omo_manager_mail_compress.py`

Workflow:
- inspect current instructions and the source task first
- run `omo_manager_mail_compress.py snapshot`
- create a fresh private export directory, for example `mktemp -d /tmp/manager-mail-compress.XXXXXX`
- run `omo_manager_mail_compress.py export --out-dir PRIVATE_DIR`
- use the exported `manifest.tsv`, `uids.txt`, and `UID.txt` files to group topics and write human-facing summaries
- classify any memo the human should read in whole as retained source mail; omit those UIDs from the superseded UID file
- write a task-private `superseded-uids.txt` containing only source UIDs fully replaced by the new summaries
- keep private bodies in `/tmp` or another owner-only scratch directory
- send replacement summaries with `email_me.py --manager-human --subject-file SUBJECT --message-file BODY`
- after replacements are sent, run `omo_manager_mail_compress.py trash-superseded --uid-file /tmp/manager-mail-compress-PRIVATE/superseded-uids.txt --yes`
- verify the helper reports `verify_remaining=0`
- write a task-linked audit with topics, counts, UID boundary, replacement subjects, and any skipped boundary
- report through `omo_report.sh`
- delete the private export directory after the task if the audit no longer needs raw local copies

Watcher trigger:
- `email_idle_watcher.py` records manager mail counts in `email-manager-mail-counts.tsv`
- it queues this workflow when unread manager mail is more than `16`
- the trigger is a manager work item only; replacement summaries and `trash-superseded` still require the explicit workflow above

Safety boundary:
- the helper reads the same Gmail/Himalaya config as `email_idle_watcher.py`
- snapshot/export search only `INBOX` unread messages from the configured self address with `[a]` or old `[omo_manager]` in the subject
- snapshot/export locally re-parse headers and skip boundary mismatches before exporting bodies
- `trash-superseded` only acts on an explicit UID list
- before moving mail, `trash-superseded` rechecks that each still-inbox UID is self-addressed manager mail
- export refuses a non-empty output directory so stale private body files are not mixed into a new run
- the helper moves only explicit superseded source mail to `[Gmail]/Trash`; it never expunges or permanently deletes message bodies
- use `email_me.py --manager-human --subject-file SUBJECT --message-file BODY` for replacement summary text

Judgment boundary:
- the helper does not choose topics or summarize email bodies
- the agent must decide what information remains useful to the human
- the agent must retain full-read memo emails instead of replacing them with compressed summaries
- omit low-level implementation trivia unless the human needs it for action or review
