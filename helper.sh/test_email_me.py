from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).with_name("email_me.py")
SPEC = importlib.util.spec_from_file_location("email_me", MODULE_PATH)
assert SPEC and SPEC.loader
email_me = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = email_me
SPEC.loader.exec_module(email_me)
omo_email_subject = sys.modules["omo_email_subject"]
omo_guest_images = sys.modules["omo_guest_images"]
omo_email_config = sys.modules["omo_email_config"]

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

    def test_build_message_assigns_rfc_message_id(self) -> None:
        msg = email_me.build_message("me@example.com", "hi", "body\n")
        self.assertRegex(str(msg["Message-ID"]), r"^<[^<>\s]+@example\.com>$")

    def test_build_message_binds_superseded_message_ids(self) -> None:
        msg = email_me.build_message(
            "me@example.com",
            "hi",
            "body\n",
            supersedes_message_ids=("<old-1@example.com>", "<old-2@example.com>"),
        )
        self.assertEqual(["<old-1@example.com>", "<old-2@example.com>"], msg.get_all("X-OMO-Supersedes"))

    def test_build_message_binds_agent_session_identity(self) -> None:
        session_id = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        msg = email_me.build_message("me@example.com", "hi", "body\n", agent_session=session_id)
        self.assertEqual(session_id, msg["X-OMO-Agent-Session-ID"])

    def test_agent_session_prefers_session_over_different_thread_id(self) -> None:
        session_id = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        thread_id = "01a04b9d-7895-70f2-ae4b-5f59d920e99a"
        with patch.dict(os.environ, {"CODEX_SESSION_ID": session_id, "CODEX_THREAD_ID": thread_id}):
            self.assertEqual(session_id, email_me.agent_session_id())

    def test_parse_args_rejects_malformed_superseded_message_id(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")), patch("sys.stderr", new_callable=StringIO), self.assertRaises(SystemExit):
            email_me.parse_args(["--subject", "hi", "--supersedes-message-id", "bad"])

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

    def test_guest_hees_mode_requires_manager_human_and_guest_session(self) -> None:
        for argv in (["--guest-hees", "--subject", "hi"], ["--guest-hees", "--manager-human", "--tmux-target", "other:1", "--subject", "hi"]):
            with self.subTest(argv=argv), patch.object(sys, "stdin", StringIO("body\n")), patch("sys.stderr", new_callable=StringIO), self.assertRaises(SystemExit) as raised:
                email_me.parse_args(argv)
            self.assertEqual(2, raised.exception.code)
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:1", "--subject", "hi"])
        self.assertTrue(args.guest_hees)

    def test_guest_sender_uses_real_helper_imports(self) -> None:
        self.assertIs(email_me.configured_agent_mail, omo_email_config.configured_agent_mail)
        self.assertIs(email_me.prepare_subject_and_headers, omo_email_subject.prepare_subject_and_headers)
        self.assertIs(email_me.reply_attachments, omo_guest_images.reply_attachments)

    def test_guest_image_reference_rejects_non_guest_producer(self) -> None:
        reference = "guest-image:v1:" + "a" * 64
        with (
            patch.dict(os.environ, {"OMO_AGENT_TMUX_TARGET": "other:1"}, clear=False),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--manager-human", "--guest-image-reference", reference, "--subject", "hi"])
        self.assertEqual(2, result)
        self.assertIn("requires a guest_hees producer target", stderr.getvalue())

    def test_guest_manager_target_implies_pinned_guest_recipient(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(["--manager-human", "--tmux-target", "guest_hees:0", "--subject", "hi"])
        self.assertTrue(args.guest_hees)

    def test_guest_dedupe_includes_image_references_and_uses_separate_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": tmp}, clear=False):
            reference_a = "guest-image:v1:" + "a" * 64
            reference_b = "guest-image:v1:" + "b" * 64
            self.assertTrue(email_me.should_send_manager_email_key("topic", "topic", f"body\0{reference_a}", "guest-hees"))
            self.assertTrue(email_me.should_send_manager_email_key("topic", "topic", f"body\0{reference_b}", "guest-hees"))
            email_me.log_manager_email("topic", "guest-hees")
            state = Path(tmp)
            self.assertTrue((state / "guest-hees-email-dedupe.tsv").is_file())
            self.assertTrue((state / "guest-hees-email-sent.tsv").is_file())
            self.assertFalse((state / "human-email-dedupe.tsv").exists())
            self.assertFalse((state / "human-email-sent.tsv").exists())

    def test_guest_hees_reply_is_sent_to_exact_guest_address(self) -> None:
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

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            parent = "<guest-request@example.test>"
            self.assertTrue(omo_email_config.ensure_guest_hees_reply_obligation(state, "guest_hees_manager_mail/request.txt", parent))
            prepared = ("Re: [guest_hees:1] Topic", {"In-Reply-To": parent, "References": parent})
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            with (
                patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False),
                patch.object(sys, "stdin", StringIO("guest reply\n")),
                patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                patch.object(email_me, "verify_guest_reply_in_sent", return_value=evidence),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:1", "--subject", "Re: Topic"])
        self.assertEqual(0, result)
        self.assertEqual("46496337@qq.com", sent_messages[0]["To"])
        self.assertIn("[guest_hees:1]", sent_messages[0]["Subject"])

    def test_inferred_guest_agent_target_sends_to_exact_guest_address(self) -> None:
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

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            parent = "<guest-inferred@example.test>"
            self.assertTrue(omo_email_config.ensure_guest_hees_reply_obligation(state, "guest_hees_manager_mail/inferred.txt", parent))
            prepared = ("Re: [guest_hees:2] Topic", {"In-Reply-To": parent, "References": parent})
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            with (
                patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(state), "OMO_AGENT_TMUX_TARGET": "guest_hees:2"}, clear=False),
                patch.object(sys, "stdin", StringIO("guest reply\n")),
                patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                patch.object(email_me, "verify_guest_reply_in_sent", return_value=evidence),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(["--manager-human", "--subject", "Re: Topic"])
        self.assertEqual(0, result)
        self.assertEqual("46496337@qq.com", sent_messages[0]["To"])
        self.assertIn("[guest_hees:2]", sent_messages[0]["Subject"])

    def test_guest_hees_reply_resolves_selected_images_before_smtp(self) -> None:
        sent_messages = []
        image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00IEND\x00\x00\x00\x00"

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

        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            incoming = email_me.EmailMessage()
            incoming.set_content("request")
            incoming.add_attachment(image_bytes, maintype="image", subtype="png", filename="image.png")
            image_root = Path(tmp) / "images"
            reference = omo_guest_images.store_message_images(
                incoming,
                sender="46496337@qq.com",
                route_target="guest_hees:3",
                authentication=omo_guest_images.AUTHENTICATION,
                source_id="gmail:test:reply",
                root=image_root,
            )[0]
            state = Path(tmp) / "state"
            parent = "<guest-image@example.test>"
            self.assertTrue(omo_email_config.ensure_guest_hees_reply_obligation(state, "guest_hees_manager_mail/image.txt", parent))
            prepared = ("Re: [guest_hees:3] Image", {"In-Reply-To": parent, "References": parent})
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            with (
                patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(state), "OMO_GUEST_IMAGE_ROOT": str(image_root), "OMO_AGENT_TMUX_TARGET": "guest_hees:3"}, clear=False),
                patch.object(sys, "stdin", StringIO("guest reply\n")),
                patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                patch.object(email_me, "verify_guest_reply_in_sent", return_value=evidence),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(["--guest-image-reference", reference, "--manager-human", "--subject", "Re: Image"])
        self.assertEqual(0, result)
        self.assertEqual("46496337@qq.com", sent_messages[0]["To"])
        self.assertIn("[guest_hees:3]", sent_messages[0]["Subject"])
        attachments = list(sent_messages[0].iter_attachments())
        self.assertEqual(1, len(attachments))
        self.assertEqual(image_bytes, attachments[0].get_payload(decode=True))

    def test_guest_reply_rejects_lifecycle_notice_and_missing_thread_before_smtp(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        cases = (
            (
                "Task: guest.md\nOutcome: task done\n",
                ("Re: [guest_hees:7] Topic", {"In-Reply-To": "<request@example.test>", "References": "<request@example.test>"}),
                "substantive",
            ),
            ("This is the requested answer.\n", ("Re: [guest_hees:7] Topic", {}), "thread"),
        )
        for content, prepared, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / "state"
                with (
                    patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False),
                    patch.object(sys, "stdin", StringIO(content)),
                    patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                    patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                    patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
                    patch("sys.stderr", new_callable=StringIO) as stderr,
                ):
                    result = email_me.main(
                        ["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:7", "--subject", "Re: Topic"]
                    )
                self.assertEqual(2, result)
                self.assertIn(error, stderr.getvalue())
                smtp.assert_not_called()

    def test_sent_guest_reply_rejects_same_headers_with_different_body(self) -> None:
        headers = {"In-Reply-To": "<request@example.test>", "References": "<request@example.test>"}
        expected = email_me.build_message(
            "agent@example.test",
            "Re: Topic",
            "Expected substantive answer.\n",
            False,
            "Re: [guest_hees:7] Topic",
            headers,
            manager_human=True,
            recipient_email="46496337@qq.com",
        )
        candidate = email_me.build_message(
            "agent@example.test",
            "Re: Topic",
            "Different substantive answer.\n",
            False,
            "Re: [guest_hees:7] Topic",
            headers,
            manager_human=True,
            recipient_email="46496337@qq.com",
        )
        candidate.replace_header("Message-ID", str(expected["Message-ID"]))
        self.assertFalse(email_me.sent_message_matches_guest_reply(candidate, expected, "agent@example.test"))
        exact = email_me.BytesParser(policy=email_me.policy.default).parsebytes(expected.as_bytes())
        self.assertTrue(email_me.sent_message_matches_guest_reply(exact, expected, "agent@example.test"))

    def test_guest_smtp_uncertainty_requires_sent_mail_evidence(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class UncertainSmtp:
            def __init__(self, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> "UncertainSmtp":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def login(self, _sender: str, _password: str) -> None:
                return None

            def send_message(self, _msg: object) -> None:
                raise email_me.smtplib.SMTPException("connection lost after submit")

        for verified in (False, True):
            with self.subTest(verified=verified), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / "state"
                parent = f"<uncertain-{verified}@example.test>"
                source = f"guest_hees_manager_mail/uncertain-{verified}.txt"
                self.assertTrue(omo_email_config.ensure_guest_hees_reply_obligation(state, source, parent))
                prepared = ("Re: [guest_hees:7] Topic", {"In-Reply-To": parent, "References": parent})
                evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64) if verified else None
                with (
                    patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False),
                    patch.object(sys, "stdin", StringIO("Substantive answer despite uncertain SMTP.\n")),
                    patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                    patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                    patch.object(email_me, "verify_guest_reply_in_sent", return_value=evidence),
                    patch.object(email_me.smtplib, "SMTP_SSL", UncertainSmtp),
                    patch.object(email_me.ssl, "create_default_context", return_value=None),
                    patch.object(email_me, "maybe_print_thread_reminder"),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    result = email_me.main(
                        ["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:7", "--subject", "Re: Topic"]
                    )
                self.assertEqual(0 if verified else 1, result)
                obligation = omo_email_config.read_guest_hees_reply_obligation(state, source)
                assert obligation is not None
                self.assertEqual("fulfilled" if verified else "open", obligation.status)
                self.assertFalse((state / "guest-hees-email-dedupe.tsv").exists())

    def test_guest_uncertain_attempt_retries_same_state_then_fulfills_once(self) -> None:
        smtp_messages: list[object] = []

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class UncertainSmtp:
            def __init__(self, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> "UncertainSmtp":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def login(self, _sender: str, _password: str) -> None:
                return None

            def send_message(self, msg: object) -> None:
                smtp_messages.append(msg)
                raise email_me.smtplib.SMTPException("uncertain after submit")

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            parent = "<same-state-retry@example.test>"
            source = "guest_hees_manager_mail/same-state-retry.txt"
            self.assertTrue(omo_email_config.ensure_guest_hees_reply_obligation(state, source, parent))
            prepared = ("Re: [guest_hees:7] Retry", {"In-Reply-To": parent, "References": parent})
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            env = {"OMO_MANAGER_STATE_DIR": str(state), "OMO_MANAGER_EMAIL_DEDUPE_S": "300"}
            argv = ["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:7", "--subject", "Re: Retry"]

            sent_evidence = iter((None, None, evidence))

            def invoke() -> int:
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch.object(sys, "stdin", StringIO("Substantive answer retried in the same state.\n")),
                    patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                    patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                    patch.object(email_me, "verify_guest_reply_in_sent", side_effect=sent_evidence),
                    patch.object(email_me.smtplib, "SMTP_SSL", UncertainSmtp),
                    patch.object(email_me.ssl, "create_default_context", return_value=None),
                    patch.object(email_me, "maybe_print_thread_reminder"),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    return email_me.main(argv)

            self.assertEqual(1, invoke())
            first = omo_email_config.read_guest_hees_reply_obligation(state, source)
            assert first is not None
            self.assertEqual("open", first.status)
            self.assertEqual(1, len(smtp_messages))

            self.assertEqual(0, invoke())
            second = omo_email_config.read_guest_hees_reply_obligation(state, source)
            assert second is not None
            self.assertEqual("fulfilled", second.status)
            self.assertEqual(2, len(smtp_messages))

            self.assertEqual(2, invoke())
            self.assertEqual(2, len(smtp_messages))

    def test_guest_retry_reconciles_delayed_sent_copy_before_resending(self) -> None:
        smtp_messages: list[object] = []

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class UncertainSmtp:
            def __init__(self, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> "UncertainSmtp":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def login(self, _sender: str, _password: str) -> None:
                return None

            def send_message(self, msg: object) -> None:
                smtp_messages.append(msg)
                raise email_me.smtplib.SMTPException("uncertain after submit")

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            parent = "<delayed-copy@example.test>"
            source = "guest_hees_manager_mail/delayed-copy.txt"
            self.assertTrue(omo_email_config.ensure_guest_hees_reply_obligation(state, source, parent))
            prepared = ("Re: [guest_hees:7] Delayed", {"In-Reply-To": parent, "References": parent})
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            env = {"OMO_MANAGER_STATE_DIR": str(state), "OMO_MANAGER_EMAIL_DEDUPE_S": "300"}
            argv = ["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:7", "--subject", "Re: Delayed"]

            def invoke(sent_evidence: object) -> int:
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch.object(sys, "stdin", StringIO("Substantive delayed-copy answer.\n")),
                    patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                    patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                    patch.object(email_me, "verify_guest_reply_in_sent", return_value=sent_evidence),
                    patch.object(email_me.smtplib, "SMTP_SSL", UncertainSmtp),
                    patch.object(email_me.ssl, "create_default_context", return_value=None),
                    patch.object(email_me, "maybe_print_thread_reminder"),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    return email_me.main(argv)

            self.assertEqual(1, invoke(None))
            self.assertEqual(1, len(smtp_messages))
            self.assertEqual(0, invoke(evidence))
            self.assertEqual(1, len(smtp_messages))
            obligation = omo_email_config.read_guest_hees_reply_obligation(state, source)
            assert obligation is not None
            self.assertEqual("fulfilled", obligation.status)

    def test_guest_fake_send_is_rejected_without_fulfillment(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            parent = "<duplicate@example.test>"
            self.assertTrue(
                omo_email_config.ensure_guest_hees_reply_obligation(
                    state, "guest_hees_manager_mail/duplicate.txt", parent
                )
            )
            prepared = ("Re: [guest_hees:7] Topic", {"In-Reply-To": parent, "References": parent})
            env = {"OMO_MANAGER_STATE_DIR": str(state), "OMO_MANAGER_EMAIL_DEDUPE_S": "300"}
            argv = ["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:7", "--subject", "Re: Topic"]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(sys, "stdin", StringIO("Substantive duplicate answer.\n")),
                patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                patch.object(email_me, "fake_send_log_path", return_value=state / "fake-send.txt"),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(2, email_me.main(argv))
            self.assertIn("cannot verify", stderr.getvalue())
            obligation = omo_email_config.read_guest_hees_reply_obligation(state, "guest_hees_manager_mail/duplicate.txt")
            assert obligation is not None
            self.assertEqual("open", obligation.status)
            self.assertFalse((state / "fake-send.txt").exists())

    def test_guest_obligation_reservation_blocks_different_body_concurrently(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            parent = "<concurrent@example.test>"
            source = "guest_hees_manager_mail/concurrent.txt"
            self.assertTrue(omo_email_config.ensure_guest_hees_reply_obligation(state, source, parent))
            prepared = ("Re: [guest_hees:7] Topic", {"In-Reply-To": parent, "References": parent})
            env = {"OMO_MANAGER_STATE_DIR": str(state), "OMO_MANAGER_EMAIL_DEDUPE_S": "300"}
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(sys, "stdin", StringIO("A completely different substantive answer.\n")),
                patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                claim = email_me.acquire_guest_reply_claim(state, source)
                assert claim is not None
                try:
                    self.assertEqual(
                        1,
                        email_me.main(
                            ["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:7", "--subject", "Re: Topic"]
                        ),
                    )
                finally:
                    claim.close()
            self.assertIn("claim is unavailable", stderr.getvalue())
            smtp.assert_not_called()

    def test_guest_claim_state_faults_fail_closed_and_then_retry(self) -> None:
        smtp_messages: list[object] = []

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
                smtp_messages.append(msg)

        faults = (
            ("directory", lambda: patch.object(email_me.Path, "mkdir", side_effect=OSError("directory unavailable"))),
            ("lock", lambda: patch.object(email_me.fcntl, "flock", side_effect=BlockingIOError("claim busy"))),
            ("write", lambda: patch.object(email_me.os, "write", side_effect=OSError("write failed"))),
        )
        for name, fault in faults:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                smtp_messages.clear()
                state = Path(tmp) / "state"
                source = f"guest_hees_manager_mail/claim-{name}.txt"
                parent = f"<claim-{name}@example.test>"
                self.assertTrue(omo_email_config.ensure_guest_hees_reply_obligation(state, source, parent))
                prepared = ("Re: [guest_hees:7] Claim", {"In-Reply-To": parent, "References": parent})
                env = {"OMO_MANAGER_STATE_DIR": str(state)}
                argv = ["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:7", "--subject", "Re: Claim"]
                with (
                    fault(),
                    patch.dict(os.environ, env, clear=False),
                    patch.object(sys, "stdin", StringIO("Substantive claim-recovery answer.\n")),
                    patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                    patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                    patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    self.assertEqual(1, email_me.main(argv))
                self.assertEqual([], smtp_messages)
                obligation = omo_email_config.read_guest_hees_reply_obligation(state, source)
                assert obligation is not None
                self.assertEqual("open", obligation.status)

                with (
                    patch.dict(os.environ, env, clear=False),
                    patch.object(sys, "stdin", StringIO("Substantive claim-recovery answer.\n")),
                    patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                    patch.object(email_me, "prepare_subject_and_headers", return_value=prepared),
                    patch.object(email_me, "verify_guest_reply_in_sent", return_value=email_me.GuestSentEvidence("a" * 64, "b" * 64)),
                    patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                    patch.object(email_me.ssl, "create_default_context", return_value=None),
                    patch.object(email_me, "maybe_print_thread_reminder"),
                ):
                    self.assertEqual(0, email_me.main(argv))
                self.assertEqual(1, len(smtp_messages))
                fulfilled = omo_email_config.read_guest_hees_reply_obligation(state, source)
                assert fulfilled is not None
                self.assertEqual("fulfilled", fulfilled.status)

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
            self.assertEqual(("wl:1",), prepare.call_args.args)
            self.assertEqual("primary", prepare.call_args.kwargs["route_profile"].route_kind)
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

        def select(predicate: object, subject_query: str) -> object:
            self.assertTrue(callable(predicate))
            self.assertEqual("hcfg:1", subject_query)
            self.assertTrue(predicate(matching))
            self.assertFalse(predicate(sibling))
            return matching

        with patch.object(omo_email_subject, "find_recent_thread_matching", side_effect=select):
            self.assertEqual(matching, omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1"))

    def test_target_thread_lookup_rejects_nonleading_and_competing_tags(self) -> None:
        nonleading = omo_email_subject.RecentHeader("human@example.test", "Re: Topic [hcfg:1]", None)
        competing = omo_email_subject.RecentHeader("human@example.test", "Re: [hcfg:1] [wl:2] Topic", None)
        missing_separator = omo_email_subject.RecentHeader("human@example.test", "Re: [hcfg:1]Topic", None)

        def select(predicate: object, subject_query: str) -> None:
            self.assertTrue(callable(predicate))
            self.assertEqual("hcfg:1", subject_query)
            self.assertFalse(predicate(nonleading))
            self.assertFalse(predicate(competing))
            self.assertFalse(predicate(missing_separator))
            return None

        with patch.object(omo_email_subject, "find_recent_thread_matching", side_effect=select):
            self.assertIsNone(omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1"))

    def test_target_thread_lookup_accepts_alternating_legacy_reply_prefixes(self) -> None:
        matching = omo_email_subject.RecentHeader("human@example.test", "Re: [a] Re: [hcfg:1] Topic", None)

        def select(predicate: object, subject_query: str) -> object:
            self.assertTrue(callable(predicate))
            self.assertEqual("hcfg:1", subject_query)
            self.assertTrue(predicate(matching))
            return matching

        with patch.object(omo_email_subject, "find_recent_thread_matching", side_effect=select):
            self.assertEqual(matching, omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1"))

    def test_route_profiles_keep_guest_and_primary_thread_participants_separate(self) -> None:
        guest = omo_email_subject.MailRouteProfile("agent@example.test", "46496337@qq.com", "guest-hees")
        primary = omo_email_subject.MailRouteProfile("agent@example.test", "human@example.test", "primary")
        primary_sent = omo_email_subject.RecentHeader(
            "Agent <agent@example.test>", "Topic", None, "<primary@example.test>", "", "Human <human@example.test>"
        )
        guest_sent = omo_email_subject.RecentHeader(
            "agent@example.test", "Topic", None, "<guest@example.test>", "", "46496337@qq.com"
        )
        guest_incoming = omo_email_subject.RecentHeader(
            "46496337@qq.com", "Topic", None, "<incoming@example.test>", "", "agent@example.test"
        )
        self.assertTrue(omo_email_subject.route_matches_header(primary_sent, primary.agent_address, primary.counterparty_address))
        self.assertFalse(omo_email_subject.route_matches_header(primary_sent, guest.agent_address, guest.counterparty_address))
        self.assertTrue(omo_email_subject.route_matches_header(guest_sent, guest.agent_address, guest.counterparty_address))
        self.assertFalse(omo_email_subject.route_matches_header(guest_sent, primary.agent_address, primary.counterparty_address))
        self.assertTrue(omo_email_subject.route_matches_header(guest_incoming, guest.counterparty_address, guest.agent_address))

    def test_route_match_rejects_wrong_or_multiple_recipients(self) -> None:
        wrong = omo_email_subject.RecentHeader(
            "agent@example.test", "Topic", None, "<wrong@example.test>", "", "other@example.test"
        )
        multiple = omo_email_subject.RecentHeader(
            "agent@example.test", "Topic", None, "<multiple@example.test>", "", "46496337@qq.com, human@example.test"
        )
        self.assertFalse(omo_email_subject.route_matches_header(wrong, "agent@example.test", "46496337@qq.com"))
        self.assertFalse(omo_email_subject.route_matches_header(multiple, "agent@example.test", "46496337@qq.com"))

    def test_route_profile_lookup_cannot_select_same_subject_from_other_counterparty(self) -> None:
        calls: list[tuple[object, ...]] = []

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class FakeClient:
            def __init__(self, host: str, timeout: float) -> None:
                calls.append(("connect", host, timeout))
                self.mailbox = ""

            def login(self, user: str, password: str) -> None:
                calls.append(("login", user, password))

            def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                calls.append(("select", mailbox, readonly))
                self.mailbox = mailbox
                return "OK", []

            def uid(self, command: str, *args: str) -> tuple[str, list[bytes]]:
                calls.append((command, *args))
                return ("OK", [b"1 2"]) if command == "search" and self.mailbox == '"[Gmail]/Sent Mail"' else ("OK", [b""])

            def logout(self) -> None:
                return None

        now = email_me.datetime.now().astimezone()
        primary_header = omo_email_subject.RecentHeader(
            "agent@example.test", "Topic", now, "<primary@example.test>", "", "human@example.test"
        )
        guest_header = omo_email_subject.RecentHeader(
            "agent@example.test", "Topic", now, "<guest@example.test>", "", "46496337@qq.com"
        )
        guest_profile = omo_email_subject.MailRouteProfile("agent@example.test", "46496337@qq.com", "guest-hees")
        with (
            patch.object(omo_email_subject, "configured_agent_mail", return_value=Settings()),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(omo_email_subject, "fetch_recent_headers", return_value=[primary_header, guest_header]),
            patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False),
        ):
            selected = omo_email_subject.find_recent_thread("topic", guest_profile, reject_ambiguous=True)
        self.assertEqual(guest_header, selected)
        self.assertTrue(any(call[0] == "search" and '"46496337@qq.com"' in call for call in calls))
        self.assertFalse(any(call[0] == "search" and '"human@example.test"' in call for call in calls))
        calls.clear()
        primary_profile = omo_email_subject.MailRouteProfile("agent@example.test", "human@example.test", "primary")
        with (
            patch.object(omo_email_subject, "configured_agent_mail", return_value=Settings()),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(omo_email_subject, "fetch_recent_headers", return_value=[primary_header, guest_header]),
            patch.dict(os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False),
        ):
            selected = omo_email_subject.find_recent_thread("topic", primary_profile, reject_ambiguous=True)
        self.assertEqual(primary_header, selected)
        self.assertTrue(any(call[0] == "search" and '"human@example.test"' in call for call in calls))
        self.assertFalse(any(call[0] == "search" and '"46496337@qq.com"' in call for call in calls))

    def test_verified_thread_selection_rejects_ambiguous_or_missing_identity(self) -> None:
        first = omo_email_subject.RecentHeader(
            "agent@example.test", "Topic", None, "<first@example.test>", "", "human@example.test"
        )
        second = omo_email_subject.RecentHeader(
            "agent@example.test", "Topic", None, "<second@example.test>", "", "human@example.test"
        )
        missing = omo_email_subject.RecentHeader("agent@example.test", "Topic", None, "", "", "human@example.test")
        with self.assertRaisesRegex(omo_email_subject.SubjectInputError, "ambiguous"):
            omo_email_subject.select_recent_thread([first, second], reject_ambiguous=True)
        with self.assertRaisesRegex(omo_email_subject.SubjectInputError, "missing an exact Message-ID"):
            omo_email_subject.select_recent_thread([missing], reject_ambiguous=True)

    def test_verified_reply_rejects_missing_or_failed_route_lookup(self) -> None:
        profile = omo_email_subject.MailRouteProfile("agent@example.test", "human@example.test", "primary")
        with patch.object(omo_email_subject, "find_recent_thread", return_value=None):
            with self.assertRaisesRegex(omo_email_subject.SubjectInputError, "no exact email thread"):
                omo_email_subject.prepare_subject_and_headers("Re: Topic", "wl:1", route_profile=profile)
        with patch.object(omo_email_subject, "find_recent_thread", side_effect=RuntimeError("imap down")):
            with self.assertRaisesRegex(omo_email_subject.SubjectInputError, "lookup failed.*imap down"):
                omo_email_subject.prepare_subject_and_headers("Re: Topic", "wl:1", route_profile=profile)

    def test_failed_verified_reply_does_not_open_smtp(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me, "prepare_subject_and_headers", side_effect=email_me.SubjectInputError("ambiguous route thread")),
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--manager-human", "--tmux-target", "wl:1", "--subject", "Re: Topic"])
        self.assertEqual(2, result)
        self.assertIn("ambiguous route thread", stderr.getvalue())
        smtp.assert_not_called()

    def test_explicit_new_route_message_does_not_attach_a_thread(self) -> None:
        profile = omo_email_subject.MailRouteProfile("agent@example.test", "46496337@qq.com", "guest-hees")
        with patch.object(omo_email_subject, "verified_recent_thread_header") as lookup:
            self.assertEqual(("[guest_hees:1] Topic", {}), omo_email_subject.prepare_subject_and_headers("Topic", "guest_hees:1", route_profile=profile))
        lookup.assert_not_called()

    def test_explicit_guest_target_rejects_verified_primary_producer(self) -> None:
        current = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:7.0\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session", "TMUX_PANE": "%42", "OMO_AGENT_TMUX_TARGET": "wl:7"}, clear=False),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me.subprocess, "run", return_value=current),
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--guest-hees", "--manager-human", "--tmux-target", "guest_hees:1", "--subject", "Topic"])
        self.assertEqual(2, result)
        self.assertIn("conflicts with verified producer route", stderr.getvalue())
        smtp.assert_not_called()

    def test_explicit_primary_target_rejects_verified_guest_producer(self) -> None:
        current = subprocess.CompletedProcess(["tmux"], 0, stdout="guest_hees:1.0\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session", "TMUX_PANE": "%42", "OMO_AGENT_TMUX_TARGET": "guest_hees:1"}, clear=False),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me.subprocess, "run", return_value=current),
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--manager-human", "--tmux-target", "wl:1", "--subject", "Topic"])
        self.assertEqual(2, result)
        self.assertIn("conflicts with verified producer route", stderr.getvalue())
        smtp.assert_not_called()

    def test_primary_route_rejects_pinned_guest_recipient_configuration(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "46496337@qq.com"
            app_password = "secret"

        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me, "configured_agent_mail", return_value=Settings()),
            patch.object(email_me, "prepare_subject_and_headers") as prepare,
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--manager-human", "--tmux-target", "wl:1", "--subject", "Topic"])
        self.assertEqual(2, result)
        self.assertIn("primary email route must not use the pinned guest recipient", stderr.getvalue())
        prepare.assert_not_called()
        smtp.assert_not_called()

    def test_thread_lookup_deadline_covers_both_mailboxes(self) -> None:
        self.assertGreaterEqual(
            omo_email_subject.DEFAULT_THREAD_LOOKUP_DEADLINE_S,
            9 * omo_email_subject.DEFAULT_THREAD_LOOKUP_OPERATION_TIMEOUT_S,
        )

    def test_recent_headers_are_fetched_in_one_batch(self) -> None:
        client = Mock()
        client.uid.return_value = (
            "OK",
            [
                (b"1", b"From: human@example.test\nSubject: [wl:1] First\n\n"),
                b")",
                (b"2", b"From: agent@example.test\nSubject: [wl:1] Second\n\n"),
            ],
        )
        headers = omo_email_subject.fetch_recent_headers(client, ["1", "2"])
        self.assertEqual(["[wl:1] First", "[wl:1] Second"], [header.subject for header in headers])
        client.uid.assert_called_once_with("fetch", "1,2", "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID REFERENCES)])")

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

    def test_manager_dry_run_rejects_missing_split_configuration(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")), patch.object(email_me, "configured_agent_mail", return_value=None), patch.object(email_me, "prepare_subject_and_headers") as prepare, patch.object(email_me, "append_pwd_footer") as append_footer, patch("sys.stderr", new_callable=StringIO) as stderr:
            result = email_me.main(["--dry-run", "--manager-human", "--no-pwd-footer", "--tmux-target", "wl:1", "--subject", "hi"])
        self.assertEqual(2, result)
        self.assertIn("requires split email configuration", stderr.getvalue())
        prepare.assert_not_called()
        append_footer.assert_not_called()

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
            self.assertEqual(("manager_status_email_unification_followup_7872.md status answer", "wl:1"), prepare.call_args.args)
            self.assertEqual("primary", prepare.call_args.kwargs["route_profile"].route_kind)
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
            self.assertEqual(("Topic", "wl:7"), prepare.call_args.args)
            self.assertEqual("primary", prepare.call_args.kwargs["route_profile"].route_kind)
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
            with patch.dict(os.environ, env, clear=False), patch.object(email_me.subprocess, "run", return_value=current) as run, patch.object(email_me, "prepare_subject_and_headers", return_value=("Re: [vl:3] Topic", {})):
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
            with patch.dict(os.environ, env, clear=False), patch.object(email_me.subprocess, "run") as run, patch.object(email_me, "prepare_subject_and_headers", return_value=("Re: [vl:15] Topic", {})):
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

    def test_manager_human_smtp_path_rejects_missing_split_configuration(self) -> None:
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
            with patch.dict(os.environ, env, clear=False), patch.object(email_me, "ENV_FILE_PATH", env_file), patch.object(email_me, "configured_agent_mail", return_value=None), patch.object(email_me, "prepare_subject_and_headers") as prepare, patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp), patch.object(email_me.ssl, "create_default_context", return_value=None), patch("sys.stderr", new_callable=StringIO) as stderr:
                self.assertEqual(2, email_me.main(["--manager-human", "--no-pwd-footer", "--subject-file", str(subject), "--message-file", str(body)]))
        self.assertEqual([], sent_messages)
        prepare.assert_not_called()
        self.assertIn("requires split email configuration", stderr.getvalue())

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

    def test_smtp_uncertain_failure_reports_reusable_message_id(self) -> None:
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

            def send_message(self, _msg: object) -> None:
                raise email_me.smtplib.SMTPException("connection lost")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state")}, clear=False),
            patch.object(email_me, "configured_agent_mail", return_value=Settings()),
            patch.object(email_me, "prepare_subject_and_headers", return_value=("[wl:1] Topic", {})),
            patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
            patch.object(email_me.ssl, "create_default_context", return_value=None),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--manager-human", "--tmux-target", "wl:1", "--subject", "Topic"],)
        self.assertEqual(1, result)
        self.assertRegex(stderr.getvalue(), r"Delivery-uncertain Message-ID: <[^<>\s]+@example\.test>")


if __name__ == "__main__":
    _ = unittest.main()
