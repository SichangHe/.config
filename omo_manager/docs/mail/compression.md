# manager mail compression

Use this only to replace unread manager-sent mail with fewer topic summaries.
Follow [cleanup.md](cleanup.md)'s source-boundary, task-authority, evidence, recovery, and final-verification safeguards first. A threshold starts this review; it never authorizes compression or Trash by itself. Cleanup's unread-thread retention and `\Seen`-only move rules do not apply to a compression source that its recorded replacement fully supersedes.

Helper:

- `~/.config/omo_manager/omo_manager_mail_compress.py`

Workflow:

- inspect current instructions and source task, then run `omo_manager_mail_compress.py snapshot`
- create a fresh private export directory, for example `mktemp -d /tmp/manager-mail-compress.XXXXXX`, then run `export --out-dir PRIVATE_DIR`
- make a private UID map from `manifest.tsv`, `uids.txt`, and `UID.txt`; tie every proposed source UID to one replacement summary or an explicit retained reason
- group only fully replaceable topics; retain an unread report and any flagged, saved, read-later, full-read, or uncertain memo or report
- send replacement summaries with `email_me.py --manager-human --sender-tmux-target OWNER_TARGET --subject-file SUBJECT --message-file BODY`, and record their delivery before listing superseded UIDs
- write `superseded-uids.txt` with only fully replaced source UIDs, then run `trash-superseded --uid-file PRIVATE_DIR/superseded-uids.txt --yes`
- recheck final live state: every selected source left `INBOX`, retained sources remain, `verify_remaining=0`, and any external drift is separately recorded; keep recovery and paired receipts
- report only topics, counts, UID boundary, replacement identities, skipped boundaries, and verification; delete the private export directory when raw copies are no longer needed

Safety:

- keep private bodies in `/tmp` or another owner-only scratch directory; export refuses a non-empty output directory
- snapshot/export use the human mailbox config and only unread mail within the exact configured sender/recipient boundary; legacy self-addressed cleanup remains limited to historical `[a]` or `[omo_manager]` mail
- PB news, PB stock watch, and PB urgent threads are excluded from snapshot/export, Trash eligibility, and threshold counts, including unread counts
- the helper re-parses headers and skips boundary mismatches before exporting bodies; `trash-superseded` rechecks the same boundary in `INBOX`
- act only on the explicit UID list; move sources only to `[Gmail]/Trash`; never expunge or permanently delete message bodies
- the helper does not choose topics or summarize bodies; retain when human usefulness is uncertain
