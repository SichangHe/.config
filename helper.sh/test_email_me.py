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
    def setUp(self) -> None:
        self.env_patch = patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0"})
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()

    def test_appends_pwd_footer_to_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                msg = email_me.build_message("me@example.com", "hi", "body\n")
            finally:
                os.chdir(old_cwd)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(f"body\n\nPWD: {Path(tmp).name}\n", plain.get_content())

    def test_keeps_existing_pwd_footer(self) -> None:
        content = "body\n\nPWD: /already-there\n"
        msg = email_me.build_message("me@example.com", "hi", content)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(content, plain.get_content())

    def test_keeps_existing_quoted_pwd_footer(self) -> None:
        content = "body\n\n> PWD: /already-there\n"
        msg = email_me.build_message("me@example.com", "hi", content)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(content, plain.get_content())

    def test_keeps_existing_pwd_footer_with_spaces(self) -> None:
        content = "body\n\nPWD: /tmp/path with space\n"
        msg = email_me.build_message("me@example.com", "hi", content)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(content, plain.get_content())

    def test_can_omit_pwd_footer_when_explicitly_requested(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "body\n", add_pwd_footer=False)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual("body\n", plain.get_content())

    def test_preserves_manager_subject_prefix(self) -> None:
        for subject in ("[a] hi", "[omo_manager] hi"):
            with self.subTest(subject=subject):
                msg = email_me.build_message("me@example.com", subject, "body")
                self.assertEqual("[a] hi", msg["Subject"])

    def test_uses_short_manager_subject_prefix_by_default(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "body")
        self.assertEqual("[a] hi", msg["Subject"])

    def test_preserves_manager_reply_subject(self) -> None:
        for subject in ("Re: [a] hi", "Re:[a] hi", "Re: [omo_manager] hi", "Re:[omo_manager] hi", "Re:  [omo_manager] hi"):
            with self.subTest(subject=subject):
                msg = email_me.build_message("me@example.com", subject, "body")
                self.assertEqual("Re: [a] hi", msg["Subject"])

    def test_fallback_subject_normalizer_matches_manager_basics(self) -> None:
        with patch.object(email_me, "prepare_subject", None):
            self.assertEqual("[a] hi", email_me.normalize_subject("[omo_manager] hi"))
            self.assertEqual("Re: [a] hi", email_me.normalize_subject("Re: [omo_manager] hi"))
            with self.assertRaisesRegex(ValueError, "placeholder SUBJECT"):
                email_me.normalize_subject("[a] SUBJECT")
            with self.assertRaisesRegex(ValueError, r"\[omo\] is deprecated"):
                email_me.normalize_subject("Re: Re: [omo] direct")

    def test_rejects_non_manager_reply_subject(self) -> None:
        with self.assertRaisesRegex(ValueError, r"\[omo\] is deprecated"):
            email_me.build_message("me@example.com", "Re: [omo] hi", "body")
        with self.assertRaisesRegex(ValueError, r"\[omo\] is deprecated"):
            email_me.build_message("me@example.com", "Re:[omo] hi", "body")
        msg = email_me.build_message("me@example.com", "Re: hi", "body")
        self.assertEqual("Re: [a] hi", msg["Subject"])

    def test_markdown_gets_html_and_plain_url_fallback(self) -> None:
        body = "# Update\n\n- See [Story](https://example.com/a?b=1&c=2).\n- Run `echo $HOME`.\n\n> quoted <raw>\n"
        msg = email_me.build_message("me@example.com", "hi", body)
        plain = msg.get_body(preferencelist=("plain",))
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(plain)
        self.assertIsNotNone(html)
        self.assertIn("Story: https://example.com/a?b=1&c=2", plain.get_content())
        self.assertIn("<ul", html.get_content())
        self.assertIn("<li", html.get_content())
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', html.get_content())
        self.assertIn(">Story</a>", html.get_content())
        self.assertIn("<code", html.get_content())
        self.assertIn("echo $HOME", html.get_content())
        self.assertIn("<blockquote", html.get_content())
        self.assertIn("quoted &lt;raw&gt;", html.get_content())
        self.assertNotIn("<body style=\"font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; line-height: 1.45;\"><pre", html.get_content())

    def test_non_link_markdown_still_gets_html_alternative(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "## Tasks\n\nfirst\nsecond\n")
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        self.assertIn("<h2", html.get_content())
        self.assertIn("first<br> second", html.get_content())

    def test_markdown_html_keeps_intraword_underscores_literal(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "work_manager_2026-06-15.md\nhttps://example.com/foo_bar_baz\nsnake_case_identifier\n\n_emphasis_\n")
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn("work_manager_2026-06-15.md", content)
        self.assertIn("https://example.com/foo_bar_baz", content)
        self.assertIn("snake_case_identifier", content)
        self.assertIn("<em>emphasis</em>", content)
        self.assertNotIn("work<em>", content)
        self.assertNotIn("snake<em>", content)

    def test_heading_keeps_trailing_hash_without_space(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "# C#\n\n# Title #\n")
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn(">C#</h1>", content)
        self.assertIn(">Title</h1>", content)

    def test_list_continuation_stays_inside_list_item(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "- first line\n  continuation with work_manager_foo.md\n- second\n")
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn("first line<br> continuation with work_manager_foo.md</li>", content)
        self.assertEqual(2, content.count("<li"))

    def test_nested_bullets_keep_inline_rendering_without_whole_body_pre(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "- first with [Story](https://example.com/story)\n  - nested with `code`\n    - deeper\n")
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn("<ul", content)
        self.assertIn('href="https://example.com/story"', content)
        self.assertIn("<code", content)
        self.assertIn("nested with <code", content)
        self.assertIn("deeper</li>", content)
        self.assertNotIn("<body style=\"font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; line-height: 1.45;\"><pre", content)

    def test_indented_code_block_is_not_misread_as_list(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "```md\n    - not a list\n```\n\n    - still code-like text\n")
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn("<pre", content)
        self.assertIn("- not a list", content)
        self.assertIn("<p style=\"margin: 0 0 12px 0;\">    - still code-like text</p>", content)
        self.assertNotIn("<li style=\"margin: 0 0 4px 0;\">still code-like text</li>", content)

    def test_parse_args_reads_body_from_stdin_by_default(self) -> None:
        with patch.object(sys, "stdin", StringIO(SHELL_SENSITIVE_BODY)):
            args = email_me.parse_args(["hi"])
        self.assertEqual("hi", args.title)
        self.assertEqual(SHELL_SENSITIVE_BODY, args.content)
        self.assertTrue(args.add_pwd_footer)

    def test_parse_args_can_disable_pwd_footer(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(["--no-pwd-footer", "hi"])
        self.assertFalse(args.add_pwd_footer)

    def test_help_mentions_markdown_but_prefers_plain_text(self) -> None:
        with patch("sys.stdout", new_callable=StringIO) as stdout, self.assertRaises(SystemExit) as raised:
            email_me.parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("body accepts Markdown input", help_text)
        self.assertIn("plain text is preferred", help_text)

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

    def test_parse_args_reads_subject_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subject_path = Path(tmp) / "subject.txt"
            message_path = Path(tmp) / "body.md"
            subject_path.write_text("File subject\n", encoding="utf-8")
            message_path.write_text("body\n", encoding="utf-8")
            args = email_me.parse_args(["--subject-file", str(subject_path), "--message-file", str(message_path)])
        self.assertEqual("File subject", args.title)
        self.assertEqual("body\n", args.content)

    def test_parse_args_rejects_multiline_subject_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subject_path = Path(tmp) / "subject.txt"
            subject_path.write_text("one\n\n", encoding="utf-8")
            with patch("sys.stderr", new_callable=StringIO) as stderr, self.assertRaises(SystemExit) as raised:
                email_me.parse_args(["--subject-file", str(subject_path)])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("exactly one text line", stderr.getvalue())

    def test_dry_run_does_not_require_smtp_credentials(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")), patch("sys.stdout", new_callable=StringIO) as stdout:
            result = email_me.main(["--dry-run", "hi"])
        self.assertEqual(0, result)
        self.assertIn("dry-run: email not sent", stdout.getvalue())

    def test_dry_run_can_omit_pwd_footer(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")), patch("sys.stdout", new_callable=StringIO) as stdout:
            result = email_me.main(["--dry-run", "--no-pwd-footer", "hi"])
        self.assertEqual(0, result)
        self.assertIn("body-bytes=5", stdout.getvalue())

    def test_manager_human_mode_dedupes_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Manager update\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "300",
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            with patch.dict(os.environ, env, clear=False), patch("sys.stdout", new_callable=StringIO) as stdout:
                first = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
                second = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, first)
            self.assertEqual(0, second)
            self.assertIn("Emailed the human", stdout.getvalue())
            self.assertIn("Skipped duplicate human email", stdout.getvalue())
            self.assertEqual("[a] Manager update\nbody\n", send_log.read_text(encoding="utf-8"))
            self.assertIn("[a] Manager update", (state_dir / "human-email-sent.tsv").read_text(encoding="utf-8"))


if __name__ == "__main__":
    _ = unittest.main()
