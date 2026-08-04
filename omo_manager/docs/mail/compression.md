# manager mail compression

Use this only to replace unread manager-sent mail with fewer topic summaries.
Follow [cleanup.md](cleanup.md)'s source-boundary, task-authority, evidence, recovery, and final-verification safeguards first. A threshold starts this review; it never authorizes compression or Trash by itself. Cleanup's unread-thread retention and `\Seen`-only move rules do not apply to a compression source that its recorded replacement fully supersedes.

Helper:

- `~/.config/omo_manager/omo_manager_mail_compress.py`

Workflow:

- inspect current instructions and source task, then run `omo_manager_mail_compress.py snapshot`
- run `omo_manager_mail_compress.py identity-preflight` before export; it uses the same configured human-mail IMAP authentication as `snapshot`, reports only aggregate Gmail identity and complete-thread evidence, exits nonzero on `gate=block`, and retains every source without mutation
- use `OMO_HUMAN_EMAIL_CONFIG_PATH` as the default authentication path for compression; this workflow requires no separate Gmail REST API, OAuth token, browser profile, sign-in, or application-default credential
- require the configured IMAP session to expose unique `X-GM-MSGID` and `X-GM-THRID` values for every selected source and complete thread membership through the discovered `\All` mailbox; missing, duplicate, conflicting, or incomplete evidence blocks the gate
- create a fresh private export directory, for example `mktemp -d /tmp/manager-mail-compress.XXXXXX`, then run `export --out-dir PRIVATE_DIR`
- export reruns the same configured-mailbox, identity, and complete-thread gates and records IMAP Gmail message/thread IDs, flags, labels, a full-message digest, and source UIDVALIDITY in the private map
- make a private UID map from `manifest.tsv`, `uids.txt`, and `UID.txt`; tie every proposed source UID to one replacement summary or an explicit retained reason
- maintain one private task-wide original-Gmail-thread ledger across rolling frozen batches; send at most one replacement per original thread and retain any changed or previously handled thread whole
- group only fully replaceable original Gmail threads; retain an unread report and any flagged, saved, read-later, full-read, out-of-scope, incomplete, or uncertain memo or report
- send replacement summaries with `email_me.py --manager-human --sender-tmux-target OWNER_TARGET --subject-file SUBJECT --message-file BODY`, and record their delivery before listing superseded UIDs
- write `superseded-uids.txt` with only fully replaced source UIDs, then run `trash-superseded --uid-file PRIVATE_DIR/superseded-uids.txt --yes`
- pass `--ignore-important-label` only when authoritative human text explicitly says Gmail `Important` alone is not a retention reason; this never overrides starred, flagged, saved, read-later, thread-context, unresolved, human-pending, full-read, or uncertain retention
- immediately before every IMAP move, rerun configured-mailbox authentication, source identity, complete-thread membership/content/label checks, source UIDVALIDITY/content/flag checks, and protected-intent checks; retain and replan only an affected thread on drift
- recheck final live state after every batch through the same IMAP connection: select Trash and refetch every member of each moved thread there, require membership and content to remain exact and no source to retain `\Inbox` (Gmail may omit its `\Trash` token from `X-GM-LABELS`), reject any new protected intent, verify every selected source left `INBOX`, verify retained complete threads remain unchanged and replacements exist, reconcile counts with `verify_remaining=0`, and separately record any external drift; keep ordered recovery and paired intent/outcome receipts
- continue with later frozen batches until a final live scan has no eligible source absent from the task-wide ledger; ordinary later arrivals belong to later batches
- report only topics, counts, UID boundary, replacement identities, skipped boundaries, and verification; delete the private export directory when raw copies are no longer needed

Safety:

- keep private bodies in `/tmp` or another owner-only scratch directory; export refuses a non-empty output directory
- snapshot/export use the human mailbox config and only unread mail within the exact configured sender/recipient boundary; legacy self-addressed cleanup remains limited to historical `[a]` or `[omo_manager]` mail
- PB news, PB stock watch, and PB urgent threads are excluded from snapshot/export, Trash eligibility, and threshold counts, including unread counts
- the helper re-parses headers and skips boundary mismatches before exporting bodies; `trash-superseded` rechecks the same boundary in `INBOX`
- act only on the explicit UID list; move sources only to `[Gmail]/Trash`; never expunge or permanently delete message bodies
- the helper does not choose topics or summarize bodies; retain when human usefulness is uncertain
