from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from lifecycle_platform import durable_cas as cas_module
from lifecycle_platform._bindings import Identity
from lifecycle_platform.durable_cas import (
    _MAX_FILE_BYTES,
    CasApplied,
    CasFailure,
    CasFailureState,
    CasPhase,
    CasRecovered,
    RecoveryAuthority,
    durable_compare_exchange,
    recover_exchange,
)

_OLD = b"task: pending\n"
_NEW = b"task: complete\n"


@pytest.fixture
def authority() -> RecoveryAuthority:
    return RecoveryAuthority.generate()


def test_exchange_is_durable_and_both_versions_are_recoverable(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)

    applied = durable_compare_exchange(target, _OLD, _NEW, authority=authority)

    assert isinstance(applied, CasApplied)
    assert target.read_bytes() == _NEW
    assert (tmp_path / applied.recovery.data_name).read_bytes() == _OLD
    restored = recover_exchange(
        target, applied.recovery, hashlib.sha256(_OLD).hexdigest(), authority=authority
    )
    assert isinstance(restored, CasRecovered)
    assert restored.changed
    assert target.read_bytes() == _OLD
    repeated = recover_exchange(
        target, applied.recovery, hashlib.sha256(_OLD).hexdigest(), authority=authority
    )
    assert isinstance(repeated, CasRecovered)
    assert not repeated.changed


def test_expected_mismatch_has_no_namespace_side_effect(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)

    result = durable_compare_exchange(target, b"wrong", _NEW, authority=authority)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.EXPECTED_MISMATCH
    assert target.read_bytes() == _OLD
    assert tuple(tmp_path.iterdir()) == (target,)


def test_original_target_is_fsynced_before_preparation(
    tmp_path: Path, authority: RecoveryAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)
    target_key = (target.stat().st_dev, target.stat().st_ino)
    target_fsyncs = 0
    observed_before_preparation = False
    real_fsync = cas_module.os.fsync

    def observe_fsync(fd: int) -> None:
        nonlocal target_fsyncs
        value = os.fstat(fd)
        if (value.st_dev, value.st_ino) == target_key:
            target_fsyncs += 1
        real_fsync(fd)

    def observe_phase(phase: CasPhase) -> None:
        nonlocal observed_before_preparation
        if phase is CasPhase.BOUND:
            assert target_fsyncs == 1
            observed_before_preparation = True

    monkeypatch.setattr(cas_module.os, "fsync", observe_fsync)
    result = durable_compare_exchange(
        target, _OLD, _NEW, authority=authority, _phase_hook=observe_phase
    )

    assert isinstance(result, CasApplied)
    assert target_fsyncs == 1
    assert observed_before_preparation


def test_final_component_swap_at_exchange_boundary_is_not_exchanged(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    parked = tmp_path / "original"
    target.write_bytes(_OLD)

    def swap(phase: CasPhase) -> None:
        if phase is CasPhase.BEFORE_EXCHANGE:
            target.rename(parked)
            target.write_bytes(b"attacker\n")

    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority, _phase_hook=swap)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.NAMESPACE_DRIFT
    assert target.read_bytes() == b"attacker\n"
    assert parked.read_bytes() == _OLD
    assert result.recovery is not None
    assert (tmp_path / result.recovery.data_name).read_bytes() == _NEW


def test_final_component_swap_in_kernel_window_is_indeterminate(
    tmp_path: Path, authority: RecoveryAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "TODO"
    parked = tmp_path / "original"
    target.write_bytes(_OLD)
    real_exchange = cas_module._exchange
    first_exchange = True

    def swap_then_exchange(
        parent_fd: int, first: str, second: str
    ) -> tuple[CasFailureState, str] | None:
        nonlocal first_exchange
        if first_exchange:
            first_exchange = False
            target.rename(parked)
            target.write_bytes(b"attacker\n")
        return real_exchange(parent_fd, first, second)

    monkeypatch.setattr(cas_module, "_exchange", swap_then_exchange)
    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.INDETERMINATE
    assert target.read_bytes() == _NEW
    assert parked.read_bytes() == _OLD
    assert result.recovery is not None
    assert (tmp_path / result.recovery.data_name).read_bytes() == b"attacker\n"


def test_in_place_content_race_at_exchange_boundary_is_not_exchanged(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)

    def mutate(phase: CasPhase) -> None:
        if phase is CasPhase.BEFORE_EXCHANGE:
            target.write_bytes(b"concurrent writer\n")

    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority, _phase_hook=mutate)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.NAMESPACE_DRIFT
    assert target.read_bytes() == b"concurrent writer\n"
    assert result.recovery is not None
    assert (tmp_path / result.recovery.data_name).read_bytes() == _NEW


def test_ancestor_swap_at_linearization_mutates_no_public_replacement(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "work"
    parent.mkdir(parents=True)
    target = parent / "TODO"
    target.write_bytes(_OLD)
    retained_ancestor = tmp_path / "retained-ancestor"

    def swap(phase: CasPhase) -> None:
        if phase is CasPhase.BEFORE_EXCHANGE:
            ancestor.rename(retained_ancestor)
            replacement_parent = ancestor / "work"
            replacement_parent.mkdir(parents=True)
            (replacement_parent / "TODO").write_bytes(b"public replacement\n")

    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority, _phase_hook=swap)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.NAMESPACE_DRIFT
    assert target.read_bytes() == b"public replacement\n"
    assert (retained_ancestor / "work" / "TODO").read_bytes() == _OLD


def test_final_component_swap_after_durability_is_indeterminate_not_overwritten(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    captured = tmp_path / "captured-new"
    target.write_bytes(_OLD)

    def swap(phase: CasPhase) -> None:
        if phase is CasPhase.DURABLE:
            target.rename(captured)
            target.write_bytes(b"post-commit attacker\n")

    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority, _phase_hook=swap)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.INDETERMINATE
    assert target.read_bytes() == b"post-commit attacker\n"
    assert captured.read_bytes() == _NEW
    assert result.recovery is not None
    assert (tmp_path / result.recovery.data_name).read_bytes() == _OLD


def test_recovery_rejects_same_uid_journal_replacement(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)
    applied = durable_compare_exchange(target, _OLD, _NEW, authority=authority)
    assert isinstance(applied, CasApplied)
    journal = tmp_path / applied.recovery.journal_name
    displaced = tmp_path / "real-journal"
    journal.rename(displaced)
    journal.write_bytes(displaced.read_bytes())

    result = recover_exchange(
        target, applied.recovery, hashlib.sha256(_OLD).hexdigest(), authority=authority
    )

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.IO_FAILURE
    assert target.read_bytes() == _NEW


def test_recovery_rejects_journal_from_a_forged_authority(tmp_path: Path) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)
    attacker_authority = RecoveryAuthority.generate()
    trusted_authority = RecoveryAuthority.generate()
    forged = durable_compare_exchange(target, _OLD, _NEW, authority=attacker_authority)
    assert isinstance(forged, CasApplied)

    result = recover_exchange(
        target,
        forged.recovery,
        hashlib.sha256(_OLD).hexdigest(),
        authority=trusted_authority,
    )

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.IO_FAILURE
    assert "MAC authentication" in result.detail
    assert target.read_bytes() == _NEW


def test_recovery_rejects_non_leaf_capability_names(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    outside = tmp_path / "outside"
    target.write_bytes(_OLD)
    outside.write_bytes(b"outside\n")
    applied = durable_compare_exchange(target, _OLD, _NEW, authority=authority)
    assert isinstance(applied, CasApplied)
    forged = replace(applied.recovery, data_name="../outside")

    result = recover_exchange(target, forged, hashlib.sha256(_OLD).hexdigest(), authority=authority)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.IO_FAILURE
    assert target.read_bytes() == _NEW
    assert outside.read_bytes() == b"outside\n"


def test_recovery_rejects_signed_pair_moved_to_replacement_directory(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    first = tmp_path / "first"
    retained = tmp_path / "retained"
    first.mkdir()
    source_target = first / "TODO"
    source_target.write_bytes(_OLD)
    applied = durable_compare_exchange(source_target, _OLD, _NEW, authority=authority)
    assert isinstance(applied, CasApplied)
    first.rename(retained)
    first.mkdir()
    for name in ("TODO", applied.recovery.data_name, applied.recovery.journal_name):
        (retained / name).rename(first / name)
    moved_recovery = replace(
        applied.recovery,
        journal_identity=Identity.from_stat((first / applied.recovery.journal_name).stat()),
    )

    result = recover_exchange(
        source_target,
        moved_recovery,
        hashlib.sha256(_OLD).hexdigest(),
        authority=authority,
    )

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.IO_FAILURE
    assert "directory chain" in result.detail
    assert source_target.read_bytes() == _NEW


def test_recovery_data_swap_at_exchange_boundary_does_not_exchange(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    parked = tmp_path / "journaled-old"
    target.write_bytes(_OLD)
    applied = durable_compare_exchange(target, _OLD, _NEW, authority=authority)
    assert isinstance(applied, CasApplied)
    data = tmp_path / applied.recovery.data_name

    def swap(phase: CasPhase) -> None:
        if phase is CasPhase.BEFORE_RECOVERY_EXCHANGE:
            data.rename(parked)
            data.write_bytes(b"attacker\n")

    result = recover_exchange(
        target,
        applied.recovery,
        hashlib.sha256(_OLD).hexdigest(),
        authority=authority,
        _phase_hook=swap,
    )

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.NAMESPACE_DRIFT
    assert target.read_bytes() == _NEW
    assert data.read_bytes() == b"attacker\n"
    assert parked.read_bytes() == _OLD


def test_data_swap_after_pair_open_prevents_rollback(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "work"
    parent.mkdir(parents=True)
    target = parent / "TODO"
    target.write_bytes(_OLD)
    retained = tmp_path / "retained"
    data_name = ""

    def swap(phase: CasPhase) -> None:
        nonlocal data_name
        if phase is CasPhase.PREPARED:
            data_name = next(item.name for item in parent.iterdir() if item.name.endswith(".data"))
        elif phase is CasPhase.AFTER_EXCHANGE:
            ancestor.rename(retained)
            replacement_parent = ancestor / "work"
            replacement_parent.mkdir(parents=True)
            (replacement_parent / "TODO").write_bytes(b"public replacement\n")
        elif phase is CasPhase.BEFORE_ROLLBACK:
            data = retained / "work" / data_name
            data.rename(data.with_name("journaled-old"))
            data.write_bytes(b"attacker\n")

    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority, _phase_hook=swap)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.INDETERMINATE
    assert target.read_bytes() == b"public replacement\n"
    assert (retained / "work" / "TODO").read_bytes() == _NEW
    assert (retained / "work" / data_name).read_bytes() == b"attacker\n"
    assert (retained / "work" / "journaled-old").read_bytes() == _OLD


def test_post_exchange_fsync_failure_is_indeterminate(
    tmp_path: Path, authority: RecoveryAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)
    after_exchange = False
    real_fsync = cas_module.os.fsync

    def hook(phase: CasPhase) -> None:
        nonlocal after_exchange
        after_exchange = after_exchange or phase is CasPhase.AFTER_EXCHANGE

    def fail_after_exchange(fd: int) -> None:
        if after_exchange:
            raise OSError("injected post-exchange fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(cas_module.os, "fsync", fail_after_exchange)
    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority, _phase_hook=hook)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.INDETERMINATE
    assert target.read_bytes() == _NEW


def test_post_exchange_hash_failure_is_indeterminate(
    tmp_path: Path, authority: RecoveryAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)
    after_exchange = False
    real_digest = cas_module.digest_fd

    def hook(phase: CasPhase) -> None:
        nonlocal after_exchange
        after_exchange = after_exchange or phase is CasPhase.AFTER_EXCHANGE

    def fail_after_exchange(fd: int) -> bytes:
        if after_exchange:
            raise OSError("injected post-exchange hash failure")
        return real_digest(fd)

    monkeypatch.setattr(cas_module, "digest_fd", fail_after_exchange)
    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority, _phase_hook=hook)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.INDETERMINATE
    assert target.read_bytes() == _NEW


def test_recovery_post_exchange_fsync_failure_is_indeterminate(
    tmp_path: Path, authority: RecoveryAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)
    applied = durable_compare_exchange(target, _OLD, _NEW, authority=authority)
    assert isinstance(applied, CasApplied)
    armed = False
    real_fsync = cas_module.os.fsync

    def hook(phase: CasPhase) -> None:
        nonlocal armed
        armed = armed or phase is CasPhase.BEFORE_RECOVERY_EXCHANGE

    def fail_after_exchange(fd: int) -> None:
        if armed:
            raise OSError("injected recovery fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(cas_module.os, "fsync", fail_after_exchange)
    result = recover_exchange(
        target,
        applied.recovery,
        hashlib.sha256(_OLD).hexdigest(),
        authority=authority,
        _phase_hook=hook,
    )

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.INDETERMINATE
    assert target.read_bytes() == _OLD


def test_already_desired_recovery_fsyncs_parent(
    tmp_path: Path, authority: RecoveryAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)
    applied = durable_compare_exchange(target, _OLD, _NEW, authority=authority)
    assert isinstance(applied, CasApplied)
    calls = 0
    real_fsync = cas_module.os.fsync

    def count_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        real_fsync(fd)

    monkeypatch.setattr(cas_module.os, "fsync", count_fsync)
    result = recover_exchange(
        target, applied.recovery, hashlib.sha256(_NEW).hexdigest(), authority=authority
    )

    assert isinstance(result, CasRecovered)
    assert not result.changed
    assert calls == 1


def test_oversize_replacement_is_rejected_without_preparation(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)

    result = durable_compare_exchange(target, _OLD, bytes(_MAX_FILE_BYTES + 1), authority=authority)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.IO_FAILURE
    assert target.read_bytes() == _OLD
    assert tuple(tmp_path.iterdir()) == (target,)


def test_symlink_target_is_rejected_without_following(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    real = tmp_path / "real"
    target = tmp_path / "TODO"
    real.write_bytes(_OLD)
    os.symlink(real.name, target)

    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.IO_FAILURE
    assert real.read_bytes() == _OLD


def test_fifo_target_fails_closed_without_blocking(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    os.mkfifo(target)

    result = durable_compare_exchange(target, _OLD, _NEW, authority=authority)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.IO_FAILURE
    assert target.is_fifo()


def test_relative_target_is_rejected_before_binding(
    tmp_path: Path, authority: RecoveryAuthority, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "TODO"
    target.write_bytes(_OLD)
    monkeypatch.chdir(tmp_path)

    result = durable_compare_exchange(Path("TODO"), _OLD, _NEW, authority=authority)

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.IO_FAILURE
    assert target.read_bytes() == _OLD


def test_fifo_recovery_data_fails_closed_without_blocking(
    tmp_path: Path, authority: RecoveryAuthority
) -> None:
    target = tmp_path / "TODO"
    parked = tmp_path / "journaled-old"
    target.write_bytes(_OLD)
    applied = durable_compare_exchange(target, _OLD, _NEW, authority=authority)
    assert isinstance(applied, CasApplied)
    data = tmp_path / applied.recovery.data_name
    data.rename(parked)
    os.mkfifo(data)

    result = recover_exchange(
        target, applied.recovery, hashlib.sha256(_OLD).hexdigest(), authority=authority
    )

    assert isinstance(result, CasFailure)
    assert result.state is CasFailureState.RECOVERY_REQUIRED
    assert target.read_bytes() == _NEW
    assert data.is_fifo()
    assert parked.read_bytes() == _OLD
