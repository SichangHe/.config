from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("email_me.py")
SPEC = importlib.util.spec_from_file_location("email_me", MODULE_PATH)
assert SPEC and SPEC.loader
email_me = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = email_me
SPEC.loader.exec_module(email_me)


class EmailMeTests(unittest.TestCase):
    def test_appends_pwd_footer_to_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                msg = email_me.build_message("me@example.com", "hi", "body\n")
            finally:
                os.chdir(old_cwd)
        self.assertEqual(f"body\n\nPWD: {tmp}\n", msg.get_content())

    def test_keeps_existing_pwd_footer(self) -> None:
        content = "body\n\nPWD: /already-there\n"
        msg = email_me.build_message("me@example.com", "hi", content)
        self.assertEqual(content, msg.get_content())

    def test_preserves_manager_subject_prefix(self) -> None:
        msg = email_me.build_message("me@example.com", "[omo_manager] hi", "body")
        self.assertEqual("[omo_manager] hi", msg["Subject"])


if __name__ == "__main__":
    _ = unittest.main()
