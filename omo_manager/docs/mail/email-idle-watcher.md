# email idle watcher

`email_idle_watcher.py` reads the agent Gmail inbox. Untagged and reserved-tag Human mail is written to `manager_mail/UID.txt` and appended as a pending block. Mail whose subject has exactly one leading stable-agent-id tag, such as `[pb] ...` or `[main] ...`, is an AMH candidate: the watcher fetches Gmail thread metadata, and only then may load the AMH bridge. Missing Gmail thread id, unenabled or unsupported agent ids, and new AMH-tagged mail without `--manager-target` stay on the current-manager pending path and do not create an AMH commit. After AMH commits a source, later launch, metadata, or subject-route failure remains AMH replay or manual recovery instead of falling back.

It accepts any subject only from the exact configured human address; split-account mode also requires the Gmail transport sender and SPF identity. Addressed mail for an active task is appended to that task; unaddressed current-manager mail uses the current `work_manager_YYYY-MM-DD.md`. Normal current-manager mail intake is complete once the Markdown pointer is durable, and `omo_pending_watch.py` owns delivery. The email watcher never sends raw `pending: file=...` references for normal mail. AMH-owned mail does not write a `manager_mail` pending block.

Legacy self-addressed manager-thread replies may end in a final `PWD: NAME` footer and are ignored instead of being surfaced back to the manager as inbound pending work. The watcher recognizes that final unquoted footer and already-sent `tmux` footers; quoted footer text and messages whose display name is `Human` remain eligible as human replies. Split agent-to-human mail omits this footer because it cannot loop into the agent inbox. Ignored legacy self-authored echoes are also excluded from manager-mail threshold counters.

It keeps IMAP IDLE as the push path, runs an unread pull scan every `--pull-interval-s` seconds, default `600`, and exits after `--idle-exit-after-s` quiet seconds, default `3600`, so the setup supervisor refreshes it. One-shot `--once` scans flush the async queue before process exit.

Legacy self-addressed mode retains `[a]` and `[omo_manager]` threshold checks for already-sent mail. Split-account mode watches only the agent inbox; human-mail cleanup remains available explicitly through the separate human mailbox config and `omo_manager_mail_compress.py`.

Split-account manager-mail checks queue the singular compression owner when non-PB Inbox mail exceeds 29 messages. After that marker is consumed, each additional message above the limit retriggers cleanup. Independent unread-growth and 24-hour-volume triggers remain early-warning paths. These triggers only start the documented compression workflow; they do not bypass its replacement, review, or recoverable-Trash gates.

The processed-UID record is authoritative even after its source task is archived. Only UIDs explicitly recorded as unaccepted delivery attempts are retried. UID state is namespaced by agent inbox because Gmail UID numbers are mailbox-local.

New human email pending blocks write `(record and delegate manager_mail/UID.txt)`. Legacy `(from email manager_mail/UID.txt)` and `[source: email manager_mail/UID.txt]` remain recognized for historical duplicate detection but should not be written for new email blocks.

Stored `manager_mail/UID.txt` files keep the body and a normalized subject, omitting redundant self `From`, `Date`, and `UID` headers because the source marker/file name already identify the message. Legacy human `Re: [a] ...` and `Re: [omo_manager] ...` subjects are stored as `Re: ...` so task-file matching sees the same subject shape the manager originally used.

Leading full tmux window or pane subject tags such as `wl:9`, `[pb:1]`, and stacked `Re: wl:9 wl:6 ...` forms are routing metadata and are stripped before storage or pending presentation. Window targets and zero-pane targets are aliases, so `hcfg:1` and `hcfg:1.0` match the same task. An active addressed task receives only `(pending)` and the email source pointer. The watcher does not write delivery-route metadata; `omo_pending_watch.py` chooses `runat` or `managerat` from the task and message content.

`omo_pending_watch.py` remains the durable delivery path for pending Markdown refs.
