import unittest
from pathlib import Path


class ManagerInstructionSourceTests(unittest.TestCase):
    def test_config_root_has_no_authoritative_manager_md(self) -> None:
        config_root = Path(__file__).resolve().parents[2]
        self.assertFalse((config_root / "MANAGER.md").exists())

    def test_quiet_check_uses_work_log_manager_md(self) -> None:
        config_root = Path(__file__).resolve().parents[2]
        text = (config_root / "omo_manager" / "omo_manager_quiet_check.sh").read_text(encoding="utf-8")
        self.assertIn("OMO_WORK_LOGS_ROOT", text)
        self.assertIn("work_logs_root / 'MANAGER.md'", text)
        self.assertIn("remove config-root manager instructions", text)

    def test_live_model_switch_doc_is_manual_only(self) -> None:
        config_root = Path(__file__).resolve().parents[2]
        text = (config_root / "omo_manager" / "docs" / "codex" / "live-model-switch.md").read_text(encoding="utf-8")
        self.assertIn("live Codex session", text)
        self.assertIn("interactive model picker", text)
        self.assertIn("No helper script is shipped for this path", text)
        self.assertIn("prefer a fresh launch over live mutation", text)
