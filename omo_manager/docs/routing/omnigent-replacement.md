# OmniGent replacement boundary
(authored by agents unless marked 🧑)

authority

- Source-1957 answers “1” to “Replace the current read-only owner atomically with one unrestricted OmniGent session in the existing DeGenTWeb_writeup directory, preserving the task and queue and delivering only open work”
- approval identifies the operation; it does not establish that native writers have stopped

bootstrap

- `omo_omnigent_server.py` composes public installed OmniGent stores, runtime, authentication and `create_app`
  - distribution version and a sorted path/content digest of all installed Python sources must match the reviewed package before constructing stores
  - explicit loopback bind, absolute SQLite database locations and local artifact directory
    - explicit CLI database/artifact values override config-file values, matching the upstream CLI
  - same optional separate conversation database, runtime timeout, default policies, administrator/domain lists and policy modules
  - current supported routing has no external or LLM backend
  - unsupported configuration, authentication, routing, storage and backend configuration return a rejection before lifecycle writes
- `--check` validates without creating stores, directories or runtime state
- `create_server` returns the original FastAPI app, guarded ASGI app, composed conversation store and durable fence store
  - injected store reaches original route and runner-router closures
  - original `app.state.host_registry` remains available to a trusted coordinator
  - private Source1957 Unix control is optional; there is no administrative HTTP endpoint or general bypass credential
- CLI serving chains Uvicorn's signal handler after lifespan startup
  - marks server shutdown and closes session streams before tunnels disconnect
  - closes control admission immediately and drains already accepted coordinator work before normal server shutdown
  - preserves native tunnel message-size, ping and graceful-shutdown settings
  - existing process logger handlers remain installed

configuration evidence

- observed startup specifies host, port, SQLite database and artifacts with no config-file argument
  - CLI server configuration is therefore empty
  - global provider configuration contains only local host identity
- public `/v1/info` establishes single-user mode, accounts disabled, no login URL, no managed sandbox, no routing backend, sharing on and public sharing enabled
- refresh authenticates the original environment bytes without writing their values to an artifact
  - the original interpreter can read protected process links and environment through its existing `CAP_SYS_PTRACE` capability
  - the standby reads and verifies the environment before stopping the server, then preserves it for `exec`
  - plan and receipts retain only its digest; public behavior remains an additional comparison
- same configured environment is consumed by public upstream components
  - loopback header mode keeps the upstream default single-user marker
  - this replacement supports only the verified local single-user header mode; explicit multi-user header and account/OIDC modes are rejected
  - runner tunnel token allowlist is retained if configured
  - data/config homes, feature/sharing settings, credentials and telemetry remain upstream inputs
- public behavior comparison establishes the planned local operational path
  - headerless `/v1/me` returns `local`; supplying `X-Forwarded-Email: local` returns no identity, proving the default identity header is recognized
  - an unknown strip-prefix has no effect on headerless calls
  - reviewed loopback tunnel code retries without the token allowlist, so an unknown old allowlist does not alter the local host/runner path
  - this does not replace the process, package, source or environment binding

server refresh

- `omo_omnigent_refresh.py` accepts only the existing explicit loopback CLI deployment and a clean published configuration checkout
  - original interpreter, PID incarnation, listening socket, package, source, authority, task, TODO, queue, host tree and native thread are pinned
  - required `old_native_cwd` and nullable `old_bridge_cwd` bind observed predecessor directories independently of the strict configured successor workspace
  - refresh never infers missing predecessor cwd fields; every observation rechecks their exact reviewed values and all three completed native turns without replay
  - a separately reviewed refresh plan is required before stopping the server
- a standby records its own process identity before the original server receives a graceful stop
  - the standby owns the wait-for-exit and exact bootstrap `exec`, independent of the invoking client
  - unknown process outcomes remain uncertain; no second spawn or forced kill follows
- read-only reconciliation compares preserved native processes, session bindings, full thread history and public behavior
  - server refresh does not replace the paper owner or authorize prompt delivery
  - the execution packet is built against the refreshed server from unchanged preparation inputs and separately reviewed

private control

- six explicit startup settings bind socket, preparation, packet, review, backend config and preparation digest
  - canonical owned parent directories must already exist with mode `0700`
  - input files and socket must be owned, singly linked and mode `0600`
  - only a refused, unchanged private socket can be removed during restart recovery
- one canonical JSON line requests `prepare`, `inspect`, `execute` or `reconcile`
  - fields are `action`, `preparation_sha256`, `packet_sha256`, `approval_sha256`
  - absent action-specific digests are empty strings
  - no caller-selected paths, observed-state claims, receipts or generic backend commands
  - Linux peer credentials must identify the server's user
- one actor owns a dedicated coordinator thread
  - accepted operations survive client disconnect
  - unexpected failures return an uncertain result, fail queued requests and stop control admission
  - concrete backend calls synchronize with the original server event loop
  - an unreturned backend call blocks later control actions; shutdown drains that worker rather than admitting a retry beside it
- coordinator checks immutable packet and separately pinned review before execution
  - replay reuses durable operation, successor and delivery identities
  - startup preparation commitment and artifact paths cannot be rebound
  - post-startup input drift rejects preparation or execution; do not overwrite artifacts or infer restart authority

store admission

- every session mutation holds a durable admission through completion
  - binding, host, native thread, labels, metadata, append, fork, create and async deletion
  - deletion admits the complete immutable-parent subtree
  - creation and host binding carry the actual host into exclusive host admission
  - unknown future public methods fail closed
- frozen historical rows and transcript items remain unchanged and readable
  - returned conversation copies mask the runner binding used by the installed runner router
  - runner lookup, connectivity and reconnect lists suppress the frozen owner
  - heartbeat updates skip fenced runner bindings
- `replacement_session(operation_id)` reads the operation's raw historical binding only after comparing runner, host, native thread and workspace
  - the trusted coordinator must authenticate its endpoint
  - this read grants no mutation, launch or delivery permission
- ordinary HTTP admission precedes upstream handlers
  - the ASGI guard also controls tunneled internal dispatch and host launches
  - pre-fence stored conversation objects still require the tunnel boundary

native callbacks

- the claimed runner's existing mint handshake issues short-lived single-use native credentials
  - session, runner, native and host process incarnations, transaction and server generation are bound
  - native metadata and observational callbacks have exact method, endpoint and body checks
  - no credential grants input, retry, settings, launch or ownership authority
- zero-authority placeholders accommodate installed-client caching before native readiness and after confirmation
  - they do not lift any fence or authenticate any endpoint
- the user transcript mirror must match the durable manifest dispatch exactly
  - tool callbacks that could resolve an elicitation wait for delivery confirmation
  - their unchanged requests then require ordinary stock authentication, without a native privilege grant
  - pending callback receipts survive interruption; unknown results are not replayed
- tests exercise installed metadata/forwarder clients and actual server routes and SQLite stores

completion boundary

- server/store fencing does not prove native quiescence
- the coordinator must independently authenticate authority, reserve unchanged task/queue revisions, establish the old writer domain has stopped, create one reserved successor and reconcile uncertain delivery
- store denial raises an invariant error for upstream callers whose return types cannot express rejection
  - no denied store operation reaches application storage
  - ordinary public requests receive the ASGI guard's rejection
- tests use temporary actual SQLite stores, installed entities and the actual ASGI application
  - run the reviewed installed interpreter with `-m unittest omo_manager.tests.test_omnigent_server`
  - actual Unix sockets exercise peer, permission, envelope and commitment rejection before coordinator work
  - no live service restart, host stop or replacement follows from passing these tests

durable fence

- private SQLite journal uses synchronous durable commits and retains the exact prepared packet
  - canonical session identifiers share one reservation; original packet bytes stay intact
  - operation, reserved successor and one-shot delivery identities never change during reconciliation
- phases advance from prepared through fenced, old quiesced, new prepared and committed to delivery confirmed
  - an uncertain side effect requires evidence inspection, never a blind second attempt
  - a completed operation remains readable when its former backend is unavailable
- admissions bind process incarnation and advance routing or host generations
  - fencing compares generations and in-flight admissions in the same transaction
  - remote admissions survive transport loss until an independent process barrier proves they cannot execute
- the selected host is exclusive during transfer
  - one outgoing launch binds the reserved successor to its actual runner
  - public successor input stays closed until delivery confirmation
  - native sockets are outside the ASGI boundary and need separate process evidence

task ownership

- `build-preparation` creates only a private input artifact from a nine-entry reviewed manifest
  - every entry binds its queue position and text digest and names open work or a retained constraint
  - serialized file images, source pins and workspace identity are checked again before reservation
  - the exact canonical workspace comes from configured `OMO_SOURCE1957_WORKSPACE`, never a basename match
- the packet binds exact Human source bytes, configured task root, original task and TODO bytes, ordered nine-item queue, workspace identity, runtime state and executing source closure
- a separately reviewed manifest accounts for each queue item as open work or a retained constraint
  - historical task-body prompts are preserved as evidence, not dispatched
  - unresolved dispatch markers prevent preparation
- supported root, task and target locks serialize publication and delivery
  - every completed file exchange is rechecked before another exchange
  - drift rolls back only transaction-owned file versions and preserves foreign changes
  - uncertain or mixed state cannot release successor delivery
- publication intent, exact inode receipts and review approval are durable
  - reconciliation distinguishes an owned incomplete effect from a foreign same-byte file
  - lost delivery results require positive observation of the one reviewed message, never retransmission
- final ownership validation surrounds delivery acceptance
  - post-dispatch drift is uncertainty, not evidence that delivered work was undone
- initialization recovery is separate from manifest dispatch
  - recovery may continue only the authenticated owned host and runner before `manifest_dispatch_intent`
  - after that intent, reconciliation inspects the exact mirrored user item and never resends

native verification

- the backend pins the exclusive host process tree and native bridge before stopping anything
  - pidfds bind process incarnations; a freeze-and-recheck barrier detects forks or exits
  - the old process tree and transport must be gone before successor creation
  - the replacement host connection's TCP peer socket must belong to its recorded process incarnation
- successor initialization preserves the existing workspace and uses a fresh native thread
  - no old thread history, pending input or task-body prompt is copied
  - the reserved bridge must be absent and unaliased before launch, with replay artifacts absent before every initialization
  - model, effort, workspace and actual unrestricted permissions are checked before open-work dispatch
- a creation nonce authenticates a partially created reserved session
  - recovery uses public store operations on that exact empty row, never a different successor ID
- the host standby exits if its parent closes the release pipe without authorization
  - its durable process identity survives the authorized `exec`
  - an unclaimed runner binding remains a fail-closed uncertainty, not permission to clear or launch again
- [official app-server documentation](https://developers.openai.com/codex/app-server/) distinguishes thread reads from resuming or starting turns
  - installed-version schemas define the settings fields used by verification
  - configuration defaults alone do not certify a live thread's permissions
