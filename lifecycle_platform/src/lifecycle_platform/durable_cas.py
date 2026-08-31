"""Descriptor-bound, directory-durable compare-and-swap for small regular files."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import os
import stat
import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast

from ._bindings import BoundDirectory, Identity, bind_directory, digest_fd, entry_identity, read_all

_RENAME_EXCHANGE = 2
_JOURNAL_MAGIC = b"LPCAS\x00\x02\x00"
_JOURNAL_PREFIX = struct.Struct("!8s32s32sI")
_IDENTITY = struct.Struct("!8Q2q")
_JOURNAL_MAC_BYTES = hashlib.sha256().digest_size
_FILE_OPEN = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_FILE_BYTES = 64 * 1024 * 1024
_LOWER_HEX = frozenset("0123456789abcdef")
_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = _libc.renameat2
_renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
_renameat2.restype = ctypes.c_int


class CasFailureState(Enum):
    """Stable fail-closed outcomes."""

    EXPECTED_MISMATCH = "expected-mismatch"
    NAMESPACE_DRIFT = "namespace-drift"
    RACE_ROLLED_BACK = "race-rolled-back"
    RECOVERY_REQUIRED = "recovery-required"
    UNSUPPORTED = "unsupported"
    IO_FAILURE = "io-failure"
    INDETERMINATE = "indeterminate"


class CasPhase(Enum):
    """Deterministic test observation points; not part of the public API."""

    BOUND = "bound"
    PREPARED = "prepared"
    BEFORE_EXCHANGE = "before-exchange"
    AFTER_EXCHANGE = "after-exchange"
    BEFORE_ROLLBACK = "before-rollback"
    BEFORE_RECOVERY_EXCHANGE = "before-recovery-exchange"
    DURABLE = "durable"


class RecoveryAuthority:
    """Caller-held MAC authority kept outside the hostile target directory."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes) -> None:
        if len(key) != hashlib.sha256().digest_size:
            raise ValueError("recovery authority must contain exactly 32 bytes")
        self._key = bytes(key)

    @classmethod
    def generate(cls) -> RecoveryAuthority:
        return cls(os.urandom(hashlib.sha256().digest_size))

    def export_key(self) -> bytes:
        """Export key material for integrity-protected external recovery storage."""
        return self._key

    def _mac(self, payload: bytes) -> bytes:
        return hmac.digest(self._key, payload, "sha256")

    def __repr__(self) -> str:
        return "RecoveryAuthority(<redacted>)"


@dataclass(frozen=True, slots=True)
class RecoveryToken:
    """Durable names and journal identity authenticated by a separate authority."""

    journal_name: str
    data_name: str
    journal_sha256: str
    journal_identity: Identity


@dataclass(frozen=True, slots=True)
class CasApplied:
    previous_sha256: str
    current_sha256: str
    recovery: RecoveryToken


@dataclass(frozen=True, slots=True)
class CasRecovered:
    current_sha256: str
    changed: bool
    recovery: RecoveryToken


@dataclass(frozen=True, slots=True)
class CasFailure:
    state: CasFailureState
    detail: str
    recovery: RecoveryToken | None = None


type CasResult = CasApplied | CasFailure
type RecoveryResult = CasRecovered | CasFailure
type _PhaseHook = Callable[[CasPhase], None]


@dataclass(frozen=True, slots=True)
class _JournalRecord:
    target_hash: bytes
    recovery_names_hash: bytes
    directory_chain: tuple[Identity, ...]
    original: Identity
    original_digest: bytes
    replacement: Identity
    replacement_digest: bytes


@dataclass(frozen=True, slots=True)
class _RecoveryNames:
    journal: str
    data: str


class _BoundTarget:
    def __init__(self, chain: BoundDirectory, name: str, file_fd: int, identity: Identity) -> None:
        self.chain = chain
        self.name = name
        self.file_fd = file_fd
        self.identity = identity

    @classmethod
    def open(cls, path: Path) -> _BoundTarget:
        if not path.is_absolute() or path.name in ("", ".", ".."):
            raise ValueError("target must be an absolute path naming a file")
        chain = bind_directory(path.parent)
        file_fd: int | None = None
        try:
            file_fd = os.open(path.name, _FILE_OPEN, dir_fd=chain.fd)
            value = os.fstat(file_fd)
            if not stat.S_ISREG(value.st_mode):
                raise ValueError("target is not a regular file")
            return cls(chain, path.name, file_fd, Identity.from_stat(value))
        except BaseException:
            if file_fd is not None:
                os.close(file_fd)
            chain.close()
            raise

    def attached_as(self, identity: Identity) -> bool:
        try:
            return entry_identity(self.chain.fd, self.name).same_object(identity)
        except OSError:
            return False

    def unchanged(self, expected_digest: bytes) -> bool:
        return (
            Identity.from_fd(self.file_fd) == self.identity
            and digest_fd(self.file_fd) == expected_digest
        )

    def close(self) -> None:
        os.close(self.file_fd)
        self.chain.close()


def durable_compare_exchange(
    path: Path,
    expected: bytes,
    replacement: bytes,
    *,
    authority: RecoveryAuthority,
    _phase_hook: _PhaseHook | None = None,
) -> CasResult:
    """Atomically exchange exact bytes and retain both durable versions for recovery."""
    if expected == replacement:
        return CasFailure(CasFailureState.EXPECTED_MISMATCH, "replacement equals expected bytes")
    if len(expected) > _MAX_FILE_BYTES or len(replacement) > _MAX_FILE_BYTES:
        return CasFailure(CasFailureState.IO_FAILURE, "input exceeds the supported size")
    bound: _BoundTarget | None = None
    data_fd: int | None = None
    journal_fd: int | None = None
    token: RecoveryToken | None = None
    exchange_completed = False
    try:
        bound = _BoundTarget.open(path)
        expected_digest = hashlib.sha256(expected).digest()
        if read_all(bound.file_fd) != expected:
            return CasFailure(CasFailureState.EXPECTED_MISMATCH, "target bytes do not match")
        if not bound.chain.validate() or not bound.attached_as(bound.identity):
            return CasFailure(CasFailureState.NAMESPACE_DRIFT, "binding changed during validation")
        os.fsync(bound.file_fd)
        if (
            not bound.chain.validate()
            or not bound.attached_as(bound.identity)
            or not bound.unchanged(expected_digest)
        ):
            return CasFailure(
                CasFailureState.NAMESPACE_DRIFT,
                "target changed while its original bytes were made durable",
            )
        _call_hook(_phase_hook, CasPhase.BOUND)
        names, data_fd = _prepare_data(bound, replacement)
        replacement_identity = Identity.from_fd(data_fd)
        replacement_digest = hashlib.sha256(replacement).digest()
        record = _JournalRecord(
            _target_hash(path),
            _recovery_names_hash(names),
            bound.chain.identities,
            bound.identity,
            expected_digest,
            replacement_identity,
            replacement_digest,
        )
        journal_fd = _write_journal(bound.chain.fd, names.journal, record, authority)
        token = RecoveryToken(
            names.journal,
            names.data,
            digest_fd(journal_fd).hex(),
            Identity.from_fd(journal_fd),
        )
        os.fsync(bound.chain.fd)
        _call_hook(_phase_hook, CasPhase.PREPARED)
        _call_hook(_phase_hook, CasPhase.BEFORE_EXCHANGE)
        if not _prepared_pair_is_attached(bound, token, data_fd, journal_fd, record):
            return CasFailure(
                CasFailureState.NAMESPACE_DRIFT,
                "namespace or content changed at the exchange boundary",
                token,
            )
        exchange_error = _exchange(bound.chain.fd, token.data_name, bound.name)
        if exchange_error is not None:
            return CasFailure(exchange_error[0], exchange_error[1], token)
        exchange_completed = True
        _call_hook(_phase_hook, CasPhase.AFTER_EXCHANGE)
        pair = _open_pair(bound.chain.fd, bound.name, token.data_name)
        if isinstance(pair, CasFailure):
            return CasFailure(pair.state, pair.detail, token)
        target_fd, displaced_fd = pair
        try:
            pair_valid = _committed_pair_is_attached(
                bound,
                token,
                target_fd,
                displaced_fd,
                journal_fd,
                record,
            )
            namespace_valid = bound.chain.validate()
            if not pair_valid or not namespace_valid:
                return _rollback(
                    bound,
                    token,
                    record,
                    target_fd,
                    displaced_fd,
                    journal_fd,
                    _phase_hook,
                )
            os.fsync(bound.chain.fd)
            _call_hook(_phase_hook, CasPhase.DURABLE)
            if (
                not bound.chain.validate()
                or not _entry_matches_fd(bound.chain.fd, bound.name, target_fd)
                or not _entry_matches_fd(bound.chain.fd, token.data_name, displaced_fd)
                or not _entry_matches_fd(bound.chain.fd, token.journal_name, journal_fd)
                or not _exact_pair(
                    target_fd,
                    record.replacement,
                    record.replacement_digest,
                    displaced_fd,
                    record.original,
                    record.original_digest,
                )
                or Identity.from_fd(journal_fd) != token.journal_identity
                or digest_fd(journal_fd).hex() != token.journal_sha256
            ):
                return CasFailure(
                    CasFailureState.INDETERMINATE,
                    "post-durability identity verification failed",
                    token,
                )
            return CasApplied(expected_digest.hex(), replacement_digest.hex(), token)
        finally:
            os.close(target_fd)
            os.close(displaced_fd)
    except (OSError, ValueError) as error:
        state = CasFailureState.INDETERMINATE if exchange_completed else CasFailureState.IO_FAILURE
        return CasFailure(state, str(error), token)
    finally:
        if journal_fd is not None:
            os.close(journal_fd)
        if data_fd is not None:
            os.close(data_fd)
        if bound is not None:
            bound.close()


def recover_exchange(
    path: Path,
    recovery: RecoveryToken,
    desired_sha256: str,
    *,
    authority: RecoveryAuthority,
    _phase_hook: _PhaseHook | None = None,
) -> RecoveryResult:
    """Durably select a journaled version, refusing any unrecognized pair or drift."""
    bound: _BoundTarget | None = None
    journal_fd: int | None = None
    data_fd: int | None = None
    exchange_completed = False
    try:
        desired = bytes.fromhex(desired_sha256)
        if len(desired) != hashlib.sha256().digest_size:
            raise ValueError("desired digest must be SHA-256")
        bound = _BoundTarget.open(path)
        names = _validate_recovery_names(bound.name, recovery)
        journal_fd = os.open(recovery.journal_name, _FILE_OPEN, dir_fd=bound.chain.fd)
        data_fd = os.open(recovery.data_name, _FILE_OPEN, dir_fd=bound.chain.fd)
        if (
            Identity.from_fd(journal_fd) != recovery.journal_identity
            or digest_fd(journal_fd).hex() != recovery.journal_sha256
            or not _entry_matches_fd(bound.chain.fd, recovery.journal_name, journal_fd)
        ):
            raise ValueError("recovery journal authentication failed")
        record = _decode_journal(read_all(journal_fd), authority)
        if record.target_hash != _target_hash(path):
            raise ValueError("journal belongs to another target")
        if record.recovery_names_hash != _recovery_names_hash(names):
            raise ValueError("journal belongs to another recovery name pair")
        if not _same_directory_chain(bound.chain.identities, record.directory_chain):
            raise ValueError("journal belongs to another directory chain")
        if desired not in (record.original_digest, record.replacement_digest):
            raise ValueError("desired digest is absent from the journal")
        if not bound.chain.validate(exact_metadata=False):
            return CasFailure(CasFailureState.NAMESPACE_DRIFT, "directory chain changed", recovery)
        if (
            not bound.attached_as(bound.identity)
            or not _entry_matches_fd(bound.chain.fd, recovery.data_name, data_fd)
            or not _entry_matches_fd(bound.chain.fd, recovery.journal_name, journal_fd)
        ):
            return CasFailure(
                CasFailureState.NAMESPACE_DRIFT,
                "journaled pair attachment changed before recovery",
                recovery,
            )
        target_digest = digest_fd(bound.file_fd)
        data_digest = digest_fd(data_fd)
        if not _recognized_pair(
            bound.identity, target_digest, Identity.from_fd(data_fd), data_digest, record
        ):
            return CasFailure(
                CasFailureState.RECOVERY_REQUIRED, "journaled pair is unrecognized", recovery
            )
        if target_digest == desired:
            os.fsync(bound.chain.fd)
            if not _recovery_pair_is_attached(
                bound,
                recovery,
                journal_fd,
                data_fd,
                record,
            ):
                return CasFailure(
                    CasFailureState.NAMESPACE_DRIFT,
                    "journaled pair attachment changed during recovery",
                    recovery,
                )
            return CasRecovered(desired.hex(), False, recovery)
        _call_hook(_phase_hook, CasPhase.BEFORE_RECOVERY_EXCHANGE)
        if not _recovery_pair_is_attached(
            bound,
            recovery,
            journal_fd,
            data_fd,
            record,
        ):
            return CasFailure(
                CasFailureState.NAMESPACE_DRIFT,
                "journaled pair changed at the recovery exchange boundary",
                recovery,
            )
        exchange_error = _exchange(bound.chain.fd, recovery.data_name, bound.name)
        if exchange_error is not None:
            return CasFailure(exchange_error[0], exchange_error[1], recovery)
        exchange_completed = True
        os.fsync(bound.chain.fd)
        target_now = os.open(bound.name, _FILE_OPEN, dir_fd=bound.chain.fd)
        data_now = os.open(recovery.data_name, _FILE_OPEN, dir_fd=bound.chain.fd)
        try:
            target_identity = Identity.from_fd(target_now)
            data_identity = Identity.from_fd(data_now)
            target_digest = digest_fd(target_now)
            data_digest = digest_fd(data_now)
            if (
                not bound.chain.validate()
                or target_digest != desired
                or not _recognized_pair(
                    target_identity,
                    target_digest,
                    data_identity,
                    data_digest,
                    record,
                )
                or not _entry_matches_fd(bound.chain.fd, bound.name, target_now)
                or not _entry_matches_fd(bound.chain.fd, recovery.data_name, data_now)
                or not _entry_matches_fd(bound.chain.fd, recovery.journal_name, journal_fd)
                or Identity.from_fd(journal_fd) != recovery.journal_identity
                or digest_fd(journal_fd).hex() != recovery.journal_sha256
            ):
                return CasFailure(
                    CasFailureState.INDETERMINATE,
                    "recovery final verification failed",
                    recovery,
                )
        finally:
            os.close(target_now)
            os.close(data_now)
        return CasRecovered(desired.hex(), True, recovery)
    except (OSError, ValueError) as error:
        state = CasFailureState.INDETERMINATE if exchange_completed else CasFailureState.IO_FAILURE
        return CasFailure(state, str(error), recovery)
    finally:
        if data_fd is not None:
            os.close(data_fd)
        if journal_fd is not None:
            os.close(journal_fd)
        if bound is not None:
            bound.close()


def _prepared_pair_is_attached(
    bound: _BoundTarget,
    token: RecoveryToken,
    data_fd: int,
    journal_fd: int,
    record: _JournalRecord,
) -> bool:
    return (
        bound.chain.validate()
        and bound.attached_as(bound.identity)
        and bound.unchanged(record.original_digest)
        and _entry_matches_fd(bound.chain.fd, token.data_name, data_fd)
        and _stable_metadata_matches(Identity.from_fd(data_fd), record.replacement)
        and digest_fd(data_fd) == record.replacement_digest
        and _entry_matches_fd(bound.chain.fd, token.journal_name, journal_fd)
        and Identity.from_fd(journal_fd) == token.journal_identity
        and digest_fd(journal_fd).hex() == token.journal_sha256
    )


def _recovery_pair_is_attached(
    bound: _BoundTarget,
    recovery: RecoveryToken,
    journal_fd: int,
    data_fd: int,
    record: _JournalRecord,
) -> bool:
    target_identity = Identity.from_fd(bound.file_fd)
    data_identity = Identity.from_fd(data_fd)
    target_digest = digest_fd(bound.file_fd)
    data_digest = digest_fd(data_fd)
    return (
        bound.chain.validate()
        and bound.attached_as(target_identity)
        and _entry_matches_fd(bound.chain.fd, recovery.data_name, data_fd)
        and _entry_matches_fd(bound.chain.fd, recovery.journal_name, journal_fd)
        and Identity.from_fd(journal_fd) == recovery.journal_identity
        and digest_fd(journal_fd).hex() == recovery.journal_sha256
        and _recognized_pair(
            target_identity,
            target_digest,
            data_identity,
            data_digest,
            record,
        )
    )


def _committed_pair_is_attached(
    bound: _BoundTarget,
    token: RecoveryToken,
    target_fd: int,
    displaced_fd: int,
    journal_fd: int,
    record: _JournalRecord,
) -> bool:
    return (
        _entry_matches_fd(bound.chain.fd, bound.name, target_fd)
        and _entry_matches_fd(bound.chain.fd, token.data_name, displaced_fd)
        and _entry_matches_fd(bound.chain.fd, token.journal_name, journal_fd)
        and _exact_pair(
            target_fd,
            record.replacement,
            record.replacement_digest,
            displaced_fd,
            record.original,
            record.original_digest,
        )
        and Identity.from_fd(journal_fd) == token.journal_identity
        and digest_fd(journal_fd).hex() == token.journal_sha256
    )


def _prepare_data(bound: _BoundTarget, replacement: bytes) -> tuple[_RecoveryNames, int]:
    target_tag = hashlib.sha256(os.fsencode(bound.name)).hexdigest()[:12]
    for _attempt in range(128):
        nonce = os.urandom(16).hex()
        base = f".lpcas-{target_tag}-{nonce}"
        names = _RecoveryNames(f"{base}.journal", f"{base}.data")
        try:
            data_fd = os.open(
                names.data,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                stat.S_IMODE(bound.identity.mode),
                dir_fd=bound.chain.fd,
            )
        except FileExistsError:
            continue
        try:
            _write_all(data_fd, replacement)
            os.fchown(data_fd, bound.identity.uid, bound.identity.gid)
            os.fchmod(data_fd, stat.S_IMODE(bound.identity.mode))
            os.fsync(data_fd)
            return names, data_fd
        except BaseException:
            os.close(data_fd)
            raise
    raise OSError(errno.EEXIST, "could not allocate a recovery data name")


def _write_journal(
    parent_fd: int,
    name: str,
    record: _JournalRecord,
    authority: RecoveryAuthority,
) -> int:
    fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        _write_all(fd, _encode_journal(record, authority))
        os.fsync(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _rollback(
    bound: _BoundTarget,
    token: RecoveryToken,
    record: _JournalRecord,
    target_fd: int,
    displaced_fd: int,
    journal_fd: int,
    phase_hook: _PhaseHook | None,
) -> CasFailure:
    _call_hook(phase_hook, CasPhase.BEFORE_ROLLBACK)
    if not (
        _entry_matches_fd(bound.chain.fd, bound.name, target_fd)
        and _entry_matches_fd(bound.chain.fd, token.data_name, displaced_fd)
        and _entry_matches_fd(bound.chain.fd, token.journal_name, journal_fd)
        and _exact_pair(
            target_fd,
            record.replacement,
            record.replacement_digest,
            displaced_fd,
            record.original,
            record.original_digest,
        )
        and Identity.from_fd(journal_fd) == token.journal_identity
        and digest_fd(journal_fd).hex() == token.journal_sha256
    ):
        return CasFailure(
            CasFailureState.INDETERMINATE,
            "exchanged pair changed before rollback; no second exchange was attempted",
            token,
        )
    exchange_error = _exchange(bound.chain.fd, token.data_name, bound.name)
    if exchange_error is not None:
        return CasFailure(CasFailureState.INDETERMINATE, exchange_error[1], token)
    os.fsync(bound.chain.fd)
    if not (
        _entry_matches_fd(bound.chain.fd, bound.name, displaced_fd)
        and _entry_matches_fd(bound.chain.fd, token.data_name, target_fd)
        and _entry_matches_fd(bound.chain.fd, token.journal_name, journal_fd)
        and _exact_pair(
            displaced_fd,
            record.original,
            record.original_digest,
            target_fd,
            record.replacement,
            record.replacement_digest,
        )
        and Identity.from_fd(journal_fd) == token.journal_identity
        and digest_fd(journal_fd).hex() == token.journal_sha256
    ):
        return CasFailure(
            CasFailureState.INDETERMINATE, "rollback identity verification failed", token
        )
    state = (
        CasFailureState.NAMESPACE_DRIFT
        if not bound.chain.validate()
        else CasFailureState.RACE_ROLLED_BACK
    )
    return CasFailure(state, "exchange validation failed and was durably rolled back", token)


def _open_pair(parent_fd: int, target_name: str, data_name: str) -> tuple[int, int] | CasFailure:
    try:
        target_fd = os.open(target_name, _FILE_OPEN, dir_fd=parent_fd)
        try:
            data_fd = os.open(data_name, _FILE_OPEN, dir_fd=parent_fd)
        except BaseException:
            os.close(target_fd)
            raise
        return target_fd, data_fd
    except OSError as error:
        return CasFailure(
            CasFailureState.INDETERMINATE, f"exchanged pair cannot be opened: {error}"
        )


def _recognized_pair(
    target_identity: Identity,
    target_digest: bytes,
    data_identity: Identity,
    data_digest: bytes,
    record: _JournalRecord,
) -> bool:
    original_new = (
        _stable_metadata_matches(target_identity, record.original)
        and target_digest == record.original_digest
        and _stable_metadata_matches(data_identity, record.replacement)
        and data_digest == record.replacement_digest
    )
    new_original = (
        _stable_metadata_matches(target_identity, record.replacement)
        and target_digest == record.replacement_digest
        and _stable_metadata_matches(data_identity, record.original)
        and data_digest == record.original_digest
    )
    return original_new or new_original


def _exchange(parent_fd: int, first: str, second: str) -> tuple[CasFailureState, str] | None:
    result = _renameat2(
        parent_fd,
        os.fsencode(first),
        parent_fd,
        os.fsencode(second),
        _RENAME_EXCHANGE,
    )
    if result == 0:
        return None
    error_number = ctypes.get_errno()
    state = (
        CasFailureState.UNSUPPORTED
        if error_number in (errno.ENOSYS, errno.EINVAL)
        else CasFailureState.IO_FAILURE
    )
    return state, os.strerror(error_number)


def _entry_matches_fd(parent_fd: int, name: str, fd: int) -> bool:
    try:
        return entry_identity(parent_fd, name).same_object(Identity.from_fd(fd))
    except OSError:
        return False


def _exact_pair(
    first_fd: int,
    first_identity: Identity,
    first_digest: bytes,
    second_fd: int,
    second_identity: Identity,
    second_digest: bytes,
) -> bool:
    return (
        _stable_metadata_matches(Identity.from_fd(first_fd), first_identity)
        and digest_fd(first_fd) == first_digest
        and _stable_metadata_matches(Identity.from_fd(second_fd), second_identity)
        and digest_fd(second_fd) == second_digest
    )


def _stable_metadata_matches(current: Identity, expected: Identity) -> bool:
    return (
        current.same_object(expected)
        and current.mode == expected.mode
        and current.uid == expected.uid
        and current.gid == expected.gid
        and current.link_count == expected.link_count
        and current.size_bytes == expected.size_bytes
        and current.modified_ns == expected.modified_ns
    )


def _write_all(fd: int, value: bytes) -> None:
    offset_bytes = 0
    while offset_bytes < len(value):
        offset_bytes += os.write(fd, value[offset_bytes:])


def _target_hash(path: Path) -> bytes:
    return hashlib.sha256(os.fsencode(path)).digest()


def _validate_recovery_names(target_name: str, recovery: RecoveryToken) -> _RecoveryNames:
    journal_value = cast(object, recovery.journal_name)
    data_value = cast(object, recovery.data_name)
    if not isinstance(journal_value, str) or not isinstance(data_value, str):
        raise ValueError("recovery names must be strings")
    target_tag = hashlib.sha256(os.fsencode(target_name)).hexdigest()[:12]
    prefix = f".lpcas-{target_tag}-"
    suffix = ".journal"
    journal_name = journal_value
    if not journal_name.startswith(prefix) or not journal_name.endswith(suffix):
        raise ValueError("recovery journal name is not bound to this target")
    nonce = journal_name[len(prefix) : -len(suffix)]
    if len(nonce) != 32 or any(character not in _LOWER_HEX for character in nonce):
        raise ValueError("recovery journal nonce is invalid")
    if data_value != f"{prefix}{nonce}.data":
        raise ValueError("recovery data name does not match the journal")
    return _RecoveryNames(journal_name, data_value)


def _recovery_names_hash(names: _RecoveryNames) -> bytes:
    digest = hashlib.sha256()
    for name in (names.journal, names.data):
        encoded = os.fsencode(name)
        digest.update(struct.pack("!I", len(encoded)))
        digest.update(encoded)
    return digest.digest()


def _identity_values(identity: Identity) -> tuple[int, ...]:
    return (
        identity.device_major,
        identity.device_minor,
        identity.inode,
        identity.mode,
        identity.uid,
        identity.gid,
        identity.link_count,
        identity.size_bytes,
        identity.modified_ns,
        identity.changed_ns,
    )


def _identity_from(values: tuple[int, ...]) -> Identity:
    return Identity(*values)


def _encode_journal(record: _JournalRecord, authority: RecoveryAuthority) -> bytes:
    payload = b"".join(
        (
            _JOURNAL_PREFIX.pack(
                _JOURNAL_MAGIC,
                record.target_hash,
                record.recovery_names_hash,
                len(record.directory_chain),
            ),
            *(_IDENTITY.pack(*_identity_values(identity)) for identity in record.directory_chain),
            _IDENTITY.pack(*_identity_values(record.original)),
            record.original_digest,
            _IDENTITY.pack(*_identity_values(record.replacement)),
            record.replacement_digest,
        )
    )
    return payload + authority._mac(payload)


def _decode_journal(value: bytes, authority: RecoveryAuthority) -> _JournalRecord:
    if len(value) < _JOURNAL_PREFIX.size + 2 * _IDENTITY.size + 64 + _JOURNAL_MAC_BYTES:
        raise ValueError("invalid journal size")
    payload = value[:-_JOURNAL_MAC_BYTES]
    supplied_mac = value[-_JOURNAL_MAC_BYTES:]
    if not hmac.compare_digest(authority._mac(payload), supplied_mac):
        raise ValueError("recovery journal MAC authentication failed")
    magic, target_hash, recovery_names_hash, chain_count = _JOURNAL_PREFIX.unpack_from(payload)
    if magic != _JOURNAL_MAGIC:
        raise ValueError("invalid journal capability/version")
    expected_size = _JOURNAL_PREFIX.size + (chain_count + 2) * _IDENTITY.size + 64
    if chain_count == 0 or len(payload) != expected_size:
        raise ValueError("invalid journal directory chain")
    offset = _JOURNAL_PREFIX.size
    chain: list[Identity] = []
    for _index in range(chain_count):
        chain.append(_identity_from(_IDENTITY.unpack_from(payload, offset)))
        offset += _IDENTITY.size
    original = _identity_from(_IDENTITY.unpack_from(payload, offset))
    offset += _IDENTITY.size
    original_digest = payload[offset : offset + 32]
    offset += 32
    replacement = _identity_from(_IDENTITY.unpack_from(payload, offset))
    offset += _IDENTITY.size
    replacement_digest = payload[offset : offset + 32]
    return _JournalRecord(
        target_hash=target_hash,
        recovery_names_hash=recovery_names_hash,
        directory_chain=tuple(chain),
        original=original,
        original_digest=original_digest,
        replacement=replacement,
        replacement_digest=replacement_digest,
    )


def _same_directory_chain(current: tuple[Identity, ...], expected: tuple[Identity, ...]) -> bool:
    return len(current) == len(expected) and all(
        present.same_object(recorded)
        and present.mode == recorded.mode
        and present.uid == recorded.uid
        and present.gid == recorded.gid
        for present, recorded in zip(current, expected, strict=True)
    )


def _call_hook(hook: _PhaseHook | None, phase: CasPhase) -> None:
    if hook is not None:
        hook(phase)
