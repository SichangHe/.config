import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPER = Path.home() / ".config/omo_manager/opencode_quota_profile_watch.py"


class OpenCodeQuotaProfileWatchTests(unittest.TestCase):
    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_low_quota_signal_initiates_human_gated_plan_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "signal.log"
            _ = signal.write_text("OpenCode error: quota exceeded for current profile\n", encoding="utf-8")
            result = self.run_helper("--check-file", str(signal), "--candidate-name", "chimel")
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertIn("watcher_status: low_quota_detected", result.stdout)
            self.assertIn("safe_automatic_action: notify_and_prepare_human_gated_plan_only", result.stdout)
            self.assertIn("live_credential_mutation: blocked_without_explicit_human_authorization", result.stdout)
            self.assertIn("--smoke-mode plan", result.stdout)

    def test_redacts_secret_shaped_quota_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "signal.log"
            raw = "quota exceeded token=sk-SECRET123456789 Authorization: Bearer abc123456789.def123456789.ghi123456789"
            _ = signal.write_text(raw, encoding="utf-8")
            result = self.run_helper("--check-file", str(signal), "--candidate-name", "chimel")
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertNotIn("sk-SECRET", result.stdout)
            self.assertNotIn("abc123456789.def123456789.ghi123456789", result.stdout)
            self.assertIn("<redacted", result.stdout)

    def test_percent_threshold_detects_low_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "quota.txt"
            _ = signal.write_text("remaining quota: 4%\n", encoding="utf-8")
            result = self.run_helper("--check-file", str(signal), "--low-percent", "5")
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertIn("remaining_percent<=5", result.stdout)


    def test_stall_signal_triggers_without_quota_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "stuck.txt"
            _ = signal.write_text("maybe-stuck: task=x.md target=x:0.0 elapsed_s=4000 hint=history-unavailable\n", encoding="utf-8")
            result = self.run_helper("--check-file", str(signal), "--candidate-name", "chimel")
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertIn("watcher_status: hang_or_stall_detected", result.stdout)
            self.assertIn("quota_report_advisory_only: true", result.stdout)
            self.assertIn("non_openai_trigger_path:", result.stdout)
            self.assertIn("live_credential_mutation: blocked_without_explicit_human_authorization", result.stdout)

    def test_nonzero_health_command_is_stall_trigger(self) -> None:
        result = self.run_helper("--health-command", "printf 'unhealthy: local manager timeout\n' >&2; exit 2")
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("watcher_status: hang_or_stall_detected", result.stdout)
        self.assertIn("health_command", result.stdout)

    def test_missing_stall_command_is_trigger_but_missing_quota_command_is_unknown(self) -> None:
        stall = self.run_helper("--stall-command", "/definitely/missing/omo-stuck-watch")
        self.assertEqual(1, stall.returncode, stall.stderr)
        self.assertIn("watcher_status: hang_or_stall_detected", stall.stdout)
        quota = self.run_helper("--quota-command", "/definitely/missing/quota-command")
        self.assertEqual(0, quota.returncode, quota.stderr)
        self.assertIn("watcher_status: quota_or_stall_unknown", quota.stdout)


    def test_proposes_highest_observed_candidate_without_live_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp) / "auth"
            auth_dir.mkdir(mode=0o700)
            for name in ("auth.low.json", "auth.high.json"):
                path = auth_dir / name
                _ = path.write_text("{}", encoding="utf-8")
                path.chmod(0o600)
            evidence = Path(tmp) / "evidence.txt"
            evidence_text = "\n".join(
                (
                    "candidate=low remaining quota: 12% smoke_ok=true",
                    "candidate=high remaining quota: 88% smoke_ok=true token=sk-SECRET123456789",
                    "",
                )
            )
            _ = evidence.write_text(evidence_text, encoding="utf-8")
            result = self.run_helper(
                "--auth-dir",
                str(auth_dir),
                "--propose-candidate",
                "--candidate-observation-file",
                str(evidence),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("proposal_status: proposed_from_observations", result.stdout)
            self.assertIn("proposed_candidate: high", result.stdout)
            self.assertIn("observed_remaining_percent: 88", result.stdout)
            self.assertIn("live_switch: blocked_without_explicit_human_authorization", result.stdout)
            self.assertNotIn("sk-SECRET", result.stdout)

    def test_candidate_proposal_blocks_without_human_authorized_probe_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp) / "auth"
            auth_dir.mkdir(mode=0o700)
            candidate = auth_dir / "auth.candidate.json"
            _ = candidate.write_text("{}", encoding="utf-8")
            candidate.chmod(0o600)
            result = self.run_helper("--auth-dir", str(auth_dir), "--propose-candidate")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("proposal_status: blocked_no_reliable_candidate_quota_observation", result.stdout)
            self.assertIn("human-authorized isolated Docker quota/smoke probe", result.stdout)
            self.assertIn("--smoke-mode plan", result.stdout)

    def test_no_sources_is_unknown_and_no_switch(self) -> None:
        result = self.run_helper()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("watcher_status: quota_or_stall_unknown", result.stdout)
        self.assertIn("candidate_name: <unset>", result.stdout)
        self.assertIn("real_smoke: blocked_without_human_authorized_smoke", result.stdout)

    def test_report_file_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal = Path(tmp) / "signal.log"
            report = Path(tmp) / "report.txt"
            _ = signal.write_text("usage limit reached\n", encoding="utf-8")
            result = self.run_helper("--check-file", str(signal), "--report-file", str(report))
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertTrue(report.exists())
            self.assertEqual(0o600, report.stat().st_mode & 0o777)
            self.assertIn("watcher_status: low_quota_detected", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    _ = unittest.main()
