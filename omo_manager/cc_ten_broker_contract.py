"""Offline contract checks for one protected ten-page dispatch; no I/O or launch."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


# 🧑 "dw:2 concrete next step on exact TEN manifest SHA2ce23e45...: please produce an OFFLINE broker interface contract + adversarial tests (not launch)"
MANIFEST_SHA256 = "2ce23e45ab8bbbee4d087ca26c36796142e3113cc1c0098f617f475f6007d115"
APPROVED_RUNTIME_UID = 30033
REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "request_nonce",
        "manifest_sha256",
        "launcher_sha256",
        "client_sha256",
        "closure_sha256",
        "scorer_pid",
        "scorer_start_ticks",
        "scorer_cmdline_sha256",
        "scorer_exe_sha256",
        "bino_pid",
        "bino_start_ticks",
        "bino_cmdline_sha256",
        "bino_exe_sha256",
        "socket_device",
        "socket_inode",
        "log_device",
        "log_inode",
        "pane_start_sha256",
        "output_dir",
        "runtime_uid",
        "broker_install_sha256",
        "interpreter_sha256",
        "dependency_tree_sha256",
        "server_auth_sha256",
    }
)
LIVE_FIELDS = frozenset(
    {
        "closure_sha256",
        "scorer_pid",
        "scorer_start_ticks",
        "scorer_cmdline_sha256",
        "scorer_exe_sha256",
        "bino_pid",
        "bino_start_ticks",
        "bino_cmdline_sha256",
        "bino_exe_sha256",
        "socket_device",
        "socket_inode",
        "log_device",
        "log_inode",
        "pane_start_sha256",
        "output_dir",
        "server_auth_sha256",
    }
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True)
class ProtectedSnapshot:
    reviewer_public_key: bytes
    broker_uid: int
    runtime_uid: int
    immutable_ancestors: bool
    interpreter_sha256: str
    dependency_tree_sha256: str
    broker_install_sha256: str
    launcher_bytes: bytes
    client_bytes: bytes
    live_evidence: dict[str, object]
    server_auth_sha256: str | None


def verify_offline_contract(
    envelope: dict[str, object],
    snapshot: ProtectedSnapshot,
    consumed_nonce_digests: frozenset[str],
    *,
    peer_uid: int,
    child_uid: int,
    child_environment: dict[str, str],
    sealed_launcher: bytes,
    sealed_client: bytes,
) -> str:
    """Validate a proposed dispatch, returning its nonce digest without consuming it.

    A protected broker must revalidate live evidence, atomically fsync a consumed
    receipt, and dispatch only sealed bytes. This function grants no authority.
    """

    if set(envelope) != {"payload", "signature"} or not isinstance(envelope["payload"], dict):
        raise ValueError("request envelope changed")
    payload = envelope["payload"]
    if set(payload) != REQUIRED_FIELDS:
        raise ValueError("signed request fields changed")
    signature = envelope["signature"]
    if not isinstance(signature, str) or re.fullmatch(r"[0-9a-f]{128}", signature) is None:
        raise ValueError("reviewer signature encoding changed")
    try:
        Ed25519PublicKey.from_public_bytes(snapshot.reviewer_public_key).verify(bytes.fromhex(signature), canonical(payload))
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("reviewer signature invalid") from exc
    nonce = payload["request_nonce"]
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
        raise ValueError("fresh request nonce missing")
    nonce_digest = digest(nonce.encode())
    if nonce_digest in consumed_nonce_digests:
        raise ValueError("one-use request already consumed")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["manifest_sha256"] != MANIFEST_SHA256:
        raise ValueError("ten-page request scope changed")
    if snapshot.broker_uid == APPROVED_RUNTIME_UID or snapshot.runtime_uid != APPROVED_RUNTIME_UID or not snapshot.immutable_ancestors:
        raise ValueError("protected broker installation unavailable")
    if peer_uid != snapshot.broker_uid or child_uid != snapshot.runtime_uid:
        raise ValueError("authenticated broker-to-sender identity changed")
    if child_environment != {"PATH": "/usr/bin", "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"}:
        raise ValueError("child environment inherited untrusted inputs")
    for field, expected in (
        ("runtime_uid", snapshot.runtime_uid),
        ("launcher_sha256", digest(snapshot.launcher_bytes)),
        ("client_sha256", digest(snapshot.client_bytes)),
        ("interpreter_sha256", snapshot.interpreter_sha256),
        ("dependency_tree_sha256", snapshot.dependency_tree_sha256),
        ("broker_install_sha256", snapshot.broker_install_sha256),
        ("server_auth_sha256", snapshot.server_auth_sha256),
    ):
        if payload[field] != expected:
            raise ValueError(f"protected {field} drifted")
    if snapshot.server_auth_sha256 is None:
        raise ValueError("same-UID Bino callers require server-side authorization")
    if sealed_launcher != snapshot.launcher_bytes or sealed_client != snapshot.client_bytes:
        raise ValueError("child did not receive reviewed sealed bytes")
    if set(snapshot.live_evidence) != LIVE_FIELDS:
        raise ValueError("independent live evidence is incomplete")
    for field, actual in snapshot.live_evidence.items():
        if field not in REQUIRED_FIELDS or payload[field] != actual:
            raise ValueError(f"live {field} drifted")
    return nonce_digest
