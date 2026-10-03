from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omo_manager import cc_ten_external_verifier as verifier


def test_unsealed_candidate_fails_before_reading_external_files():
    with patch.object(verifier, "LAUNCHER") as launcher:
        with pytest.raises(ValueError, match="launcher pin is absent"):
            verifier.verify_candidate()
    launcher.read_bytes.assert_not_called()


def test_pins_without_separate_principal_do_not_grant_authority(monkeypatch):
    monkeypatch.setattr(verifier, "REVIEWED_LAUNCHER_SHA256", "a" * 64)
    monkeypatch.setattr(verifier, "REVIEWED_CLIENT_SHA256", "b" * 64)
    monkeypatch.setattr(verifier, "REVIEWER_PUBLIC_KEY_HEX", "c" * 64)
    with patch.object(verifier, "LAUNCHER") as launcher:
        with pytest.raises(ValueError, match="separate verifier principal is absent"):
            verifier.verify_candidate()
    launcher.read_bytes.assert_not_called()


@pytest.fixture
def sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    files = {name: tmp_path / name for name in (
        "launcher.py", "client.py", "approval.json", "manifest.json", "closure.json", "socket", "log",
    )}
    for name, content in (
        ("launcher.py", b"raise RuntimeError('exact launcher bytes ran')\n"),
        ("client.py", b"client = True\n"),
        ("manifest.json", b"ten pinned inputs\n"),
        ("closure.json", b"pinned source closure\n"),
    ):
        files[name].write_bytes(content)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    monkeypatch.setattr(verifier, "REVIEWED_LAUNCHER_SHA256", verifier.digest(files["launcher.py"].read_bytes()))
    monkeypatch.setattr(verifier, "REVIEWED_CLIENT_SHA256", verifier.digest(files["client.py"].read_bytes()))
    monkeypatch.setattr(verifier, "REVIEWER_PUBLIC_KEY_HEX", public)
    monkeypatch.setattr(verifier, "TRUSTED_VERIFIER_UID", 0)
    monkeypatch.setattr(verifier, "verify_trust_boundary", lambda: None)
    monkeypatch.setattr(verifier, "MANIFEST_SHA256", verifier.digest(files["manifest.json"].read_bytes()))
    for attribute, key_name in (
        ("LAUNCHER", "launcher.py"), ("CLIENT", "client.py"), ("APPROVAL", "approval.json"),
        ("MANIFEST", "manifest.json"), ("CLOSURE", "closure.json"), ("SOCKET", "socket"),
        ("LOG", "log"),
    ):
        monkeypatch.setattr(verifier, attribute, files[key_name])
    monkeypatch.setattr(verifier, "OUTPUT_DIR", tmp_path / "acquisition")
    socket = SimpleNamespace(st_mode=stat.S_IFSOCK, st_dev=11, st_ino=21, st_uid=os.geteuid())
    log = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=11, st_ino=22, st_uid=os.geteuid())
    monkeypatch.setattr(Path, "lstat", lambda path: socket if path == files["socket"] else log)
    monkeypatch.setattr(verifier, "process_identity", lambda pid: (101 + pid, "a" * 64, "b" * 64))
    pane_start = b"python scorer --log " + str(files["log"]).encode()
    monkeypatch.setattr(verifier.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout=b"41|" + pane_start + b"\n",
    ))
    payload = {
        "schema_version": 1, "review_verdict": "ACCEPT", "manifest_sha256": verifier.MANIFEST_SHA256,
        "client_sha256": verifier.REVIEWED_CLIENT_SHA256,
        "launcher_sha256": verifier.REVIEWED_LAUNCHER_SHA256,
        "closure_sha256": verifier.digest(files["closure.json"].read_bytes()),
        "output_dir": str(verifier.OUTPUT_DIR), "scorer_pid": 41, "scorer_start_ticks": 142,
        "bino_pid": 42, "bino_start_ticks": 143, "socket_device": 11, "socket_inode": 21,
        "log_device": 11, "log_inode": 22, "pane_start_sha256": verifier.digest(pane_start),
    }
    for prefix in ("scorer", "bino"):
        payload[f"{prefix}_cmdline_sha256"] = "a" * 64
        payload[f"{prefix}_exe_sha256"] = "b" * 64

    def sign() -> None:
        files["approval.json"].write_bytes(verifier.canonical({
            "payload": payload, "signature": key.sign(verifier.canonical(payload)).hex(),
        }))

    sign()
    return files, payload, sign


def test_valid_candidate_only_reads_pinned_evidence(sealed):
    files, payload, _ = sealed
    snapshot = verifier.verify_candidate()
    assert snapshot.payload == payload
    assert snapshot.launcher_bytes == files["launcher.py"].read_bytes()
    assert snapshot.approval_sha256 == hashlib.sha256(files["approval.json"].read_bytes()).hexdigest()


def test_signed_payload_drift_fails_before_process_inspection(sealed, monkeypatch):
    files, payload, _ = sealed
    envelope = json.loads(files["approval.json"].read_bytes())
    envelope["payload"]["scorer_pid"] = 99
    files["approval.json"].write_bytes(verifier.canonical(envelope))
    with patch.object(verifier, "process_identity") as process:
        with pytest.raises(ValueError, match="invalid reviewer signature"):
            verifier.verify_candidate()
    process.assert_not_called()


def test_renamed_launcher_after_snapshot_does_not_change_verified_bytes(sealed):
    files, _, _ = sealed
    snapshot = verifier.verify_candidate()
    files["launcher.py"].write_bytes(b"raise RuntimeError('unreviewed launcher ran')\n")
    assert snapshot.launcher_bytes != files["launcher.py"].read_bytes()
    with pytest.raises(ValueError, match="reviewed launcher drifted"):
        verifier.verify_candidate()


def test_verify_never_executes_launcher_snapshot(sealed):
    snapshot = verifier.verify_candidate()
    assert snapshot.launcher_bytes.startswith(b"raise RuntimeError")
