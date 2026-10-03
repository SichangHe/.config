"""Offline adversarial checks; no live scorer, socket, DB, or broker dispatch."""

import unittest
from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from omo_manager.cc_ten_broker_contract import (
    MANIFEST_SHA256,
    ProtectedSnapshot,
    canonical,
    digest,
    verify_offline_contract,
)


class BrokerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.payload = {
            "schema_version": 1,
            "request_nonce": "1" * 64,
            "manifest_sha256": MANIFEST_SHA256,
            "launcher_sha256": digest(b"launcher"),
            "client_sha256": digest(b"client"),
            "closure_sha256": "2" * 64,
            "scorer_pid": 1032636,
            "scorer_start_ticks": 100,
            "scorer_cmdline_sha256": "3" * 64,
            "scorer_exe_sha256": "4" * 64,
            "bino_pid": 4150491,
            "bino_start_ticks": 200,
            "bino_cmdline_sha256": "5" * 64,
            "bino_exe_sha256": "6" * 64,
            "socket_device": 10,
            "socket_inode": 20,
            "log_device": 30,
            "log_inode": 40,
            "pane_start_sha256": "7" * 64,
            "output_dir": "/protected/acquisition",
            "runtime_uid": 30033,
            "broker_install_sha256": "8" * 64,
            "interpreter_sha256": "9" * 64,
            "dependency_tree_sha256": "a" * 64,
            "server_auth_sha256": "b" * 64,
        }
        self.snapshot = ProtectedSnapshot(
            self.key.public_key().public_bytes_raw(),
            30034,
            30033,
            True,
            self.payload["interpreter_sha256"],
            self.payload["dependency_tree_sha256"],
            self.payload["broker_install_sha256"],
            b"launcher",
            b"client",
            {
                field: self.payload[field]
                for field in (
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
                )
            },
            self.payload["server_auth_sha256"],
        )
        self.environment = {"PATH": "/usr/bin", "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"}

    def check(self, *, payload=None, snapshot=None, consumed=frozenset(), peer_uid=30034, child_uid=30033, environment=None, launcher=b"launcher", client=b"client"):
        request = self.payload if payload is None else payload
        envelope = {"payload": request, "signature": self.key.sign(canonical(request)).hex()}
        return verify_offline_contract(
            envelope,
            self.snapshot if snapshot is None else snapshot,
            consumed,
            peer_uid=peer_uid,
            child_uid=child_uid,
            child_environment=self.environment if environment is None else environment,
            sealed_launcher=launcher,
            sealed_client=client,
        )

    def test_reviewed_scope_is_offline_and_nonce_is_one_use(self) -> None:
        nonce_digest = self.check()
        self.assertEqual(nonce_digest, digest(("1" * 64).encode()))
        with self.assertRaisesRegex(ValueError, "already consumed"):
            self.check(consumed=frozenset({nonce_digest}))

    def test_mutated_signature_and_exact_manifest_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope changed"):
            self.check(payload={**self.payload, "manifest_sha256": "0" * 64})
        with self.assertRaisesRegex(ValueError, "signature invalid"):
            envelope = {"payload": self.payload, "signature": "0" * 128}
            verify_offline_contract(envelope, self.snapshot, frozenset(), peer_uid=30034, child_uid=30033, child_environment=self.environment, sealed_launcher=b"launcher", sealed_client=b"client")

    def test_signed_boolean_schema_version_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope changed"):
            self.check(payload={**self.payload, "schema_version": True})

    def test_legacy_client_approval_cannot_authorize_broker_child(self) -> None:
        legacy = {
            field: value
            for field, value in self.payload.items()
            if field
            not in {
                "request_nonce",
                "runtime_uid",
                "broker_install_sha256",
                "interpreter_sha256",
                "dependency_tree_sha256",
                "server_auth_sha256",
            }
        }
        legacy["review_verdict"] = "ACCEPT"
        with self.assertRaisesRegex(ValueError, "signed request fields changed"):
            self.check(payload=legacy)

    def test_mutable_ancestry_or_same_uid_verifier_fail(self) -> None:
        for snapshot in (replace(self.snapshot, immutable_ancestors=False), replace(self.snapshot, broker_uid=30033)):
            with self.assertRaisesRegex(ValueError, "protected broker"):
                self.check(snapshot=snapshot)

    def test_untrusted_path_import_peer_or_child_fail(self) -> None:
        for environment in (
            {**self.environment, "PATH": "/tmp"},
            {**self.environment, "PYTHONPATH": "/tmp/attacker"},
        ):
            with self.assertRaisesRegex(ValueError, "environment"):
                self.check(environment=environment)
        for changes in ({"peer_uid": 30033}, {"child_uid": 30034}):
            with self.assertRaisesRegex(ValueError, "identity"):
                self.check(**changes)

    def test_pathname_swap_cannot_change_sealed_bytes(self) -> None:
        for changes in ({"launcher": b"attacker"}, {"client": b"attacker"}):
            with self.assertRaisesRegex(ValueError, "sealed bytes"):
                self.check(**changes)

    def test_live_socket_pid_closure_drift_fails(self) -> None:
        for field in ("socket_inode", "log_inode", "scorer_start_ticks", "bino_start_ticks", "closure_sha256"):
            snapshot = replace(self.snapshot, live_evidence={**self.snapshot.live_evidence, field: "different"})
            with self.assertRaisesRegex(ValueError, "live"):
                self.check(snapshot=snapshot)

    def test_missing_live_evidence_fails(self) -> None:
        for evidence in ({}, {"scorer_pid": 1032636}):
            with self.assertRaisesRegex(ValueError, "evidence is incomplete"):
                self.check(snapshot=replace(self.snapshot, live_evidence=evidence))

    def test_missing_server_policy_digest_fails(self) -> None:
        snapshot = replace(self.snapshot, server_auth_sha256=None)
        payload = {**self.payload, "server_auth_sha256": None}
        with self.assertRaisesRegex(ValueError, "server-side authorization"):
            self.check(payload=payload, snapshot=snapshot)


if __name__ == "__main__":
    unittest.main()
