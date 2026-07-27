from __future__ import annotations

import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager.omo_email_config import configured_agent_mail, human_config_path


class EmailConfigTests(unittest.TestCase):
    def test_loads_complete_split_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text(
                'export OMO_AGENT_GMAIL_ADDRESS="agent@example.test"\n'
                'export OMO_AGENT_GMAIL_APP_PASSWORD="secret"\n'
                'export OMO_HUMAN_EMAIL_ADDRESS="human@example.test"\n',
                encoding="utf-8",
            )
            settings = configured_agent_mail(path)
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual("agent@example.test", settings.agent_address)
        self.assertEqual("human@example.test", settings.human_address)

    def test_empty_inherited_values_do_not_disable_local_split_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text(
                'export OMO_AGENT_GMAIL_ADDRESS="agent@example.test"\n'
                'export OMO_AGENT_GMAIL_APP_PASSWORD="secret"\n'
                'export OMO_HUMAN_EMAIL_ADDRESS="human@example.test"\n',
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "OMO_AGENT_GMAIL_ADDRESS": "",
                    "OMO_AGENT_GMAIL_APP_PASSWORD": "",
                    "OMO_HUMAN_EMAIL_ADDRESS": "",
                },
                clear=False,
            ):
                settings = configured_agent_mail(path)
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual("agent@example.test", settings.agent_address)

    def test_rejects_partial_or_same_address_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text('export OMO_AGENT_GMAIL_ADDRESS="agent@example.test"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incomplete split email configuration"):
                configured_agent_mail(path)
            path.write_text(
                'export OMO_AGENT_GMAIL_ADDRESS="same@example.test"\n'
                'export OMO_AGENT_GMAIL_APP_PASSWORD="secret"\n'
                'export OMO_HUMAN_EMAIL_ADDRESS="same@example.test"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must differ"):
                configured_agent_mail(path)

    def test_rejects_multiple_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text(
                'export OMO_AGENT_GMAIL_ADDRESS="agent@example.test"\n'
                'export OMO_AGENT_GMAIL_APP_PASSWORD="secret"\n'
                'export OMO_HUMAN_EMAIL_ADDRESS="human@example.test, other@example.test"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid human email address"):
                configured_agent_mail(path)

    def test_human_cleanup_config_has_independent_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_env = Path(tmp) / "local.env"
            config = Path(tmp) / "human-himalaya.toml"
            local_env.write_text(f'export OMO_HUMAN_EMAIL_CONFIG_PATH="{config}"\n', encoding="utf-8")
            with patch.dict("os.environ", {"OMO_EMAIL_CONFIG_PATH": "/legacy.toml"}, clear=False):
                self.assertEqual(config, human_config_path(local_env))


if __name__ == "__main__":
    unittest.main()
