import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omo_manager import opencode_auth_rotation_dryrun as rotation


HELPER = Path.home() / ".config/omo_manager/opencode_auth_rotation_dryrun.py"


class OpenCodeAuthRotationDryRunTests(unittest.TestCase):
    def run_helper(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), "--auth-dir", str(root), *args],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def write_auth(self, root: Path, name: str, data: object | None = None) -> Path:
        path = root / name
        payload = {"openai": {"type": "oauth", "token": "RAW_SECRET_VALUE"}}
        path.write_text(json.dumps(payload if data is None else data), encoding="utf-8")
        path.chmod(0o600)
        return path

    def write_fake_opencode(self, root: Path) -> Path:
        path = root / "opencode"
        path.write_text("#!/bin/sh\necho fake-opencode\n", encoding="utf-8")
        path.chmod(0o700)
        return path

    def with_auth_dir(self) -> tempfile.TemporaryDirectory[str]:
        tmp = tempfile.TemporaryDirectory()
        Path(tmp.name).chmod(0o700)
        return tmp

    def test_success_is_metadata_ready_not_rotation_ready(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(root, "auth.candidate.json")
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("metadata_ready: true", result.stdout)
            self.assertIn("rotation_ready: false", result.stdout)
            self.assertIn("isolated smoke test and human-present authorization", result.stdout)

    def test_redacts_secret_shaped_keys_and_never_prints_values(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            secret_key_data = {
                "user@example.test": "EMAIL_VALUE",
                "https://example.test/secret": "URL_VALUE",
                "abc123456789.def123456789.ghi123456789": "JWT_VALUE",
                "opaqueKey123456": "OPAQUE_VALUE",
                "token": "TOKEN_VALUE",
                "openai": {"safe": "SAFE_VALUE"},
            }
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(root, "auth.candidate.json", secret_key_data)
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertEqual(0, result.returncode, result.stderr)
            for raw in (
                "user@example.test",
                "https://example.test/secret",
                "abc123456789.def123456789.ghi123456789",
                "opaqueKey123456",
                "EMAIL_VALUE",
                "URL_VALUE",
                "JWT_VALUE",
                "OPAQUE_VALUE",
                "TOKEN_VALUE",
                "SAFE_VALUE",
            ):
                self.assertNotIn(raw, result.stdout)
            self.assertIn("<redacted-key>", result.stdout)

    def test_redacted_key_collisions_are_numbered(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(
                root,
                "auth.candidate.json",
                {"token": "one", "secret": "two", "api" + "_key": "three"},
            )
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("<redacted-key>", result.stdout)
            self.assertIn("<redacted-key>#2", result.stdout)
            self.assertIn("<redacted-key>#3", result.stdout)

    def test_no_op_does_not_mutate_files(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            paths = [
                self.write_auth(root, "auth.json"),
                self.write_auth(root, "auth.current.json"),
                self.write_auth(root, "auth.candidate.json"),
            ]
            before = {path.name: (path.read_bytes(), path.stat().st_mode) for path in paths}
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertEqual(0, result.returncode, result.stderr)
            after = {path.name: (path.read_bytes(), path.stat().st_mode) for path in paths}
            self.assertEqual(before, after)

    def test_ambiguous_matching_fails_closed(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(root, "auth.candidate.json")
            self.write_auth(root, "auth-candidate.json")
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("metadata_ready: false", result.stdout)
            self.assertIn("ambiguous auth name matches", result.stdout)

    def test_invalid_json_fails_closed(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            bad = root / "auth.candidate.json"
            bad.write_text("{not-json", encoding="utf-8")
            bad.chmod(0o600)
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("metadata_ready: false", result.stdout)
            self.assertIn("candidate named auth file JSON unreadable", result.stdout)

    def test_broad_permissions_fail_closed(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            candidate = self.write_auth(root, "auth.candidate.json")
            candidate.chmod(0o644)
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("candidate named auth file permissions are too broad", result.stdout)

    def test_broad_directory_permissions_fail_closed(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(root, "auth.candidate.json")
            root.chmod(0o755)
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("auth directory permissions are too broad", result.stdout)
            root.chmod(0o700)

    def test_symlink_fails_closed(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            target = self.write_auth(root, "real-candidate.json")
            os.symlink(target.name, root / "auth.candidate.json")
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("candidate named auth file must not be a symlink", result.stdout)

    def test_auth_directory_symlink_fails_closed(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            link = root.parent / f"{root.name}-auth-link"
            os.symlink(root, link)
            result = self.run_helper(link, "--current-name", "current", "--candidate-name", "candidate")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("auth directory must not be a symlink", result.stderr)

    def test_rejects_live_auth_json_as_snapshot_and_same_file(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.candidate.json")
            live_current = self.run_helper(root, "--current-name", "auth.json", "--candidate-name", "candidate")
            self.assertNotEqual(0, live_current.returncode)
            self.assertIn("current named auth file must not be live auth.json", live_current.stdout)
            self.write_auth(root, "auth.same.json")
            same = self.run_helper(root, "--current-name", "same", "--candidate-name", "same")
            self.assertNotEqual(0, same.returncode)
            self.assertIn("current and candidate named auth files must differ", same.stdout)

    def test_rejects_hardlinked_live_and_same_file(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            live = self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.candidate.json")
            os.link(live, root / "auth.current.json")
            live_link = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertNotEqual(0, live_link.returncode)
            self.assertIn("current named auth file must not be live auth.json", live_link.stdout)
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            current = self.write_auth(root, "auth.current.json")
            os.link(current, root / "auth.candidate.json")
            same_link = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertNotEqual(0, same_link.returncode)
            self.assertIn("current and candidate named auth files must differ", same_link.stdout)

    def test_docker_smoke_plan_is_noop_and_not_rotation_ready(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            candidate = self.write_auth(root, "auth.candidate.json")
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            before = candidate.read_bytes()
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--smoke-mode",
                "plan",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("docker_smoke_plan:", result.stdout)
            self.assertIn("image_pattern: self-prepared minimal image", result.stdout)
            self.assertIn("rotation_ready: false", result.stdout)
            self.assertNotIn("RAW_SECRET_VALUE", result.stdout)
            self.assertEqual(before, candidate.read_bytes())

    def test_docker_smoke_run_requires_human_authorization(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(root, "auth.candidate.json")
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--smoke-mode",
                "run",
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("--human-authorized-smoke is required", result.stdout)

    def test_docker_smoke_run_injects_candidate_and_cleans_temp_home(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            run_marker = root / "run-marker.txt"
            cleanup_marker = root / "cleanup-marker.txt"
            fake_docker = root / "fake-docker"
            fake_docker.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "if args[:3] == ['rm', '-f', '-v']:\n"
                f"    pathlib.Path({str(cleanup_marker)!r}).write_text(args[3])\n"
                "    raise SystemExit(0)\n"
                "name = args[args.index('--name') + 1]\n"
                "mount = args[args.index('-v') + 1].split(':', 1)[0]\n"
                "auth = pathlib.Path(mount) / '.local/share/opencode/auth.json'\n"
                "data = json.loads(auth.read_text())\n"
                "is_candidate = data.get('fixture_marker') == 'candidate'\n"
                f"pathlib.Path({str(run_marker)!r}).write_text(str(is_candidate) + '\\n' + mount + '\\n' + name + '\\n' + json.dumps(args))\n"
                "print('yes')\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            self.write_auth(root, "auth.json", {"fixture_marker": "live"})
            self.write_auth(root, "auth.current.json", {"fixture_marker": "current"})
            self.write_auth(root, "auth.candidate.json", {"fixture_marker": "candidate"})
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--smoke-mode",
                "run",
                "--human-authorized-smoke",
                "--prepare-smoke-image",
                "none",
                "--opencode-bin",
                str(self.write_fake_opencode(root)),
                "--docker-bin",
                str(fake_docker),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("smoke_matched_expected: true", result.stdout)
            is_candidate, mount, run_name, argv_json = run_marker.read_text(encoding="utf-8").splitlines()
            self.assertEqual("True", is_candidate)
            self.assertFalse(Path(mount).exists())
            self.assertEqual(run_name, cleanup_marker.read_text(encoding="utf-8"))
            docker_args = json.loads(argv_json)
            self.assertIn("--rm", docker_args)
            self.assertIn("--name", docker_args)
            self.assertIn("--cap-drop=ALL", docker_args)
            self.assertIn("--security-opt=no-new-privileges", docker_args)
            self.assertIn("--user", docker_args)
            self.assertIn("--pull=never", docker_args)
            self.assertEqual("sh", docker_args[-3])
            self.assertEqual("-c", docker_args[-2])
            envs = [docker_args[idx + 1] for idx, value in enumerate(docker_args) if value == "-e"]
            self.assertIn("PATH=/workspace/bin:/usr/local/bin:/usr/bin:/bin", envs)
            mounts = [docker_args[idx + 1] for idx, value in enumerate(docker_args) if value == "-v"]
            self.assertTrue(any(mount.endswith(":/workspace/bin/opencode:ro") for mount in mounts))

    def test_docker_smoke_can_prepare_own_image_before_run(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            events = root / "events.jsonl"
            fake_docker = root / "fake-docker"
            fake_docker.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                f"events = pathlib.Path({str(events)!r})\n"
                "with events.open('a') as file:\n"
                "    file.write(json.dumps(args) + '\\n')\n"
                "if args[:2] == ['image', 'inspect']:\n"
                "    raise SystemExit(1)\n"
                "if args and args[0] == 'build':\n"
                "    dockerfile = pathlib.Path(args[args.index('-f') + 1])\n"
                "    text = dockerfile.read_text()\n"
                "    assert 'FROM debian:bookworm-slim' in text\n"
                "    assert '/workspace/bin' in text\n"
                "    raise SystemExit(0)\n"
                "if args[:3] == ['rm', '-f', '-v']:\n"
                "    raise SystemExit(0)\n"
                "mounts = [args[i + 1] for i, value in enumerate(args) if value == '-v']\n"
                "assert any(m.endswith(':/workspace/bin/opencode:ro') for m in mounts)\n"
                "print('yes')\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            self.write_auth(root, "auth.json", {"fixture_marker": "live"})
            self.write_auth(root, "auth.current.json", {"fixture_marker": "current"})
            self.write_auth(root, "auth.candidate.json", {"fixture_marker": "candidate"})
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--smoke-mode",
                "run",
                "--human-authorized-smoke",
                "--opencode-bin",
                str(self.write_fake_opencode(root)),
                "--docker-bin",
                str(fake_docker),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("smoke_image_prepare: built", result.stdout)
            self.assertIn("smoke_matched_expected: true", result.stdout)
            calls = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["image", "inspect"], calls[0][:2])
            self.assertEqual("build", calls[1][0])
            self.assertEqual("run", calls[2][0])

    def test_docker_smoke_detects_and_stores_refreshed_candidate(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            output_dir = root / "refresh-out"
            marker = root / "marker.txt"
            fake_docker = root / "fake-docker"
            fake_docker.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "if args[:3] == ['rm', '-f', '-v']:\n"
                "    raise SystemExit(0)\n"
                "mount = args[args.index('-v') + 1].split(':', 1)[0]\n"
                "auth = pathlib.Path(mount) / '.local/share/opencode/auth.json'\n"
                "data = json.loads(auth.read_text())\n"
                f"pathlib.Path({str(marker)!r}).write_text(data['fixture_marker'])\n"
                "data['fixture_marker'] = 'refreshed-candidate'\n"
                "auth.write_text(json.dumps(data))\n"
                "print('yes')\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            self.write_auth(root, "auth.json", {"fixture_marker": "live"})
            self.write_auth(root, "auth.current.json", {"fixture_marker": "current"})
            self.write_auth(root, "auth.candidate.json", {"fixture_marker": "candidate"})
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--smoke-mode",
                "run",
                "--human-authorized-smoke",
                "--prepare-smoke-image",
                "none",
                "--opencode-bin",
                str(self.write_fake_opencode(root)),
                "--docker-bin",
                str(fake_docker),
                "--refreshed-output-dir",
                str(output_dir),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("candidate", marker.read_text(encoding="utf-8"))
            self.assertIn("refreshed_credential_produced: true", result.stdout)
            refreshed_files = list(output_dir.glob("*.json"))
            self.assertEqual(1, len(refreshed_files))
            refreshed_data = json.loads(refreshed_files[0].read_text(encoding="utf-8"))
            self.assertEqual("refreshed-candidate", refreshed_data["fixture_marker"])
            self.assertNotIn("refreshed-candidate", result.stdout)

    def test_all_candidates_plan_lists_filenames_without_smoke(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(root, "auth.candidate.json")
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--all-candidates",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("all_candidate_smoke_plan:", result.stdout)
            self.assertIn("candidate: auth.candidate.json", result.stdout)
            self.assertIn("real_smoke_status: blocked_without_human_authorized_smoke", result.stdout)

    def test_docker_smoke_timeout_invokes_deterministic_cleanup(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            cleanup_marker = root / "cleanup-marker.txt"
            fake_docker = root / "fake-docker"
            fake_docker.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys, time\n"
                "args = sys.argv[1:]\n"
                "if args[:3] == ['rm', '-f', '-v']:\n"
                f"    pathlib.Path({str(cleanup_marker)!r}).write_text(args[3])\n"
                "    raise SystemExit(0)\n"
                "time.sleep(5)\n",
                encoding="utf-8",
            )
            fake_docker.chmod(0o700)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(root, "auth.candidate.json")
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--smoke-mode",
                "run",
                "--human-authorized-smoke",
                "--prepare-smoke-image",
                "none",
                "--opencode-bin",
                str(self.write_fake_opencode(root)),
                "--docker-bin",
                str(fake_docker),
                "--smoke-timeout-s",
                "1",
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("smoke_error: TimeoutExpired", result.stdout)
            cleanup_name = cleanup_marker.read_text(encoding="utf-8")
            self.assertTrue(cleanup_name.startswith("opencode-auth-smoke-"))

    def test_docker_smoke_run_detects_mismatch_without_printing_raw_output(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            fake_docker = root / "fake-docker"
            fake_docker.write_text("#!/bin/sh\necho no\n", encoding="utf-8")
            fake_docker.chmod(0o700)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            self.write_auth(root, "auth.candidate.json")
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--smoke-mode",
                "run",
                "--human-authorized-smoke",
                "--prepare-smoke-image",
                "none",
                "--opencode-bin",
                str(self.write_fake_opencode(root)),
                "--docker-bin",
                str(fake_docker),
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("smoke_matched_expected: false", result.stdout)
            self.assertIn("smoke_stdout_len:", result.stdout)
            self.assertNotIn("\nno\n", result.stdout)

    def test_rejects_path_names_and_missing_candidate(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            self.write_auth(root, "auth.json")
            self.write_auth(root, "auth.current.json")
            path_name = self.run_helper(root, "--current-name", "current", "--candidate-name", "../candidate")
            self.assertNotEqual(0, path_name.returncode)
            self.assertIn("must be a basename", path_name.stderr)
            missing = self.run_helper(root, "--current-name", "current")
            self.assertNotEqual(0, missing.returncode)
            self.assertIn("candidate name is required", missing.stdout)

    def test_owner_failures_are_reported_for_auth_dir_and_files(self) -> None:
        with self.with_auth_dir() as tmp:
            root = Path(tmp)
            live = self.write_auth(root, "auth.json")
            current = self.write_auth(root, "auth.current.json")
            candidate = self.write_auth(root, "auth.candidate.json")
            other_uid = root.stat().st_uid + 1
            with mock.patch.object(rotation, "getuid", return_value=other_uid):
                errors = rotation.readiness_errors(
                    auth_dir=root,
                    live_path=live,
                    current_match=rotation.NamedMatch(path=current, errors=()),
                    candidate_name="candidate",
                    candidate_match=rotation.NamedMatch(path=candidate, errors=()),
                )
            self.assertIn("auth directory owner is not current user", errors)
            self.assertIn("live auth.json owner is not current user", errors)
            self.assertIn("current named auth file owner is not current user", errors)
            self.assertIn("candidate named auth file owner is not current user", errors)


if __name__ == "__main__":
    unittest.main()
