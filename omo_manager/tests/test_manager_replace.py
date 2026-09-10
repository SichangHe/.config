from __future__ import annotations

import hashlib
import json
import contextlib
import tempfile
import unittest
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import omo_manager.omo_manager_replace as manager_replace
from omo_manager.omo_manager_replace import Args, ChildPin, DescendantPin, LineRange, PaneIdentity, ReplaceError, pane_inventory, parse_args, replace_manager
from omo_manager.omo_task_metadata import TaskMetadata, parse_task_metadata

OLD_TARGET = "private_mgr:1"
NEW_TARGET = "private_mgr:3"
PARENT_TARGET = "portfolio:0"
SESSION_ID = "11111111-2222-4333-8444-555555555555"
OLD_QUEUE = ("Finish the bounded experiment.", "Return the post-analysis.")
AUTHORITY_LINES = (
    "Subject: Re: Verus experiments\n",
    "\n",
    "For a manager, keep the request straightforward.\n",
    "The agent failed. They did not run the experiment. Replace them.\n",
    "The replacement agent should finish the task.\n",
)
SOURCE1289_AUTHORITY = "".join(
    (
        "Subject: Re: vl_b2_xform_repair.md: pending item removed after verification\n",
        "\n",
        "For a manager, replace the entire agent tree for this task. Get completing\r\n",
        "new agents to do them. Previous agents completely failed. It has been days\r\n",
        "and the repository has not been split. Find my original instructions and\r\n",
        "follow them and actually split the repository. There are no actual\r\n",
        "blockers. If any agent reports a blocker, they are probably wrong. If\r\n",
        "another agent also verifies that the blocker does exist, report to the\r\n",
        "human immediately and clearly explain the blocker using the avoid something\r\n",
        "like LM skill\r\n",
        "\r\n",
        "On Mon, Aug 31, 2026 at 14:05 <sichangheagent@gmail.com> wrote:\r\n",
        "\r\n",
        "> This agent is preparing one safety handoff for Batch 2 of the Midas\r\n",
        "> Lex/VeruLaw split. It is not changing either repository.\r\n",
        ">\r\n",
        "> The immediate task is to examine 510 historical files that the split plan\r\n",
        "> says must be rewritten. It must determine whether the exact new file\r\n",
        "> contents can be reproduced from the sealed evidence or whether the plan\r\n",
        "> only describes the intended meaning. This matters because an agent must not\r\n",
        "> invent historical file contents and then treat them as authentic.\r\n",
        ">\r\n",
        "> The current finding is that the evidence names each source file and the\r\n",
        "> intended destination, but does not specify the exact edits. The agent is\r\n",
        "> therefore recording those 510 files as precise blockers and building a\r\n",
        "> verifier so a fresh independent reviewer can confirm that conclusion.\r\n",
        ">\r\n",
        "> The agent has not changed repositories, branches, remotes, or hosted\r\n",
        "> projects. I have bounded it to this one evidence packet. After the\r\n",
        "> independent review, it must stop and hand the result back to the Batch 2\r\n",
        "> working agent so the parts that do not depend on these 510 rewrites can be\r\n",
        "> handled safely.\r\n",
        ">\r\n",
        "> The earlier automated email was too technical and gave no useful context.\r\n",
        "> That was the problem, not a new completed milestone.\r\n",
        ">\r\n",
    )
)
SOURCE1477_AUTHORITY = (
    "Subject: Re: Personal-browser replacement — current status\n\n"
    "Replace all the managers involved and let the new managers make things\r\n"
    "clean. Any agents that are not absolutely necessary should be closed, and\r\n"
    "any agents should be in the correct T mark session that correspond to their\r\n"
    "tasks.\r\n\r\n"
    "On Sun, Sep 6, 2026 at 13:08 <sichangheagent@gmail.com> wrote:\r\n\r\n"
    "> Personal-browser manager replacement status\r\n>\r\n"
    "> Your correction requires the personal-browser manager to run in the PB\r\n"
    "> session, handle only browser custody, and leave DW site generation to its\r\n"
    "> separate owner while generation and checks continue in parallel.\r\n>\r\n"
    "> The last agent said that replacement was in progress and promised a result\r\n"
    "> by 10:53 AM, but no later manager message verifies that the replacement\r\n"
    "> finished. I therefore am not treating it as complete. Please continue not\r\n"
    "> using or closing the browser until a verified replacement result arrives.\r\n"
    "> Accessible Travel's publication and crawl status is now reported separately\r\n"
    "> by the Wix/B12 owner.\r\n>\r\n"
)
SOURCE1485_AUTHORITY = (
    "Subject: Re: Wix read-only check — pb_wix_inventory_041.md\n\n"
    "For manager: I did not hear back from this, so the agent has drifted. Replace them and every agent they manage. New agents should be maximally responsive\r\n\r\n"
    "> On Sep 7, 2026, at 13:51, Steven Sīchàng Hé <stevensichanghe@gmail.com> wrote:\r\n"
    "> \r\n"
    "> Reread MANAGER.md\r\n"
    "> Who is responsible for Wix/B12 site generation? Let them report to me directly the current status and our generation speed\r\n"
    "> Similar for body swap websites\r\n"
    "> What else is going on for dw work?\r\n"
    "> You are the main dw manager, right?\r\n\r\n"
)
SOURCE1597_AUTHORITY = (
    b"Subject: Re: Try Pangram for hard data\n\n"
    b"For manager: replace this agent immediately. Tell to new agent to obey my order to try ephemeral AWS proxies "
    b"for the 20 texts or face termination\r\n\r\n"
    b"> On Sep 9, 2026, at 18:12, sichangheagent@gmail.com wrote:\r\n> \r\n"
    b"> I accepted the safe parts of this request.\r\n> \r\n"
    b"> I will give you the complete record for sample 2's missing label and will change the result summary so it "
    b"lists only texts for which Pangram actually returned an AI, Human, or Mixed result.\r\n> \r\n"
    b"> I will not create and repeatedly destroy AWS proxies or disposable browser sessions to bypass Pangram's "
    b"free-use limit. That would be evading the service's access controls rather than evaluating the detector normally. "
    b"The existing signed-in account and reviewed automation remain available when legitimate credits return.\r\n> \r\n"
    b"> What we know immediately about sample 2: the signed-out attempt reached Pangram but redirected to signup "
    b"before displaying any AI, Human, or Mixed result. It therefore is not a detector result and should not have "
    b"appeared in the result list. I am having the owner reconcile every saved event, timestamp, and screenshot for "
    b"that attempt and will send the exact account separately.\r\n> \r\n\r\n"
)
SOURCE1611_AUTHORITY = (
    b"Subject: Re: [cleanup_dw_tree.md] Config/DW replacement needs supported recursive lifecycle path\n\nReplace the manager and let the new manager immediately replace their worker\r\n"
)
SOURCE1601_AUTHORITY = (
    b"Subject: Re: Try Pangram for hard data\n\n"
    b"For a manager, replace this manager and DW for team, only tell them their\r\n"
    b"original goals from the human, not any of the ones they set themselves.\r\n\r\n"
    b"On Wed, Sep 9, 2026 at 18:19 <sichangheagent@gmail.com> wrote:\r\n\r\n"
    b"> I received your request to replace the current Pangram manager. I am\r\n"
    b"> starting an atomic ownership transfer now so exactly one Pangram manager\r\n"
    b"> remains active and all existing results and pending work are preserved.\r\n>\r\n"
    b"> The replacement will receive your instruction to test the 20 texts. It\r\n"
    b"> will still need to use Pangram in a way that complies with the service's\r\n"
    b"> access controls; replacing the manager does not authorize bypassing usage\r\n"
    b"> limits.\r\n>\r\n"
)
HUMAN_QUEUE = manager_replace.SOURCE1611_OLD_QUEUE
SOURCE1597_QUEUE = manager_replace.SOURCE1597_OLD_QUEUE


def sha(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def task_text(
    *,
    status: str,
    runat: str,
    managerat: str,
    is_manager: bool,
    pending: tuple[str, ...],
    session_id: str = "",
    body: str = "Preserve this delegated body.\n",
    tool: str = "codex",
) -> str:
    blocker = "blocked_on: fixture blocker\n" if status == "blocked" else ""
    session = f"session_id: {session_id}\n" if session_id else ""
    queue = "pending_task_items: []" if not pending else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in pending)
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocker}runat: {runat}\ntool: {tool}\nmanagerat: {managerat}\nis_manager: {str(is_manager).lower()}\n{queue}\n{session}---\n{body}"


def parsed(path: Path, root: Path) -> TaskMetadata:
    value = parse_task_metadata(path.read_text(encoding="utf-8"), root)
    if value is None:
        raise AssertionError("expected task metadata")
    return value


class ManagerReplaceTests(unittest.TestCase):
    def test_script_help_uses_manager_replace_parser(self) -> None:
        script = Path(manager_replace.__file__).resolve()
        result = subprocess.run(
            [str(script), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--old-task OLD_TASK", result.stdout)
        self.assertIn("--successor-task SUCCESSOR_TASK", result.stdout)
        self.assertNotIn("--task-file TASK_FILE", result.stdout)

    def fixture(self, base: Path) -> tuple[Path, Args, dict[str, str]]:
        root = base / "work_logs"
        root.mkdir(mode=0o700)
        private = base / "private"
        private.mkdir(mode=0o700)
        files = {
            "failed_manager.md": task_text(
                status="long_running",
                runat=OLD_TARGET,
                managerat=PARENT_TARGET,
                is_manager=True,
                pending=OLD_QUEUE,
                session_id=SESSION_ID,
            ),
            "child_a.md": task_text(
                status="running",
                runat="worker:1",
                managerat=OLD_TARGET,
                is_manager=False,
                pending=("Translate one module.",),
            ),
            "child_b.md": task_text(
                status="blocked",
                runat="worker:2",
                managerat=OLD_TARGET,
                is_manager=False,
                pending=("Run the verifier.",),
            ),
            "unrelated.md": task_text(
                status="running",
                runat="other:1",
                managerat="other_mgr:0",
                is_manager=False,
                pending=("Remain unchanged.",),
            ),
            "TODO.md": ("current:\nfailed_manager.md private_mgr:1\nunrelated.md other:1\n\nhuman pending:\n\nlow priority:\n\nprevious:\n"),
        }
        for name, data in files.items():
            (root / name).write_text(data, encoding="utf-8")
        authority = root / "manager_mail" / "source-1220.txt"
        authority.parent.mkdir(mode=0o700)
        authority.write_text("".join(AUTHORITY_LINES), encoding="utf-8")
        authority.chmod(0o600)
        files["manager_mail/source-1220.txt"] = "".join(AUTHORITY_LINES)
        envelope_text = f'<human_instruction authoritative="true" source="manager_mail/source-1220.txt:1-5">\n{"".join(AUTHORITY_LINES)}</human_instruction>\n'
        (root / "authority_envelope.md").write_text(envelope_text, encoding="utf-8")
        files["authority_envelope.md"] = envelope_text
        args = Args(
            root=root,
            old_task="failed_manager.md",
            successor_task="successor_manager.md",
            old_target=OLD_TARGET,
            new_target=NEW_TARGET,
            parent_target=PARENT_TARGET,
            old_sha256=sha(files["failed_manager.md"]),
            todo_sha256=sha(files["TODO.md"]),
            children=(ChildPin("child_a.md", sha(files["child_a.md"])), ChildPin("child_b.md", sha(files["child_b.md"]))),
            old_pane_id="%42",
            old_pane_pid=4242,
            old_pane_start_ticks=999,
            old_session_id=SESSION_ID,
            authority_file="manager_mail/source-1220.txt",
            authority_lines=LineRange(1, 5),
            authority_sha256=sha(files["manager_mail/source-1220.txt"]),
            authority_envelope_task="authority_envelope.md",
            authority_envelope_sha256=sha(files["authority_envelope.md"]),
            successor_item_lines=(LineRange(3, 5),),
            protected_targets=("human_data:9",),
            audit_output=private / "manager-replace.json",
            preparer="setup-agent",
            reviewer="independent-reviewer",
        )
        return root, args, files

    def runtime(self, state: dict[str, bool], old_target: str = OLD_TARGET, new_target: str = NEW_TARGET):
        def inventory() -> dict[str, PaneIdentity]:
            result: dict[str, PaneIdentity] = {}
            if state.get("old_live", True):
                result[manager_replace.canonical_target(old_target)] = PaneIdentity(manager_replace.canonical_target(old_target), "%42", 4242, 999)
            if state.get("new_live", False):
                result[manager_replace.canonical_target(new_target)] = PaneIdentity(manager_replace.canonical_target(new_target), "%77", 7777, 1001)
            return result

        def stopped(_args: object) -> str:
            state["old_live"] = False
            hook = state.get("stop_hook")
            if callable(hook):
                hook()
            return SESSION_ID

        return (
            patch.object(manager_replace, "pane_inventory", side_effect=inventory),
            patch.object(manager_replace, "stop", side_effect=stopped),
            patch.object(manager_replace, "has_bound_close_proof", side_effect=lambda *_args: not state.get("old_live", True)),
        )

    def source1477_fixture(self, base: Path, old_task: str, old_target: str, new_session: str) -> tuple[Path, Args]:
        root, args, files = self.fixture(base)
        old_text = task_text(
            status="long_running",
            runat=old_target,
            managerat=PARENT_TARGET,
            is_manager=True,
            pending=OLD_QUEUE,
            session_id=SESSION_ID,
        )
        (root / args.old_task).unlink()
        (root / old_task).write_text(old_text, encoding="utf-8")
        children: list[ChildPin] = []
        for task, status, runat, pending in (
            ("child_a.md", "running", "worker:1", ("Translate one module.",)),
            ("child_b.md", "blocked", "worker:2", ("Run the verifier.",)),
        ):
            text = task_text(status=status, runat=runat, managerat=old_target, is_manager=False, pending=pending)
            (root / task).write_text(text, encoding="utf-8")
            children.append(ChildPin(task, sha(text)))
        todo = files["TODO.md"].replace(f"{args.old_task} {OLD_TARGET}", f"{old_task} {old_target}")
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        authority_path = root / manager_replace.SOURCE1477_FILE
        authority_path.write_bytes(SOURCE1477_AUTHORITY.encode())
        authority_path.chmod(0o600)
        canonical_excerpt = "\n".join(SOURCE1477_AUTHORITY.splitlines()[:6])
        envelope = f'<human_instruction authoritative="true" source="{manager_replace.SOURCE1477_FILE}:1-6">\n{canonical_excerpt}\n</human_instruction>\n'
        (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
        return root, replace(
            args,
            old_task=old_task,
            successor_task=f"successor_{new_session}.md",
            old_target=old_target,
            new_target=f"{new_session}:19",
            old_sha256=sha(old_text),
            todo_sha256=sha(todo),
            children=tuple(children),
            authority_file=manager_replace.SOURCE1477_FILE,
            authority_sha256=manager_replace.SOURCE1477_SHA256,
            authority_lines=LineRange(*manager_replace.SOURCE1477_CARRIER_LINES),
            authority_envelope_sha256=sha(envelope),
            successor_item_lines=(LineRange(*manager_replace.SOURCE1477_SUCCESSOR_LINES),),
        )

    def source1485_root_fixture(self, base: Path) -> tuple[Path, Args, dict[str, PaneIdentity]]:
        root = base / "work_logs"
        root.mkdir(mode=0o700)
        private = base / "private"
        private.mkdir(mode=0o700)
        old = task_text(
            status="long_running",
            runat="dw:0",
            managerat="config:1",
            is_manager=True,
            pending=(),
            session_id=SESSION_ID,
        )
        child_specs = (
            ("dw_fpr_mgr_replacement2.md", "long_running", "dw:14", ("Preserve FPR work.",), True),
            ("dw_cc_sampling.md", "running", "dw2:0", ("Preserve Archive work.",), False),
            (manager_replace.SOURCE1485_UMBRELLA_TASK, "long_running", "wl:7", (), True),
            ("dw_bodyswap_pr.md", "running", "dw8:1", (), False),
        )
        children: list[ChildPin] = []
        for task, status, target, queue, is_manager in child_specs:
            data = task_text(
                status=status,
                runat=target,
                managerat="dw:0",
                is_manager=is_manager,
                pending=queue,
                session_id=("aaaaaaaa-2222-4333-8444-555555555555" if task == "dw_fpr_mgr_replacement2.md" else ""),
            )
            (root / task).write_text(data, encoding="utf-8")
            children.append(ChildPin(task, sha(data), manager_replace.json_digest(list(queue))))
        nested = task_text(
            status="long_running",
            runat="dw11:1",
            managerat="dw:14",
            is_manager=True,
            pending=(),
            session_id="bbbbbbbb-2222-4333-8444-555555555555",
        )
        (root / "dw_present_mgr_replacement.md").write_text(nested, encoding="utf-8")
        coordinator = task_text(
            status="long_running",
            runat="config:1",
            managerat="wl:1",
            is_manager=True,
            pending=("Coordinate the DW replacement.",),
        )
        (root / "coordinator.md").write_text(coordinator, encoding="utf-8")
        (root / manager_replace.SOURCE1485_ROOT_TASK).write_text(old, encoding="utf-8")
        todo = (
            "current:\n"
            "dw_manager.md dw:0\n"
            "dw_fpr_mgr_replacement2.md dw:14\n"
            "dw_cc_sampling.md dw2:0\n"
            "dw_bodyswap_pr.md dw8:1\n"
            "dw_present_mgr_replacement.md dw11:1\n\n"
            "human pending:\n\n"
            "low priority:\n\n"
            "previous:\n"
            "resume_dw_work.md wl:7\n"
        )
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        authority = root / manager_replace.SOURCE1485_FILE
        authority.parent.mkdir(mode=0o700)
        authority.write_bytes(SOURCE1485_AUTHORITY.encode())
        authority.chmod(0o600)
        source_lines = SOURCE1485_AUTHORITY.splitlines()
        envelope_body = "\n".join((manager_replace.SOURCE1485_ENVELOPE_SUBJECT, *source_lines[1:]))
        envelope = f'<human_instruction authoritative="true" source="{manager_replace.SOURCE1485_FILE}:1-12">\n{envelope_body}\n</human_instruction>\n'
        envelope_path = root / "dw_rotate_repair.md"
        envelope_path.write_text(envelope, encoding="utf-8")
        self.assertEqual(manager_replace.SOURCE1485_SHA256, sha(SOURCE1485_AUTHORITY))
        self.assertEqual(manager_replace.SOURCE1485_ENVELOPE_SHA256, sha(envelope))
        identities = {
            "dw:0.0": PaneIdentity("dw:0.0", "%42", 4242, 999),
            "config:1.0": PaneIdentity("config:1.0", "%43", 4243, 1000),
            "dw:14.0": PaneIdentity("dw:14.0", "%44", 4244, 1001),
            "dw2:0.0": PaneIdentity("dw2:0.0", "%45", 4245, 1002),
            "wl:7.0": PaneIdentity("wl:7.0", "%46", 4246, 1003),
            "dw8:1.0": PaneIdentity("dw8:1.0", "%47", 4247, 1004),
            "dw11:1.0": PaneIdentity("dw11:1.0", "%48", 4248, 1005),
        }
        protected = tuple(sorted(target for target in identities if target != "dw:0.0"))
        provisional = Args(
            root=root,
            old_task=manager_replace.SOURCE1485_ROOT_TASK,
            successor_task="dw_manager_replacement.md",
            old_target="dw:0",
            new_target="dw:15",
            parent_target="config:1",
            old_sha256=sha(old),
            todo_sha256=sha(todo),
            children=tuple(sorted(children, key=lambda child: child.task)),
            old_pane_id="%42",
            old_pane_pid=4242,
            old_pane_start_ticks=999,
            old_session_id=SESSION_ID,
            authority_file=manager_replace.SOURCE1485_FILE,
            authority_lines=LineRange(*manager_replace.SOURCE1485_CARRIER_LINES),
            authority_sha256=manager_replace.SOURCE1485_SHA256,
            authority_envelope_task="dw_rotate_repair.md",
            authority_envelope_sha256=manager_replace.SOURCE1485_ENVELOPE_SHA256,
            successor_item_lines=(LineRange(*manager_replace.SOURCE1485_SUCCESSOR_LINES),),
            protected_targets=protected,
            audit_output=private / "source1485-root.json",
            preparer="setup-agent",
            reviewer="independent-reviewer",
            old_queue_sha256=manager_replace.json_digest([]),
            authority_envelope_file_sha256=sha(envelope),
        )
        protected_sha = manager_replace.protected_inventory_digest(provisional, identities)
        return root, replace(provisional, protected_targets_sha256=protected_sha), identities

    def source1485_nonroot_fixture(
        self,
        base: Path,
        replacement: tuple[str, str, str, str, str],
    ) -> tuple[Path, Args, dict[str, PaneIdentity]]:
        old_task, old_canonical, successor_task, new_canonical, parent_canonical = replacement
        root = base / "work_logs"
        root.mkdir(mode=0o700)
        private = base / "private"
        private.mkdir(mode=0o700)
        old_target = old_canonical.removesuffix(".0")
        new_target = new_canonical.removesuffix(".0")
        parent_target = parent_canonical.removesuffix(".0")
        old_queue = (f"Preserve {old_task} work.",)
        old = task_text(
            status="long_running",
            runat=old_target,
            managerat=parent_target,
            is_manager=True,
            pending=old_queue,
            session_id=SESSION_ID,
        )
        (root / old_task).write_text(old, encoding="utf-8")
        parent_owner = task_text(
            status="long_running",
            runat=parent_target,
            managerat=("wl:7" if parent_target == "dw:13" else "dw:0"),
            is_manager=True,
            pending=("Preserve parent coordination.",),
            session_id="cccccccc-2222-4333-8444-555555555555",
        )
        (root / "parent_manager.md").write_text(parent_owner, encoding="utf-8")
        if old_task == "dw_present_mgr.md":
            child_specs = (("dw_present_worker.md", "running", "dwp:2", ("Preserve presentation work.",), False),)
            nested_specs: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = ()
        else:
            child_specs = (
                ("dw_present_mgr_replacement.md", "long_running", "dw11:1", (), True),
                ("dw_fpr_worker.md", "running", "dw9:0", ("Preserve FPR work.",), False),
            )
            nested_specs = (("dw_present_worker.md", "running", "dwp:2", ("Preserve presentation work.",), False),)
        children: list[ChildPin] = []
        for task, status, target, queue, is_manager in child_specs:
            data = task_text(
                status=status,
                runat=target,
                managerat=old_target,
                is_manager=is_manager,
                pending=queue,
                session_id=("bbbbbbbb-2222-4333-8444-555555555555" if is_manager else ""),
            )
            (root / task).write_text(data, encoding="utf-8")
            children.append(ChildPin(task, sha(data), manager_replace.json_digest(list(queue))))
        for task, status, target, queue, is_manager in nested_specs:
            data = task_text(
                status=status,
                runat=target,
                managerat="dw11:1",
                is_manager=is_manager,
                pending=queue,
            )
            (root / task).write_text(data, encoding="utf-8")
        todo_rows = [f"{old_task} {old_target}"]
        todo_rows.extend(f"{task} {target}" for task, _status, target, _queue, _is_manager in (*child_specs, *nested_specs))
        todo = "current:\n" + "\n".join(todo_rows) + "\n\nhuman pending:\n\nlow priority:\n\nprevious:\n"
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        authority = root / manager_replace.SOURCE1485_FILE
        authority.parent.mkdir(mode=0o700)
        authority.write_bytes(SOURCE1485_AUTHORITY.encode())
        authority.chmod(0o600)
        source_lines = SOURCE1485_AUTHORITY.splitlines()
        envelope_body = "\n".join((manager_replace.SOURCE1485_ENVELOPE_SUBJECT, *source_lines[1:]))
        envelope = f'<human_instruction authoritative="true" source="{manager_replace.SOURCE1485_FILE}:1-12">\n{envelope_body}\n</human_instruction>\n'
        envelope_path = root / "dw_rotate_repair.md"
        envelope_path.write_text(envelope, encoding="utf-8")
        identities: dict[str, PaneIdentity] = {}
        for index, target in enumerate(
            (old_canonical, parent_canonical, *(manager_replace.canonical_target(spec[2]) for spec in (*child_specs, *nested_specs))),
            start=42,
        ):
            identities[target] = PaneIdentity(target, f"%{index}", 4200 + index, 900 + index)
        protected = tuple(sorted(target for target in identities if target != old_canonical))
        provisional = Args(
            root=root,
            old_task=old_task,
            successor_task=successor_task,
            old_target=old_target,
            new_target=new_target,
            parent_target=parent_target,
            old_sha256=sha(old),
            todo_sha256=sha(todo),
            children=tuple(sorted(children, key=lambda child: child.task)),
            old_pane_id=identities[old_canonical].pane_id,
            old_pane_pid=identities[old_canonical].pid,
            old_pane_start_ticks=identities[old_canonical].start_ticks,
            old_session_id=SESSION_ID,
            authority_file=manager_replace.SOURCE1485_FILE,
            authority_lines=LineRange(*manager_replace.SOURCE1485_CARRIER_LINES),
            authority_sha256=manager_replace.SOURCE1485_SHA256,
            authority_envelope_task="dw_rotate_repair.md",
            authority_envelope_sha256=manager_replace.SOURCE1485_ENVELOPE_SHA256,
            successor_item_lines=(LineRange(*manager_replace.SOURCE1485_SUCCESSOR_LINES),),
            protected_targets=protected,
            audit_output=private / f"{old_task}.json",
            preparer="setup-agent",
            reviewer="independent-reviewer",
            old_queue_sha256=manager_replace.json_digest(list(old_queue)),
            authority_envelope_file_sha256=sha(envelope),
        )
        protected_sha = manager_replace.protected_inventory_digest(provisional, identities)
        return root, replace(provisional, protected_targets_sha256=protected_sha), identities

    def source1597_fixture(self, base: Path) -> tuple[Path, Args, tuple[PaneIdentity, ...]]:
        root, args, files = self.fixture(base)
        old_text = task_text(
            status="long_running",
            runat=manager_replace.SOURCE1597_OLD_TARGET,
            managerat=manager_replace.SOURCE1597_PARENT_TARGET,
            is_manager=True,
            pending=SOURCE1597_QUEUE,
            session_id=SESSION_ID,
        )
        (root / args.old_task).unlink()
        old_path = root / manager_replace.SOURCE1597_TASK
        old_path.write_text(old_text, encoding="utf-8")
        children: list[ChildPin] = []
        identities = [
            PaneIdentity("dw:15.0", "%50", 5000, 900),
            PaneIdentity("other:1.0", "%51", 5001, 901),
        ]
        for index, (task, status, runat, pending) in enumerate(
            (
                ("child_a.md", "running", "worker:1", ("Translate one module.",)),
                ("child_b.md", "blocked", "worker:2", ("Run the verifier.",)),
            ),
            start=2,
        ):
            data = task_text(status=status, runat=runat, managerat=manager_replace.SOURCE1597_OLD_TARGET, is_manager=False, pending=pending)
            (root / task).write_text(data, encoding="utf-8")
            children.append(ChildPin(task, sha(data)))
            identities.append(PaneIdentity(f"{runat}.0", f"%{50 + index}", 5000 + index, 900 + index))
        todo = files["TODO.md"].replace(
            f"{args.old_task} {OLD_TARGET}",
            f"{manager_replace.SOURCE1597_TASK} {manager_replace.SOURCE1597_OLD_TARGET}",
        )
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        authority_path = root / manager_replace.SOURCE1597_FILE
        authority_path.write_bytes(SOURCE1597_AUTHORITY)
        authority_path.chmod(0o600)
        source1601_path = root / manager_replace.SOURCE1601_FILE
        source1601_path.write_bytes(SOURCE1601_AUTHORITY)
        source1601_path.chmod(0o600)
        self.assertEqual(manager_replace.SOURCE1597_SHA256, hashlib.sha256(SOURCE1597_AUTHORITY).hexdigest())
        self.assertEqual(manager_replace.SOURCE1601_SHA256, hashlib.sha256(SOURCE1601_AUTHORITY).hexdigest())
        protected = tuple(sorted(identities, key=lambda identity: identity.target))
        exact = replace(
            args,
            old_task=manager_replace.SOURCE1597_TASK,
            successor_task=manager_replace.SOURCE1597_SUCCESSOR_TASK,
            old_target=manager_replace.SOURCE1597_OLD_TARGET,
            new_target=manager_replace.SOURCE1597_SUCCESSOR_TARGET,
            parent_target=manager_replace.SOURCE1597_PARENT_TARGET,
            old_sha256=sha(old_text),
            todo_sha256=sha(todo),
            children=tuple(children),
            authority_file=manager_replace.SOURCE1597_FILE,
            authority_lines=LineRange(*manager_replace.SOURCE1597_LINES),
            authority_sha256=manager_replace.SOURCE1597_SHA256,
            authority_envelope_task=manager_replace.SOURCE1597_TASK,
            authority_envelope_sha256=sha(old_text),
            successor_item_lines=(LineRange(*manager_replace.SOURCE1597_LINES),),
            protected_targets=tuple(identity.target for identity in protected),
            old_queue_sha256=manager_replace.json_digest(list(SOURCE1597_QUEUE)),
            protected_targets_sha256=manager_replace.json_digest(
                [{"target": identity.target, "pane_id": identity.pane_id, "pid": identity.pid, "start_ticks": identity.start_ticks} for identity in protected]
            ),
        )
        return root, exact, protected

    def source1597_runtime(
        self,
        state: dict[str, object],
        args: Args,
        protected: tuple[PaneIdentity, ...],
    ) -> tuple[object, object, object]:
        def inventory() -> dict[str, PaneIdentity]:
            result = {identity.target: identity for identity in protected}
            extra = state.get("extra_identity")
            if isinstance(extra, PaneIdentity):
                result[extra.target] = extra
            if state.get("protected_drift"):
                first = protected[0]
                result[first.target] = replace(first, pid=first.pid + 100)
            if state.get("old_live", True):
                result[manager_replace.canonical_target(args.old_target)] = PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999)
            return result

        def stopped(_args: object) -> str:
            state["old_live"] = False
            hook = state.get("stop_hook")
            if callable(hook):
                hook()
            return SESSION_ID

        return (
            patch.object(manager_replace, "pane_inventory", side_effect=inventory),
            patch.object(manager_replace, "stop", side_effect=stopped),
            patch.object(manager_replace, "has_bound_close_proof", side_effect=lambda *_args: not state.get("old_live", True)),
        )

    def source1611_fixture(self, base: Path) -> tuple[Path, Args, tuple[PaneIdentity, ...]]:
        root, args, files = self.fixture(base)
        source_file = manager_replace.SOURCE1611_FILE
        source_bytes = SOURCE1611_AUTHORITY
        source_lines = manager_replace.SOURCE1611_LINES
        source_sha256 = manager_replace.SOURCE1611_SHA256
        old_task = manager_replace.SOURCE1611_TASK
        old_target = manager_replace.SOURCE1611_OLD_TARGET
        successor_task = manager_replace.SOURCE1611_SUCCESSOR_TASK
        successor_target = manager_replace.SOURCE1611_SUCCESSOR_TARGET
        parent_target = manager_replace.SOURCE1611_PARENT_TARGET
        human_queue = HUMAN_QUEUE
        old_text = task_text(
            status="long_running",
            runat=old_target,
            managerat=parent_target,
            is_manager=True,
            pending=human_queue,
            session_id=SESSION_ID,
        )
        (root / args.old_task).unlink()
        (root / old_task).write_text(old_text, encoding="utf-8")
        children: list[ChildPin] = []
        identities = [
            PaneIdentity(manager_replace.canonical_target(parent_target), "%60", 6000, 1000),
            PaneIdentity("other:1.0", "%61", 6001, 1001),
        ]
        for index, (task, status, runat, pending) in enumerate(
            (
                ("child_a.md", "running", "worker:1", ("🧑 Translate one Human-requested module.",)),
                ("child_b.md", "blocked", "worker:2", ("🧑 Run the Human-requested verifier.",)),
            ),
            start=2,
        ):
            data = task_text(status=status, runat=runat, managerat=old_target, is_manager=False, pending=pending)
            (root / task).write_text(data, encoding="utf-8")
            children.append(ChildPin(task, sha(data)))
            identities.append(PaneIdentity(f"{runat}.0", f"%{60 + index}", 6000 + index, 1000 + index))
        todo = files["TODO.md"].replace(f"{args.old_task} {OLD_TARGET}", f"{old_task} {old_target}")
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        authority_path = root / source_file
        authority_path.write_bytes(source_bytes)
        authority_path.chmod(0o600)
        source1601_path = root / manager_replace.SOURCE1601_FILE
        source1601_path.write_bytes(SOURCE1601_AUTHORITY)
        source1601_path.chmod(0o600)
        self.assertEqual(source_sha256, hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(manager_replace.SOURCE1601_SHA256, hashlib.sha256(SOURCE1601_AUTHORITY).hexdigest())
        protected = tuple(sorted(identities, key=lambda identity: identity.target))
        exact = replace(
            args,
            old_task=old_task,
            successor_task=successor_task,
            old_target=old_target,
            new_target=successor_target,
            parent_target=parent_target,
            old_sha256=sha(old_text),
            todo_sha256=sha(todo),
            children=tuple(children),
            authority_file=source_file,
            authority_lines=LineRange(*source_lines),
            authority_sha256=source_sha256,
            authority_envelope_task=old_task,
            authority_envelope_sha256=sha(old_text),
            successor_item_lines=(LineRange(*source_lines),),
            protected_targets=tuple(identity.target for identity in protected),
            old_queue_sha256=manager_replace.json_digest(list(human_queue)),
            protected_targets_sha256=manager_replace.json_digest(
                [{"target": identity.target, "pane_id": identity.pane_id, "pid": identity.pid, "start_ticks": identity.start_ticks} for identity in protected]
            ),
        )
        return root, exact, protected

    def source1611_direct_worker_fixture(self, base: Path) -> tuple[Path, Args, tuple[PaneIdentity, ...]]:
        root, args, protected = self.source1611_fixture(base)
        old_task = manager_replace.SOURCE1611_DIRECT_WORKER_TASK
        old_target = manager_replace.SOURCE1611_DIRECT_WORKER_OLD_TARGET
        successor_task = manager_replace.SOURCE1611_DIRECT_WORKER_SUCCESSOR_TASK
        successor_target = manager_replace.SOURCE1611_DIRECT_WORKER_SUCCESSOR_TARGET
        parent_target = manager_replace.SOURCE1611_DIRECT_WORKER_PARENT_TARGET
        old_text = task_text(
            status="long_running",
            runat=old_target,
            managerat=parent_target,
            is_manager=True,
            pending=manager_replace.SOURCE1611_DIRECT_WORKER_OLD_QUEUE,
            session_id=SESSION_ID,
        )
        (root / args.old_task).unlink()
        (root / old_task).write_text(old_text, encoding="utf-8")
        parent_text = task_text(
            status="long_running",
            runat=parent_target,
            managerat=manager_replace.SOURCE1611_PARENT_TARGET,
            is_manager=True,
            pending=("🧑 Track the serial Source-1611 transfer.",),
            session_id="22222222-2222-4333-8444-555555555555",
        )
        parent_path = root / manager_replace.SOURCE1611_SUCCESSOR_TASK
        parent_path.write_text(parent_text, encoding="utf-8")
        source1597_path = root / manager_replace.SOURCE1597_FILE
        source1597_path.write_bytes(SOURCE1597_AUTHORITY)
        source1597_path.chmod(0o600)
        self.assertEqual(manager_replace.SOURCE1597_SHA256, hashlib.sha256(SOURCE1597_AUTHORITY).hexdigest())
        children: list[ChildPin] = []
        for child in args.children:
            path = root / child.task
            migrated = path.read_text(encoding="utf-8").replace(f"managerat: {args.old_target}\n", f"managerat: {old_target}\n")
            path.write_text(migrated, encoding="utf-8")
            children.append(ChildPin(child.task, sha(migrated)))
        todo = (root / "TODO.md").read_text(encoding="utf-8").replace(f"{args.old_task} {args.old_target}", f"{old_task} {old_target}")
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        direct_protected = tuple(PaneIdentity(parent_target + ".0", "%65", 6005, 1005) if item.target == manager_replace.canonical_target(args.parent_target) else item for item in protected)
        direct_protected = tuple(sorted(direct_protected, key=lambda item: item.target))
        exact = replace(
            args,
            old_task=old_task,
            successor_task=successor_task,
            old_target=old_target,
            new_target=successor_target,
            parent_target=parent_target,
            old_sha256=sha(old_text),
            todo_sha256=sha(todo),
            children=tuple(children),
            authority_envelope_task=old_task,
            authority_envelope_sha256=sha(old_text),
            source1611_parent_sha256=sha(parent_text),
            protected_targets=tuple(item.target for item in direct_protected),
            old_queue_sha256=manager_replace.json_digest(list(manager_replace.SOURCE1611_DIRECT_WORKER_OLD_QUEUE)),
            protected_targets_sha256=manager_replace.json_digest([{"target": item.target, "pane_id": item.pane_id, "pid": item.pid, "start_ticks": item.start_ticks} for item in direct_protected]),
        )
        return root, exact, direct_protected

    def whole_tree_fixture(self, base: Path) -> tuple[Path, Args, dict[str, str]]:
        root, args, files = self.fixture(base)
        sessions = (
            "aaaaaaaa-2222-4333-8444-555555555555",
            "bbbbbbbb-2222-4333-8444-555555555555",
        )
        targets = ("worker:1", "worker:2")
        updated_children: list[ChildPin] = []
        descendants: list[DescendantPin] = []
        for index, (task, session, target) in enumerate(zip(("child_a.md", "child_b.md"), sessions, targets, strict=True), start=1):
            text = files[task].replace("---\nPreserve", f"session_id: {session}\n---\nPreserve")
            files[task] = text
            (root / task).write_text(text, encoding="utf-8")
            child_sha = sha(text)
            updated_children.append(ChildPin(task, child_sha))
            queue = parsed(root / task, root).pending_task_items
            descendants.append(DescendantPin(task, child_sha, target, f"%{50 + index}", 5000 + index, 900 + index, session, manager_replace.json_digest(list(queue))))
        return root, replace(args, children=tuple(updated_children), descendants=tuple(descendants)), files

    def whole_tree_authority(self, args: Args) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(
            patch.object(
                manager_replace,
                "is_source1289_whole_tree",
                side_effect=lambda candidate: bool(candidate.descendants or candidate.empty_tree_envelope_sha256),
            )
        )
        stack.enter_context(patch.object(manager_replace, "SOURCE1289_SHA256", args.authority_sha256))
        stack.enter_context(
            patch.object(
                manager_replace,
                "SOURCE1289_CARRIER_LINES",
                (args.authority_lines.start, args.authority_lines.end),
            )
        )
        stack.enter_context(
            patch.object(
                manager_replace,
                "SOURCE1289_TREE_LINES",
                (args.successor_item_lines[0].start, args.successor_item_lines[0].end),
            )
        )
        return stack

    def source1289_authority_fixture(self, base: Path) -> tuple[Path, Args]:
        root, args, files = self.whole_tree_fixture(base)
        old_path = root / args.old_task
        source1289_path = root / manager_replace.SOURCE1289_TASK
        old_path.rename(source1289_path)
        todo = files["TODO.md"].replace(args.old_task, manager_replace.SOURCE1289_TASK)
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        source_path = root / manager_replace.SOURCE1289_FILE
        source_path.write_bytes(SOURCE1289_AUTHORITY.encode())
        source_path.chmod(0o600)
        source_lines = SOURCE1289_AUTHORITY.splitlines()
        carrier = "\n".join(source_lines[:13])
        envelope = f'<human_instruction authoritative="true" source="{manager_replace.SOURCE1289_FILE}:1-13">\n{carrier}\n</human_instruction>\n'
        envelope_path = root / args.authority_envelope_task
        envelope_path.write_text(envelope, encoding="utf-8")
        changed = replace(
            args,
            old_task=manager_replace.SOURCE1289_TASK,
            todo_sha256=sha(todo),
            authority_file=manager_replace.SOURCE1289_FILE,
            authority_lines=LineRange(1, 13),
            authority_sha256=manager_replace.SOURCE1289_SHA256,
            authority_envelope_sha256=sha(envelope),
            successor_item_lines=(LineRange(3, 10),),
        )
        return root, changed

    def source1289_runtime(self, args: Args) -> tuple[contextlib.ExitStack, list[str]]:
        live = {
            manager_replace.canonical_target(args.old_target),
            *(manager_replace.canonical_target(item.target) for item in args.descendants),
        }
        identities = {
            manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
            **{
                manager_replace.canonical_target(item.target): PaneIdentity(manager_replace.canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks)
                for item in args.descendants
            },
        }
        sessions = {item.target: item.session_id for item in args.descendants} | {args.old_target: args.old_session_id}
        stopped_targets: list[str] = []

        def inventory() -> dict[str, PaneIdentity]:
            return {target: identity for target, identity in identities.items() if target in live}

        def stopped(stop_args) -> str:
            stopped_targets.append(stop_args.target)
            live.remove(manager_replace.canonical_target(stop_args.target))
            Path(stop_args.bound_close_proof_path).write_text(f"{stop_args.bound_close_proof_secret}\n", encoding="utf-8")
            Path(stop_args.bound_close_proof_path).chmod(0o600)
            return sessions[stop_args.target]

        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(manager_replace, "pane_inventory", side_effect=inventory))
        stack.enter_context(patch.object(manager_replace, "process_start_ticks", return_value=None))
        stack.enter_context(patch.object(manager_replace, "stop", side_effect=stopped))
        return stack, stopped_targets

    def empty_tree_fixture(self, base: Path) -> tuple[Path, Args, dict[str, str], contextlib.ExitStack]:
        root, args, files = self.fixture(base)
        old = parsed(root / args.old_task, root)
        for child in args.children:
            path = root / child.task
            text = manager_replace.render_pending_items(path.read_text(encoding="utf-8"), ())
            text = manager_replace.update_frontmatter_status(text, "done", "", root)
            path.write_text(text, encoding="utf-8")
            files[child.task] = text
        source1292 = "Subject: close empty tree\n\nClose either way. Directly use tmux if needed\n\n"
        source_path = root / manager_replace.SOURCE1292_FILE
        source_path.parent.mkdir(exist_ok=True)
        source_path.write_text(source1292, encoding="utf-8")
        source_path.chmod(0o600)
        original = (root / args.authority_envelope_task).read_text(encoding="utf-8")
        second = (
            f'<human_instruction authoritative="true" source="{manager_replace.SOURCE1292_FILE}:1-4">\n'
            "Subject: close empty tree\n\nClose either way. Directly use tmux if needed\n"
            "</human_instruction>\n"
        )
        envelope = original + second
        (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
        first_block = original
        changed = replace(
            args,
            children=(),
            descendants=(),
            authority_envelope_sha256=sha(first_block),
            empty_tree_envelope_sha256=sha(second),
        )
        stack = self.whole_tree_authority(changed)
        stack.enter_context(patch.object(manager_replace, "SOURCE1292_SHA256", sha(source1292)))
        self.assertEqual((), manager_replace.active_child_task_refs(root, root / args.old_task, old.runat))
        return root, changed, files, stack

    def dual_descendant_fixture(self, base: Path) -> tuple[Path, Args, dict[str, str], contextlib.ExitStack]:
        root, args, files = self.whole_tree_fixture(base)
        source1292 = "Subject: close tree\n\nClose either way. Directly use tmux if needed\n\n"
        source_path = root / manager_replace.SOURCE1292_FILE
        source_path.parent.mkdir(exist_ok=True)
        source_path.write_text(source1292, encoding="utf-8")
        source_path.chmod(0o600)
        original = (root / args.authority_envelope_task).read_text(encoding="utf-8")
        second = (
            f'<human_instruction authoritative="true" source="{manager_replace.SOURCE1292_FILE}:1-4">\nSubject: close tree\n\nClose either way. Directly use tmux if needed\n</human_instruction>\n'
        )
        (root / args.authority_envelope_task).write_text(original + second, encoding="utf-8")
        changed = replace(args, descendant_authority_envelope_sha256=sha(second))
        stack = self.whole_tree_authority(changed)
        stack.enter_context(patch.object(manager_replace, "SOURCE1292_SHA256", sha(source1292)))
        return root, changed, files, stack

    def dual_descendant_alias_fixture(self, base: Path) -> tuple[Path, Args, dict[str, str], contextlib.ExitStack]:
        root, args, files, authority = self.dual_descendant_fixture(base)
        carrier = args.children[0].task
        envelope = (root / args.authority_envelope_task).read_text(encoding="utf-8")
        carrier_text = (root / carrier).read_text(encoding="utf-8") + envelope
        (root / carrier).write_text(carrier_text, encoding="utf-8")
        files[carrier] = carrier_text
        children = tuple(ChildPin(child.task, sha(carrier_text) if child.task == carrier else child.sha256) for child in args.children)
        descendants = tuple(replace(item, sha256=sha(carrier_text)) if item.task == carrier else item for item in args.descendants)
        return (
            root,
            replace(
                args,
                children=children,
                descendants=descendants,
                authority_envelope_task=carrier,
            ),
            files,
            authority,
        )

    def run_replacement(self, args: Args, state: dict[str, bool]) -> str:
        inventory, stopped, proof = self.runtime(state, args.old_target, args.new_target)
        with inventory, stopped, proof:
            return replace_manager(args)

    def guest1269_fixture(self, base: Path) -> tuple[Path, Args, dict[str, str]]:
        root, args, files = self.fixture(base)
        old_task = "guest_hees_mail_mgr.md"
        old_target = "guest_hees:0"
        old = task_text(
            status="long_running",
            runat=old_target,
            managerat=PARENT_TARGET,
            is_manager=True,
            pending=OLD_QUEUE,
            session_id=SESSION_ID,
        )
        (root / args.old_task).unlink()
        files.pop(args.old_task)
        files[old_task] = old
        (root / old_task).write_text(old, encoding="utf-8")
        children: list[ChildPin] = []
        for name in ("child_a.md", "child_b.md"):
            data = files[name].replace(f"managerat: {OLD_TARGET}", f"managerat: {old_target}")
            files[name] = data
            (root / name).write_text(data, encoding="utf-8")
            children.append(ChildPin(name, sha(data)))
        todo = files["TODO.md"].replace(f"{args.old_task} {OLD_TARGET}", f"{old_task} {old_target}")
        files["TODO.md"] = todo
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        authority_file = "manager_mail/85c5dff58359-1269.txt"
        authority = (
            "Subject: Fix guest mail handling\n"
            "\n"
            "The guest has reported that they do not receive response for emails sent to\n"
            "you guys. Whatever the previous responsible agents were doing, they\n"
            "completely failed. Replace them. The new agent should be skeptical of\n"
            "anything done previously and make sure that in the future replies get sent\n"
            "to the guest also It was not like the guest received nothing. They report\n"
            "receiving empty emails. Investigate this with the new agents. Completing\n"
            "overhaul any garbage that's left.\n"
        )
        authority_path = root / authority_file
        authority_path.write_text(authority, encoding="utf-8")
        authority_path.chmod(0o600)
        envelope = f'<human_instruction authoritative="true" source="{authority_file}:3-9">\n{"".join(authority.splitlines(keepends=True)[2:9])}</human_instruction>\n'
        (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
        files[authority_file] = authority
        files[args.authority_envelope_task] = envelope
        return (
            root,
            replace(
                args,
                old_task=old_task,
                old_target=old_target,
                old_sha256=sha(old),
                todo_sha256=sha(todo),
                children=tuple(children),
                authority_file=authority_file,
                authority_lines=LineRange(3, 9),
                authority_sha256=sha(authority),
                authority_envelope_sha256=sha(envelope),
                successor_item_lines=(LineRange(3, 9),),
                old_queue_sha256=manager_replace.json_digest(list(OLD_QUEUE)),
            ),
            files,
        )

    def pcodx_fixture(self, base: Path) -> tuple[Path, Args, dict[str, str], dict[str, str]]:
        root, args, files = self.fixture(base)
        old_target = "hwl:3"
        new_target = "wl:31"
        old = task_text(
            status="long_running",
            runat=old_target,
            managerat=PARENT_TARGET,
            is_manager=True,
            pending=OLD_QUEUE,
            tool="pcodx",
        )
        files[args.old_task] = old
        (root / args.old_task).write_text(old, encoding="utf-8")
        for name in ("child_a.md", "child_b.md"):
            data = files[name].replace(f"managerat: {OLD_TARGET}", f"managerat: {old_target}")
            files[name] = data
            (root / name).write_text(data, encoding="utf-8")
        extra_children: list[ChildPin] = []
        for index in (3, 4):
            name = f"child_{index}.md"
            data = task_text(
                status="running",
                runat=f"worker:{index}",
                managerat=old_target,
                is_manager=False,
                pending=(f"Preserve child {index} queue.",),
            )
            files[name] = data
            (root / name).write_text(data, encoding="utf-8")
            extra_children.append(ChildPin(name, sha(data)))
        todo = files["TODO.md"].replace(f"failed_manager.md {OLD_TARGET}", f"failed_manager.md {old_target}")
        files["TODO.md"] = todo
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        authority_lines = (
            "Subject: failed_manager.md protected PCODX replacement\n",
            "\n",
            "The PCODX agent failed. They did not run the task. Replace them.\n",
            f"Close {old_target}. Replace {args.old_task} with one plain Codex successor.\n",
        )
        authority = "".join(authority_lines)
        authority_path = root / args.authority_file
        authority_path.write_text(authority, encoding="utf-8")
        envelope = f'<human_instruction authoritative="true" source="{args.authority_file}:1-4">\n{authority}</human_instruction>\n'
        (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
        ledger = base / "pcodx-run" / "ledger.json"
        ledger.parent.mkdir(mode=0o700)
        ledger.write_text('{"sequence":1}\n', encoding="utf-8")
        pcodx = {
            "PCODX_POC_ROOT": str(base / "pcodx-poc"),
            "PCODX_RUN_DIR": str(ledger.parent),
            "PCODX_LEDGER_PATH": str(ledger),
            "PCODX_SESSION_ID": "pcodx-source-1228",
        }
        Path(pcodx["PCODX_POC_ROOT"]).mkdir(mode=0o700)
        identity = PaneIdentity(f"{old_target}.0", "%42", 4242, 999)
        wrapper = Path(manager_replace.__file__).resolve().with_name("pcodx").read_bytes()
        children = tuple(
            sorted(
                (
                    ChildPin("child_a.md", sha(files["child_a.md"])),
                    ChildPin("child_b.md", sha(files["child_b.md"])),
                    *extra_children,
                ),
                key=lambda child: child.task,
            )
        )
        changed = replace(
            args,
            old_target=old_target,
            new_target=new_target,
            old_sha256=sha(old),
            todo_sha256=sha(todo),
            children=children,
            authority_lines=LineRange(1, 4),
            authority_sha256=sha(authority),
            authority_envelope_sha256=sha(envelope),
            successor_item_lines=(LineRange(3, 4),),
            protected_targets=(old_target,),
            old_queue_sha256=manager_replace.json_digest(list(OLD_QUEUE)),
            old_pcodx_state_sha256=manager_replace.json_digest(pcodx),
            old_pcodx_ledger_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest(),
            old_pcodx_wrapper_sha256=hashlib.sha256(wrapper).hexdigest(),
            protected_targets_sha256=manager_replace.json_digest([{"target": identity.target, "pane_id": identity.pane_id, "pid": identity.pid, "start_ticks": identity.start_ticks}]),
            authority_envelope_file_sha256=sha(envelope),
        )
        return root, changed, files, pcodx

    def pcodx_runtime(self, args: Args, pcodx: dict[str, str]):
        state = {"old_live": True}

        def inventory() -> dict[str, PaneIdentity]:
            if not state["old_live"]:
                return {}
            identity = PaneIdentity(manager_replace.canonical_target(args.old_target), args.old_pane_id, args.old_pane_pid, args.old_pane_start_ticks)
            return {identity.target: identity}

        def stopped(stop_args: object) -> str:
            self.assertEqual(args.old_task, stop_args.task_file)
            self.assertEqual(args.old_target, stop_args.human_close_authorized_target)
            self.assertEqual(args.authority_sha256, stop_args.human_close_authorization_sha256)
            self.assertIsNotNone(stop_args.bound_pre_input_check)
            stop_args.bound_pre_input_check()
            state["old_live"] = False
            return args.old_session_id

        return (
            patch.object(manager_replace, "pane_inventory", side_effect=inventory),
            patch.object(manager_replace, "pcodx_state", return_value=pcodx),
            patch.object(manager_replace, "stop", side_effect=stopped),
            patch.object(manager_replace, "has_bound_close_proof", side_effect=lambda *_args: not state["old_live"]),
        )

    def test_success_closes_migrates_and_publishes_unlaunched_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            result = self.run_replacement(args, {"old_live": True})

            old = parsed(root / args.old_task, root)
            successor = parsed(root / args.successor_task, root)
            self.assertEqual("done", old.status)
            self.assertEqual((), old.pending_task_items)
            self.assertEqual("blocked", successor.status)
            self.assertEqual(NEW_TARGET, successor.runat)
            self.assertEqual(3, len(successor.pending_task_items))
            self.assertEqual(OLD_QUEUE, successor.pending_task_items[:2])
            self.assertIn("manager_mail/source-1220.txt:3-5", successor.pending_task_items[-1])
            self.assertNotIn("The agent failed", (root / args.successor_task).read_text(encoding="utf-8"))
            self.assertEqual("", successor.session_id)
            self.assertEqual(NEW_TARGET, parsed(root / "child_a.md", root).managerat)
            self.assertEqual(NEW_TARGET, parsed(root / "child_b.md", root).managerat)
            self.assertEqual(("Translate one module.",), parsed(root / "child_a.md", root).pending_task_items)
            self.assertEqual(("Run the verifier.",), parsed(root / "child_b.md", root).pending_task_items)
            self.assertEqual(files["unrelated.md"], (root / "unrelated.md").read_text(encoding="utf-8"))
            todo = (root / "TODO.md").read_text(encoding="utf-8")
            self.assertIn("successor_manager.md private_mgr:3", todo)
            self.assertIn("previous:\nfailed_manager.md private_mgr:1", todo)
            audit = json.loads(args.audit_output.read_text(encoding="utf-8"))
            self.assertEqual("committed", audit["state"])
            self.assertEqual(["failed_manager.md", "child_a.md", "child_b.md", "TODO.md", "successor_manager.md"], audit["completed_writes"])
            self.assertIn("launch remains a separate supported operation", result)

    def test_human_owned_pcodx_accepts_only_exact_authority_and_publishes_plain_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, pcodx = self.pcodx_fixture(Path(tmp))
            inventory, state, stopped, proof = self.pcodx_runtime(args, pcodx)
            with inventory, state, stopped, proof:
                result = replace_manager(args)
            old = parsed(root / args.old_task, root)
            successor = parsed(root / args.successor_task, root)
            self.assertEqual("done", old.status)
            self.assertEqual("blocked", successor.status)
            self.assertEqual("codex", successor.tool)
            self.assertEqual("", successor.session_id)
            self.assertEqual(4, len(manager_replace.active_child_task_refs(root, root / args.successor_task, args.new_target)))
            self.assertIn("blocked unlaunched", result)

    def test_source1240_exact_replacement_sentence_is_direct_close_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, pcodx = self.pcodx_fixture(Path(tmp))
            source = (
                "Subject: Re: Low-priority task decisions\n\n"
                f"Replace the failed PCODX manager {args.old_task} at {args.old_target} "
                "with one fresh plain-Codex manager inheriting all tasks and comments.\n"
                "Just do it\n"
            )
            envelope = f'<human_instruction authoritative="true" source="{args.authority_file}:1-4">\n{source}</human_instruction>\n'
            (root / args.authority_file).write_text(source, encoding="utf-8")
            (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
            changed = replace(
                args,
                authority_sha256=sha(source),
                authority_envelope_sha256=sha(envelope),
                authority_envelope_file_sha256=sha(envelope),
                successor_item_lines=(LineRange(3, 4),),
            )
            inventory, state, stopped, proof = self.pcodx_runtime(changed, pcodx)
            with inventory, state, stopped, proof:
                result = replace_manager(changed)
            self.assertIn("sole ownership", result)
            self.assertEqual("codex", parsed(root / changed.successor_task, root).tool)

    def test_source1240_replacement_sentence_must_bind_exact_task_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, pcodx = self.pcodx_fixture(Path(tmp))
            for replacement_text in (
                f"Replace the failed PCODX manager other.md at {args.old_target} with one fresh plain-Codex manager inheriting all tasks and comments.\n",
                f"Replace the failed PCODX manager {args.old_task} at hwl:4 with one fresh plain-Codex manager inheriting all tasks and comments.\n",
            ):
                source = "Subject: Re: Low-priority task decisions\n\n" + replacement_text + "Just do it\n"
                envelope = f'<human_instruction authoritative="true" source="{args.authority_file}:1-4">\n{source}</human_instruction>\n'
                (root / args.authority_file).write_text(source, encoding="utf-8")
                (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
                changed = replace(
                    args,
                    authority_sha256=sha(source),
                    authority_envelope_sha256=sha(envelope),
                    authority_envelope_file_sha256=sha(envelope),
                )
                inventory, state, stopped, proof = self.pcodx_runtime(changed, pcodx)
                with inventory, state, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "does not explicitly prove"):
                    replace_manager(changed)
                stop_mock.assert_not_called()

    def test_source1240_replacement_sentence_rejects_suffix_and_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, pcodx = self.pcodx_fixture(Path(tmp))
            exact = f"Replace the failed PCODX manager {args.old_task} at {args.old_target} with one fresh plain-Codex manager inheriting all tasks and comments."
            for selected in (
                f"{exact} Do not close it.",
                f"{exact}\n{exact}",
                f"{exact}\nDo not replace that manager.",
                f"{exact}\nCancel the replacement.",
                f"{exact}\nNo replacement of that manager.",
                exact.replace("Replace the failed", "REPLACE THE FAILED"),
            ):
                with self.subTest(selected=selected):
                    source = f"Subject: Re: Low-priority task decisions\n\n{selected}\nJust do it\n"
                    source_line_count = len(source.splitlines())
                    envelope = f'<human_instruction authoritative="true" source="{args.authority_file}:1-{source_line_count}">\n{source}</human_instruction>\n'
                    (root / args.authority_file).write_text(source, encoding="utf-8")
                    (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
                    changed = replace(
                        args,
                        authority_lines=LineRange(1, source_line_count),
                        authority_sha256=sha(source),
                        authority_envelope_sha256=sha(envelope),
                        authority_envelope_file_sha256=sha(envelope),
                        successor_item_lines=(LineRange(3, source_line_count - 1),),
                    )
                    inventory, state, stopped, proof = self.pcodx_runtime(changed, pcodx)
                    with inventory, state, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "does not explicitly prove"):
                        replace_manager(changed)
                    stop_mock.assert_not_called()

    def test_human_owned_pcodx_rejects_every_identity_authority_and_custody_drift_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, pcodx = self.pcodx_fixture(Path(tmp))
            cases = (
                (replace(args, old_sha256="0" * 64), "digest changed"),
                (replace(args, todo_sha256="0" * 64), "digest changed"),
                (replace(args, old_queue_sha256="0" * 64), "ordered queue changed"),
                (replace(args, old_pcodx_state_sha256="0" * 64), "identity, session, or custody changed"),
                (replace(args, old_pcodx_ledger_sha256="0" * 64), "ledger bytes changed"),
                (replace(args, old_pcodx_wrapper_sha256="0" * 64), "wrapper bytes changed"),
                (replace(args, protected_targets_sha256="0" * 64), "protected pane/process inventory changed"),
                (replace(args, protected_targets=()), "included in the exact protected"),
                (replace(args, children=args.children[:-1]), "active child set changed"),
                (replace(args, authority_sha256="0" * 64), "authority digest changed"),
                (replace(args, authority_envelope_sha256="0" * 64), "envelope block digest changed"),
                (replace(args, authority_envelope_file_sha256="0" * 64), "envelope file bytes changed"),
            )
            for changed, error in cases:
                with self.subTest(error=error):
                    inventory, state, stopped, proof = self.pcodx_runtime(changed, pcodx)
                    with inventory, state, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, error):
                        replace_manager(changed)
                    stop_mock.assert_not_called()
                    self.assertFalse(args.audit_output.exists())

            indirect = (root / args.authority_file).read_text(encoding="utf-8").replace(f"Close {args.old_target}.", f"Please consider stopping {args.old_target}.")
            (root / args.authority_file).write_text(indirect, encoding="utf-8")
            envelope = f'<human_instruction authoritative="true" source="{args.authority_file}:1-4">\n{indirect}</human_instruction>\n'
            (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
            indirect_args = replace(
                args,
                authority_sha256=sha(indirect),
                authority_envelope_sha256=sha(envelope),
                authority_envelope_file_sha256=sha(envelope),
            )
            inventory, state, stopped, proof = self.pcodx_runtime(indirect_args, pcodx)
            with inventory, state, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "directly close"):
                replace_manager(indirect_args)
            stop_mock.assert_not_called()

    def test_pcodx_replacement_rejects_changed_pane_process_session_and_non_pcodx_old_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, pcodx = self.pcodx_fixture(Path(tmp))
            drifted = dict(pcodx)
            drifted["PCODX_SESSION_ID"] = "different-session"
            inventory, state, stopped, proof = self.pcodx_runtime(args, drifted)
            with inventory, state, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "identity, session, or custody changed"):
                replace_manager(args)
            stop_mock.assert_not_called()

            wrong_tool = (root / args.old_task).read_text(encoding="utf-8").replace("tool: pcodx", "tool: codex")
            (root / args.old_task).write_text(wrong_tool, encoding="utf-8")
            changed = replace(args, old_sha256=sha(wrong_tool))
            inventory, state, stopped, proof = self.pcodx_runtime(changed, pcodx)
            with inventory, state, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "exact live long-running"):
                replace_manager(changed)
            stop_mock.assert_not_called()

            (root / args.old_task).write_text(wrong_tool.replace("tool: codex", "tool: pcodx"), encoding="utf-8")
            identity = PaneIdentity(manager_replace.canonical_target(args.old_target), args.old_pane_id, args.old_pane_pid + 1, args.old_pane_start_ticks)
            with (
                patch.object(manager_replace, "pane_inventory", return_value={identity.target: identity}),
                patch.object(manager_replace, "pcodx_state", return_value=pcodx),
                patch.object(manager_replace, "stop") as stop_mock,
                self.assertRaisesRegex(ReplaceError, "pane identity changed"),
            ):
                replace_manager(args)
            stop_mock.assert_not_called()

    def test_pcodx_exact_pre_input_seam_rejects_lifecycle_drift_without_closing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files, pcodx = self.pcodx_fixture(Path(tmp))
            owner_live = True

            def inventory() -> dict[str, PaneIdentity]:
                if not owner_live:
                    return {}
                identity = PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999)
                return {identity.target: identity}

            def drift_at_pre_input(stop_args: object) -> str:
                nonlocal owner_live
                (root / "child_4.md").write_text(files["child_4.md"] + "exact seam drift\n", encoding="utf-8")
                stop_args.bound_pre_input_check()
                owner_live = False
                return args.old_session_id

            with (
                self.whole_tree_authority(args),
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "pcodx_state", return_value=pcodx),
                patch.object(manager_replace, "stop", side_effect=drift_at_pre_input),
                patch.object(manager_replace, "has_bound_close_proof", return_value=False),
                self.assertRaisesRegex(ReplaceError, "manager stop failed before lifecycle mutation: pre-close active child"),
            ):
                replace_manager(args)
            self.assertTrue(owner_live)
            self.assertEqual(files[args.old_task], (root / args.old_task).read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())

    def test_stale_task_todo_or_child_digest_rejects_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            cases = (
                replace(args, old_sha256="0" * 64),
                replace(args, todo_sha256="0" * 64),
                replace(args, children=(ChildPin("child_a.md", "0" * 64), args.children[1])),
            )
            for index, changed in enumerate(cases):
                with self.subTest(index=index):
                    state = {"old_live": True}
                    inventory, stopped, proof = self.runtime(state)
                    with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "digest changed"):
                        replace_manager(changed)
                    stop_mock.assert_not_called()
                    self.assertFalse(args.audit_output.exists())
                    self.assertEqual(files["failed_manager.md"], (root / "failed_manager.md").read_text(encoding="utf-8"))

    def test_authority_source_and_envelope_are_digest_bound_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            cases = (
                replace(args, authority_sha256="0" * 64),
                replace(args, authority_envelope_sha256="0" * 64),
            )
            for changed in cases:
                with self.subTest(changed=changed):
                    inventory, stopped, proof = self.runtime({"old_live": True})
                    with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "authority.*digest"):
                        replace_manager(changed)
                    stop_mock.assert_not_called()

            malformed = "Agent-authored routing text without an authoritative envelope.\n"
            (root / args.authority_envelope_task).write_text(malformed, encoding="utf-8")
            malformed_args = replace(args, authority_envelope_sha256=sha(malformed))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "expected blocks"):
                replace_manager(malformed_args)
            stop_mock.assert_not_called()

    def test_authority_envelope_child_alias_is_rejected_outside_source1292_descendant_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            changed = replace(args, authority_envelope_task=args.children[0].task)
            with self.assertRaisesRegex(ReplaceError, "alias is restricted"):
                replace_manager(changed)

    def test_authority_block_digest_ignores_unrelated_outer_envelope_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            original = (root / args.authority_envelope_task).read_text(encoding="utf-8")
            block = original.rstrip("\n")
            wrapped = f"manager-authentication-wrapper: v1\nrouting-note: unrelated outer text\n\n{block}\npostscript: unrelated outer text\n"
            (root / args.authority_envelope_task).write_text(wrapped, encoding="utf-8")
            changed = replace(args, authority_envelope_sha256=sha(block + "\n"))
            plan = manager_replace.prepare(changed, manager_replace.markdown_paths(root))
            self.assertEqual(wrapped.encode(), plan.authority_envelope.data)
            self.assertIn("envelope-block-sha256", plan.successor_queue[-1])

    def test_authority_range_trailing_blank_uses_envelope_body_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            source = "".join(AUTHORITY_LINES) + "\n"
            authority = root / args.authority_file
            authority.write_text(source, encoding="utf-8")
            body = "".join(AUTHORITY_LINES).rstrip("\n")
            envelope = f'<human_instruction authoritative="true" source="{args.authority_file}:1-6">\n{body}\n</human_instruction>\n'
            envelope_path = root / args.authority_envelope_task
            envelope_path.write_text(envelope, encoding="utf-8")
            changed = replace(
                args,
                authority_lines=LineRange(1, 6),
                authority_sha256=sha(source),
                authority_envelope_sha256=sha(envelope),
            )
            plan = manager_replace.prepare(changed, manager_replace.markdown_paths(root))
            self.assertEqual(source.encode(), plan.authority.data)
            self.assertEqual(envelope.encode(), plan.authority_envelope.data)

    def test_parse_args_accepts_tmux_zero_pane_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = [
                "--root",
                str(root),
                "--old-task",
                "old.md",
                "--successor-task",
                "new.md",
                "--old-target",
                "mgr:1.0",
                "--new-target",
                "mgr:1.1",
                "--parent-target",
                "parent:0",
                "--old-sha256",
                "a" * 64,
                "--todo-sha256",
                "b" * 64,
                "--old-pane-id",
                "%0",
                "--old-pane-pid",
                "42",
                "--old-pane-start-ticks",
                "99",
                "--old-session-id",
                SESSION_ID,
                "--authority-file",
                "manager_mail/source.txt",
                "--authority-lines",
                "1-1",
                "--authority-sha256",
                "c" * 64,
                "--authority-envelope-task",
                "envelope.md",
                "--authority-envelope-sha256",
                "d" * 64,
                "--successor-item-lines",
                "1-1",
                "--audit-output",
                str(root / "audit.json"),
                "--preparer",
                "a",
                "--reviewer",
                "b",
            ]
            self.assertEqual("%0", parse_args(source).old_pane_id)

    def test_parse_args_accepts_exact_source1269_queue_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.guest1269_fixture(Path(tmp))
            source = [
                "--root",
                str(args.root),
                "--old-task",
                args.old_task,
                "--successor-task",
                args.successor_task,
                "--old-target",
                args.old_target,
                "--new-target",
                args.new_target,
                "--parent-target",
                args.parent_target,
                "--old-sha256",
                args.old_sha256,
                "--todo-sha256",
                args.todo_sha256,
                "--old-pane-id",
                args.old_pane_id,
                "--old-pane-pid",
                str(args.old_pane_pid),
                "--old-pane-start-ticks",
                str(args.old_pane_start_ticks),
                "--old-session-id",
                args.old_session_id,
                "--authority-file",
                args.authority_file,
                "--authority-lines",
                f"{args.authority_lines.start}-{args.authority_lines.end}",
                "--authority-sha256",
                args.authority_sha256,
                "--authority-envelope-task",
                args.authority_envelope_task,
                "--authority-envelope-sha256",
                args.authority_envelope_sha256,
                "--successor-item-lines",
                f"{args.successor_item_lines[0].start}-{args.successor_item_lines[0].end}",
                "--audit-output",
                str(args.audit_output),
                "--preparer",
                args.preparer,
                "--reviewer",
                args.reviewer,
                "--old-queue-sha256",
                args.old_queue_sha256,
            ]
            for child in args.children:
                source.extend(("--child", f"{child.task}={child.sha256}"))
            parsed_args = parse_args(source)
            self.assertEqual(args.old_queue_sha256, parsed_args.old_queue_sha256)
            self.assertEqual(args.children, parsed_args.children)

    def test_parse_args_accepts_source1485_child_queue_and_inventory_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _identities = self.source1485_root_fixture(Path(tmp))
            source = [
                "--root",
                str(args.root),
                "--old-task",
                args.old_task,
                "--successor-task",
                args.successor_task,
                "--old-target",
                args.old_target,
                "--new-target",
                args.new_target,
                "--parent-target",
                args.parent_target,
                "--old-sha256",
                args.old_sha256,
                "--todo-sha256",
                args.todo_sha256,
                "--old-pane-id",
                args.old_pane_id,
                "--old-pane-pid",
                str(args.old_pane_pid),
                "--old-pane-start-ticks",
                str(args.old_pane_start_ticks),
                "--old-session-id",
                args.old_session_id,
                "--authority-file",
                args.authority_file,
                "--authority-lines",
                f"{args.authority_lines.start}-{args.authority_lines.end}",
                "--authority-sha256",
                args.authority_sha256,
                "--authority-envelope-task",
                args.authority_envelope_task,
                "--authority-envelope-sha256",
                args.authority_envelope_sha256,
                "--authority-envelope-file-sha256",
                args.authority_envelope_file_sha256,
                "--successor-item-lines",
                f"{args.successor_item_lines[0].start}-{args.successor_item_lines[0].end}",
                "--protected-targets-sha256",
                args.protected_targets_sha256,
                "--old-queue-sha256",
                args.old_queue_sha256,
                "--audit-output",
                str(args.audit_output),
                "--preparer",
                args.preparer,
                "--reviewer",
                args.reviewer,
            ]
            for child in args.children:
                source.extend(("--child", f"{child.task}={child.sha256}={child.queue_sha256}"))
            for target in args.protected_targets:
                source.extend(("--protected-target", target))
            parsed_args = parse_args(source)
            self.assertEqual(args.children, parsed_args.children)
            self.assertEqual(args.protected_targets, parsed_args.protected_targets)
            self.assertEqual(args.protected_targets_sha256, parsed_args.protected_targets_sha256)

    def test_pane_inventory_accepts_tmux_zero_pane_id(self) -> None:
        result = manager_replace.subprocess.CompletedProcess(
            ["tmux", "list-panes"],
            0,
            "mgr:1.0\t%0\t42\t0\n",
            "",
        )
        with (
            patch.object(manager_replace.subprocess, "run", return_value=result),
            patch.object(manager_replace, "process_start_ticks", return_value=99),
        ):
            self.assertEqual(
                {"mgr:1.0": PaneIdentity("mgr:1.0", "%0", 42, 99)},
                pane_inventory(),
            )

    def test_pane_inventory_ignores_only_tmux_confirmed_dead_panes(self) -> None:
        result = manager_replace.subprocess.CompletedProcess(
            ["tmux", "list-panes"],
            0,
            "stale:0.0\t%1\t999\t1\nmgr:1.0\t%42\t42\t0\n",
            "",
        )
        with (
            patch.object(manager_replace.subprocess, "run", return_value=result),
            patch.object(manager_replace, "process_start_ticks", return_value=99) as ticks,
        ):
            self.assertEqual(
                {"mgr:1.0": PaneIdentity("mgr:1.0", "%42", 42, 99)},
                pane_inventory(),
            )
        ticks.assert_called_once_with(42)

    def test_pane_inventory_fails_closed_for_unprovable_live_pane(self) -> None:
        result = manager_replace.subprocess.CompletedProcess(
            ["tmux", "list-panes"],
            0,
            "mgr:1.0\t%42\t42\t0\n",
            "",
        )
        with (
            patch.object(manager_replace.subprocess, "run", return_value=result),
            patch.object(manager_replace, "process_start_ticks", return_value=None),
            self.assertRaisesRegex(ReplaceError, "cannot prove one process identity"),
        ):
            pane_inventory()

    def test_authenticated_authority_without_failure_evidence_cannot_close_healthy_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            benign_lines = (
                "Subject: status\n",
                "\n",
                "Keep the existing manager running.\n",
                "It completed the requested experiment.\n",
                "Do not replace it.\n",
            )
            source = "".join(benign_lines)
            envelope = f'<human_instruction authoritative="true" source="manager_mail/source-1220.txt:1-5">\n{source}</human_instruction>\n'
            (root / args.authority_file).write_text(source, encoding="utf-8")
            (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
            changed = replace(args, authority_sha256=sha(source), authority_envelope_sha256=sha(envelope))
            inventory, stopped, proof = self.runtime({"old_live": True}, changed.old_target, changed.new_target)
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "does not explicitly prove"):
                replace_manager(changed)
            stop_mock.assert_not_called()

    def test_exact_source1269_guest_replacement_allows_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.guest1269_fixture(Path(tmp))
            inventory, stopped, proof = self.runtime({"old_live": True}, args.old_target, args.new_target)
            with inventory, stopped as stop_mock, proof:
                replace_manager(args)
            self.assertEqual(30.0, stop_mock.call_args.args[0].wait_s)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_source1269_guest_replacement_rejects_later_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.guest1269_fixture(Path(tmp))
            source = files[args.authority_file].replace("Replace them.", "Replace them. Do not replace them.")
            excerpt = "".join(source.splitlines(keepends=True)[2:9])
            envelope = f'<human_instruction authoritative="true" source="{args.authority_file}:3-9">\n{excerpt}</human_instruction>\n'
            (root / args.authority_file).write_text(source, encoding="utf-8")
            (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
            changed = replace(args, authority_sha256=sha(source), authority_envelope_sha256=sha(envelope))
            inventory, stopped, proof = self.runtime({"old_live": True}, changed.old_target, changed.new_target)
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "does not explicitly prove"):
                replace_manager(changed)
            stop_mock.assert_not_called()

    def test_source1269_guest_replacement_requires_exact_ordered_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.guest1269_fixture(Path(tmp))
            for queue_sha256, error in (("", "requires the full ordered queue"), ("0" * 64, "ordered queue changed")):
                with self.subTest(error=error):
                    changed = replace(args, old_queue_sha256=queue_sha256)
                    inventory, stopped, proof = self.runtime({"old_live": True}, changed.old_target, changed.new_target)
                    with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, error):
                        replace_manager(changed)
                    stop_mock.assert_not_called()

    def test_source1269_guest_replacement_rejects_agent_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.guest1269_fixture(Path(tmp))
            source = files[args.authority_file].replace("The guest has reported", "An agent claimed: The guest has reported")
            excerpt = "".join(source.splitlines(keepends=True)[2:9])
            envelope = f'<human_instruction authoritative="true" source="{args.authority_file}:3-9">\n{excerpt}</human_instruction>\n'
            (root / args.authority_file).write_text(source, encoding="utf-8")
            (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
            changed = replace(args, authority_sha256=sha(source), authority_envelope_sha256=sha(envelope))
            inventory, stopped, proof = self.runtime({"old_live": True}, changed.old_target, changed.new_target)
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "does not explicitly prove"):
                replace_manager(changed)
            stop_mock.assert_not_called()

    def test_source1269_guest_replacement_rejects_other_source_or_manager(self) -> None:
        for other_source in (True, False):
            with self.subTest(other_source=other_source), tempfile.TemporaryDirectory() as tmp:
                if other_source:
                    root, guest_args, files = self.guest1269_fixture(Path(tmp))
                    source_file = "manager_mail/85c5dff58359-other.txt"
                    source = files[guest_args.authority_file]
                    source_path = root / source_file
                    source_path.write_text(source, encoding="utf-8")
                    source_path.chmod(0o600)
                    excerpt = "".join(source.splitlines(keepends=True)[2:9])
                    envelope = f'<human_instruction authoritative="true" source="{source_file}:3-9">\n{excerpt}</human_instruction>\n'
                    (root / guest_args.authority_envelope_task).write_text(envelope, encoding="utf-8")
                    changed = replace(
                        guest_args,
                        authority_file=source_file,
                        authority_sha256=sha(source),
                        authority_envelope_sha256=sha(envelope),
                        old_queue_sha256="",
                    )
                else:
                    root, changed, _files = self.fixture(Path(tmp))
                    source_file, _line_range, _task, _target, evidence = manager_replace.GUEST1269_REPLACEMENT
                    source = f"Subject: Fix guest mail handling\n\n{evidence}\n"
                    source_path = root / source_file
                    source_path.write_text(source, encoding="utf-8")
                    source_path.chmod(0o600)
                    excerpt = "".join(source.splitlines(keepends=True)[2:9])
                    envelope = f'<human_instruction authoritative="true" source="{source_file}:3-9">\n{excerpt}</human_instruction>\n'
                    (root / changed.authority_envelope_task).write_text(envelope, encoding="utf-8")
                    changed = replace(
                        changed,
                        authority_file=source_file,
                        authority_lines=LineRange(3, 9),
                        authority_sha256=sha(source),
                        authority_envelope_sha256=sha(envelope),
                        successor_item_lines=(LineRange(3, 9),),
                    )
                inventory, stopped, proof = self.runtime({"old_live": True}, changed.old_target, changed.new_target)
                with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "does not explicitly prove"):
                    replace_manager(changed)
                stop_mock.assert_not_called()

    def test_non_long_running_manager_cannot_be_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            healthy = task_text(
                status="running",
                runat=OLD_TARGET,
                managerat=PARENT_TARGET,
                is_manager=True,
                pending=OLD_QUEUE,
                session_id=SESSION_ID,
            )
            (root / args.old_task).write_text(healthy, encoding="utf-8")
            changed = replace(args, old_sha256=sha(healthy))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "exact live long-running failed-manager"):
                replace_manager(changed)
            stop_mock.assert_not_called()

    def test_concurrent_todo_change_is_preserved_and_owned_writes_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))

            def mutate_todo() -> None:
                (root / "TODO.md").write_text(files["TODO.md"] + "external concurrent note\n", encoding="utf-8")

            with self.assertRaisesRegex(ReplaceError, "owned lifecycle writes rolled back"):
                self.run_replacement(args, {"old_live": True, "stop_hook": mutate_todo})
            self.assertEqual(files["failed_manager.md"], (root / "failed_manager.md").read_text(encoding="utf-8"))
            self.assertEqual(files["child_a.md"], (root / "child_a.md").read_text(encoding="utf-8"))
            self.assertEqual(files["child_b.md"], (root / "child_b.md").read_text(encoding="utf-8"))
            self.assertTrue((root / "TODO.md").read_text(encoding="utf-8").endswith("external concurrent note\n"))
            self.assertEqual("rolled_back", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_whole_tree_closes_every_pinned_descendant_before_manager_and_commits_one_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.whole_tree_fixture(Path(tmp))
            live = {manager_replace.canonical_target(args.old_target), *(manager_replace.canonical_target(item.target) for item in args.descendants)}
            identities = {
                manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
                **{
                    manager_replace.canonical_target(item.target): PaneIdentity(manager_replace.canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks)
                    for item in args.descendants
                },
            }
            sessions = {item.target: item.session_id for item in args.descendants} | {args.old_target: args.old_session_id}
            order: list[str] = []

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target in live}

            def stopped(stop_args) -> str:
                order.append(stop_args.target)
                live.remove(manager_replace.canonical_target(stop_args.target))
                Path(stop_args.bound_close_proof_path).write_text(f"{stop_args.bound_close_proof_secret}\n", encoding="utf-8")
                Path(stop_args.bound_close_proof_path).chmod(0o600)
                return sessions[stop_args.target]

            with (
                self.whole_tree_authority(args),
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop", side_effect=stopped),
            ):
                result = replace_manager(args)
            self.assertEqual([item.target for item in args.descendants] + [args.old_target], order)
            self.assertIn("sole ownership", result)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)
            self.assertEqual((), tuple(live))

    def test_whole_tree_descendant_drift_fails_before_any_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.whole_tree_fixture(Path(tmp))
            inventory = {manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999)}
            with (
                self.whole_tree_authority(args),
                patch.object(manager_replace, "pane_inventory", return_value=inventory),
                patch.object(manager_replace, "stop") as stop_mock,
                self.assertRaisesRegex(ReplaceError, "active descendant pane identity changed"),
            ):
                replace_manager(args)
            stop_mock.assert_not_called()

    def test_source1289_cannot_use_legacy_manager_only_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            changed = replace(
                args,
                old_task=manager_replace.SOURCE1289_TASK,
                authority_file=manager_replace.SOURCE1289_FILE,
            )
            with self.assertRaisesRegex(ReplaceError, "descendants or exact Source-1292"):
                replace_manager(changed)

    def test_source1289_literal_authenticated_wording_is_semantic_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args = self.source1289_authority_fixture(Path(tmp))
            selected = "\n".join(SOURCE1289_AUTHORITY.splitlines()[2:10])
            self.assertIsNone(manager_replace.FAILED_MANAGER_EVIDENCE_RE.search(selected))
            self.assertLess(selected.index("replace the entire agent tree"), selected.index("completely failed"))
            self.assertIn("new agents", selected)
            self.assertIn("agents completely failed", selected)
            self.assertIn("repository has not been split", selected)
            runtime, stopped_targets = self.source1289_runtime(args)
            with runtime:
                result = replace_manager(args)
            self.assertIn("sole ownership", result)
            self.assertEqual([item.target for item in args.descendants] + [args.old_target], stopped_targets)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_source1289_semantic_authority_rejects_every_reuse_dimension(self) -> None:
        variants = (
            ("task", {"old_task": "other.md"}),
            ("source", {"authority_file": "manager_mail/other.txt"}),
            ("digest", {"authority_sha256": "0" * 64}),
            ("carrier", {"authority_lines": LineRange(1, 12)}),
            ("selection", {"successor_item_lines": (LineRange(3, 9),)}),
            ("extra-selection", {"successor_item_lines": (LineRange(3, 10), LineRange(12, 12))}),
        )
        for label, changes in variants:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                _root, args = self.source1289_authority_fixture(Path(tmp))
                variant = replace(args, **changes)
                runtime, stopped_targets = self.source1289_runtime(variant)
                with runtime, self.assertRaises(ReplaceError):
                    replace_manager(variant)
                self.assertEqual([], stopped_targets)

    def test_source1443_semantic_exception_is_exact_and_non_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            exact = replace(
                args,
                old_task=manager_replace.SOURCE1443_TASK,
                old_target=manager_replace.SOURCE1443_OLD_TARGET,
                new_target="pb:13",
                authority_file=manager_replace.SOURCE1443_FILE,
                authority_sha256=manager_replace.SOURCE1443_SHA256,
                authority_lines=LineRange(*manager_replace.SOURCE1443_CARRIER_LINES),
                successor_item_lines=(LineRange(*manager_replace.SOURCE1443_SUCCESSOR_LINES),),
            )
            self.assertTrue(manager_replace.is_source1443_semantic_exception(exact))
            variants = (
                {"old_task": "other.md"},
                {"old_target": "wl:9"},
                {"new_target": "wl:9"},
                {"authority_file": "manager_mail/other.txt"},
                {"authority_sha256": "0" * 64},
                {"authority_lines": LineRange(1, 2)},
                {"successor_item_lines": (LineRange(2, 2),)},
                {"successor_item_lines": (LineRange(3, 3), LineRange(3, 3))},
            )
            for changes in variants:
                with self.subTest(changes=changes):
                    self.assertFalse(manager_replace.is_source1443_semantic_exception(replace(exact, **changes)))

    def test_source1477_semantic_exception_is_exact_and_non_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            for old_task, old_target, new_session in manager_replace.SOURCE1477_REPLACEMENTS:
                exact = replace(
                    args,
                    old_task=old_task,
                    old_target=old_target,
                    new_target=f"{new_session}:19",
                    authority_file=manager_replace.SOURCE1477_FILE,
                    authority_sha256=manager_replace.SOURCE1477_SHA256,
                    authority_lines=LineRange(*manager_replace.SOURCE1477_CARRIER_LINES),
                    successor_item_lines=(LineRange(*manager_replace.SOURCE1477_SUCCESSOR_LINES),),
                )
                self.assertTrue(manager_replace.is_source1477_semantic_exception(exact))
                variants = (
                    {"old_task": "other.md"},
                    {"old_target": "wl:9"},
                    {"new_target": "wl:19"},
                    {"authority_file": "manager_mail/other.txt"},
                    {"authority_sha256": "0" * 64},
                    {"authority_lines": LineRange(1, 5)},
                    {"successor_item_lines": (LineRange(3, 5),)},
                    {"successor_item_lines": (LineRange(3, 6), LineRange(3, 6))},
                )
                for changes in variants:
                    with self.subTest(old_task=old_task, changes=changes):
                        self.assertFalse(manager_replace.is_source1477_semantic_exception(replace(exact, **changes)))

    def test_source1477_replacements_retain_transactional_gates(self) -> None:
        for old_task, old_target, new_session in manager_replace.SOURCE1477_REPLACEMENTS:
            with self.subTest(old_task=old_task), tempfile.TemporaryDirectory() as tmp:
                root, args = self.source1477_fixture(Path(tmp), old_task, old_target, new_session)
                state = {"old_live": True}
                runtime = self.runtime(state, old_target, args.new_target)
                with runtime[0], runtime[1], runtime[2]:
                    result = replace_manager(args)
                self.assertIn("sole ownership", result)
                self.assertEqual("done", parsed(root / old_task, root).status)
                self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

        with tempfile.TemporaryDirectory() as tmp:
            _root, args = self.source1477_fixture(Path(tmp), "personal_browser_mgr_pb.md", "pb:13.0", "pb")
            state = {"old_live": True, "new_live": True}
            inventory, _stopped, proof = self.runtime(state, args.old_target, args.new_target)
            with inventory, proof, patch.object(manager_replace, "stop") as stop_mock, self.assertRaisesRegex(ReplaceError, "successor target is already live"):
                replace_manager(args)
            stop_mock.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            root, args = self.source1477_fixture(Path(tmp), "dw_fpr_mgr.md", "dw:5.0", "dw")
            (root / args.children[0].task).write_text("concurrent child drift\n", encoding="utf-8")
            with patch.object(manager_replace, "stop") as stop_mock, self.assertRaises(ReplaceError):
                replace_manager(args)
            stop_mock.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            _root, args = self.source1477_fixture(Path(tmp), "personal_browser_mgr_pb.md", "pb:13.0", "pb")
            with (
                patch.object(manager_replace, "pane_inventory", return_value={}),
                patch.object(manager_replace, "stop") as stop_mock,
                self.assertRaisesRegex(ReplaceError, "old manager pane identity changed"),
            ):
                replace_manager(args)
            stop_mock.assert_not_called()

    def test_source1485_exact_root_replacement_binds_acyclic_custody(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, identities = self.source1485_root_fixture(Path(tmp))
            state = {"old_live": True}

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target != "dw:0.0" or state["old_live"]}

            def stopped(_args: object) -> str:
                state["old_live"] = False
                return SESSION_ID

            with (
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "stop", side_effect=stopped),
                patch.object(manager_replace, "has_bound_close_proof", side_effect=lambda *_args: not state["old_live"]),
            ):
                result = replace_manager(args)
            self.assertIn("sole ownership", result)
            self.assertEqual("done", parsed(root / args.old_task, root).status)
            successor = parsed(root / args.successor_task, root)
            self.assertEqual("blocked", successor.status)
            self.assertEqual("config:1", successor.managerat)
            self.assertEqual(tuple(child.task for child in args.children), manager_replace.active_child_task_refs(root, root / args.successor_task, args.new_target))
            audit = json.loads(args.audit_output.read_text(encoding="utf-8"))
            topology = audit["source1485_topology"]
            self.assertTrue(topology["acyclic"])
            self.assertEqual(
                "replacement-subtree-plus-complete-active-manager-parent-ancestry",
                topology["acyclic_scope"],
            )
            self.assertEqual("coordinator.md", topology["ancestor_rows"][0]["task"])
            self.assertEqual(manager_replace.json_digest(topology), audit["source1485_topology_sha256"])
            self.assertEqual(args.protected_targets_sha256, manager_replace.json_digest(audit["protected_inventory"]))

    def test_source1485_authority_and_mapping_are_non_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _identities = self.source1485_root_fixture(Path(tmp))
            for old_task, old_target, successor_task, new_target, parent_target in manager_replace.SOURCE1485_REPLACEMENTS:
                exact = replace(
                    args,
                    old_task=old_task,
                    old_target=old_target,
                    successor_task=successor_task,
                    new_target=new_target,
                    parent_target=parent_target,
                )
                self.assertTrue(manager_replace.is_source1485_semantic_exception(exact))
            variants = (
                {"old_task": "other.md"},
                {"successor_task": "other.md"},
                {"old_target": "dw:1"},
                {"new_target": "dw:16"},
                {"parent_target": "wl:7"},
                {"authority_file": "manager_mail/other.txt"},
                {"authority_sha256": "0" * 64},
                {"authority_envelope_task": "other.md"},
                {"authority_envelope_sha256": "0" * 64},
                {"authority_lines": LineRange(2, 12)},
                {"successor_item_lines": (LineRange(3, 10),)},
            )
            for changes in variants:
                with self.subTest(changes=changes):
                    self.assertFalse(manager_replace.is_source1485_semantic_exception(replace(args, **changes)))

    def test_source1485_nonroot_replacements_preserve_exact_discovered_trees(self) -> None:
        replacements = manager_replace.SOURCE1485_REPLACEMENTS[:2]
        for index, replacement in enumerate(replacements):
            with self.subTest(old_task=replacement[0]), tempfile.TemporaryDirectory() as tmp:
                root, args, identities = self.source1485_nonroot_fixture(Path(tmp), replacement)
                state = {"old_live": True}

                def inventory() -> dict[str, PaneIdentity]:
                    return {target: identity for target, identity in identities.items() if target != manager_replace.canonical_target(args.old_target) or state["old_live"]}

                def stopped(_args: object) -> str:
                    state["old_live"] = False
                    return SESSION_ID

                with (
                    patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                    patch.object(manager_replace, "stop", side_effect=stopped),
                    patch.object(manager_replace, "has_bound_close_proof", side_effect=lambda *_args: not state["old_live"]),
                ):
                    result = replace_manager(args)
                self.assertIn("sole ownership", result)
                successor = parsed(root / args.successor_task, root)
                self.assertEqual("blocked", successor.status)
                self.assertEqual(args.parent_target, successor.managerat)
                self.assertEqual(
                    tuple(child.task for child in args.children),
                    manager_replace.active_child_task_refs(root, root / args.successor_task, args.new_target),
                )
                audit = json.loads(args.audit_output.read_text(encoding="utf-8"))
                topology = audit["source1485_topology"]
                self.assertEqual(args.successor_task, topology["root_task"])
                self.assertEqual(2 + 2 * index, len(topology["rows"]))
                self.assertEqual(
                    "replacement-subtree-plus-immediate-parent-owner",
                    topology["acyclic_scope"],
                )
                self.assertEqual(["parent_manager.md"], [row["task"] for row in topology["ancestor_rows"]])

    def test_source1485_nonroot_replacement_requires_unique_reporting_parent_manager(self) -> None:
        for mode in ("absent", "non_manager", "duplicate"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root, args, identities = self.source1485_nonroot_fixture(
                    Path(tmp),
                    manager_replace.SOURCE1485_REPLACEMENTS[0],
                )
                if mode == "absent":
                    (root / "parent_manager.md").unlink()
                elif mode == "non_manager":
                    parent = (root / "parent_manager.md").read_text(encoding="utf-8")
                    (root / "parent_manager.md").write_text(
                        parent.replace("is_manager: true", "is_manager: false"),
                        encoding="utf-8",
                    )
                else:
                    duplicate = task_text(
                        status="running",
                        runat=args.parent_target,
                        managerat="wl:7",
                        is_manager=True,
                        pending=(),
                    )
                    (root / "duplicate_parent.md").write_text(duplicate, encoding="utf-8")
                with (
                    patch.object(manager_replace, "pane_inventory", return_value=identities),
                    patch.object(manager_replace, "stop") as stop_mock,
                    self.assertRaisesRegex(
                        ReplaceError,
                        "requires exactly one active reporting-parent manager owner|multiple active manager owners",
                    ),
                ):
                    replace_manager(args)
                stop_mock.assert_not_called()

    def test_source1485_requires_old_task_record_session_custody(self) -> None:
        for recorded_session in ("", "ffffffff-2222-4333-8444-555555555555"):
            with self.subTest(recorded_session=recorded_session), tempfile.TemporaryDirectory() as tmp:
                root, args, identities = self.source1485_nonroot_fixture(
                    Path(tmp),
                    manager_replace.SOURCE1485_REPLACEMENTS[0],
                )
                old_path = root / args.old_task
                current = old_path.read_text(encoding="utf-8")
                if recorded_session:
                    changed = current.replace(f"session_id: {SESSION_ID}", f"session_id: {recorded_session}")
                else:
                    changed = current.replace(f"session_id: {SESSION_ID}\n", "")
                old_path.write_text(changed, encoding="utf-8")
                changed_args = replace(args, old_sha256=sha(changed))
                with (
                    patch.object(manager_replace, "pane_inventory", return_value=identities),
                    patch.object(manager_replace, "stop") as stop_mock,
                    self.assertRaisesRegex(ReplaceError, "exact live long-running failed-manager record"),
                ):
                    replace_manager(changed_args)
                stop_mock.assert_not_called()

    def test_source1485_nonroot_replacement_rejects_discovered_child_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, identities = self.source1485_nonroot_fixture(
                Path(tmp),
                manager_replace.SOURCE1485_REPLACEMENTS[0],
            )
            extra = task_text(
                status="running",
                runat="dwp:3",
                managerat=args.old_target,
                is_manager=False,
                pending=("Unexpected active child.",),
            )
            (root / "unexpected.md").write_text(extra, encoding="utf-8")
            identities["dwp:3.0"] = PaneIdentity("dwp:3.0", "%99", 4299, 999)
            with (
                patch.object(manager_replace, "pane_inventory", return_value=identities),
                self.assertRaisesRegex(ReplaceError, "active child set changed"),
            ):
                replace_manager(args)

    def test_source1485_reporting_parent_bytes_are_revalidated_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, identities = self.source1485_nonroot_fixture(
                Path(tmp),
                manager_replace.SOURCE1485_REPLACEMENTS[0],
            )
            with patch.object(manager_replace, "pane_inventory", return_value=identities):
                plan = manager_replace.prepare(args, manager_replace.markdown_paths(root))
                parent_path = root / "parent_manager.md"
                parent_path.write_text(
                    parent_path.read_text(encoding="utf-8").replace(
                        "Preserve parent coordination.",
                        "Concurrent parent coordination drift.",
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ReplaceError, "post-graph changed before guarded manager close"):
                    manager_replace.require_preclose_eligibility(args, plan)

    def test_source1485_root_rejects_cycle_queue_drift_and_omitted_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, identities = self.source1485_root_fixture(Path(tmp))
            cases = (
                ("queue", replace(args, children=tuple(replace(child, queue_sha256="0" * 64) if child.task == "dw_cc_sampling.md" else child for child in args.children)), "ordered queue"),
                ("omitted", replace(args, children=tuple(child for child in args.children if child.task != "dw_cc_sampling.md")), "root-child"),
            )
            for _name, changed, error in cases:
                with self.subTest(case=_name), patch.object(manager_replace, "pane_inventory", return_value=identities), self.assertRaisesRegex(ReplaceError, error):
                    replace_manager(changed)
            cycle_child = task_text(status="running", runat="config:1", managerat="wl:7", is_manager=False, pending=())
            (root / "cycle.md").write_text(cycle_child, encoding="utf-8")
            with patch.object(manager_replace, "pane_inventory", return_value=identities), self.assertRaisesRegex(ReplaceError, "childless|cycle"):
                replace_manager(args)

    def test_source1485_root_rejects_reporting_parent_edge_into_retained_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, identities = self.source1485_root_fixture(Path(tmp))
            coordinator = task_text(
                status="long_running",
                runat="config:1",
                managerat="dw:14",
                is_manager=True,
                pending=("Coordinate the DW replacement.",),
            )
            (root / "coordinator.md").write_text(coordinator, encoding="utf-8")
            with (
                patch.object(manager_replace, "pane_inventory", return_value=identities),
                patch.object(manager_replace, "stop") as stop_mock,
                self.assertRaisesRegex(ReplaceError, "repeats task|reporting edge.*cycle"),
            ):
                replace_manager(args)
            stop_mock.assert_not_called()

    def test_source1485_root_rejects_multi_hop_reporting_parent_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, identities = self.source1485_root_fixture(Path(tmp))
            coordinator = task_text(
                status="long_running",
                runat="config:1",
                managerat="config2:0",
                is_manager=True,
                pending=("Coordinate the DW replacement.",),
            )
            ancestor = task_text(
                status="long_running",
                runat="config2:0",
                managerat="config:1",
                is_manager=True,
                pending=("Retain upstream coordination.",),
            )
            (root / "coordinator.md").write_text(coordinator, encoding="utf-8")
            (root / "ancestor_manager.md").write_text(ancestor, encoding="utf-8")
            identities["config2:0.0"] = PaneIdentity("config2:0.0", "%99", 4299, 1099)
            protected = tuple(sorted((*args.protected_targets, "config2:0.0")))
            provisional = replace(args, protected_targets=protected)
            changed = replace(
                provisional,
                protected_targets_sha256=manager_replace.protected_inventory_digest(
                    provisional,
                    identities,
                ),
            )
            with (
                patch.object(manager_replace, "pane_inventory", return_value=identities),
                patch.object(manager_replace, "stop") as stop_mock,
                self.assertRaisesRegex(ReplaceError, "ancestry.*cycle"),
            ):
                replace_manager(changed)
            stop_mock.assert_not_called()

    def test_source1597_exception_is_exact_source_only_and_non_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, exact, _protected = self.source1597_fixture(Path(tmp))
            self.assertTrue(manager_replace.is_source1597_semantic_exception(exact))
            variants = (
                {"old_task": "other.md"},
                {"successor_task": "dw_fpr_wrong.md"},
                {"old_target": "dw:13"},
                {"new_target": "dw:17"},
                {"parent_target": "dw:18"},
                {"authority_file": "manager_mail/other.txt"},
                {"authority_sha256": "0" * 64},
                {"authority_lines": LineRange(2, 3)},
                {"successor_item_lines": (LineRange(2, 3),)},
                {"authority_envelope_task": "authority_envelope.md"},
                {"authority_envelope_sha256": "0" * 64},
            )
            for changes in variants:
                with self.subTest(changes=changes):
                    self.assertFalse(manager_replace.is_source1597_semantic_exception(replace(exact, **changes)))

    def test_source1597_replacement_preserves_queue_and_protected_panes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1597_fixture(Path(tmp))
            state: dict[str, object] = {"old_live": True}
            runtime = self.source1597_runtime(state, args, protected)
            with runtime[0], runtime[1], runtime[2]:
                result = replace_manager(args)
            self.assertIn("sole ownership", result)
            self.assertEqual((), parsed(root / args.old_task, root).pending_task_items)
            self.assertEqual(
                (
                    *SOURCE1597_QUEUE,
                    f"🧑 Source {args.authority_file}: {manager_replace.SOURCE1597_DIRECTIVE}",
                    f"🧑 Source {manager_replace.SOURCE1601_FILE}: {manager_replace.SOURCE1601_QUEUE_GOAL}",
                ),
                parsed(root / args.successor_task, root).pending_task_items,
            )
            self.assertEqual(args.new_target, parsed(root / args.successor_task, root).runat)
            record = json.loads(args.audit_output.read_text(encoding="utf-8"))
            self.assertEqual(manager_replace.SOURCE_ONLY_AUTHORITY_MODE, record["authority_mode"])
            self.assertEqual(args.old_queue_sha256, record["old_queue_sha256"])
            self.assertEqual(args.protected_targets_sha256, record["protected_targets_sha256"])

    def test_source1597_rejects_queue_source_and_protected_drift_before_mutation(self) -> None:
        cases = (
            ("queue", lambda args: replace(args, old_queue_sha256="0" * 64), "ordered queue changed"),
            ("source", lambda args: replace(args, authority_sha256="0" * 64), "source-only replacement"),
            ("protected", lambda args: replace(args, protected_targets_sha256="0" * 64), "protected pane/process inventory changed"),
        )
        for label, mutate, error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root, args, protected = self.source1597_fixture(Path(tmp))
                changed = mutate(args)
                runtime = self.source1597_runtime({"old_live": True}, changed, protected)
                before = (root / args.old_task).read_bytes()
                with runtime[0], runtime[1] as stop_mock, runtime[2], self.assertRaisesRegex(ReplaceError, error):
                    replace_manager(changed)
                stop_mock.assert_not_called()
                self.assertEqual(before, (root / args.old_task).read_bytes())
                self.assertFalse((root / args.successor_task).exists())

    def test_source1597_rejects_omitted_or_new_live_protected_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1597_fixture(Path(tmp))
            reduced = protected[:-1]
            changed = replace(
                args,
                protected_targets=tuple(identity.target for identity in reduced),
                protected_targets_sha256=manager_replace.json_digest(
                    [{"target": identity.target, "pane_id": identity.pane_id, "pid": identity.pid, "start_ticks": identity.start_ticks} for identity in reduced]
                ),
            )
            runtime = self.source1597_runtime({"old_live": True}, changed, protected)
            with runtime[0], runtime[1] as stop_mock, runtime[2], self.assertRaisesRegex(ReplaceError, "not the complete canonical live inventory"):
                replace_manager(changed)
            stop_mock.assert_not_called()
            self.assertFalse((root / args.successor_task).exists())

        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1597_fixture(Path(tmp))
            state: dict[str, object] = {"old_live": True, "extra_identity": PaneIdentity("late:1.0", "%70", 5070, 970)}
            runtime = self.source1597_runtime(state, args, protected)
            with runtime[0], runtime[1] as stop_mock, runtime[2], self.assertRaisesRegex(ReplaceError, "not the complete canonical live inventory"):
                replace_manager(args)
            stop_mock.assert_not_called()
            self.assertFalse((root / args.successor_task).exists())

    def test_source1597_revalidates_protected_inventory_after_stop_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1597_fixture(Path(tmp))
            state: dict[str, object] = {"old_live": True}
            state["stop_hook"] = lambda: state.__setitem__("protected_drift", True)
            runtime = self.source1597_runtime(state, args, protected)
            before = (root / args.old_task).read_bytes()
            with runtime[0], runtime[1], runtime[2], self.assertRaisesRegex(ReplaceError, "protected pane/process inventory changed"):
                replace_manager(args)
            self.assertEqual(before, (root / args.old_task).read_bytes())
            self.assertFalse((root / args.successor_task).exists())

    def test_source1611_exception_is_exact_and_requires_source1601(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, exact, _protected = self.source1611_fixture(Path(tmp))
            self.assertTrue(manager_replace.is_source1611_semantic_exception(exact))
            variants = (
                {"old_task": "other.md"},
                {"successor_task": "cleanup_dw_tree_bad.md"},
                {"old_target": "config:2"},
                {"new_target": "config:24"},
                {"parent_target": "wl:2"},
                {"authority_file": manager_replace.SOURCE1601_FILE},
                {"authority_sha256": "0" * 64},
                {"authority_lines": LineRange(2, 3)},
                {"successor_item_lines": (LineRange(2, 3),)},
                {"authority_envelope_task": "other.md"},
            )
            for changes in variants:
                with self.subTest(changes=changes):
                    self.assertFalse(manager_replace.is_source1611_semantic_exception(replace(exact, **changes)))
            source1601 = root / manager_replace.SOURCE1601_FILE
            source1601.write_bytes(source1601.read_bytes() + b"drift\n")
            runtime = self.source1597_runtime({"old_live": True}, exact, _protected)
            with runtime[0], runtime[1] as stop_mock, runtime[2], self.assertRaisesRegex(ReplaceError, "Source-1601 authority source or digest changed"):
                replace_manager(exact)
            stop_mock.assert_not_called()

    def test_source1611_replacement_preserves_only_human_queue_and_complete_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_fixture(Path(tmp))
            runtime = self.source1597_runtime({"old_live": True}, args, protected)
            with runtime[0], runtime[1], runtime[2]:
                result = replace_manager(args)
            self.assertIn("sole ownership", result)
            successor = parsed(root / args.successor_task, root)
            self.assertTrue(successor.is_manager)
            self.assertEqual(HUMAN_QUEUE, successor.pending_task_items)
            self.assertNotIn("Preserve this delegated body.", (root / args.successor_task).read_text(encoding="utf-8"))
            record = json.loads(args.audit_output.read_text(encoding="utf-8"))
            self.assertEqual(manager_replace.SOURCE1601_SHA256, record["source1601_sha256"])
            self.assertEqual(list(manager_replace.SOURCE1601_LINES), record["source1601_lines"])

    def test_source1611_rejects_agent_authored_queue_and_source_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_fixture(Path(tmp))
            old_path = root / args.old_task
            changed_text = old_path.read_text(encoding="utf-8").replace(HUMAN_QUEUE[1], "Agent-authored replacement strategy.")
            old_path.write_text(changed_text, encoding="utf-8")
            changed_queue = parsed(old_path, root).pending_task_items
            changed = replace(
                args,
                old_sha256=sha(changed_text),
                authority_envelope_sha256=sha(changed_text),
                old_queue_sha256=manager_replace.json_digest(list(changed_queue)),
            )
            runtime = self.source1597_runtime({"old_live": True}, changed, protected)
            with runtime[0], runtime[1] as stop_mock, runtime[2], self.assertRaisesRegex(ReplaceError, "Human-provenance queue changed"):
                replace_manager(changed)
            stop_mock.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_fixture(Path(tmp))
            state: dict[str, object] = {"old_live": True}
            source1601 = root / manager_replace.SOURCE1601_FILE
            state["stop_hook"] = lambda: source1601.write_bytes(source1601.read_bytes() + b"late drift\n")
            runtime = self.source1597_runtime(state, args, protected)
            before = (root / args.old_task).read_bytes()
            with runtime[0], runtime[1], runtime[2], self.assertRaisesRegex(ReplaceError, "Source-1601 replacement authority"):
                replace_manager(args)
            self.assertEqual(before, (root / args.old_task).read_bytes())
            self.assertFalse((root / args.successor_task).exists())

    def test_source1611_rejects_noncompliant_successor_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _protected = self.source1611_fixture(Path(tmp))
            changed = replace(args, successor_task="cleanup_dw_tree_successor.md")
            with self.assertRaisesRegex(ReplaceError, "ordered queue binding|exact replacement program|shorter than 25"):
                manager_replace.validate_targets(changed)

    def test_source1611_direct_worker_mapping_is_exact_and_nonrecursive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, exact, _protected = self.source1611_direct_worker_fixture(Path(tmp))
            self.assertTrue(manager_replace.is_source1611_direct_worker_semantic_exception(exact))
            self.assertTrue(manager_replace.is_source1611_semantic_exception(exact))
            self.assertEqual((), manager_replace.source_only_added_goals(exact, manager_replace.SOURCE1611_DIRECT_WORKER_OLD_QUEUE))
            variants = (
                {"old_task": "other.md"},
                {"successor_task": "dw_root_other.md"},
                {"old_target": "dw:14"},
                {"new_target": "dw:17"},
                {"parent_target": "config:22"},
                {"authority_file": manager_replace.SOURCE1601_FILE},
                {"authority_sha256": "0" * 64},
                {"authority_lines": LineRange(2, 3)},
                {"successor_item_lines": (LineRange(2, 3),)},
                {"authority_envelope_task": "other.md"},
            )
            for changes in variants:
                with self.subTest(changes=changes):
                    self.assertFalse(manager_replace.is_source1611_direct_worker_semantic_exception(replace(exact, **changes)))
            with self.assertRaisesRegex(ReplaceError, "exact active parent SHA-256"):
                manager_replace.validate_targets(replace(exact, source1611_parent_sha256=""))

    def test_source1611_direct_worker_preserves_human_queue_and_migrates_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
            runtime = self.source1597_runtime({"old_live": True}, args, protected)
            with runtime[0], runtime[1], runtime[2]:
                result = replace_manager(args)
            self.assertIn("sole ownership", result)
            successor_text = (root / args.successor_task).read_text(encoding="utf-8")
            successor = parsed(root / args.successor_task, root)
            self.assertEqual(manager_replace.SOURCE1611_DIRECT_WORKER_OLD_QUEUE, successor.pending_task_items)
            self.assertNotIn(manager_replace.SOURCE1611_FILE, successor_text)
            self.assertNotIn(manager_replace.SOURCE1601_FILE, successor_text)
            self.assertNotIn("Preserve this delegated body.", successor_text)
            for child in args.children:
                child_metadata = parsed(root / child.task, root)
                self.assertEqual(args.new_target, child_metadata.managerat)
            record = json.loads(args.audit_output.read_text(encoding="utf-8"))
            self.assertEqual(manager_replace.SOURCE_ONLY_AUTHORITY_MODE, record["authority_mode"])
            self.assertEqual(args.old_queue_sha256, record["old_queue_sha256"])
            self.assertEqual(manager_replace.SOURCE1597_SHA256, record["source1597_sha256"])
            self.assertEqual(list(manager_replace.SOURCE1597_LINES), record["source1597_lines"])
            self.assertEqual(manager_replace.SOURCE1611_SUCCESSOR_TASK, record["source1611_parent_task"])
            self.assertEqual(args.source1611_parent_sha256, record["source1611_parent_sha256"])

    def test_source1611_direct_worker_requires_exact_sole_parent(self) -> None:
        for mode in ("absent", "alternate_owner"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
                parent_path = root / manager_replace.SOURCE1611_SUCCESSOR_TASK
                if mode == "absent":
                    parent_path.unlink()
                else:
                    (root / "other_config23_manager.md").write_text(
                        task_text(
                            status="long_running",
                            runat=args.parent_target,
                            managerat="wl:1",
                            is_manager=True,
                            pending=(),
                        ),
                        encoding="utf-8",
                    )
                runtime = self.source1597_runtime({"old_live": True}, args, protected)
                with runtime[0], runtime[1] as stop_mock, runtime[2], self.assertRaisesRegex(ReplaceError, "direct-worker parent"):
                    replace_manager(args)
                stop_mock.assert_not_called()
                self.assertFalse((root / args.successor_task).exists())

    def test_source1611_direct_worker_recovers_after_partial_child_migration(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
            original = manager_replace.replace_snapshot
            crashed = False

            def crash_after_first_child(expected, data, label):
                nonlocal crashed
                result = original(expected, data, label)
                if label == "active child child_a.md" and not crashed:
                    crashed = True
                    raise SimulatedCrash()
                return result

            state = {"old_live": True}
            runtime = self.source1597_runtime(state, args, protected)
            with runtime[0], runtime[1], runtime[2], patch.object(manager_replace, "replace_snapshot", side_effect=crash_after_first_child), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            self.assertFalse(state["old_live"])

            parent_path = root / manager_replace.SOURCE1611_SUCCESSOR_TASK
            parent_before = parent_path.read_bytes()
            parent_path.write_bytes(parent_before + b"concurrent parent drift\n")
            runtime = self.source1597_runtime(state, args, protected)
            with runtime[0], runtime[1], runtime[2], self.assertRaisesRegex(ReplaceError, "direct-worker parent task or digest changed"):
                replace_manager(args)
            parent_path.write_bytes(parent_before)

            runtime = self.source1597_runtime(state, args, protected)
            with runtime[0], runtime[1], runtime[2]:
                result = replace_manager(args)
            self.assertIn("sole ownership", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual(manager_replace.SOURCE1611_DIRECT_WORKER_OLD_QUEUE, parsed(root / args.successor_task, root).pending_task_items)
            for child in args.children:
                self.assertEqual(args.new_target, parsed(root / child.task, root).managerat)

    def test_source1611_direct_worker_rejects_agent_queue_and_protected_race(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
            old_path = root / args.old_task
            changed_text = old_path.read_text(encoding="utf-8").replace(manager_replace.SOURCE1611_DIRECT_WORKER_OLD_QUEUE[0], "Agent-authored replacement strategy.")
            old_path.write_text(changed_text, encoding="utf-8")
            changed = replace(
                args,
                old_sha256=sha(changed_text),
                authority_envelope_sha256=sha(changed_text),
                old_queue_sha256=manager_replace.json_digest(["Agent-authored replacement strategy."]),
            )
            runtime = self.source1597_runtime({"old_live": True}, changed, protected)
            with runtime[0], runtime[1] as stop_mock, runtime[2], self.assertRaisesRegex(ReplaceError, "Human-provenance queue changed"):
                replace_manager(changed)
            stop_mock.assert_not_called()
            self.assertFalse((root / args.successor_task).exists())

        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
            state: dict[str, object] = {"old_live": True}
            state["stop_hook"] = lambda: state.__setitem__("protected_drift", True)
            runtime = self.source1597_runtime(state, args, protected)
            before = (root / args.old_task).read_bytes()
            with runtime[0], runtime[1], runtime[2], self.assertRaisesRegex(ReplaceError, "protected pane/process inventory changed"):
                replace_manager(args)
            self.assertEqual(before, (root / args.old_task).read_bytes())
            self.assertFalse((root / args.successor_task).exists())

        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
            source1597 = root / manager_replace.SOURCE1597_FILE
            source1597.write_bytes(source1597.read_bytes() + b"drift\n")
            runtime = self.source1597_runtime({"old_live": True}, args, protected)
            with runtime[0], runtime[1] as stop_mock, runtime[2], self.assertRaisesRegex(ReplaceError, "Source-1597 direct-worker authority source or digest changed"):
                replace_manager(args)
            stop_mock.assert_not_called()
            self.assertFalse((root / args.successor_task).exists())

        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
            source1601 = root / manager_replace.SOURCE1601_FILE
            state = {"old_live": True, "stop_hook": lambda: source1601.write_bytes(source1601.read_bytes() + b"late drift\n")}
            runtime = self.source1597_runtime(state, args, protected)
            before = (root / args.old_task).read_bytes()
            with runtime[0], runtime[1], runtime[2], self.assertRaisesRegex(ReplaceError, "Source-1601 replacement authority"):
                replace_manager(args)
            self.assertEqual(before, (root / args.old_task).read_bytes())
            self.assertFalse((root / args.successor_task).exists())

        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
            parent_path = root / manager_replace.SOURCE1611_SUCCESSOR_TASK
            state = {"old_live": True, "stop_hook": lambda: parent_path.write_bytes(parent_path.read_bytes() + b"late drift\n")}
            runtime = self.source1597_runtime(state, args, protected)
            before = (root / args.old_task).read_bytes()
            with runtime[0], runtime[1], runtime[2], self.assertRaisesRegex(ReplaceError, "Source-1611 direct-worker parent"):
                replace_manager(args)
            self.assertEqual(before, (root / args.old_task).read_bytes())
            self.assertFalse((root / args.successor_task).exists())

        with tempfile.TemporaryDirectory() as tmp:
            root, args, protected = self.source1611_direct_worker_fixture(Path(tmp))
            source1597 = root / manager_replace.SOURCE1597_FILE
            state = {"old_live": True, "stop_hook": lambda: source1597.write_bytes(source1597.read_bytes() + b"late drift\n")}
            runtime = self.source1597_runtime(state, args, protected)
            before = (root / args.old_task).read_bytes()
            with runtime[0], runtime[1], runtime[2], self.assertRaisesRegex(ReplaceError, "Source-1597 direct-worker authority"):
                replace_manager(args)
            self.assertEqual(before, (root / args.old_task).read_bytes())
            self.assertFalse((root / args.successor_task).exists())

    def test_source1612_requires_separate_handoff_complete_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _protected = self.source1611_fixture(Path(tmp))
            changed = replace(
                args,
                authority_file=manager_replace.SOURCE1612_FILE,
                authority_sha256="0" * 64,
            )
            with self.assertRaisesRegex(ReplaceError, "separate authenticated handoff-complete proof"):
                manager_replace.validate_targets(changed)

    def test_other_authority_cannot_request_descendant_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.whole_tree_fixture(Path(tmp))
            with self.assertRaisesRegex(ReplaceError, "only by exact Source-1289"):
                replace_manager(args)

    def test_source1292_empty_tree_replaces_only_live_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, authority = self.empty_tree_fixture(Path(tmp))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with authority, inventory, stopped, proof:
                result = replace_manager(args)
            self.assertIn("sole ownership", result)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)
            self.assertEqual("done", parsed(root / args.old_task, root).status)

    def test_source1292_empty_tree_rejects_hidden_active_child_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, authority = self.empty_tree_fixture(Path(tmp))
            hidden = root / "hidden.md"
            hidden.write_text(
                task_text(
                    status="running",
                    runat="hidden:1",
                    managerat=args.old_target,
                    is_manager=False,
                    pending=("Preserve hidden work.",),
                ),
                encoding="utf-8",
            )
            inventory, stopped, proof = self.runtime({"old_live": True})
            with (
                authority,
                inventory,
                stopped as stop_mock,
                proof,
                self.assertRaisesRegex(ReplaceError, "active child set changed"),
            ):
                replace_manager(args)
            stop_mock.assert_not_called()

    def test_source1292_empty_tree_rejects_wrong_envelope_digest_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files, authority = self.empty_tree_fixture(Path(tmp))
            changed = replace(args, empty_tree_envelope_sha256="0" * 64)
            inventory, stopped, proof = self.runtime({"old_live": True})
            with (
                authority,
                inventory,
                stopped as stop_mock,
                proof,
                self.assertRaisesRegex(ReplaceError, "Source-1292 empty-tree envelope binding changed"),
            ):
                replace_manager(changed)
            stop_mock.assert_not_called()

    def test_source1292_empty_tree_rejects_nonprivate_source_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, authority = self.empty_tree_fixture(Path(tmp))
            (root / manager_replace.SOURCE1292_FILE).chmod(0o644)
            inventory, stopped, proof = self.runtime({"old_live": True})
            with (
                authority,
                inventory,
                stopped as stop_mock,
                proof,
                self.assertRaisesRegex(ReplaceError, "Source-1292 authority source and directory must be owner-private"),
            ):
                replace_manager(args)
            stop_mock.assert_not_called()

    def test_source1292_dual_authority_descendant_prepare_preserves_both_queue_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, authority = self.dual_descendant_fixture(Path(tmp))
            inventory, _stopped, _proof = self.runtime({"old_live": True})
            with authority, inventory:
                plan = manager_replace.prepare(args, manager_replace.markdown_paths(root))
            self.assertEqual(2, len(plan.successor_queue) - len(OLD_QUEUE))
            self.assertIn(args.authority_file, plan.successor_queue[-2])
            self.assertIn(manager_replace.SOURCE1292_FILE, plan.successor_queue[-1])
            self.assertIsNotNone(plan.empty_tree_authority)

    def test_source1292_dual_authority_descendant_rejects_missing_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, authority = self.dual_descendant_fixture(Path(tmp))
            changed = replace(args, descendant_authority_envelope_sha256="")
            with authority, self.assertRaisesRegex(ReplaceError, "expected blocks"):
                manager_replace.prepare(changed, manager_replace.markdown_paths(root))

    def test_source1292_dual_authority_drift_after_close_fails_before_lifecycle_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files, authority = self.dual_descendant_fixture(Path(tmp))
            live = {
                manager_replace.canonical_target(args.old_target),
                *(manager_replace.canonical_target(item.target) for item in args.descendants),
            }
            identities = {
                manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
                **{
                    manager_replace.canonical_target(item.target): PaneIdentity(
                        manager_replace.canonical_target(item.target),
                        item.pane_id,
                        item.pane_pid,
                        item.pane_start_ticks,
                    )
                    for item in args.descendants
                },
            }
            sessions = {item.target: item.session_id for item in args.descendants} | {args.old_target: args.old_session_id}

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target in live}

            def stopped(stop_args: object) -> str:
                target = manager_replace.canonical_target(stop_args.target)
                live.remove(target)
                Path(stop_args.bound_close_proof_path).write_text(f"{stop_args.bound_close_proof_secret}\n", encoding="utf-8")
                Path(stop_args.bound_close_proof_path).chmod(0o600)
                if target == manager_replace.canonical_target(args.old_target):
                    source1292 = root / manager_replace.SOURCE1292_FILE
                    source1292.write_text(source1292.read_text(encoding="utf-8") + "late drift\n", encoding="utf-8")
                return sessions[stop_args.target]

            with (
                authority,
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop", side_effect=stopped),
                patch.object(manager_replace, "require_descendants_closed"),
                self.assertRaisesRegex(ReplaceError, "Source-1292 replacement authority changed"),
            ):
                replace_manager(args)
            self.assertEqual(files[args.old_task], (root / args.old_task).read_text(encoding="utf-8"))
            self.assertEqual("rolled_back", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_source1292_dual_authority_child_alias_commits_and_reauthenticates_migrated_carrier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files, authority = self.dual_descendant_alias_fixture(Path(tmp))
            live = {
                manager_replace.canonical_target(args.old_target),
                *(manager_replace.canonical_target(item.target) for item in args.descendants),
            }
            identities = {
                manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
                **{
                    manager_replace.canonical_target(item.target): PaneIdentity(
                        manager_replace.canonical_target(item.target),
                        item.pane_id,
                        item.pane_pid,
                        item.pane_start_ticks,
                    )
                    for item in args.descendants
                },
            }
            sessions = {item.target: item.session_id for item in args.descendants} | {args.old_target: args.old_session_id}

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target in live}

            def stopped(stop_args: object) -> str:
                live.remove(manager_replace.canonical_target(stop_args.target))
                Path(stop_args.bound_close_proof_path).write_text(f"{stop_args.bound_close_proof_secret}\n", encoding="utf-8")
                Path(stop_args.bound_close_proof_path).chmod(0o600)
                return sessions[stop_args.target]

            with (
                authority,
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop", side_effect=stopped),
            ):
                result = replace_manager(args)
                recovered = replace_manager(args)
            self.assertIn("sole ownership", result)
            self.assertIn("recovered committed", recovered)
            carrier = parsed(root / args.authority_envelope_task, root)
            self.assertEqual(args.new_target, carrier.managerat)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_source1292_dual_authority_child_alias_extra_committed_byte_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files, authority = self.dual_descendant_alias_fixture(Path(tmp))
            original_replace = manager_replace.replace_snapshot

            def replace_then_drift(snapshot: object, data: bytes, label: str) -> manager_replace.Snapshot:
                result = original_replace(snapshot, data, label)
                if label == f"active child {args.authority_envelope_task}":
                    result.path.write_bytes(result.data + b"unexpected byte\n")
                return result

            live = {
                manager_replace.canonical_target(args.old_target),
                *(manager_replace.canonical_target(item.target) for item in args.descendants),
            }
            identities = {
                manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
                **{
                    manager_replace.canonical_target(item.target): PaneIdentity(
                        manager_replace.canonical_target(item.target),
                        item.pane_id,
                        item.pane_pid,
                        item.pane_start_ticks,
                    )
                    for item in args.descendants
                },
            }
            sessions = {item.target: item.session_id for item in args.descendants} | {args.old_target: args.old_session_id}

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target in live}

            def stopped(stop_args: object) -> str:
                live.remove(manager_replace.canonical_target(stop_args.target))
                Path(stop_args.bound_close_proof_path).write_text(f"{stop_args.bound_close_proof_secret}\n", encoding="utf-8")
                Path(stop_args.bound_close_proof_path).chmod(0o600)
                return sessions[stop_args.target]

            with (
                authority,
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop", side_effect=stopped),
                patch.object(manager_replace, "replace_snapshot", side_effect=replace_then_drift),
                self.assertRaisesRegex(ReplaceError, "replacement failed"),
            ):
                replace_manager(args)
            carrier_bytes = (root / args.authority_envelope_task).read_bytes()
            self.assertTrue(carrier_bytes.endswith(b"unexpected byte\n"))
            self.assertNotEqual(files[args.authority_envelope_task].encode(), carrier_bytes)
            self.assertEqual("rollback_failed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_source1292_modes_keep_distinct_recovery_binding_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "empty"
            base.mkdir()
            root, empty_args, _files, empty_authority = self.empty_tree_fixture(base)
            inventory, _stopped, _proof = self.runtime({"old_live": True})
            with empty_authority, inventory:
                empty_plan = manager_replace.prepare(empty_args, manager_replace.markdown_paths(root))
                empty_record = manager_replace.audit_record(empty_args, empty_plan, "a" * 64, sha("a" * 64))
            self.assertEqual(
                empty_args.empty_tree_envelope_sha256,
                empty_record["empty_tree_envelope_sha256"],
            )
            self.assertNotIn("descendant_authority_envelope_sha256", empty_record)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "descendant"
            base.mkdir()
            root, descendant_args, _files, descendant_authority = self.dual_descendant_fixture(base)
            inventory, _stopped, _proof = self.runtime({"old_live": True})
            with descendant_authority, inventory:
                descendant_plan = manager_replace.prepare(descendant_args, manager_replace.markdown_paths(root))
                descendant_record = manager_replace.audit_record(descendant_args, descendant_plan, "b" * 64, sha("b" * 64))
            self.assertEqual(
                descendant_args.descendant_authority_envelope_sha256,
                descendant_record["descendant_authority_envelope_sha256"],
            )
            self.assertNotIn("empty_tree_envelope_sha256", descendant_record)

    def test_direct_args_cannot_add_or_omit_a_descendant_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.whole_tree_fixture(Path(tmp))
            malformed = replace(args, descendants=args.descendants[:1])
            with self.whole_tree_authority(malformed), self.assertRaisesRegex(ReplaceError, "identical descendant pin for every child"):
                replace_manager(malformed)

    def test_whole_tree_recovers_after_first_descendant_close_without_retargeting_it(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.whole_tree_fixture(Path(tmp))
            identities = {
                manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
                **{
                    manager_replace.canonical_target(item.target): PaneIdentity(manager_replace.canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks)
                    for item in args.descendants
                },
            }
            live = set(identities)
            calls: list[str] = []

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target in live}

            def stopped(stop_args) -> str:
                calls.append(stop_args.target)
                live.remove(manager_replace.canonical_target(stop_args.target))
                Path(stop_args.bound_close_proof_path).write_text(f"{stop_args.bound_close_proof_secret}\n", encoding="utf-8")
                Path(stop_args.bound_close_proof_path).chmod(0o600)
                if len(calls) == 1:
                    raise SimulatedCrash()
                return next(
                    (item.session_id for item in args.descendants if item.target == stop_args.target),
                    args.old_session_id,
                )

            patches = (
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop", side_effect=stopped),
            )
            with self.whole_tree_authority(args), patches[0], patches[1], patches[2], self.assertRaises(SimulatedCrash):
                replace_manager(args)
            with (
                self.whole_tree_authority(args),
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop", side_effect=stopped),
            ):
                result = replace_manager(args)
            self.assertEqual(1, calls.count(args.descendants[0].target))
            self.assertIn("sole ownership", result)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_whole_tree_durably_contains_close_without_proof(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.whole_tree_fixture(Path(tmp))
            identities = {
                manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
                **{
                    manager_replace.canonical_target(item.target): PaneIdentity(manager_replace.canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks)
                    for item in args.descendants
                },
            }
            live = set(identities)

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target in live}

            def crash_after_close(stop_args) -> str:
                live.remove(manager_replace.canonical_target(stop_args.target))
                raise SimulatedCrash()

            with (
                self.whole_tree_authority(args),
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop", side_effect=crash_after_close),
                self.assertRaises(SimulatedCrash),
            ):
                replace_manager(args)
            with (
                self.whole_tree_authority(args),
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop") as stop_mock,
                self.assertRaisesRegex(ReplaceError, "durably ambiguous"),
            ):
                replace_manager(args)
            stop_mock.assert_not_called()

    def test_whole_tree_retries_exact_live_manager_after_descendants_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.whole_tree_fixture(Path(tmp))
            identities = {
                manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
                **{
                    manager_replace.canonical_target(item.target): PaneIdentity(manager_replace.canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks)
                    for item in args.descendants
                },
            }
            live = set(identities)
            manager_attempts = 0

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target in live}

            def stopped(stop_args) -> str:
                nonlocal manager_attempts
                if stop_args.target == args.old_target:
                    manager_attempts += 1
                    if manager_attempts == 1:
                        raise RuntimeError("transient guarded stop failure")
                    session = args.old_session_id
                else:
                    session = next(item.session_id for item in args.descendants if item.target == stop_args.target)
                live.remove(manager_replace.canonical_target(stop_args.target))
                Path(stop_args.bound_close_proof_path).write_text(f"{stop_args.bound_close_proof_secret}\n", encoding="utf-8")
                Path(stop_args.bound_close_proof_path).chmod(0o600)
                return session

            def run() -> str:
                with (
                    self.whole_tree_authority(args),
                    patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                    patch.object(manager_replace, "process_start_ticks", return_value=None),
                    patch.object(manager_replace, "stop", side_effect=stopped),
                ):
                    return replace_manager(args)

            with self.assertRaisesRegex(ReplaceError, "transient guarded stop failure"):
                run()
            result = run()
            self.assertEqual(2, manager_attempts)
            self.assertIn("sole ownership", result)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_whole_tree_reappeared_descendant_blocks_old_manager_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.whole_tree_fixture(Path(tmp))
            identities = {
                manager_replace.canonical_target(args.old_target): PaneIdentity(manager_replace.canonical_target(args.old_target), "%42", 4242, 999),
                **{
                    manager_replace.canonical_target(item.target): PaneIdentity(manager_replace.canonical_target(item.target), item.pane_id, item.pane_pid, item.pane_start_ticks)
                    for item in args.descendants
                },
            }
            live = set(identities)
            calls: list[str] = []

            def inventory() -> dict[str, PaneIdentity]:
                return {target: identity for target, identity in identities.items() if target in live}

            def stopped(stop_args) -> str:
                calls.append(stop_args.target)
                live.remove(manager_replace.canonical_target(stop_args.target))
                Path(stop_args.bound_close_proof_path).write_text(f"{stop_args.bound_close_proof_secret}\n", encoding="utf-8")
                Path(stop_args.bound_close_proof_path).chmod(0o600)
                if stop_args.target == args.descendants[-1].target:
                    live.add(manager_replace.canonical_target(args.descendants[0].target))
                return next(item.session_id for item in args.descendants if item.target == stop_args.target)

            with (
                self.whole_tree_authority(args),
                patch.object(manager_replace, "pane_inventory", side_effect=inventory),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "stop", side_effect=stopped),
                self.assertRaisesRegex(ReplaceError, "descendant closure changed before old-manager close"),
            ):
                replace_manager(args)
            self.assertNotIn(args.old_target, calls)

    def test_concurrent_old_task_change_rejects_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            changed = files["failed_manager.md"] + "external task note\n"

            def mutate_task() -> None:
                (root / "failed_manager.md").write_text(changed, encoding="utf-8")

            with self.assertRaisesRegex(ReplaceError, "owned lifecycle writes rolled back"):
                self.run_replacement(args, {"old_live": True, "stop_hook": mutate_task})
            self.assertEqual(changed, (root / "failed_manager.md").read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())

    def test_bound_artifact_drift_after_preparation_never_closes_old_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            original = manager_replace.reserve_audit
            close_authority = args.audit_output.with_name(f".{args.audit_output.name}.close-authority")

            def mutate_after_preparation(path, record):
                result = original(path, record)
                if path == close_authority:
                    (root / "child_a.md").write_text(files["child_a.md"] + "concurrent drift\n", encoding="utf-8")
                return result

            inventory, stopped, proof = self.runtime({"old_live": True})
            with (
                inventory,
                stopped as stop_mock,
                proof,
                patch.object(manager_replace, "reserve_audit", side_effect=mutate_after_preparation),
                self.assertRaisesRegex(ReplaceError, "pre-close active child"),
            ):
                replace_manager(args)
            stop_mock.assert_not_called()
            self.assertEqual(files["failed_manager.md"], (root / args.old_task).read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())

    def test_partial_child_migration_rolls_back_exact_owned_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            original = manager_replace.replace_snapshot

            def fail_second_child(expected, data, label):
                if label == "active child child_b.md":
                    raise ReplaceError("injected partial migration fault")
                return original(expected, data, label)

            inventory, stopped, proof = self.runtime({"old_live": True})
            with (
                inventory,
                stopped,
                proof,
                patch.object(manager_replace, "replace_snapshot", side_effect=fail_second_child),
                self.assertRaisesRegex(ReplaceError, "all lifecycle writes rolled back"),
            ):
                replace_manager(args)
            for name, data in files.items():
                self.assertEqual(data, (root / name).read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())
            self.assertEqual("rolled_back", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_post_publication_proof_failure_removes_successor_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with (
                inventory,
                stopped,
                proof,
                patch.object(manager_replace, "prove_committed", side_effect=ReplaceError("injected proof fault")),
                self.assertRaisesRegex(ReplaceError, "all lifecycle writes rolled back"),
            ):
                replace_manager(args)
            for name, data in files.items():
                self.assertEqual(data, (root / name).read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())

    def test_crash_after_partial_migration_recovers_to_one_committed_successor(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            original = manager_replace.replace_snapshot
            crashed = False

            def crash_after_first_child(expected, data, label):
                nonlocal crashed
                result = original(expected, data, label)
                if label == "active child child_a.md" and not crashed:
                    crashed = True
                    raise SimulatedCrash()
                return result

            state = {"old_live": True}
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped, proof, patch.object(manager_replace, "replace_snapshot", side_effect=crash_after_first_child), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            self.assertFalse(state["old_live"])
            self.assertTrue(args.audit_output.exists())

            result = self.run_replacement(args, state)
            self.assertIn("sole ownership", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual("done", parsed(root / args.old_task, root).status)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)
            self.assertEqual(NEW_TARGET, parsed(root / "child_a.md", root).managerat)
            self.assertEqual(NEW_TARGET, parsed(root / "child_b.md", root).managerat)

    def test_crash_after_audit_reservation_recovers_the_same_pinned_close(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            state = {"old_live": True}
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped, proof, patch.object(manager_replace, "stop_old_manager", side_effect=SimulatedCrash), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            self.assertTrue(state["old_live"])
            self.assertEqual("prepared", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

            result = self.run_replacement(args, state)
            self.assertFalse(state["old_live"])
            self.assertIn("sole ownership", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_crash_between_audit_and_close_authority_recovers_without_a_second_owner(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            original = manager_replace.reserve_audit
            crashed = False

            def crash_after_main_audit(path, record):
                nonlocal crashed
                result = original(path, record)
                if path == args.audit_output and not crashed:
                    crashed = True
                    raise SimulatedCrash()
                return result

            state = {"old_live": True}
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped, proof, patch.object(manager_replace, "reserve_audit", side_effect=crash_after_main_audit), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            close_authority = args.audit_output.with_name(f".{args.audit_output.name}.close-authority")
            self.assertTrue(args.audit_output.exists())
            self.assertFalse(close_authority.exists())
            self.assertTrue(state["old_live"])

            result = self.run_replacement(args, state)
            self.assertTrue(close_authority.exists())
            self.assertFalse(state["old_live"])
            self.assertIn("sole ownership", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_source1269_guard_loss_reconciles_authorized_absent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.guest1269_fixture(Path(tmp))
            state = {"old_live": True}

            def killed_then_guard_lost(_args: object) -> str:
                state["old_live"] = False
                raise RuntimeError("tmux symbolic target no longer owns the exact pane at command execution")

            inventory, _stopped, proof = self.runtime(state, args.old_target, args.new_target)
            with (
                inventory,
                proof,
                patch.object(manager_replace, "stop", side_effect=killed_then_guard_lost),
                self.assertRaisesRegex(ReplaceError, "manager stop failed before lifecycle mutation"),
            ):
                replace_manager(args)
            self.assertEqual("stop_failed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

            with (
                patch.object(manager_replace, "pane_inventory", return_value={}),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "has_bound_close_proof", return_value=False),
            ):
                result = replace_manager(args)
            self.assertIn("sole ownership", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual(
                "authorized-absence",
                json.loads(args.audit_output.read_text(encoding="utf-8"))["owner_close_evidence"],
            )
            self.assertEqual("done", parsed(root / args.old_task, root).status)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_source1269_guard_loss_recovery_rejects_reused_pane_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.guest1269_fixture(Path(tmp))
            state = {"old_live": True}

            def killed_then_guard_lost(_args: object) -> str:
                state["old_live"] = False
                raise RuntimeError("tmux symbolic target no longer owns the exact pane at command execution")

            inventory, _stopped, proof = self.runtime(state, args.old_target, args.new_target)
            with (
                inventory,
                proof,
                patch.object(manager_replace, "stop", side_effect=killed_then_guard_lost),
                self.assertRaises(ReplaceError),
            ):
                replace_manager(args)
            reused = PaneIdentity("other:1.0", args.old_pane_id, 9999, 1000)
            with (
                patch.object(manager_replace, "pane_inventory", return_value={reused.target: reused}),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                self.assertRaisesRegex(ReplaceError, "exact closed-owner recovery state cannot be proved"),
            ):
                replace_manager(args)

    def test_source1269_closed_owner_audit_rebases_current_todo_without_reclosing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.guest1269_fixture(Path(tmp))
            state = {"old_live": True}

            def killed_then_guard_lost(_args: object) -> str:
                state["old_live"] = False
                raise RuntimeError("tmux symbolic target no longer owns the exact pane at command execution")

            inventory, _stopped, proof = self.runtime(state, args.old_target, args.new_target)
            with (
                inventory,
                proof,
                patch.object(manager_replace, "stop", side_effect=killed_then_guard_lost),
                self.assertRaisesRegex(ReplaceError, "manager stop failed before lifecycle mutation"),
            ):
                replace_manager(args)
            source_bytes = args.audit_output.read_bytes()
            concurrent_task = "concurrent_lifecycle.md"
            (root / concurrent_task).write_text(
                task_text(
                    status="running",
                    runat="other:8",
                    managerat="other:7",
                    is_manager=False,
                    pending=("Preserve concurrent lifecycle work.",),
                ),
                encoding="utf-8",
            )
            current_todo = (
                (root / "TODO.md")
                .read_text(encoding="utf-8")
                .replace(
                    "unrelated.md other:1\n",
                    f"unrelated.md other:1\n{concurrent_task} other:8\n",
                )
            )
            (root / "TODO.md").write_text(current_todo, encoding="utf-8")
            rebased = replace(
                args,
                todo_sha256=sha(current_todo),
                audit_output=args.audit_output.with_name("current-manager-replace.json"),
                reviewer="fresh-independent-reviewer",
                closed_owner_audit=args.audit_output,
                closed_owner_audit_sha256=hashlib.sha256(source_bytes).hexdigest(),
            )
            with (
                patch.object(manager_replace, "pane_inventory", return_value={}),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "has_bound_close_proof", return_value=False),
                patch.object(manager_replace, "stop") as stop_mock,
            ):
                result = replace_manager(rebased)
            stop_mock.assert_not_called()
            self.assertIn("sole ownership", result)
            self.assertEqual(source_bytes, args.audit_output.read_bytes())
            record = json.loads(rebased.audit_output.read_text(encoding="utf-8"))
            self.assertEqual("committed", record["state"])
            self.assertEqual("authorized-absence", record["owner_close_evidence"])
            self.assertEqual(str(args.audit_output), record["closed_owner_audit"])
            self.assertIn(f"{concurrent_task} other:8", (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertEqual("done", parsed(root / args.old_task, root).status)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)
            with (
                patch.object(manager_replace, "pane_inventory", return_value={}),
                patch.object(manager_replace, "has_bound_close_proof", return_value=False),
            ):
                self.assertIn("recovered committed", replace_manager(rebased))

    def test_source1269_closed_owner_audit_rejects_lifecycle_or_pane_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.guest1269_fixture(Path(tmp))
            state = {"old_live": True}

            def killed_then_guard_lost(_args: object) -> str:
                state["old_live"] = False
                raise RuntimeError("tmux symbolic target no longer owns the exact pane at command execution")

            inventory, _stopped, proof = self.runtime(state, args.old_target, args.new_target)
            with inventory, proof, patch.object(manager_replace, "stop", side_effect=killed_then_guard_lost), self.assertRaises(ReplaceError):
                replace_manager(args)
            rebased = replace(
                args,
                audit_output=args.audit_output.with_name("current-manager-replace.json"),
                closed_owner_audit=args.audit_output,
                closed_owner_audit_sha256=hashlib.sha256(args.audit_output.read_bytes()).hexdigest(),
            )
            reused = PaneIdentity("other:1.0", args.old_pane_id, 9999, 1000)
            with (
                patch.object(manager_replace, "pane_inventory", return_value={reused.target: reused}),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                self.assertRaisesRegex(ReplaceError, "pane or process identity is no longer absent"),
            ):
                replace_manager(rebased)
            self.assertFalse(rebased.audit_output.exists())
            (root / args.old_task).write_text(files[args.old_task] + "concurrent lifecycle drift\n", encoding="utf-8")
            with (
                patch.object(manager_replace, "pane_inventory", return_value={}),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                patch.object(manager_replace, "has_bound_close_proof", return_value=False),
                self.assertRaisesRegex(ReplaceError, "lifecycle bytes changed outside the current TODO"),
            ):
                replace_manager(rebased)
            self.assertFalse(rebased.audit_output.exists())

    def test_source1269_closed_owner_retry_reauthenticates_source_and_absence(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        for scenario in ("source", "authority", "proof", "pane", "pid"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as tmp:
                _root, args, _files = self.guest1269_fixture(Path(tmp))
                state = {"old_live": True}

                def killed_then_guard_lost(_args: object) -> str:
                    state["old_live"] = False
                    raise RuntimeError("tmux symbolic target no longer owns the exact pane at command execution")

                inventory, _stopped, proof = self.runtime(state, args.old_target, args.new_target)
                with inventory, proof, patch.object(manager_replace, "stop", side_effect=killed_then_guard_lost), self.assertRaises(ReplaceError):
                    replace_manager(args)
                rebased = replace(
                    args,
                    audit_output=args.audit_output.with_name("current-manager-replace.json"),
                    closed_owner_audit=args.audit_output,
                    closed_owner_audit_sha256=hashlib.sha256(args.audit_output.read_bytes()).hexdigest(),
                )
                with (
                    patch.object(manager_replace, "pane_inventory", return_value={}),
                    patch.object(manager_replace, "process_start_ticks", return_value=None),
                    patch.object(manager_replace, "has_bound_close_proof", return_value=False),
                    patch.object(manager_replace, "replace_snapshot", side_effect=SimulatedCrash),
                    self.assertRaises(SimulatedCrash),
                ):
                    replace_manager(rebased)
                self.assertEqual("owner_absent", json.loads(rebased.audit_output.read_text(encoding="utf-8"))["state"])
                authority_path, _proof_path = manager_replace.closed_owner_evidence_paths(args.audit_output)
                if scenario == "source":
                    args.audit_output.write_bytes(args.audit_output.read_bytes() + b"drift\n")
                elif scenario == "authority":
                    authority_path.write_bytes(authority_path.read_bytes() + b"drift\n")
                inventory_value = {"other:1.0": PaneIdentity("other:1.0", args.old_pane_id, 9999, 1000)} if scenario == "pane" else {}
                with (
                    patch.object(manager_replace, "pane_inventory", return_value=inventory_value),
                    patch.object(
                        manager_replace,
                        "process_start_ticks",
                        return_value=(args.old_pane_start_ticks if scenario == "pid" else None),
                    ),
                    patch.object(manager_replace, "has_bound_close_proof", return_value=scenario == "proof"),
                    self.assertRaises(ReplaceError),
                ):
                    replace_manager(rebased)

    def test_closed_owner_audit_is_rejected_for_non_source1269_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            changed = replace(
                args,
                closed_owner_audit=args.audit_output.with_name("prior.json"),
                closed_owner_audit_sha256="0" * 64,
            )
            with self.assertRaisesRegex(ReplaceError, "only for the exact Source-1269 replacement"):
                manager_replace.validate_targets(changed)

    def test_other_manager_guard_loss_cannot_use_source1269_absence_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            state = {"old_live": True}

            def disappeared_during_guard(_args: object) -> str:
                state["old_live"] = False
                raise RuntimeError("tmux symbolic target no longer owns the exact pane at command execution")

            inventory, _stopped, proof = self.runtime(state)
            with (
                inventory,
                proof,
                patch.object(manager_replace, "stop", side_effect=disappeared_during_guard),
                self.assertRaises(ReplaceError),
            ):
                replace_manager(args)
            with (
                patch.object(manager_replace, "pane_inventory", return_value={}),
                patch.object(manager_replace, "process_start_ticks", return_value=None),
                self.assertRaisesRegex(ReplaceError, "exact closed-owner recovery state cannot be proved"),
            ):
                replace_manager(args)

    def test_crash_after_all_writes_recovers_commit_without_second_stop(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            state = {"old_live": True}
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped, proof, patch.object(manager_replace, "prove_committed", side_effect=SimulatedCrash), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped as stop_mock, proof:
                result = replace_manager(args)
            stop_mock.assert_not_called()
            self.assertIn("recovered committed", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_atomic_exchange_preserves_a_concurrently_rebound_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.md"
            path.write_text("expected\n", encoding="utf-8")
            expected = manager_replace.read_snapshot(path, "fixture")
            original = manager_replace.rename_at2
            injected = False

            def rebind_before_exchange(parent_fd, source, target, flags):
                nonlocal injected
                if flags == manager_replace.RENAME_EXCHANGE and not injected:
                    injected = True
                    foreign = path.with_name("foreign.md")
                    foreign.write_text("foreign concurrent bytes\n", encoding="utf-8")
                    foreign.replace(path)
                return original(parent_fd, source, target, flags)

            with patch.object(manager_replace, "rename_at2", side_effect=rebind_before_exchange), self.assertRaisesRegex(ReplaceError, "concurrently rebound"):
                manager_replace.replace_snapshot(expected, b"transaction bytes\n", "fixture")
            self.assertEqual("foreign concurrent bytes\n", path.read_text(encoding="utf-8"))

    def test_atomic_successor_removal_preserves_a_concurrently_rebound_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "successor.md"
            path.write_text("transaction successor\n", encoding="utf-8")
            expected = manager_replace.read_snapshot(path, "fixture successor")
            original = manager_replace.rename_at2
            injected = False

            def rebind_before_remove(parent_fd, source, target, flags):
                nonlocal injected
                if flags == manager_replace.RENAME_NOREPLACE and source == path.name and not injected:
                    injected = True
                    foreign = path.with_name("foreign-successor.md")
                    foreign.write_text("foreign concurrent successor\n", encoding="utf-8")
                    foreign.replace(path)
                return original(parent_fd, source, target, flags)

            with patch.object(manager_replace, "rename_at2", side_effect=rebind_before_remove), self.assertRaisesRegex(ReplaceError, "rebound"):
                manager_replace.remove_created(expected)
            self.assertEqual("foreign concurrent successor\n", path.read_text(encoding="utf-8"))

    def test_late_orphan_child_is_detected_and_never_committed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            orphan = task_text(
                status="running",
                runat="other:1",
                managerat=OLD_TARGET,
                is_manager=False,
                pending=("Concurrent child request.",),
            )

            def retarget_unrelated() -> None:
                (root / "unrelated.md").write_text(orphan, encoding="utf-8")

            with self.assertRaisesRegex(ReplaceError, "all lifecycle writes rolled back"):
                self.run_replacement(args, {"old_live": True, "stop_hook": retarget_unrelated})
            self.assertEqual(orphan, (root / "unrelated.md").read_text(encoding="utf-8"))
            self.assertEqual(files["failed_manager.md"], (root / args.old_task).read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())
            self.assertEqual("rolled_back", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_duplicate_owner_or_incomplete_child_set_rejects_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            (root / "duplicate.md").write_text(
                task_text(status="running", runat=OLD_TARGET, managerat=PARENT_TARGET, is_manager=True, pending=("duplicate",)),
                encoding="utf-8",
            )
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "exactly one authoritative active owner"):
                replace_manager(args)
            stop_mock.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            incomplete = replace(args, children=(args.children[0],))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "active child set changed"):
                replace_manager(incomplete)
            stop_mock.assert_not_called()

    def test_human_or_explicitly_protected_target_rejects_without_pane_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            cases = (
                replace(args, old_target="hprivate:1"),
                replace(args, new_target="hprivate:3"),
                replace(args, protected_targets=("private_mgr:1.0",)),
            )
            for changed in cases:
                with self.subTest(target=(changed.old_target, changed.new_target, changed.protected_targets)):
                    with patch.object(manager_replace, "pane_inventory") as inventory, self.assertRaisesRegex(ReplaceError, "human-owned|protected"):
                        replace_manager(changed)
                    inventory.assert_not_called()

    def test_live_successor_target_rejects_launch_before_singular_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            inventory, stopped, proof = self.runtime({"old_live": True, "new_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "launch-before-singular-proof"):
                replace_manager(args)
            stop_mock.assert_not_called()
            self.assertFalse(args.audit_output.exists())


if __name__ == "__main__":
    unittest.main()
