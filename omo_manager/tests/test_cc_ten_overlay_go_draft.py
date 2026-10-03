from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from omo_manager.cc_ten_overlay_go_draft import DOMAIN, inspect_private_key_custody, public_pin_draft, unsigned_go_draft


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation": "unaccepted-overlay-only",
        "nonce": "a" * 64,
        "destination": "/offline/ten-scored-unaccepted-overlay",
        "parent": {"path": "/offline", "device": 1, "inode": 2, "uid": 3, "gid": 4, "mode_octal": "2700"},
        "outputs": [
            {"name": "source2128-ten-scored-unaccepted.preview.json", "size_bytes": 12, "sha256": "b" * 64},
            {"name": "source2128-ten-scored-associations.preview.json", "size_bytes": 13, "sha256": "c" * 64},
        ],
        "code": {"commit": "d" * 40, "source_sha256": "e" * 64, "test_sha256": "f" * 64},
        "inputs": {
            "plan_sha256": "1" * 64,
            "manifest_sha256": "2" * 64,
            "contract_sha256": "3" * 64,
            "ledger_sha256": "4" * 64,
            "attestation_sha256": "5" * 64,
            "scorer_verdict_sha256": "6" * 64,
            "ordered_results_sha256": "7" * 64,
        },
        "policy": {
            "scored": True,
            "accepted": False,
            "production_supersession_permitted": False,
            "production_acceptance_permitted": False,
            "loader_writes_permitted": False,
            "database_writes_permitted": False,
            "provider_calls_permitted": False,
            "scorer_calls_permitted": False,
        },
    }


class OfflineOverlayGoDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issued = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)
        self.options = {
            "issued_at": self.issued,
            "expires_at": self.issued + timedelta(minutes=5),
            "reviewed_commit": "d" * 40,
            "reviewed_source_sha256": "e" * 64,
            "reviewed_test_sha256": "f" * 64,
            "reviewed_request_sha256": hashlib.sha256(canonical(request())).hexdigest(),
        }

    def test_public_pin_is_explicitly_not_authenticated(self) -> None:
        draft = json.loads(
            public_pin_draft(
                "1" * 64,
                manager_approval_receipt_sha256="2" * 64,
                reviewed_code_commit="d" * 40,
                reviewed_source_sha256="e" * 64,
                reviewed_test_sha256="f" * 64,
            )
        )
        self.assertEqual(draft["authority"], "draft-only-not-authenticated")
        self.assertEqual(draft["public_key_sha256"], hashlib.sha256(bytes.fromhex("1" * 64)).hexdigest())
        with self.assertRaisesRegex(ValueError, "manager approval"):
            public_pin_draft(
                "1" * 64,
                manager_approval_receipt_sha256="",
                reviewed_code_commit="d" * 40,
                reviewed_source_sha256="e" * 64,
                reviewed_test_sha256="f" * 64,
            )

    def test_unsigned_go_binds_exact_request_without_go_hash_cycle(self) -> None:
        source = canonical(request())
        draft = unsigned_go_draft(source, **self.options)
        self.assertTrue(draft.startswith(DOMAIN))
        self.assertEqual(hashlib.sha256(draft).hexdigest(), "d56ff351e8ffca1fb41db3846ee9093e9f6c0562c683b08e429472a592625f42")
        payload = json.loads(draft[len(DOMAIN) :])
        self.assertEqual(payload["request_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(payload["outputs"], request()["outputs"])
        self.assertNotIn("manager_go_sha256", draft.decode(errors="replace"))
        self.assertEqual(draft, unsigned_go_draft(source, **self.options))

    def test_unreviewed_request_or_unsafe_policy_cannot_be_drafted(self) -> None:
        for changed in (
            {"schema_version": True},
            {"nonce": "0"},
            {"policy": {**request()["policy"], "accepted": True}},
            {"code": {**request()["code"], "source_sha256": "0" * 64}},
            {"code": {"commit": [], "source_sha256": None, "test_sha256": {}}},
            {"inputs": {**request()["inputs"], "ledger_sha256": "0" * 64}},
            {"destination": "/offline/../alternate/ten-scored-unaccepted-overlay"},
            {"destination": "//offline/ten-scored-unaccepted-overlay"},
            {"destination": "/offline/unapproved-overlay"},
            {"outputs": [{**request()["outputs"][0], "size_bytes": True}, request()["outputs"][1]]},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                unsigned_go_draft(canonical({**request(), **changed}), **self.options)
        with self.assertRaisesRegex(ValueError, "canonical"):
            unsigned_go_draft(canonical(request()) + b"\n", **self.options)

    def test_window_is_short_and_utc(self) -> None:
        for expires_at in (self.issued, self.issued + timedelta(minutes=31)):
            with self.assertRaisesRegex(ValueError, "UTC"):
                unsigned_go_draft(canonical(request()), **{**self.options, "expires_at": expires_at})
        with self.assertRaisesRegex(ValueError, "UTC"):
            unsigned_go_draft(canonical(request()), **{**self.options, "issued_at": self.issued.replace(tzinfo=None)})

    def test_proposed_private_key_custody_is_read_only_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "manager-only"
            directory.mkdir(mode=0o700)
            key_path = directory / "offline-fixture.key"
            key_path.write_bytes(b"not-a-private-key; custody fixture only")
            key_path.chmod(0o600)
            inspect_private_key_custody(directory, key_path.name, expected_uid=os.getuid())
            key_path.chmod(0o640)
            with self.assertRaisesRegex(ValueError, "0600"):
                inspect_private_key_custody(directory, key_path.name, expected_uid=os.getuid())
            key_path.chmod(0o600)
            key_path.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "nonempty"):
                inspect_private_key_custody(directory, key_path.name, expected_uid=os.getuid())
            key_path.write_bytes(b"not-a-private-key; custody fixture only")
            with self.assertRaisesRegex(ValueError, "owned"):
                inspect_private_key_custody(directory, key_path.name, expected_uid=os.getuid() + 1)
            directory.chmod(0o770)
            with self.assertRaisesRegex(ValueError, "0700"):
                inspect_private_key_custody(directory, key_path.name, expected_uid=os.getuid())
            directory.chmod(0o700)
            key_path.unlink()
            key_path.symlink_to(root / "missing")
            with self.assertRaises(OSError):
                inspect_private_key_custody(directory, key_path.name, expected_uid=os.getuid())
            key_path.unlink()
            os.mkfifo(key_path, mode=0o600)
            with self.assertRaisesRegex(ValueError, "0600"):
                inspect_private_key_custody(directory, key_path.name, expected_uid=os.getuid())


if __name__ == "__main__":
    unittest.main()
