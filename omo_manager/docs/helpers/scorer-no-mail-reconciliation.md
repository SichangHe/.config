# scorer no-mail reconciliation

(authored by agents unless marked 🧑)

`omo_scorer_pending_reconcile.py` is an incident-specific candidate for the five Human-authored items in `scorer_recovery_0926.md`. It does not send mail or interact with the scorer. It has two separate modes: `archive` can cancel only the archived-ownership item on the later Human correction; `operational` can resolve only the four remaining work items. Archive must be applied first. Both modes are disabled for real mutation while `APPROVED_RECEIPTS` is `None`.

- the caller must be the authenticated current `dw_submanager_65.md` manager, not a process that merely sets a task file or tmux environment variable
- the reviewed manifest must contain `mode`, a unique lowercase 64-character `nonce`, exact `task_sha256`, `manager_sha256`, `todo_sha256`, and `sources`, keyed by the required Human mail numbers, each with its exact pinned source `sha256` and Inbox `message_id`
- `--manifest-sha256` pins the manifest file; read-only preview prints the exact receipt digest; `--apply` additionally requires `--expected-receipt-sha256`, and an independently reviewed receipt pinned in the source
- the helper re-fetches exact authenticated Human Inbox MIME and already-Sent MIME, checks their original stored source text, frozen reply hashes, sender, recipient, date, reply thread and reference, then checks task/manager/TODO/source hashes under the existing task locks
- one atomic scorer task replacement removes only that mode's frozen items and appends a receipt with its nonce and Sent Message-ID; replay, prior nonce use, or any changed queue/source/task/TODO/manager aborts

Execution remains HOLD: an independent reviewer must approve an exact archive receipt before its pin may change from `None`. The four operational items need a *separate* item-by-item independent determination that the one cross-thread Sent reply answers each Human request; Inbox authentication and matching phrases alone do not prove that mapping. The manager's detached app-server tool must also have a supported authenticated task/pane identity; the helper will not accept an ambient `TMUX_PANE` or task-file claim. Do not run `--apply`, pin either receipt, remove queue items, resend mail, or stop the sole scorer based on this candidate alone.
