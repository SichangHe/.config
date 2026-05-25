# Manager Markdown pending interface

Markdown files under the work-logs root are the durable queue for manager-visible messages. Email watchers, agent reports, and manual manager notes must preserve the message in the relevant `.md` file. For ordinary agent/helper messages, the pending Markdown block plus `omo_pending_watch.py` is the single manager notification path; do not also send a separate live manager push after writing a durable pending/report block.

Pending message blocks use this forward format:

```md
(pending)
(from email manager_mail/UID.txt)
[source: email manager_mail/UID.txt]
[summary: human reply to manager]
```

```md
(pending)
(from agent AGENT via omo_report.sh status=STATUS)
(report manager YYYY-MM-DD HH:MM agent=AGENT status=STATUS)
[message-sha256: SHA256_OF_MESSAGE_FILE]
message:
> report body copied from MESSAGE_FILE
```

The parenthesized `(from ...)` line is the human-readable source marker. The legacy `[source: email ...]` line remains valid and must stay supported for historical email blocks and duplicate-UID mitigation. Helpers may add extra metadata lines after the source marker, but must not weaken sender checks, recovery-email checks, or duplicate/idempotency protections. Direct manager pushes are reserved for concrete reliability exceptions where Markdown plus watcher cannot work; document the exact exception before using one. Agent report helpers should not append a second unresolved pending block with the same agent, status, and message hash.

Manager rule: when an agent has no immediate task, is about to exit, or is stopped after completion, ask for concise feedback on instructions, communication, and manager coordination. Preserve useful task-specific feedback in that task/repo Markdown; preserve generally useful feedback in manager docs, the manager tracker, or a dedicated manager feedback section/file.
