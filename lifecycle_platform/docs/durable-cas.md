# durable cas

purpose

- change one small regular task or todo file only when its bytes exactly match
- keep old and new versions as an authenticated recovery pair
- expose explicit outcomes instead of claiming success after interference

trust root

- Linux kernel vfs, `renameat2`, and `fsync` behavior
- filesystem support for `RENAME_EXCHANGE` and directory `fsync`
- caller-supplied expected bytes and target path at the initial bind
- target and recovery calls require the same canonical absolute target path
- caller-held 32-byte `RecoveryAuthority` key
  - generated independently of the target directory
  - confidential and integrity-protected for as long as its journals remain recoverable
- process memory and retained fds
- limitation
  - no pathname-only API can identify which same-content inode the caller intended before the first bind
  - callers needing that guarantee must authenticate the initial namespace or run inside a trusted root

binding and mutation

- open `/` once and walk the absolute path from that retained anchor
- walk every directory component with `O_DIRECTORY|O_NOFOLLOW`
- retain every directory fd, the target fd, and each device/inode/type identity
- open final files with `O_NONBLOCK`, then require regular-file type so fifo or
  other special-file replacement cannot stall validation or recovery
- after binding
  - resolve only saved single-component names relative to retained parent fds
  - never resolve the public target path again
  - verify every parent-child attachment before mutation and after rollback or commit
- require a stable retained-fd read equal to the expected bytes
- `fsync` the retained original target and revalidate its attachment, metadata, and bytes
- create the replacement and binary journal with `O_EXCL|O_NOFOLLOW`
- write, hash, `fsync` both files, then `fsync` the parent
- revalidate the chain, target, replacement, journal, metadata, and content
- atomically exchange replacement and target with `RENAME_EXCHANGE`
- open both exchanged names and require the exact retained inode/hash pair
- on precommit interference
  - exchange the pair back only when both names still attach the exact opened original/replacement
  - `fsync` the parent and verify the rollback
- on commit
  - `fsync` the parent
  - verify chain, names, inode identities, hashes, and journal again

outcomes

- `CasApplied`
  - target is the replacement at final verification
  - old version is the recovery data entry
  - exchange is directory-fsynced
- `CasFailure`
  - `expected-mismatch`: no prepared entries
  - `namespace-drift`: a retained chain or target attachment changed
  - `race-rolled-back`: exchange occurred, then a verified rollback was directory-fsynced
  - `recovery-required`: journaled names do not form an authenticated pair
  - `unsupported`: kernel or filesystem lacks the required exchange
  - `io-failure`: validation or i/o failed before a known commit
  - `indeterminate`: post-exchange verification cannot prove commit or rollback

durability and recovery

- successful preparation has fsynced original, replacement, journal, and parent before exchange
- successful commit or rollback includes a parent-directory `fsync`
- the library never unlinks recovery files
  - Linux has no conditional “unlink this open inode” operation
  - unlinking an attacker-swapped name would violate fail-closed behavior
- `RecoveryToken` binds the retained journal identity, digest, and strict leaf names
- `RecoveryAuthority` authenticates the version-2 journal with hmac-sha-256
- the authenticated journal covers
  - the complete bound directory-chain identities
  - exact caller-supplied target-path hash
  - both recovery-name hashes
  - original and replacement identities
  - original and replacement sha-256 values
- `recover_exchange`
  - requires the same external authority
  - accepts only the journaled pair
  - validates both attachments immediately before any exchange
  - selects either digest through an atomic exchange when necessary
  - `fsync`s the parent even when the desired version is already visible
  - verifies the final chain, names, and content
- process or power loss
  - before exchange: target remains old; prepared entries may remain
  - before exchange `fsync`: filesystem may recover the old or new pair
  - after exchange `fsync`: target is durably new and recovery data is durably old
  - caller retries distinguish old, new, or foreign target bytes
  - cross-crash authenticated recovery requires the caller to persist both the returned token and authority outside this directory
  - a crash before token delivery leaves only untrusted orphan names; do not infer a token from them under a hostile same-uid writer

limits

- supported
  - Linux x86-64 and other Python-supported Linux architectures with `renameat2`
  - local filesystems honoring regular-file and directory `fsync`
  - regular files up to the binding helper's 64 mib read limit
- preserved metadata
  - permission bits, uid, and gid
- not preserved
  - acl, xattrs, security labels, sparse layout, timestamps, or file flags
- same uid can still
  - modify an inode through an already-open writable fd
  - change the namespace after final verification
  - ptrace or kill the process where kernel policy permits
- the primitive detects interference through the final check; it does not provide continued protection after return
- an attacker racing between a validation and the kernel exchange can force `indeterminate`
  - Linux exposes no identity-conditional rename
  - `RENAME_EXCHANGE` cannot prove the checked inodes are still attached at its linearization point
  - a same-uid substitution in that final kernel window can therefore be exchanged once
  - rollback is attempted only when both post-exchange names still prove the signed pair; it never exchanges an unrecognized entry
  - the primitive never reports that state as success
- crash recovery relies on device/inode/type, mode, uid/gid, link count, size, mtime, and content hashes
  - ctime is recorded but excluded from pair recognition because the exchange itself can change it
  - pathological inode reuse with an identical metadata snapshot is a kernel/filesystem limit
- kernel, filesystem, storage firmware, or hardware compromise is outside the guarantee
