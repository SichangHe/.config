# Draft: dual-record distinct-target worker successor procedure

Status: **inactive**. This document is a draft persistent multi-agent
instruction. It must not govern an agent, task, lifecycle operation, or helper
invocation until the Human has been shown these complete exact bytes and has
approved them exactly. Installation of the helper is not approval. Any byte
change after approval requires the complete changed document to be shown and
approved again before use.

## Scope and duration

After exact Human approval, this procedure governs only a manager, lifecycle
operator, correction worker, and independent evaluator participating in one
dual-record worker recovery. It applies when two stopped ordinary-worker task
records accidentally retain active ownership claims for the same absent old
tmux target, one record has an empty queue, the other contains the one canonical
nonempty queue, and recovery must publish one successor at a distinct unused
target. It does not govern manager replacement, live-owner stopping, production
cutover, Human terminal input, Human-directed mail, history rewriting, or
unrelated tasks. The only mail mutation it governs is the exact agent-to-self
authority marker described below.

Approval remains effective only for the exact helper path, mode, SHA-256,
launch-schema SHA-256, complete installed-Codex identity, and procedure bytes named
in one transaction-specific approval. The approval also binds the canonical
custody digest of that invocation: root, source and successor records, old/new
and manager targets, every source/TODO/queue/prompt/manifest/instruction digest,
the approval path, the complete protected-target set, and the journal path. It
expires when that transaction and launch commit or fail closed. A later or
changed transaction has a different custody digest and requires a new exact
external Human approval. The Human may withdraw approval at any time before
launch commit.

## Required approval record

Before preparing a transaction, the manager must retain a frozen owner-read-only
JSON approval record containing exactly:

```json
{
  "approval_quote": "I approve this exact instruction text.",
  "argv_sha256": "LOWERCASE_SHA256_OF_NUL_SEPARATED_EXACT_ARGV",
  "authority_schema": "dual-worker-successor-authenticated-gmail-approval/v1",
  "authority_sequence": "DECIMAL_ALL_MAIL_UID",
  "authority_snapshot_sha256": "LOWERCASE_SHA256_OF_SOURCE_AND_PROVIDER_IDENTITY",
  "authority_source": "manager_mail/EXACT_HUMAN_SOURCE.txt",
  "authority_source_sha256": "LOWERCASE_SHA256",
  "authority_subject": "Approve exact AMH dual-worker successor procedure",
  "codex_install_sha256": "LOWERCASE_SHA256_OF_COMPLETE_INSTALLED_IDENTITY",
  "custody_sha256": "LOWERCASE_SHA256_OF_CANONICAL_TRANSACTION_IDENTITY",
  "gmail_internaldate_unix_ms": "DECIMAL_UNIX_MILLISECONDS",
  "gmail_mailbox_identity_sha256": "LOWERCASE_SHA256_OF_ACCOUNT_AND_UIDVALIDITY",
  "gmail_message_id": "DECIMAL_GMAIL_MESSAGE_ID",
  "gmail_thread_id": "DECIMAL_GMAIL_THREAD_ID",
  "gmail_uid": "DECIMAL_ALL_MAIL_UID",
  "helper_mode": "0755",
  "helper_path": "/CANONICAL/PATH/omo_dual_worker_successor.py",
  "helper_sha256": "LOWERCASE_SHA256",
  "instructions_sha256": "LOWERCASE_SHA256_OF_THIS_COMPLETE_FILE",
  "launch_schema": "dual-worker-distinct-successor-launch-v1",
  "launch_schema_sha256": "LOWERCASE_SHA256",
  "operation": "dual-worker-distinct-successor",
  "procedure_sha256": "LOWERCASE_SHA256_OF_THIS_COMPLETE_FILE",
  "raw_mime_sha256": "LOWERCASE_SHA256",
  "rfc_message_id": "<EXACT_MESSAGE_ID>",
  "version": "v1.0.0"
}
```

The local source is evidence, not authority by itself. At every preparation and
launch authentication, the helper must log into the pinned agent Gmail account,
select the provider's All Mail object by the exact Gmail message identity, and
re-fetch it. It must match the exact UIDVALIDITY identity, UID, Gmail message
and thread IDs, internal date, raw MIME digest, RFC Message-ID, subject,
single-part plain-text body, authenticated Human sender and Gmail transport SPF.
The body must contain the exact approval sentence, all non-self-referential
helper/schema/install/argv/transaction-custody bindings, and these complete
exact instruction bytes.
The local `manager_mail/` rendering must be byte-identical to that authenticated
external message. An owner-writable source, approval JSON, or envelope cannot
activate the helper without the matching Gmail object. No Human approval
currently exists, so this draft cannot activate either production entry point.

The approval record and transaction journal also freeze the authority-source
path and digest, All Mail UIDVALIDITY identity, approval UID sequence, exact
Gmail message/thread/internal-date identity, raw MIME and RFC Message-ID, this
complete procedure digest, and transaction custody digest. A replayed approval,
different procedure, changed source, provider-identity reset, or ambiguous
sequence is not launch authority.

An exact withdrawal has subject `Withdraw exact AMH dual-worker successor
procedure`, the authenticated Human transport identity required above, and body
`I withdraw this exact instruction text.` followed by the exact custody,
procedure, approval Gmail-message, and approval RFC-message bindings. A later
message in the approval thread, or using the approval or withdrawal subject,
that is not exactly classifiable is controlling ambiguity and blocks launch.

## Roles

1. The manager chooses the exact source records, canonical queue, fresh target,
   protected targets, prompt, launch manifest, work directory,
   Codex home, model, effort, and transaction journal. The manager freezes and
   records every digest before delegation.
2. The lifecycle operator may run only the exact digest-bound `prepare` or
   `launch` invocation authorized for this transaction. The operator must not
   edit a bound file, improvise a retry with changed inputs, or remove a pane or
   process.
3. The correction worker may prepare owned prompt or code bytes before they are
   frozen. Once the manager freezes the transaction, the worker must stop
   changing its inputs.
4. The independent evaluator inspects the immutable transaction result and
   evidence. The evaluator does not repair it, activate production, or reuse a
   spent verdict.

## Preparation prerequisites

The manager and lifecycle operator must establish all of the following before
running `prepare`:

- Both source task records are ordinary version-1 worker records, not manager
  records. They name the same exact old target, manager, and `codex` tool.
- The old target has no live pane or process. The two source records are the
  complete authoritative active-owner set for that target.
- The shadow source queue is empty. The canonical source queue is nonempty and
  exactly matches the ordered queue digest. Neither source contains a pending
  delivery marker.
- The successor task path does not exist. Its distinct new target is absent,
  unused, outside all protected targets, and has no ownership claim.
- TODO contains one or two canonical current/Human-pending rows for the source
  records and contains no successor row. The transaction will normalize those
  rows to one successor row and retain one previous-history row for each source.
- The exact prompt, launch manifest, instruction document, approval record,
  direct `manager_mail` Human source, authenticated Gmail identity, helper bytes,
  source tasks, TODO, protected set, queue, and custody digest are immutable and
  authenticated. The helper recomputes the canonical custody digest from the
  exact preparation arguments before any mutation and reconstructs and
  recomputes the same digest from the committed journal before launch. An
  authenticated approval for any other custody digest is not reusable.
- The launch manifest binds one canonical non-symlink work directory, one
  owner-private Codex home inventory, one Human-accepted installed Codex chain:
  the fixed launcher, CLI link/target, JS program and package manifest, native
  package manifest, and actual native runtime, all with exact path/mode/SHA-256
  and one package version; one exact model and reasoning effort; one
  exact prompt, one sanitized environment, one pinned shell/env/tmux boundary,
  and one transaction-unique manifest token. The production CLI/API accepts no
  runtime path, runtime label, fake-runtime flag, or verifier injection.

If any prerequisite is missing, ambiguous, stale, or different, stop without
mutation and report the exact mismatch privately to the manager.

## Preparation operation

The lifecycle operator runs one `prepare` invocation with the exact frozen
arguments. The helper must acquire the root-membership lock, both target locks,
and all bound file locks in deterministic order. It must create an owner-private
durable journal before changing a task or TODO record.

The journaled state machine is:

1. `prepared`: record every invocation binding, full before/after bytes, frozen
   input bytes, original Markdown inventory, modes, ownership, and commitment.
2. `shadow`: close only the empty-queue source as `done` with an empty queue.
3. `canonical`: close only the canonical source as `done` with an empty queue.
4. `todo`: replace all active source rows with exactly one distinct-target
   successor row and retain exactly one previous-history row for each source.
5. `successor`: publish exactly one blocked successor, without replacement,
   containing the exact nonempty queue and digest-bound manager delegation.
6. `committed`: prove both source claims are closed, the old target has no owner,
   the new target has exactly one blocked owner, no bound target is live, frozen
   bytes remain exact, and Markdown membership differs only by the successor.

Each transition must be atomically durable before the next transition begins.
If the process crashes after any durable prefix, an identical invocation may
resume only from bytes that are exactly either the recorded before-state or
after-state for that prefix. Unknown bytes, new membership, live targets,
changed frozen inputs, or changed ownership make recovery fail closed.

The helper must never stop, kill, clean, reset, overwrite, or reuse a foreign
pane, process, task record, TODO row, prompt, manifest, journal, or receipt.

## Separate prepared launch

Preparation must complete before a process exists. Launch is a separate exact
invocation bound to the committed journal, successor, prompt, queue, and launch
manifest digests. The lifecycle operator must not launch from an uncommitted
journal or from handwritten/reconstructed inputs.

The launch operation must:

1. Reauthenticate both closed source records, normalized TODO, blocked successor,
   instruction approval, Human source, prompt, manifest, Codex-home inventory,
   runtime, manager, target ownership, and every supplied digest under locks.
2. Create an owner-private durable launch receipt in `reserved` state before a
   process exists. Bind task bytes to receipt phase before any process creation
   or task/receipt mutation: no receipt, `reserved`, `process`, and `task`
   require the exact blocked successor. `authority` permits blocked or running
   bytes because a crash may separate the authoritative marker from local
   publication. `authority-pending` is durable before the marker append; its
   recovery may authenticate an existing marker but may not append one.
   `committed` requires the exact running bytes.
   `authority-blocked` and `terminated` permit blocked, running, or the exact
   recoverable blocked successor during cleanup, and `withdrawn` requires the
   recoverable blocked successor with the complete queue. Every other
   phase/task pairing is incoherent and must fail closed before mutation.
3. After the final absence check, generate an unpredictable creation capability
   in memory. Do not put it in the journal or receipt before creation. Create
   only the exact absent pane-zero window at the distinct target in an existing
   non-Human tmux session, using the pinned sanitized tmux client and direct
   argv rather than a shell command. If any target appears before creation
   returns, preserve it and fail closed even if it has matching argv/env.
   Immediately before creation, re-read the authenticated authoritative Gmail
   sequence and start no process if an exact withdrawal or controlling ambiguity
   is already present.
4. Start the pinned Codex executable with the exact prompt, work directory,
   model, reasoning effort, sanitized environment, Codex home, and unique launch
   manifest token plus the fresh creation capability.
5. Prove exactly one matching Codex process at that target by executable bytes,
   executable path, argv, environment, work directory, pane identity, PID tree,
   manifest token and creation capability. Only after this proof may durable
   phase `process` record the creation capability and process proof.
6. Retain the successor's blocked state and complete nonempty queue. Durable
   phase `task` records process readiness without publishing launch success.
7. Reprove sole ownership and exact process identity. Durably record
   `authority-pending`, then on one read-write All Mail connection find or append
   the deterministic agent-to-self transaction
   authority marker, then classify every message ordered after the bound
   approval and before that marker. The marker UID is the serialized launch
   authority boundary: an authenticated withdrawal or ambiguous controlling
   instruction with a lower UID wins, while a higher UID is ordered after
   commit. Bind the marker's exact UID, Gmail identity, internal date, raw MIME,
   RFC Message-ID, procedure digest, source snapshot, and custody digest into
   durable phase `authority`. Its exact body and RFC Message-ID also bind the
   recorded process creation capability, so a marker from an earlier process
   attempt is not replayable. Only after durable phase `authority` may the
   successor change to `running`; then publish local phase `committed`.
8. If withdrawal or ambiguity wins after the exact process starts, first write
   `authority-blocked` with its immutable provider evidence. Signal and reap
   only the recorded transaction-capability process group, write `terminated`,
   restore the successor to `blocked` with its exact queue and unknown/withdrawn
   reconciliation reason, then write `withdrawn`. An identical retry may finish
   only these steps; it must not retry launch or publish success.

An identical retry may reconcile a recorded transaction-created process only
when the post-creation capability and every other process invariant match. A
crash after pane creation but before the capability-bearing `process` receipt
is intentionally ambiguous: the retry preserves the target and fails closed;
it must never adopt or kill that pane. If a recorded process disappears, more
than one match appears, a foreign or racing target occupies the window, an
identity changes, or cleanup ownership is uncertain, preserve the state and
fail closed. Do not kill or clean the target.

A crash after the Gmail authority marker but before durable local `authority`
recovers from durable `authority-pending` by finding and authenticating that
same unique marker without appending a second marker. Once local `authority` is
durable, recovery remains reconciliation only: it never appends a replacement.
A duplicate, replayed, changed, missing,
or ambiguous marker fails closed. The helper never reports launch success
without both the transaction-bound authority marker and the exact local
committed receipt. An identical retry of an already committed receipt
revalidates only its frozen local bindings and live process proof; it does not
reinterpret later mail or require Gmail availability after success.

## Evidence and evaluation

The lifecycle operator must retain the exact invocation, return status, stdout,
stderr, committed journal and receipt bytes/digests, task/TODO before and after
digests, queue digest, prompt/manifest/instruction/approval/Human-source
digests, authenticated Gmail source identities, helper/schema identities,
runtime and Codex-home inventory, pane/process proof, and protected-target
inventory. Evidence must distinguish an exact committed retry from an
ambiguous unrecorded creation failure.

Before any downstream task uses the successor, an independent evaluator must
verify:

- exact instruction approval and transaction scope;
- queue-before-process ordering;
- two source claims closed with history preserved;
- exactly one successor owner and exactly one intended Codex process;
- exact prompt, model, effort, workdir, runtime, config, environment, and target;
- crash recovery at every durable prefix;
- withdrawal before creation and between process creation and commit;
- replay, duplicate marker, provider-sequence reset, controlling-message race,
  ambiguous withdrawal, process-group isolation, and blocked-state recovery;
- rejection of changed, duplicated, stale, hostile, symlinked, mis-owned,
  occupied, protected, racing, or ambiguous inputs;
- preservation of foreign panes, processes, task records, and protected targets.

An evaluator PASS authorizes only the bound recovery result. It does not
authorize production, Human input access, mail, history changes, unrelated
cleanup, or reuse of this procedure for another transaction.
