from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("email_me.py")
SPEC = importlib.util.spec_from_file_location("email_me", MODULE_PATH)
assert SPEC and SPEC.loader
email_me = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = email_me
SPEC.loader.exec_module(email_me)

SHELL_SENSITIVE_BODY = """literal $HOME
literal $(touch /tmp/email-me-should-not-run)
literal `touch /tmp/email-me-should-not-run-backtick`
> quoted markdown line
"""


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

    def test_markdown_link_gets_html_anchor_and_plain_url(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "See [Story](https://example.com/a?b=1&c=2).")
        plain = msg.get_body(preferencelist=("plain",))
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(plain)
        self.assertIsNotNone(html)
        self.assertIn("Story: https://example.com/a?b=1&c=2", plain.get_content())
        self.assertIn('<a href="https://example.com/a?b=1&amp;c=2">Story</a>', html.get_content())

    def test_parse_args_reads_body_from_stdin_by_default(self) -> None:
        with patch.object(sys, "stdin", StringIO(SHELL_SENSITIVE_BODY)):
            args = email_me.parse_args(["hi"])
        self.assertEqual("hi", args.title)
        self.assertEqual(SHELL_SENSITIVE_BODY, args.content)

    def test_parse_args_rejects_positional_body(self) -> None:
        with patch("sys.stderr", new_callable=StringIO) as stderr, self.assertRaises(SystemExit) as raised:
            email_me.parse_args(["hi", "legacy body"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("not as a shell argument", stderr.getvalue())

    def test_parse_args_reads_message_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(SHELL_SENSITIVE_BODY, encoding="utf-8")
            args = email_me.parse_args(["hi", "--message-file", str(path)])
        self.assertEqual(SHELL_SENSITIVE_BODY, args.content)

    def test_dry_run_does_not_require_smtp_credentials(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")), patch("sys.stdout", new_callable=StringIO) as stdout:
            result = email_me.main(["--dry-run", "hi"])
        self.assertEqual(0, result)
        self.assertIn("dry-run: email not sent", stdout.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
