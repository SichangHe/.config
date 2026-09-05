# Namespace drain

`omo_namespace_drain.py` closes a Human-authorized set of non-Human agent
namespaces without per-task completion mail. It is deliberately a two-step
transaction.

First create a complete immutable plan. The authority file must have the
supplied SHA-256 and contain the exact namespace-close directive. Planning
rejects `h*` prefixes or targets, inventories every root task record whose
`runat` belongs to a requested prefix, preserves its status, blocker, queue,
and ownership, and records each shared target only once. It canonicalizes a
window target as pane zero and accepts it only when tmux resolves that exact
session, window, and pane; tmux's fallback to another window is not evidence
that a missing historical target is live.

```text
omo_namespace_drain.py plan \
  --root /absolute/work_logs \
  --prefix opsmail0802 --prefix agent_managers \
  --authority-file /absolute/work_logs/manager_mail/85c5dff58359-1261.txt \
  --authority-sha256 SHA256 \
  --preparer PREPARER_TARGET \
  --output /absolute/private/drain-plan.json
```

An independent reviewer outside both drained namespaces and distinct from
the preparer and executor must inspect that exact plan and produce a 0600 JSON
receipt with schema `omo-namespace-drain-review/v1`, the exact
`plan_sha256`, verdict `PASS`, its task SHA-256, and its exact `reviewer`
target. Apply authenticates that task owner before accepting the receipt.

```text
omo_namespace_drain.py apply \
  --plan /absolute/private/drain-plan.json --plan-sha256 SHA256 \
  --review /absolute/private/review.json --review-sha256 SHA256 \
  --reviewer-task /absolute/work_logs/REVIEWER.md \
  --reviewer-task-sha256 SHA256 --executor EXECUTOR_TARGET \
  --consumed-review-receipt ~/.local/state/omo-manager/report-receipts/CONSUMED.json \
  --consumed-review-receipt-sha256 SHA256 \
  --ledger /absolute/private/consolidated-ledger.json
```

Apply revalidates authority, every task byte, and every target state before
the first mutation. A root-scoped exclusive transaction lock prevents two
drains from applying concurrently. It serializes shared-target stops, suppresses completion
mail, preserves the original task metadata and queues in the durable ledger,
parks drained records in the supported blocked/retired terminal form, and verifies that no planned live target or active
`runat` remains. It also enumerates the namespace directly so an untracked
live pane cannot escape the plan or final verification. The immutable ledger
is never rewritten; completion or partial-failure state is written beside it
as `<ledger>.progress.json`, including the ledger digest and stopped-target
set for reviewed recovery. If a task-write phase fails, the helper restores
only records still matching its exact replacement bytes and records any
foreign-drift rollback blocker. Stopped panes are never restarted
automatically. Never rerun blindly;
create a fresh plan from current state.

The consumed receipt must be the canonical output of `omo_report.sh
--verify-consumed` for the exact review draft. The drain resolves the
reviewer's task target to its live pane and reruns that supported verifier
with the reviewer-bound route, work-log root, and separately valid report
agent identity. A caller-created JSON file, even with a matching public
attestation hash, is insufficient.

## Exact exited-shell prerequisites

Planning still rejects every `not_codex` target. Two narrowly fixed
reconciliation operations remove the two known exited-shell blockers before a
new full plan is created. They are unavailable until the independent
`ns_code_review.md` owner returns a consumed 0600 JSON PASS with schema
`omo-namespace-drain-code-review/v1`. That receipt binds the exact helper,
test, documentation, reviewer-task, reviewer-target, and report-agent bytes.
The caller must be an authenticated live non-Human pane outside both drained
namespaces. Binding and reconciliation must run from the same caller pane.
The canonical review object has exactly the following relevant identities;
`omo_report.sh` must consume that exact file before either binding command:

```json
{
  "schema": "omo-namespace-drain-code-review/v1",
  "verdict": "PASS",
  "helper_sha256": "SHA256",
  "tests_sha256": "SHA256",
  "documentation_sha256": "SHA256",
  "reviewer": "cedit:28",
  "reviewer_task": "ns_code_review.md",
  "reviewer_task_sha256": "SHA256",
  "report_agent": "REPORT_AGENT"
}
```

The taskless operation is fixed to `opsmail0802:0.0`:

```text
omo_namespace_drain.py bind-taskless-shell \
  --root /ssd1/sichangheagent/work_logs \
  --code-review /absolute/private/code-review.json \
  --code-review-sha256 REVIEW_SHA256 \
  --consumed-code-review-receipt /absolute/report-receipts/CONSUMED.json \
  --consumed-code-review-receipt-sha256 CONSUMED_SHA256 \
  --executor EXECUTOR_TARGET \
  --output /absolute/private/taskless-shell-binding.json

omo_namespace_drain.py reconcile-taskless-shell \
  --root /ssd1/sichangheagent/work_logs \
  --code-review /absolute/private/code-review.json \
  --code-review-sha256 REVIEW_SHA256 \
  --consumed-code-review-receipt /absolute/report-receipts/CONSUMED.json \
  --consumed-code-review-receipt-sha256 CONSUMED_SHA256 \
  --executor EXECUTOR_TARGET \
  --binding /absolute/private/taskless-shell-binding.json \
  --binding-sha256 BINDING_SHA256 \
  --audit-output /absolute/private/taskless-shell-audit.json
```

`reconcile-taskless-shell` is intentionally frozen in the current helper. The
supported filesystem route cannot distinguish a legitimate hard-link ctime
transition from hard-link plus same-inode write/restore with restored mtime
using the available Linux stat fields. The operation therefore fails closed at
the final pre-removal boundary, before `guarded_remove_shell()`, unless a sealed
execution primitive supplies a durable generation proof for phase publication.

Binding scans every task record under the membership lock and requires zero
records for the exact target. Parsed numeric aliases are canonicalized, and a
malformed record claiming the `opsmail0802` namespace is refused, including
zero-padded aliases such as `opsmail0802:00.00`, even when a tagged folded YAML
scalar places that alias on the following line or a `runat` alias references an
earlier anchor. Anchored merge mappings, any explicit YAML mapping key in a
malformed record, and all tab-indented malformed `runat` keys or continuations
fail closed. Any target-namespace claim in frontmatter that cannot be composed
as YAML also fails closed, including escaped quoted scalars decoded from YAML
tokens before a later syntax error.
Binding captures the exact pane and the observed idle nested-shell topology:
the tmux-owned zsh session/process-group leader and its sole foreground fish
child. It binds both process generations, commands, command lines,
executables, session/process-group/foreground-terminal relationships, tty,
the absence of grandchildren, pane dimensions/history/cursor, working
directory, host, and capture digest. For `opsmail0802:0.0`, the only accepted
transcript shape is the bound two-line fish redraw: one history prompt and one
current prompt, each beginning with the exact prompt glyph, containing only
whitespace before the same host/absolute-workdir suffix, with cursor `(2,0)`.
Command text, output, a differing suffix, extra history, or layout drift is
refused.

Reconciliation repeats the complete proof, opens pidfds for both generations,
revalidates the tree before signalling, stops the foreground child and then
the root, and compares a dedicated frozen-tree snapshot in states `T/t`. One
tmux server predicate binds the symbolic slot, exact pane ID, pane PID, pane
index, and foreground command before the exact-pane kill. A byte-exact receipt
classifies acceptance; a timeout, nonzero client result, truncated output, or
rebound target is an irreversible unknown unless the exact frozen pane is
proved unchanged. Both processes are resumed in every cleanup path and an
accepted/unknown removal retains both pidfds until their exact generations are
proved exited. It has no task-write or mail path.

The completed-task operation is fixed to `amh1232_term_eval.md` and
`agent_managers:39.0` / pane `%1855`:

```text
omo_namespace_drain.py bind-completed-shell \
  --root /ssd1/sichangheagent/work_logs \
  --code-review /absolute/private/code-review.json \
  --code-review-sha256 REVIEW_SHA256 \
  --consumed-code-review-receipt /absolute/report-receipts/CONSUMED.json \
  --consumed-code-review-receipt-sha256 CONSUMED_SHA256 \
  --completion-receipt /absolute/completion-email-delivered/MESSAGE_KEY \
  --completion-receipt-sha256 COMPLETION_SHA256 \
  --executor EXECUTOR_TARGET \
  --output /absolute/private/completed-shell-binding.json

omo_namespace_drain.py reconcile-completed-shell \
  --root /ssd1/sichangheagent/work_logs \
  --code-review /absolute/private/code-review.json \
  --code-review-sha256 REVIEW_SHA256 \
  --consumed-code-review-receipt /absolute/report-receipts/CONSUMED.json \
  --consumed-code-review-receipt-sha256 CONSUMED_SHA256 \
  --executor EXECUTOR_TARGET \
  --binding /absolute/private/completed-shell-binding.json \
  --binding-sha256 BINDING_SHA256 \
  --audit-output /absolute/private/completed-shell-audit.json
```

Binding requires the exact blocked, non-manager, queue-empty task; its sole
current TODO row; one active task at the target; one live manager record; the
canonical delivered-completion marker and accepted Message-ID; pane `%1855`;
and a stable Git dirty-path/index manifest. The protected untracked names `0`
and `0{n++}` are represented only by their exact NUL-delimited porcelain
status/path bytes; the helper must not stat, open, resolve, read, stage, rename,
or remove them. Every other dirty entry and the index are captured through
no-follow descriptors and a descriptor-bound Git work-tree/index route. The
sole-manager proof applies the
same conservative malformed `runat` namespace guard, including quoted, spaced,
tagged, folded, anchored, aliased, and zero-padded forms. Its valid active-task scan also uses canonical
numeric target equality, so a second record at `agent_managers:039.00`
conflicts with `agent_managers:39.0`. It stores the complete task and
TODO source and supported replacement bytes.

Both prerequisite operations use a deterministic binding-bound staged audit
and phase journal (`prepared`, `kill-started`, `pane-removed`,
`files-exchanged`, `complete`) in one pinned owner-private output directory.
The staged audit and every future phase object are fsynced before `prepared`;
each phase promotion hard-links the already-held staged inode into the
committed phase name and retains the staged name as immutable evidence. A
committed phase is valid only when both names bind the exact journaled object.
Because the hard-link transition lacks a durable same-UID generation proof, the
taskless route stops after the pre-removal validation and does not attempt tmux
removal. No lifecycle record is published as done while the pane is live. An
absent pane without the exact pre-kill journal is never recovery authority.
A rerun rejects foreign phase, stage, audit, task, TODO, output-directory, or
pane identities.

The completed operation changes only the bound task status/blocker plus close
note and moves only its bound TODO row from `current` to `previous`. It never
calls pending removal or completion mail, and the audit records
`email_policy: suppressed`.

## Exact absent-manager historical custody

One narrow operation prepares an absent long-running manager target for
targetless historical custody without claiming completion. It is for a
manager record whose target is provably absent, whose task is the singular
active manager owner of that exact target, and whose current TODO row names
that same target exactly once.

```text
omo_namespace_drain.py bind-absent-manager-history \
  --root /ssd1/sichangheagent/work_logs \
  --code-review /absolute/private/code-review.json \
  --code-review-sha256 REVIEW_SHA256 \
  --consumed-code-review-receipt /absolute/report-receipts/CONSUMED.json \
  --consumed-code-review-receipt-sha256 CONSUMED_SHA256 \
  --executor EXECUTOR_TARGET \
  --task mail_archive_ops_submgr_0802.md \
  --task-sha256 3f1220339468cdb51c401917db14526cace2fdc5a662a47f77c9a65b55222bd8 \
  --target opsmail0802:1 \
  --output /absolute/private/absent-manager-binding.json

omo_namespace_drain.py reconcile-absent-manager-history \
  --root /ssd1/sichangheagent/work_logs \
  --code-review /absolute/private/code-review.json \
  --code-review-sha256 REVIEW_SHA256 \
  --consumed-code-review-receipt /absolute/report-receipts/CONSUMED.json \
  --consumed-code-review-receipt-sha256 CONSUMED_SHA256 \
  --executor EXECUTOR_TARGET \
  --binding /absolute/private/absent-manager-binding.json \
  --binding-sha256 BINDING_SHA256 \
  --audit-output /absolute/private/absent-manager-audit.json
```

Binding authenticates the Source-1261 authority, exact task bytes, exact TODO
bytes, exact ordered queue, blocker text, manager ownership, and a read-only
tmux absence proof. It computes the only supported replacement shape: change
`status: long_running` to `status: blocked`, change only the exact `runat`
value to `retired`, and remove only the target suffix from the sole current
TODO row. The computed replacement preserves `blocked_on`, `managerat`,
`is_manager: true`, `session_id`, body history, and every queued item in
order.

`reconcile-absent-manager-history` is intentionally fail-closed in the current
packet. The available public-file replacement helper uses pathname
`RENAME_EXCHANGE` and cannot prove descriptor-bound task/TODO
compare-and-swap against same-UID name swaps. Reconciliation therefore
revalidates the binding, target absence, task/TODO bytes, authority, dirty
state, and ownership, then raises before any public task/TODO mutation. It
never calls tmux stop/remove, pending removal, or completion-mail helpers.

The operation fails closed for Human targets, aliased target spellings, live or
rebound panes, duplicate or missing TODO rows, duplicate active manager
ownership, task/TODO byte drift, authority/review/executor drift,
pending-marker body text, protected output collision, dirty-state drift, or a
failed pre-mutation verification, or the current public-file CAS capability
blocker.

### Current non-executable preparation blockers

These source-level operations are not yet an approved runtime interface and
must not be invoked against real panes. The helper is mutable same-UID Python
and imports PyYAML plus manager modules before any in-process identity check.
No reviewed immutable bootstrap currently starts an exact root-owned Python
in isolated mode, captures the helper and complete mutable import closure
before execution, and loads only those captured bytes. An internal self-hash
or a hash-then-path import is not a trust root.

The completed lifecycle exchange now uses deterministic binding-bound private
task and TODO exchange entries in the pinned owner-private recovery directory.
Recovery accepts only journal-bound source, replacement, directory, and private
entry inode/digest identities. The helper deliberately refuses the final
task/TODO `RENAME_EXCHANGE` step because Linux exposes it as a pathname
operation: another same-UID actor can swap either name after descriptor
validation and before the syscall. The helper therefore cannot claim the
required arbitrary same-UID exact-target CAS proof for lifecycle mutation with
the available primitive. A supported future path must either provide that
stronger compare-and-swap primitive/tooling or explicitly narrow the invariant
to cooperative locking. Until then, the completed-shell lifecycle operation is
non-executable and fails closed before publishing task/TODO replacements.
The runtime trust-root blocker above also still prevents freezing or executable
use until the full helper/test/documentation packet receives the required
independent PASS; no pane, task, TODO, queue, or mailbox state may change.

The exact stale-completion operation is fixed to
`unslop_skill_repair_1119.md` on shared pane `wl:1` / `%2`:

```text
omo_namespace_drain.py bind-unslop-done \
  --root /ssd1/sichangheagent/work_logs \
  --code-review /absolute/private/code-review.json \
  --code-review-sha256 REVIEW_SHA256 \
  --consumed-code-review-receipt /absolute/report-receipts/CONSUMED.json \
  --consumed-code-review-receipt-sha256 CONSUMED_SHA256 \
  --executor EXECUTOR_TARGET \
  --output /absolute/private/unslop-binding.json

omo_namespace_drain.py reconcile-unslop-done \
  --root /ssd1/sichangheagent/work_logs \
  --code-review /absolute/private/code-review.json \
  --code-review-sha256 REVIEW_SHA256 \
  --consumed-code-review-receipt /absolute/report-receipts/CONSUMED.json \
  --consumed-code-review-receipt-sha256 CONSUMED_SHA256 \
  --executor EXECUTOR_TARGET \
  --binding /absolute/private/unslop-binding.json \
  --binding-sha256 BINDING_SHA256 \
  --audit-output /absolute/private/unslop-audit.json
```

The shipped binding is unconditionally non-executable because no authenticated
Human instruction currently names this task and action. It exposes no runtime
authority path, line, digest, or envelope options. A future reviewed code change
would have to bind all five internal constants to one already-injected,
digest-checked manager-mail excerpt and one separately digest-checked envelope
containing exactly one
`<human_instruction authoritative="true" source="FILE:LINES">` whose locator
and normalized text match that excerpt. The exact required text is
`Reconcile unslop_skill_repair_1119.md to done without changing wl:1, sending
mail, or removing queue items.` Source-1261 does not name this task and cannot
authorize it; caller-created source/envelope files and caller-chosen digests
cannot enable it.

Once exact Human authority is bound by reviewed code, binding also requires the
exact authorized task digest, v1 blocked non-manager
state, empty queue, `done_close_failed` refusal for `%2`, and the one embedded
accepted-completion sentence. It authenticates full result commit
`b15d313329d872286e6d5b3b7090ba00ff4a8cfb` as an ancestor of
the live `refs/heads/macos` tip returned by the fixed remote URL
`git@github.com:SichangHe/.config.git`; binds that configured-origin identity,
live remote tip, commit tree, exact task-specific Human source and envelope,
TODO bytes and absence of a TODO row, both manager records, shared pane ID/PID
and process start ticks, and both repositories' unrelated dirty state.
Reconciliation repeats every proof under membership, target, task, TODO, and
manager locks. It changes only the task's `status` to `done` and removes its
blocker. It never sends mail, removes a queue item, writes TODO, stops or sends
input to `%2`, or calls ordinary done handling. Any drift aborts; a failure
after the task exchange restores the exact source bytes when they still match.
The Human source/envelope and actual remote proof are repeated after the
exchange before the audit is committed, so mid-transaction drift rolls back.
The audit records the unchanged TODO/result/shared-pane identities and the
suppressed mail, unchanged queue, unchanged TODO, and untouched pane policies.

Neither prerequisite is the full namespace drain. After their immutable audit
receipts exist, create a fresh complete plan; do not reuse a plan that captured
either exited shell.

## Source-1386 ordinary-worker handoff

`bind-source1385-live-worker-handoff` may prepare a read-only binding for the
exact `mail_compress_1261.md` custody move. It binds the Human authority, source
task and full ordered queue, canonical TODO row, source pane/process/session,
both source and destination managers, an explicit destination acceptance, the
complete active-task graph, and unrelated repository state. The proposed
successor must be a new task on a distinct absent non-Human target.

The corresponding reconcile command is deliberately non-executable. The
available pathname `RENAME_EXCHANGE` cannot provide descriptor-bound task and
TODO compare-and-swap against an arbitrary same-UID name swap. In addition, the
available launcher cannot establish and prove accepted live successor custody
before stopping the old worker. Stopping first would leave no owner if any
later lifecycle write failed.

The helper therefore refuses before stopping a pane, publishing a successor,
or changing task, TODO, queue, repository, or mailbox state. A production
packet must not be prepared until both missing primitives are reviewed and
installed: descriptor-bound recoverable task/TODO CAS, and a live-before-stop
successor activation transaction with collision, replay, rollback, and
exactly-one-owner proof.
