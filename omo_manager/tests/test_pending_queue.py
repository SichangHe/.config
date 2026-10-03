from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_pending import Args
from omo_manager.omo_pending import REMOVAL_NOTICE_RECOVERIES
from omo_manager.omo_pending import SOURCE1929_RECOVERY_ID
from omo_manager.omo_pending import SOURCE2048_BATCH_PATH_EVIDENCE
from omo_manager.omo_pending import SOURCE2048_BATCH_PATH_ITEM
from omo_manager.omo_pending import SOURCE2048_DELIVERY_MESSAGE_SHA256
from omo_manager.omo_pending import SOURCE2048_PAPER_CWD
from omo_manager.omo_pending import SOURCE2048_PAPER_SESSION
from omo_manager.omo_pending import SOURCE2048_PAPER_TRANSCRIPT
from omo_manager.omo_pending import SOURCE2048_BROADER_ITEM
from omo_manager.omo_pending import SOURCE2048_DELIVERY_EVIDENCE
from omo_manager.omo_pending import SOURCE2048_DELIVERY_MESSAGE_ID
from omo_manager.omo_pending import SOURCE2048_DELIVERY_SUPPORT_ITEM
from omo_manager.omo_pending import SOURCE2048_SENT_BODY_SHA256
from omo_manager.omo_pending import SOURCE2048_SENT_SUBJECT_SHA256
from omo_manager.omo_pending import RemovalNoticeRecovery
from omo_manager.omo_pending import parse_args
from omo_manager.omo_pending import pending_display_text
from omo_manager.omo_pending import run
from omo_manager.omo_pending import WEEKLY_MEMO_RECOVERY_BLOCKER
from omo_manager.omo_pending import WEEKLY_MEMO_RECOVERY_ITEM
from omo_manager.omo_pending import recover_weekly_memo_reviewed_sent
from omo_manager.omo_pending import SOURCE2234_ITEM
from omo_manager.omo_pending import recover_source2234_worker_ack
from omo_manager.omo_pending import SOURCE2230_ITEMS
from omo_manager.omo_pending import recover_source2230_reviewed_sent
from omo_manager.omo_pending import SOURCE2259_ITEM
from omo_manager.omo_pending import SOURCE2259_HUMAN_MESSAGE_ID
from omo_manager.omo_pending import recover_source2259_manager_answer
from omo_manager.omo_pending import SOURCE2288_ITEM
from omo_manager.omo_pending import recover_source2288_reviewed_sent
from omo_manager.omo_pending import MEETING_SVM_ITEM
from omo_manager.omo_pending import MEETING_SVM_BLOCKER
from omo_manager.omo_pending import MEETING_SVM_MESSAGE_ID
from omo_manager.omo_pending import MEETING_SVM_PARENT_ID
from omo_manager.omo_pending import MEETING_SVM_SUBJECT
from omo_manager.omo_pending import MEETING_SVM_SUBJECT_SHA256
from omo_manager.omo_pending import MEETING_SVM_BODY_SHA256
from omo_manager.omo_pending import recover_meeting_svm_reviewed_sent
from omo_manager.omo_pending import CONFIG_ROUTING_ITEM
from omo_manager.omo_pending import recover_config_routing_reviewed_sent
from omo_manager.omo_pending import CONFIG_DETECTOR_ITEMS
from omo_manager.omo_pending import recover_config_detector_reviewed_sent
from omo_manager.omo_pending import CONFIG_WORKER_SOURCE_ITEM
from omo_manager.omo_pending import recover_config_worker_source_reviewed_sent
from omo_manager.omo_pending import SOURCE2180_ITEM
from omo_manager.omo_pending import SOURCE2180_WORKER_ITEM
from omo_manager.omo_pending import recover_source2180_worker_result
from omo_manager.omo_pending import SOURCE2000_CONFIG_ITEM
from omo_manager.omo_pending import recover_source2000_retired_pb
from omo_manager.omo_pending import SOURCE1974_ITEM
from omo_manager.omo_pending import SOURCE2291_ITEM
from omo_manager.omo_pending import recover_source1974_ownership
from omo_manager.omo_pending import SOURCE2261_ITEM
from omo_manager.omo_pending import SOURCE2261_NOTICE_MESSAGE_ID
from omo_manager.omo_pending import recover_source2261_slide_result
from omo_manager.omo_completion_email import claim_completion_email
from omo_manager.omo_completion_email import completion_email_is_delivered
from omo_manager.omo_completion_email import plan_completion_email
from omo_manager.omo_completion_email import refresh_unattempted_completion_claim
from omo_manager.omo_completion_email import send_completion_email
from omo_manager.omo_task_context import infer_active_task
from omo_manager.omo_task_context import infer_pending_task
from omo_manager.omo_task_metadata import frontmatter_parts


PARTICIPANT_EVIDENCE = (hashlib.sha256(b"agent@example.test").hexdigest(), hashlib.sha256(b"human@example.test").hexdigest())
SOURCE2048_REVIEWED_DELIVERY_MESSAGE = """The reviewed normal owner-local batch path is ready. It sends one owner-authenticated final answer, then removes exactly the four listed Source-2048 Human items in one guarded mutation. It does not list or remove the distinct broader review item.

After the final integrated wording review passes, write the final email subject as one line to `/tmp/source2048-final-subject.txt` and the final reviewed email body to `/tmp/source2048-final-message.txt`. Then run this exact command once from `DeGenTWeb_writeup:0`:

```sh
completion_key=$(python3 -c 'import hashlib, secrets; print(hashlib.sha256(secrets.token_bytes(32)).hexdigest())')
omo_pending.py remove \\
  --item '🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Draft the supplied 605-page Pangram-versus-Binoculars comparison into the paper, preserving that the observed cohort was incomplete and non-random, lacked Pangram-scored matched human texts, and used detector rules not matched by false-positive rate.' \\
  --item '🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Include the existing GitHub Issue plot in the paper after cleaning it up.' \\
  --item '🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Make a good, evidence-supported paper argument from the Pangram result without claiming higher overall accuracy.' \\
  --item '🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Use ChatGPT CLI to decide and review the wording.' \\
  --outcome completed \\
  --evidence 'Final Source-2048 result, ChatGPT answer e3c999663e9efbf5b57562639aeb230133884159bba4f205701c07f64a09d596, integrated wording, and Human-facing email were independently reviewed.' \\
  --completion-key "$completion_key" \\
  --answer-subject-file /tmp/source2048-final-subject.txt \\
  --answer-message-file /tmp/source2048-final-message.txt
```

Do not use `--no-email` or `reconcile-sent-remove`. Confirm afterward that `omo_pending.py list` retains the broader review item and no Source-2048 item.
"""
SOURCE2048_FINAL_MESSAGE = """The Pangram paragraph and cleaned plot are in Section 3.1, “Evaluation on Newer Generators.” The body remains eight pages.

Open the paper in Overleaf to review these two items:
https://www.overleaf.com/project/67b5138d6bdbe59818a13d48

1. PDF page 5, the paragraph beginning “Pangram”: is its argument clear?

“Pangram [56] marks more of the 605 observed Claude Sonnet-generated replacement texts as AI than Binoculars (Figure 5). Both detectors scored all 605 texts, a nonrandom subset of 780 Sonnet 4/4.6 replacements. Pangram labels 594 (98.2%) AI, including all 470 (77.7%) marked by Binoculars’ pre-existing max-F1 rule and 124 additional texts. Because we did not test Pangram on comparable human-written texts or set the detectors to the same false-positive rate, this result does not establish higher overall accuracy.”

2. PDF page 6, Figure 5 and its caption: are they readable at paper size?

“Pangram AI percentage (y-axis) versus Binoculars score (x-axis) for 605 observed Claude Sonnet 4/4.6 generated replacement texts. Each cross is one text. Binoculars calls texts left of the blue max-F1 cutoff AI; this gives the reported 470 calls. Orange shows its stricter alternative, calibrated for a 0.01% false-positive rate outside this sample. This generated-only sample does not measure that rate. The scatter shows the score relationship; Pangram’s count and overlap use its returned AI labels.”

Please reply with comments or edit the passages directly in Overleaf.
"""
SOURCE2048_ITEMS = (
    "🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Draft the supplied 605-page Pangram-versus-Binoculars comparison into the paper, preserving that the observed cohort was incomplete and non-random, lacked Pangram-scored matched human texts, and used detector rules not matched by false-positive rate.",
    "🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Include the existing GitHub Issue plot in the paper after cleaning it up.",
    "🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Make a good, evidence-supported paper argument from the Pangram result without claiming higher overall accuracy.",
    "🧑 Source-2048 (manager_mail/85c5dff58359-2048.txt): Use ChatGPT CLI to decide and review the wording.",
)
SOURCE2048_EVIDENCE = (
    "Final Source-2048 result, ChatGPT answer e3c999663e9efbf5b57562639aeb230133884159bba4f205701c07f64a09d596, integrated wording, and Human-facing email were independently reviewed."
)
SOURCE2048_KEY = "803ba79e684265b9d33fc98159bc0c94366cd93206916a36c5ae5825771dda2e"


def task_text(status: str = "running", items: tuple[str, ...] = ()) -> str:
    pending = "pending_task_items: []" if not items else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in items)
    blocked_on = "blocked_on: persistent role\n" if status in {"long_running", "blocked"} else ""
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocked_on}runat: cfg:2\ntool: codex\nmanagerat: cfg:1\nis_manager: false\n{pending}\n---\nwork\n"


def v2_task_text(item: str = "finish review") -> str:
    return f"""---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000001
status: running
runat: cfg:2
tool: codex
managerat: cfg:1
is_manager: false
pending_task_items:
  - id: pi_019f0000-0000-7000-8000-000000000002
    text: {item}
    blocked_on: []
    notices: []
resolved_task_items: []
---
work
"""


def removal_recovery(path: Path, text: str, items: tuple[str, ...], evidence: str) -> RemovalNoticeRecovery:
    return RemovalNoticeRecovery(path.name, hashlib.sha256(text.encode()).hexdigest(), items, evidence, "b" * 64)


class PendingQueueTests(unittest.TestCase):
    def test_source1974_recovers_only_committed_ownership_item_with_original_human_and_sent_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "work_logs"
            root.mkdir()
            sources = root / "manager_mail"
            sources.mkdir()
            ownership = sources / "85c5dff58359-1974.txt"
            extraction = sources / "85c5dff58359-2291.txt"
            ownership.write_text("Subject: helper ownership\n\nidk\n", encoding="utf-8")
            extraction.write_text("Subject: watcher\n\nExtract Human work instead\n", encoding="utf-8")
            task = root / "config_repair_0926.md"
            other = "🧑 Keep another Human task open"
            task.write_text(
                "---\nversion: v1.0.0\nstatus: long_running\nrunat: config:1\n"
                "tool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items:\n"
                f"  - {json.dumps(SOURCE1974_ITEM, ensure_ascii=False)}\n"
                f"  - {json.dumps(SOURCE2291_ITEM, ensure_ascii=False)}\n"
                f"  - {json.dumps(other, ensure_ascii=False)}\n"
                "---\n(record and delegate manager_mail/85c5dff58359-2291.txt)\n",
                encoding="utf-8",
            )
            todo = root / "TODO.md"
            todo.write_text("current:\nconfig_repair_0926.md config:1\n", encoding="utf-8")
            repo = Path(temporary) / "config"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            helpers = {
                "omo_manager/omo_codex_stop.py": b"stop helper\n",
                "omo_manager/omo_manager_replace.py": b"replacement helper\n",
                "omo_manager/tests/test_manager_replace.py": b"replacement tests\n",
            }
            for name, content in helpers.items():
                helper = repo / name
                helper.parent.mkdir(parents=True, exist_ok=True)
                helper.write_bytes(content)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "preserve helpers"],
                check=True,
            )
            commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            args = Args(
                "recover-source1974-ownership",
                (SOURCE1974_ITEM,),
                task_file=task.name,
                expected_task_sha256=hashlib.sha256(task.read_bytes()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_pending.SOURCE1974_ROOT", root),
                patch("omo_manager.omo_pending.SOURCE1974_HELPER_REPO", repo),
                patch("omo_manager.omo_pending.SOURCE1974_HELPER_COMMIT", commit),
                patch("omo_manager.omo_pending.SOURCE1974_COMMITTED_HELPERS", {name: hashlib.sha256(content).hexdigest() for name, content in helpers.items()}),
                patch("omo_manager.omo_pending.SOURCE1974_MAIL_SHA256", hashlib.sha256(ownership.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2291_MAIL_SHA256", hashlib.sha256(extraction.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task),
                patch("omo_manager.omo_pending.verify_original_human_in_inbox", return_value=True) as human,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=("a" * 64, "b" * 64)) as sent,
                patch("omo_manager.omo_pending.require_owner_completion", side_effect=AssertionError("no email")),
            ):
                before = task.read_bytes()
                with patch("omo_manager.omo_pending.authenticated_pending_task", side_effect=BlockingError("wrong owner")):
                    with self.assertRaisesRegex(BlockingError, "wrong owner"):
                        recover_source1974_ownership(args, root)
                human.assert_not_called()
                sent.assert_not_called()
                self.assertEqual(0, recover_source1974_ownership(replace(args, dry_run=True), root))
                self.assertEqual(before, task.read_bytes())
                self.assertEqual(2, human.call_count)
                self.assertEqual("<D6F3AA77-4023-402F-B09C-A36B68695E6F@gmail.com>", sent.call_args.kwargs["required_reference_id"])
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source1974_ownership(replace(args, expected_todo_sha256="0" * 64), root)
                source2291_row = f"  - {json.dumps(SOURCE2291_ITEM, ensure_ascii=False)}\n"
                task.write_text(before.decode().replace(source2291_row, ""), encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source1974_ownership(replace(args, expected_task_sha256=hashlib.sha256(task.read_bytes()).hexdigest()), root)
                task.write_bytes(before)
                with patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None):
                    with self.assertRaisesRegex(BlockingError, "Sent evidence"):
                        recover_source1974_ownership(args, root)
                (repo / "omo_manager/omo_codex_stop.py").write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "bytes changed"):
                    recover_source1974_ownership(args, root)
                (repo / "omo_manager/omo_codex_stop.py").write_bytes(helpers["omo_manager/omo_codex_stop.py"])
                real_run = subprocess.run
                def change_head_during_proof(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                    result = real_run(command, **kwargs)
                    if len(command) == 5 and command[3] == "show" and command[4].endswith(":omo_manager/tests/test_manager_replace.py"):
                        (repo / "unrelated.txt").write_text("later commit\n", encoding="utf-8")
                        real_run(["git", "-C", str(repo), "add", "unrelated.txt"], check=True)
                        real_run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "unrelated"], check=True)
                    return result
                with patch("omo_manager.omo_pending.subprocess.run", side_effect=change_head_during_proof):
                    with self.assertRaisesRegex(BlockingError, "custody changed"):
                        recover_source1974_ownership(args, root)
                self.assertEqual(before, task.read_bytes())
                real_run(["git", "-C", str(repo), "reset", "--hard", commit], check=True, capture_output=True)
                rev_parse_calls = 0
                def change_worktree_during_proof(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                    nonlocal rev_parse_calls
                    result = real_run(command, **kwargs)
                    if len(command) == 5 and command[3:] == ["rev-parse", "HEAD"]:
                        rev_parse_calls += 1
                        if rev_parse_calls == 2:
                            (repo / "omo_manager/omo_codex_stop.py").write_text("late tamper", encoding="utf-8")
                    return result
                with patch("omo_manager.omo_pending.subprocess.run", side_effect=change_worktree_during_proof):
                    with self.assertRaisesRegex(BlockingError, "custody changed"):
                        recover_source1974_ownership(args, root)
                self.assertEqual(before, task.read_bytes())
                (repo / "omo_manager/omo_codex_stop.py").write_bytes(helpers["omo_manager/omo_codex_stop.py"])
                self.assertEqual(0, recover_source1974_ownership(args, root))
                self.assertIn(other, task.read_text(encoding="utf-8"))
                self.assertIn(SOURCE2291_ITEM, task.read_text(encoding="utf-8"))
                self.assertNotIn(SOURCE1974_ITEM, task.read_text(encoding="utf-8"))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source1974_ownership(args, root)

    def test_source2000_retired_pb_recovery_preserves_paused_service_and_other_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            request = mail_dir / "85c5dff58359-2000.txt"
            request.write_text("Subject: PB retirement\n\nClose the old manager.", encoding="utf-8")
            approval = mail_dir / "85c5dff58359-2028.txt"
            approval.write_text("Subject: PB retirement\n\nyes", encoding="utf-8")
            config = root / "config_repair_0926.md"
            other_item = "🧑 Keep the unrelated pending work"
            config.write_text(
                "---\nversion: v1.0.0\nstatus: long_running\n"
                "runat: config:1\ntool: codex\nmanagerat: wl:1\nis_manager: false\n"
                f"pending_task_items:\n  - {json.dumps(SOURCE2000_CONFIG_ITEM, ensure_ascii=False)}\n"
                f"  - {json.dumps(other_item, ensure_ascii=False)}\n---\nbody\n",
                encoding="utf-8",
            )
            pb = root / "pb_news_mgr.md"
            pb.write_text(
                "---\nversion: v1.0.0\nstatus: done\nrunat: pb:1\ntool: codex\n"
                "managerat: wl:1\nis_manager: true\npending_task_items: []\n---\n"
                "(missing-target record closed without tmux mutation: pb:1; authority: manager_mail/85c5dff58359-2028.txt:3-3; audited)\n",
                encoding="utf-8",
            )
            news = root / "news_service.md"
            news.write_text(
                "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: explicit Human request to resume PB news\n"
                "runat: pb:0\ntool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items: []\n---\npaused\n",
                encoding="utf-8",
            )
            pb_before = pb.read_bytes()
            news_before = news.read_bytes()
            todo = root / "TODO.md"
            todo.write_text("current:\nconfig_repair_0926.md config:1\nmail_cleanup_0927.md pb:1\n", encoding="utf-8")
            args = Args(
                "recover-source2000-retired-pb", (SOURCE2000_CONFIG_ITEM,),
                task_file=config.name,
                expected_task_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
                expected_pb_sha256=hashlib.sha256(pb.read_bytes()).hexdigest(),
                expected_news_sha256=hashlib.sha256(news.read_bytes()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_pending.SOURCE2000_ROOT", root),
                patch("omo_manager.omo_pending.SOURCE2000_MAIL_SHA256", hashlib.sha256(request.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2000_APPROVAL_SHA256", hashlib.sha256(approval.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=config) as owner,
                patch("omo_manager.omo_pending.require_owner_completion", side_effect=AssertionError("no mail")),
            ):
                before = config.read_bytes()
                self.assertEqual(0, recover_source2000_retired_pb(replace(args, dry_run=True), root))
                self.assertEqual(before, config.read_bytes())
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source2000_retired_pb(replace(args, expected_news_sha256="0" * 64), root)
                approval.write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "approval changed"):
                    recover_source2000_retired_pb(args, root)
                approval.write_text("Subject: PB retirement\n\nyes", encoding="utf-8")
                self.assertEqual(0, recover_source2000_retired_pb(args, root))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source2000_retired_pb(args, root)
            owner.assert_called()
            self.assertEqual(pb_before, pb.read_bytes())
            self.assertEqual(news_before, news.read_bytes())
            self.assertIn(other_item, config.read_text(encoding="utf-8"))
            self.assertNotIn(SOURCE2000_CONFIG_ITEM, config.read_text(encoding="utf-8"))

    def test_source2261_uses_result_not_creation_notice_and_preserves_older_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manager_mail" / "85c5dff58359-2261.txt"
            source.parent.mkdir()
            source.write_text("Subject: slides\n\nRetry ChatGPT CLI.", encoding="utf-8")
            task = root / "tuesday_slides_0927.md"
            text = (
                "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: human\n"
                "runat: wl:4\ntool: codex\nmanagerat: wl:5\nis_manager: false\n"
                f"pending_task_items:\n  - older Human request\n  - '{SOURCE2261_ITEM}'\n"
                "---\nbody\n(record and delegate manager_mail/85c5dff58359-2261.txt)\n"
            )
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("human pending:\ntuesday_slides_0927.md wl:4\n", encoding="utf-8")
            args = Args("recover-source2261-slide-result", (SOURCE2261_ITEM,))
            with (
                patch("omo_manager.omo_pending.SOURCE2261_ROOT", root),
                patch("omo_manager.omo_pending.SOURCE2261_TASK_SHA256", hashlib.sha256(text.encode()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2261_TODO_SHA256", hashlib.sha256(todo.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2261_MAIL_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task),
                patch("omo_manager.omo_pending.verify_original_human_in_inbox", return_value=True),
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", side_effect=[PARTICIPANT_EVIDENCE, None]) as sent,
            ):
                with self.assertRaisesRegex(BlockingError, "reviewed result"):
                    recover_source2261_slide_result(args, root)
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                sent.side_effect = [PARTICIPANT_EVIDENCE, PARTICIPANT_EVIDENCE]
                self.assertEqual(0, recover_source2261_slide_result(args, root))
                sent.side_effect = None
                sent.return_value = PARTICIPANT_EVIDENCE
                with self.assertRaisesRegex(BlockingError, "changed"):
                    recover_source2261_slide_result(args, root)
            self.assertEqual(SOURCE2261_NOTICE_MESSAGE_ID, sent.call_args_list[-2].args[0])
            self.assertIn("older Human request", task.read_text(encoding="utf-8"))
            self.assertNotIn(SOURCE2261_ITEM, task.read_text(encoding="utf-8"))
            self.assertIn("Cross-thread Human result", task.read_text(encoding="utf-8"))
            self.assertEqual("recover-source2261-slide-result", parse_args(["recover-source2261-slide-result"]).command)

    def test_source2259_recovery_requires_original_thread_and_exact_manager_custody(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manager_mail" / "85c5dff58359-2259.txt"
            source.parent.mkdir()
            source.write_text("Subject: reply\n\nWhat is my original request?", encoding="utf-8")
            task = root / "wl_ops_0927.md"
            text = (
                "---\nversion: v1.0.0\nstatus: long_running\n"
                "runat: wl:5\ntool: codex\nmanagerat: wl:1\nis_manager: true\n"
                f"pending_task_items:\n  - '{SOURCE2259_ITEM}'\n"
                "---\nbody\n(record and delegate manager_mail/85c5dff58359-2259.txt)\n"
            )
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nwl_ops_0927.md wl:5\n", encoding="utf-8")
            args = Args("recover-source2259-manager-answer", (SOURCE2259_ITEM,))
            with (
                patch("omo_manager.omo_pending.SOURCE2259_ROOT", root),
                patch("omo_manager.omo_pending.SOURCE2259_TASK_SHA256", hashlib.sha256(text.encode()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2259_TODO_SHA256", hashlib.sha256(todo.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2259_MAIL_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task),
                patch("omo_manager.omo_pending.verify_original_human_in_inbox", return_value=True) as inbox,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
            ):
                with self.assertRaisesRegex(BlockingError, "not exact original-thread Sent-Mail"):
                    recover_source2259_manager_answer(args, root)
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                sent.return_value = PARTICIPANT_EVIDENCE
                self.assertEqual(0, recover_source2259_manager_answer(args, root))
                with self.assertRaisesRegex(BlockingError, "changed"):
                    recover_source2259_manager_answer(args, root)
            inbox.assert_called_with(
                SOURCE2259_HUMAN_MESSAGE_ID,
                "5018cb2e1fbfb1a6f9d3fc0d4b58a2c95c71ca495a58385f2a4d0b4019912e96",
                "b1b0929dc42577a7c569554abc401da1b38d9d9b94486adf6c480cf7703c6168",
            )
            sent.assert_called_with(
                "<179064177588.2645862.371621574684915649@gmail.com>",
                "5018cb2e1fbfb1a6f9d3fc0d4b58a2c95c71ca495a58385f2a4d0b4019912e96",
                "816cfef4b436cd0abcc8631ba742f51a8be484dae24f19f6f05ea3515b73c508",
                required_reference_id=SOURCE2259_HUMAN_MESSAGE_ID,
            )
            self.assertIn("pending_task_items: []", task.read_text(encoding="utf-8"))
            self.assertEqual(0, task.read_text(encoding="utf-8").count(SOURCE2259_ITEM))
            self.assertEqual("recover-source2259-manager-answer", parse_args(["recover-source2259-manager-answer"]).command)

    def test_config_routing_existing_sent_removes_only_answered_item_without_email(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "config_repair_0926.md"
            other = "🧑 Human unfinished task"
            source = "My email for wl:1 went to you\nLet's stop stripping out the agent address tag"
            text = (
                "---\nversion: v1.0.0\nstatus: long_running\nrunat: config:1\ntool: codex\n"
                "managerat: wl:1\nis_manager: false\npending_task_items:\n"
                f"  - '{CONFIG_ROUTING_ITEM}'\n  - '{other}'\n---\n{source}\n"
            )
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nconfig_repair_0926.md config:1\n", encoding="utf-8")
            answer = root / "answer.txt"
            answer.write_text("Sent routing result\n", encoding="utf-8")
            args = Args(
                "recover-config-routing-reviewed-sent",
                (CONFIG_ROUTING_ITEM,),
                task_file=task.name,
                expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_pending.SOURCE2234_ROOT", root),
                patch("omo_manager.omo_pending.CONFIG_ROUTING_ANSWER_PATH", answer),
                patch("omo_manager.omo_pending.CONFIG_ROUTING_ANSWER_SHA256", hashlib.sha256(answer.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task) as owner,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
                patch("omo_manager.omo_completion_email.send_completion_email", side_effect=AssertionError("no email")),
            ):
                with self.assertRaisesRegex(BlockingError, "Sent-Mail"):
                    recover_config_routing_reviewed_sent(args, root)
                sent.return_value = PARTICIPANT_EVIDENCE
                owner.return_value = root / "other.md"
                with self.assertRaisesRegex(BlockingError, "authenticated owner"):
                    recover_config_routing_reviewed_sent(args, root)
                owner.return_value = task
                with self.assertRaisesRegex(BlockingError, "exact work-log task"):
                    recover_config_routing_reviewed_sent(replace(args, task_file="other.md"), root)
                answer.write_text("changed", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "answer bytes changed"):
                    recover_config_routing_reviewed_sent(args, root)
                answer.write_text("Sent routing result\n", encoding="utf-8")
                sent.reset_mock()
                self.assertEqual(0, recover_config_routing_reviewed_sent(replace(args, dry_run=True), root))
                self.assertEqual(2, sent.call_count)
                sent.assert_any_call(
                    "<179057126729.3268183.18179798725032047003@gmail.com>",
                    "d9665f9ec4ff713e954b75a294546f93b0d22fde0b46e37261da18434e6cdb8e",
                    "264d29b510ae55887366456c4fa90a42af01312732d387d10df0ff171d01a2b3",
                )
                sent.assert_any_call(
                    "<179067908563.2475668.15671929232853372656@gmail.com>",
                    "4b76823b5d816c9cfcdb8448936c2a2cd84c3b34ff12491fdf64eeb4e954a181",
                    "4a49711163c2f388f3054e797d11f184c3683af071fb63cc4c12f015aa181884",
                    required_reference_id="<179057126729.3268183.18179798725032047003@gmail.com>",
                    required_task_tag="config_repair_0926",
                    required_normalized_body="Sent routing result\n",
                )
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                task.write_text(text + "changed\n", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_config_routing_reviewed_sent(args, root)
                task.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_config_routing_reviewed_sent(replace(args, expected_todo_sha256="0" * 64), root)
                changed_owner = text.replace("managerat: wl:1", "managerat: dw:1")
                task.write_text(changed_owner, encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_config_routing_reviewed_sent(replace(args, expected_task_sha256=hashlib.sha256(changed_owner.encode()).hexdigest()), root)
                task.write_text(text, encoding="utf-8")
                self.assertEqual(0, recover_config_routing_reviewed_sent(args, root))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_config_routing_reviewed_sent(args, root)
            result = task.read_text(encoding="utf-8")
            self.assertIn(other, result)
            self.assertNotIn(CONFIG_ROUTING_ITEM, result.split("---", 2)[1])
            self.assertIn("no new email sent", result)
            self.assertEqual(
                "recover-config-routing-reviewed-sent",
                parse_args([
                    "recover-config-routing-reviewed-sent",
                    "--expected-task-sha256", "0" * 64,
                    "--expected-todo-sha256", "0" * 64,
                ]).command,
            )

    def test_detector_existing_sent_recovery_is_exact_and_replay_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "config_repair_0926.md"
            items = "\n".join(f"  - {json.dumps(item, ensure_ascii=False)}" for item in CONFIG_DETECTOR_ITEMS)
            text = (
                "---\nversion: v1.0.0\nstatus: long_running\nrunat: config:1\n"
                "tool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items:\n"
                f"{items}\n---\nHuman detector request\n"
            )
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nconfig_repair_0926.md config:1\n", encoding="utf-8")
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            for suffix in ("2301", "2303"):
                (mail_dir / f"85c5dff58359-{suffix}.txt").write_text(suffix, encoding="utf-8")
            state = root / "state" / "email-lifecycle-stops"
            state.mkdir(parents=True)
            receipt_name = hashlib.sha256(b"2301").hexdigest()
            (state / f"{receipt_name}.json").write_text(json.dumps({
                "outcome": "stopped", "requested_action": "replace", "target": "dw8:0",
                "mail_sha256": receipt_name, "owner": str(root / "gpu_det_2287.md"),
            }), encoding="utf-8")
            args = Args("recover-config-detector-reviewed-sent", CONFIG_DETECTOR_ITEMS,
                        task_file=task.name, expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                        expected_todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest())
            with (
                patch("omo_manager.omo_pending.SOURCE2234_ROOT", root),
                patch("omo_manager.omo_pending.CONFIG_DETECTOR_STOP_RECEIPT", receipt_name),
                patch("omo_manager.omo_pending.CONFIG_DETECTOR_FOLLOWUP_SHA256", hashlib.sha256(b"2303").hexdigest()),
                patch("omo_manager.omo_pending.completion_email_state_dir", return_value=root / "state"),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task) as owner,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
                patch("omo_manager.omo_completion_email.send_completion_email", side_effect=AssertionError("no email")),
            ):
                with self.assertRaisesRegex(BlockingError, "Sent-Mail"):
                    recover_config_detector_reviewed_sent(args, root)
                sent.return_value = PARTICIPANT_EVIDENCE
                owner.return_value = root / "other.md"
                with self.assertRaisesRegex(BlockingError, "authenticated configuration owner"):
                    recover_config_detector_reviewed_sent(args, root)
                owner.return_value = task
                self.assertEqual(0, recover_config_detector_reviewed_sent(replace(args, dry_run=True), root))
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                (mail_dir / "85c5dff58359-2303.txt").write_text("altered", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "source"):
                    recover_config_detector_reviewed_sent(args, root)
                (mail_dir / "85c5dff58359-2303.txt").write_text("2303", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "task, TODO"):
                    recover_config_detector_reviewed_sent(replace(args, expected_todo_sha256="0" * 64), root)
                self.assertEqual(0, recover_config_detector_reviewed_sent(args, root))
                self.assertIn("pending_task_items: []", task.read_text(encoding="utf-8"))
                with self.assertRaisesRegex(BlockingError, "ordered queue changed"):
                    recover_config_detector_reviewed_sent(args, root)

    def test_worker_source_existing_sent_recovery_is_exact_and_replay_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "config_repair_0926.md"
            text = (
                "---\nversion: v1.0.0\nstatus: long_running\nrunat: config:1\n"
                "tool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items:\n"
                f"  - {json.dumps(CONFIG_WORKER_SOURCE_ITEM, ensure_ascii=False)}\n---\nHuman request\n"
            )
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nconfig_repair_0926.md config:1\n", encoding="utf-8")
            args = Args(
                "recover-config-worker-source-reviewed-sent", (CONFIG_WORKER_SOURCE_ITEM,), task_file=task.name,
                expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_pending.SOURCE2234_ROOT", root),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task) as owner,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
                patch("omo_manager.omo_completion_email.send_completion_email", side_effect=AssertionError("no email")),
            ):
                with self.assertRaisesRegex(BlockingError, "Sent Mail"):
                    recover_config_worker_source_reviewed_sent(args, root)
                sent.return_value = PARTICIPANT_EVIDENCE
                owner.return_value = root / "other.md"
                with self.assertRaisesRegex(BlockingError, "authenticated task owner"):
                    recover_config_worker_source_reviewed_sent(args, root)
                owner.return_value = task
                self.assertEqual(0, recover_config_worker_source_reviewed_sent(replace(args, dry_run=True), root))
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_config_worker_source_reviewed_sent(replace(args, expected_todo_sha256="0" * 64), root)
                self.assertEqual(0, recover_config_worker_source_reviewed_sent(args, root))
                self.assertIn("pending_task_items: []", task.read_text(encoding="utf-8"))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_config_worker_source_reviewed_sent(args, root)

    def test_meeting_svm_exact_sent_removes_only_the_answered_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "meeting.vtt"
            source.write_text("Sichang: train on unfiltered sites and test on unfiltered sites\n", encoding="utf-8")
            task = root / "cc_dw_new_0928.md"
            other = "🧑 Other unfinished Human request"
            text = (
                f"---\nversion: v1.0.0\nstatus: blocked\nblocked_on: {MEETING_SVM_BLOCKER}\nrunat: dw:5\ntool: codex\n"
                "managerat: dw:1\nis_manager: false\npending_task_items:\n"
                f"  - '{MEETING_SVM_ITEM}'\n  - '{other}'\n---\n"
            )
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ncc_dw_new_0928.md dw:5\n", encoding="utf-8")
            args = Args(
                "recover-meeting-svm-reviewed-sent",
                (MEETING_SVM_ITEM,),
                task_file=task.name,
                expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_pending.MEETING_SVM_ROOT", root),
                patch("omo_manager.omo_pending.MEETING_SVM_SOURCE", source),
                patch("omo_manager.omo_pending.MEETING_SVM_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task) as owner,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
                patch("omo_manager.omo_completion_email.send_completion_email", side_effect=AssertionError("no email")),
            ):
                with self.assertRaisesRegex(BlockingError, "Sent-Mail"):
                    recover_meeting_svm_reviewed_sent(args, root)
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                sent.assert_called_once_with(
                    MEETING_SVM_MESSAGE_ID,
                    MEETING_SVM_SUBJECT_SHA256,
                    MEETING_SVM_BODY_SHA256,
                    required_reference_id=MEETING_SVM_PARENT_ID,
                    required_task_tag=task.stem,
                    required_record_subject=MEETING_SVM_SUBJECT,
                )
                sent.return_value = PARTICIPANT_EVIDENCE
                with patch("omo_manager.omo_pending.MEETING_SVM_SUBJECT", "Re: [wrong_task] Positives in CC 2014 dataset"):
                    with self.assertRaisesRegex(BlockingError, "task tag"):
                        recover_meeting_svm_reviewed_sent(args, root)
                with patch("omo_manager.omo_pending.MEETING_SVM_PARENT_ID", "<wrong-parent@gmail.com>"):
                    sent.return_value = None
                    with self.assertRaisesRegex(BlockingError, "Sent-Mail"):
                        recover_meeting_svm_reviewed_sent(args, root)
                sent.return_value = PARTICIPANT_EVIDENCE
                owner.return_value = root / "other.md"
                with self.assertRaisesRegex(BlockingError, "authenticated worker owner"):
                    recover_meeting_svm_reviewed_sent(args, root)
                owner.return_value = task
                source.write_text("changed", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "transcript changed"):
                    recover_meeting_svm_reviewed_sent(args, root)
                source.write_text("Sichang: train on unfiltered sites and test on unfiltered sites\n", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_meeting_svm_reviewed_sent(replace(args, expected_todo_sha256="0" * 64), root)
                with patch("omo_manager.omo_pending.MEETING_SVM_BLOCKER", "different blocker"):
                    with self.assertRaisesRegex(BlockingError, "custody changed"):
                        recover_meeting_svm_reviewed_sent(args, root)
                self.assertEqual(0, recover_meeting_svm_reviewed_sent(replace(args, dry_run=True), root))
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(0, recover_meeting_svm_reviewed_sent(args, root))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_meeting_svm_reviewed_sent(args, root)
            updated = task.read_text(encoding="utf-8")
            self.assertNotIn(MEETING_SVM_ITEM, updated)
            self.assertIn(other, updated)

    def test_source2288_exact_original_thread_removes_only_one_item_without_email(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manager_mail" / "85c5dff58359-2288.txt"
            source.parent.mkdir()
            source.write_text("Subject: answer\n\nMark this as done. Clear your pending item if applicable.", encoding="utf-8")
            task = root / "cc_dw_new_0928.md"
            other = "🧑 Other unanswered Human task"
            text = (
                "---\nversion: v1.0.0\nstatus: running\nrunat: dw:5\ntool: codex\n"
                "managerat: dw:1\nis_manager: false\npending_task_items:\n"
                f"  - '{SOURCE2288_ITEM}'\n  - '{other}'\n---\n"
                "(record and delegate manager_mail/85c5dff58359-2288.txt)\n"
            )
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ncc_dw_new_0928.md dw:5\n", encoding="utf-8")
            args = Args(
                "recover-source2288-reviewed-sent",
                (SOURCE2288_ITEM,),
                task_file=task.name,
                expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_pending.SOURCE2288_ROOT", root),
                patch("omo_manager.omo_pending.SOURCE2288_MAIL_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task) as owner,
                patch("omo_manager.omo_pending.verify_original_human_in_inbox", return_value=True) as inbox,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
                patch("omo_manager.omo_completion_email.send_completion_email", side_effect=AssertionError("no email")),
            ):
                with self.assertRaisesRegex(BlockingError, "Sent-Mail"):
                    recover_source2288_reviewed_sent(args, root)
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                owner.return_value = root / "wrong_owner.md"
                with self.assertRaisesRegex(BlockingError, "authenticated worker owner"):
                    recover_source2288_reviewed_sent(args, root)
                owner.return_value = task
                with self.assertRaisesRegex(BlockingError, "exact work-log root, task and item"):
                    recover_source2288_reviewed_sent(replace(args, task_file="other.md"), root)
                sent.return_value = PARTICIPANT_EVIDENCE
                source.write_text("Subject: changed", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "Human source changed"):
                    recover_source2288_reviewed_sent(args, root)
                source.write_text("Subject: answer\n\nMark this as done. Clear your pending item if applicable.", encoding="utf-8")
                self.assertEqual(0, recover_source2288_reviewed_sent(replace(args, dry_run=True), root))
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source2288_reviewed_sent(replace(args, expected_todo_sha256="0" * 64), root)
                self.assertEqual(0, recover_source2288_reviewed_sent(args, root))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source2288_reviewed_sent(args, root)
            result = task.read_text(encoding="utf-8")
            self.assertIn(other, result)
            self.assertNotIn(f"  - '{SOURCE2288_ITEM}'", result)
            self.assertIn("no new email sent", result)
            inbox.assert_called_with(
                "<3E6629A4-099A-46C4-9AB7-9885D0E208EB@gmail.com>",
                "0b1909491b5c0513f01bbb206d34bf1eb7a8ca71d87f8866c7811d9258137f09",
                "8c0c42f09f6441ccc1020d226c9092ab621885cc9fa663682f3bc8fd23aa3cef",
            )
            sent.assert_called_with(
                "<179066040763.790981.13814461570856129042@gmail.com>",
                "0b1909491b5c0513f01bbb206d34bf1eb7a8ca71d87f8866c7811d9258137f09",
                "69835fb88ee242abe2e60063c602b1339772d4f9292d7bf72e384ebbb9efa355",
                required_reference_id="<3E6629A4-099A-46C4-9AB7-9885D0E208EB@gmail.com>",
            )

    def test_source2180_cross_task_result_preserves_other_manager_and_worker_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manager_mail" / "85c5dff58359-2180.txt"
            source.parent.mkdir()
            source.write_text("Subject: reply\n\nDo the repair NOW", encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest = {
                "accepted": True,
                "publication_kind": "versioned-four-site-accepted-score-overlay",
                "scored_production_pages": 60,
                "scored_raw_pages": 60,
                "sites": [{"production_pages": 15, "raw_pages": 15} for _ in range(4)],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manager = root / "dw_manager_0926.md"
            other = "🧑 Another manager Human item"
            manager_text = (
                "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: pending manager replacement\nrunat: dw:1\ntool: codex\n"
                "managerat: wl:1\nis_manager: true\npending_task_items:\n"
                f"  - '{SOURCE2180_ITEM}'\n  - '{other}'\n---\n"
                "(record and delegate manager_mail/85c5dff58359-2180.txt)\n"
            )
            manager.write_text(manager_text, encoding="utf-8")
            worker = root / "cc_dw_new_0928.md"
            worker_text = (
                "---\nversion: v1.0.0\nstatus: running\nrunat: dw:5\ntool: codex\n"
                f"managerat: dw:1\nis_manager: false\npending_task_items:\n  - '{SOURCE2180_WORKER_ITEM}'\n---\n"
                "Worker result Message-ID <179065335103.2662615.12759391741780929279@gmail.com>\n"
            )
            worker.write_text(worker_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ncc_dw_new_0928.md dw:5\nhuman-pending:\ndw_manager_0926.md dw:1\n", encoding="utf-8")
            args = Args(
                "recover-source2180-worker-result",
                (SOURCE2180_ITEM,),
                task_file=manager.name,
                expected_task_sha256=hashlib.sha256(manager_text.encode()).hexdigest(),
                expected_worker_sha256=hashlib.sha256(worker_text.encode()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_pending.SOURCE2180_ROOT", root),
                patch("omo_manager.omo_pending.SOURCE2180_MANIFEST", manifest_path),
                patch("omo_manager.omo_pending.SOURCE2180_MANIFEST_SHA256", hashlib.sha256(manifest_path.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2180_MAIL_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=manager) as owner,
                patch("omo_manager.omo_pending.verify_original_human_in_inbox", return_value=True) as inbox,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
                patch("omo_manager.omo_completion_email.send_completion_email", side_effect=AssertionError("no email")),
            ):
                owner.return_value = root / "wrong_owner.md"
                with self.assertRaisesRegex(BlockingError, "authenticated DW manager owner"):
                    recover_source2180_worker_result(args, root)
                inbox.assert_not_called()
                owner.return_value = manager
                with self.assertRaisesRegex(BlockingError, "Sent-Mail"):
                    recover_source2180_worker_result(args, root)
                sent.return_value = PARTICIPANT_EVIDENCE
                with self.assertRaisesRegex(BlockingError, "exact DW manager task"):
                    recover_source2180_worker_result(replace(args, task_file="other.md"), root)
                manifest_path.write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "accepted four-site manifest changed"):
                    recover_source2180_worker_result(args, root)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source2180_worker_result(replace(args, expected_worker_sha256="0" * 64), root)
                worker.write_text(worker_text.replace(SOURCE2180_WORKER_ITEM, "unrelated worker item"), encoding="utf-8")
                missing_item_args = replace(args, expected_worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest())
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source2180_worker_result(missing_item_args, root)
                worker.write_text(worker_text, encoding="utf-8")
                self.assertEqual(0, recover_source2180_worker_result(replace(args, dry_run=True), root))
                self.assertEqual(manager_text, manager.read_text(encoding="utf-8"))
                self.assertEqual(0, recover_source2180_worker_result(args, root))
                with self.assertRaisesRegex(BlockingError, "custody changed"):
                    recover_source2180_worker_result(args, root)
            result = manager.read_text(encoding="utf-8")
            self.assertIn(other, result)
            self.assertNotIn(f"  - '{SOURCE2180_ITEM}'", result)
            self.assertEqual(worker_text, worker.read_text(encoding="utf-8"))
            inbox.assert_called_with(
                "<B23B2868-4DF5-4435-B248-D558A352DF06@gmail.com>",
                "01225c090f8049fa163892744430ffc9249dbde7393cc1ffa0fadb56f863d764",
                "cf12490efaf89ba3c56580b8463c58e8e68abf436f0c69aa25b29f6f84848f04",
            )

    def test_source2259_rejects_wrong_owner_before_mail_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "wl_ops_0927.md"
            with (
                patch("omo_manager.omo_pending.SOURCE2259_ROOT", root),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=root / "other.md"),
                patch("omo_manager.omo_pending.verify_original_human_in_inbox") as inbox,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent") as sent,
                self.assertRaisesRegex(BlockingError, "authenticated manager owner"),
            ):
                recover_source2259_manager_answer(Args("recover-source2259-manager-answer", (SOURCE2259_ITEM,)), root)
            inbox.assert_not_called()
            sent.assert_not_called()
            self.assertFalse(task.exists())

    def test_explicit_list_is_read_only_when_detached_mutation_is_unauthenticated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "task.md"
            original = task_text(items=("🧑 preserve the original request",))
            task.write_text(original, encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", side_effect=TaskFrontmatterError("current tmux pane cannot be identified")):
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(0, run(Args("list", task_file="task.md"), root))
                self.assertIn("preserve the original request", output.getvalue())
                with self.assertRaisesRegex(TaskFrontmatterError, "current tmux pane cannot be identified"):
                    run(Args("add", ("new request",), task_file="task.md"), root)
            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_explicit_task_file_adds_agent_item_without_pane_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "task.md"
            task.write_text(task_text(items=("🧑 preserve the original request",)), encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_task_context.current_tmux_target", side_effect=TaskFrontmatterError("current tmux pane cannot be identified")),
                patch("omo_manager.omo_blocking_actor.request", side_effect=OSError("actor cannot authenticate detached tool")),
                patch("omo_manager.omo_pending.require_pending_add_notice") as notice,
            ):
                self.assertEqual(0, run(Args("add", ("repair the helper",), task_file="task.md"), root))
            notice.assert_not_called()
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(("🧑 preserve the original request", "repair the helper"), metadata.pending_task_items)

    def test_detached_explicit_task_file_persists_human_item_without_impersonating_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "task.md"
            original_item = "🧑 preserve the original request"
            new_item = "🧑 answer the Human's new request"
            task.write_text(task_text(items=(original_item,)), encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_task_context.current_tmux_target", side_effect=TaskFrontmatterError("current tmux pane cannot be identified")),
                patch("omo_manager.omo_blocking_actor.request", side_effect=OSError("detached tool")),
                patch("omo_manager.omo_pending.require_pending_add_notice") as notice,
                patch("omo_manager.omo_pending.delivered_pending_add_notice") as delivered,
            ):
                args = Args("add", (new_item,), task_file="task.md")
                self.assertEqual(0, run(args, root))
                self.assertEqual(0, run(args, root))
            notice.assert_not_called()
            delivered.assert_not_called()
            text = task.read_text(encoding="utf-8")
            metadata = parse_task_metadata(text, root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual((original_item, new_item), metadata.pending_task_items)
            self.assertNotIn("pending item creation notice:", text)

    def test_named_self_owned_add_notifies_only_human_items_once(self) -> None:
        for version in ("v1", "v2"):
            for ambient_selection in (False, True):
                with self.subTest(version=version, ambient_selection=ambient_selection), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    task = root / "task.md"
                    task.write_text(task_text() if version == "v1" else v2_task_text(), encoding="utf-8")
                    (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
                    environment = {"OMO_AGENT_TASK_FILE": "task.md" if ambient_selection else ""}
                    human_item = "🧑 answer the Human's new request"
                    args = Args("add", (human_item, "repair the helper"), task_file=None if ambient_selection else "task.md")

                    def notice_before_add(*notice_args: object) -> bool:
                        self.assertNotIn(human_item, task.read_text(encoding="utf-8"))
                        self.assertEqual((human_item,), notice_args[3])
                        return True

                    with (
                        patch.dict("os.environ", environment),
                        patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2"),
                        patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                        patch("omo_manager.omo_pending.delivered_pending_add_notice", return_value=("", False)),
                        patch("omo_manager.omo_pending.require_pending_add_notice", side_effect=notice_before_add) as notice,
                    ):
                        self.assertEqual(0, run(args, root))
                        self.assertEqual(0, run(args, root))
                    notice.assert_called_once()
                    text = task.read_text(encoding="utf-8")
                    metadata = parse_task_metadata(text, root)
                    assert metadata is not None
                    self.assertIn(human_item, metadata.pending_task_items)
                    self.assertIn("repair the helper", metadata.pending_task_items)
                    self.assertRegex(text, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}; items: [0-9a-f]{64}(?:,[0-9a-f]{64})*\)")

    def test_foreign_named_human_add_does_not_authenticate_from_filename(self) -> None:
        for version in ("v1", "v2"):
            for ambient_selection in (False, True):
                with self.subTest(version=version, ambient_selection=ambient_selection), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    task = root / "task.md"
                    task.write_text(task_text() if version == "v1" else v2_task_text(), encoding="utf-8")
                    (root / "other.md").write_text(task_text().replace("runat: cfg:2", "runat: cfg:3"), encoding="utf-8")
                    (root / "TODO.md").write_text("current:\ntask.md cfg:2\nother.md cfg:3\n", encoding="utf-8")
                    environment = {"OMO_AGENT_TASK_FILE": "task.md" if ambient_selection else ""}
                    with (
                        patch.dict("os.environ", environment),
                        patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:3"),
                        patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                        patch("omo_manager.omo_pending.require_pending_add_notice") as notice,
                        patch("omo_manager.omo_pending.delivered_pending_add_notice") as delivered,
                    ):
                        self.assertEqual(0, run(Args("add", ("🧑 Human request",), task_file=None if ambient_selection else "task.md"), root))
                    notice.assert_not_called()
                    delivered.assert_not_called()
                    metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                    assert metadata is not None
                    self.assertIn("🧑 Human request", metadata.pending_task_items)
                    self.assertNotIn("pending item creation notice:", task.read_text(encoding="utf-8"))

    def test_named_owner_retry_recovers_persisted_unnotified_items_once(self) -> None:
        for version in ("v1", "v2"):
            for ambient_selection in (False, True):
                with self.subTest(version=version, ambient_selection=ambient_selection), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    task = root / "task.md"
                    task.write_text(task_text() if version == "v1" else v2_task_text(), encoding="utf-8")
                    (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
                    items = ("🧑 answer the first Human request", "🧑 answer the second Human request")
                    args = Args("add", items, task_file=None if ambient_selection else "task.md")
                    environment = {"OMO_MANAGER_STATE_DIR": str(root / "state"), "OMO_AGENT_TASK_FILE": "task.md" if ambient_selection else ""}
                    with (
                        patch.dict("os.environ", environment),
                        patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                        patch("omo_manager.omo_task_context.current_tmux_target", side_effect=TaskFrontmatterError("current tmux pane cannot be identified")),
                        patch("omo_manager.omo_blocking_actor.request", side_effect=OSError("detached tool")),
                        patch("omo_manager.omo_completion_email.subprocess.run") as detached_email,
                    ):
                        self.assertEqual(0, run(args, root))
                    detached_email.assert_not_called()
                    unnotified = task.read_text(encoding="utf-8")
                    self.assertNotIn("pending item creation notice:", unnotified)
                    bodies: list[str] = []

                    def capture_email(command: list[str], check: bool) -> None:
                        self.assertEqual(unnotified, task.read_text(encoding="utf-8"))
                        bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

                    with (
                        patch.dict("os.environ", environment),
                        patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                        patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2"),
                        patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email) as email,
                    ):
                        write_target = "omo_manager.omo_pending.replace_if_unchanged" if version == "v1" else "omo_manager.omo_pending.write_document"
                        with patch(write_target, side_effect=OSError("creation marker write lost a race")):
                            with self.assertRaisesRegex(OSError, "creation marker write lost a race"):
                                run(args, root)
                        self.assertEqual(unnotified, task.read_text(encoding="utf-8"))
                        self.assertEqual(0, run(args, root))
                        recovered = task.read_text(encoding="utf-8")
                        self.assertEqual(0, run(args, root))
                        self.assertEqual(recovered, task.read_text(encoding="utf-8"))
                    email.assert_called_once()
                    self.assertEqual(["pending item created:\n- answer the first Human request\n- answer the second Human request\n"], bodies)
                    metadata = parse_task_metadata(recovered, root)
                    assert metadata is not None
                    for item in items:
                        self.assertEqual(1, metadata.pending_task_items.count(item))
                    self.assertEqual(1, recovered.count("pending item creation notice:"))

    def test_manager_named_human_add_notifies_once_without_agent_notice(self) -> None:
        for version in ("v1", "v2"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = root / "manager.md"
                text = (task_text() if version == "v1" else v2_task_text()).replace("is_manager: false", "is_manager: true")
                task.write_text(text + "Report only to your manager.\n", encoding="utf-8")
                (root / "TODO.md").write_text("current:\nmanager.md cfg:2\n", encoding="utf-8")
                args = Args("add", ("🧑 answer the Human", "agent repair"), task_file="manager.md")
                bodies: list[str] = []

                def capture_email(command: list[str], check: bool) -> None:
                    self.assertNotIn("🧑 answer the Human", task.read_text(encoding="utf-8"))
                    bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

                with (
                    patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(root / "state"), "OMO_AGENT_TASK_FILE": ""}),
                    patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                    patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2"),
                    patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email) as email,
                ):
                    self.assertEqual(0, run(args, root))
                    self.assertEqual(0, run(args, root))
                email.assert_called_once()
                self.assertEqual(["pending item created:\n- answer the Human\n"], bodies)
                metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                assert metadata is not None
                self.assertIn("🧑 answer the Human", metadata.pending_task_items)
                self.assertIn("agent repair", metadata.pending_task_items)

    def test_named_duplicate_cannot_recover_ambiguous_grouped_notice_history(self) -> None:
        for version in ("v1", "v2"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = root / "task.md"
                item = "🧑 notified in a group"
                task.write_text(task_text(items=(item,)) if version == "v1" else v2_task_text(item), encoding="utf-8")
                (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
                original = task.read_text(encoding="utf-8") + f"(pending item creation notice: {'a' * 64}:{'b' * 64})\n"
                task.write_text(original, encoding="utf-8")
                with (
                    patch.dict("os.environ", {"OMO_AGENT_TASK_FILE": ""}),
                    patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2"),
                    patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                    patch("omo_manager.omo_pending.require_pending_add_notice") as notice,
                ):
                    with self.assertRaisesRegex(BlockingError, "legacy creation notice has unknown item membership"):
                        run(Args("add", (item,), task_file="task.md"), root)
                notice.assert_not_called()
                self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_new_notice_membership_distinguishes_group_subset_from_unnotified_item(self) -> None:
        for version in ("v1", "v2"):
            for grouped_notice, initially_detached in ((True, False), (False, False), (False, True)):
                with self.subTest(version=version, grouped_notice=grouped_notice, initially_detached=initially_detached), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    task = root / "task.md"
                    task.write_text(task_text() if version == "v1" else v2_task_text(), encoding="utf-8")
                    (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
                    first, second = "🧑 first Human request", "🧑 second Human request"
                    bodies: list[str] = []

                    def capture_email(command: list[str], check: bool) -> None:
                        bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

                    with (
                        patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(root / "state"), "OMO_AGENT_TASK_FILE": ""}),
                        patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                        patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2"),
                        patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email) as email,
                    ):
                        if initially_detached:
                            with patch("omo_manager.omo_pending.authenticated_pending_task", side_effect=TaskFrontmatterError("detached tool")):
                                self.assertEqual(0, run(Args("add", (first, second), task_file="task.md"), root))
                            email.assert_not_called()
                        self.assertEqual(0, run(Args("add", (first, second) if grouped_notice else (first,), task_file="task.md"), root))
                        if not grouped_notice and not initially_detached:
                            with patch("omo_manager.omo_pending.authenticated_pending_task", side_effect=TaskFrontmatterError("detached tool")):
                                self.assertEqual(0, run(Args("add", (second,), task_file="task.md"), root))
                            self.assertEqual(1, email.call_count)
                        retry = Args("add", (second,), task_file="task.md")
                        self.assertEqual(0, run(retry, root))
                        recovered = task.read_text(encoding="utf-8")
                        self.assertEqual(0, run(retry, root))
                        self.assertEqual(recovered, task.read_text(encoding="utf-8"))
                    expected = ["pending item created:\n- first Human request\n- second Human request\n"] if grouped_notice else [
                        "pending item created:\n- first Human request\n", "pending item created:\n- second Human request\n",
                    ]
                    self.assertEqual(expected, bodies)
                    self.assertEqual(len(expected), email.call_count)
                    metadata = parse_task_metadata(recovered, root)
                    assert metadata is not None
                    self.assertEqual(1, metadata.pending_task_items.count(first))
                    self.assertEqual(1, metadata.pending_task_items.count(second))

    def test_detached_identical_readd_has_new_owner_notice_generation(self) -> None:
        for version in ("v1", "v2"):
            for grouped in (False, True):
                with self.subTest(version=version, grouped=grouped), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    task = root / "task.md"
                    task.write_text(task_text() if version == "v1" else v2_task_text(), encoding="utf-8")
                    (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
                    first, second = "🧑 repeated first request", "🧑 repeated second request"
                    items = (first, second) if grouped else (first,)
                    bodies: list[str] = []

                    def capture_email(command: list[str], check: bool) -> None:
                        bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

                    with (
                        patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(root / "state"), "OMO_AGENT_TASK_FILE": ""}),
                        patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                        patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2"),
                        patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email) as email,
                    ):
                        self.assertEqual(0, run(Args("add", items, task_file="task.md"), root))
                        if version == "v1":
                            from omo_manager.omo_task_edit import remove_pending_items as actual_remove

                            updated, _count = actual_remove(task.read_text(encoding="utf-8"), items)
                            task.write_text(updated, encoding="utf-8")
                        else:
                            from omo_manager.omo_blocking import load_task as actual_load
                            from omo_manager.omo_blocking import resolve_item as actual_resolve

                            for item in items:
                                document = actual_load(task, root=root)
                                pending = next(pending for pending in document.metadata["pending_task_items"] if pending["text"] == item)
                                actual_resolve(document, pending["id"], "completed", "test prior generation completed")
                        with patch("omo_manager.omo_pending.authenticated_pending_task", side_effect=TaskFrontmatterError("detached tool")):
                            self.assertEqual(0, run(Args("add", items, task_file="task.md"), root))
                        self.assertEqual(1, email.call_count)
                        self.assertEqual(1, task.read_text(encoding="utf-8").count("pending item creation intent:"))
                        for item in items:
                            args = Args("add", (item,), task_file="task.md")
                            write_target = "omo_manager.omo_pending.replace_if_unchanged" if version == "v1" else "omo_manager.omo_pending.write_document"
                            with patch(write_target, side_effect=OSError("intent resolution lost a race")):
                                with self.assertRaisesRegex(OSError, "intent resolution lost a race"):
                                    run(args, root)
                            self.assertEqual(0, run(args, root))
                            self.assertEqual(0, run(args, root))
                    self.assertEqual(1 + len(items), email.call_count)
                    self.assertEqual(["pending item created:\n" + "".join(f"- {item.removeprefix('🧑 ')}\n" for item in items)] + [
                        f"pending item created:\n- {item.removeprefix('🧑 ')}\n" for item in items
                    ], bodies)
                    metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
                    assert metadata is not None
                    for item in items:
                        self.assertEqual(1, metadata.pending_task_items.count(item))

    def test_detached_creation_intent_cannot_move_to_another_owner(self) -> None:
        for version in ("v1", "v2"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = root / "task.md"
                task.write_text(task_text() if version == "v1" else v2_task_text(), encoding="utf-8")
                (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
                args = Args("add", ("🧑 request assigned to original owner",), task_file="task.md")
                with (
                    patch.dict("os.environ", {"OMO_AGENT_TASK_FILE": ""}),
                    patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                    patch("omo_manager.omo_pending.authenticated_pending_task", side_effect=TaskFrontmatterError("detached tool")),
                ):
                    self.assertEqual(0, run(args, root))
                transferred = task.read_text(encoding="utf-8").replace("runat: cfg:2", "runat: cfg:3")
                task.write_text(transferred, encoding="utf-8")
                (root / "TODO.md").write_text("current:\ntask.md cfg:3\n", encoding="utf-8")
                with (
                    patch.dict("os.environ", {"OMO_AGENT_TASK_FILE": ""}),
                    patch("omo_manager.omo_pending.v2_enabled", return_value=version == "v2"),
                    patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:3"),
                    patch("omo_manager.omo_pending.require_pending_add_notice") as notice,
                ):
                    with self.assertRaisesRegex(BlockingError, "pending creation intent belongs to a different queue owner"):
                        run(args, root)
                notice.assert_not_called()
                self.assertEqual(transferred, task.read_text(encoding="utf-8"))

    def test_explicit_task_file_human_add_rejects_wrong_todo_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "task.md"
            original = task_text()
            task.write_text(original, encoding="utf-8")
            (root / "TODO.md").write_text("current:\nother.md cfg:2\n", encoding="utf-8")
            with patch("omo_manager.omo_pending.require_pending_add_notice") as notice:
                with self.assertRaises(TaskFrontmatterError):
                    run(Args("add", ("🧑 Human request",), task_file="task.md"), root)
            notice.assert_not_called()
            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_explicit_task_file_cannot_run_incident_or_human_closure_detached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "task.md"
            original = task_text(items=("🧑 preserve the original request",))
            task.write_text(original, encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.authenticated_pending_task", side_effect=TaskFrontmatterError("no authenticated owner")),
                patch("omo_manager.omo_pending.require_owner_completion") as completion,
            ):
                with self.assertRaisesRegex(TaskFrontmatterError, "no authenticated owner"):
                    run(Args("recover-source2057", task_file="task.md"), root)
                with self.assertRaisesRegex(TaskFrontmatterError, "no authenticated owner"):
                    run(Args("remove", ("🧑 preserve the original request",), task_file="task.md", evidence="completed", completion_key="a" * 64), root)
            completion.assert_not_called()
            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_pending_list_hides_source_prefix_without_changing_item_identity(self) -> None:
        source = "🧑 Human Source2230 (manager_mail/85c5dff58359-2230.txt): “Fix the pending tag.”"
        self.assertEqual("🧑 “Fix the pending tag.”", pending_display_text(source))
        self.assertEqual(source, pending_display_text(source, verbatim=True))
        new_item = parse_args(["add", "--human-authored", "--item", source.removeprefix("🧑 ")]).items[0]
        self.assertEqual("🧑 “Fix the pending tag.” (Human, manager_mail/85c5dff58359-2230.txt)", new_item)
        self.assertEqual("🧑 “Fix the pending tag.”", pending_display_text(new_item))
        self.assertEqual(new_item, pending_display_text(new_item, verbatim=True))
        named = "🧑 Human manager_mail/85c5dff58359-2214.txt: restore subject tags"
        self.assertEqual("🧑 restore subject tags", pending_display_text(named))
        embedded = "🧑 Keep Source2230 inside the Human quote unchanged"
        self.assertEqual(embedded, pending_display_text(embedded))
        self.assertTrue(parse_args(["list", "--verbatim"]).verbatim)

    def test_source2230_reviewed_sent_removes_only_two_answered_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manager_mail" / "85c5dff58359-2230.txt"
            source.parent.mkdir()
            source.write_text("Subject: pending workflow\n\nFix pending blocks.\n", encoding="utf-8")
            task = root / "config_repair_0926.md"
            text = (
                "---\nversion: v1.0.0\nstatus: long_running\n"
                "runat: config:1\ntool: codex\nmanagerat: wl:1\nis_manager: false\n"
                f"pending_task_items:\n  - existing unfinished work\n  - '{SOURCE2230_ITEMS[0]}'\n  - '{SOURCE2230_ITEMS[1]}'\n"
                "---\nbody\n(record and delegate manager_mail/85c5dff58359-2230.txt)\n"
            )
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nconfig_repair_0926.md config:1\n", encoding="utf-8")
            args = Args("recover-source2230-reviewed-sent", SOURCE2230_ITEMS)
            with (
                patch("omo_manager.omo_pending.SOURCE2230_ROOT", root),
                patch("omo_manager.omo_pending.SOURCE2230_TASK_SHA256", hashlib.sha256(text.encode()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2230_TODO_SHA256", hashlib.sha256(todo.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2230_MAIL_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=task),
                patch("omo_manager.omo_pending.verify_original_human_in_inbox", return_value=True),
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
            ):
                with self.assertRaisesRegex(BlockingError, "Sent-Mail evidence"):
                    recover_source2230_reviewed_sent(args, root)
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                sent.return_value = ("sender", "recipient")
                with redirect_stdout(StringIO()):
                    self.assertEqual(0, recover_source2230_reviewed_sent(args, root))
                with self.assertRaisesRegex(BlockingError, "task, queue, or TODO custody changed"):
                    recover_source2230_reviewed_sent(args, root)
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(("existing unfinished work",), metadata.pending_task_items)
            self.assertEqual("long_running", metadata.status)

    def test_source2234_worker_ack_records_only_one_item_without_mail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manager_mail" / "85c5dff58359-2234.txt"
            source.parent.mkdir()
            source.write_text("Subject: Re: four-site repair\n\nJust get them done. Use different providers or retry as needed.\n", encoding="utf-8")
            task = root / "cc_dw26.md"
            text = (
                "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: capacity reset\n"
                "runat: dw-manager-20260926:1\ntool: codex\nmanagerat: dw:1\nis_manager: false\n"
                "pending_task_items:\n  - preserve this CC work\n---\nbody\n"
                "(record and delegate manager_mail/85c5dff58359-2234.txt)\n"
            )
            task.write_text(text, encoding="utf-8")
            manager = root / "dw_manager_0926.md"
            manager_text = "---\nversion: v1.0.0\nstatus: long_running\nrunat: dw:1\ntool: codex\nmanagerat: wl:1\nis_manager: true\npending_task_items: []\n---\nmanage\n"
            manager.write_text(manager_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ncc_dw26.md dw-manager-20260926:1\n", encoding="utf-8")
            args = Args("recover-source2234-worker-ack", (SOURCE2234_ITEM,))
            with (
                patch("omo_manager.omo_pending.SOURCE2234_ROOT", root),
                patch("omo_manager.omo_pending.SOURCE2234_TASK_SHA256", hashlib.sha256(text.encode()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2234_TODO_SHA256", hashlib.sha256(todo.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.SOURCE2234_MAIL_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=root / "dw_manager_0926.md"),
                patch("omo_manager.omo_pending.verify_original_human_in_inbox", return_value=False) as inbox,
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=("sender", "recipient")) as sent,
            ):
                with patch("omo_manager.omo_pending.authenticated_pending_task", return_value=root / "nested" / "dw_manager_0926.md"):
                    with self.assertRaisesRegex(BlockingError, "authenticated DW manager owner"):
                        recover_source2234_worker_ack(args, root)
                    inbox.assert_not_called()
                with self.assertRaisesRegex(BlockingError, "Inbox evidence"):
                    recover_source2234_worker_ack(args, root)
                sent.assert_not_called()
                inbox.return_value = True
                todo.write_text("changed", encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "TODO custody changed"):
                    recover_source2234_worker_ack(args, root)
                todo.write_text("current:\ncc_dw26.md dw-manager-20260926:1\n", encoding="utf-8")
                manager.write_text(manager_text.replace("runat: dw:1", "runat: dw:5"), encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "TODO custody changed"):
                    recover_source2234_worker_ack(args, root)
                manager.write_text(manager_text.replace("is_manager: true", "is_manager: false"), encoding="utf-8")
                with self.assertRaisesRegex(BlockingError, "TODO custody changed"):
                    recover_source2234_worker_ack(args, root)
                manager.write_text(manager_text, encoding="utf-8")
                with redirect_stdout(StringIO()):
                    self.assertEqual(0, recover_source2234_worker_ack(args, root))
                with self.assertRaisesRegex(BlockingError, "queue, or TODO custody changed"):
                    recover_source2234_worker_ack(args, root)
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(("preserve this CC work", SOURCE2234_ITEM), metadata.pending_task_items)
            self.assertEqual("blocked", metadata.status)

    def test_weekly_memo_recovery_checks_exact_sent_and_preserves_pane(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "weekly_memo_0927.md"
            text = (
                "---\nversion: v1.0.0\nstatus: blocked\n"
                f"blocked_on: {WEEKLY_MEMO_RECOVERY_BLOCKER}\n"
                "runat: wl:3\ntool: codex\nmanagerat: wl:5\nis_manager: false\n"
                f"pending_task_items:\n  - {WEEKLY_MEMO_RECOVERY_ITEM}\n---\nbody\n"
            )
            task.write_text(text, encoding="utf-8")
            (root / "TODO.md").write_text("current:\nweekly_memo_0927.md wl:3\nprevious:\n", encoding="utf-8")
            args = Args("recover-weekly-memo-reviewed-sent", (WEEKLY_MEMO_RECOVERY_ITEM,))
            with (
                patch("omo_manager.omo_pending.WEEKLY_MEMO_RECOVERY_ROOT", root),
                patch("omo_manager.omo_pending.WEEKLY_MEMO_RECOVERY_TASK_SHA256", hashlib.sha256(text.encode()).hexdigest()),
                patch("omo_manager.omo_pending.current_pending_task", return_value=root / "config_repair_0926.md"),
                patch("omo_manager.omo_pending.verify_ordinary_completion_in_sent", return_value=None) as sent,
            ):
                with self.assertRaisesRegex(BlockingError, "Sent-Mail evidence"):
                    recover_weekly_memo_reviewed_sent(args, root)
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                sent.return_value = ("sender", "recipient")
                with redirect_stdout(StringIO()):
                    self.assertEqual(0, recover_weekly_memo_reviewed_sent(args, root))
            updated = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual((), updated.pending_task_items)
            self.assertEqual("blocked", updated.status)
            self.assertIn("verified removed pending item", task.read_text(encoding="utf-8"))

    def test_source2048_delivery_support_recovery_rejects_same_name_nested_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "nested" / "watch_err_email.md"
            path.parent.mkdir()
            path.write_text(task_text(items=(SOURCE2048_DELIVERY_SUPPORT_ITEM,)), encoding="utf-8")
            original = path.read_text(encoding="utf-8")
            args = Args(
                "recover-source2048-batch-delivery",
                (SOURCE2048_DELIVERY_SUPPORT_ITEM,),
                evidence=SOURCE2048_DELIVERY_EVIDENCE,
            )
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch(
                    "omo_manager.omo_pending.ordinary_completion_participant_evidence",
                    return_value=PARTICIPANT_EVIDENCE,
                ) as sent,
                self.assertRaises(BlockingError),
            ):
                run(args, root)
            sent.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_source2048_delivery_support_recovery_requires_sent_and_exact_paper_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "watch_err_email.md"
            paper = root / "paper_finish.md"
            path.write_text(task_text(items=(SOURCE2048_DELIVERY_SUPPORT_ITEM,)), encoding="utf-8")
            paper.write_text(task_text(items=(SOURCE2048_BROADER_ITEM,)), encoding="utf-8")
            args = Args(
                "recover-source2048-batch-delivery",
                (SOURCE2048_DELIVERY_SUPPORT_ITEM,),
                evidence=SOURCE2048_DELIVERY_EVIDENCE,
            )
            original = path.read_text(encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.ordinary_completion_participant_evidence", return_value=None),
                self.assertRaises(BlockingError),
            ):
                run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

            paper.write_text(task_text(items=(SOURCE2048_BROADER_ITEM, "unexpected item")), encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch(
                    "omo_manager.omo_pending.ordinary_completion_participant_evidence",
                    return_value=PARTICIPANT_EVIDENCE,
                ),
                self.assertRaises(BlockingError),
            ):
                run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            paper.write_text(task_text(items=(SOURCE2048_BROADER_ITEM,)), encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch(
                    "omo_manager.omo_pending.ordinary_completion_participant_evidence",
                    return_value=PARTICIPANT_EVIDENCE,
                ) as sent,
            ):
                self.assertEqual(0, run(args, root))
            sent.assert_called_once_with(
                SOURCE2048_DELIVERY_MESSAGE_ID,
                SOURCE2048_SENT_SUBJECT_SHA256,
                SOURCE2048_SENT_BODY_SHA256,
            )
            self.assertNotIn(SOURCE2048_DELIVERY_SUPPORT_ITEM, path.read_text(encoding="utf-8"))
            self.assertEqual((SOURCE2048_BROADER_ITEM,), parse_task_metadata(paper.read_text(), root).pending_task_items)

    def source2048_delivery(self, root: Path, message: str = SOURCE2048_REVIEWED_DELIVERY_MESSAGE) -> Args:
        message_path = root / "delivery-message.txt"
        message_path.write_text(message, encoding="utf-8")
        transcript = root / "sessions" / SOURCE2048_PAPER_TRANSCRIPT
        transcript.parent.mkdir(parents=True, exist_ok=True)
        wrapper = f'<agent_message from="config:4">\n{message.rstrip()}\n</agent_message>'
        records = (
            {"type": "session_meta", "payload": {"id": SOURCE2048_PAPER_SESSION, "cwd": SOURCE2048_PAPER_CWD}},
            {
                "type": "response_item",
                "payload": {"role": "user", "content": [{"type": "input_text", "text": wrapper}]},
            },
        )
        transcript.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")
        if message == SOURCE2048_REVIEWED_DELIVERY_MESSAGE:
            self.assertEqual(SOURCE2048_DELIVERY_MESSAGE_SHA256, hashlib.sha256(message.rstrip().encode()).hexdigest())
        return Args(
            "recover-source2048-batch-path",
            (SOURCE2048_BATCH_PATH_ITEM,),
            evidence=SOURCE2048_BATCH_PATH_EVIDENCE,
            delivery_transcript=transcript,
            delivery_message_file=message_path,
            delivery_message_sha256=hashlib.sha256(message.rstrip().encode()).hexdigest(),
        )

    def test_source2048_recovery_removes_only_exact_item_after_verified_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "watch_err_email.md"
            other = "retain unrelated pending work"
            original = task_text(items=(SOURCE2048_BATCH_PATH_ITEM, other))
            path.write_text(original, encoding="utf-8")
            args = self.source2048_delivery(root)

            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.CODEX_SESSIONS_ROOT", root / "sessions"),
                patch("omo_manager.omo_pending.require_owner_completion") as email,
            ):
                self.assertEqual(0, run(args, root))

            email.assert_not_called()
            updated = path.read_text(encoding="utf-8")
            self.assertNotIn(SOURCE2048_BATCH_PATH_ITEM, updated)
            self.assertIn(f"  - {other}\n", updated)
            self.assertIn(SOURCE2048_BATCH_PATH_EVIDENCE, updated)

    def test_source2048_recovery_rejects_unbound_or_missing_evidence_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "watch_err_email.md"
            original = task_text(items=(SOURCE2048_BATCH_PATH_ITEM, "retain unrelated pending work"))
            path.write_text(original, encoding="utf-8")
            valid = self.source2048_delivery(root)
            cases = (
                replace(valid, delivery_message_sha256="0" * 64),
                replace(valid, evidence="changed evidence"),
                replace(valid, items=("different item",)),
            )
            for args in cases:
                with (
                    self.subTest(args=args),
                    patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                    patch("omo_manager.omo_pending.CODEX_SESSIONS_ROOT", root / "sessions"),
                    self.assertRaises(BlockingError),
                ):
                    run(args, root)
                self.assertEqual(original, path.read_text(encoding="utf-8"))

            transcript = valid.delivery_transcript
            assert transcript is not None
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": SOURCE2048_PAPER_SESSION, "cwd": SOURCE2048_PAPER_CWD}}) + "\n",
                encoding="utf-8",
            )
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.CODEX_SESSIONS_ROOT", root / "sessions"), self.assertRaises(BlockingError):
                run(valid, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

            unrelated = self.source2048_delivery(root, "unrelated valid config:4 delivery")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.CODEX_SESSIONS_ROOT", root / "sessions"),
                self.assertRaises(BlockingError),
            ):
                run(unrelated, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_source2048_recovery_parser_requires_digest(self) -> None:
        argv = [
            "recover-source2048-batch-path",
            "--delivery-transcript",
            "/tmp/transcript",
            "--delivery-message-file",
            "/tmp/message",
            "--delivery-message-sha256",
            "BAD",
        ]
        with self.assertRaises(SystemExit):
            parse_args(argv)

    def test_source2048_same_key_refresh_sends_once_then_removes_exact_four(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "paper_finish.md"
            broader = "Guide the remaining broader paper review."
            original = task_text(items=(broader, *SOURCE2048_ITEMS)).replace(
                "runat: cfg:2",
                "runat: DeGenTWeb_writeup:0\nsession_id: 01a0bb49-92ae-70e2-a0eb-0ea7373862e7",
            )
            path.write_text(original, encoding="utf-8")
            subject = root / "subject.txt"
            message = root / "message.txt"
            subject.write_text("Re: Try Pangram for hard data\n", encoding="utf-8")
            message.write_text(SOURCE2048_FINAL_MESSAGE, encoding="utf-8")

            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch("omo_manager.omo_completion_email.current_pending_task", return_value=path):
                old = plan_completion_email(
                    root,
                    path,
                    original,
                    "pending item removed after verification",
                    items=SOURCE2048_ITEMS,
                    evidence=SOURCE2048_EVIDENCE,
                    human_subject="Re: Try Pangram for hard data",
                    human_body=SOURCE2048_FINAL_MESSAGE,
                    semantic_key=SOURCE2048_KEY,
                    pending_item_owner=True,
                )
                assert old is not None
                self.assertTrue(claim_completion_email(old))
                with self.assertRaisesRegex(OSError, "requires changed task bytes"):
                    refresh_unattempted_completion_claim(replace(old, key="f" * 64), old.key)

                current = original.replace("status: running", "status: long_running\nblocked_on: exact same-key recovery")
                path.write_text(current, encoding="utf-8")
                plan = plan_completion_email(
                    root,
                    path,
                    current,
                    "pending item removed after verification",
                    items=SOURCE2048_ITEMS,
                    evidence=SOURCE2048_EVIDENCE,
                    human_subject="Re: Try Pangram for hard data",
                    human_body=SOURCE2048_FINAL_MESSAGE,
                    semantic_key=SOURCE2048_KEY,
                    pending_item_owner=True,
                )
                assert plan is not None
                tampered = replace(plan, body="tampered reviewed answer\n")
                for operation in (
                    completion_email_is_delivered,
                    claim_completion_email,
                    send_completion_email,
                ):
                    with (
                        self.subTest(operation=operation.__name__),
                        self.assertRaisesRegex(OSError, "reviewed recovery bindings"),
                    ):
                        operation(tampered)
                with self.assertRaisesRegex(OSError, "changed its reviewed answer"):
                    refresh_unattempted_completion_claim(
                        replace(
                            plan,
                            key="d" * 64,
                            notice_key="e" * 64,
                            semantic_key="0" * 64,
                        ),
                        old.key,
                    )
                with patch("omo_manager.omo_completion_email.subprocess.run") as bypass_sender, self.assertRaisesRegex(OSError, "reviewed recovery bindings"):
                    send_completion_email(tampered)
                bypass_sender.assert_not_called()
                refresh_unattempted_completion_claim(plan, old.key)
                with patch("omo_manager.omo_completion_email.subprocess.run") as sender:
                    self.assertTrue(send_completion_email(plan))
                    self.assertFalse(send_completion_email(plan))
                command = sender.call_args.args[0]
                self.assertIn("--preserve-source2048-thread", command)
                self.assertIn("--require-human-recipient", command)

                args = Args(
                    "remove",
                    SOURCE2048_ITEMS,
                    evidence=SOURCE2048_EVIDENCE,
                    answer_subject_file=subject,
                    answer_message_file=message,
                    completion_key=SOURCE2048_KEY,
                )
                with patch("omo_manager.omo_pending.current_pending_task", return_value=path):
                    self.assertEqual(0, run(args, root))

            updated = path.read_text(encoding="utf-8")
            self.assertIn(f"  - {broader}\n", updated)
            for item in SOURCE2048_ITEMS:
                self.assertNotIn(item, updated)

    def test_add_requires_and_encodes_explicit_provenance(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["add", "--item", "review request"])
        self.assertEqual(("🧑 review request",), parse_args(["add", "--human-authored", "--item", "review request"]).items)
        self.assertEqual(("review request",), parse_args(["add", "--agent-authored", "--item", "review request"]).items)
        self.assertEqual(("review request",), parse_args(["add", "--agent-authored", "--item", "🧑 🧑 review request"]).items)
        for old_flag in ("--human", "--agent"):
            with self.subTest(old_flag=old_flag), self.assertRaises(SystemExit):
                parse_args(["add", old_flag, "--item", "review request"])

    def test_add_help_defines_provenance_by_author(self) -> None:
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            parse_args(["add", "--help"])

        help_text = " ".join(output.getvalue().split())
        self.assertIn("trusted caller assertion; helpers do not infer authorship", help_text)
        self.assertIn("who authored the pending item", help_text)
        self.assertIn("even when it asks for or awaits a Human decision", help_text)

    def test_replace_preserves_human_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(items=("🧑 old wording",)), encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path):
                self.assertEqual(0, run(Args("replace", old_item="🧑 old wording", new_item="new wording"), root))
            self.assertIn("  - 🧑 new wording\n", path.read_text(encoding="utf-8"))

    def test_add_emails_before_mutation_and_retry_does_not_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            original = task_text()
            path.write_text(original, encoding="utf-8")
            args = Args("add", ("🧑 inspect failure",), task_file="task.md")
            from omo_manager.omo_task_edit import replace_if_unchanged as actual_replace

            calls = 0

            def fail_once(target: Path, updated: str, before: os.stat_result) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    target.write_text(target.read_text(encoding="utf-8") + "unrelated note\n", encoding="utf-8")
                    raise OSError("task changed concurrently")
                actual_replace(target, updated, before)

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.replace_if_unchanged", side_effect=fail_once),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(OSError, "task changed concurrently"):
                    run(args, root)
                self.assertEqual(original + "unrelated note\n", path.read_text(encoding="utf-8"))
                self.assertEqual(0, run(args, root))

            email.assert_called_once()
            text = path.read_text(encoding="utf-8")
            self.assertIn("  - 🧑 inspect failure\n", text)
            self.assertIn("unrelated note\n", text)
            self.assertRegex(text, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}; items: [0-9a-f]{64}\)")

    def test_add_mail_failure_keeps_queue_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            original = task_text()
            path.write_text(original, encoding="utf-8")
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("mail unavailable")),
            ):
                with self.assertRaisesRegex(OSError, "not confirmed delivered"):
                    run(Args("add", ("🧑 inspect failure",), task_file="task.md"), root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_mixed_add_under_agent_scoped_no_contact_emails_only_human_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(
                task_text() + "Agent-authored pending items must not email the Human.\n",
                encoding="utf-8",
            )
            bodies: list[str] = []

            def capture_email(command: list[str], check: bool) -> None:
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email),
            ):
                self.assertEqual(0, run(Args("add", ("🧑 Human work", "agent work")), root))

            self.assertEqual(["pending item created:\n- Human work\n"], bodies)
            text = path.read_text(encoding="utf-8")
            self.assertIn("  - 🧑 Human work\n", text)
            self.assertIn("  - agent work\n", text)

    def test_agent_add_mutates_without_mail_or_notice_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(), encoding="utf-8")
            args = parse_args(["add", "--agent-authored", "--item", "inspect failure"])
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(0, run(args, root))
            plan.assert_not_called()
            require.assert_not_called()
            text = path.read_text(encoding="utf-8")
            self.assertIn("  - inspect failure\n", text)
            self.assertNotIn("pending item creation notice", text)

    def test_add_race_with_competing_same_item_finalizes_notice_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(task_text(), encoding="utf-8")
            args = Args("add", ("🧑 inspect failure",))
            from omo_manager.omo_task_edit import add_pending_items as actual_add
            from omo_manager.omo_task_edit import remove_pending_items as actual_remove
            from omo_manager.omo_task_edit import replace_if_unchanged as actual_replace

            calls = 0

            def competing_add(target: Path, updated: str, before: os.stat_result) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    race_before = target.stat()
                    raced, _count = actual_add(target.read_text(encoding="utf-8"), args.items)
                    actual_replace(target, raced, race_before)
                    raise OSError("task changed concurrently")
                actual_replace(target, updated, before)

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.replace_if_unchanged", side_effect=competing_add),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(OSError, "task changed concurrently"):
                    run(args, root)
                self.assertEqual(0, run(args, root))
                reconciled = path.read_text(encoding="utf-8")
                self.assertRegex(reconciled, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}; items: [0-9a-f]{64}\)")

                removed, _count = actual_remove(reconciled, args.items)
                path.write_text(removed, encoding="utf-8")
                self.assertEqual(0, run(args, root))

            self.assertEqual(2, email.call_count)

    def test_duplicate_add_does_not_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original = task_text(items=("🧑 inspect failure",))
            path.write_text(original, encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.require_pending_add_notice") as notice:
                self.assertEqual(0, run(Args("add", ("🧑 inspect failure",)), root))
            notice.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_mixed_existing_and_new_add_emails_only_new_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(items=("🧑 already tracked",)), encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.require_pending_add_notice", return_value=True) as notice:
                self.assertEqual(0, run(Args("add", ("🧑 already tracked", "🧑 new work")), root))
            self.assertEqual(("🧑 new work",), notice.call_args.args[3])
            text = path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("  - 🧑 already tracked\n"))
            self.assertEqual(1, text.count("  - 🧑 new work\n"))

    def test_repeated_item_in_one_add_is_rejected_before_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original = task_text()
            path.write_text(original, encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.require_pending_add_notice") as notice:
                with self.assertRaisesRegex(BlockingError, "repeated"):
                    run(Args("add", ("same work", "same work")), root)
            notice.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_v2_add_emails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            document = MagicMock()
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.v2_enabled", return_value=True),
                patch("omo_manager.omo_pending.load_task", return_value=document),
                patch("omo_manager.omo_pending.require_pending_add_notice", return_value=True) as notice,
                patch("omo_manager.omo_pending.add_items", return_value=("pi_new",)) as add,
            ):
                self.assertEqual(0, run(Args("add", ("🧑 inspect another failure",)), root))
            notice.assert_called_once()
            add.assert_called_once()
            self.assertEqual((document, ("🧑 inspect another failure",)), add.call_args.args)
            self.assertRegex(
                add.call_args.kwargs["body_comment"],
                r"^pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}; items: [0-9a-f]{64}$",
            )

    def test_v2_detached_explicit_task_file_persists_human_item_without_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "task.md"
            task.write_text(v2_task_text(), encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_task_context.current_tmux_target", side_effect=TaskFrontmatterError("current tmux pane cannot be identified")),
                patch("omo_manager.omo_blocking_actor.request", side_effect=OSError("detached tool")),
                patch("omo_manager.omo_pending.v2_enabled", return_value=True),
                patch("omo_manager.omo_pending.require_pending_add_notice") as notice,
                patch("omo_manager.omo_pending.delivered_pending_add_notice") as delivered,
            ):
                self.assertEqual(0, run(Args("add", ("🧑 Human request",), task_file="task.md"), root))
            notice.assert_not_called()
            delivered.assert_not_called()
            text = task.read_text(encoding="utf-8")
            metadata = parse_task_metadata(text, root)
            assert metadata is not None
            self.assertIn("🧑 Human request", metadata.pending_task_items)
            self.assertNotIn("pending item creation notice:", text)

    def test_v2_add_retry_after_unrelated_write_race_does_not_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            (root / ".omo-task-v2-enabled.yaml").write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")
            args = Args("add", ("🧑 inspect another failure",))
            from omo_manager.omo_blocking import write_document as actual_write

            calls = 0

            def fail_once(document: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    path.write_text(path.read_text(encoding="utf-8") + "unrelated note\n", encoding="utf-8")
                    raise BlockingError("task changed concurrently")
                actual_write(document)  # type: ignore[arg-type]

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state), "OMO_AGENT_TASK_FILE": "task.md"}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_blocking.write_document", side_effect=fail_once),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(BlockingError, "task changed concurrently"):
                    run(args, root)
                self.assertEqual(0, run(args, root))

            email.assert_called_once()
            text = path.read_text(encoding="utf-8")
            self.assertIn("text: 🧑 inspect another failure\n", text)
            self.assertIn("unrelated note\n", text)
            self.assertRegex(text, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}; items: [0-9a-f]{64}\)")

    def test_v2_add_race_with_competing_same_item_finalizes_notice_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            (root / ".omo-task-v2-enabled.yaml").write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")
            args = Args("add", ("🧑 inspect another failure",))
            from omo_manager.omo_blocking import document_with as actual_document_with
            from omo_manager.omo_blocking import load_task as actual_load
            from omo_manager.omo_blocking import resolve_item as actual_resolve
            from omo_manager.omo_blocking import write_document as actual_write

            original_body = actual_load(path, root=root).body
            calls = 0

            def competing_add(document: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    actual_write(actual_document_with(document, document.metadata, original_body))  # type: ignore[arg-type,union-attr]
                    raise BlockingError("task changed concurrently")
                actual_write(document)  # type: ignore[arg-type]

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_blocking.write_document", side_effect=competing_add),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(BlockingError, "task changed concurrently"):
                    run(args, root)
                self.assertEqual(0, run(args, root))
                reconciled = path.read_text(encoding="utf-8")
                self.assertRegex(reconciled, r"\(pending item creation notice: [0-9a-f]{64}:[0-9a-f]{64}; items: [0-9a-f]{64}\)")

                document = actual_load(path, root=root)
                item = next(item for item in document.metadata["pending_task_items"] if item["text"] == args.items[0])
                actual_resolve(document, item["id"], "completed", "race test completed")
                self.assertEqual(0, run(args, root))

            self.assertEqual(2, email.call_count)

    def test_failed_owner_email_keeps_item_and_retry_cannot_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            original = task_text(items=("🧑 finish review",)) + "Report results directly to the Human.\n"
            path.write_text(original, encoding="utf-8")
            args = Args("remove", ("🧑 finish review",), evidence="review passed", completion_key="a" * 64)
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("uncertain")) as email,
            ):
                for _ in range(2):
                    with self.assertRaisesRegex(OSError, "not confirmed delivered"):
                        run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertEqual(2, email.call_count)

    def test_mixed_remove_under_agent_scoped_no_contact_emails_only_human_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(
                task_text(items=("🧑 Human work", "agent work")) + "Agent-authored pending items must not email the Human.\n",
                encoding="utf-8",
            )
            bodies: list[str] = []

            def capture_email(command: list[str], check: bool) -> None:
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email),
            ):
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            ("🧑 Human work", "agent work"),
                            evidence="both completed",
                            completion_key="a" * 64,
                        ),
                        root,
                    ),
                )

            self.assertEqual(["pending item deleted:\n- Human work\n"], bodies)
            self.assertIn("pending_task_items: []", path.read_text(encoding="utf-8"))

    def test_recover_removal_notice_is_evidence_bound_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            evidence = "reviewed result was already reported"
            items = ("🧑 first request", "🧑 second request", "🧑 third request")
            original = (
                task_text()
                + "Agent-authored pending items must not email the Human.\n"
                + "Implement the correction without weakening explicit no-contact or delivery safeguards.\n"
                + f"(verified removed pending items: {evidence})\n"
            )
            path.write_text(original, encoding="utf-8")
            recovery = removal_recovery(path, original, items, evidence)
            args = Args("recover-removal-notice", recovery_id="test-recovery")
            bodies: list[str] = []

            def capture_email(command: list[str], check: bool) -> None:
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with (
                patch.dict(REMOVAL_NOTICE_RECOVERIES, {"test-recovery": recovery}, clear=True),
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email),
            ):
                self.assertEqual(0, run(args, root))
                self.assertEqual(0, run(args, root))

            self.assertEqual(["pending item deleted:\n- first request\n- second request\n- third request\n"], bodies)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_recover_removal_notice_failure_keeps_completed_queue_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            evidence = "reviewed result was already reported"
            original = task_text() + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            recovery = removal_recovery(path, original, ("🧑 request",), evidence)
            args = Args("recover-removal-notice", recovery_id="test-recovery")
            with (
                patch.dict(REMOVAL_NOTICE_RECOVERIES, {"test-recovery": recovery}, clear=True),
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("mail unavailable")),
            ):
                with self.assertRaisesRegex(OSError, "not confirmed delivered"):
                    run(args, root)

            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_recover_removal_notice_rejects_changed_or_blanket_no_contact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            evidence = "reviewed result was already reported"
            original = task_text() + "Never email the Human.\n" + f"(verified removed pending item: {evidence})\n"
            path.write_text(original, encoding="utf-8")
            recovery = removal_recovery(path, original, ("🧑 request",), evidence)
            base = Args("recover-removal-notice", recovery_id="test-recovery")
            with (
                patch.dict(REMOVAL_NOTICE_RECOVERIES, {"test-recovery": recovery}, clear=True),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                with self.assertRaisesRegex(BlockingError, "removal evidence"):
                    REMOVAL_NOTICE_RECOVERIES["test-recovery"] = removal_recovery(path, original, ("🧑 request",), "different evidence")
                    run(base, root)
                REMOVAL_NOTICE_RECOVERIES["test-recovery"] = recovery
                with self.assertRaisesRegex(BlockingError, "blanket no-contact"):
                    run(base, root)
                with self.assertRaisesRegex(BlockingError, "digest changed"):
                    REMOVAL_NOTICE_RECOVERIES["test-recovery"] = RemovalNoticeRecovery(
                        recovery.task_name,
                        "e" * 64,
                        recovery.items,
                        recovery.evidence,
                        recovery.completion_key,
                    )
                    run(base, root)
            email.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_remove_with_missing_completion_entrypoint_does_not_mutate_or_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            entrypoint = root / "omo_completion_email.py"
            original = task_text(items=("🧑 finish review",))
            path.write_text(original, encoding="utf-8")
            entrypoint.write_text("#!/bin/sh\n", encoding="utf-8")
            entrypoint.chmod(0o600)
            state = root / "state"
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.COMPLETION_ENTRYPOINT", entrypoint),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.EMAIL_HELPER", root / "must-not-run-email-helper"),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=AssertionError("must not email")),
            ):
                with self.assertRaisesRegex(OSError, "not safely executable"):
                    run(Args("remove", ("🧑 finish review",), evidence="review passed", completion_key="a" * 64), root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertFalse((state / "completion-email-claims.tsv").exists())

    def test_remove_help_explains_single_email_answer_workflow(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(output):
            parse_args(["remove", "--help"])
        self.assertIn(
            "answer a human question and remove one or more exact Human-authored pending items with one email",
            " ".join(output.getvalue().split()),
        )

    def test_emailing_remove_requires_lowercase_sha256_completion_key(self) -> None:
        base = ["remove", "--item", "🧑 finish review", "--evidence", "review passed"]
        for value in ("not-a-digest", "A" * 64):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                parse_args([*base, "--completion-key", value])
        with self.assertRaises(SystemExit):
            parse_args(base)
        self.assertEqual("a" * 64, parse_args([*base, "--completion-key", "a" * 64]).completion_key)
        self.assertEqual("", parse_args(["remove", "--item", "legacy item", "--evidence", "done"]).completion_key)

    def test_combined_answer_remove_accepts_unique_human_items_only(self) -> None:
        base = [
            "remove",
            "--item",
            "🧑 result one",
            "--item",
            "🧑 result two",
            "--evidence",
            "reviewed",
            "--completion-key",
            "a" * 64,
            "--answer-subject-file",
            "/tmp/subject.txt",
            "--answer-message-file",
            "/tmp/message.txt",
        ]
        self.assertEqual(("🧑 result one", "🧑 result two"), parse_args(base).items)
        with self.assertRaises(SystemExit):
            parse_args([*base, "--item", "agent work"])
        with self.assertRaises(SystemExit):
            parse_args([*base, "--item", "🧑 result one"])

    def test_reconcile_sent_remove_requires_exact_human_item_and_sent_evidence(self) -> None:
        base = [
            "reconcile-sent-remove",
            "--item",
            "🧑 finish review",
            "--evidence",
            "review passed",
            "--completion-key",
            "a" * 64,
            "--message-id",
            "<sent@example.test>",
            "--sent-subject-sha256",
            "b" * 64,
            "--sent-body-sha256",
            "c" * 64,
        ]
        args = parse_args(base)
        self.assertEqual("reconcile-sent-remove", args.command)
        self.assertEqual(("🧑 finish review",), args.items)
        for changed in (
            [value if value != "🧑 finish review" else "agent work" for value in base],
            [value if value != "<sent@example.test>" else "bad" for value in base],
            [value if value != "b" * 64 else "B" * 64 for value in base],
            [*base[:3], "--item", "🧑 finish review", *base[3:]],
        ):
            with self.subTest(changed=changed), self.assertRaises(SystemExit):
                parse_args(changed)

    def test_reconcile_sent_remove_verifies_before_removing_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            item = "🧑 finish review"
            path.write_text(task_text(items=(item,)) + "Do not send another Human email.\n", encoding="utf-8")
            args = Args(
                "reconcile-sent-remove",
                (item,),
                evidence="review passed",
                completion_key="a" * 64,
                message_id="<sent@example.test>",
                sent_subject_sha256="b" * 64,
                sent_body_sha256="c" * 64,
            )
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=PARTICIPANT_EVIDENCE) as verify,
                patch(
                    "omo_manager.omo_completion_email.configured_agent_mail",
                    return_value=MagicMock(agent_address="agent@example.test", human_address="human@example.test"),
                ) as mail_config,
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=AssertionError("must not email")),
            ):
                self.assertEqual(0, run(args, root))

            self.assertNotIn(item, path.read_text(encoding="utf-8").split("---", 2)[1])
            verify.assert_called_once_with(
                "<sent@example.test>",
                "b" * 64,
                "c" * 64,
                required_items=("🧑 finish review",),
                required_evidence="review passed",
                require_record=True,
                required_task_tag="task",
            )
            mail_config.assert_not_called()

    def test_reconcile_sent_remove_validates_items_before_binding_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(items=("🧑 finish review",)), encoding="utf-8")
            args = Args(
                "reconcile-sent-remove",
                ("🧑 misspelled item",),
                evidence="review passed",
                completion_key="a" * 64,
                message_id="<sent@example.test>",
                sent_subject_sha256="b" * 64,
                sent_body_sha256="c" * 64,
            )
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.reconcile_ordinary_sent_completion") as reconcile,
                self.assertRaisesRegex(TaskFrontmatterError, "not found"),
            ):
                run(args, root)
            reconcile.assert_not_called()

    def test_reconcile_sent_remove_rejects_duplicate_legacy_item_before_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            item = "🧑 finish review"
            path.write_text(task_text(items=(item, item)), encoding="utf-8")
            args = Args(
                "reconcile-sent-remove",
                (item,),
                evidence="review passed",
                completion_key="a" * 64,
                message_id="<sent@example.test>",
                sent_subject_sha256="b" * 64,
                sent_body_sha256="c" * 64,
            )
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.reconcile_ordinary_sent_completion") as reconcile,
                self.assertRaisesRegex(BlockingError, "exactly once"),
            ):
                run(args, root)
            reconcile.assert_not_called()

    def test_recover_removal_notice_requires_human_items_and_exact_digests(self) -> None:
        base = ["recover-removal-notice", "--recovery-id", SOURCE1929_RECOVERY_ID]
        args = parse_args(base)
        self.assertEqual("recover-removal-notice", args.command)
        self.assertEqual(SOURCE1929_RECOVERY_ID, args.recovery_id)
        for changed in (
            ["recover-removal-notice", "--recovery-id", "unknown"],
            [*base, "--item", "🧑 forged item"],
            [*base, "--completion-key", "a" * 64],
        ):
            with self.subTest(changed=changed), self.assertRaises(SystemExit):
                parse_args(changed)

    def test_legacy_remove_no_email_preserves_evidence_without_mail_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            path.write_text(task_text(items=("finish review",)) + "Report results directly to the Human.\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            ("finish review",),
                            evidence="review passed",
                            outcome="completed",
                            no_email=True,
                        ),
                        root,
                    ),
                )
            plan.assert_not_called()
            require.assert_not_called()
            text = path.read_text(encoding="utf-8")
            self.assertIn("pending_task_items: []", text)
            self.assertIn("verified removed pending item: review passed", text)

            from omo_manager.omo_completion_email import reconcile_ordinary_sent_completion
            from omo_manager.omo_completion_email import require_owner_completion as actual_require
            from omo_manager.omo_task_status import Args as StatusArgs
            from omo_manager.omo_task_status import StopArgs
            from omo_manager.omo_task_status import run as status_run

            message_id = "<legacy-completion@example.test>"
            digest = "b" * 64
            semantic_key = "a" * 64
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_completion_email.current_active_task", return_value=path),
                patch("omo_manager.omo_completion_email.verify_ordinary_completion_in_sent", return_value=PARTICIPANT_EVIDENCE),
                patch("omo_manager.omo_task_status.require_owner_completion", side_effect=actual_require),
                patch(
                    "omo_manager.omo_task_status.stop_done_agent",
                    return_value=(StopArgs("cfg:2", 10.0, 2000, False, False, root, "task.md", True, 0.0), "session-1"),
                ),
                patch("omo_manager.omo_task_status.record_close"),
                patch("omo_manager.omo_tmux_send.send_system_to_codex") as queue,
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                reconcile_ordinary_sent_completion(
                    root,
                    path,
                    "legacy pending items removed without email",
                    message_id,
                    digest,
                    digest,
                    semantic_key=semantic_key,
                )
                self.assertEqual(2, status_run(StatusArgs(root, Path("task.md"), "done", "", completion_key=semantic_key)))
                self.assertEqual(0, status_run(StatusArgs(root, Path("task.md"), "done", "", completion_key=semantic_key)))
            queue.assert_not_called()
            email.assert_called_once()
            self.assertIn("status: done\n", path.read_text(encoding="utf-8"))

    def test_agent_or_legacy_remove_mutates_without_mail_or_completion_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_text(items=("agent work",)), encoding="utf-8")
            args = parse_args(["remove", "--item", "agent work", "--evidence", "verified done"])
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(0, run(args, root))
            plan.assert_not_called()
            require.assert_not_called()
            self.assertNotIn("agent work", path.read_text(encoding="utf-8").split("---", 2)[1])

    def test_legacy_remove_matches_displayed_text_from_quoted_colon_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            item = "fresh residual review: exact bookkeeping candidate"
            path.write_text(task_text(items=(f"'{item}'", "keep this")), encoding="utf-8")

            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(0, run(Args("remove", (item,), evidence="review passed", no_email=True), root))

            plan.assert_not_called()
            require.assert_not_called()
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(item, text.split("---", 2)[1])
            self.assertIn("  - keep this\n", text)

    def test_legacy_remove_no_email_failure_does_not_call_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original = task_text(items=("finish review",))
            path.write_text(original, encoding="utf-8")
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                with self.assertRaisesRegex(TaskFrontmatterError, "pending task item not found"):
                    run(Args("remove", ("different item",), evidence="not applicable", no_email=True), root)
            plan.assert_not_called()
            require.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_legacy_remove_no_email_rejects_email_options(self) -> None:
        for option in ("--answer-subject-file", "--answer-message-file"):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parse_args(
                    [
                        "remove",
                        "--item",
                        "finish review",
                        "--evidence",
                        "review passed",
                        "--no-email",
                        option,
                        "answer.txt",
                    ]
                )

    def test_human_remove_no_email_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["remove", "--item", "🧑 answer request", "--evidence", "already sent", "--no-email"])

        with self.assertRaisesRegex(ValueError, "Human-authored"):
            run(Args("remove", ("🧑 answer request",), evidence="already sent", no_email=True), Path("/unused"))

    def test_legacy_remove_accepts_documented_outcome(self) -> None:
        args = parse_args(
            [
                "remove",
                "--item",
                "finish review",
                "--evidence",
                "review passed",
                "--outcome",
                "completed",
                "--completion-key",
                "a" * 64,
            ]
        )
        self.assertEqual("completed", args.outcome)
        self.assertFalse(args.no_email)

    def test_remove_with_answer_files_sends_once_and_removes_exact_human_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            remove = tuple(f"🧑 Source-2048 result {index}" for index in range(4))
            keep = "broader paper review"
            path.write_text(task_text(items=(*remove, keep)), encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            sent: list[tuple[str, str]] = []

            def capture_email(command: list[str], check: bool) -> None:
                sent.append(
                    (
                        Path(command[command.index("--subject-file") + 1]).read_text(encoding="utf-8"),
                        Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"),
                    )
                )

            args = Args(
                "remove",
                remove,
                evidence="reviewed final result",
                answer_subject_file=subject,
                answer_message_file=message,
                completion_key="c" * 64,
            )
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=capture_email),
            ):
                self.assertEqual(0, run(args, root))
                with self.assertRaisesRegex(TaskFrontmatterError, "not found"):
                    run(args, root)

            self.assertEqual(1, len(sent))
            sent_subject, sent_body = sent[0]
            self.assertEqual("Re: Original question\n", sent_subject)
            self.assertIn("The concise answer.\n\nCompletion record:\n", sent_body)
            self.assertIn('"evidence":"reviewed final result"', sent_body)
            for item in remove:
                self.assertIn(item.removeprefix("🧑 "), sent_body)
            self.assertNotIn(keep, sent_body)
            remaining = path.read_text(encoding="utf-8")
            self.assertIn(f"  - {keep}\n", remaining)
            for item in remove:
                self.assertNotIn(item, remaining)

    def test_remove_with_answer_failure_preserves_all_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            items = ("🧑 result one", "🧑 result two", "keep this")
            original = task_text(items=items)
            path.write_text(original, encoding="utf-8")
            subject.write_text("Final result\n", encoding="utf-8")
            message.write_text("Reviewed answer.\n", encoding="utf-8")
            args = Args(
                "remove",
                items[:2],
                evidence="reviewed",
                answer_subject_file=subject,
                answer_message_file=message,
                completion_key="d" * 64,
            )
            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.current_pending_task", return_value=path),
                patch("omo_manager.omo_completion_email.subprocess.run", side_effect=OSError("mail failed")),
                self.assertRaisesRegex(OSError, "not confirmed delivered"),
            ):
                run(args, root)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_remove_with_answer_refuses_before_mutation_when_reporting_is_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            original = task_text(items=("🧑 answer question",))
            path.write_text(original, encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            with patch("omo_manager.omo_pending.current_pending_task", return_value=path), patch("omo_manager.omo_pending.plan_completion_email", return_value=None):
                with self.assertRaisesRegex(BlockingError, "not allowed by this task's reporting policy"):
                    run(
                        Args(
                            "remove",
                            ("🧑 answer question",),
                            evidence="answered",
                            answer_subject_file=subject,
                            answer_message_file=message,
                            completion_key="c" * 64,
                        ),
                        root,
                    )
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_v2_remove_sends_exact_owner_completion_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task_text("🧑 finish review"), encoding="utf-8")
            document = MagicMock(metadata={"resolved_task_items": []})
            email = object()
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.authenticated_pending_task", return_value=path),
                patch("omo_manager.omo_pending.v2_enabled", return_value=True),
                patch("omo_manager.omo_pending.load_task", return_value=document),
                patch("omo_manager.omo_pending.resolve_item") as resolve,
                patch("omo_manager.omo_pending.blocking_request"),
                patch("omo_manager.omo_pending.plan_completion_email", return_value=email) as plan,
                patch("omo_manager.omo_pending.require_owner_completion", return_value=True) as require,
            ):
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            evidence="review passed",
                            item_id="pi_019f0000-0000-7000-8000-000000000002",
                            outcome="completed",
                            completion_key="d" * 64,
                        ),
                        root,
                    ),
                )
            resolve.assert_called_once_with(document, "pi_019f0000-0000-7000-8000-000000000002", "completed", "review passed")
            self.assertEqual(("🧑 finish review",), plan.call_args.kwargs["items"])
            self.assertEqual("review passed", plan.call_args.kwargs["evidence"])
            self.assertEqual("d" * 64, plan.call_args.kwargs["semantic_key"])
            self.assertEqual("d" * 64, require.call_args.kwargs["semantic_key"])
            require.assert_called_once()

    def test_v2_human_item_id_rejects_detached_replace_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "task.md"
            original = v2_task_text("🧑 finish review")
            path.write_text(original, encoding="utf-8")
            document = MagicMock(metadata={"resolved_task_items": []})
            item_id = "pi_019f0000-0000-7000-8000-000000000002"
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.authenticated_pending_task", side_effect=TaskFrontmatterError("no authenticated owner")),
                patch("omo_manager.omo_pending.v2_enabled", return_value=True),
                patch("omo_manager.omo_pending.load_task", return_value=document),
                patch("omo_manager.omo_pending.replace_item") as replace,
                patch("omo_manager.omo_pending.resolve_item") as resolve,
                patch("omo_manager.omo_pending.require_owner_completion") as mail,
            ):
                with self.assertRaisesRegex(TaskFrontmatterError, "no authenticated owner"):
                    run(Args("replace", item_id=item_id, new_item="forget the request", task_file="task.md"), root)
                with self.assertRaisesRegex(TaskFrontmatterError, "no authenticated owner"):
                    run(Args("remove", item_id=item_id, outcome="cancelled", evidence="not needed", completion_key="a" * 64, task_file="task.md"), root)
            replace.assert_not_called()
            resolve.assert_not_called()
            mail.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_v2_agent_remove_mutates_without_mail_or_completion_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task_text(), encoding="utf-8")
            document = MagicMock(metadata={"resolved_task_items": []})
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_pending.v2_enabled", return_value=True),
                patch("omo_manager.omo_pending.load_task", return_value=document),
                patch("omo_manager.omo_pending.resolve_item") as resolve,
                patch("omo_manager.omo_pending.blocking_request"),
                patch("omo_manager.omo_pending.plan_completion_email") as plan,
                patch("omo_manager.omo_pending.require_owner_completion") as require,
            ):
                self.assertEqual(
                    0,
                    run(
                        Args(
                            "remove",
                            evidence="review passed",
                            item_id="pi_019f0000-0000-7000-8000-000000000002",
                            outcome="completed",
                        ),
                        root,
                    ),
                )
            resolve.assert_called_once_with(document, "pi_019f0000-0000-7000-8000-000000000002", "completed", "review passed")
            plan.assert_not_called()
            require.assert_not_called()

    def test_v2_remove_with_answer_files_is_rejected_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            subject = root / "subject.txt"
            message = root / "message.txt"
            path.write_text(v2_task_text("🧑 finish review"), encoding="utf-8")
            subject.write_text("Re: Original question\n", encoding="utf-8")
            message.write_text("The concise answer.\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "requires legacy --item"):
                run(
                    Args(
                        "remove",
                        evidence="answered",
                        item_id="pi_019f0000-0000-7000-8000-000000000002",
                        outcome="completed",
                        answer_subject_file=subject,
                        answer_message_file=message,
                        completion_key="e" * 64,
                    ),
                    root,
                )

    def test_inference_rejects_current_previous_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")
            (root / "current.md").write_text(task_text("long_running"), encoding="utf-8")
            (root / "old.md").write_text(task_text(), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_active_task(root, "cfg:2.0")

    def test_pending_inference_prefers_runnable_owner_over_blocked_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")
            (root / "current.md").write_text(task_text("long_running"), encoding="utf-8")
            (root / "old.md").write_text(task_text("blocked"), encoding="utf-8")

            self.assertEqual(root / "current.md", infer_pending_task(root, "cfg:2.0"))

    def test_pending_inference_rejects_two_runnable_owners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md cfg:2\nb.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")
            (root / "a.md").write_text(task_text("running"), encoding="utf-8")
            (root / "b.md").write_text(task_text("long_running"), encoding="utf-8")
            (root / "old.md").write_text(task_text("blocked"), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_pending_task(root, "cfg:2")

    def test_pending_inference_rejects_two_blocked_owners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\na.md cfg:2\n\nprevious:\nb.md cfg:2\n", encoding="utf-8")
            (root / "a.md").write_text(task_text("blocked"), encoding="utf-8")
            (root / "b.md").write_text(task_text("blocked"), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_pending_task(root, "cfg:2")

    def test_human_item_removal_uses_runnable_owner_among_blocked_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "current.md"
            blocked = root / "old.md"
            item = "🧑 answer the human"
            task.write_text(task_text("running", (item,)), encoding="utf-8")
            blocked.write_text(task_text("blocked"), encoding="utf-8")
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")

            with (
                patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}),
                patch("omo_manager.omo_task_context.current_tmux_target", return_value="cfg:2.0"),
                patch("omo_manager.omo_completion_email.subprocess.run") as email,
            ):
                self.assertEqual(
                    0,
                    run(
                        Args("remove", (item,), evidence="answer delivered", completion_key="a" * 64),
                        root,
                    ),
                )

            self.assertNotIn(item, task.read_text(encoding="utf-8"))
            email.assert_called_once()

    def test_inference_accepts_one_long_running_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n", encoding="utf-8")
            (root / "current.md").write_text(task_text("long_running"), encoding="utf-8")

            self.assertEqual(root / "current.md", infer_active_task(root, "cfg:2.0"))

    def test_inference_rejects_ambiguous_noncurrent_queues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("human pending:\na.md cfg:2\nb.md cfg:2\n", encoding="utf-8")
            (root / "a.md").write_text(task_text(), encoding="utf-8")
            (root / "b.md").write_text(task_text(), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_active_task(root, "cfg:2")

    def test_agent_add_list_replace_remove_is_path_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "secret-task.md"
            (root / "TODO.md").write_text("current:\nsecret-task.md cfg:2\n", encoding="utf-8")
            path.write_text(task_text("long_running"), encoding="utf-8")
            output = StringIO()
            with (
                patch("omo_manager.omo_pending.current_pending_task", return_value=path),
                patch("omo_manager.omo_task_edit.frontmatter_parts", wraps=frontmatter_parts) as parse_parts,
                patch("omo_manager.omo_pending.plan_completion_email", return_value=None),
                patch("omo_manager.omo_pending.require_owner_completion", return_value=True) as require,
                redirect_stdout(output),
            ):
                self.assertEqual(0, run(Args("add", ("inspect failure",)), root))
                self.assertEqual(0, run(Args("list"), root))
                self.assertEqual(0, run(Args("replace", old_item="inspect failure", new_item="repair failure"), root))
                self.assertEqual(0, run(Args("remove", ("repair failure",), evidence="verified fixed"), root))
            require.assert_not_called()
            self.assertGreaterEqual(parse_parts.call_count, 3)
            self.assertNotIn("secret-task", output.getvalue())
            text = path.read_text(encoding="utf-8")
            self.assertIn("verified removed pending item: verified fixed", text)
            self.assertIn("pending_task_items: []", text)


if __name__ == "__main__":
    unittest.main()
