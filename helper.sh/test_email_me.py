from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
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
        self.state_tmp = tempfile.TemporaryDirectory()
        self.env_patch = patch.dict(
            os.environ,
            {
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_STATE_DIR": self.state_tmp.name,
                "TMUX": "",
                "TMUX_PANE": "",
                "OMO_AGENT_TMUX_TARGET": "",
                "OMO_MANAGER_TMUX_TARGET": "",
            },
        )
        self.env_patch.start()
        self.non_completion_caller_patch = patch.object(
            email_me, "validate_non_completion_owner", return_value=None
        )
        self.non_completion_caller_patch.start()
        self.completion_caller_patch = patch.object(
            email_me, "validate_completion_owner", return_value=None
        )
        self.completion_caller_patch.start()

    def tearDown(self) -> None:
        self.completion_caller_patch.stop()
        self.non_completion_caller_patch.stop()
        self.env_patch.stop()
        self.state_tmp.cleanup()

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
            msg = email_me.build_message(
                "me@example.com", "hi", "body\n", tmux_target="wl:7"
            )
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual("[wl:7] hi", msg["Subject"])
        self.assertEqual(f"body\n\nPWD: {Path.cwd().name}\n", plain.get_content())
        run.assert_not_called()

    def test_zero_pane_tmux_target_uses_window_tag(self) -> None:
        msg = email_me.build_message(
            "me@example.com", "hi", "body\n", tmux_target="wl:7.0"
        )
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual("[wl:7] hi", msg["Subject"])
        self.assertEqual(f"body\n\nPWD: {Path.cwd().name}\n", plain.get_content())

    def test_stale_env_tmux_target_does_not_override_exact_caller_pane(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:0.0\n", stderr="")
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX": "/tmp/tmux-session",
                    "TMUX_PANE": "%42",
                    "OMO_AGENT_TMUX_TARGET": "wl:4",
                },
                clear=False,
            ),
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
            patch.dict(
                os.environ,
                {
                    "TMUX": "/tmp/tmux-session",
                    "TMUX_PANE": "%42",
                    "OMO_AGENT_TMUX_TARGET": "wl:4.1",
                },
                clear=False,
            ),
            patch.object(email_me.subprocess, "run", return_value=result),
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        self.assertEqual("[wl:4.1] hi", msg["Subject"])

    def test_stale_sibling_pane_suffix_does_not_override_exact_pane(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:4.2\n", stderr="")
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX": "/tmp/tmux-session",
                    "TMUX_PANE": "%42",
                    "OMO_AGENT_TMUX_TARGET": "wl:4.1",
                },
                clear=False,
            ),
            patch.object(email_me.subprocess, "run", return_value=result),
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        self.assertEqual("[wl:4.2] hi", msg["Subject"])

    def test_env_tmux_target_is_fallback_without_exact_pane_identity(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX": "/tmp/tmux-session",
                    "TMUX_PANE": "",
                    "OMO_AGENT_TMUX_TARGET": "wl:4",
                },
                clear=False,
            ),
            patch.object(email_me.subprocess, "run") as run,
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        self.assertEqual("[wl:4] hi", msg["Subject"])
        run.assert_not_called()

    def test_malformed_env_tmux_target_falls_back_to_caller_tmux(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:2\n", stderr="")
        with (
            patch.dict(
                os.environ,
                {"TMUX": "/tmp/tmux-session", "OMO_AGENT_TMUX_TARGET": "wl:bad"},
                clear=False,
            ),
            patch.object(email_me.subprocess, "run", return_value=result) as run,
        ):
            msg = email_me.build_message("me@example.com", "hi", "body\n")
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(f"body\n\nPWD: {Path.cwd().name}\n", plain.get_content())
        run.assert_called_once()

    def test_tmux_lookup_failure_falls_back_to_pwd_footer(self) -> None:
        result = subprocess.CompletedProcess(
            ["tmux"], 1, stdout="", stderr="not attached"
        )
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
        self.assertEqual(
            f"{content.rstrip()}\n\nPWD: {Path.cwd().name}\n", plain.get_content()
        )

    def test_existing_crlf_tmux_footer_does_not_replace_pwd_footer(self) -> None:
        content = "body\r\n\r\ntmux: wl:2\r\n"
        msg = email_me.build_message("me@example.com", "hi", content)
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual(
            f"body\n\ntmux: wl:2\n\nPWD: {Path.cwd().name}\n", plain.get_content()
        )

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
        self.assertEqual(
            f"body\n\n> tmux: wl:2\n\nPWD: {Path(tmp).name}\n", plain.get_content()
        )

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
        msg = email_me.build_message(
            "me@example.com", "hi", "body\n", add_pwd_footer=False
        )
        plain = msg.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertEqual("body\n", plain.get_content())

    def test_can_omit_footer_inside_tmux_when_explicitly_requested(self) -> None:
        result = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:2\n", stderr="")
        with (
            patch.dict(os.environ, {"TMUX": "/tmp/tmux-session"}, clear=False),
            patch.object(email_me.subprocess, "run", return_value=result) as run,
        ):
            msg = email_me.build_message(
                "me@example.com", "hi", "body\n", add_pwd_footer=False
            )
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
        for subject in (
            "Re: [a] hi",
            "Re:[a] hi",
            "Re: [omo_manager] hi",
            "Re:[omo_manager] hi",
            "Re:  [omo_manager] hi",
        ):
            with self.subTest(subject=subject):
                msg = email_me.build_message("me@example.com", subject, "body")
                self.assertEqual("Re: hi", msg["Subject"])

    def test_manager_reply_subject_adds_thread_headers_when_found(self) -> None:
        with patch.object(
            email_me,
            "reply_headers_for_subject",
            return_value={
                "In-Reply-To": "<old@example.test>",
                "References": "<root@example.test> <old@example.test>",
            },
        ) as headers:
            msg = email_me.build_message(
                "me@example.com",
                "Re: manager_status_email_unification_followup_7872.md status answer",
                "body",
            )
        headers.assert_called_once_with(
            "Re: manager_status_email_unification_followup_7872.md status answer"
        )
        self.assertEqual(
            "Re: manager_status_email_unification_followup_7872.md status answer",
            msg["Subject"],
        )
        self.assertEqual("<old@example.test>", msg["In-Reply-To"])
        self.assertEqual("<root@example.test> <old@example.test>", msg["References"])

    def test_fallback_subject_normalizer_matches_manager_basics(self) -> None:
        with patch.object(email_me, "prepare_subject", None):
            self.assertEqual("hi", email_me.normalize_subject("[omo_manager] hi"))
            self.assertEqual(
                "Re: hi", email_me.normalize_subject("Re: [omo_manager] hi")
            )
            self.assertEqual(
                "[wl:7] hi", email_me.normalize_subject("[omo_manager] hi", "wl:7")
            )
            self.assertEqual(
                "Re: [wl:7] hi",
                email_me.normalize_subject("Re: [omo_manager] hi", "wl:7"),
            )
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
        self.assertNotIn(
            '<body style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; line-height: 1.45;"><pre',
            html.get_content(),
        )

    def test_non_link_markdown_still_gets_html_alternative(self) -> None:
        msg = email_me.build_message(
            "me@example.com", "hi", "## Tasks\n\nfirst\nsecond\n"
        )
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        self.assertIn("<h2", html.get_content())
        self.assertIn("first<br> second", html.get_content())

    def test_markdown_html_keeps_intraword_underscores_literal(self) -> None:
        msg = email_me.build_message(
            "me@example.com",
            "hi",
            "work_manager_2026-06-15.md\nhttps://example.com/foo_bar_baz\nsnake_case_identifier\n\n_emphasis_\n",
        )
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
        msg = email_me.build_message(
            "me@example.com",
            "hi",
            "- first line\n  continuation with work_manager_foo.md\n- second\n",
        )
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn(
            "first line<br> continuation with work_manager_foo.md</li>", content
        )
        self.assertEqual(2, content.count("<li"))

    def test_nested_bullets_keep_inline_rendering_without_whole_body_pre(self) -> None:
        msg = email_me.build_message(
            "me@example.com",
            "hi",
            "- first with [Story](https://example.com/story)\n  - nested with `code`\n    - deeper\n",
        )
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn("<ul", content)
        self.assertIn('href="https://example.com/story"', content)
        self.assertIn("<code", content)
        self.assertIn("nested with <code", content)
        self.assertIn("deeper</li>", content)
        self.assertIn("Story</a>\n<ul", content)
        self.assertIn("nested with <code", content)
        self.assertIn("</code>\n<ul", content)
        self.assertEqual(3, content.count("<ul"))
        self.assertNotIn(
            '<body style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; line-height: 1.45;"><pre',
            content,
        )

    def test_nested_mixed_lists_preserve_kind_transitions(self) -> None:
        msg = email_me.build_message(
            "me@example.com",
            "hi",
            "- parent\n  1. ordered child\n  2. second child\n    - bullet grandchild\n- sibling\n",
        )
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn("parent\n<ol", content)
        self.assertIn("second child\n<ul", content)
        self.assertIn("bullet grandchild</li>", content)
        self.assertIn("</ol></li>\n<li", content)

    def test_indented_code_block_is_not_misread_as_list(self) -> None:
        msg = email_me.build_message(
            "me@example.com",
            "hi",
            "```md\n    - not a list\n```\n\n    - still code-like text\n",
        )
        html = msg.get_body(preferencelist=("html",))
        self.assertIsNotNone(html)
        content = html.get_content()
        self.assertIn("<pre", content)
        self.assertIn("- not a list", content)
        self.assertIn(
            '<p style="margin: 0 0 12px 0;">    - still code-like text</p>', content
        )
        self.assertNotIn(
            '<li style="margin: 0 0 4px 0;">still code-like text</li>', content
        )

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
        self.assertEqual(
            ["<old-1@example.com>", "<old-2@example.com>"],
            msg.get_all("X-OMO-Supersedes"),
        )

    def test_build_message_binds_agent_session_identity(self) -> None:
        session_id = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        msg = email_me.build_message(
            "me@example.com", "hi", "body\n", agent_session=session_id
        )
        self.assertEqual(session_id, msg["X-OMO-Agent-Session-ID"])

    def test_agent_session_prefers_session_over_different_thread_id(self) -> None:
        session_id = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        thread_id = "01a04b9d-7895-70f2-ae4b-5f59d920e99a"
        with patch.dict(
            os.environ, {"CODEX_SESSION_ID": session_id, "CODEX_THREAD_ID": thread_id}
        ):
            self.assertEqual(session_id, email_me.agent_session_id())

    def test_agent_session_uses_cursor_conversation_id(self) -> None:
        session_id = "b44fe714-f011-4c2b-b449-0a5a0d13b0ec"
        with patch.dict(
            os.environ,
            {
                "CODEX_SESSION_ID": "",
                "CODEX_THREAD_ID": "",
                "CURSOR_CONVERSATION_ID": session_id,
            },
        ):
            self.assertEqual(session_id, email_me.agent_session_id())

    def test_parse_args_rejects_malformed_superseded_message_id(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch("sys.stderr", new_callable=StringIO),
            self.assertRaises(SystemExit),
        ):
            email_me.parse_args(["--subject", "hi", "--supersedes-message-id", "bad"])

    def test_parse_args_allows_omitted_subject(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args([])
        self.assertIsNone(args.title)

    def test_parse_args_rejects_explicit_empty_subject(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("Closed wl:1\n")),
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            self.assertRaises(SystemExit),
        ):
            email_me.main(
                [
                    "--manager-human",
                    "--completion-authorization",
                    "a" * 64,
                    "--subject",
                    "",
                ]
            )
        smtp.assert_not_called()

    def test_parse_args_can_disable_pwd_footer(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(["--no-pwd-footer", "--subject", "hi"])
        self.assertFalse(args.add_pwd_footer)

    def test_parse_args_accepts_explicit_tmux_target(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(["--tmux-target", "wl:7", "--subject", "hi"])
        self.assertEqual("wl:7", args.tmux_target)

    def test_parse_args_accepts_omnigent_target(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(
                ["--tmux-target", "omnigent://session-1", "--subject", "hi"]
            )
        self.assertEqual("omnigent://session-1", args.tmux_target)

    def test_inferred_target_uses_omnigent_identity_without_tmux(self) -> None:
        identity = SimpleNamespace(target="omnigent://session-1")
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX": "",
                    "TMUX_PANE": "",
                    "OMO_AGENT_TMUX_TARGET": "",
                    "OMO_MANAGER_TMUX_TARGET": "",
                },
            ),
            patch(
                "omo_omnigent_identity.authenticate_current_omnigent",
                return_value=identity,
            ),
        ):
            self.assertEqual(
                "omnigent://session-1", email_me.inferred_tmux_target(False)
            )

    def test_inferred_target_prefers_authenticated_omnigent_over_inherited_tmux(self) -> None:
        identity = SimpleNamespace(target="omnigent://session-1")
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX": "/tmp/tmux-session",
                    "TMUX_PANE": "%42",
                    "OMO_AGENT_TMUX_TARGET": "main:0",
                },
            ),
            patch(
                "omo_omnigent_identity.authenticate_current_omnigent",
                return_value=identity,
            ),
            patch.object(email_me, "current_tmux_window") as current_tmux,
        ):
            self.assertEqual(
                "omnigent://session-1", email_me.inferred_tmux_target(False)
            )
        current_tmux.assert_not_called()

    def test_inferred_target_rejects_broken_omnigent_identity_before_tmux_fallback(self) -> None:
        identity_error = __import__("omo_omnigent_identity").OmniGentIdentityError
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX_PANE": "",
                    "OMO_AGENT_TMUX_TARGET": "main:0",
                },
            ),
            patch(
                "omo_omnigent_identity.authenticate_current_omnigent",
                side_effect=identity_error("broken identity"),
            ),
            self.assertRaisesRegex(ValueError, "could not be authenticated"),
        ):
            email_me.inferred_tmux_target(False)

    def test_inferred_target_rejects_invalid_omnigent_target_before_inherited_fallback(self) -> None:
        identity = SimpleNamespace(target="")
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX_PANE": "",
                    "OMO_AGENT_TMUX_TARGET": "main:0",
                },
            ),
            patch(
                "omo_omnigent_identity.authenticate_current_omnigent",
                return_value=identity,
            ),
            self.assertRaisesRegex(ValueError, "could not be authenticated"),
        ):
            email_me.inferred_tmux_target(False)

    def test_inferred_target_rejects_tmux_shaped_omnigent_target_before_inherited_fallback(self) -> None:
        identity = SimpleNamespace(target="main:7")
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX_PANE": "",
                    "OMO_AGENT_TMUX_TARGET": "main:0",
                },
            ),
            patch(
                "omo_omnigent_identity.authenticate_current_omnigent",
                return_value=identity,
            ),
            self.assertRaisesRegex(ValueError, "could not be authenticated"),
        ):
            email_me.inferred_tmux_target(False)

    def test_manager_human_fake_send_uses_authenticated_omnigent_subject_tag(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        identity = SimpleNamespace(target="omnigent://session-1")
        with tempfile.TemporaryDirectory() as tmp:
            sent = Path(tmp) / "sent.txt"
            with (
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_ME_FAKE_SEND_LOG": str(sent),
                        "TMUX_PANE": "%42",
                        "OMO_AGENT_TMUX_TARGET": "main:0",
                    },
                ),
                patch.object(sys, "stdin", StringIO("figure update\n")),
                patch.object(email_me, "configured_agent_mail", return_value=Settings()),
                patch(
                    "omo_omnigent_identity.authenticate_current_omnigent",
                    return_value=identity,
                ),
            ):
                self.assertEqual(
                    0,
                    email_me.main(
                        [
                            "--manager-human",
                            "--non-completion",
                            "--subject",
                            "Figure update",
                        ]
                    ),
                )
            self.assertTrue(
                sent.read_text(encoding="utf-8").startswith(
                    "[omnigent://session-1] Figure update\n"
                )
            )

    def test_help_says_tmux_target_should_normally_be_omitted(self) -> None:
        with (
            patch("sys.stdout", new_callable=StringIO) as stdout,
            self.assertRaises(SystemExit) as raised,
        ):
            email_me.parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("Normally omit: the helper infers producer identity", help_text)
        self.assertIn(
            "from an authenticated OmniGent runtime, then the exact current pane and launch environment",
            help_text,
        )
        self.assertIn("never pass a task owner or delivery target.", help_text)

    def test_help_restricts_no_pwd_footer_to_explicit_instruction(self) -> None:
        with (
            patch("sys.stdout", new_callable=StringIO) as stdout,
            self.assertRaises(SystemExit) as raised,
        ):
            email_me.parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        self.assertIn("Agents must not use this option unless", stdout.getvalue())
        self.assertIn("explicitly told to.", stdout.getvalue())

    def test_parse_args_accepts_sender_tmux_target_alias(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(
                ["--sender-tmux-target", "wl:7", "--subject", "hi"]
            )
        self.assertEqual("wl:7", args.tmux_target)

    def test_parse_args_rejects_malformed_tmux_target(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch("sys.stderr", new_callable=StringIO) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            email_me.parse_args(["--tmux-target", "wl:bad", "--subject", "hi"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("session:window", stderr.getvalue())

    def test_help_mentions_markdown_but_prefers_plain_text(self) -> None:
        with (
            patch("sys.stdout", new_callable=StringIO) as stdout,
            self.assertRaises(SystemExit) as raised,
        ):
            email_me.parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("body accepts Markdown input", help_text)
        self.assertIn("plain text is preferred", help_text)

    def test_parse_args_rejects_positional_arguments(self) -> None:
        with (
            patch("sys.stderr", new_callable=StringIO) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
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
            args = email_me.parse_args(
                [
                    "--subject-file",
                    str(subject_path),
                    "--message-file",
                    str(message_path),
                ]
            )
        self.assertEqual("File subject", args.title)
        self.assertEqual("body\n", args.content)

    def test_parse_args_rejects_multiline_subject_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            subject_path = Path(tmp) / "subject.txt"
            subject_path.write_text("one\n\n", encoding="utf-8")
            with (
                patch("sys.stderr", new_callable=StringIO) as stderr,
                self.assertRaises(SystemExit) as raised,
            ):
                email_me.parse_args(["--subject-file", str(subject_path)])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("exactly one text line", stderr.getvalue())

    def test_dry_run_does_not_require_smtp_credentials(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            result = email_me.main(["--dry-run", "--subject", "hi"])
        self.assertEqual(0, result)
        self.assertIn("dry-run: email not sent", stdout.getvalue())

    def test_guest_hees_mode_requires_manager_human_and_guest_session(self) -> None:
        for argv in (
            ["--guest-hees", "--subject", "hi"],
            [
                "--guest-hees",
                "--manager-human",
                "--tmux-target",
                "other:1",
                "--subject",
                "hi",
            ],
        ):
            with (
                self.subTest(argv=argv),
                patch.object(sys, "stdin", StringIO("body\n")),
                patch("sys.stderr", new_callable=StringIO),
                self.assertRaises(SystemExit) as raised,
            ):
                email_me.parse_args(argv)
            self.assertEqual(2, raised.exception.code)
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(
                [
                    "--guest-hees",
                    "--manager-human",
                    "--tmux-target",
                    "guest_hees:1",
                    "--subject",
                    "hi",
                ]
            )
        self.assertTrue(args.guest_hees)

    def test_guest_sender_uses_real_helper_imports(self) -> None:
        self.assertIs(
            email_me.configured_agent_mail, omo_email_config.configured_agent_mail
        )
        self.assertIs(
            email_me.prepare_subject_and_headers,
            omo_email_subject.prepare_subject_and_headers,
        )
        self.assertIs(email_me.reply_attachments, omo_guest_images.reply_attachments)

    def test_guest_image_reference_rejects_non_guest_producer(self) -> None:
        reference = "guest-image:v1:" + "a" * 64
        with (
            patch.dict(os.environ, {"OMO_AGENT_TMUX_TARGET": "other:1"}, clear=False),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(
                [
                    "--manager-human",
                    "--non-completion",
                    "--guest-image-reference",
                    reference,
                    "--subject",
                    "hi",
                ]
            )
        self.assertEqual(2, result)
        self.assertIn("requires a guest_hees producer target", stderr.getvalue())

    def test_guest_manager_target_implies_pinned_guest_recipient(self) -> None:
        with patch.object(sys, "stdin", StringIO("body\n")):
            args = email_me.parse_args(
                [
                    "--manager-human",
                    "--non-completion",
                    "--tmux-target",
                    "guest_hees:0",
                    "--subject",
                    "hi",
                ]
            )
        self.assertTrue(args.guest_hees)

    def test_guest_dedupe_includes_image_references_and_uses_separate_state(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": tmp}, clear=False),
        ):
            reference_a = "guest-image:v1:" + "a" * 64
            reference_b = "guest-image:v1:" + "b" * 64
            self.assertTrue(
                email_me.should_send_manager_email_key(
                    "topic", "topic", f"body\0{reference_a}", "guest-hees"
                )
            )
            self.assertTrue(
                email_me.should_send_manager_email_key(
                    "topic", "topic", f"body\0{reference_b}", "guest-hees"
                )
            )
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
            self.assertTrue(
                omo_email_config.ensure_guest_hees_reply_obligation(
                    state, "guest_hees_manager_mail/request.txt", parent
                )
            )
            prepared = (
                "Re: [guest_hees:1] Topic",
                {"In-Reply-To": parent, "References": parent},
            )
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            with (
                patch.dict(
                    os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False
                ),
                patch.object(sys, "stdin", StringIO("guest reply\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ),
                patch.object(
                    email_me, "verify_guest_reply_in_sent", return_value=evidence
                ),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(
                    [
                        "--guest-hees",
                        "--manager-human",
                        "--tmux-target",
                        "guest_hees:1",
                        "--subject",
                        "Re: Topic",
                    ]
                )
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
            self.assertTrue(
                omo_email_config.ensure_guest_hees_reply_obligation(
                    state, "guest_hees_manager_mail/inferred.txt", parent
                )
            )
            prepared = (
                "Re: [guest_hees:2] Topic",
                {"In-Reply-To": parent, "References": parent},
            )
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            with (
                patch.dict(
                    os.environ,
                    {
                        "OMO_MANAGER_STATE_DIR": str(state),
                        "OMO_AGENT_TMUX_TARGET": "guest_hees:2",
                    },
                    clear=False,
                ),
                patch.object(sys, "stdin", StringIO("guest reply\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ),
                patch.object(
                    email_me, "verify_guest_reply_in_sent", return_value=evidence
                ),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(
                    ["--manager-human", "--non-completion", "--subject", "Re: Topic"]
                )
        self.assertEqual(0, result)
        self.assertEqual("46496337@qq.com", sent_messages[0]["To"])
        self.assertIn("[guest_hees:2]", sent_messages[0]["Subject"])

    def test_guest_hees_reply_resolves_selected_images_before_smtp(self) -> None:
        sent_messages = []
        image_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            + b"\x00" * 13
            + b"\x00\x00\x00\x00IEND\x00\x00\x00\x00"
        )

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
            incoming.add_attachment(
                image_bytes, maintype="image", subtype="png", filename="image.png"
            )
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
            self.assertTrue(
                omo_email_config.ensure_guest_hees_reply_obligation(
                    state, "guest_hees_manager_mail/image.txt", parent
                )
            )
            prepared = (
                "Re: [guest_hees:3] Image",
                {"In-Reply-To": parent, "References": parent},
            )
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            with (
                patch.dict(
                    os.environ,
                    {
                        "OMO_MANAGER_STATE_DIR": str(state),
                        "OMO_GUEST_IMAGE_ROOT": str(image_root),
                        "OMO_AGENT_TMUX_TARGET": "guest_hees:3",
                    },
                    clear=False,
                ),
                patch.object(sys, "stdin", StringIO("guest reply\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ),
                patch.object(
                    email_me, "verify_guest_reply_in_sent", return_value=evidence
                ),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(
                    [
                        "--guest-image-reference",
                        reference,
                        "--manager-human",
                        "--subject",
                        "Re: Image",
                    ]
                )
        self.assertEqual(0, result)
        self.assertEqual("46496337@qq.com", sent_messages[0]["To"])
        self.assertIn("[guest_hees:3]", sent_messages[0]["Subject"])
        attachments = list(sent_messages[0].iter_attachments())
        self.assertEqual(1, len(attachments))
        self.assertEqual(image_bytes, attachments[0].get_payload(decode=True))

    def test_guest_reply_rejects_lifecycle_notice_and_missing_thread_before_smtp(
        self,
    ) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        cases = (
            (
                "Task: guest.md\nOutcome: task done\n",
                (
                    "Re: [guest_hees:7] Topic",
                    {
                        "In-Reply-To": "<request@example.test>",
                        "References": "<request@example.test>",
                    },
                ),
                "substantive",
            ),
            (
                "This is the requested answer.\n",
                ("Re: [guest_hees:7] Topic", {}),
                "thread",
            ),
        )
        for content, prepared, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / "state"
                with (
                    patch.dict(
                        os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False
                    ),
                    patch.object(sys, "stdin", StringIO(content)),
                    patch.object(
                        email_me, "configured_agent_mail", return_value=Settings()
                    ),
                    patch.object(
                        email_me, "prepare_subject_and_headers", return_value=prepared
                    ),
                    patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
                    patch("sys.stderr", new_callable=StringIO) as stderr,
                ):
                    result = email_me.main(
                        [
                            "--guest-hees",
                            "--manager-human",
                            "--tmux-target",
                            "guest_hees:7",
                            "--subject",
                            "Re: Topic",
                        ]
                    )
                self.assertEqual(2, result)
                self.assertIn(error, stderr.getvalue())
                smtp.assert_not_called()

    def test_sent_guest_reply_rejects_same_headers_with_different_body(self) -> None:
        headers = {
            "In-Reply-To": "<request@example.test>",
            "References": "<request@example.test>",
        }
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
        self.assertFalse(
            email_me.sent_message_matches_guest_reply(
                candidate, expected, "agent@example.test"
            )
        )
        exact = email_me.BytesParser(policy=email_me.policy.default).parsebytes(
            expected.as_bytes()
        )
        self.assertTrue(
            email_me.sent_message_matches_guest_reply(
                exact, expected, "agent@example.test"
            )
        )

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
                self.assertTrue(
                    omo_email_config.ensure_guest_hees_reply_obligation(
                        state, source, parent
                    )
                )
                prepared = (
                    "Re: [guest_hees:7] Topic",
                    {"In-Reply-To": parent, "References": parent},
                )
                evidence = (
                    email_me.GuestSentEvidence("a" * 64, "b" * 64) if verified else None
                )
                with (
                    patch.dict(
                        os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False
                    ),
                    patch.object(
                        sys,
                        "stdin",
                        StringIO("Substantive answer despite uncertain SMTP.\n"),
                    ),
                    patch.object(
                        email_me, "configured_agent_mail", return_value=Settings()
                    ),
                    patch.object(
                        email_me, "prepare_subject_and_headers", return_value=prepared
                    ),
                    patch.object(
                        email_me, "verify_guest_reply_in_sent", return_value=evidence
                    ),
                    patch.object(email_me.smtplib, "SMTP_SSL", UncertainSmtp),
                    patch.object(
                        email_me.ssl, "create_default_context", return_value=None
                    ),
                    patch.object(email_me, "maybe_print_thread_reminder"),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    result = email_me.main(
                        [
                            "--guest-hees",
                            "--manager-human",
                            "--tmux-target",
                            "guest_hees:7",
                            "--subject",
                            "Re: Topic",
                        ]
                    )
                self.assertEqual(0 if verified else 1, result)
                obligation = omo_email_config.read_guest_hees_reply_obligation(
                    state, source
                )
                assert obligation is not None
                self.assertEqual("fulfilled" if verified else "open", obligation.status)
                self.assertFalse((state / "guest-hees-email-dedupe.tsv").exists())

    def test_guest_uncertain_attempt_retries_same_state_then_fulfills_once(
        self,
    ) -> None:
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
            self.assertTrue(
                omo_email_config.ensure_guest_hees_reply_obligation(
                    state, source, parent
                )
            )
            prepared = (
                "Re: [guest_hees:7] Retry",
                {"In-Reply-To": parent, "References": parent},
            )
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            env = {
                "OMO_MANAGER_STATE_DIR": str(state),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "300",
            }
            argv = [
                "--guest-hees",
                "--manager-human",
                "--tmux-target",
                "guest_hees:7",
                "--subject",
                "Re: Retry",
            ]

            sent_evidence = iter((None, None, evidence))

            def invoke() -> int:
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch.object(
                        sys,
                        "stdin",
                        StringIO("Substantive answer retried in the same state.\n"),
                    ),
                    patch.object(
                        email_me, "configured_agent_mail", return_value=Settings()
                    ),
                    patch.object(
                        email_me, "prepare_subject_and_headers", return_value=prepared
                    ),
                    patch.object(
                        email_me,
                        "verify_guest_reply_in_sent",
                        side_effect=sent_evidence,
                    ),
                    patch.object(email_me.smtplib, "SMTP_SSL", UncertainSmtp),
                    patch.object(
                        email_me.ssl, "create_default_context", return_value=None
                    ),
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
            self.assertTrue(
                omo_email_config.ensure_guest_hees_reply_obligation(
                    state, source, parent
                )
            )
            prepared = (
                "Re: [guest_hees:7] Delayed",
                {"In-Reply-To": parent, "References": parent},
            )
            evidence = email_me.GuestSentEvidence("a" * 64, "b" * 64)
            env = {
                "OMO_MANAGER_STATE_DIR": str(state),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "300",
            }
            argv = [
                "--guest-hees",
                "--manager-human",
                "--tmux-target",
                "guest_hees:7",
                "--subject",
                "Re: Delayed",
            ]

            def invoke(sent_evidence: object) -> int:
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch.object(
                        sys, "stdin", StringIO("Substantive delayed-copy answer.\n")
                    ),
                    patch.object(
                        email_me, "configured_agent_mail", return_value=Settings()
                    ),
                    patch.object(
                        email_me, "prepare_subject_and_headers", return_value=prepared
                    ),
                    patch.object(
                        email_me,
                        "verify_guest_reply_in_sent",
                        return_value=sent_evidence,
                    ),
                    patch.object(email_me.smtplib, "SMTP_SSL", UncertainSmtp),
                    patch.object(
                        email_me.ssl, "create_default_context", return_value=None
                    ),
                    patch.object(email_me, "maybe_print_thread_reminder"),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    return email_me.main(argv)

            self.assertEqual(1, invoke(None))
            self.assertEqual(1, len(smtp_messages))
            self.assertEqual(0, invoke(evidence))
            self.assertEqual(1, len(smtp_messages))
            obligation = omo_email_config.read_guest_hees_reply_obligation(
                state, source
            )
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
            prepared = (
                "Re: [guest_hees:7] Topic",
                {"In-Reply-To": parent, "References": parent},
            )
            env = {
                "OMO_MANAGER_STATE_DIR": str(state),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "300",
            }
            argv = [
                "--guest-hees",
                "--manager-human",
                "--tmux-target",
                "guest_hees:7",
                "--subject",
                "Re: Topic",
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(sys, "stdin", StringIO("Substantive duplicate answer.\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ),
                patch.object(
                    email_me, "fake_send_log_path", return_value=state / "fake-send.txt"
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(2, email_me.main(argv))
            self.assertIn("cannot verify", stderr.getvalue())
            obligation = omo_email_config.read_guest_hees_reply_obligation(
                state, "guest_hees_manager_mail/duplicate.txt"
            )
            assert obligation is not None
            self.assertEqual("open", obligation.status)
            self.assertFalse((state / "fake-send.txt").exists())

    def test_guest_obligation_reservation_blocks_different_body_concurrently(
        self,
    ) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            parent = "<concurrent@example.test>"
            source = "guest_hees_manager_mail/concurrent.txt"
            self.assertTrue(
                omo_email_config.ensure_guest_hees_reply_obligation(
                    state, source, parent
                )
            )
            prepared = (
                "Re: [guest_hees:7] Topic",
                {"In-Reply-To": parent, "References": parent},
            )
            env = {
                "OMO_MANAGER_STATE_DIR": str(state),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "300",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    sys,
                    "stdin",
                    StringIO("A completely different substantive answer.\n"),
                ),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ),
                patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                claim = email_me.acquire_guest_reply_claim(state, source)
                assert claim is not None
                try:
                    self.assertEqual(
                        1,
                        email_me.main(
                            [
                                "--guest-hees",
                                "--manager-human",
                                "--tmux-target",
                                "guest_hees:7",
                                "--subject",
                                "Re: Topic",
                            ]
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
            (
                "directory",
                lambda: patch.object(
                    email_me.Path, "mkdir", side_effect=OSError("directory unavailable")
                ),
            ),
            (
                "lock",
                lambda: patch.object(
                    email_me.fcntl, "flock", side_effect=BlockingIOError("claim busy")
                ),
            ),
            (
                "write",
                lambda: patch.object(
                    email_me.os, "write", side_effect=OSError("write failed")
                ),
            ),
        )
        for name, fault in faults:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                smtp_messages.clear()
                state = Path(tmp) / "state"
                source = f"guest_hees_manager_mail/claim-{name}.txt"
                parent = f"<claim-{name}@example.test>"
                self.assertTrue(
                    omo_email_config.ensure_guest_hees_reply_obligation(
                        state, source, parent
                    )
                )
                prepared = (
                    "Re: [guest_hees:7] Claim",
                    {"In-Reply-To": parent, "References": parent},
                )
                env = {"OMO_MANAGER_STATE_DIR": str(state)}
                argv = [
                    "--guest-hees",
                    "--manager-human",
                    "--tmux-target",
                    "guest_hees:7",
                    "--subject",
                    "Re: Claim",
                ]
                with (
                    fault(),
                    patch.dict(os.environ, env, clear=False),
                    patch.object(
                        sys, "stdin", StringIO("Substantive claim-recovery answer.\n")
                    ),
                    patch.object(
                        email_me, "configured_agent_mail", return_value=Settings()
                    ),
                    patch.object(
                        email_me, "prepare_subject_and_headers", return_value=prepared
                    ),
                    patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    self.assertEqual(1, email_me.main(argv))
                self.assertEqual([], smtp_messages)
                obligation = omo_email_config.read_guest_hees_reply_obligation(
                    state, source
                )
                assert obligation is not None
                self.assertEqual("open", obligation.status)

                with (
                    patch.dict(os.environ, env, clear=False),
                    patch.object(
                        sys, "stdin", StringIO("Substantive claim-recovery answer.\n")
                    ),
                    patch.object(
                        email_me, "configured_agent_mail", return_value=Settings()
                    ),
                    patch.object(
                        email_me, "prepare_subject_and_headers", return_value=prepared
                    ),
                    patch.object(
                        email_me,
                        "verify_guest_reply_in_sent",
                        return_value=email_me.GuestSentEvidence("a" * 64, "b" * 64),
                    ),
                    patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                    patch.object(
                        email_me.ssl, "create_default_context", return_value=None
                    ),
                    patch.object(email_me, "maybe_print_thread_reminder"),
                ):
                    self.assertEqual(0, email_me.main(argv))
                self.assertEqual(1, len(smtp_messages))
                fulfilled = omo_email_config.read_guest_hees_reply_obligation(
                    state, source
                )
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
            prepared = (
                "Re: [wl:1] Existing topic",
                {"In-Reply-To": "<prior@example.test>"},
            )
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me,
                    "prepare_latest_thread_for_tmux_target",
                    return_value=prepared,
                ) as prepare,
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(
                    ["--manager-human", "--non-completion", "--message-file", str(body)]
                )
            self.assertEqual(0, result)
            self.assertEqual(("wl:1",), prepare.call_args.args)
            self.assertEqual(
                "primary", prepare.call_args.kwargs["route_profile"].route_kind
            )
            self.assertEqual(
                "Re: [wl:1] Existing topic\nbody\n",
                (Path(tmp) / "sent.txt").read_text(encoding="utf-8"),
            )

    def test_completion_without_subject_requires_an_agent_used_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("Closed wl:1\n", encoding="utf-8")
            prepared = (
                "Re: Re: exact prior Human wording",
                {"In-Reply-To": "<prior@example.test>"},
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_ME_FAKE_SEND_LOG": str(Path(tmp) / "sent.txt"),
                        "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                        "OMO_MANAGER_TMUX_TARGET": "wl:1",
                        "CODEX_SESSION_ID": "01a0369c-7895-70f2-ae4b-5f59d920e99a",
                    },
                    clear=False,
                ),
                patch.object(
                    email_me, "validate_completion_authorization", return_value={}
                ),
                patch.object(email_me, "consume_completion_authorization"),
                patch.object(
                    email_me,
                    "prepare_latest_thread_for_tmux_target",
                    return_value=prepared,
                ) as prepare,
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--completion-authorization",
                        "a" * 64,
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(
                "01a0369c-7895-70f2-ae4b-5f59d920e99a",
                prepare.call_args.kwargs["required_agent_session"],
            )
            self.assertEqual(
                "Re: Re: exact prior Human wording\nClosed wl:1\n",
                (Path(tmp) / "sent.txt").read_text(encoding="utf-8"),
            )

    def test_pending_notice_without_subject_requires_same_agent_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("pending item created:\n- review\n", encoding="utf-8")
            prepared = (
                "Re: Re: exact prior Human wording",
                {"In-Reply-To": "<prior@example.test>"},
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_ME_FAKE_SEND_LOG": str(Path(tmp) / "sent.txt"),
                        "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                        "OMO_MANAGER_TMUX_TARGET": "wl:1",
                        "CODEX_SESSION_ID": "01a0369c-7895-70f2-ae4b-5f59d920e99a",
                    },
                    clear=False,
                ),
                patch.object(email_me, "validate_non_completion_owner"),
                patch.object(email_me, "validate_non_completion_thread"),
                patch.object(
                    email_me,
                    "prepare_latest_thread_for_tmux_target",
                    return_value=prepared,
                ) as prepare,
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--pending-notice-key",
                        "a" * 64,
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(
                "01a0369c-7895-70f2-ae4b-5f59d920e99a",
                prepare.call_args.kwargs["required_agent_session"],
            )
            self.assertEqual(
                "Re: Re: exact prior Human wording\npending item created:\n- review\n",
                (Path(tmp) / "sent.txt").read_text(encoding="utf-8"),
            )

    def test_pending_notice_rejects_explicit_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("pending item created:\n- review\n", encoding="utf-8")
            with patch("sys.stderr", new_callable=StringIO) as stderr:
                with self.assertRaises(SystemExit) as exit_status:
                    email_me.main(
                        [
                            "--manager-human",
                            "--non-completion",
                            "--pending-notice-key",
                            "a" * 64,
                            "--subject",
                            "older thread",
                            "--message-file",
                            str(body),
                        ]
                    )
            self.assertEqual(2, exit_status.exception.code)
            self.assertIn("reuse the latest verified thread", stderr.getvalue())

    def test_pending_notice_without_agent_session_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("pending item created:\n- review\n", encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_ME_FAKE_SEND_LOG": str(Path(tmp) / "sent.txt"),
                        "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                        "OMO_MANAGER_TMUX_TARGET": "wl:1",
                        "CODEX_SESSION_ID": "",
                        "CODEX_THREAD_ID": "",
                    },
                    clear=False,
                ),
                patch.object(
                    email_me, "prepare_latest_thread_for_tmux_target"
                ) as lookup,
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--pending-notice-key",
                        "a" * 64,
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(2, result)
            self.assertIn("current agent session identity", stderr.getvalue())
            lookup.assert_not_called()

    def test_pending_deletion_notice_without_agent_session_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("pending item deleted:\n- review\n", encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_ME_FAKE_SEND_LOG": str(Path(tmp) / "sent.txt"),
                        "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                        "OMO_MANAGER_TMUX_TARGET": "wl:1",
                        "CODEX_SESSION_ID": "",
                        "CODEX_THREAD_ID": "",
                    },
                    clear=False,
                ),
                patch.object(
                    email_me, "validate_completion_authorization", return_value={}
                ),
                patch.object(
                    email_me, "prepare_latest_thread_for_tmux_target"
                ) as lookup,
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--completion-authorization",
                        "a" * 64,
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(2, result)
            self.assertIn("current agent session identity", stderr.getvalue())
            lookup.assert_not_called()

    def assert_pending_notice_rejects_another_agent_session_thread(
        self, body_text: str, argv: list[str]
    ) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class FakeClient:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def login(self, _user: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                if not readonly:
                    raise AssertionError("thread lookup must be readonly")
                return "OK", []

            def uid(self, command: str, *_args: str) -> tuple[str, list[bytes]]:
                return ("OK", [b"1"]) if command == "search" else ("OK", [b""])

            def logout(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text(body_text, encoding="utf-8")
            current_session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
            other_session_thread = omo_email_subject.RecentHeader(
                "agent@example.test",
                "Re: exact other agent thread",
                email_me.datetime.now().astimezone(),
                "<other@example.test>",
                "",
                "human@example.test",
                agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99b",
            )

            with (
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_ME_FAKE_SEND_LOG": str(Path(tmp) / "sent.txt"),
                        "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                        "OMO_MANAGER_TMUX_TARGET": "wl:1",
                        "CODEX_SESSION_ID": current_session,
                        "CODEX_THREAD_ID": "",
                        "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400",
                    },
                    clear=False,
                ),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me, "validate_completion_authorization", return_value={}
                ),
                patch.object(
                    omo_email_subject, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
                patch.object(
                    omo_email_subject,
                    "fetch_recent_headers",
                    return_value=[other_session_thread],
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                result = email_me.main([*argv, "--message-file", str(body)])
            self.assertEqual(2, result)
            self.assertIn("no exact email thread", stderr.getvalue())
            self.assertFalse((Path(tmp) / "sent.txt").exists())

    def test_pending_creation_notice_rejects_another_agent_session_thread(self) -> None:
        self.assert_pending_notice_rejects_another_agent_session_thread(
            "pending item created:\n- review\n",
            ["--manager-human", "--non-completion", "--pending-notice-key", "a" * 64],
        )

    def test_pending_deletion_notice_rejects_another_agent_session_thread(self) -> None:
        self.assert_pending_notice_rejects_another_agent_session_thread(
            "pending item deleted:\n- review\n",
            ["--manager-human", "--completion-authorization", "a" * 64],
        )

    def test_pending_notice_requires_active_manager_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("pending item created:\n- review\n", encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {
                        "OMO_MANAGER_TMUX_TARGET": "wl:1",
                        "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                    },
                    clear=False,
                ),
                patch.object(
                    email_me, "validate_non_completion_owner", return_value=False
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--pending-notice-key",
                        "a" * 64,
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(2, result)
            self.assertIn("exact active manager owner", stderr.getvalue())

    def test_pending_notice_rejects_trailing_blank_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("pending item created:\n- review\n\n", encoding="utf-8")
            with patch("sys.stderr", new_callable=StringIO) as stderr:
                with self.assertRaises(SystemExit) as exit_status:
                    email_me.main(
                        [
                            "--manager-human",
                            "--non-completion",
                            "--pending-notice-key",
                            "a" * 64,
                            "--message-file",
                            str(body),
                        ]
                    )
            self.assertEqual(2, exit_status.exception.code)
            self.assertIn("exact pending-item creation body", stderr.getvalue())

    def test_pending_notice_retry_is_idempotent_when_latest_subject_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            sent = Path(tmp) / "sent.txt"
            body.write_text("pending item created:\n- review\n", encoding="utf-8")
            prepared = iter(
                [
                    (
                        "Re: [wl:1] first subject",
                        {"In-Reply-To": "<first@example.test>"},
                    ),
                    (
                        "Re: [wl:1] newer subject",
                        {"In-Reply-To": "<newer@example.test>"},
                    ),
                ]
            )
            argv = [
                "--manager-human",
                "--non-completion",
                "--pending-notice-key",
                "a" * 64,
                "--message-file",
                str(body),
            ]
            with (
                patch.dict(
                    os.environ,
                    {
                        "EMAIL_ME_FAKE_SEND_LOG": str(sent),
                        "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                        "OMO_MANAGER_TMUX_TARGET": "wl:1",
                        "CODEX_SESSION_ID": "01a0369c-7895-70f2-ae4b-5f59d920e99a",
                    },
                    clear=False,
                ),
                patch.object(email_me, "validate_non_completion_owner"),
                patch.object(email_me, "validate_non_completion_thread"),
                patch.object(
                    email_me,
                    "prepare_latest_thread_for_tmux_target",
                    side_effect=lambda *_args, **_kwargs: next(prepared),
                ),
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                self.assertEqual(0, email_me.main(argv))
                first = sent.read_text(encoding="utf-8")
                self.assertEqual(0, email_me.main(argv))
            self.assertEqual(
                "Re: [wl:1] first subject\npending item created:\n- review\n", first
            )
            self.assertEqual(first, sent.read_text(encoding="utf-8"))

    def test_agent_used_thread_may_have_an_older_target_ancestor(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "human@example.test", "primary"
        )
        header = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Re: [wl:7] Existing topic",
            email_me.datetime.now().astimezone(),
            "<current@example.test>",
            "<older@example.test>",
            "human@example.test",
            thread_target="wl:1",
            agent_session=session,
        )
        with patch.object(
            omo_email_subject, "verified_recent_thread_header", return_value=header
        ) as lookup:
            prepared = omo_email_subject.prepare_latest_thread_for_tmux_target(
                "wl:7", profile, required_agent_session=session
            )
        self.assertEqual(
            (
                "Re: [wl:7] Existing topic",
                {
                    "In-Reply-To": "<current@example.test>",
                    "References": "<older@example.test> <current@example.test>",
                },
            ),
            prepared,
        )
        self.assertEqual(session, lookup.call_args.kwargs["required_agent_session"])
        with (
            patch.object(
                omo_email_subject, "verified_recent_thread_header", return_value=header
            ),
            self.assertRaisesRegex(
                omo_email_subject.SubjectInputError, "wl:1; wl:7 may not retag"
            ),
        ):
            omo_email_subject.prepare_latest_thread_for_tmux_target("wl:7", profile)

    def test_completion_thread_reuses_exact_latest_subject(self) -> None:
        session = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "human@example.test", "primary"
        )
        header = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Re: Re: exact Human wording",
            email_me.datetime.now().astimezone(),
            "<current@example.test>",
            "<older@example.test>",
            "human@example.test",
            thread_target="wl:1",
            agent_session=session,
        )
        with patch.object(
            omo_email_subject, "verified_recent_thread_header", return_value=header
        ):
            subject, reply_headers = (
                omo_email_subject.prepare_latest_thread_for_tmux_target(
                    "wl:1", profile, required_agent_session=session
                )
            )
        self.assertEqual(header.subject, subject)
        self.assertEqual("<current@example.test>", reply_headers["In-Reply-To"])

    def test_completion_missing_or_ambiguous_previous_thread_fails_before_delivery(
        self,
    ) -> None:
        for detail in (
            "no recent email thread found for tmux target wl:1",
            "email thread lookup is ambiguous for the selected route",
        ):
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as tmp:
                body = Path(tmp) / "body.md"
                body.write_text("pending item created:\n- review\n", encoding="utf-8")
                with (
                    patch.dict(
                        os.environ,
                        {
                            "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                            "OMO_MANAGER_TMUX_TARGET": "wl:1",
                            "CODEX_SESSION_ID": "01a0369c-7895-70f2-ae4b-5f59d920e99a",
                        },
                        clear=False,
                    ),
                    patch.object(
                        email_me, "validate_completion_authorization", return_value={}
                    ),
                    patch.object(
                        email_me,
                        "prepare_latest_thread_for_tmux_target",
                        side_effect=email_me.SubjectInputError(detail),
                    ),
                    patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
                    patch("sys.stderr", new_callable=StringIO) as stderr,
                ):
                    result = email_me.main(
                        [
                            "--manager-human",
                            "--completion-authorization",
                            "a" * 64,
                            "--message-file",
                            str(body),
                        ]
                    )
                self.assertEqual(2, result)
                self.assertIn(detail.split()[0], stderr.getvalue())
                smtp.assert_not_called()

    def test_omitted_subject_fails_when_no_thread_exists(self) -> None:
        with (
            patch.dict(os.environ, {"OMO_MANAGER_TMUX_TARGET": "wl:1"}, clear=False),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(
                email_me,
                "prepare_latest_thread_for_tmux_target",
                side_effect=email_me.SubjectInputError(
                    "no recent email thread found for tmux target wl:1"
                ),
            ),
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--manager-human", "--non-completion"])
        self.assertEqual(2, result)
        self.assertIn("no recent email thread", stderr.getvalue())

    def test_omitted_subject_fails_closed_on_ambiguous_threads(self) -> None:
        with (
            patch.dict(os.environ, {"OMO_MANAGER_TMUX_TARGET": "wl:1"}, clear=False),
            patch.object(sys, "stdin", StringIO("Closed wl:1\n")),
            patch.object(
                email_me,
                "prepare_latest_thread_for_tmux_target",
                side_effect=email_me.SubjectInputError(
                    "email thread lookup is ambiguous for the selected route"
                ),
            ),
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(["--manager-human", "--non-completion"])
        self.assertEqual(2, result)
        self.assertIn("ambiguous", stderr.getvalue())
        smtp.assert_not_called()

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
            {
                "In-Reply-To": "<prior@example.test>",
                "References": "<root@example.test> <prior@example.test>",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(
                    os.environ,
                    {"OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state")},
                    clear=False,
                ),
                patch.object(sys, "stdin", StringIO("body\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me,
                    "prepare_latest_thread_for_tmux_target",
                    return_value=prepared,
                ),
                patch.object(email_me, "maybe_print_thread_reminder"),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
            ):
                self.assertEqual(
                    0,
                    email_me.main(
                        ["--manager-human", "--non-completion", "--tmux-target", "wl:1"]
                    ),
                )
        self.assertEqual("Re: [wl:1] Existing topic", sent_messages[0]["Subject"])
        self.assertEqual("<prior@example.test>", sent_messages[0]["In-Reply-To"])
        self.assertEqual(
            "<root@example.test> <prior@example.test>", sent_messages[0]["References"]
        )

    def test_thread_reminder_is_printed_one_in_eight(self) -> None:
        with (
            patch.object(email_me.secrets, "randbelow", return_value=0),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            email_me.maybe_print_thread_reminder()
        self.assertIn("omit --subject", stdout.getvalue())

    def test_thread_reminder_is_otherwise_quiet(self) -> None:
        with (
            patch.object(email_me.secrets, "randbelow", return_value=1),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            email_me.maybe_print_thread_reminder()
        self.assertEqual("", stdout.getvalue())

    def test_target_thread_lookup_matches_zero_pane_alias_only(self) -> None:
        matching = omo_email_subject.RecentHeader(
            "human@example.test", "Re: [hcfg:1.0] Topic", None
        )
        sibling = omo_email_subject.RecentHeader(
            "human@example.test", "Re: [hcfg:1.1] Topic", None
        )

        def select(predicate: object, subject_query: str) -> object:
            self.assertTrue(callable(predicate))
            self.assertEqual("hcfg:1", subject_query)
            self.assertTrue(predicate(matching))
            self.assertFalse(predicate(sibling))
            return matching

        with patch.object(
            omo_email_subject, "find_recent_thread_matching", side_effect=select
        ):
            self.assertEqual(
                matching, omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1")
            )

    def test_target_thread_lookup_rejects_nonleading_and_competing_tags(self) -> None:
        nonleading = omo_email_subject.RecentHeader(
            "human@example.test", "Re: Topic [hcfg:1]", None
        )
        competing = omo_email_subject.RecentHeader(
            "human@example.test", "Re: [hcfg:1] [wl:2] Topic", None
        )
        missing_separator = omo_email_subject.RecentHeader(
            "human@example.test", "Re: [hcfg:1]Topic", None
        )

        def select(predicate: object, subject_query: str) -> None:
            self.assertTrue(callable(predicate))
            self.assertEqual("hcfg:1", subject_query)
            self.assertFalse(predicate(nonleading))
            self.assertFalse(predicate(competing))
            self.assertFalse(predicate(missing_separator))
            return None

        with patch.object(
            omo_email_subject, "find_recent_thread_matching", side_effect=select
        ):
            self.assertIsNone(
                omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1")
            )

    def test_target_thread_lookup_accepts_alternating_legacy_reply_prefixes(
        self,
    ) -> None:
        matching = omo_email_subject.RecentHeader(
            "human@example.test", "Re: [a] Re: [hcfg:1] Topic", None
        )

        def select(predicate: object, subject_query: str) -> object:
            self.assertTrue(callable(predicate))
            self.assertEqual("hcfg:1", subject_query)
            self.assertTrue(predicate(matching))
            return matching

        with patch.object(
            omo_email_subject, "find_recent_thread_matching", side_effect=select
        ):
            self.assertEqual(
                matching, omo_email_subject.find_recent_thread_for_tmux_target("hcfg:1")
            )

    def test_route_profiles_keep_guest_and_primary_thread_participants_separate(
        self,
    ) -> None:
        guest = omo_email_subject.MailRouteProfile(
            "agent@example.test", "46496337@qq.com", "guest-hees"
        )
        primary = omo_email_subject.MailRouteProfile(
            "agent@example.test", "human@example.test", "primary"
        )
        primary_sent = omo_email_subject.RecentHeader(
            "Agent <agent@example.test>",
            "Topic",
            None,
            "<primary@example.test>",
            "",
            "Human <human@example.test>",
        )
        guest_sent = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Topic",
            None,
            "<guest@example.test>",
            "",
            "46496337@qq.com",
        )
        guest_incoming = omo_email_subject.RecentHeader(
            "46496337@qq.com",
            "Topic",
            None,
            "<incoming@example.test>",
            "",
            "agent@example.test",
        )
        self.assertTrue(
            omo_email_subject.route_matches_header(
                primary_sent, primary.agent_address, primary.counterparty_address
            )
        )
        self.assertFalse(
            omo_email_subject.route_matches_header(
                primary_sent, guest.agent_address, guest.counterparty_address
            )
        )
        self.assertTrue(
            omo_email_subject.route_matches_header(
                guest_sent, guest.agent_address, guest.counterparty_address
            )
        )
        self.assertFalse(
            omo_email_subject.route_matches_header(
                guest_sent, primary.agent_address, primary.counterparty_address
            )
        )
        self.assertTrue(
            omo_email_subject.route_matches_header(
                guest_incoming, guest.counterparty_address, guest.agent_address
            )
        )

    def test_qq_reply_prefix_resolves_exact_guest_parent(self) -> None:
        raw_subject = "回复：[guest_hees:4] guest_hees_contain.md: task done"
        parent = "<tencent_B0967E13E3E05719BBB66C1B8A1B897B6808@qq.com>"
        root = "<178812140322.1211423.5011212448780635128@gmail.com>"
        profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "46496337@qq.com", "guest-hees", frozenset({parent})
        )
        complaint_time = email_me.datetime.now().astimezone() - timedelta(minutes=2)
        sent_root = omo_email_subject.RecentHeader(
            "agent@example.test",
            "[guest_hees:4] guest_hees_contain.md: task done",
            complaint_time - timedelta(minutes=1),
            root,
            "",
            "46496337@qq.com",
        )
        complaint = omo_email_subject.RecentHeader(
            "46496337@qq.com",
            raw_subject,
            complaint_time,
            parent,
            root,
            "agent@example.test",
        )
        later_inbound = omo_email_subject.RecentHeader(
            "46496337@qq.com",
            raw_subject,
            complaint_time + timedelta(seconds=30),
            "<later-unbound-inbound@qq.com>",
            f"{root} {parent}",
            "agent@example.test",
        )
        later_sent = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Re: [guest_hees:6] guest_hees_contain.md: task done",
            complaint_time + timedelta(minutes=1),
            "<later-sent@example.test>",
            f"{root} {parent}",
            "46496337@qq.com",
        )
        subject_key = omo_email_subject.normalized_subject_key(
            raw_subject, guest_hees=True
        )
        self.assertEqual(
            subject_key,
            omo_email_subject.normalized_subject_key(
                later_sent.subject, guest_hees=True
            ),
        )
        self.assertTrue(
            omo_email_subject.route_matches_header(
                later_sent, profile.agent_address, profile.counterparty_address
            )
        )
        self.assertEqual(
            later_sent,
            omo_email_subject.select_recent_thread(
                [sent_root, complaint, later_inbound, later_sent], reject_ambiguous=True
            ),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class FakeClient:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def login(self, _user: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                self.assert_readonly(readonly)
                return "OK", []

            @staticmethod
            def assert_readonly(readonly: bool) -> None:
                if not readonly:
                    raise AssertionError("thread lookup must be readonly")

            def uid(self, command: str, *_args: str) -> tuple[str, list[bytes]]:
                return ("OK", [b"1 2 3 4"]) if command == "search" else ("OK", [b""])

            def logout(self) -> None:
                return None

        with (
            patch.object(
                omo_email_subject, "configured_agent_mail", return_value=Settings()
            ),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(
                omo_email_subject,
                "fetch_recent_headers",
                return_value=[sent_root, complaint, later_inbound, later_sent],
            ),
            patch.dict(
                os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False
            ),
        ):
            prepared = omo_email_subject.prepare_subject_and_headers(
                raw_subject, "guest_hees:7", route_profile=profile
            )
        self.assertEqual(
            (
                "Re: [guest_hees:7] guest_hees_contain.md: task done",
                {"In-Reply-To": parent, "References": f"{root} {parent}"},
            ),
            prepared,
        )
        self.assertEqual(
            ["guest_hees:7"],
            omo_email_subject.BRACKETED_TMUX_TAG_RE.findall(prepared[0]),
        )

    def test_qq_reply_prefix_passes_guest_sender_validation(self) -> None:
        sent_messages = []
        raw_subject = "回复：[guest_hees:4] guest_hees_contain.md: task done"
        parent = "<tencent_B0967E13E3E05719BBB66C1B8A1B897B6808@qq.com>"
        root = "<178812140322.1211423.5011212448780635128@gmail.com>"
        complaint = omo_email_subject.RecentHeader(
            "46496337@qq.com",
            raw_subject,
            email_me.datetime.now().astimezone(),
            parent,
            root,
            "agent@example.test",
        )

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

        def select_open_parent(
            subject_key: str, route_profile: object, reject_ambiguous: bool
        ) -> object:
            self.assertEqual("guest_hees_contain.md: task done", subject_key)
            self.assertIsInstance(route_profile, omo_email_subject.MailRouteProfile)
            assert isinstance(route_profile, omo_email_subject.MailRouteProfile)
            self.assertEqual(frozenset({parent}), route_profile.parent_message_ids)
            self.assertTrue(reject_ambiguous)
            return complaint

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            self.assertTrue(
                omo_email_config.ensure_guest_hees_reply_obligation(
                    state, "guest_hees_manager_mail/complaint.txt", parent
                )
            )
            with (
                patch.dict(
                    os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False
                ),
                patch.object(sys, "stdin", StringIO("这是对空内容投诉的正式答复。\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    omo_email_subject,
                    "find_recent_thread",
                    side_effect=select_open_parent,
                ),
                patch.object(
                    email_me,
                    "verify_guest_reply_in_sent",
                    return_value=email_me.GuestSentEvidence("a" * 64, "b" * 64),
                ),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch.object(email_me, "maybe_print_thread_reminder"),
            ):
                result = email_me.main(
                    [
                        "--guest-hees",
                        "--manager-human",
                        "--tmux-target",
                        "guest_hees:7",
                        "--subject",
                        raw_subject,
                    ]
                )
        self.assertEqual(0, result)
        self.assertEqual(1, len(sent_messages))
        self.assertEqual(
            "Re: [guest_hees:7] guest_hees_contain.md: task done",
            sent_messages[0]["Subject"],
        )
        self.assertEqual(parent, sent_messages[0]["In-Reply-To"])
        self.assertEqual(f"{root} {parent}", sent_messages[0]["References"])

    def test_qq_reply_prefix_change_preserves_primary_subject_handling(self) -> None:
        profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "human@example.test", "primary"
        )
        parent = "<primary-reply@example.test>"
        header = omo_email_subject.RecentHeader(
            "human@example.test",
            "Re: [wl:3] Primary topic",
            email_me.datetime.now().astimezone(),
            parent,
            "<primary-root@example.test>",
            "agent@example.test",
        )
        with patch.object(omo_email_subject, "find_recent_thread", return_value=header):
            with self.assertRaisesRegex(
                omo_email_subject.SubjectInputError,
                "addressed to wl:3; wl:7 may not retag it",
            ):
                omo_email_subject.prepare_subject_and_headers(
                    "Re: [wl:3] Primary topic", "wl:7", route_profile=profile
                )
        with patch.object(omo_email_subject, "verified_recent_thread_header") as lookup:
            new_topic = omo_email_subject.prepare_subject_and_headers(
                "回复：Primary new topic", "wl:7", route_profile=profile
            )
        self.assertEqual(("[wl:7] 回复：Primary new topic", {}), new_topic)
        lookup.assert_not_called()
        self.assertEqual(
            "回复：primary new topic",
            omo_email_subject.normalized_subject_key("回复：Primary new topic"),
        )

    def test_route_match_rejects_wrong_or_multiple_recipients(self) -> None:
        wrong = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Topic",
            None,
            "<wrong@example.test>",
            "",
            "other@example.test",
        )
        multiple = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Topic",
            None,
            "<multiple@example.test>",
            "",
            "46496337@qq.com, human@example.test",
        )
        self.assertFalse(
            omo_email_subject.route_matches_header(
                wrong, "agent@example.test", "46496337@qq.com"
            )
        )
        self.assertFalse(
            omo_email_subject.route_matches_header(
                multiple, "agent@example.test", "46496337@qq.com"
            )
        )

    def test_route_profile_lookup_cannot_select_same_subject_from_other_counterparty(
        self,
    ) -> None:
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
                return (
                    ("OK", [b"1 2"])
                    if command == "search" and self.mailbox == '"[Gmail]/Sent Mail"'
                    else ("OK", [b""])
                )

            def logout(self) -> None:
                return None

        now = email_me.datetime.now().astimezone()
        primary_header = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Topic",
            now,
            "<primary@example.test>",
            "",
            "human@example.test",
        )
        guest_header = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Topic",
            now,
            "<guest@example.test>",
            "",
            "46496337@qq.com",
        )
        guest_profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "46496337@qq.com", "guest-hees"
        )
        with (
            patch.object(
                omo_email_subject, "configured_agent_mail", return_value=Settings()
            ),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(
                omo_email_subject,
                "fetch_recent_headers",
                return_value=[primary_header, guest_header],
            ),
            patch.dict(
                os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False
            ),
        ):
            selected = omo_email_subject.find_recent_thread(
                "topic", guest_profile, reject_ambiguous=True
            )
        self.assertEqual(guest_header, selected)
        self.assertTrue(
            any(call[0] == "search" and '"46496337@qq.com"' in call for call in calls)
        )
        self.assertFalse(
            any(
                call[0] == "search" and '"human@example.test"' in call for call in calls
            )
        )
        calls.clear()
        primary_profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "human@example.test", "primary"
        )
        with (
            patch.object(
                omo_email_subject, "configured_agent_mail", return_value=Settings()
            ),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(
                omo_email_subject,
                "fetch_recent_headers",
                return_value=[primary_header, guest_header],
            ),
            patch.dict(
                os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False
            ),
        ):
            selected = omo_email_subject.find_recent_thread(
                "topic", primary_profile, reject_ambiguous=True
            )
        self.assertEqual(primary_header, selected)
        self.assertTrue(
            any(
                call[0] == "search" and '"human@example.test"' in call for call in calls
            )
        )
        self.assertFalse(
            any(call[0] == "search" and '"46496337@qq.com"' in call for call in calls)
        )

    def test_agent_used_thread_lookup_selects_latest_same_session_agent_mail(
        self,
    ) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class FakeClient:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout

            def login(self, _user: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
                self.assert_readonly(readonly)
                return "OK", []

            @staticmethod
            def assert_readonly(readonly: bool) -> None:
                if not readonly:
                    raise AssertionError("thread lookup must be readonly")

            def uid(self, command: str, *_args: str) -> tuple[str, list[bytes]]:
                return ("OK", [b"1"]) if command == "search" else ("OK", [b""])

            def logout(self) -> None:
                return None

        now = email_me.datetime.now().astimezone()
        sent = omo_email_subject.RecentHeader(
            "agent@example.test",
            "[wl:1] Existing topic",
            now - timedelta(minutes=1),
            "<sent@example.test>",
            "",
            "human@example.test",
            agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
        )
        inbound = omo_email_subject.RecentHeader(
            "human@example.test",
            "Re: [wl:1] Existing topic",
            now,
            "<inbound@example.test>",
            "<sent@example.test>",
            "agent@example.test",
        )
        older_sent = omo_email_subject.RecentHeader(
            "agent@example.test",
            "[wl:1] Older topic",
            now - timedelta(minutes=2),
            "<older-sent@example.test>",
            "",
            "human@example.test",
            agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
        )
        older_inbound = omo_email_subject.RecentHeader(
            "human@example.test",
            "Re: [wl:1] Older topic",
            now + timedelta(minutes=1),
            "<older-inbound@example.test>",
            "<older-sent@example.test>",
            "agent@example.test",
        )
        profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "human@example.test", "primary"
        )
        with (
            patch.object(
                omo_email_subject, "configured_agent_mail", return_value=Settings()
            ),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(
                omo_email_subject,
                "fetch_recent_headers",
                return_value=[older_sent, older_inbound, sent, inbound],
            ),
            patch.dict(
                os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False
            ),
        ):
            selected = omo_email_subject.find_recent_thread_for_tmux_target(
                "wl:1",
                profile,
                reject_ambiguous=True,
                required_agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
            )
        self.assertEqual(sent, selected)
        with (
            patch.object(
                omo_email_subject, "configured_agent_mail", return_value=Settings()
            ),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(
                omo_email_subject, "fetch_recent_headers", return_value=[inbound]
            ),
            patch.dict(
                os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False
            ),
        ):
            selected = omo_email_subject.find_recent_thread_for_tmux_target(
                "wl:1",
                profile,
                reject_ambiguous=True,
                required_agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
            )
        self.assertIsNone(selected)

        tied_sent = omo_email_subject.RecentHeader(
            **{**older_sent.__dict__, "date": sent.date}
        )
        with (
            patch.object(
                omo_email_subject, "configured_agent_mail", return_value=Settings()
            ),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(
                omo_email_subject,
                "fetch_recent_headers",
                return_value=[sent, tied_sent],
            ),
            patch.dict(
                os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False
            ),
            self.assertRaisesRegex(omo_email_subject.SubjectInputError, "ambiguous"),
        ):
            omo_email_subject.find_recent_thread_for_tmux_target(
                "wl:1",
                profile,
                reject_ambiguous=True,
                required_agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
            )

        for headers in (
            [omo_email_subject.RecentHeader(**{**sent.__dict__, "date": None})],
        ):
            with (
                self.subTest(headers=headers),
                patch.object(
                    omo_email_subject, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
                patch.object(
                    omo_email_subject, "fetch_recent_headers", return_value=headers
                ),
                patch.dict(
                    os.environ,
                    {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"},
                    clear=False,
                ),
                self.assertRaisesRegex(
                    omo_email_subject.SubjectInputError, "ambiguous timestamp"
                ),
            ):
                omo_email_subject.find_recent_thread_for_tmux_target(
                    "wl:1",
                    profile,
                    reject_ambiguous=True,
                    required_agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
                )

        tied_reply = omo_email_subject.RecentHeader(
            **{**inbound.__dict__, "date": sent.date}
        )
        with (
            patch.object(
                omo_email_subject, "configured_agent_mail", return_value=Settings()
            ),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(
                omo_email_subject,
                "fetch_recent_headers",
                return_value=[sent, tied_reply],
            ),
            patch.dict(
                os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False
            ),
        ):
            selected = omo_email_subject.find_recent_thread_for_tmux_target(
                "wl:1",
                profile,
                reject_ambiguous=True,
                required_agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
            )
        self.assertEqual(sent, selected)

        other_session = omo_email_subject.RecentHeader(
            **{**sent.__dict__, "agent_session": "01a0369c-7895-70f2-ae4b-5f59d920e99b"}
        )
        with (
            patch.object(
                omo_email_subject, "configured_agent_mail", return_value=Settings()
            ),
            patch.object(omo_email_subject.imaplib, "IMAP4_SSL", FakeClient),
            patch.object(
                omo_email_subject,
                "fetch_recent_headers",
                return_value=[other_session, inbound],
            ),
            patch.dict(
                os.environ, {"OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "86400"}, clear=False
            ),
        ):
            selected = omo_email_subject.find_recent_thread_for_tmux_target(
                "wl:1",
                profile,
                reject_ambiguous=True,
                required_agent_session="01a0369c-7895-70f2-ae4b-5f59d920e99a",
            )
        self.assertIsNone(selected)

    def test_verified_thread_selection_rejects_ambiguous_or_missing_identity(
        self,
    ) -> None:
        first = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Topic",
            None,
            "<first@example.test>",
            "",
            "human@example.test",
        )
        second = omo_email_subject.RecentHeader(
            "agent@example.test",
            "Topic",
            None,
            "<second@example.test>",
            "",
            "human@example.test",
        )
        missing = omo_email_subject.RecentHeader(
            "agent@example.test", "Topic", None, "", "", "human@example.test"
        )
        with self.assertRaisesRegex(omo_email_subject.SubjectInputError, "ambiguous"):
            omo_email_subject.select_recent_thread(
                [first, second], reject_ambiguous=True
            )
        with self.assertRaisesRegex(
            omo_email_subject.SubjectInputError, "missing an exact Message-ID"
        ):
            omo_email_subject.select_recent_thread([missing], reject_ambiguous=True)

    def test_verified_reply_rejects_missing_or_failed_route_lookup(self) -> None:
        profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "human@example.test", "primary"
        )
        with patch.object(omo_email_subject, "find_recent_thread", return_value=None):
            with self.assertRaisesRegex(
                omo_email_subject.SubjectInputError, "no exact email thread"
            ):
                omo_email_subject.prepare_subject_and_headers(
                    "Re: Topic", "wl:1", route_profile=profile
                )
        with patch.object(
            omo_email_subject,
            "find_recent_thread",
            side_effect=RuntimeError("imap down"),
        ):
            with self.assertRaisesRegex(
                omo_email_subject.SubjectInputError, "lookup failed.*imap down"
            ):
                omo_email_subject.prepare_subject_and_headers(
                    "Re: Topic", "wl:1", route_profile=profile
                )

    def test_failed_verified_reply_does_not_open_smtp(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(
                email_me,
                "prepare_subject_and_headers",
                side_effect=email_me.SubjectInputError("ambiguous route thread"),
            ),
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(
                [
                    "--manager-human",
                    "--non-completion",
                    "--tmux-target",
                    "wl:1",
                    "--subject",
                    "Re: Topic",
                ]
            )
        self.assertEqual(2, result)
        self.assertIn("ambiguous route thread", stderr.getvalue())
        smtp.assert_not_called()

    def test_explicit_new_route_message_does_not_attach_a_thread(self) -> None:
        profile = omo_email_subject.MailRouteProfile(
            "agent@example.test", "46496337@qq.com", "guest-hees"
        )
        with patch.object(omo_email_subject, "verified_recent_thread_header") as lookup:
            self.assertEqual(
                ("[guest_hees:1] Topic", {}),
                omo_email_subject.prepare_subject_and_headers(
                    "Topic", "guest_hees:1", route_profile=profile
                ),
            )
        lookup.assert_not_called()

    def test_explicit_guest_target_rejects_verified_primary_producer(self) -> None:
        current = subprocess.CompletedProcess(["tmux"], 0, stdout="wl:7.0\n", stderr="")
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX": "/tmp/tmux-session",
                    "TMUX_PANE": "%42",
                    "OMO_AGENT_TMUX_TARGET": "wl:7",
                },
                clear=False,
            ),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me.subprocess, "run", return_value=current),
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(
                [
                    "--guest-hees",
                    "--manager-human",
                    "--tmux-target",
                    "guest_hees:1",
                    "--subject",
                    "Topic",
                ]
            )
        self.assertEqual(2, result)
        self.assertIn("conflicts with verified producer route", stderr.getvalue())
        smtp.assert_not_called()

    def test_explicit_primary_target_rejects_verified_guest_producer(self) -> None:
        current = subprocess.CompletedProcess(
            ["tmux"], 0, stdout="guest_hees:1.0\n", stderr=""
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TMUX": "/tmp/tmux-session",
                    "TMUX_PANE": "%42",
                    "OMO_AGENT_TMUX_TARGET": "guest_hees:1",
                },
                clear=False,
            ),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me.subprocess, "run", return_value=current),
            patch.object(email_me.smtplib, "SMTP_SSL") as smtp,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(
                [
                    "--manager-human",
                    "--non-completion",
                    "--tmux-target",
                    "wl:1",
                    "--subject",
                    "Topic",
                ]
            )
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
            result = email_me.main(
                [
                    "--manager-human",
                    "--non-completion",
                    "--tmux-target",
                    "wl:1",
                    "--subject",
                    "Topic",
                ]
            )
        self.assertEqual(2, result)
        self.assertIn(
            "primary email route must not use the pinned guest recipient",
            stderr.getvalue(),
        )
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
                (
                    b"2",
                    b"From: agent@example.test\nSubject: [wl:1] Second\n"
                    b"X-OMO-Agent-Session-ID: 01a0369c-7895-70f2-ae4b-5f59d920e99a\n\n",
                ),
            ],
        )
        headers = omo_email_subject.fetch_recent_headers(client, ["1", "2"])
        self.assertEqual(
            ["[wl:1] First", "[wl:1] Second"], [header.subject for header in headers]
        )
        self.assertEqual(
            "01a0369c-7895-70f2-ae4b-5f59d920e99a", headers[1].agent_session
        )
        client.uid.assert_called_once_with(
            "fetch",
            "1,2",
            "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID REFERENCES IN-REPLY-TO X-OMO-AGENT-SESSION-ID)])",
        )

    def test_dry_run_can_omit_pwd_footer(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            result = email_me.main(["--dry-run", "--no-pwd-footer", "--subject", "hi"])
        self.assertEqual(0, result)
        self.assertIn("body-bytes=5", stdout.getvalue())

    def test_split_dry_run_omits_pwd_footer_by_default(self) -> None:
        settings = type(
            "Settings",
            (),
            {
                "agent_address": "agent@example.test",
                "human_address": "human@example.test",
                "app_password": "secret",
            },
        )()
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me, "configured_agent_mail", return_value=settings),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            result = email_me.main(["--dry-run", "--subject", "hi"])
        self.assertEqual(0, result)
        self.assertIn("body-bytes=5", stdout.getvalue())

    def test_required_human_recipient_rejects_missing_split_configuration(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me, "configured_agent_mail", return_value=None),
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(
                ["--dry-run", "--require-human-recipient", "--subject", "watcher error"]
            )
        self.assertEqual(2, result)
        self.assertIn("requires separate agent and human mailboxes", stderr.getvalue())

    def test_required_human_recipient_accepts_split_configuration(self) -> None:
        settings = type(
            "Settings",
            (),
            {
                "agent_address": "agent@example.test",
                "human_address": "human@example.test",
                "app_password": "secret",
            },
        )()
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me, "configured_agent_mail", return_value=settings),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            result = email_me.main(
                ["--dry-run", "--require-human-recipient", "--subject", "watcher error"]
            )
        self.assertEqual(0, result)
        self.assertIn("dry-run: email not sent", stdout.getvalue())

    def test_manager_dry_run_rejects_missing_split_configuration(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("body\n")),
            patch.object(email_me, "configured_agent_mail", return_value=None),
            patch.object(email_me, "prepare_subject_and_headers") as prepare,
            patch.object(email_me, "append_pwd_footer") as append_footer,
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(
                [
                    "--dry-run",
                    "--manager-human",
                    "--non-completion",
                    "--no-pwd-footer",
                    "--tmux-target",
                    "wl:1",
                    "--subject",
                    "hi",
                ]
            )
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
            with (
                patch.dict(os.environ, env, clear=False),
                patch("sys.stdout", new_callable=StringIO) as stdout,
            ):
                first = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
                second = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, first)
            self.assertEqual(0, second)
            self.assertIn("Emailed the human", stdout.getvalue())
            self.assertIn("Skipped duplicate human email", stdout.getvalue())
            self.assertEqual(
                "[wl:1] Manager update\nbody\n", send_log.read_text(encoding="utf-8")
            )
            self.assertIn(
                "[wl:1] Manager update",
                (state_dir / "human-email-sent.tsv").read_text(encoding="utf-8"),
            )

    def test_manager_human_non_completion_claim_does_not_expire_across_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("one news report\n", encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "0",
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            argv = [
                "--manager-human",
                "--non-completion",
                "--subject",
                "News",
                "--message-file",
                str(body),
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch("sys.stdout", new_callable=StringIO) as stdout,
            ):
                self.assertEqual(0, email_me.main(argv))
                with patch.object(
                    email_me.time,
                    "time",
                    return_value=email_me.time.time() + 10_000_000,
                ):
                    self.assertEqual(0, email_me.main(argv))

            self.assertEqual(
                "[wl:1] News\none news report\n", send_log.read_text(encoding="utf-8")
            )
            self.assertIn("Skipped duplicate human email", stdout.getvalue())
            claims = list((state_dir / "human-email-exact-once").glob("*.claim"))
            self.assertEqual(1, len(claims))

    def test_manager_human_uncertain_smtp_retry_never_resubmits(self) -> None:
        sent: list[object] = []

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

            def send_message(self, message: object) -> None:
                sent.append(message)
                raise email_me.smtplib.SMTPException("uncertain after submit")

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            env = {
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            argv = [
                "--manager-human",
                "--non-completion",
                "--tmux-target",
                "wl:1",
                "--subject",
                "News",
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(sys, "stdin", StringIO("one uncertain report\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("[wl:1] News", {}),
                ),
                patch.object(email_me.smtplib, "SMTP_SSL", UncertainSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch("sys.stderr", new_callable=StringIO),
            ):
                self.assertEqual(1, email_me.main(argv))
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(sys, "stdin", StringIO("one uncertain report\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("[wl:1] News", {}),
                ),
                patch.object(email_me.smtplib, "SMTP_SSL", UncertainSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
            ):
                self.assertEqual(1, email_me.main(argv))
            self.assertEqual(1, len(sent))

    def test_manager_human_post_submit_authentication_error_never_resubmits(
        self,
    ) -> None:
        sent: list[object] = []

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        class UncertainAuthSmtp:
            def __init__(self, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> "UncertainAuthSmtp":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def login(self, _sender: str, _password: str) -> None:
                return None

            def send_message(self, message: object) -> None:
                sent.append(message)
                raise email_me.smtplib.SMTPAuthenticationError(
                    535, b"uncertain after submit"
                )

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            argv = [
                "--manager-human",
                "--non-completion",
                "--tmux-target",
                "wl:1",
                "--subject",
                "News",
            ]
            for _attempt in range(2):
                with (
                    patch.dict(os.environ, env, clear=False),
                    patch.object(sys, "stdin", StringIO("one uncertain report\n")),
                    patch.object(
                        email_me, "configured_agent_mail", return_value=Settings()
                    ),
                    patch.object(
                        email_me,
                        "prepare_subject_and_headers",
                        return_value=("[wl:1] News", {}),
                    ),
                    patch.object(email_me.smtplib, "SMTP_SSL", UncertainAuthSmtp),
                    patch.object(
                        email_me.ssl, "create_default_context", return_value=None
                    ),
                    patch("sys.stderr", new_callable=StringIO),
                ):
                    self.assertEqual(1, email_me.main(argv))
            self.assertEqual(1, len(sent))

    def test_manager_human_exact_once_rejects_orphan_delivery_artifacts(self) -> None:
        for artifact_kind in ("regular", "dangling-symlink"):
            with (
                self.subTest(artifact_kind=artifact_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                state = Path(tmp) / "state"
                claims = state / "human-email-exact-once"
                claims.mkdir(mode=0o700, parents=True)
                state.chmod(0o700)
                digest, payload = email_me.exact_once_email_record(
                    "News", "[wl:1] News", "body\0", "wl:1"
                )
                delivered = claims / f"{digest}.delivered"
                if artifact_kind == "regular":
                    delivered.write_bytes(payload)
                    delivered.chmod(0o600)
                else:
                    delivered.symlink_to(claims / "missing")
                with patch.dict(
                    os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False
                ):
                    with self.assertRaisesRegex(ValueError, "orphan terminal artifact"):
                        email_me.exact_once_email_claim(
                            "News", "[wl:1] News", "body\0", "wl:1"
                        )
                self.assertFalse((claims / f"{digest}.claim").exists())

    def test_manager_human_exact_once_rejects_ambiguous_receipt_state(self) -> None:
        for artifact_kind in (
            "mismatched-claim",
            "release",
            "different-inode-delivered",
        ):
            with (
                self.subTest(artifact_kind=artifact_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                state = Path(tmp) / "state"
                claims = state / "human-email-exact-once"
                claims.mkdir(mode=0o700, parents=True)
                state.chmod(0o700)
                digest, payload = email_me.exact_once_email_record(
                    "News", "[wl:1] News", "body\0", "wl:1"
                )
                claim = claims / f"{digest}.claim"
                claim.write_bytes(
                    b"wrong\n" if artifact_kind == "mismatched-claim" else payload
                )
                claim.chmod(0o600)
                if artifact_kind == "release":
                    os.link(claim, claim.with_suffix(".release"))
                elif artifact_kind == "different-inode-delivered":
                    delivered = claim.with_suffix(".delivered")
                    delivered.write_bytes(payload)
                    delivered.chmod(0o600)
                with patch.dict(
                    os.environ, {"OMO_MANAGER_STATE_DIR": str(state)}, clear=False
                ):
                    with self.assertRaisesRegex(
                        ValueError, "conflicts|incomplete release"
                    ):
                        email_me.exact_once_email_claim(
                            "News", "[wl:1] News", "body\0", "wl:1"
                        )

    def test_manager_human_exact_once_rejects_subject_target_mismatch(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        with tempfile.TemporaryDirectory() as tmp:
            send_log = Path(tmp) / "sent.txt"
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state"),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(sys, "stdin", StringIO("one report\n")),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("[pb:13] News", {}),
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(
                    2,
                    email_me.main(
                        [
                            "--manager-human",
                            "--non-completion",
                            "--tmux-target",
                            "wl:1",
                            "--subject",
                            "News",
                        ]
                    ),
                )
            self.assertIn(
                "does not match its authenticated producer", stderr.getvalue()
            )
            self.assertFalse(send_log.exists())

    def test_manager_human_exact_once_binds_producer_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            body = Path(tmp) / "body.md"
            body.write_text("same report\n", encoding="utf-8")
            for target in ("wl:1", "pb:13"):
                send_log = Path(tmp) / f"{target.replace(':', '-')}.txt"
                env = {
                    "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                    "OMO_MANAGER_STATE_DIR": str(state),
                    "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                }
                with patch.dict(os.environ, env, clear=False):
                    self.assertEqual(
                        0,
                        email_me.main(
                            [
                                "--manager-human",
                                "--non-completion",
                                "--tmux-target",
                                target,
                                "--subject",
                                "News",
                                "--message-file",
                                str(body),
                            ]
                        ),
                    )
                self.assertTrue(send_log.exists())
            self.assertEqual(
                2, len(list((state / "human-email-exact-once").glob("*.delivered")))
            )

    def test_manager_human_exact_once_state_failure_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "blocked-state"
            state_path.write_text("not a directory\n", encoding="utf-8")
            send_log = Path(tmp) / "sent.txt"
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_path),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(sys, "stdin", StringIO("one news report\n")),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(
                    2,
                    email_me.main(
                        ["--manager-human", "--non-completion", "--subject", "News"]
                    ),
                )
            self.assertIn("exact-once state is unavailable", stderr.getvalue())
            self.assertFalse(send_log.exists())

    def test_manager_human_exact_once_claim_releases_before_smtp_attempt(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"
            app_password = "secret"

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("one news report\n", encoding="utf-8")
            env = {
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            argv = [
                "--manager-human",
                "--non-completion",
                "--tmux-target",
                "wl:1",
                "--subject",
                "News",
                "--message-file",
                str(body),
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("[wl:1] News", {}),
                ),
                patch.object(
                    email_me.smtplib,
                    "SMTP_SSL",
                    side_effect=OSError("connection refused"),
                ),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch("sys.stderr", new_callable=StringIO),
            ):
                self.assertEqual(1, email_me.main(argv))
            self.assertEqual(
                [], list((state_dir / "human-email-exact-once").glob("*.claim"))
            )
            with patch.dict(
                os.environ,
                {**env, "EMAIL_ME_FAKE_SEND_LOG": str(send_log)},
                clear=False,
            ):
                self.assertEqual(0, email_me.main(argv))
            self.assertTrue(send_log.exists())

    def test_completion_mail_requires_exact_owner_claim_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            authorization_dir = state_dir / "completion-email-authorizations"
            state_dir.mkdir(mode=0o700)
            authorization_dir.mkdir(mode=0o700)
            key = "a" * 64
            notice_key = "b" * 64
            semantic_key = "d" * 64
            subject = "task.md: task done"
            body = "Task: task.md\nOutcome: task done\n"
            root = Path(tmp) / "work_logs"
            root.mkdir()
            task = root / "task.md"
            task.write_text("task bytes\n", encoding="utf-8")
            task_sha256 = hashlib.sha256(task.read_bytes()).hexdigest()
            authorization = authorization_dir / key
            authorization.write_text(
                "version=1\n"
                "target=wl:1\n"
                f"root={root}\n"
                "task=task.md\n"
                f"task_sha256={task_sha256}\n"
                f"notice_key={notice_key}\n"
                f"semantic_key={semantic_key}\n"
                f"subject_sha256={hashlib.sha256(subject.encode()).hexdigest()}\n"
                f"body_sha256={hashlib.sha256(body.encode()).hexdigest()}\n",
                encoding="utf-8",
            )
            authorization.chmod(0o600)
            claims = state_dir / "completion-email-claims.tsv"
            claims.write_text(
                f"{key}\twl:1\ttask.md\twl:0\t{task_sha256}\t{notice_key}\t{semantic_key}\n",
                encoding="utf-8",
            )
            claims.chmod(0o600)
            message = Path(tmp) / "body.md"
            message.write_text(body, encoding="utf-8")
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(Path(tmp) / "sent.txt"),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1",
            }
            argv = [
                "--manager-human",
                "--completion-authorization",
                key,
                "--tmux-target",
                "wl:1",
                "--subject",
                subject,
                "--message-file",
                str(message),
            ]
            self.completion_caller_patch.stop()
            try:
                with (
                    patch.dict(os.environ, {**env, "TMUX_PANE": "%2"}, clear=False),
                    patch.object(email_me, "current_tmux_window", return_value="wl:2"),
                    patch("sys.stderr", new_callable=StringIO) as stderr,
                ):
                    self.assertEqual(2, email_me.main(["--dry-run", *argv]))
                self.assertIn(
                    "target does not match the invoking pane", stderr.getvalue()
                )
                active = root / "replacement.md"
                active.write_text(
                    "---\nversion: v1.0.0\nstatus: running\nrunat: wl:1\ntool: codex\nmanagerat: wl:0\n"
                    "is_manager: false\npending_task_items: []\n---\n",
                    encoding="utf-8",
                )
                (root / "TODO.md").write_text(
                    "current:\nreplacement.md wl:1\n\nlow priority:\n\nhuman pending:\n\nprevious:\n",
                    encoding="utf-8",
                )
                with (
                    patch.dict(os.environ, {**env, "TMUX_PANE": "%1"}, clear=False),
                    patch.object(email_me, "current_tmux_window", return_value="wl:1"),
                    patch.object(
                        email_me, "invoking_process_belongs_to_pane", return_value=True
                    ),
                    patch("sys.stderr", new_callable=StringIO) as stderr,
                ):
                    self.assertEqual(2, email_me.main(["--dry-run", *argv]))
                self.assertIn("different active task", stderr.getvalue())
            finally:
                self.completion_caller_patch.start()
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(0, email_me.main(["--dry-run", *argv]))
                self.assertFalse(
                    (state_dir / "completion-email-authorization-used" / key).exists()
                )
                with (
                    patch.object(
                        email_me, "should_send_manager_email_key"
                    ) as generic_dedupe,
                    patch.object(
                        email_me, "fsync_directory", wraps=email_me.fsync_directory
                    ) as fsync_dir,
                ):
                    self.assertEqual(0, email_me.main(argv))
                generic_dedupe.assert_not_called()
                fsync_dir.assert_called_once_with(
                    state_dir / "completion-email-authorization-used"
                )
            message.write_text(body + "changed\n", encoding="utf-8")
            with (
                patch.dict(os.environ, env, clear=False),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(2, email_me.main(argv))
            self.assertIn("content does not match", stderr.getvalue())

    def test_manager_completion_like_route_rejects_unclassified_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("done\n", encoding="utf-8")
            env = {"OMO_MANAGER_TMUX_TARGET": "wl:1"}
            with (
                patch.dict(os.environ, env, clear=False),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(
                    2,
                    email_me.main(
                        [
                            "--manager-human",
                            "--subject",
                            "task done",
                            "--message-file",
                            str(body),
                        ]
                    ),
                )
            self.assertIn("requires --non-completion", stderr.getvalue())

    def test_worker_cannot_relabel_completion_as_privileged_non_completion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("All assigned work is complete.\n", encoding="utf-8")
            env = {"OMO_MANAGER_TMUX_TARGET": "wl:1"}
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me,
                    "validate_non_completion_owner",
                    side_effect=ValueError(
                        "non-completion Human mail requires an exact active manager owner"
                    ),
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(
                    2,
                    email_me.main(
                        [
                            "--manager-human",
                            "--non-completion",
                            "--subject",
                            "operational",
                            "--message-file",
                            str(body),
                        ]
                    ),
                )
            self.assertIn("exact active manager owner", stderr.getvalue())

    def test_manager_cannot_mail_on_worker_lifecycle_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("The lifecycle request was reviewed.\n", encoding="utf-8")
            guard, payload = omo_email_subject.worker_lifecycle_report_guard(
                Path(self.state_tmp.name),
                "wl:1",
                "<lifecycle@example.test>",
            )
            guard.parent.mkdir(mode=0o700)
            guard.write_bytes(payload)
            guard.chmod(0o600)
            with (
                patch.object(
                    email_me, "validate_non_completion_owner", return_value=True
                ),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=(
                        "[wl:1] Lifecycle final result",
                        {
                            "In-Reply-To": "<lifecycle@example.test>",
                            "References": "<earlier@example.test> <lifecycle@example.test>",
                        },
                    ),
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--tmux-target",
                        "wl:1",
                        "--subject",
                        "Lifecycle final result",
                        "--message-file",
                        str(body),
                    ],
                )
        self.assertEqual(2, result)
        self.assertIn("disabled on this worker lifecycle thread", stderr.getvalue())

    def test_worker_cannot_bypass_lifecycle_completion_authorization(self) -> None:
        guard, payload = omo_email_subject.worker_lifecycle_report_guard(
            Path(self.state_tmp.name),
            "worker:2",
            "<lifecycle@example.test>",
        )
        guard.parent.mkdir(mode=0o700)
        guard.write_bytes(payload)
        guard.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "owner completion authorization"):
            email_me.validate_non_completion_thread(
                "worker:2",
                {
                    "In-Reply-To": "<lifecycle@example.test>",
                    "References": "<lifecycle@example.test>",
                },
            )

    def test_same_subject_different_thread_remains_available(self) -> None:
        guard, payload = omo_email_subject.worker_lifecycle_report_guard(
            Path(self.state_tmp.name),
            "wl:1",
            "<first-thread@example.test>",
        )
        guard.parent.mkdir(mode=0o700)
        guard.write_bytes(payload)
        guard.chmod(0o600)
        email_me.validate_non_completion_thread(
            "wl:1",
            {
                "In-Reply-To": "<second-thread@example.test>",
                "References": "<second-thread@example.test>",
            },
        )

    def test_manager_cannot_start_fresh_non_completion_result_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("The worker result is ready.\n", encoding="utf-8")
            with (
                patch.object(
                    email_me, "validate_non_completion_owner", return_value=True
                ),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("[wl:1] Worker result", {}),
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--tmux-target",
                        "wl:1",
                        "--subject",
                        "Worker result",
                        "--message-file",
                        str(body),
                    ],
                )
        self.assertEqual(2, result)
        self.assertIn(
            "must reply to one verified Human email thread", stderr.getvalue()
        )

    def test_manager_operational_reply_allows_only_acknowledgment_or_question(
        self,
    ) -> None:
        headers = {
            "In-Reply-To": "<human@example.test>",
            "References": "<earlier@example.test> <human@example.test>",
        }
        email_me.validate_manager_operational_reply(
            headers, "Acknowledged: I accepted this request.\n"
        )
        email_me.validate_manager_operational_reply(
            headers, "pending item created:\n- first\n- second\n"
        )
        email_me.validate_manager_operational_reply(
            headers, "Question: Which worker should own this?\n"
        )
        with self.assertRaisesRegex(ValueError, "responsible worker reports"):
            email_me.validate_manager_operational_reply(
                headers, "The worker result is complete.\n"
            )
        for bypass in (
            "Acknowledged: I accepted this request.\nThe worker result is complete.\n",
            "Acknowledged: The worker result is complete.\n",
            "Digest: non-urgent queued items\n",
            "Question: Which worker owns this?\nThe result is complete.\n",
            "Acknowledged: I accepted this request.\n> The worker result is complete.\n",
        ):
            with (
                self.subTest(bypass=bypass),
                self.assertRaisesRegex(ValueError, "responsible worker reports"),
            ):
                email_me.validate_manager_operational_reply(headers, bypass)

    def test_digest_authorization_binds_exact_content_and_is_one_use(self) -> None:
        state = Path(self.state_tmp.name)
        state.chmod(0o700)
        authorization_dir = state / "manager-digest-authorizations"
        authorization_dir.mkdir(mode=0o700)
        subject = "Non-urgent news digest"
        body = "Non-urgent digest items queued for afternoon/evening idle delivery:\n"
        key = "a" * 64
        path = omo_email_subject.manager_digest_authorization_path(state, key)
        payload = omo_email_subject.manager_digest_authorization_payload(subject, body)
        path.write_bytes(payload)
        path.chmod(0o600)
        with patch.object(sys, "stdin", StringIO(body)):
            args = email_me.parse_args(
                [
                    "--manager-human",
                    "--digest-authorization",
                    key,
                    "--tmux-target",
                    "wl:1",
                    "--subject",
                    subject,
                ]
            )

        authorization = email_me.validate_digest_authorization(args)
        email_me.consume_digest_authorization(*authorization)

        self.assertFalse(path.exists())
        with self.assertRaisesRegex(ValueError, "missing or malformed"):
            email_me.validate_digest_authorization(args)

    def test_digest_authorization_rejects_changed_body(self) -> None:
        state = Path(self.state_tmp.name)
        state.chmod(0o700)
        authorization_dir = state / "manager-digest-authorizations"
        authorization_dir.mkdir(mode=0o700)
        subject = "Non-urgent news digest"
        key = "b" * 64
        path = omo_email_subject.manager_digest_authorization_path(state, key)
        path.write_bytes(
            omo_email_subject.manager_digest_authorization_payload(
                subject, "authorized\n"
            )
        )
        path.chmod(0o600)
        with patch.object(sys, "stdin", StringIO("changed\n")):
            args = email_me.parse_args(
                [
                    "--manager-human",
                    "--digest-authorization",
                    key,
                    "--tmux-target",
                    "wl:1",
                    "--subject",
                    subject,
                ]
            )

        with self.assertRaisesRegex(ValueError, "does not match"):
            email_me.validate_digest_authorization(args)

    def test_digest_authorization_rejects_task_reply_subject(self) -> None:
        state = Path(self.state_tmp.name)
        state.chmod(0o700)
        authorization_dir = state / "manager-digest-authorizations"
        authorization_dir.mkdir(mode=0o700)
        subject = "Re: Worker result"
        body = "Non-urgent digest items queued for afternoon/evening idle delivery:\n"
        key = "c" * 64
        path = omo_email_subject.manager_digest_authorization_path(state, key)
        path.write_bytes(
            omo_email_subject.manager_digest_authorization_payload(subject, body)
        )
        path.chmod(0o600)
        with patch.object(sys, "stdin", StringIO(body)):
            args = email_me.parse_args(
                [
                    "--manager-human",
                    "--digest-authorization",
                    key,
                    "--tmux-target",
                    "wl:1",
                    "--subject",
                    subject,
                ]
            )

        with self.assertRaisesRegex(ValueError, "fresh subject"):
            email_me.validate_digest_authorization(args)

    def test_digest_authorization_sends_through_exact_once_boundary(self) -> None:
        state = Path(self.state_tmp.name)
        state.chmod(0o700)
        authorization_dir = state / "manager-digest-authorizations"
        authorization_dir.mkdir(mode=0o700)
        subject = "Non-urgent news digest"
        body_text = (
            "Non-urgent digest items queued for afternoon/evening idle delivery:\n"
        )
        key = "d" * 64
        authorization = omo_email_subject.manager_digest_authorization_path(state, key)
        authorization.write_bytes(
            omo_email_subject.manager_digest_authorization_payload(subject, body_text)
        )
        authorization.chmod(0o600)
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            sent = Path(tmp) / "sent.txt"
            body.write_text(body_text, encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "EMAIL_ME_FAKE_SEND_LOG": str(sent),
                    "OMO_AGENT_TMUX_TARGET": "wl:1",
                    "OMO_MANAGER_STATE_DIR": str(state),
                },
                clear=False,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--digest-authorization",
                        key,
                        "--subject",
                        subject,
                        "--message-file",
                        str(body),
                    ]
                )

            self.assertEqual(0, result)
            self.assertFalse(authorization.exists())
            self.assertTrue(
                sent.read_text(encoding="utf-8").startswith(
                    "[wl:1] Non-urgent news digest\n"
                )
            )
            self.assertIn(body_text, sent.read_text(encoding="utf-8"))

    def test_fresh_digest_subject_ignores_same_subject_mailbox_history(self) -> None:
        with patch.object(
            omo_email_subject, "has_recent_thread", return_value=True
        ) as lookup:
            subject = omo_email_subject.fresh_manager_subject(
                "Non-urgent news digest", "wl:1"
            )

        self.assertEqual("[wl:1] Non-urgent news digest", subject)
        lookup.assert_not_called()

    def test_non_completion_owner_binding_accepts_active_manager_and_worker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            task.write_text(
                "---\nversion: v1.0.0\nstatus: running\nrunat: wl:1\ntool: codex\nmanagerat: main:0\n"
                "is_manager: true\npending_task_items: []\n---\n",
                encoding="utf-8",
            )
            (root / "TODO.md").write_text(
                "current:\nmanager.md wl:1\n\nlow priority:\n\nhuman pending:\n\nprevious:\n",
                encoding="utf-8",
            )
            self.non_completion_caller_patch.stop()
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"OMO_WORK_LOGS_ROOT": str(root), "TMUX_PANE": "%1"},
                        clear=False,
                    ),
                    patch.object(email_me, "current_tmux_window", return_value="wl:1"),
                    patch.object(
                        email_me, "invoking_process_belongs_to_pane", return_value=True
                    ),
                ):
                    email_me.validate_non_completion_owner("wl:1")
                    with self.assertRaisesRegex(ValueError, "does not match"):
                        email_me.validate_non_completion_owner("wl:2")
                    task.write_text(
                        task.read_text(encoding="utf-8").replace(
                            "is_manager: true", "is_manager: false"
                        ),
                        encoding="utf-8",
                    )
                    email_me.validate_non_completion_owner("wl:1")
            finally:
                self.non_completion_caller_patch.start()

    def test_non_completion_owner_binding_uses_runnable_queue_over_blocked_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            running = root / "running.md"
            blocked = root / "blocked.md"
            running.write_text(
                "---\nversion: v1.0.0\nstatus: running\nrunat: wl:1\ntool: codex\nmanagerat: main:0\n"
                "is_manager: false\npending_task_items: []\n---\n",
                encoding="utf-8",
            )
            blocked.write_text(
                "---\nversion: v1.0.0\nstatus: blocked\nrunat: wl:1\ntool: codex\nmanagerat: main:0\n"
                "is_manager: false\npending_task_items: []\n---\n",
                encoding="utf-8",
            )
            (root / "TODO.md").write_text(
                "current:\nrunning.md wl:1\nblocked.md wl:1\n\nlow priority:\n\nhuman pending:\n\nprevious:\n",
                encoding="utf-8",
            )
            self.non_completion_caller_patch.stop()
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"OMO_WORK_LOGS_ROOT": str(root), "TMUX_PANE": "%1"},
                        clear=False,
                    ),
                    patch.object(email_me, "current_tmux_window", return_value="wl:1"),
                    patch.object(
                        email_me, "invoking_process_belongs_to_pane", return_value=True
                    ),
                ):
                    email_me.validate_non_completion_owner("wl:1")
            finally:
                self.non_completion_caller_patch.start()

    def test_non_completion_owner_binding_uses_configured_work_log_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "worker.md"
            task.write_text(
                "---\nversion: v1.0.0\nstatus: running\nrunat: wl:1\ntool: codex\nmanagerat: main:0\n"
                "is_manager: false\npending_task_items: []\n---\n",
                encoding="utf-8",
            )
            (root / "TODO.md").write_text(
                "current:\nworker.md wl:1\n\nlow priority:\n\nhuman pending:\n\nprevious:\n",
                encoding="utf-8",
            )
            self.non_completion_caller_patch.stop()
            try:
                with (
                    patch.dict(os.environ, {"TMUX_PANE": "%1"}, clear=True),
                    patch("omo_agent_status.DEFAULT_ROOT", root),
                    patch.object(email_me, "current_tmux_window", return_value="wl:1"),
                    patch.object(
                        email_me, "invoking_process_belongs_to_pane", return_value=True
                    ),
                ):
                    email_me.validate_non_completion_owner("wl:1")
            finally:
                self.non_completion_caller_patch.start()

    def test_non_completion_owner_binding_rejects_inactive_or_ownerless_worker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "worker.md"
            active_worker = (
                "---\nversion: v1.0.0\nstatus: running\nrunat: wl:1\ntool: codex\nmanagerat: main:0\n"
                "is_manager: false\npending_task_items: []\n---\n"
            )
            task.write_text(active_worker, encoding="utf-8")
            (root / "TODO.md").write_text(
                "current:\nworker.md wl:1\n\nlow priority:\n\nhuman pending:\n\nprevious:\n",
                encoding="utf-8",
            )
            self.non_completion_caller_patch.stop()
            try:
                with (
                    patch.dict(
                        os.environ,
                        {"OMO_WORK_LOGS_ROOT": str(root), "TMUX_PANE": "%1"},
                        clear=False,
                    ),
                    patch.object(email_me, "current_tmux_window", return_value="wl:1"),
                    patch.object(
                        email_me, "invoking_process_belongs_to_pane", return_value=True
                    ),
                ):
                    task.write_text(
                        active_worker.replace("status: running", "status: done"),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "exact active task owner"):
                        email_me.validate_non_completion_owner("wl:1")
                    task.write_text(
                        active_worker.replace("managerat: main:0", "managerat: "),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "exact active task owner"):
                        email_me.validate_non_completion_owner("wl:1")
            finally:
                self.non_completion_caller_patch.start()

    def test_invoking_process_must_descend_from_tmux_pane_shell(self) -> None:
        process = Mock(returncode=0, stdout="4242\n")
        with (
            patch.object(email_me.subprocess, "run", return_value=process),
            patch.object(
                email_me, "process_ancestor_pids", return_value={1, 4242, os.getpid()}
            ),
        ):
            self.assertTrue(email_me.invoking_process_belongs_to_pane("%1"))
        with (
            patch.object(email_me.subprocess, "run", return_value=process),
            patch.object(
                email_me, "process_ancestor_pids", return_value={1, os.getpid()}
            ),
        ):
            self.assertFalse(email_me.invoking_process_belongs_to_pane("%1"))

    def test_ordinary_verified_direct_human_send_agrees_between_dry_run_and_live(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text(
                "The same manager directive was delivered twice.\n", encoding="utf-8"
            )
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_DEDUPE_S": "300",
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            argv = [
                "--subject",
                "Duplicate manager directive",
                "--message-file",
                str(body),
            ]
            with patch.dict(os.environ, env, clear=False):
                dry_run = email_me.main(["--dry-run", *argv])
                live = email_me.main(argv)

            self.assertEqual(0, dry_run)
            self.assertEqual(0, live)
            self.assertEqual(
                "Duplicate manager directive\nThe same manager directive was delivered twice.\n",
                send_log.read_text(encoding="utf-8"),
            )

    def test_ordinary_human_mode_does_not_claim_before_credential_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("same report\n", encoding="utf-8")
            env = {
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            argv = [
                "--subject",
                "Duplicate manager directive",
                "--message-file",
                str(body),
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(email_me, "configured_agent_mail", return_value=None),
                patch.object(email_me, "parse_env_file", return_value={}),
                patch("sys.stderr", new_callable=StringIO),
            ):
                first = email_me.main(argv)
            with patch.dict(
                os.environ,
                {**env, "EMAIL_ME_FAKE_SEND_LOG": str(send_log)},
                clear=False,
            ):
                second = email_me.main(argv)

            self.assertEqual(2, first)
            self.assertEqual(0, second)
            self.assertEqual(
                "Duplicate manager directive\nsame report\n",
                send_log.read_text(encoding="utf-8"),
            )

    def test_ordinary_human_mode_releases_claim_after_smtp_connect_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("same report\n", encoding="utf-8")
            settings = type(
                "Settings",
                (),
                {
                    "agent_address": "agent@example.test",
                    "human_address": "human@example.test",
                    "app_password": "secret",
                },
            )()
            env = {
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            argv = [
                "--subject",
                "Duplicate manager directive",
                "--message-file",
                str(body),
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(email_me, "configured_agent_mail", return_value=settings),
                patch.object(
                    email_me.smtplib,
                    "SMTP_SSL",
                    side_effect=OSError("connection refused"),
                ),
                patch("sys.stderr", new_callable=StringIO),
            ):
                first = email_me.main(argv)
            with patch.dict(
                os.environ,
                {**env, "EMAIL_ME_FAKE_SEND_LOG": str(send_log)},
                clear=False,
            ):
                second = email_me.main(argv)

            self.assertEqual(1, first)
            self.assertEqual(0, second)
            self.assertEqual(
                "Duplicate manager directive\nsame report\n",
                send_log.read_text(encoding="utf-8"),
            )

    def test_completion_authorization_is_recoverable_before_smtp_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(mode=0o700)
            body = Path(tmp) / "body.md"
            body.write_text("done\n", encoding="utf-8")
            key = "a" * 64
            values = {
                "target": "wl:1",
                "task": "task.md",
                "task_sha256": "b" * 64,
                "notice_key": "c" * 64,
                "semantic_key": "d" * 64,
            }
            claims = state_dir / "completion-email-claims.tsv"
            claims.write_text(
                f"{key}\twl:1\ttask.md\twl:0\t{values['task_sha256']}\t{values['notice_key']}\t{values['semantic_key']}\n",
                encoding="utf-8",
            )
            claims.chmod(0o600)
            settings = type(
                "Settings",
                (),
                {
                    "agent_address": "agent@example.test",
                    "human_address": "human@example.test",
                    "app_password": "secret",
                },
            )()
            env = {
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
            }
            argv = [
                "--manager-human",
                "--completion-authorization",
                key,
                "--tmux-target",
                "wl:1",
                "--subject",
                "task done",
                "--message-file",
                str(body),
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me, "validate_completion_authorization", return_value=values
                ),
                patch.object(email_me, "configured_agent_mail", return_value=settings),
                patch.object(
                    email_me.smtplib,
                    "SMTP_SSL",
                    side_effect=OSError("connection refused"),
                ),
                patch("sys.stderr", new_callable=StringIO),
            ):
                self.assertEqual(1, email_me.main(argv))
            self.assertFalse(
                (state_dir / "completion-email-authorization-used" / key).exists()
            )

            send_log = Path(tmp) / "sent.txt"
            with (
                patch.dict(
                    os.environ,
                    {**env, "EMAIL_ME_FAKE_SEND_LOG": str(send_log)},
                    clear=False,
                ),
                patch.object(
                    email_me, "validate_completion_authorization", return_value=values
                ),
            ):
                self.assertEqual(0, email_me.main(argv))
            self.assertTrue(
                (state_dir / "completion-email-authorization-used" / key).is_file()
            )

    def test_source2048_thread_recovery_requires_exact_authorization(self) -> None:
        values = {
            "target": email_me.SOURCE2048_RECOVERY_OWNER,
            "task": email_me.SOURCE2048_RECOVERY_TASK,
            "semantic_key": email_me.SOURCE2048_RECOVERY_KEY,
            "subject_sha256": email_me.SOURCE2048_RECOVERY_SUBJECT_SHA256,
            "body_sha256": email_me.SOURCE2048_RECOVERY_BODY_SHA256,
        }
        with patch("sys.stdin", StringIO("done\n")):
            args = email_me.parse_args(
                [
                    "--manager-human",
                    "--completion-authorization",
                    "a" * 64,
                    "--preserve-source2048-thread",
                    "--subject",
                    "Re: Try Pangram for hard data",
                ]
            )
        email_me.validate_source2048_thread_recovery(
            args, values, email_me.SOURCE2048_RECOVERY_OWNER
        )
        for changed in (
            {**values, "semantic_key": "0" * 64},
            {**values, "target": "dw:15"},
            {**values, "body_sha256": "0" * 64},
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(ValueError, "does not match"),
            ):
                email_me.validate_source2048_thread_recovery(
                    args, changed, email_me.SOURCE2048_RECOVERY_OWNER
                )
        settings = type(
            "Settings",
            (),
            {
                "agent_address": "agent@example.test",
                "human_address": "human@example.test",
                "app_password": "secret",
            },
        )()
        for extra in ((), ("--preserve-source2048-thread",)):
            with (
                self.subTest(extra=extra),
                patch("sys.stdin", StringIO("done\n")),
                patch.object(email_me, "configured_agent_mail", return_value=settings),
                patch.object(email_me, "validate_manager_route_identity"),
                patch.object(
                    email_me,
                    "validate_completion_authorization",
                    return_value=values,
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--completion-authorization",
                        "a" * 64,
                        "--tmux-target",
                        email_me.SOURCE2048_RECOVERY_OWNER,
                        "--subject",
                        "Re: Try Pangram for hard data",
                        *extra,
                    ]
                )
            self.assertEqual(2, result)
            self.assertIn("requires preserved-thread recovery", stderr.getvalue())
        tampered = {**values, "target": "config:2"}
        with (
            patch("sys.stdin", StringIO("done\n")),
            patch.object(email_me, "configured_agent_mail", return_value=settings),
            patch.object(email_me, "validate_manager_route_identity"),
            patch.object(
                email_me,
                "validate_completion_authorization",
                return_value=tampered,
            ),
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(
                [
                    "--manager-human",
                    "--completion-authorization",
                    "a" * 64,
                    "--tmux-target",
                    email_me.SOURCE2048_RECOVERY_OWNER,
                    "--subject",
                    "Re: Try Pangram for hard data",
                ]
            )
        self.assertEqual(2, result)
        self.assertIn("requires preserved-thread recovery", stderr.getvalue())

    def test_superseded_completion_authorization_cannot_cross_outbound_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(mode=0o700)
            key = "a" * 64
            values = {
                "target": "wl:1",
                "task": "task.md",
                "task_sha256": "b" * 64,
                "notice_key": "c" * 64,
                "semantic_key": "d" * 64,
            }
            claims = state_dir / "completion-email-claims.tsv"
            claims.write_text(
                f"{'e' * 64}\twl:1\ttask.md\twl:0\t{'f' * 64}\t{values['notice_key']}\t{values['semantic_key']}\n",
                encoding="utf-8",
            )
            claims.chmod(0o600)

            with (
                patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": str(state_dir)}),
                self.assertRaisesRegex(ValueError, "no current durable claim"),
            ):
                email_me.consume_completion_authorization(key, values)

            self.assertFalse(
                (state_dir / "completion-email-authorization-used" / key).exists()
            )

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
            with (
                patch.dict(os.environ, env, clear=False),
                patch.dict(os.environ, {"TMUX": ""}, clear=False),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(
                    2,
                    email_me.main(
                        [
                            "--manager-human",
                            "--non-completion",
                            "--subject-file",
                            str(subject),
                            "--message-file",
                            str(body),
                        ]
                    ),
                )
            self.assertIn("requires a tmux target", stderr.getvalue())

    def test_manager_human_mode_reuses_prepared_thread_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            send_log = Path(tmp) / "sent.txt"
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text(
                "manager_status_email_unification_followup_7872.md status answer\n",
                encoding="utf-8",
            )
            env = {
                "EMAIL_ME_FAKE_SEND_LOG": str(send_log),
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            prepared = (
                "Re: [wl:1] manager_status_email_unification_followup_7872.md status answer",
                {
                    "In-Reply-To": "<prior@example.test>",
                    "References": "<prior@example.test>",
                },
            )
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ) as prepare,
                patch.object(email_me, "reply_headers_for_subject") as headers,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(
                (
                    "manager_status_email_unification_followup_7872.md status answer",
                    "wl:1",
                ),
                prepare.call_args.args,
            )
            self.assertEqual(
                "primary", prepare.call_args.kwargs["route_profile"].route_kind
            )
            headers.assert_not_called()
            self.assertEqual(
                "Re: [wl:1] manager_status_email_unification_followup_7872.md status answer\nbody\n",
                send_log.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Re: [wl:1] manager_status_email_unification_followup_7872.md status answer",
                (state_dir / "human-email-sent.tsv").read_text(encoding="utf-8"),
            )

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
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ) as prepare,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--tmux-target",
                        "wl:7",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(("Topic", "wl:7"), prepare.call_args.args)
            self.assertEqual(
                "primary", prepare.call_args.kwargs["route_profile"].route_kind
            )
            self.assertEqual(
                "[wl:7] Topic\nbody\n", send_log.read_text(encoding="utf-8")
            )

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
            current = subprocess.CompletedProcess(
                ["tmux"], 0, stdout="vl:3.0\n", stderr=""
            )
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(email_me.subprocess, "run", return_value=current) as run,
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("Re: [vl:3] Topic", {}),
                ),
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(
                "Re: [vl:3] Topic\nbody\n", send_log.read_text(encoding="utf-8")
            )
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
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("Re: Untagged source subject", {}),
                ),
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(
                "Re: [wl:1] Untagged source subject\nbody\n",
                send_log.read_text(encoding="utf-8"),
            )

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
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("Re: [a] [wl:1] [vl:2] Topic", {}),
                ),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(2, result)
            self.assertIn(
                "exactly one bracketed tmux or OmniGent tag", stderr.getvalue()
            )

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
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ) as prepare,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--tmux-target",
                        "wl:7",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(("Topic", "wl:7"), prepare.call_args.args)
            self.assertEqual(
                "[wl:7] Topic\nbody\n", send_log.read_text(encoding="utf-8")
            )

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
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(email_me.subprocess, "run") as run,
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("Re: [vl:15] Topic", {}),
                ),
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--sender-tmux-target",
                        "vl:15",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(
                "Re: [vl:15] Topic\nbody\n", send_log.read_text(encoding="utf-8")
            )
            run.assert_not_called()

    def test_no_pwd_footer_still_passes_tmux_target_to_subject_preparation(
        self,
    ) -> None:
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
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me, "prepare_subject_and_headers", return_value=prepared
                ) as prepare,
            ):
                result = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--no-pwd-footer",
                        "--tmux-target",
                        "wl:7",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(("Topic", "wl:7"), prepare.call_args.args)
            self.assertEqual(
                "[wl:7] Topic\nbody\n", send_log.read_text(encoding="utf-8")
            )

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
            prepared = [
                ("[wl:1] Topic", {}),
                (
                    "Re: [wl:1] Topic",
                    {
                        "In-Reply-To": "<prior@example.test>",
                        "References": "<prior@example.test>",
                    },
                ),
            ]
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    email_me, "prepare_subject_and_headers", side_effect=prepared
                ),
            ):
                first = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
                second = email_me.main(
                    [
                        "--manager-human",
                        "--non-completion",
                        "--subject-file",
                        str(subject),
                        "--message-file",
                        str(body),
                    ]
                )
            self.assertEqual(0, first)
            self.assertEqual(0, second)
            self.assertEqual(
                "[wl:1] Topic\nbody\n", send_log.read_text(encoding="utf-8")
            )

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
            env_file.write_text(
                "EMAIL_ME_GMAIL_ADDRESS=me@example.test\nEMAIL_ME_GMAIL_APP_PASSWORD=secret\n",
                encoding="utf-8",
            )
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("Topic\n", encoding="utf-8")
            env = {
                "OMO_MANAGER_STATE_DIR": str(state_dir),
                "OMO_MANAGER_EMAIL_THREAD_LOOKUP_S": "0",
                "OMO_MANAGER_TMUX_TARGET": "wl:1.0",
            }
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(email_me, "ENV_FILE_PATH", env_file),
                patch.object(email_me, "configured_agent_mail", return_value=None),
                patch.object(email_me, "prepare_subject_and_headers") as prepare,
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
                patch("sys.stderr", new_callable=StringIO) as stderr,
            ):
                self.assertEqual(
                    2,
                    email_me.main(
                        [
                            "--manager-human",
                            "--non-completion",
                            "--no-pwd-footer",
                            "--subject-file",
                            str(subject),
                            "--message-file",
                            str(body),
                        ]
                    ),
                )
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
                patch.dict(
                    os.environ,
                    {"OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state")},
                    clear=False,
                ),
                patch.object(
                    email_me, "configured_agent_mail", return_value=Settings()
                ),
                patch.object(
                    email_me,
                    "prepare_subject_and_headers",
                    return_value=("[wl:1] Topic", {}),
                ),
                patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
                patch.object(email_me.ssl, "create_default_context", return_value=None),
            ):
                self.assertEqual(
                    0,
                    email_me.main(
                        [
                            "--manager-human",
                            "--non-completion",
                            "--tmux-target",
                            "wl:1",
                            "--subject",
                            "Topic",
                            "--message-file",
                            str(body),
                        ]
                    ),
                )
        self.assertEqual("agent@example.test", sent_messages[0]["From"])
        self.assertEqual("human@example.test", sent_messages[0]["To"])
        self.assertNotIn(
            "PWD:", sent_messages[0].get_body(preferencelist=("plain",)).get_content()
        )

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
            patch.dict(
                os.environ,
                {"OMO_MANAGER_STATE_DIR": str(Path(tmp) / "state")},
                clear=False,
            ),
            patch.object(email_me, "configured_agent_mail", return_value=Settings()),
            patch.object(
                email_me,
                "prepare_subject_and_headers",
                return_value=("[wl:1] Topic", {}),
            ),
            patch.object(email_me.smtplib, "SMTP_SSL", FakeSmtp),
            patch.object(email_me.ssl, "create_default_context", return_value=None),
            patch.object(sys, "stdin", StringIO("body\n")),
            patch("sys.stderr", new_callable=StringIO) as stderr,
        ):
            result = email_me.main(
                [
                    "--manager-human",
                    "--non-completion",
                    "--tmux-target",
                    "wl:1",
                    "--subject",
                    "Topic",
                ],
            )
        self.assertEqual(1, result)
        self.assertRegex(
            stderr.getvalue(),
            r"Delivery-uncertain Message-ID: <[^<>\s]+@example\.test>",
        )


if __name__ == "__main__":
    _ = unittest.main()
