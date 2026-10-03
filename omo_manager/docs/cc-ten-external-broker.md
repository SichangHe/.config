# Ten-page external verifier and one-use broker (HOLD)

(authored by agents unless marked 🧑)

`cc_ten_external_verifier.py` is an offline candidate, not launch authority. Its
launcher/client/public-key/principal pins are unset and it never sends a Bino
request. The current config tree belongs to runtime UID 30033; putting pins
there would not establish an independent trust boundary. The scorer/Bino
processes and their socket stay owned by UID 30033.

## Protected installation

- An administrator installs a fixed, isolated Python interpreter, the broker,
  verifier, dependencies, and every parent directory under an identity that
  UID 30033 cannot modify. The process manager fixes executable path, cwd,
  environment, `PATH`, `PYTHONPATH` (empty), and the tmux executable; no
  attacker-controlled imports may run before these checks. A Python script
  attempting to validate its *own* import chain after importing cryptography
  is insufficient.
- Pin the entire reviewed launcher and client SHA-256 and the reviewer's
  Ed25519 public key in that installation. Keep the signing **private** key
  accessible only to the reviewer; never pass it to the scorer or broker.
- Authenticate exactly one reviewer-authorized invocation independently of
  the signed `ACCEPT` payload. Under a protected parent, atomically create and
  fsync a unique, payload-digest-bound `consumed` receipt **before** spawning
  the runtime sender. Missing or existing receipts fail closed; a crash,
  timeout, or unknown response cannot reauthorize or auto-retry a request.

## Snapshot and dispatch

1. The protected broker independently validates the signed canonical JSON,
   full launcher/client bytes, pinned ten-item manifest, closure digest,
   PID/start/cmd/executable identities for both processes, socket/log
   device+inode, scorer-pane command, and output directory. Check the *live*
   evidence again immediately before each one-use dispatch.
2. Pin code and approval as byte snapshots. Pass them to a child using sealed
   read-only descriptors rather than pathname imports; compare digests inside
   the child. A pathname may be replaced by UID 30033 between any two checks.
   The current launcher/client reread `__file__` and `CLIENT`, so dw:2 must
   provide and review an explicit bytes-only entrypoint before dispatch is
   possible. The broker must never patch source text or replace its imports
   implicitly.
3. A privileged, independently installed process drops the child to the
   pinned runtime UID/GID 30033 with no retained privilege or writable
   inherited descriptors. Run the fixed isolated interpreter and fixed
   dependency snapshot with a scrubbed environment. Client ownership and Bino
   `SO_PEERCRED` checks require the **sender**, not the protected verifier, to
   have runtime UID 30033. Never execute client code inside the reviewer UID.
4. Tie the consumed receipt to the exact child execution attempt and preserve
   unknown outcomes. An approval signature alone must not launch. A direct
   runtime-UID invocation must not be able to forge a broker receipt or
   inspect signing material.

## Independent adversarial review before any GO

- Change launcher/client pathname, Python import path, cwd, `PATH`, tmux
  executable, approval, manifest, closure, socket inode, log inode, PID start
  ticks, or child UID across preflight and dispatch; each must fail before a
  request. Prove child executed **only** the pinned byte snapshots.
- Two simultaneous invocations with the same authority yield at most one
  consumed receipt and at most one dispatch; crash after consumption or an
  ambiguous socket outcome never retries. Review the protected installer,
  principal and key custody separately from the Python unit tests.
- Client-only checks cannot prevent arbitrary same-UID callers from sending
  their own AF_UNIX requests to Bino. If exclusivity is required, deploy a
  separately reviewed server-side authorization policy. Until then, do not
  represent this broker as exclusive or authorize ten-page acquisition.

The current offline candidate's passing unit tests establish only its
signature/evidence parsing, not privileged installation, safe dispatch,
one-use authority, or server-side authorization. The independent verdict is
**HOLD**. No launch pin, approval, sender credential, or live process change
is part of this design.

## Offline broker contract for the existing ten-sample manifest

`cc_ten_broker_contract.py` validates only a proposed signed envelope against
a supplied snapshot. It cannot consume a nonce, open an IPC endpoint, launch a
child, access the scorer, or send a Bino request. The manifest digest is fixed
to `2ce23e45ab8bbbee4d087ca26c36796142e3113cc1c0098f617f475f6007d115`.
Do not replace it with the nine-page subset or new generated samples.

- The reviewer signs canonical JSON containing a fresh 256-bit request nonce,
  exact manifest, launcher/client/interpreter/dependency/installed-broker
  digests, closure, scorer and Bino PID/start/command/executable identities,
  socket and log device/inode, pane command, output path, runtime UID, and
  Bino server authorization policy digest. The private key stays solely with
  the reviewer; a signature cannot itself trigger dispatch.
- A separate protected broker authenticates the reviewer request and validates
  the entire ancestor chain, pinned byte snapshots, and live identities.
  Under a parent UID 30033 cannot alter, it atomically creates and fsyncs one
  nonce-digest receipt **before** any child execution. Duplicate, crashed,
  timed-out, and uncertain requests never start another child.
- The broker sends sealed launcher/client descriptors plus nonce-bound receipt
  through a one-shot Unix socket whose peer identity is checked on both ends;
  it drops to UID 30033 and starts a pinned isolated interpreter with an exact
  environment (`PATH=/usr/bin`, empty `PYTHONPATH`, `PYTHONNOUSERSITE=1`).
  The sender must verify descriptor contents, receipt, and live identities
  before any Bino call. It must never import mutable launcher/client pathnames.
- A proposed IPC boundary is a single `AF_UNIX/SOCK_SEQPACKET` connection
  under a protected parent directory. Both endpoints check kernel
  `SO_PEERCRED`; the broker passes launcher/client/receipt descriptors with
  `SCM_RIGHTS`, backed by sealed memfds (`F_SEAL_WRITE`, `F_SEAL_GROW`,
  `F_SEAL_SHRINK`, `F_SEAL_SEAL`). The receipt names the nonce digest and signed
  request digest, is created exclusively under a protected directory and
  fsynced with its directory before descriptor delivery, and is never removed
  after an unknown result. The sender opens no runtime-writable code path.
  The broker fixes interpreter bytes, argv, cwd, UID/GID, dependency snapshot,
  environment, and all executable ancestor ownership *before* starting the
  child; the sender rechecks the received descriptors and live identities.
  These are requirements for a future implementation, not properties of the
  present offline validator.
- Same-UID processes could call the existing Bino Unix socket directly.
  Therefore an independently reviewed Bino **server-side** per-request
  authorization policy is a hard prerequisite for claiming exclusive
  acquisition; the broker contract rejects a missing policy digest and
  compares a caller-supplied live policy digest, but has no authenticated
  policy attestation and cannot verify that Bino enforces it. A digest alone
  does not prevent same-UID bypass: no deployed
  server-side check or protected IPC installation has been verified. No live
  approval or dispatch follows from an offline test pass.

`tests/test_cc_ten_broker_contract.py` checks supplied nonce replay, signature
and manifest tampering, claimed immutable ancestry, supplied peer/child UID,
inherited `PATH`/`PYTHONPATH`, sealed-byte substitution,
PID/socket/log/closure drift, and an absent server policy digest. These
checks cannot authenticate a kernel peer or demonstrate server enforcement;
they are not privileged integration tests.
The offline tests require a Python environment with `cryptography` installed;
they were run with the existing `dw5/.venv` interpreter, not installed into
the protected principal or the config environment.

## Child contract and responsible installer (not implemented)

- The signed request is **not** the current client's `REQUIRED_APPROVAL`.
  Its exact v1 keys are `REQUIRED_FIELDS` in `cc_ten_broker_contract.py`;
  `review_verdict=ACCEPT` in the legacy approval grants no broker dispatch.
  The present launcher reads `CLIENT` and `__file__` by path, and the client
  reads its own `__file__` and hardcoded approval path. Both would reject the
  new fields or reintroduce a path race. No one should add those fields to the
  legacy approval or edit the client on the assumption that this schema runs.
- A future **bytes-only** child entrypoint receives sealed FDs for the
  reviewed launcher and client, exact ten-page manifest, all ten raw-text
  contents, and protected consumed receipt. The child matches the manifest
  digest and each page's raw-text digest and identity against the supplied
  bytes. It never rereads `MANIFEST_PATH`, any `raw_text_path`, launcher,
  client, or approval pathname after validation. Its
  bootstrap and dependency bytes must also be pinned and loaded without
  mutable path imports. It verifies the full signed v1 request against the
  installed reviewer key, all FD seals/digests, and a receipt containing the
  request digest, nonce digest, manifest digest, child executable digest, and
  broker installation digest. The broker fixes an isolated interpreter,
  bootstrap, argv, cwd, dependencies, UID/GID 30033, and exact environment.
  An authenticated one-shot protected socket passes FDs only to the freshly
  launched PID/start identity; the child checks the broker's kernel peer UID.
  The broker must atomically consume and fsync the nonce before it launches a
  child. Any crash or ambiguous response remains consumed.
- The **protected installer**, not dw:2 or a runtime-UID script, must be an
  administrator or independently controlled service principal. It provisions
  a different UID, immutable full executable/interpreter/dependency ancestor
  chain, pinned reviewer public key and launch/client byte digests, and a
  journal directory that UID 30033 cannot replace. The private signing key
  stays with the reviewer, not the installer, broker, child, or scorer. The
  present `.config` ancestor and runtime tree do not meet this requirement.
- The **Bino server owner** must separately install and review server-side
  per-request authorization under a principal UID 30033 cannot modify, or
  provide another independently protected authorization service. A client
  check alone does not stop other UID 30033 processes from calling today's
  world-writable Bino socket. A policy digest in the signed request is a
  comparison field, **not evidence of deployed enforcement**. No such
  server-side installation or actor has been authenticated here.
- A possible future authorization service, installed separately from UID
  30033, registers the broker's consumed request digest with the Bino server
  over a protected channel before child dispatch. The Bino server must require
  the registered nonce and check kernel peer PID, UID and process start time
  for **each** raw-text request, allow the exact ten manifest identities only,
  and atomically consume each page authorization before a reply. The child
  must be non-dumpable, and the service must reject any caller without a
  broker-registered identity. No such server check exists today; neither a
  signed digest nor a client-only check establishes it.
- The current scorer dequeues database-backed work; this schema does not
  turn the ten raw-text files into scorer jobs. Before any scoring request,
  dw:2 and the Bino server owner must show a reviewed raw-text API that needs
  no unapproved database or overlay write, or obtain separate authority for
  an ingestion path. Until that interface and server authorization exist,
  the ten texts remain unscored and the broker stays **HOLD**.
