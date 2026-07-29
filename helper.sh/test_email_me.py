from __future__ import annotations

import importlib.util
import os
import subprocess
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
omo_email_subject = sys.modules["omo_email_subject"]

SHELL_SENSITIVE_BODY = """literal $HOME
literal $(touch /tmp/email-me-should-not-run)
literal `touch /tmp/email-me-should-not-run-backtick`
> quoted markdown line
"""


class EmailMeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_patch = patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0", "TMUX": "", "TMUX_PANE": "", "OMO_AGENT_TMUX_TARGET": "", "OMO_MANAGER_TMUX_TARGET": ""})
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

    def test_tmux_context_still_uses_pwd_footer(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:2\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session"}, clear=False),
            patch.object(email_me.subprocess, "run", return_value=result) as run,
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(f"body\n\nPWD: {Path.cwd().name}\n", plain.get_content())
        run.assert_called_once_with(
            ["tmux", "display-message", "-p", "#S:#I"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

    def test_explicit_tmux_target_overrides_caller_tmux_footer(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:0\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session"}, clear=False),
            patch.object(email_me.subprocess, "run", return_value=result) as run,
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n", tmux_target="wl:7")
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual("[wl:7] hi", msg["Subject"])
        self.assertEqual(f"body\n\nPWD: {Path.cwd().name}\n", plain.get_content())
        run.assert_not_called()

    def test_zero_pane_tmux_target_uses_window_tag(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "body\n", tmux_target="wl:7.0")
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual("[wl:7] hi", msg["Subject"])
        self.assertEqual(f"body\n\nPWD: {Path.cwd().name}\n", plain.get_content())

    def test_stale_env_tmux_target_does_not_override_exact_caller_pane(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:0.0\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session", "TMUX_PANE": "%42", "OMO_AGENT_TMUX_TARGET": "wl:4"}, clear=False),
            patch.object(email_me.subprocess, "run", return_value=result) as run,
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual("[wl:0] hi", msg["Subject"])
        self.assertEqual(f"body\n\nPWD: {Path.cwd().name}\n", plain.get_content())
        run.assert_called_once_with(
            ["tmux", "display-message", "-p", "-t", "%42", "#S:#I.#P"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )

    def test_matching_env_tmux_target_preserves_exact_nonzero_pane(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:4.1\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session", "TMUX_PANE": "%42", "OMO_AGENT_TMUX_TARGET": "wl:4.1"}, clear=False),
            patch.object(email_me.subprocess, "run", return_value=result),
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        self.assertEqual("[wl:4.1] hi", msg["Subject"])

    def test_stale_sibling_pane_suffix_does_not_override_exact_pane(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:4.2\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session", "TMUX_PANE": "%42", "OMO_AGENT_TMUX_TARGET": "wl:4.1"}, clear=False),
            patch.object(email_me.subprocess, "run", return_value=result),
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        self.assertEqual("[wl:4.2] hi", msg["Subject"])

    def test_env_tmux_target_is_fallback_without_exact_pane_identity(self) -> None:
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session", "TMUX_PANE": "", "OMO_AGENT_TMUX_TARGET": "wl:4"}, clear=False),
            patch.object(email_me.subprocess, "run") as run,
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        self.assertEqual("[wl:4] hi", msg["Subject"])
        run.assert_not_called()

    def test_malformed_env_tmux_target_falls_back_to_caller_tmux(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:2\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session", "OMO_AGENT_TMUX_TARGET": "wl:bad"}, clear=False),
            patch.object(email_me.subprocess, "run", return_value=result) as run,
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(f"body\n\nPWD: {Path.cwd().name}\n", plain.get_content())
        run.assert_called_once()

    def test_tmux_lookup_failure_falls_back_to_pwd_footer(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 1, stdout="", stderr="not attached")
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with (
                    patch.dict(os.environ, {"TMUX": "/tmp/tmux-session"}, clear=False),
                    patch.object(email_me.subprocess, "run", return_value=result),
                ):
                    msg = email_me.build_message("me@example.com", "hi", "body\n")
            finally:
                os.chdir(old_cwd)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(f"body\n\nPWD: {Path(tmp).name}\n", plain.get_content())

    def test_malformed_tmux_target_falls_back_to_pwd_footer(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="notes\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                with (
                    patch.dict(os.environ, {"TMUX": "/tmp/tmux-session"}, clear=False),
                    patch.object(email_me.subprocess, "run", return_value=result),
                ):
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

    def test_existing_tmux_footer_does_not_replace_pwd_footer(self) -> None:
        content = "body\n\ntmux: wl:2\n"
        msg = email_me.build_message("me@example.com", "hi", content)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(f"{content.rstrip()}\n\nPWD: {Path.cwd().name}\n", plain.get_content())

    def test_existing_crlf_tmux_footer_does_not_replace_pwd_footer(self) -> None:
        content = "body\r\n\r\ntmux: wl:2\r\n"
        msg = email_me.build_message("me@example.com", "hi", content)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(f"body\n\ntmux: wl:2\n\nPWD: {Path.cwd().name}\n", plain.get_content())

    def test_quoted_tmux_text_gets_current_footer(self) -> None:
        content = "body\n\n> tmux: wl:2\n"
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                msg = email_me.build_message("me@example.com", "hi", content)
            finally:
                os.chdir(old_cwd)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(f"body\n\n> tmux: wl:2\n\nPWD: {Path(tmp).name}\n", plain.get_content())

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

    def test_can_omit_footer_inside_tmux_when_explicitly_requested(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:2\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session"}, clear=False),
            patch.object(email_me.subprocess, "run", return_value=result) as run,
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n", add_pwd_footer=False)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual("[wl:2] hi", msg["Subject"])
        self.assertEqual("body\n", plain.get_content())
        run.assert_called_once()

    def test_preserves_manager_subject_prefix(self) -> None:
        for subject in ("[a] hi", "[omo_manager] hi"):
            with self.subTest(subject=subject):
                msg = email_me.build_message("me@example.com", subject, "body")
                self.assertEqual("hi", msg["Subject"])

    def test_uses_short_manager_subject_prefix_by_default(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "body")
        self.assertEqual("hi", msg["Subject"])

    def test_preserves_manager_reply_subject(self) -> None:
        for subject in ("Re: [a] hi", "Re:[a] hi", "Re: [omo_manager] hi", "Re:[omo_manager] hi", "Re:  [omo_manager] hi"):
            with self.subTest(subject=subject):
                msg = email_me.build_message("me@example.com", subject, "body")
                self.assertEqual("Re: hi", msg["Subject"])

    def test_manager_reply_subject_adds_thread_headers_when_found(self) -> None:
        with patch.object(email_me, "reply_headers_for_subject", return_value={"In-Reply-To": "<old@example.test>", "References": "<root@example.test> <old@example.test>"}) as headers:
            msg = email_me.build_message("me@example.com", "Re: manager_status_email_unification_followup_7872.md status answer", "body")
        headers.assert_called_once_with("Re: manager_status_email_unification_followup_7872.md status answer")
        self.assertEqual("Re: manager_status_email_unification_followup_7872.md status answer", msg["Subject"])
        self.assertEqual("<old@example.test>", msg["In-Reply-To"])
        self.assertEqual("<root@example.test> <old@example.test>", msg["References"])

    def test_fallback_subject_normalizer_matches_manager_basics(self) -> None:
        with patch.object(email_me, "prepare_subject", None):
            self.assertEqual("hi", email_me.normalize_subject("[omo_manager] hi"))
            self.assertEqual("Re: hi", email_me.normalize_subject("Re: [omo_manager] hi"))
            self.assertEqual("[wl:7] hi", email_me.normalize_subject("[omo_manager] hi", "wl:7"))
            self.assertEqual("Re: [wl:7] hi", email_me.normalize_subject("Re: [omo_manager] hi", "wl:7"))
            with self.assertRaisesRegex(ValueError, "placeholder SUBJECT"):
                email_me.normalize_subject("[a] SUBJECT")
            with self.assertRaisesRegex(ValueError, r"deprecated \[omo\]"):
                email_me.normalize_subject("Re: Re: [omo] direct")

    def test_rejects_non_manager_reply_subject(self) -> None:
        with self.assertRaisesRegex(ValueError, r"deprecated \[omo\]"):
            email_me.build_message("me@example.com", "Re: [omo] hi", "body")
        with self.assertRaisesRegex(ValueError, r"deprecated \[omo\]"):
            email_me.build_message("me@example.com", "Re:[omo] hi", "body")
        msg = email_me.build_message("me@example.com", "Re: hi", "body")
        self.assertEqual("Re: hi", msg["Subject"])

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
            args = email_me.parse_args(["--subject", "hi"])
        self.assertEqual("hi", args.title)
        self.assertEqual(SHELL_SENSITIVE_BODY, args.content)
        self.assertTrue(args.add_pwd_footer)

    def test_parse_args_allows_omitted_subject(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args([])
        self.assertIsNone(args.title)

    def test_parse_args_can_disable_pwd_footer(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(["--no-pwd-footer", "--subject", "hi"])
        self.assertFalse(args.add_pwd_footer)

    def test_parse_args_accepts_explicit_tmux_target(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(["--tmux-target", "wl:7", "--subject", "hi"])
        self.assertEqual("wl:7", args.tmux_target)

    def test_help_says_tmux_target_should_normally_be_omitted(self) -> None:
        with patch("sys.stdout", new_callable=StringIO) as stdout, self.assertRaises(SystemExit) as raised:
            email_me.parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("Normally omit: the helper infers producer identity", help_text)
        self.assertIn("from the exact current pane, then the launch environment", help_text)
        self.assertIn("never pass a task owner or delivery target.", help_text)

    def test_help_restricts_no_pwd_footer_to_explicit_instruction(self) -> None:
        with patch("sys.stdout", new_callable=StringIO) as stdout, self.assertRaises(SystemExit) as raised:
            email_me.parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        self.assertIn("Agents must not use this option unless", stdout.getvalue())
        self.assertIn("explicitly told to.", stdout.getvalue())

    def test_parse_args_accepts_sender_tmux_target_alias(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(["--sender-tmux-target", "wl:7", "--subject", "hi"])
        self.assertEqual("wl:7", args.tmux_target)

    def test_parse_args_rejects_malformed_tmux_target(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")), patch("sys.stderr", new_callable=StringIO) as stderr, self.assertRaises(SystemExit) as raised:
            email_me.parse_args(["--tmux-target", "wl:bad", "--subject", "hi"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("session:window", stderr.getvalue())

    def test_help_mentions_markdown_but_prefers_plain_text(self) -> None:
        with patch("sys.stdout", new_callable=StringIO) as stdout, self.assertRaises(SystemExit) as raised:
            email_me.parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("body accepts Markdown input", help_text)
        self.assertIn("plain text is preferred", help_text)

    def test_parse_args_rejects_positional_arguments(self) -> None:
        with patch("sys.stderr", new_callable=StringIO) as stderr, self.assertRaises(SystemExit) as raised:
            email_me.parse_args(["draft.md"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--subject or --subject-file", stderr.getvalue())
        self.assertIn("--message-file", stderr.getvalue())

    def test_parse_args_reads_message_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(SHELL_SENSITIVE_BODY, encoding="utf-8")
            args = email_me.parse_args(["--subject", "hi", "--message-file", str(path)])
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
            result = email_me.main(["--dry-run", "--subject", "hi"])
        self.assertEqual(0, result)
        self.assertIn("dry-run: email not sent", stdout.getvalue())

    def test_omitted_subject_reuses_latest_thread_for_inferred_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(Path(tmp) / "sent.txt"),
                "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            prepared = ("Re: [wl:1] Existing topic", {"In-Reply-To": "<prior@example.test>"})
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(email_me, "prepare_latest_thread_for_tmux_target", return_value=prepared) as prepare,
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(["--manager-human", "--message-file", str(body)])
            self.assertEqual(0, result)
            prepare.assert_called_once_with("wl:1")
            self.assertEqual("Re: [wl:1] Existing topic\nbody\n", (Path(tmp) / "sent.txt").read_text(encoding="utf-8"))

    def test_omitted_subject_fails_when_no_thread_exists(self) -> None:
        with (
            patch.dict(os.environ, {"OMO_MANAGER_TMUX_TARGET": "wl:1"}, clear=False),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(
                email_me,
                "prepare_latest_thread_for_tmux_target",
                side_effect=email_me.SubjectInputError("no recent email thread found for tmux target wl:1"),
            ),
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--manager-human"])
        self.assertEqual(2, result)
        self.assertIn("no recent email thread", stderr.getvalue())

    def test_omitted_subject_preserves_reply_headers_on_smtp_message(self) -> None:
        sent_messages = []

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class FakeSmtp:
            def __init__(self, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeSmtp":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def login(self, _sender: str, _password: str) -> None:
                return None

            def send_message(self, msg: object) -> None:
                sent_messages.append(msg)

        prepared = (
            "Re: [wl:1] Existing topic",
            {"In-Reply-To": "<prior@example.test>", "References": "<root@example.test> <prior@example.test>"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state")}, clear=False),
                patch.object(sys, "stdin", StringIO("body\n")),
                patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                patch.object(email_me, "prepare_latest_thread_for_tmux_target", return_value=prepared),
                patch.object(email_me, "maybe_print_thread_reminder"),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
            ):
                self.assertEqual(0, email_me.main(["--manager-human", "--tmux-target", "wl:1"]))
        self.assertEqual("Re: [wl:1] Existing topic", sent_messages[0]["Subject"])
        self.assertEqual("<prior@example.test>", sent_messages[0]["In-Reply-To"])
        self.assertEqual("<root@example.test> <prior@example.test>", sent_messages[0]["References"])

    def test_thread_reminder_is_printed_one_in_eight(self) -> None:
        with patch.object(email_me.secrets, "randbelow", return_value=0), patch("sys.stdout", new_callable=StringIO) as stdout:
            email_me.maybe_print_thread_reminder()
        self.assertIn("omit --subject", stdout.getvalue())

    def test_thread_reminder_is_otherwise_quiet(self) -> None:
        with patch.object(email_me.secrets, "randbelow", return_value=1), patch("sys.stdout", new_callable=StringIO) as stdout:
            email_me.maybe_print_thread_reminder()
        self.assertEqual("", stdout.getvalue())

    def test_target_thread_lookup_matches_zero_pane_alias_only(self) -> None:
        matching = omo_email_subject.RecentHeader("human@example.test", "Re: [hcfg:1.0] Topic", None)
        sibling = omo_email_subject.RecentHeader("human@example.test", "Re: [hcfg:1.1] Topic", None)

        def select(predicate: object) -> object:
            self.assertTrue(callable(predicate))
            self.assertTrue(predicate(matching))
            self.assertFalse(predicate(sibling))
            return matching

        with patch.object(omo_email_subject, "find_recent_thread_matching", side_effect=select):
            self.assertEqual(matching, omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1"))

    def test_target_thread_lookup_rejects_nonleading_and_competing_tags(self) -> None:
        nonleading = omo_email_subject.RecentHeader("human@example.test", "Re: Topic [hcfg:1]", None)
        competing = omo_email_subject.RecentHeader("human@example.test", "Re: [hcfg:1] [wl:2] Topic", None)
        missing_separator = omo_email_subject.RecentHeader("human@example.test", "Re: [hcfg:1]Topic", None)

        def select(predicate: object) -> None:
            self.assertTrue(callable(predicate))
            self.assertFalse(predicate(nonleading))
            self.assertFalse(predicate(competing))
            self.assertFalse(predicate(missing_separator))
            return None

        with patch.object(omo_email_subject, "find_recent_thread_matching", side_effect=select):
            self.assertIsNone(omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1"))

    def test_target_thread_lookup_accepts_alternating_legacy_reply_prefixes(self) -> None:
        matching = omo_email_subject.RecentHeader("human@example.test", "Re: [a] Re: [hcfg:1] Topic", None)

        def select(predicate: object) -> object:
            self.assertTrue(callable(predicate))
            self.assertTrue(predicate(matching))
            return matching

        with patch.object(omo_email_subject, "find_recent_thread_matching", side_effect=select):
            self.assertEqual(matching, omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1"))

    def test_dry_run_can_omit_pwd_footer(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")), patch("sys.stdout", new_callable=StringIO) as stdout:
            result = email_me.main(["--dry-run", "--no-pwd-footer", "--subject", "hi"])
        self.assertEqual(0, result)
        self.assertIn("body-bytes=5", stdout.getvalue())

    def test_split_dry_run_omits_pwd_footer_by_default(self) -> None:
        settings = type("Settings", (), {"agent_address": "agent@example.test", "human_address": "human@example.test", "app_password": "secret"})()
        with patch.object(sys, "stdin", StringIO("body\n")), patch.object(email_me, "configured_agent_mail", return_value=settings), patch("sys.stdout", new_callable=StringIO) as stdout:
            result = email_me.main(["--dry-run", "--subject", "hi"])
        self.assertEqual(0, result)
        self.assertIn("body-bytes=5", stdout.getvalue())

    def test_legacy_manager_dry_run_keeps_loop_footer(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")), patch.object(email_me, "configured_agent_mail", return_value=None), patch.object(email_me, "prepare_subject_and_headers", return_value=("[wl:1] hi", {})), patch.object(email_me, "append_pwd_footer", return_value="body\n\nPWD: config\n") as append_footer, patch("sys.stdout", new_callable=StringIO) as stdout:
            result = email_me.main(["--dry-run", "--manager-human", "--no-pwd-footer", "--tmux-target", "wl:1", "--subject", "hi"])
        self.assertEqual(0, result)
        self.assertIn("body-bytes=18", stdout.getvalue())
        append_footer.assert_called_once_with("body\n", tmux_target="wl:1", require_unquoted_footer=True)

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
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            with patch.dict(os.environ, env, clear=False), patch("sys.stdout", new_callable=StringIO) as stdout:
                first = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
                second = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, first)
            self.assertEqual(0, second)
            self.assertIn("Emailed the human", stdout.getvalue())
            self.assertIn("Skipped duplicate human email", stdout.getvalue())
            self.assertEqual("[wl:1] Manager update\nbody\n", send_log.read_text(encoding="utf-8"))
            self.assertIn("[wl:1] Manager update", (state_dir / "human-email-sent.tsv").read_text(encoding="utf-8"))

    def test_manager_human_mode_rejects_missing_tmux_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Manager update\n", encoding="utf-8")
            env = {
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "",
                "OMO_AGENT_TMUX_TARGET": "",
            }
            with patch.dict(os.environ, env, clear=False), patch.dict(os.environ, {"TMUX": ""}, clear=False), patch("sys.stderr", new_callable=StringIO) as stderr:
                self.assertEqual(2, email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)]))
            self.assertIn("requires a tmux target", stderr.getvalue())

    def test_manager_human_mode_reuses_prepared_thread_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("manager_status_email_unification_followup_7872.md status answer\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            prepared = ("Re: [wl:1] manager_status_email_unification_followup_7872.md status answer", {"In-Reply-To": "<prior@example.test>", "References": "<prior@example.test>"})
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "prepare_subject_and_headers", return_value=prepared) as prepare, patch.object(email_me, "reply_headers_for_subject") as headers:
                result = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, result)
            prepare.assert_called_once_with("manager_status_email_unification_followup_7872.md status answer", "wl:1")
            headers.assert_not_called()
            self.assertEqual("Re: [wl:1] manager_status_email_unification_followup_7872.md status answer\nbody\n", send_log.read_text(encoding="utf-8"))
            self.assertIn("Re: [wl:1] manager_status_email_unification_followup_7872.md status answer", (state_dir / "human-email-sent.tsv").read_text(encoding="utf-8"))

    def test_manager_human_mode_passes_tmux_target_to_subject_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Topic\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            prepared = ("[wl:7] Topic", {})
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "prepare_subject_and_headers", return_value=prepared) as prepare:
                result = email_me.main(["--manager-human", "--tmux-target", "wl:7", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, result)
            prepare.assert_called_once_with("Topic", "wl:7")
            self.assertEqual("[wl:7] Topic\nbody\n", send_log.read_text(encoding="utf-8"))

    def test_manager_human_mode_rejects_stale_agent_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Re: wl:9 wl:6 Topic\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
                "TMUX": "/tmp/tmux-session",
                "TMUX_PANE": "%42",
                "OMO_AGENT_TMUX_TARGET": "vl:2",
            }
            current = subprocess.CompletedProcess(["tmux"], 0, stdout="vl:3.0\n", stderr="")
            with patch.dict(os.environ, env, clear=False), patch.object(email_me.subprocess, "run", return_value=current) as run:
                result = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, result)
            self.assertEqual("Re: [vl:3] Topic\nbody\n", send_log.read_text(encoding="utf-8"))
            run.assert_called_once_with(
                ["tmux", "display-message", "-p", "-t", "%42", "#S:#I.#P"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )

    def test_manager_human_mode_repairs_untagged_prepared_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Re: Untagged source subject\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "prepare_subject_and_headers", return_value=("Re: Untagged source subject", {})):
                result = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, result)
            self.assertEqual("Re: [wl:1] Untagged source subject\nbody\n", send_log.read_text(encoding="utf-8"))

    def test_manager_human_mode_rejects_multiple_prepared_tmux_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Re: Topic\n", encoding="utf-8")
            env = {
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "prepare_subject_and_headers", return_value=("Re: [a] [wl:1] [vl:2] Topic", {})), patch("sys.stderr", new_callable=StringIO) as stderr:
                result = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(2, result)
            self.assertIn("exactly one bracketed tmux tag", stderr.getvalue())

    def test_shared_sender_mode_passes_tmux_target_to_subject_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Topic\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            prepared = ("[wl:7] Topic", {})
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "prepare_subject_and_headers", return_value=prepared) as prepare:
                result = email_me.main(["--tmux-target", "wl:7", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, result)
            prepare.assert_called_once_with("Topic", "wl:7")
            self.assertEqual("[wl:7] Topic\nbody\n", send_log.read_text(encoding="utf-8"))

    def test_sender_tmux_target_preserves_source_tag_for_forwarded_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Re: [wl:9] [pb:1] [vl:2] Topic\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "TMUX": "/tmp/tmux-session",
                "OMO_AGENT_TMUX_TARGET": "pb:99",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(email_me.subprocess, "run") as run:
                result = email_me.main(["--manager-human", "--sender-tmux-target", "vl:15", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, result)
            self.assertEqual("Re: [vl:15] Topic\nbody\n", send_log.read_text(encoding="utf-8"))
            run.assert_not_called()

    def test_no_pwd_footer_still_passes_tmux_target_to_subject_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Topic\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            prepared = ("[wl:7] Topic", {})
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "prepare_subject_and_headers", return_value=prepared) as prepare:
                result = email_me.main(["--no-pwd-footer", "--tmux-target", "wl:7", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, result)
            prepare.assert_called_once_with("Topic", "wl:7")
            self.assertEqual("[wl:7] Topic\nbody\n", send_log.read_text(encoding="utf-8"))

    def test_manager_human_dedupe_survives_thread_subject_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Topic\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "300",
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            prepared = [("[wl:1] Topic", {}), ("Re: [wl:1] Topic", {"In-Reply-To": "<prior@example.test>", "References": "<prior@example.test>"})]
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "prepare_subject_and_headers", side_effect=prepared):
                first = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
                second = email_me.main(["--manager-human", "--subject-file", str(subject), "--message-file", str(body)])
            self.assertEqual(0, first)
            self.assertEqual(0, second)
            self.assertEqual("[wl:1] Topic\nbody\n", send_log.read_text(encoding="utf-8"))

    def test_manager_human_smtp_path_uses_prepared_reply_headers(self) -> None:
        sent_messages = []

        class FakeSmtp:
            def __init__(self, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeSmtp":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def login(self, _sender: str, _password: str) -> None:
                return None

            def send_message(self, msg: object) -> None:
                sent_messages.append(msg)

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            env_file = Path(tmp) / ".env"
            env_file.write_text("EMAIL_ME_GMAIL_ADDRESS=me@example.test\nEMAIL_ME_GMAIL_APP_PASSWORD=secret\n", encoding="utf-8")
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Topic\n", encoding="utf-8")
            env = {
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            prepared = ("Re: [wl:1] Topic", {"In-Reply-To": "<prior@example.test>", "References": "<root@example.test> <prior@example.test>"})
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "ENV_FILE_PATH", env_file), patch.object(email_me, "configured_agent_mail", return_value=None), patch.object(email_me, "prepare_subject_and_headers", return_value=prepared), patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp), patch.object(email_me.ssl, "create_default_context", return_value=None):
                self.assertEqual(0, email_me.main(["--manager-human", "--no-pwd-footer", "--subject-file", str(subject), "--message-file", str(body)]))
        self.assertEqual("Re: [wl:1] Topic", sent_messages[0]["Subject"])
        self.assertEqual("<prior@example.test>", sent_messages[0]["In-Reply-To"])
        self.assertEqual("<root@example.test> <prior@example.test>", sent_messages[0]["References"])
        self.assertIn("PWD:", sent_messages[0].get_body(preferencelist=("plain",)).get_content())

    def test_split_smtp_sends_from_agent_only_to_configured_human(self) -> None:
        sent_messages = []

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class FakeSmtp:
            login_args: tuple[str, str] | None = None

            def __init__(self, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> "FakeSmtp":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def login(self, sender: str, password: str) -> None:
                self.login_args = (sender, password)

            def send_message(self, msg: object) -> None:
                sent_messages.append(msg)

        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            with (
                patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state")}, clear=False),
                patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                patch.object(email_me, "prepare_subject_and_headers", return_value=("[wl:1] Topic", {})),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
            ):
                self.assertEqual(0, email_me.main(["--manager-human", "--tmux-target", "wl:1", "--subject", "Topic", "--message-file", str(body)]))
        self.assertEqual("agent@example.test", sent_messages[0]["From"])
        self.assertEqual("human@example.test", sent_messages[0]["To"])
        self.assertNotIn("PWD:", sent_messages[0].get_body(preferencelist=("plain",)).get_content())


if __name__ == "__main__":
    _ = unittest.main()
