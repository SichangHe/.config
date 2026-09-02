# Bounded legacy wake acknowledgment

`omo_legacy_wake_ack.py` records one durable acknowledgment for PB wake
`7e6094f6391c199f` on legacy task `202607/pbw_interpreter_live.md`. It is not a
general v1 compatibility mode and does not migrate or edit task files.

The command fails closed unless the caller pins the exact task bytes, status,
target, manager, ordered queue digest, exact TODO bytes/current row, sole active
target ownership, PB SQLite inode, reviewed handoff event, prompt digest, eight
terminal decisions, five terminal manager reports, and absence of those items
from current candidates. It holds task/target locks and a temporary SQLite
writer reservation so concurrent PB changes cannot cross receipt publication;
the reservation changes no rows. It revalidates before publishing the fixed
owner-private receipt. A byte-identical receipt is idempotent; any other
existing receipt is rejected.
