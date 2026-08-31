# sealed bootstrap

purpose

- execute an interpreter only after authenticating every file it can execute
- prevent ambient project, module, loader, startup-hook, cwd, or subprocess substitution
- fail with a stable nonzero phase code when a required kernel capability or identity is absent

trust root

- the built launcher binary and its sha-256, authenticated out of band
- manifest sha-256 and immutable manifest identity compiled into that launcher
- Linux x86-64 kernel namespace, tmpfs, seccomp, chroot, procfs, and vfs behavior
- executable-format registry at the launcher invocation boundary
  - the launcher is itself an x86-64 elf process, so a pre-existing malicious
    `binfmt_misc` handler could intercept its initial `execve` before `_start`
  - the trusted invoker must establish that no matching hostile handler exists
- build-time compiler, launcher source, manifest builder, and caller-selected closure
- initial process image before `_start`

provisioning

- `create_manifest`
  - binds the exact launch cwd and each absolute source-parent chain
  - rejects symlink traversal and source paths containing a separator
  - records every directory device/inode/type and every file identity, mode, size, timestamps, and sha-256
  - requires every execute-bit closure entry to be a structurally valid Linux x86-64 elf file
  - records exact destination, argv, cwd, and environment
  - requires absolute launch, source-root, manifest-output, and launcher-output paths
  - binds the output parent, creates with `O_EXCL|O_NOFOLLOW`, directory-fsyncs, and verifies a new mode-`0400` manifest
- `build_launcher`
  - reauthenticates the manifest fd
  - compiles its digest and identity into the native source
  - requires static freestanding x86-64 elf output with no `PT_INTERP` or `PT_DYNAMIC`
  - compiles into an unnamed `O_TMPFILE`, reauthenticates the manifest, links once into a bound output parent, directory-fsyncs, and verifies a mode-`0500` launcher
- both publication paths retain and validate their complete output-directory chains
  - final-component or ancestor replacement causes an exception
  - a replacement reachable through the public path is never overwritten or removed
  - an already-created artifact can remain attached to a displaced retained directory; callers must inspect the reported failure and must not infer publication from a public pathname

launch sequence

- require
  - exact manifest descriptor identity and sha-256
  - empty ambient environment
  - non-elevated stable uid/gid
  - exact authenticated current-directory path and inode
  - authentic procfs and unprivileged user/mount namespace support
  - readable authentic `binfmt_misc` mounted at `/proc/sys/fs/binfmt_misc`
  - no initial effective, permitted, or inheritable capabilities
  - no registered handler whose flags contain `F`, including combined flag sets such as `CF`
  - Yama `ptrace_scope` at least `1`
- parse a bounded binary manifest with capability `sealed-bootstrap-linux-x86_64-v1`
- bind and retain every launch/source directory fd and source file fd
- open possible special-file substitutions nonblocking, then require a regular-file identity
- verify file attachment, immutable inode identity, metadata, and stable sha-256
- create a detached tmpfs through fd-only mount api
- copy only authenticated retained fds to canonical absolute destinations
- `fsync`, close every writer, make the detached mount recursively read-only, and rehash every copy
- revalidate the manifest fd and sha-256, current directory, all source chains, attachments, identities, and hashes
- `chroot` through the retained detached-root fd and select the sealed cwd
- require write-only character/fifo stdout and stderr, close stdin, and close every inherited fd above `2`
- lock securebits, verify every bounding-capability drop, clear ambient/effective/permitted/inheritable sets, and set no-new-privileges
- install seccomp, then `execve` the sealed interpreter path with exact manifest argv/env

executed closure

- runtime root contains only manifest files and generated directories
- a missing interpreter, elf loader, shared library, module, hook, or subprocess runtime fails inside the sealed root
- file-backed executable mappings can use only authenticated files
- writable executable mappings are denied even when file-backed
- runtime policy denies
  - `execveat`, memfd, secret memory, shared-memory attachment, and executable anonymous mappings
  - adding executable permission with `mprotect` or `pkey_mprotect`
  - received or cross-process file descriptors
  - namespace, mount, chroot, open-by-handle, and ptrace syscalls
  - all runtime `prctl` calls, including attempts to restore dumpability or a ptracer
  - clone requests creating namespaces
- subprocesses use `execve` paths inside the same read-only root and inherit chroot, seccomp, no-new-privileges, and no capabilities
- the ambient environment must be empty
- sealed environment policy rejects `LD_*`, `PYTHON*`, `GLIBC_TUNABLES`, and the documented language startup names enforced by both builder and launcher
- interpreter-specific isolation flags remain the manifest author's responsibility
  - for CPython use isolated/no-site/no-environment flags and an authenticated startup file
- standard output and error must be write-only character devices or fifos
  - regular-file stdio could expose unauthenticated executable storage and is rejected
  - standard input is closed before interpreter execution
  - output remains an intentional external side-effect channel; it cannot be read back through those fds

failure codes

- `10`: invocation
- `11`: manifest identity or authentication
- `12`: manifest format or policy
- `13`: descriptor identity query
- `14`: file identity, attachment, or content drift
- `15`: directory or cwd drift
- `16`: namespace, copy, seal, or destination failure
- `17`: identity, environment, inherited-fd, capability, or seccomp policy
- `18`: interpreter `execve`
- `19`: test-only synchronization in a separately compiled fixture launcher
- no project or manifest runtime code has executed when the launcher emits these codes

platform and limits

- required
  - Linux x86-64
  - user and mount namespaces
  - procfs at `/proc`
  - binfmt_misc mounted and readable at `/proc/sys/fs/binfmt_misc`
  - no persistent (`F`-flag) binfmt_misc interpreter registration
  - Yama `ptrace_scope` from `1` through `3`
  - `fsopen`, `fsconfig`, `fsmount`, recursive `mount_setattr`, `close_range`, and seccomp-tsync
  - build-output filesystems supporting unnamed `O_TMPFILE` creation and one atomic hard-link publication
  - a kernel/filesystem able to execute the copied elf closure from tmpfs
- intentionally unavailable
  - jit and anonymous executable code
  - writable executable mappings
  - descriptor-passed plugins
  - `recvmsg`, `recvmmsg`, `clone3`, executable `mprotect`, and shared-memory attachment
  - network and unix-domain sockets
  - `bpf`, `userfaultfd`, and `io_uring`
  - writable runtime filesystem
  - setuid, capabilities, mount changes, and alternate namespaces
- same uid and kernel limits
  - the initial kernel executable-format decision precedes all launcher code; the
    invoker must trust the registry at that boundary
  - the launcher checks authentic procfs/binfmt_misc and rejects persistent
    handlers before creating namespaces; a privileged host can still alter the
    registry after inspection and is outside the trust root
  - processes starting with any effective, permitted, or inheritable capability
    are rejected, preventing a same-uid privileged registry mutation during setup
  - mapping an unprivileged user namespace requires a short pre-seal dumpable window
  - enforced Yama policy limits that window and later `execve` dumpability to an ancestor tracer
  - a hostile parent or ancestor already tracing the launcher is outside the trust root
  - same-uid signal denial-of-service is not prevented
  - output recipients and signal timing are external; neither can add executable bytes to the sealed root
  - local pipes created after launch are allowed
  - privileged host processes, kernel compromise, malicious firmware, and hardware faults are outside the guarantee
- portability
  - no fallback exists
  - unsupported kernels, architectures, dynamic launchers, or missing closure entries fail closed
