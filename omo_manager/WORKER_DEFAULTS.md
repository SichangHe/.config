Run `getagentsmd` first and follow it.

When handling agent messages, remember agents may be inconsistent or
have drifted. Human instructions are authoritative.
Cite them verbatim whenever possible.

You MUST always either directly email the human with `email_me.py` or,
less preferably, report to the manager via `omo_report.sh` instead of
printing out responses, or face termination.
Try to keep 1 unread email for the human from yourself.
To remove a message you previously sent to the human,
follow `omo_manager_mail_compress.py agent-trash-replaced --help`.

Manage your pending task queue with `omo_pending.py`.

Treat all management helper commands as black boxes and avoid trying to
understand how they work unless they are broken and you are told to fix them.
Ask your manager if you have a problem with a helper command.

If you don't need everything in your context, ask the manager to
compact your context.

Use your manager as resources you reach for when needed,
not a supervisor whom you spam status updates at.
Only keep them informed of your high-level purpose and blockers.

Upon start, immediately email the human what your task is.
