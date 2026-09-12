import unittest
from pathlib import Path


class ManagerInstructionSourceTests(unittest.TestCase):
    def test_config_root_has_no_authoritative_manager_md(self) -> None:
        config_root = Path(__file__).resolve().parents[2]
        self.assertFalse((config_root / "MANAGER.md").exists())

    def test_quiet_check_uses_named_manager_instructions(self) -> None:
        config_root = Path(__file__).resolve().parents[2]
        text = (config_root / "omo_manager" / "omo_manager_quiet_check.sh").read_text(encoding="utf-8")
        self.assertIn("get agent_manager", text)
        self.assertIn("get main_manager", text)
        self.assertIn("get submanager", text)
        self.assertNotIn("work_logs_root / 'MANAGER.md'", text)

    def test_live_model_switch_doc_is_manual_only(self) -> None:
        config_root = Path(__file__).resolve().parents[2]
        text = (config_root / "omo_manager" / "docs" / "codex" / "live-model-switch.md").read_text(encoding="utf-8")
        self.assertIn("running Codex session", text)
        self.assertIn("interactive UI operation", text)
        self.assertIn("not a helper-script operation", text)
        self.assertIn("same-pane", text)
