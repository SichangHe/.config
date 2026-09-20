#!/usr/bin/env python3
"""Send one owner-authenticated completion email for a new task mutation."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import imaplib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from dataclasses import replace
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_email_config import GMAIL_IMAP_HOST, configured_agent_mail, guest_hees_target
from omo_manager.omo_email_subject import canonical_tmux_target, tmux_window_target
from omo_manager.omo_task_context import current_active_task
from omo_manager.omo_task_context import current_pending_task
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_file_lock_at_path
from omo_manager.omo_task_metadata import human_authored_pending_items
from omo_manager.omo_task_metadata import pending_item_without_human_prefix

EMAIL_HELPER = Path(__file__).resolve().parents[1] / "helper.sh" / "email_me.py"
COMPLETION_ENTRYPOINT = Path(__file__).resolve()
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RECONCILIATION_VERSION = "v1"
# 🧑 Human source `manager_mail/85c5dff58359-1990.txt`: "fix the helper script to allow that"
SOURCE1990_PATH = "manager_mail/85c5dff58359-1990.txt"
SOURCE1990_SHA256 = "77f7bc7cf0d63d754ac07331da5d4c49a5b6e184bb53f0c33336bc86b8feb1d7"
SOURCE1990_TEXT = "Subject: Re: Pangram queue decision — watcher_repair.md\n\nAnyway, if you are saying I said to mark something done and the helper script could not do it due to some stupid reasons, fix the helper script to allow that"
SOURCE1990_PANGRAM_ROOT = "/ssd1/sichangheagent/work_logs"
SOURCE1990_PANGRAM_TASK = "src1964_pangram.md"
SOURCE1990_PANGRAM_OWNER = "dw:15"
SOURCE1990_PANGRAM_MANAGER = "dw:60"
SOURCE1990_PANGRAM_CLAIM_BOUND_TASK_SHA256 = "98ad7210b0b7dcf88d30ad141dcbdbb201abfcbed0e7d5086d14b3f373b77c2d"
SOURCE1990_PANGRAM_AUTHORIZED_TASK_SHA256 = "fde2aac686832d25ca6817f07b048a2168e9d220a846776d9b3fd6cd239d0e38"
SOURCE1990_PANGRAM_TASK_SHA256 = "764849bdd27dfcd20bbb42c2bc099e9651c944e79c945d28e289707764e8b32e"
SOURCE1990_PANGRAM_QUEUE_SHA256 = "7809db5269454b970f1c3ef44278ceb006de48693fe6af2c4100ad1cb2230078"
SOURCE1990_PANGRAM_PURPOSE_SHA256 = "289ba586544ab9caf05744a435a5a98afcc830cbf17712cf2f5b17cc2ea8aae3"
SOURCE1990_PANGRAM_EVIDENCE = "All four items answered in reviewed Human email Message-ID <178984962018.2270905.17610697508862128015@gmail.com>; final independent review PASS in /tmp/src1975-report-feedback3.md; durable comparison in docs/notes/pangram_binoculars_comparison_2026-09-16.md."
SOURCE1990_PANGRAM_MESSAGE_ID = "<178984962018.2270905.17610697508862128015@gmail.com>"
SOURCE1990_PANGRAM_SUBJECT_SHA256 = "b2439c1be93a90abff001abd6a6c39bfaddb49354dfd1722f48301ee549f1aa8"
SOURCE1990_PANGRAM_BODY_SHA256 = "d0d3ad536fd9fdadceb0017856b120f0ecb7322e02bd319706cdf86badd36904"
SOURCE1990_PANGRAM_PRIMARY_CLAIM = (
    "4c8bf60548d6296bcac97a33d602c9c6e7cc9ebb9e9b6b80a2ecee53e235dfbc",
    "be7c7a869fd151177f82cd14ba13062ded0bd992937105c9869fc7a8e4d8dbf8",
    "dw:60",
    "7b1fadcf6e946e5f170271aed9fc40b04dd2a4695e471d4c3c59bf652cc89d75",
    "f29025212bb166d9d92172f6b4757b2c9f3de363b7b34c8ee59245d9bdd86e21",
)
SOURCE1990_PANGRAM_EXTRA_CLAIM = (
    "6887b3aaddbd5019d090774e1ea5c2414d3ac939eb8c6d86735bcb35d13dc402",
    "611c6efbaff4ddb873e8902e3c476582fc28e446f1262fe16c51e0bb88d48975",
    "dw:60",
    "93d6ab3e808ef4c8467b1aea2e3953e0a21c5a278435ace6c628d5bb28148c6c",
    "44811a627deda29f43c5672c42338c501b87b833a848dbe9fe15a4b3aa0aa1dd",
)
SOURCE1990_PANGRAM_CHURN = (
    "059f1060f1df4646211a5742c12b24e927f2eb7b",
    "d90f8d664e1093e16c06af95a074c9dece9ab0e0",
    "8d9e5a7a7873316ffa4ce2d6138037afa78c4a11",
    "bb4d7cefaddf29c5bd1f3d0dbe871c292aca24ffd8a67f19f027937ccbdd55e7",
)
SOURCE1990_PANGRAM_POST_AUTHORITY_CHURN = (
    "2e696849aa32339b4a5e8ff470adec31824cec60",
    "ee4dbda6fcb176eeb7fb42c830c53780ec3cedea",
    "ef53c0cdc9ac2afd78381cf6de26967b680d3370",
    "6deba6edfae0b6e6c79da413df9b65ec26340aaa5a8ce70a5eb661fdd3d48c14",
)
SOURCE1990_PANGRAM_CUSTODY_CHURN = (
    "fc6e1bd9fdce0c2e188ad61d7ca8192e37f31e22",
    "8d9e5a7a7873316ffa4ce2d6138037afa78c4a11",
    "ee4dbda6fcb176eeb7fb42c830c53780ec3cedea",
    "fa7066ec845ad5072932380ded11147471b2087a7278695790cbd9b7d97c42fd",
)
SOURCE1990_PANGRAM_ITEMS = (
    "🧑 Human Source-1964: Revisit every original Sep 16 Pangram TODO, resume incomplete work, explain completed items with cited evidence, and email the Human in the distinct Pangram chain",
    "🧑 Human Source-1975: Identify the Human questions that correspond to the quoted Pangram/Binoculars direct answers.",
    "🧑 Human Source-1975: Determine whether the Sonnet body-swap experiment used the wrong LLMs.",
    "🧑 Human Source-1975: Determine whether prompt or other protocol differences explain why Sonnet body swaps did better, and explain how the contrast happened.",
)
# 🧑 Human: "fix the helper so already-delivered answers can satisfy exact completed Human queue work without duplicate email. Exact case: ... five live Source-1970 items and one missing sixth sentence in its body, all answered by existing Message-ID ..."
SOURCE1970_ROOT = "/ssd1/sichangheagent/work_logs"
SOURCE1970_PATH = "manager_mail/85c5dff58359-1970.txt"
SOURCE1970_SHA256 = "1b0a78b8aa196c5ecd7938b66bf44fd144fb234a666e6123ce627510c6ec3434"
SOURCE1970_TASK = "eval_sufficiency.md"
SOURCE1970_TASK_SHA256 = "fc893edfeea1cdf7bef2b841faa592afc58fa14edcc3f2984d4093077eb79181"
SOURCE1970_OWNER = "dw:58"
SOURCE1970_MANAGER = "dw:60"
SOURCE1970_STATUS = "blocked"
SOURCE1970_BLOCKED_ON = "config:35 supported Source-1970 completion reconciliation"
SOURCE1970_SENTENCE = "How exactly do we argue this, basically the eval is sufficient and there is no need for larger datasets?"
SOURCE1970_MISSING_ITEM = f"🧑 Source-1970: “{SOURCE1970_SENTENCE}”"
SOURCE1970_MISSING_BODY_LINE = (
    f"Human Source-1970, verbatim from `manager_mail/85c5dff58359-1970.txt`: “{SOURCE1970_SENTENCE}”"
)
SOURCE1970_LIVE_ITEMS = (
    "🧑 Source-1970: “Why” — explain why the existing evaluation can or cannot support the claim that generating more sites using the same methods would produce materially similar results.",
    "🧑 Source-1970: “What do you mean?” — replace the confusing stored-site/resampling explanation with a direct explanation of the same-method new-site question.",
    "🧑 Source-1970: “What we want to argue is if we generate more sites using the same methods, the results would pretty much be the same.”",
    "🧑 Source-1970: “Do we argue in the paper like this?” — determine whether and how to use the observed 5th-to-95th-percentile narrowing in the paper.",
    "🧑 Source-1970: “Stop undermining the paper. We need to convince people this whole eval makes sense, not tell them it doesn’t really mean anything.”",
)
SOURCE1970_RESOLUTION_ITEMS = (*SOURCE1970_LIVE_ITEMS, SOURCE1970_MISSING_ITEM)
SOURCE1970_QUEUE_SHA256 = "e1c29082027f88d221503095a5ea1d8cd67551df431881cfbfb5984bcbb885e5"
SOURCE1970_RESOLUTION_SHA256 = "b80bbfb076debcf20c0d41af89bd962a8ff1d688453dc2f0f8b408293038ec0d"
SOURCE1970_MESSAGE_ID = "<178985059335.2672392.11170981607905287447@gmail.com>"
SOURCE1970_SUBJECT_SHA256 = "39836bede4faaa9871c59d661cb953afd9310fa6e29f55c9d60c4eaf4adf340d"
SOURCE1970_BODY_SHA256 = "316868914a3d039e8a0d6bde249817bb8fe97e4f9cbd9cfbe16a7689b9d3eea1"
SOURCE1970_EVIDENCE = f"All six Source-1970 items answered in Human email Message-ID {SOURCE1970_MESSAGE_ID}."
SOURCE1970_PURPOSE_SHA256 = "9f5f9c6e461cf025128bb68ee24b9702cf3f4f72f494a3ff74ef588df1c62a5a"
SOURCE1970_PLAN_KEY = "1c96b6c9f7875eea32f3fa860b1cd51f554f2d9f202306ee50582306212e7449"
SOURCE1970_NOTICE_KEY = "3462f407e63210223686243e677831f8ad6f605a25853f318c5efc7cd41881c9"
SOURCE1970_CANONICAL_BODY_SHA256 = "84361a3bcd61e30e93d514cc5a2d489dee1930d135f2129a72b95454197cd86c"
SOURCE1970_DELIVERED_CREATION_CLAIMS = (
    (
        "8cd7586d05099f4b4a8d137c64c6596f90553a61bf51bbad737ef3a1264051c7",
        "119e4a73b9883784799cb67f6aab85e6928bf6494a43a1053b35da0464544a86",
        "dw:13",
        "c899d451ca18cb153e83c3c5ceab578f53be6815be0e9fab3cc9a4fab9a26ed5",
        "fb9f46e49e2cb1129b713eb875cefb59beb0d9a411441c7397edf298421f10cb",
        "4269af36f72ee61c655461b2e5afe52f951f93899c62563cade98358eb6286a7",
    ),
    (
        "05a163eda7a8a594eb033f5b5fed45324c566be790240c88d4f0faed2786e928",
        "f3e91525d30cd4f0f83887acc174280fc26c3d5ea91c4ccced629c0ec5e32c31",
        "dw:13",
        "746c3f2ef9cb4e3b7a617a645fff38b55c4d9f8e09586d18da99f375d4c45eab",
        "5e3c2bc66c52bfa02fc02af3495e9157c5ebb8405afd0c31dfb4fd5e09f82878",
        "b1803894793e8c3abafff4f297d10148783b1dbc03b247e74728c55b276509ec",
    ),
    (
        "8866e66c90f402e54298100e2504b8d829dd790b1425b2339887c04ea9b46f2e",
        "6a224f9df12c64cb70507ad084d34ac02c134830b01a39ecb11304ab7fa2bae3",
        "dw:13",
        "aaa4856ec93c1bf66344549d3ad21fc0869533b0b494a623ac03eb65f25f4b33",
        "aad689c5dc8d55630a92ea3b934dfb535225632c82bc472015de25be7e44c38a",
        "80d7a0abf32b7771b1e97aaab71358b44772929004442cd343ea8abae5fc8bbc",
    ),
    (
        "bfbc298ab8a6411b7bed0967002462fc692c84e68d7ed6ba5ccf96f19dd57e4c",
        "b4bc7ddbcde066c8d90c988b7ab08ee1ab611810f8e97b63d887ca61553d23c6",
        "dw:13",
        "aeaaa2ab2bcea9e87f1e8068fc820a78f51d15cb2e23c0298e03081bc586fa3c",
        "d454b12e264d9803c3875d804e4ef17f231c79718f12f0785daecdbd2237d4de",
        "717367c29b1f12f7367a5f5fe902b162c63d7d6712953b98a753864aacfcd85a",
    ),
    (
        "0e5a152bb0473c3a3521d271fd0602afc5c46a24c6aec9eac7fb5f4fd3fcb729",
        "ad0118e792b311d70ff09a8d5c925cfa6a38688463003b3f6608915dd83c4cd4",
        "dw:13",
        "7867f7258aad79cbc41c8ae3a7ab919e8a8f0455905faddc5cd0af70932998f0",
        "36e52fa7b6b0c33aace0ab6a09be06c94d4683642d10207f86a188de841de7fe",
        "259344bee12e9266c8206bad358b61ad3d4cb11b14902f7993bf31054b2eefb7",
    ),
)
SOURCE1970_STALE_ADD_CLAIM = (
    "43281a18d4ad00a0dcce7a698932828f5aca2dc46816762810c58f2a5741cf75",
    "ad0118e792b311d70ff09a8d5c925cfa6a38688463003b3f6608915dd83c4cd4",
    "dw:13",
    "94feb6a13494a9733d02da9d6dea3985fe0746e065d1915f6ae1626e029bf9c6",
    "726447405441a353022f10145e6e487b746b90dfaae74a24fe6e5c7402c15465",
)
SOURCE1970_STALE_REMOVAL_CLAIM = (
    "43a09139661c2449d1f1d6d490d4a994234208bc7ae4783dfa8639a0f347a919",
    "31e45d269e499f0851a300cf6b20fa942c557825be53866cba3d904896d9e7ed",
    "dw:60",
    "fedb33a3add1ea711eacbb3550fc84b44e98295610969347a8981579e5a0a6ef",
    "7c929b465b5aa72156e152bd4e18882f6b1dd34e95b5f51f3dfa79d5cd6881f1",
)
SOURCE1970_TASK_LINEAGE = (
    (
        "059f1060f1df4646211a5742c12b24e927f2eb7b",
        "195b6a9e0fb9548b280cd679e13866c6b287b4a5",
        "7b871b10ec83f484961fce0717e28b45c96ff8a2",
        "990cc67cddf91f26dbc12a55dccdc1f00d013b328b2ef58c58d394268ae8f2e4",
        "94fde9073b913a06ab75a7b8bac3cfeb8221d9a0798b9e703752495d3e7326e2",
        "31e45d269e499f0851a300cf6b20fa942c557825be53866cba3d904896d9e7ed",
    ),
    (
        "46c542fd7c1de6b6716f750f23186c4d54949f50",
        "7b871b10ec83f484961fce0717e28b45c96ff8a2",
        "b11e506f63b90de44af60300d60fdb37268f2184",
        "e8d6501a71571440be1d2389c2ae83b7c12910e897f986cec0f8c1095593eb95",
        "31e45d269e499f0851a300cf6b20fa942c557825be53866cba3d904896d9e7ed",
        "309cf83db6b334f009233afbd102fee1276eb6754bc61756cca8b24ff6c9abdf",
    ),
    (
        "7508ffc22e8c3ba7f697f37751a7df758307a332",
        "b11e506f63b90de44af60300d60fdb37268f2184",
        "0fb2796e26d2d9fc37cf433f28654010178d864c",
        "7abc11af0add2531090e309db55c4390b369262aa494c65f91690ca8476114db",
        "309cf83db6b334f009233afbd102fee1276eb6754bc61756cca8b24ff6c9abdf",
        "6c88a9484d68e5c53a6a338fd133e228f42f672ac942db895d57e5e0069f8b9d",
    ),
    (
        "2e696849aa32339b4a5e8ff470adec31824cec60",
        "0fb2796e26d2d9fc37cf433f28654010178d864c",
        "7488418938feb158054ff124eff2aa2457dd169e",
        "234f2500893440af35c4d3931a002b8e40c8230f70d842c6a73e17a4cc57293a",
        "6c88a9484d68e5c53a6a338fd133e228f42f672ac942db895d57e5e0069f8b9d",
        "fc893edfeea1cdf7bef2b841faa592afc58fa14edcc3f2984d4093077eb79181",
    ),
)
NO_CONTACT_RE = re.compile(
    r"\bsource[- ]985\b|\bno[- ]contact\b|\b(?:do not|must not|never) (?:send )?(?:any )?(?:human(?:-facing)? )?(?:email|mail|message|report|outreach|contact)\b|\b(?:do not|must not|never)\b[^.\n]{0,200}\b(?:send )?(?:any )?human (?:email|mail|message|report|outreach|contact)\b|\b(?:do not|must not|never)\b[^.\n]{0,100}\b(?:email|report|respond|write)\b[^.\n]{0,100}\bhuman\b|\b(?:no|forbid(?:s|den)?) human-facing reports?\b|\bhuman reporting (?:is )?(?:suppressed|forbidden|prohibited|paused)\b|\bwithout human email\b|\breport only privately\b|\bprivate reports? only\b",
    re.IGNORECASE,
)
AGENT_PENDING_NO_CONTACT_SCOPE_RE = re.compile(
    r"\b(?:agent[- ]authored(?: pending)? items?|(?:pending )?items? (?:authored|originated|created) (?:by|from) agents?|ones? from agents?)\b",
    re.IGNORECASE,
)
HUMAN_PENDING_AUTHOR_SCOPE_RE = re.compile(
    r"\b(?:human[- ]+(?:authored|originated|created)(?:[- ]+or[- ]+agent[- ]+(?:authored|originated|created))?(?: pending)? items?|human[- ]+or[- ]+agent[- ]+(?:authored|originated|created)(?: pending)? items?|(?:pending )?items? (?:authored|originated|created) (?:by|from) (?:the )?human)\b",
    re.IGNORECASE,
)
NO_CONTACT_META_RE = re.compile(
    r"\bwithout (?:weakening|changing|removing)\b[^.;\n]{0,120}\bno[- ]contact\b[^.;\n]{0,120}?\b(?:rule|policy|safeguard)s?\b",
    re.IGNORECASE,
)
MANAGER_ONLY_RE = re.compile(r"\b(?:report|return) only\b[^.\n]{0,100}\b(?:manager|submanager)\b|\bmanager[- ]only reports?\b", re.IGNORECASE)
DIRECT_HUMAN_REPORT_RE = re.compile(r"\b(?:email|report|respond|write)\b[^.\n]{0,100}\b(?:directly to )?(?:the )?human\b", re.IGNORECASE)
# 🧑 Human source `manager_mail/85c5dff58359-1929.txt:6`: "Pending items originated from the human need emails, ones from agents do not."
PENDING_ITEM_NOTICE_OUTCOMES = {
    "pending item created",
    "pending item completed",
    "pending item cancelled",
    "pending item removed after verification",
}


# 🧑 Human source `manager_mail/85c5dff58359-1929.txt:6`: "Pending items originated from the human need emails, ones from agents do not."
def human_pending_notice_contact_forbidden(text: str) -> bool:
    """Return whether policy contains a blanket, rather than agent-scoped, no-contact rule."""

    policy_text = NO_CONTACT_META_RE.sub("", text)
    for match in NO_CONTACT_RE.finditer(policy_text):
        clause_start = max(policy_text.rfind(separator, 0, match.start()) for separator in ("\n", ";", ".")) + 1
        clause_ends = [
            position for separator in ("\n", ";", ".") if (position := policy_text.find(separator, match.end())) >= 0
        ]
        clause_end = min(clause_ends, default=len(policy_text))
        clause = policy_text[clause_start:clause_end]
        if AGENT_PENDING_NO_CONTACT_SCOPE_RE.search(clause) is None or HUMAN_PENDING_AUTHOR_SCOPE_RE.search(clause) is not None:
            return True
    return False


# 🧑 Human source `manager_mail/85c5dff58359-1936.txt:3`: "pending item created/deleted messages should reuse subject of the agent’s previous email and be concise: just do like ..."
def pending_item_notice_body(outcome: str, items: tuple[str, ...]) -> str:
    """Render the exact concise body for a Human pending-item notice."""
    event = "created" if outcome == "pending item created" else "deleted"
    return f"pending item {event}:\n" + "".join(f"- {pending_item_without_human_prefix(item)}\n" for item in items)
SOURCE1241_REF = "manager_mail/85c5dff58359-1241.txt:1-7"
SOURCE1241_TASK = "hmanager_replace_fix.md"
SOURCE1241_HUMAN = """Subject: Re: Why recent agent replies were missing

I don't know why I should send transfer now. I already told that other
agent to talk to the agent they are in conflict with. They should sort it
out themselves. I also don't understand what needs my authorisation. I
asked for those things to be implemented and they should agents should go
ahead and do the job"""
SOURCE1241_ENVELOPE = f'<human_instruction authoritative="true" source="{SOURCE1241_REF}">\n{SOURCE1241_HUMAN}\n</human_instruction>'
SOURCE1241_META_SPAN = "Preserve exact-owner and no-contact safeguards."
SOURCE1241_META_LINE = (
    "This exact durable Human instruction resolves the stale request for another authorization step: agents must resolve the installed-entry-point "
    "overlap themselves and complete the already requested implementation/closure. Read-only deployed-contract verification shows "
    f"`{COMPLETION_ENTRYPOINT}` is mode 755 and self-binds `COMPLETION_ENTRYPOINT = Path(__file__).resolve()`. "
    "Use that supported deployed entry point for the owner-authenticated completion notice with the existing task/root/outcome arguments. "
    f"{SOURCE1241_META_SPAN} Do not perform production replacement or other task work."
)
SOURCE1241_CONTEXT = f"(from manager omo_task_edit delegate-message)\n{SOURCE1241_ENVELOPE}\n\n{SOURCE1241_META_LINE}"
SOURCE1241_LOCATOR_ENVELOPE_RE = re.compile(
    rf'(?ms)^<human_instruction[ \t]+authoritative="true"[ \t]+source="{re.escape(SOURCE1241_REF)}">\r?\n(?P<body>.*?)\r?\n</human_instruction>[ \t]*$'
)
ROUTING_OPEN_TAG_RE = re.compile(r"<(?P<tag>[A-Za-z][A-Za-z0-9_:-]*)(?:[ \t][^>]*)?>")
ROUTING_CLOSE_TAG_RE = re.compile(r"</(?P<tag>[A-Za-z][A-Za-z0-9_:-]*)>")
ROUTING_TAG_TOKEN_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9_:-]*(?:[ \t][^>]*)?>")
MARKDOWN_FENCE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})")


@dataclass(frozen=True)
class ContactPolicyBinding:
    task_sha256: str
    source: Path
    source_sha256: str


@dataclass(frozen=True)
class CompletionEmail:
    root: Path
    task: Path
    target: str
    manager_target: str
    task_sha256: str
    outcome: str
    subject: str
    body: str
    key: str
    notice_key: str
    semantic_key: str
    contact_policy: ContactPolicyBinding | None = None
    send_allowed: bool = True

    @property
    def notice_semantic_key(self) -> str:
        if self.outcome == "task done" and self.semantic_key:
            return hashlib.sha256(f"{self.semantic_key}\0task-close".encode()).hexdigest()
        return self.semantic_key


@dataclass(frozen=True)
class OrdinaryPendingRecoveryRequest:
    mode: str
    expected_task_sha256: str
    expected_queue_sha256: str
    purpose_sha256: str
    semantic_key: str
    prior_claim_key: str
    prior_task_sha256: str
    prior_manager_target: str
    prior_semantic_key: str
    prior_authorization_sha256: str
    message_id: str
    sent_subject_sha256: str
    sent_body_sha256: str
    churn_commit: str = ""
    churn_before_blob: str = ""
    churn_after_blob: str = ""
    churn_diff_sha256: str = ""
    prior_transition_key: str = ""
    extra_claim_key: str = ""
    extra_task_sha256: str = ""
    extra_manager_target: str = ""
    extra_semantic_key: str = ""
    extra_authorization_sha256: str = ""


@dataclass(frozen=True)
class OrdinaryPendingTransition:
    key: str
    record: str
    committed_record: str
    after_task_sha256: str


def digest_fields(*values: str) -> str:
    payload = b""
    for value in values:
        encoded = value.encode()
        payload += len(encoded).to_bytes(8, "big") + encoded
    return hashlib.sha256(payload).hexdigest()


def ordinary_pending_purpose(outcome: str, items: tuple[str, ...], evidence: str) -> str:
    """Bind one recovery to its complete ordered purpose and canonical notice."""

    subject = ""
    body = pending_item_notice_body(outcome, items)
    return digest_fields("ordinary-pending-purpose-v1", outcome, *items, evidence, subject, body)


def source1970_eval_queue_items() -> tuple[str, ...]:
    return SOURCE1970_LIVE_ITEMS


def source1970_eval_resolution_items() -> tuple[str, ...]:
    return SOURCE1970_RESOLUTION_ITEMS


def source1970_eval_evidence() -> str:
    return SOURCE1970_EVIDENCE


def source1970_eval_recovery_request() -> OrdinaryPendingRecoveryRequest:
    """Return the immutable request for the sole Source-1970 recovery."""

    return OrdinaryPendingRecoveryRequest(
        "source1970-eval-remove",
        SOURCE1970_TASK_SHA256,
        SOURCE1970_QUEUE_SHA256,
        SOURCE1970_PURPOSE_SHA256,
        SOURCE1970_PURPOSE_SHA256,
        *SOURCE1970_STALE_ADD_CLAIM,
        SOURCE1970_MESSAGE_ID,
        SOURCE1970_SUBJECT_SHA256,
        SOURCE1970_BODY_SHA256,
        extra_claim_key=SOURCE1970_STALE_REMOVAL_CLAIM[0],
        extra_task_sha256=SOURCE1970_STALE_REMOVAL_CLAIM[1],
        extra_manager_target=SOURCE1970_STALE_REMOVAL_CLAIM[2],
        extra_semantic_key=SOURCE1970_STALE_REMOVAL_CLAIM[3],
        extra_authorization_sha256=SOURCE1970_STALE_REMOVAL_CLAIM[4],
    )


def ordinary_pending_transition_key(
    root: Path,
    task: Path,
    outcome: str,
    items: tuple[str, ...],
    evidence: str,
    request: OrdinaryPendingRecoveryRequest,
) -> str:
    purpose = ordinary_pending_purpose(outcome, items, evidence)
    return digest_fields(
        "ordinary-pending-transition-v1",
        str(root.resolve()),
        task.resolve().relative_to(root.resolve()).as_posix(),
        purpose,
        *request.__dict__.values(),
    )


def recovery_request_sha256(request: OrdinaryPendingRecoveryRequest) -> str:
    return digest_fields("ordinary-pending-request-v1", *request.__dict__.values())


def pending_transition_path(key: str) -> Path:
    return completion_email_state_dir() / "ordinary-pending-transitions" / key


def read_transition_record(key: str) -> tuple[dict[str, str], str] | None:
    try:
        payload = owned_private_file(pending_transition_path(key), "ordinary pending transition", 32_768).decode()
    except FileNotFoundError:
        return None
    try:
        values = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("ordinary pending transition is malformed") from exc
    if not isinstance(values, dict) or any(not isinstance(name, str) or not isinstance(value, str) for name, value in values.items()):
        raise OSError("ordinary pending transition is malformed")
    return values, payload


def canonical_json_record(values: dict[str, str]) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def git_output(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("manager-churn Git evidence could not be authenticated") from exc
    return result.stdout


def validate_manager_churn(
    root: Path,
    task: Path,
    request: OrdinaryPendingRecoveryRequest,
    old_manager: str,
    current_text: str,
) -> None:
    bindings = (
        request.churn_commit,
        request.churn_before_blob,
        request.churn_after_blob,
        request.churn_diff_sha256,
    )
    if not all(bindings) or SHA256_RE.fullmatch(request.churn_diff_sha256) is None:
        raise OSError("manager churn requires exact commit, before/after blobs, and diff digest")
    if re.fullmatch(r"[0-9a-f]{40,64}", request.churn_commit) is None or any(
        re.fullmatch(r"[0-9a-f]{40,64}", value) is None for value in bindings[1:3]
    ):
        raise OSError("manager-churn Git object identity is malformed")
    relative = task.resolve().relative_to(root.resolve()).as_posix()
    parents = git_output(root, "show", "-s", "--format=%P", request.churn_commit).decode().strip().split()
    if len(parents) != 1:
        raise OSError("manager-churn commit must have exactly one parent")
    before_blob = git_output(root, "rev-parse", f"{parents[0]}:{relative}").decode().strip()
    after_blob = git_output(root, "rev-parse", f"{request.churn_commit}:{relative}").decode().strip()
    if (before_blob, after_blob) != (request.churn_before_blob, request.churn_after_blob):
        raise OSError("manager-churn commit does not contain the exact task blobs")
    diff = git_output(root, "diff", "--no-ext-diff", "--binary", parents[0], request.churn_commit, "--", relative)
    if hashlib.sha256(diff).hexdigest() != request.churn_diff_sha256:
        raise OSError("manager-churn task diff does not match its exact digest")
    before_payload = git_output(root, "cat-file", "blob", before_blob)
    after_payload = git_output(root, "cat-file", "blob", after_blob)
    if (
        hashlib.sha256(before_payload).hexdigest() != request.prior_task_sha256
        or hashlib.sha256(after_payload).hexdigest() != request.expected_task_sha256
    ):
        raise OSError("manager-churn task blobs do not match the exact task digests")
    try:
        before = parse_task_metadata(before_payload.decode(), root)
        after = parse_task_metadata(after_payload.decode(), root)
        current = parse_task_metadata(current_text, root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise OSError("manager-churn task blobs are invalid") from exc
    if (
        before is None
        or after is None
        or current is None
        or canonical_tmux_target(before.runat) != canonical_tmux_target(current.runat)
        or canonical_tmux_target(after.runat) != canonical_tmux_target(current.runat)
        or canonical_tmux_target(before.managerat) != canonical_tmux_target(old_manager)
        or canonical_tmux_target(after.managerat) != canonical_tmux_target(current.managerat)
        or after.pending_task_items != current.pending_task_items
        or before.is_manager
        or after.is_manager
    ):
        raise OSError("manager-churn commit does not prove the exact owner and ordered-queue transition")


def transition_static_values(
    root: Path,
    task: Path,
    outcome: str,
    items: tuple[str, ...],
    evidence: str,
    request: OrdinaryPendingRecoveryRequest,
) -> dict[str, str]:
    purpose = ordinary_pending_purpose(outcome, items, evidence)
    key = ordinary_pending_transition_key(root, task, outcome, items, evidence, request)
    return {
        "schema": "omo-ordinary-pending-transition/v1",
        "transition_key": key,
        "request_sha256": recovery_request_sha256(request),
        "mode": request.mode,
        "purpose_sha256": purpose,
        "root": str(root.resolve()),
        "task": task.resolve().relative_to(root.resolve()).as_posix(),
        "before_task_sha256": request.expected_task_sha256,
        "before_queue_sha256": request.expected_queue_sha256,
        "outcome_sha256": hashlib.sha256(outcome.encode()).hexdigest(),
        "items_sha256": digest_fields("ordered-items-v1", *items),
        "evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest(),
        "canonical_subject_sha256": hashlib.sha256(b"").hexdigest(),
        "canonical_body_sha256": hashlib.sha256(pending_item_notice_body(outcome, items).encode()).hexdigest(),
        "prior_claim_key": request.prior_claim_key,
        "prior_task_sha256": request.prior_task_sha256,
        "prior_manager_target": request.prior_manager_target,
        "prior_semantic_key": request.prior_semantic_key,
        "prior_authorization_sha256": request.prior_authorization_sha256,
        "message_id": request.message_id,
        "sent_subject_sha256": request.sent_subject_sha256,
        "sent_body_sha256": request.sent_body_sha256,
        "churn_commit": request.churn_commit,
        "churn_before_blob": request.churn_before_blob,
        "churn_after_blob": request.churn_after_blob,
        "churn_diff_sha256": request.churn_diff_sha256,
        "prior_transition_key": request.prior_transition_key,
        "extra_claim_key": request.extra_claim_key,
        "extra_task_sha256": request.extra_task_sha256,
        "extra_manager_target": request.extra_manager_target,
        "extra_semantic_key": request.extra_semantic_key,
        "extra_authorization_sha256": request.extra_authorization_sha256,
    }


def validate_recovery_request(request: OrdinaryPendingRecoveryRequest) -> None:
    if request.mode not in {"adopt-add", "supersede-remove", "source1990-pangram-remove", "source1970-eval-remove"}:
        raise ValueError("ordinary pending recovery mode is invalid")
    hashes = (
        request.expected_task_sha256,
        request.expected_queue_sha256,
        request.purpose_sha256,
        request.semantic_key,
        request.prior_claim_key,
        request.prior_task_sha256,
        request.prior_semantic_key,
        request.prior_authorization_sha256,
        request.sent_subject_sha256,
        request.sent_body_sha256,
    )
    if any(SHA256_RE.fullmatch(value) is None for value in hashes):
        raise ValueError("ordinary pending recovery requires exact lowercase SHA-256 bindings")
    if request.prior_transition_key and SHA256_RE.fullmatch(request.prior_transition_key) is None:
        raise ValueError("prior transition key must be a lowercase SHA-256 digest")
    if re.fullmatch(r"<[^<>\s]+>", request.message_id) is None:
        raise ValueError("ordinary pending recovery Message-ID is invalid")
    extra = (
        request.extra_claim_key,
        request.extra_task_sha256,
        request.extra_manager_target,
        request.extra_semantic_key,
        request.extra_authorization_sha256,
    )
    if any(extra) and not all(extra):
        raise ValueError("extra stale claim bindings must be supplied together")
    if any(extra) and any(SHA256_RE.fullmatch(value) is None for value in (extra[0], extra[1], extra[3], extra[4])):
        raise ValueError("extra stale claim bindings require exact lowercase SHA-256 values")
    if request.extra_claim_key == request.prior_claim_key:
        raise ValueError("extra stale claim must differ from the primary prior claim")
    churn = (
        request.churn_commit,
        request.churn_before_blob,
        request.churn_after_blob,
        request.churn_diff_sha256,
    )
    if any(churn) and not all(churn):
        raise ValueError("manager-churn bindings must be supplied together")


def validate_prior_transition(
    request: OrdinaryPendingRecoveryRequest,
    root: Path,
    task: Path,
) -> None:
    if request.prior_task_sha256 == request.expected_task_sha256:
        if request.prior_transition_key:
            raise OSError("unchanged task bytes cannot cite a prior transition")
        return
    if request.churn_commit:
        return
    if not request.prior_transition_key:
        raise OSError("changed task bytes require one exact committed prior transition")
    loaded = read_transition_record(request.prior_transition_key)
    if loaded is None:
        raise OSError("prior ordinary pending transition is missing")
    values, _payload = loaded
    if (
        values.get("status") != "committed"
        or values.get("root") != str(root.resolve())
        or values.get("task") != task.resolve().relative_to(root.resolve()).as_posix()
        or values.get("before_task_sha256") != request.prior_task_sha256
        or values.get("after_task_sha256") != request.expected_task_sha256
    ):
        raise OSError("prior ordinary pending transition does not bind the task-byte change")


def validate_source1990_pangram_authority(
    root: Path,
    plan: CompletionEmail,
    items: tuple[str, ...],
    evidence: str,
    request: OrdinaryPendingRecoveryRequest,
    current_text: str,
) -> None:
    """Authenticate the one Human-authorized Pangram recovery incident."""

    source = root / SOURCE1990_PATH
    try:
        payload = owned_private_file(source, "Source-1990 authority", 4096)
    except FileNotFoundError as exc:
        raise OSError("Source-1990 authority is missing") from exc
    try:
        relative = plan.task.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = ""
    if (
        hashlib.sha256(payload).hexdigest() != SOURCE1990_SHA256
        or payload.decode("utf-8") != SOURCE1990_TEXT
        or str(root.resolve()) != SOURCE1990_PANGRAM_ROOT
        or relative != SOURCE1990_PANGRAM_TASK
        or plan.target != SOURCE1990_PANGRAM_OWNER
        or plan.manager_target != SOURCE1990_PANGRAM_MANAGER
        or plan.task_sha256 != SOURCE1990_PANGRAM_TASK_SHA256
        or hashlib.sha256(current_text.encode()).hexdigest() != SOURCE1990_PANGRAM_TASK_SHA256
        or items != SOURCE1990_PANGRAM_ITEMS
        or evidence != SOURCE1990_PANGRAM_EVIDENCE
        or plan.outcome != "pending item removed after verification"
        or request.mode != "source1990-pangram-remove"
        or request.prior_transition_key
        or request.expected_task_sha256 != SOURCE1990_PANGRAM_TASK_SHA256
        or request.expected_queue_sha256 != SOURCE1990_PANGRAM_QUEUE_SHA256
        or request.purpose_sha256 != SOURCE1990_PANGRAM_PURPOSE_SHA256
        or request.message_id != SOURCE1990_PANGRAM_MESSAGE_ID
        or request.sent_subject_sha256 != SOURCE1990_PANGRAM_SUBJECT_SHA256
        or request.sent_body_sha256 != SOURCE1990_PANGRAM_BODY_SHA256
        or stale_claim_bindings(request) != (SOURCE1990_PANGRAM_PRIMARY_CLAIM, SOURCE1990_PANGRAM_EXTRA_CLAIM)
        or (request.churn_commit, request.churn_before_blob, request.churn_after_blob, request.churn_diff_sha256)
        != SOURCE1990_PANGRAM_CHURN
    ):
        raise OSError("Source-1990 authority does not bind this exact Pangram recovery")
    validate_manager_churn(
        root,
        plan.task,
        replace(
            request,
            expected_task_sha256=SOURCE1990_PANGRAM_CLAIM_BOUND_TASK_SHA256,
            prior_task_sha256=request.extra_task_sha256,
            prior_manager_target=request.extra_manager_target,
        ),
        request.extra_manager_target,
        current_text,
    )
    validate_manager_churn(
        root,
        plan.task,
        replace(
            request,
            expected_task_sha256=SOURCE1990_PANGRAM_AUTHORIZED_TASK_SHA256,
            prior_task_sha256=SOURCE1990_PANGRAM_CLAIM_BOUND_TASK_SHA256,
            prior_manager_target=SOURCE1990_PANGRAM_MANAGER,
            churn_commit=SOURCE1990_PANGRAM_CUSTODY_CHURN[0],
            churn_before_blob=SOURCE1990_PANGRAM_CUSTODY_CHURN[1],
            churn_after_blob=SOURCE1990_PANGRAM_CUSTODY_CHURN[2],
            churn_diff_sha256=SOURCE1990_PANGRAM_CUSTODY_CHURN[3],
        ),
        SOURCE1990_PANGRAM_MANAGER,
        current_text,
    )
    validate_manager_churn(
        root,
        plan.task,
        replace(
            request,
            prior_task_sha256=SOURCE1990_PANGRAM_AUTHORIZED_TASK_SHA256,
            prior_manager_target=SOURCE1990_PANGRAM_MANAGER,
            churn_commit=SOURCE1990_PANGRAM_POST_AUTHORITY_CHURN[0],
            churn_before_blob=SOURCE1990_PANGRAM_POST_AUTHORITY_CHURN[1],
            churn_after_blob=SOURCE1990_PANGRAM_POST_AUTHORITY_CHURN[2],
            churn_diff_sha256=SOURCE1990_PANGRAM_POST_AUTHORITY_CHURN[3],
        ),
        SOURCE1990_PANGRAM_MANAGER,
        current_text,
    )


def validate_source1970_task_lineage(
    root: Path,
    task: Path,
    current_text: str,
    request: OrdinaryPendingRecoveryRequest,
) -> None:
    """Authenticate every exact committed task-byte step after the removal claim."""

    if (
        len(SOURCE1970_TASK_LINEAGE) != 4
        or SOURCE1970_TASK_LINEAGE[0][5] != SOURCE1970_STALE_REMOVAL_CLAIM[1]
        or SOURCE1970_TASK_LINEAGE[-1][5] != SOURCE1970_TASK_SHA256
        or any(
            left[5] != right[4] or left[2] != right[1]
            for left, right in zip(SOURCE1970_TASK_LINEAGE, SOURCE1970_TASK_LINEAGE[1:])
        )
    ):
        raise OSError("Source-1970 task lineage constants are not contiguous")
    for commit, before_blob, after_blob, diff_sha256, before_sha256, after_sha256 in SOURCE1970_TASK_LINEAGE:
        validate_manager_churn(
            root,
            task,
            replace(
                request,
                expected_task_sha256=after_sha256,
                prior_task_sha256=before_sha256,
                prior_manager_target=SOURCE1970_MANAGER,
                churn_commit=commit,
                churn_before_blob=before_blob,
                churn_after_blob=after_blob,
                churn_diff_sha256=diff_sha256,
            ),
            SOURCE1970_MANAGER,
            current_text,
        )


def validate_source1970_eval_authority(
    root: Path,
    plan: CompletionEmail,
    items: tuple[str, ...],
    evidence: str,
    request: OrdinaryPendingRecoveryRequest,
    current_text: str,
) -> None:
    """Authenticate the one Source-1970 six-item completion incident."""

    source = root / SOURCE1970_PATH
    try:
        payload = owned_private_file(source, "Source-1970 authority", 32_768)
        source_text = payload.decode("utf-8")
        metadata = parse_task_metadata(current_text, root)
    except FileNotFoundError as exc:
        raise OSError("Source-1970 authority is missing") from exc
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise OSError("Source-1970 authority or task is malformed") from exc
    try:
        relative = plan.task.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative = ""
    expected_plan = _build_completion_email(
        root,
        plan.task,
        current_text,
        "pending item removed after verification",
        items=SOURCE1970_RESOLUTION_ITEMS,
        evidence=SOURCE1970_EVIDENCE,
        semantic_key=SOURCE1970_PURPOSE_SHA256,
        sent_recovery=True,
    )
    if (
        metadata is None
        or hashlib.sha256(payload).hexdigest() != SOURCE1970_SHA256
        or SOURCE1970_SENTENCE not in source_text
        or current_text.splitlines().count(SOURCE1970_MISSING_BODY_LINE) != 1
        or str(root.resolve()) != SOURCE1970_ROOT
        or relative != SOURCE1970_TASK
        or metadata.version != "v1.0.0"
        or metadata.status != SOURCE1970_STATUS
        or metadata.blocked_on != SOURCE1970_BLOCKED_ON
        or metadata.runat != SOURCE1970_OWNER
        or metadata.managerat != SOURCE1970_MANAGER
        or metadata.is_manager
        or metadata.pending_task_items != SOURCE1970_LIVE_ITEMS
        or digest_fields("pending-queue-v1", *metadata.pending_task_items) != SOURCE1970_QUEUE_SHA256
        or digest_fields("pending-queue-v1", *items) != SOURCE1970_RESOLUTION_SHA256
        or items != SOURCE1970_RESOLUTION_ITEMS
        or evidence != SOURCE1970_EVIDENCE
        or request != source1970_eval_recovery_request()
        or ordinary_pending_purpose(plan.outcome, items, evidence) != SOURCE1970_PURPOSE_SHA256
        or plan != expected_plan
        or plan.key != SOURCE1970_PLAN_KEY
        or plan.notice_key != SOURCE1970_NOTICE_KEY
        or hashlib.sha256(plan.body.encode()).hexdigest() != SOURCE1970_CANONICAL_BODY_SHA256
        or plan.task_sha256 != SOURCE1970_TASK_SHA256
        or hashlib.sha256(current_text.encode()).hexdigest() != SOURCE1970_TASK_SHA256
    ):
        raise OSError("Source-1970 authority does not bind this exact evaluation recovery")
    validate_source1970_task_lineage(root, plan.task, current_text, request)


def claims_rows(state: Path) -> tuple[Path, list[list[str]], str]:
    ledger = state / "completion-email-claims.tsv"
    previous = owned_private_file(ledger, "completion claims ledger", 8_000_000).decode()
    rows = [line.split("\t") for line in previous.splitlines()]
    if any(len(row) not in {3, 5, 6, 7} for row in rows):
        raise OSError("completion claims ledger is malformed")
    return ledger, rows, previous


def rewrite_claims(ledger: Path, previous: str, rows: list[list[str]]) -> None:
    if owned_private_file(ledger, "completion claims ledger", 8_000_000).decode() != previous:
        raise OSError("completion claims ledger changed while locked")
    temporary = ledger.with_name(f".{ledger.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write("".join("\t".join(row) + "\n" for row in rows))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, ledger)
        fsync_directory(ledger.parent)
    finally:
        temporary.unlink(missing_ok=True)


def authorization_values(payload: str) -> dict[str, str]:
    try:
        values = dict(line.split("=", 1) for line in payload.splitlines())
    except ValueError as exc:
        raise OSError("completion email authorization is malformed") from exc
    expected = {
        "version",
        "target",
        "root",
        "task",
        "task_sha256",
        "notice_key",
        "semantic_key",
        "subject_sha256",
        "body_sha256",
    }
    if len(values) != len(payload.splitlines()) or set(values) != expected or values["version"] != "1":
        raise OSError("completion email authorization is malformed")
    return values


def forbidden_prior_claim_state(state: Path, key: str, notice_key: str) -> tuple[Path, ...]:
    return (
        state / "completion-email-authorization-used" / key,
        state / "completion-email-delivered" / key,
        state / "completion-email-reconciled" / key,
        state / "completion-email-requests" / key,
        state / "completion-email-reconciliations" / key,
        state / "completion-notice-delivered" / notice_key,
        state / "ordinary-completion-by-notice" / notice_key,
    )


def transition_tombstone(
    transition_key: str,
    request: OrdinaryPendingRecoveryRequest,
    task_name: str,
    notice_key: str,
) -> list[str]:
    _ = notice_key
    return claim_tombstone(
        transition_key,
        request.prior_claim_key,
        task_name,
        request.prior_manager_target,
        request.prior_task_sha256,
    )


def claim_tombstone(
    transition_key: str,
    claim_key: str,
    task_name: str,
    manager_target: str,
    task_sha256: str,
) -> list[str]:
    """Make one retired claim impossible for the sender to use."""

    return [
        claim_key,
        f"retired:{transition_key}",
        task_name,
        manager_target,
        task_sha256,
    ]


def stale_claim_bindings(request: OrdinaryPendingRecoveryRequest) -> tuple[tuple[str, str, str, str, str], ...]:
    """Return every exact unused claim that this recovery must permanently retire."""

    bindings = [
        (
            request.prior_claim_key,
            request.prior_task_sha256,
            request.prior_manager_target,
            request.prior_semantic_key,
            request.prior_authorization_sha256,
        )
    ]
    if request.extra_claim_key:
        bindings.append(
            (
                request.extra_claim_key,
                request.extra_task_sha256,
                request.extra_manager_target,
                request.extra_semantic_key,
                request.extra_authorization_sha256,
            )
        )
    return tuple(bindings)


def validate_source1970_eval_state(
    state: Path,
    plan: CompletionEmail,
    request: OrdinaryPendingRecoveryRequest,
    transition_key: str,
    rows: list[list[str]],
) -> None:
    """Authenticate the exact delivered and unused Source-1970 claim set."""

    authorization_dir = state / "completion-email-authorizations"
    retired_dir = state / "completion-email-retired-authorizations"
    used_dir = state / "completion-email-authorization-used"
    delivery_dir = state / "completion-email-delivered"
    notice_delivery_dir = state / "completion-notice-delivered"
    for directory, label in (
        (authorization_dir, "completion authorization directory"),
        (retired_dir, "retired completion authorization directory"),
        (used_dir, "completion authorization use directory"),
        (delivery_dir, "completion delivery directory"),
        (notice_delivery_dir, "completion notice delivery directory"),
    ):
        require_private_directory(directory, label)
    if len(SOURCE1970_DELIVERED_CREATION_CLAIMS) != len(SOURCE1970_LIVE_ITEMS):
        raise OSError("Source-1970 delivered creation bindings are incomplete")
    relative = plan.task.relative_to(plan.root).as_posix()
    for item, binding in zip(SOURCE1970_LIVE_ITEMS, SOURCE1970_DELIVERED_CREATION_CLAIMS, strict=True):
        claim_key, task_sha256, manager_target, notice_key, semantic_key, authorization_sha256 = binding
        expected = [
            claim_key,
            SOURCE1970_OWNER,
            SOURCE1970_TASK,
            manager_target,
            task_sha256,
            notice_key,
            semantic_key,
        ]
        if (
            [row for row in rows if row and row[0] == claim_key] != [expected]
            or [row for row in rows if len(row) >= 6 and row[5] == notice_key] != [expected]
            or [row for row in rows if len(row) == 7 and row[6] == semantic_key] != [expected]
        ):
            raise OSError("Source-1970 creation delivery lacks one exact claim")
        authorization_payload = owned_private_file(
            authorization_dir / claim_key,
            "Source-1970 creation authorization",
            4096,
        ).decode()
        expected_authorization = {
            "version": "1",
            "target": SOURCE1970_OWNER,
            "root": str(plan.root),
            "task": relative,
            "task_sha256": task_sha256,
            "notice_key": notice_key,
            "semantic_key": semantic_key,
            "subject_sha256": hashlib.sha256(b"").hexdigest(),
            "body_sha256": hashlib.sha256(pending_item_notice_body("pending item created", (item,)).encode()).hexdigest(),
        }
        if (
            hashlib.sha256(authorization_payload.encode()).hexdigest() != authorization_sha256
            or authorization_values(authorization_payload) != expected_authorization
            or owned_private_file(used_dir / claim_key, "Source-1970 creation authorization use", 4096).decode()
            != f"{SOURCE1970_OWNER}\t{SOURCE1970_TASK}\n"
            or owned_private_file(delivery_dir / claim_key, "Source-1970 creation delivery", 4096).decode()
            != f"{SOURCE1970_OWNER}\t{SOURCE1970_TASK}\t{task_sha256}\n"
            or owned_private_file(
                notice_delivery_dir / notice_key,
                "Source-1970 creation notice delivery",
                4096,
            ).decode()
            != f"{claim_key}\t{SOURCE1970_OWNER}\t{SOURCE1970_TASK}\t{task_sha256}\n"
            or (retired_dir / claim_key).exists()
        ):
            raise OSError("Source-1970 creation delivery evidence changed")
    for claim_key, task_sha256, manager_target, semantic_key, authorization_sha256 in stale_claim_bindings(request):
        old = authorization_dir / claim_key
        retired = retired_dir / claim_key
        live = old.exists()
        if live == retired.exists():
            raise OSError("Source-1970 unused authorization state is ambiguous")
        payload = owned_private_file(
            old if live else retired,
            "Source-1970 unused authorization",
            4096,
        ).decode()
        authorization = authorization_values(payload)
        expected = [
            claim_key,
            SOURCE1970_OWNER,
            SOURCE1970_TASK,
            manager_target,
            task_sha256,
            authorization["notice_key"],
            semantic_key,
        ]
        tombstone = claim_tombstone(transition_key, claim_key, SOURCE1970_TASK, manager_target, task_sha256)
        selected = [row for row in rows if row and row[0] == claim_key]
        if (
            hashlib.sha256(payload.encode()).hexdigest() != authorization_sha256
            or authorization["target"] != SOURCE1970_OWNER
            or authorization["root"] != SOURCE1970_ROOT
            or authorization["task"] != SOURCE1970_TASK
            or authorization["task_sha256"] != task_sha256
            or authorization["semantic_key"] != semantic_key
            or selected not in ([expected], [tombstone])
            or any(path.exists() for path in forbidden_prior_claim_state(state, claim_key, authorization["notice_key"]))
        ):
            raise OSError("Source-1970 unused claim evidence changed")
        expected_live_rows = [expected] if selected == [expected] else []
        if (
            [row for row in rows if len(row) >= 6 and row[5] == authorization["notice_key"]] != expected_live_rows
            or [row for row in rows if len(row) == 7 and row[6] == semantic_key] != expected_live_rows
        ):
            raise OSError("Source-1970 unused claim is not the sole exact purpose claim")
    matching_plan_rows = [
        row
        for row in rows
        if row
        and (
            row[0] == plan.key
            or len(row) >= 6
            and row[5] == plan.notice_key
            or len(row) == 7
            and row[6] == plan.notice_semantic_key
        )
    ]
    current_paths = (
        authorization_dir / plan.key,
        state / "completion-email-authorization-used" / plan.key,
        state / "completion-email-delivered" / plan.key,
        state / "completion-email-reconciled" / plan.key,
        state / "completion-email-requests" / plan.key,
        state / "completion-email-reconciliations" / plan.key,
        state / "completion-notice-delivered" / plan.notice_key,
        state / "ordinary-completion-by-notice" / plan.notice_key,
    )
    if matching_plan_rows or any(path.exists() for path in current_paths):
        raise OSError("Source-1970 recovery conflicts with existing completion state")


def transition_records(
    static: dict[str, str],
    *,
    plan: CompletionEmail,
    after_task_sha256: str,
    after_queue_sha256: str,
    authorization: dict[str, str],
    ordinary_record: str,
    message_record: str,
) -> tuple[str, str]:
    values = {
        **static,
        "status": "prepared",
        "owner": plan.target,
        "manager_owner": plan.manager_target,
        "semantic_key": plan.notice_semantic_key,
        "after_task_sha256": after_task_sha256,
        "after_queue_sha256": after_queue_sha256,
        "prior_notice_key": authorization["notice_key"],
        "plan_notice_key": plan.notice_key,
        "ordinary_record_sha256": hashlib.sha256(ordinary_record.encode()).hexdigest(),
        "ordinary_record": ordinary_record,
        "message_record_sha256": hashlib.sha256(message_record.encode()).hexdigest(),
        "message_record": message_record,
    }
    prepared = canonical_json_record(values)
    values["status"] = "committed"
    return prepared, canonical_json_record(values)


def prepare_ordinary_pending_transition(
    plan: CompletionEmail,
    items: tuple[str, ...],
    evidence: str,
    after_task_sha256: str,
    after_queue_sha256: str,
    request: OrdinaryPendingRecoveryRequest,
    current_text: str,
) -> OrdinaryPendingTransition:
    """Retire one unused claim and prepare one replayable no-send queue transition."""

    validate_recovery_request(request)
    if plan.send_allowed:
        raise OSError("ordinary pending recovery requires a no-send plan")
    if plan.task_sha256 != request.expected_task_sha256:
        raise OSError("ordinary pending recovery task digest changed")
    if plan.notice_semantic_key != request.semantic_key:
        raise OSError("ordinary pending recovery semantic key changed")
    purpose = ordinary_pending_purpose(plan.outcome, items, evidence)
    if purpose != request.purpose_sha256:
        raise OSError("ordinary pending recovery purpose digest does not match")
    if request.mode == "adopt-add" and (plan.outcome != "pending item created" or evidence):
        raise OSError("claim adoption is supported only for the exact no-evidence add purpose")
    if request.mode in {"supersede-remove", "source1990-pangram-remove", "source1970-eval-remove"} and plan.outcome != "pending item removed after verification":
        raise OSError("claim supersession is supported only for exact verified removal")
    if request.mode == "adopt-add" and request.extra_claim_key:
        raise OSError("claim adoption cannot retire an unrelated extra claim")
    if request.mode == "source1970-eval-remove":
        validate_source1970_eval_authority(plan.root, plan, items, evidence, request, current_text)
    else:
        validate_prior_transition(request, plan.root, plan.task)
        if request.mode == "source1990-pangram-remove":
            validate_source1990_pangram_authority(plan.root, plan, items, evidence, request, current_text)
        elif request.churn_commit:
            validate_manager_churn(plan.root, plan.task, request, request.prior_manager_target, current_text)
        elif canonical_tmux_target(request.prior_manager_target) != canonical_tmux_target(plan.manager_target):
            raise OSError("manager churn requires exact Git evidence")
    if not verify_ordinary_completion_in_sent(
        request.message_id,
        request.sent_subject_sha256,
        request.sent_body_sha256,
    ):
        raise OSError("ordinary pending recovery message is not exact verified Sent-Mail evidence")
    if request.mode not in {"source1990-pangram-remove", "source1970-eval-remove"} and request.sent_body_sha256 != hashlib.sha256(plan.body.encode()).hexdigest():
        raise OSError("ordinary pending recovery Sent-Mail body does not match the canonical recovery notice")
    static = transition_static_values(plan.root, plan.task, plan.outcome, items, evidence, request)
    transition_key = static["transition_key"]
    state = completion_email_state_dir()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    fsync_directory(state.parent)
    authorization_dir = state / "completion-email-authorizations"
    retired_dir = state / "completion-email-retired-authorizations"
    transition_dir = state / "ordinary-pending-transitions"
    message_dir = state / "ordinary-completion-by-message"
    notice_dir = state / "ordinary-completion-by-notice"
    for directory in (retired_dir, transition_dir, message_dir, notice_dir):
        directory.mkdir(mode=0o700, exist_ok=True)
    fsync_directory(state)
    transition_path = transition_dir / transition_key
    message_path = message_dir / hashlib.sha256(request.message_id.encode()).hexdigest()
    ordinary_record = ordinary_completion_record(
        plan,
        request.message_id,
        request.sent_subject_sha256,
        request.sent_body_sha256,
    )
    message_record = f"transition_key={transition_key}\n{ordinary_record}"
    lock_paths = sorted(
        (state / "completion-email-claims.lock", state / "ordinary-completion-reconcile.lock"),
        key=str,
    )
    with ExitStack() as locks:
        for lock_path in lock_paths:
            _ = locks.enter_context(task_file_lock_at_path(lock_path))
        for directory, label in (
            (state, "completion state"),
            (authorization_dir, "completion authorization directory"),
            (retired_dir, "retired completion authorization directory"),
            (transition_dir, "ordinary pending transition directory"),
            (message_dir, "ordinary completion message directory"),
            (notice_dir, "ordinary completion notice directory"),
        ):
            require_private_directory(directory, label)
        ledger, rows, previous = claims_rows(state)
        relative = plan.task.relative_to(plan.root).as_posix()
        if request.mode == "source1970-eval-remove":
            validate_source1970_eval_state(state, plan, request, transition_key, rows)
        retirements: list[tuple[Path, Path, list[str], list[str], dict[str, str], bool]] = []
        for claim_key, claim_task_sha256, manager_target, semantic_key, authorization_sha256 in stale_claim_bindings(request):
            old = authorization_dir / claim_key
            retired = retired_dir / claim_key
            live = old.exists()
            if live == retired.exists():
                raise OSError("ordinary pending recovery authorization state is ambiguous")
            authorization_payload = owned_private_file(
                old if live else retired,
                "prior completion authorization",
                4096,
            ).decode()
            if hashlib.sha256(authorization_payload.encode()).hexdigest() != authorization_sha256:
                raise OSError("prior completion authorization digest does not match")
            authorization = authorization_values(authorization_payload)
            if (
                authorization["target"] != plan.target
                or authorization["root"] != str(plan.root)
                or authorization["task"] != relative
                or authorization["task_sha256"] != claim_task_sha256
                or authorization["semantic_key"] != semantic_key
            ):
                raise OSError("prior completion authorization does not match the exact task")
            expected = [
                claim_key,
                plan.target,
                plan.task.name,
                manager_target,
                claim_task_sha256,
                authorization["notice_key"],
                semantic_key,
            ]
            retirements.append(
                (
                    old,
                    retired,
                    expected,
                    claim_tombstone(transition_key, claim_key, plan.task.name, manager_target, claim_task_sha256),
                    authorization,
                    live,
                )
            )
        authorization = retirements[0][4]
        prepared, committed = transition_records(
            static,
            plan=plan,
            after_task_sha256=after_task_sha256,
            after_queue_sha256=after_queue_sha256,
            authorization=authorization,
            ordinary_record=ordinary_record,
            message_record=message_record,
        )
        try:
            recorded_transition = owned_private_file(
                transition_path,
                "ordinary pending transition",
                32_768,
            ).decode()
        except FileNotFoundError:
            recorded_transition = ""
        if recorded_transition not in {"", prepared, committed}:
            raise OSError("ordinary pending transition is already bound to different evidence")
        try:
            recorded_message = owned_private_file(
                message_path,
                "ordinary completion message evidence",
                16_384,
            ).decode()
        except FileNotFoundError:
            recorded_message = ""
        if recorded_message not in {"", message_record}:
            raise OSError("ordinary pending recovery Message-ID is already bound to different evidence")
        selected = [row for row in rows if row and row[0] == request.prior_claim_key]
        expected_claim = retirements[0][2]
        tombstone = retirements[0][3]
        if selected not in ([expected_claim], [tombstone]):
            raise OSError("prior completion claim is missing or ambiguous")
        if request.mode == "adopt-add":
            same_purpose = (
                authorization["subject_sha256"] == hashlib.sha256(plan.subject.encode()).hexdigest()
                and authorization["body_sha256"] == hashlib.sha256(plan.body.encode()).hexdigest()
            )
            prior_identity = "\0".join(
                (
                    str(plan.root),
                    relative,
                    plan.target,
                    request.prior_manager_target,
                    request.prior_task_sha256,
                    plan.outcome,
                    "\n".join(items),
                    evidence,
                    plan.subject,
                    plan.body,
                )
            )
            if (
                not same_purpose
                or plan.contact_policy is not None
                or hashlib.sha256(prior_identity.encode()).hexdigest() != request.prior_claim_key
            ):
                raise OSError("prior completion claim belongs to a different purpose")
        replacements: dict[tuple[str, ...], list[str]] = {}
        for _old, _retired, expected, retired_claim, prior_authorization, _live in retirements:
            selected = [row for row in rows if row and row[0] == expected[0]]
            if selected not in ([expected], [retired_claim]):
                raise OSError("prior completion claim is missing or ambiguous")
            if selected == [expected]:
                semantic_rows = [row for row in rows if len(row) == 7 and row[6] == expected[6]]
                notice_rows = [row for row in rows if len(row) >= 6 and row[5] == prior_authorization["notice_key"]]
                if semantic_rows != [expected] or notice_rows != [expected]:
                    raise OSError("prior completion claim is not the sole exact purpose claim")
                replacements[tuple(expected)] = retired_claim
            if any(path.exists() for path in forbidden_prior_claim_state(state, expected[0], prior_authorization["notice_key"])):
                raise OSError("prior completion claim may have been used, requested, delivered, or reconciled")
        if not recorded_message:
            exclusive_record(message_path, message_record)
        if replacements:
            rows = [replacements.get(tuple(row), row) for row in rows]
            rewrite_claims(ledger, previous, rows)
        current_forbidden = (
            authorization_dir / plan.key,
            state / "completion-email-authorization-used" / plan.key,
            state / "completion-email-delivered" / plan.key,
            state / "completion-email-reconciled" / plan.key,
            state / "completion-email-requests" / plan.key,
            state / "completion-notice-delivered" / plan.notice_key,
            state / "ordinary-completion-by-notice" / plan.notice_key,
        )
        if plan.key != request.prior_claim_key and any(path.exists() for path in current_forbidden):
            raise OSError("ordinary pending recovery conflicts with existing completion state")
        for old, retired, _expected, _tombstone, _authorization, live in retirements:
            if live:
                os.replace(old, retired)
            fsync_directory(authorization_dir)
            fsync_directory(retired_dir)
        if not recorded_transition:
            exclusive_record(transition_path, prepared)
    return OrdinaryPendingTransition(transition_key, prepared, committed, after_task_sha256)


def commit_ordinary_pending_transition(transition: OrdinaryPendingTransition, task_sha256: str) -> None:
    """Commit a prepared recovery after the exact queue mutation is durable."""

    if task_sha256 != transition.after_task_sha256:
        raise OSError("ordinary pending transition task mutation is not exact")
    state = completion_email_state_dir()
    transition_path = pending_transition_path(transition.key)
    values = json.loads(transition.record)
    request_hash = values["request_sha256"]
    message_id = values["message_id"]
    message_path = state / "ordinary-completion-by-message" / hashlib.sha256(message_id.encode()).hexdigest()
    notice_path = state / "ordinary-completion-by-notice" / values["plan_notice_key"]
    ordinary_record = values["ordinary_record"]
    message_record = values["message_record"]
    lock_paths = sorted(
        (state / "completion-email-claims.lock", state / "ordinary-completion-reconcile.lock"),
        key=str,
    )
    with ExitStack() as locks:
        for lock_path in lock_paths:
            _ = locks.enter_context(task_file_lock_at_path(lock_path))
        current = owned_private_file(transition_path, "ordinary pending transition", 32_768).decode()
        if current == transition.committed_record:
            return
        if current != transition.record:
            raise OSError("ordinary pending transition prepared state changed")
        ledger, rows, _previous = claims_rows(state)
        _ = ledger
        request = request_from_transition_values(values)
        tombstones = {
            claim_key: claim_tombstone(transition.key, claim_key, Path(values["task"]).name, manager_target, claim_task_sha256)
            for claim_key, claim_task_sha256, manager_target, _semantic_key, _authorization_sha256 in stale_claim_bindings(request)
        }
        if recovery_request_sha256_from_values(values) != request_hash or any(
            [row for row in rows if row and row[0] == claim_key] != [tombstone]
            for claim_key, tombstone in tombstones.items()
        ):
            raise OSError("ordinary pending transition tombstone is missing or ambiguous")
        if owned_private_file(message_path, "ordinary completion message evidence", 16_384).decode() != message_record:
            raise OSError("ordinary pending transition message evidence changed")
        try:
            recorded_notice = owned_private_file(notice_path, "ordinary completion reconciliation", 16_384).decode()
        except FileNotFoundError:
            exclusive_record(notice_path, ordinary_record)
        else:
            if recorded_notice != ordinary_record:
                raise OSError("ordinary pending transition notice evidence changed")
        replace_record(transition_path, transition.record, transition.committed_record)


def recovery_request_sha256_from_values(values: dict[str, str]) -> str:
    return recovery_request_sha256(request_from_transition_values(values))


def request_from_transition_values(values: dict[str, str]) -> OrdinaryPendingRecoveryRequest:
    return OrdinaryPendingRecoveryRequest(
        values["mode"],
        values["before_task_sha256"],
        values["before_queue_sha256"],
        values["purpose_sha256"],
        values["semantic_key"],
        values["prior_claim_key"],
        values["prior_task_sha256"],
        values["prior_manager_target"],
        values["prior_semantic_key"],
        values["prior_authorization_sha256"],
        values["message_id"],
        values["sent_subject_sha256"],
        values["sent_body_sha256"],
        values["churn_commit"],
        values["churn_before_blob"],
        values["churn_after_blob"],
        values["churn_diff_sha256"],
        values["prior_transition_key"],
        values["extra_claim_key"],
        values["extra_task_sha256"],
        values["extra_manager_target"],
        values["extra_semantic_key"],
        values["extra_authorization_sha256"],
    )


def ordinary_completion_record_values(payload: str) -> dict[str, str]:
    """Parse one exact ordinary completion record."""

    lines = payload.splitlines()
    try:
        values = dict(line.split("=", 1) for line in lines)
    except ValueError as exc:
        raise OSError("ordinary completion record is malformed") from exc
    expected_names = {
        "version",
        "semantic_key",
        "canonical_key",
        "notice_key",
        "root",
        "task",
        "owner",
        "manager_owner",
        "task_sha256",
        "outcome_sha256",
        "message_id",
        "sent_subject_sha256",
        "sent_body_sha256",
    }
    sha256_names = (
        "semantic_key",
        "canonical_key",
        "notice_key",
        "task_sha256",
        "outcome_sha256",
        "sent_subject_sha256",
        "sent_body_sha256",
    )
    if (
        len(values) != len(lines)
        or set(values) != expected_names
        or values["version"] != "v1"
        or any(SHA256_RE.fullmatch(values[name]) is None for name in sha256_names)
        or re.fullmatch(r"<[^<>\s]+>", values["message_id"]) is None
    ):
        raise OSError("ordinary completion record is malformed")
    return values


def validate_ordinary_pending_transition_record(
    transition_key: str,
    values: dict[str, str],
    payload: str,
) -> OrdinaryPendingRecoveryRequest:
    """Validate one complete canonical transition record without trusting its filename."""

    expected_names = {
        "schema",
        "transition_key",
        "request_sha256",
        "mode",
        "purpose_sha256",
        "root",
        "task",
        "before_task_sha256",
        "before_queue_sha256",
        "outcome_sha256",
        "items_sha256",
        "evidence_sha256",
        "canonical_subject_sha256",
        "canonical_body_sha256",
        "prior_claim_key",
        "prior_task_sha256",
        "prior_manager_target",
        "prior_semantic_key",
        "prior_authorization_sha256",
        "message_id",
        "sent_subject_sha256",
        "sent_body_sha256",
        "churn_commit",
        "churn_before_blob",
        "churn_after_blob",
        "churn_diff_sha256",
        "prior_transition_key",
        "extra_claim_key",
        "extra_task_sha256",
        "extra_manager_target",
        "extra_semantic_key",
        "extra_authorization_sha256",
        "status",
        "owner",
        "manager_owner",
        "semantic_key",
        "after_task_sha256",
        "after_queue_sha256",
        "prior_notice_key",
        "plan_notice_key",
        "ordinary_record_sha256",
        "ordinary_record",
        "message_record_sha256",
        "message_record",
    }
    try:
        request = request_from_transition_values(values)
        validate_recovery_request(request)
        expected_key = digest_fields(
            "ordinary-pending-transition-v1",
            values["root"],
            values["task"],
            values["purpose_sha256"],
            *request.__dict__.values(),
        )
        ordinary_values = ordinary_completion_record_values(values["ordinary_record"])
    except (KeyError, ValueError) as exc:
        raise OSError("ordinary pending transition is malformed") from exc
    sha256_names = (
        "purpose_sha256",
        "before_task_sha256",
        "before_queue_sha256",
        "outcome_sha256",
        "items_sha256",
        "evidence_sha256",
        "canonical_subject_sha256",
        "canonical_body_sha256",
        "semantic_key",
        "after_task_sha256",
        "after_queue_sha256",
        "prior_notice_key",
        "plan_notice_key",
        "ordinary_record_sha256",
        "message_record_sha256",
    )
    if (
        set(values) != expected_names
        or values["schema"] != "omo-ordinary-pending-transition/v1"
        or values["transition_key"] != transition_key
        or expected_key != transition_key
        or values["status"] not in {"prepared", "committed"}
        or values["before_task_sha256"] != request.expected_task_sha256
        or values["before_queue_sha256"] != request.expected_queue_sha256
        or values["purpose_sha256"] != request.purpose_sha256
        or values["semantic_key"] != request.semantic_key
        or ordinary_values["semantic_key"] != values["semantic_key"]
        or ordinary_values["notice_key"] != values["plan_notice_key"]
        or ordinary_values["root"] != values["root"]
        or ordinary_values["task"] != values["task"]
        or ordinary_values["owner"] != values["owner"]
        or ordinary_values["manager_owner"] != values["manager_owner"]
        or ordinary_values["task_sha256"] != values["before_task_sha256"]
        or ordinary_values["outcome_sha256"] != values["outcome_sha256"]
        or ordinary_values["message_id"] != request.message_id
        or ordinary_values["sent_subject_sha256"] != request.sent_subject_sha256
        or ordinary_values["sent_body_sha256"] != request.sent_body_sha256
        or any(SHA256_RE.fullmatch(values[name]) is None for name in sha256_names)
        or recovery_request_sha256(request) != values["request_sha256"]
        or hashlib.sha256(values["ordinary_record"].encode()).hexdigest() != values["ordinary_record_sha256"]
        or hashlib.sha256(values["message_record"].encode()).hexdigest() != values["message_record_sha256"]
        or values["message_record"] != f"transition_key={transition_key}\n{values['ordinary_record']}"
        or payload != canonical_json_record(values)
    ):
        raise OSError("ordinary pending transition does not match its canonical record")
    return request


def load_ordinary_pending_transition(
    root: Path,
    task: Path,
    outcome: str,
    items: tuple[str, ...],
    evidence: str,
    request: OrdinaryPendingRecoveryRequest,
    task_sha256: str,
    queue_sha256: str,
) -> OrdinaryPendingTransition | None:
    """Load an exact prepared/committed transition after its task mutation."""

    validate_recovery_request(request)
    static = transition_static_values(root, task, outcome, items, evidence, request)
    loaded = read_transition_record(static["transition_key"])
    if loaded is None:
        return None
    values, payload = loaded
    recorded_request = validate_ordinary_pending_transition_record(static["transition_key"], values, payload)
    if (
        recorded_request != request
        or any(values.get(name) != value for name, value in static.items())
        or values.get("after_task_sha256") != task_sha256
        or values.get("after_queue_sha256") != queue_sha256
    ):
        raise OSError("ordinary pending transition does not match the exact replay")
    prepared_values = {**values, "status": "prepared"}
    committed_values = {**values, "status": "committed"}
    return OrdinaryPendingTransition(
        static["transition_key"],
        canonical_json_record(prepared_values),
        canonical_json_record(committed_values),
        values["after_task_sha256"],
    )


def ordinary_sent_text(message: Message) -> str:
    if not isinstance(message, EmailMessage):
        return ""
    body = message.get_body(preferencelist=("plain",))
    return body.get_content() if body is not None else ""


def verify_ordinary_completion_in_sent(
    message_id: str,
    subject_sha256: str,
    body_sha256: str,
) -> bool:
    """Verify one exact ordinary agent-to-Human message in Sent Mail."""

    settings = configured_agent_mail()
    if settings is None:
        raise OSError("ordinary completion reconciliation requires split email configuration")
    try:
        timeout_s = max(float(os.environ.get("OMO_COMPLETION_SENT_VERIFY_TIMEOUT_S", "20")), 0)
    except ValueError:
        timeout_s = 20
    deadline_s = time.monotonic() + timeout_s
    while True:
        client: imaplib.IMAP4_SSL | None = None
        try:
            client = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, timeout=min(max(timeout_s, 1), 30))
            client.login(settings.agent_address, settings.app_password)
            kind, _ = client.select('"[Gmail]/Sent Mail"', readonly=True)
            if kind != "OK":
                raise imaplib.IMAP4.error("cannot select Sent Mail")
            kind, found = client.uid("search", None, "HEADER", "Message-ID", message_id)  # pyright: ignore[reportArgumentType]
            uids = b" ".join(value for value in found or [] if isinstance(value, bytes)).split() if kind == "OK" else []
            if len(uids) == 1:
                kind, fetched = client.uid("fetch", uids[0].decode("ascii"), "(BODY.PEEK[])")
                payloads = [value[1] for value in fetched or [] if isinstance(value, tuple) and isinstance(value[1], bytes)]
                if kind == "OK" and len(payloads) == 1:
                    candidate = BytesParser(policy=policy.default).parsebytes(payloads[0])
                    senders = [address.casefold() for _name, address in getaddresses(candidate.get_all("From", []))]
                    recipients = [address.casefold() for _name, address in getaddresses(candidate.get_all("To", []))]
                    if (
                        str(candidate.get("Message-ID", "")) == message_id
                        and senders == [settings.agent_address.casefold()]
                        and recipients == [settings.human_address.casefold()]
                        and not candidate.get_all("Cc", [])
                        and not candidate.get_all("Bcc", [])
                        and hashlib.sha256(str(candidate.get("Subject", "")).encode()).hexdigest() == subject_sha256
                        and hashlib.sha256(ordinary_sent_text(candidate).encode()).hexdigest() == body_sha256
                    ):
                        return True
        except (OSError, ValueError, imaplib.IMAP4.error):
            pass
        finally:
            if client is not None:
                try:
                    client.logout()
                except (OSError, imaplib.IMAP4.error):
                    pass
        if time.monotonic() >= deadline_s:
            return False
        time.sleep(min(0.5, max(0, deadline_s - time.monotonic())))


def completion_notice_key(
    root: Path,
    relative: str,
    target: str,
    manager_target: str,
    outcome: str,
    items: tuple[str, ...],
    evidence: str,
    subject: str,
    body: str,
    task_sha256: str,
    contact_policy: ContactPolicyBinding | None,
    semantic_key: str = "",
) -> str:
    """Identify one task lifecycle notice while exact keys bind message bytes."""

    if semantic_key:
        if SHA256_RE.fullmatch(semantic_key) is None:
            raise ValueError("semantic completion key must be a lowercase SHA-256 digest")
        return hashlib.sha256(
            "\0".join(
                (
                    str(root.resolve()),
                    relative,
                    canonical_tmux_target(target),
                    canonical_tmux_target(manager_target),
                    semantic_key,
                )
            ).encode()
        ).hexdigest()

    identity_parts = (
        str(root.resolve()),
        relative,
        canonical_tmux_target(target),
        canonical_tmux_target(manager_target),
        outcome,
        "\n".join(items),
        evidence,
        subject,
        body,
        task_sha256,
    )
    if contact_policy is not None:
        identity_parts += (str(contact_policy.source), contact_policy.source_sha256)
    return hashlib.sha256("\0".join(identity_parts).encode()).hexdigest()


def completion_email_state_dir() -> Path:
    return Path(os.environ.get("OMO_MANAGER_STATE_DIR", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"))


def require_completion_entrypoint() -> None:
    """Fail closed unless the installed owner-authenticated entry point is safe to execute."""

    try:
        metadata = COMPLETION_ENTRYPOINT.lstat()
    except OSError as exc:
        raise OSError(f"completion entry point is unavailable: {COMPLETION_ENTRYPOINT}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise OSError(f"completion entry point has unsafe type or ownership: {COMPLETION_ENTRYPOINT}")
    if metadata.st_mode & 0o022 or not metadata.st_mode & 0o111:
        raise OSError(f"completion entry point is not safely executable: {COMPLETION_ENTRYPOINT}")


def stable_owned_file(path: Path, maximum_bytes: int) -> bytes:
    """Read one owner-controlled regular file without following or racing a path rebind."""

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o022:
            raise OSError(f"contact-policy source is unsafe: {path}")
        payload = b""
        while chunk := os.read(fd, min(65_536, maximum_bytes + 1 - len(payload))):
            payload += chunk
            if len(payload) > maximum_bytes:
                break
        after = os.fstat(fd)
    finally:
        os.close(fd)
    bound = path.lstat()
    if len(payload) > maximum_bytes:
        raise OSError(f"contact-policy source exceeds {maximum_bytes} bytes: {path}")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or (after.st_dev, after.st_ino) != (bound.st_dev, bound.st_ino):
        raise OSError(f"contact-policy source changed while read: {path}")
    return payload


def is_top_level_record(text: str, offset: int) -> bool:
    """Reject a record nested in routing markup or a Markdown code fence."""

    tags: list[str] = []
    fence = ""
    comment = False
    for line in text[:offset].splitlines():
        if comment:
            if "<!--" in line:
                return False
            if "-->" not in line:
                continue
            comment = False
            line = line.partition("-->")[2]
        if "-->" in line and "<!--" not in line:
            return False
        if "<!--" in line:
            before, _opening, after = line.partition("<!--")
            if "-->" not in after:
                comment = True
                line = before
            else:
                if "<!--" in after.partition("-->")[2]:
                    return False
                line = before + after.partition("-->")[2]
        if len(ROUTING_TAG_TOKEN_RE.findall(line)) > 1:
            return False
        fence_match = MARKDOWN_FENCE_RE.match(line)
        if not fence and fence_match is not None:
            fence = fence_match.group("fence")
            continue
        if fence:
            closing = line.strip()
            if closing and set(closing) == {fence[0]} and len(closing) >= len(fence):
                fence = ""
            continue
        close_match = ROUTING_CLOSE_TAG_RE.search(line)
        if close_match is not None:
            tag = close_match.group("tag")
            if not tags or tags.pop() != tag:
                return False
            continue
        open_match = ROUTING_OPEN_TAG_RE.search(line)
        if open_match is not None:
            tags.append(open_match.group("tag"))
    return not tags and not fence and not comment


# 🧑 Human source `manager_mail/85c5dff58359-1241.txt:1-7`: "They should sort it out themselves. ... they should go ahead and do the job"
def source1241_contact_clarification(root: Path, task: Path, text: str) -> ContactPolicyBinding | None:
    """Bind the sole exact Source-1241 meta-reference that is not a no-contact directive."""

    context_offset = text.find(SOURCE1241_CONTEXT)
    context_end = context_offset + len(SOURCE1241_CONTEXT)
    source1241_envelopes = SOURCE1241_LOCATOR_ENVELOPE_RE.findall(text)
    if (
        task.resolve() != (root / SOURCE1241_TASK).resolve()
        or text.count(SOURCE1241_CONTEXT) != 1
        or text.count(SOURCE1241_ENVELOPE) != 1
        or source1241_envelopes != [SOURCE1241_HUMAN]
        or text.count(SOURCE1241_META_LINE) != 1
        or text.count(SOURCE1241_META_SPAN) != 1
        or (context_offset > 0 and text[context_offset - 1] != "\n")
        or (context_end < len(text) and text[context_end] != "\n")
        or not is_top_level_record(text, context_offset)
    ):
        return None
    try:
        task_payload = stable_owned_file(task, 8_000_000)
        source = root / SOURCE1241_REF.partition(":")[0]
        source_payload = stable_owned_file(source, 8_000_000)
        source_text = source_payload.decode()
    except (OSError, UnicodeDecodeError):
        return None
    if task_payload != text.encode() or "\n".join(source_text.splitlines()[:7]) != SOURCE1241_HUMAN:
        return None
    return ContactPolicyBinding(hashlib.sha256(task_payload).hexdigest(), source, hashlib.sha256(source_payload).hexdigest())


def _build_completion_email(
    root: Path,
    task: Path,
    text: str,
    outcome: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
    semantic_key: str = "",
    sent_recovery: bool = False,
) -> CompletionEmail | None:
    """Build one canonical notice; recovery plans can never authorize sending."""

    metadata = parse_task_metadata(text, root)
    policy_text = text
    contact_policy = None
    if SOURCE1241_META_SPAN in text:
        contact_policy = source1241_contact_clarification(root, task, text)
        if contact_policy is not None:
            policy_text = text.replace(SOURCE1241_META_SPAN, "", 1)
    task_close = outcome == "task done"
    pending_item_notice = outcome in PENDING_ITEM_NOTICE_OUTCOMES
    human_pending_item_notice = pending_item_notice and bool(items) and human_authored_pending_items(items) == items
    contact_forbidden = (
        human_pending_notice_contact_forbidden(policy_text) if human_pending_item_notice else NO_CONTACT_RE.search(policy_text) is not None
    ) or (
        not human_pending_item_notice and MANAGER_ONLY_RE.search(policy_text) is not None and DIRECT_HUMAN_REPORT_RE.search(policy_text) is None
    )
    if (
        metadata is None
        or metadata.is_manager
        or metadata.runat == "retired"
        or metadata.runat.partition(":")[0].startswith("h")
        or guest_hees_target(metadata.runat)
        or contact_forbidden
        or (pending_item_notice and not human_pending_item_notice)
        or (not task_close and not pending_item_notice and DIRECT_HUMAN_REPORT_RE.search(policy_text) is None)
    ):
        return None
    relative = task.resolve().relative_to(root.resolve()).as_posix()
    # 🧑 "When closing an agent, use the last email chain the agent used to send an automatic email ‘Closed xx:n’ with the agent’s window."
    if task_close:
        subject = ""
        body = f"Closed {tmux_window_target(metadata.runat)}\n"
    elif human_pending_item_notice:
        subject = ""
        body = pending_item_notice_body(outcome, items)
    else:
        details = [f"Task: {relative}", f"Outcome: {outcome}"]
        if items:
            details.append("Items:")
            details.extend(f"- {item}" for item in items)
        if evidence:
            details.append(f"Evidence: {evidence}")
        subject = f"{task.name}: {outcome}"
        body = "\n".join(details) + "\n"
    if semantic_key and SHA256_RE.fullmatch(semantic_key) is None:
        raise ValueError("semantic completion key must be a lowercase SHA-256 digest")
    notice_semantic_key = hashlib.sha256(f"{semantic_key}\0task-close".encode()).hexdigest() if task_close and semantic_key else semantic_key
    task_sha256 = hashlib.sha256(text.encode()).hexdigest()
    identity_parts = (
        str(root.resolve()),
        relative,
        metadata.runat,
        metadata.managerat,
        task_sha256,
        outcome,
        "\n".join(items),
        evidence,
        subject,
        body,
    )
    if contact_policy is not None:
        identity_parts += (contact_policy.task_sha256, str(contact_policy.source), contact_policy.source_sha256)
    identity = "\0".join(identity_parts)
    notice_key = completion_notice_key(
        root,
        relative,
        metadata.runat,
        metadata.managerat,
        outcome,
        items,
        evidence,
        subject,
        body,
        task_sha256,
        contact_policy,
        notice_semantic_key,
    )
    return CompletionEmail(
        root.resolve(),
        task.resolve(),
        metadata.runat,
        metadata.managerat,
        task_sha256,
        outcome,
        subject,
        body,
        hashlib.sha256(identity.encode()).hexdigest(),
        notice_key,
        semantic_key,
        contact_policy,
        not sent_recovery,
    )


# 🧑 Human: "correct the routing/reporting behavior so workers, not managers, report only requested results."
def build_completion_email(
    root: Path,
    task: Path,
    text: str,
    outcome: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
    semantic_key: str = "",
) -> CompletionEmail | None:
    """Build the canonical notice without assigning reporter authority."""

    return _build_completion_email(
        root,
        task,
        text,
        outcome,
        items=items,
        evidence=evidence,
        semantic_key=semantic_key,
    )


# 🧑 Human source `202607/manager_mail/85c5dff58359-1090.txt:1-3`: "You should let the responsible agent report and discuss with me directly instead of duplicating messages to me"
def plan_completion_email(
    root: Path,
    task: Path,
    text: str,
    outcome: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
    human_subject: str = "",
    human_body: str = "",
    semantic_key: str = "",
    pending_item_owner: bool = False,
) -> CompletionEmail | None:
    """Return mail only when the caller is the exact task owner and contact is allowed."""

    canonical = build_completion_email(root, task, text, outcome, items=items, evidence=evidence, semantic_key=semantic_key)
    if canonical is None:
        return None
    try:
        resolve_owner = current_pending_task if pending_item_owner else current_active_task
        caller_path = resolve_owner(root).resolve()
    except (OSError, TaskFrontmatterError):
        return None
    if caller_path != task.resolve():
        return None
    require_completion_entrypoint()
    relative = task.resolve().relative_to(root.resolve()).as_posix()
    if bool(human_subject) != bool(human_body):
        raise ValueError("human answer requires both subject and body")
    if human_subject:
        if outcome == "task done":
            raise ValueError("task close cannot override its exact automatic email")
        if outcome in PENDING_ITEM_NOTICE_OUTCOMES:
            raise ValueError("pending-item notice cannot override its exact thread or body")
        if human_subject.strip() != human_subject or "\n" in human_subject or "\r" in human_subject:
            raise ValueError("human answer subject must be one non-empty trimmed line")
        subject = human_subject
        body = f"{human_body.rstrip()}\n\nCompletion record:\n{canonical.body}"
    else:
        return canonical
    identity_parts = (
        str(root.resolve()),
        relative,
        canonical.target,
        canonical.manager_target,
        canonical.task_sha256,
        outcome,
        "\n".join(items),
        evidence,
        subject,
        body,
    )
    if canonical.contact_policy is not None:
        identity_parts += (canonical.contact_policy.task_sha256, str(canonical.contact_policy.source), canonical.contact_policy.source_sha256)
    identity = "\0".join(identity_parts)
    return CompletionEmail(
        canonical.root,
        canonical.task,
        canonical.target,
        canonical.manager_target,
        canonical.task_sha256,
        outcome,
        subject,
        body,
        hashlib.sha256(identity.encode()).hexdigest(),
        completion_notice_key(
            canonical.root,
            relative,
            canonical.target,
            canonical.manager_target,
            outcome,
            items,
            evidence,
            subject,
            body,
            canonical.task_sha256,
            canonical.contact_policy,
            canonical.semantic_key,
        ),
        canonical.semantic_key,
        canonical.contact_policy,
        canonical.send_allowed,
    )


def plan_sent_recovery_completion(
    root: Path,
    task: Path,
    text: str,
    outcome: str,
    *,
    items: tuple[str, ...],
    evidence: str,
    semantic_key: str,
) -> CompletionEmail | None:
    """Build an exact-owner no-send plan for already-delivered Sent evidence."""

    plan = _build_completion_email(
        root,
        task,
        text,
        outcome,
        items=items,
        evidence=evidence,
        semantic_key=semantic_key,
        sent_recovery=True,
    )
    if plan is None:
        return None
    try:
        owner = current_pending_task(root).resolve()
    except (OSError, TaskFrontmatterError):
        return None
    return plan if owner == task.resolve() else None


def reconciliation_record(
    plan: CompletionEmail,
    task_sha256: str,
    receipt: Path,
    receipt_sha256: str,
    target_state: Path,
) -> str:
    relative = plan.task.relative_to(plan.root).as_posix()
    message_sha256 = hashlib.sha256(f"{plan.subject}\0{plan.body}".encode()).hexdigest()
    outcome_sha256 = hashlib.sha256(plan.outcome.encode()).hexdigest()
    values = (
        ("version", RECONCILIATION_VERSION),
        ("completion_key", plan.key),
        ("root", str(plan.root)),
        ("task", relative),
        ("owner", plan.target),
        ("outcome_sha256", outcome_sha256),
        ("task_sha256", task_sha256),
        ("message_sha256", message_sha256),
        ("source_receipt", str(receipt)),
        ("source_receipt_sha256", receipt_sha256),
        ("target_state", str(target_state)),
    )
    if any(any(character in value for character in "\r\n") for _, value in values):
        raise ValueError("completion reconciliation values must be single-line")
    return "".join(f"{key}={value}\n" for key, value in values)


def reconciled_completion_is_delivered(plan: CompletionEmail) -> bool:
    marker = completion_email_state_dir() / "completion-email-reconciled" / plan.key
    try:
        payload = owned_private_file(marker, "reconciled completion", 16_384).decode()
    except FileNotFoundError:
        return False
    except UnicodeDecodeError as exc:
        raise OSError("reconciled completion is not UTF-8") from exc
    lines = payload.splitlines()
    try:
        values = dict(line.split("=", 1) for line in lines)
    except ValueError:
        raise OSError("reconciled completion is malformed")
    if len(lines) != len(values) or set(values) != {
        "version",
        "completion_key",
        "root",
        "task",
        "owner",
        "outcome_sha256",
        "task_sha256",
        "message_sha256",
        "source_receipt",
        "source_receipt_sha256",
        "target_state",
    }:
        raise OSError("reconciled completion has wrong or duplicate fields")
    task_sha256 = hashlib.sha256(plan.task.read_bytes()).hexdigest()
    expected = reconciliation_record(
        plan,
        task_sha256,
        Path(values["source_receipt"]),
        values["source_receipt_sha256"],
        completion_email_state_dir().resolve(),
    )
    if payload != expected:
        raise OSError("reconciled completion does not match current task bytes or message")
    return True


def ordinary_completion_record(
    plan: CompletionEmail,
    message_id: str,
    subject_sha256: str,
    body_sha256: str,
) -> str:
    values = (
        ("version", "v1"),
        ("semantic_key", plan.notice_semantic_key),
        ("canonical_key", plan.key),
        ("notice_key", plan.notice_key),
        ("root", str(plan.root)),
        ("task", plan.task.relative_to(plan.root).as_posix()),
        ("owner", plan.target),
        ("manager_owner", plan.manager_target),
        ("task_sha256", plan.task_sha256),
        ("outcome_sha256", hashlib.sha256(plan.outcome.encode()).hexdigest()),
        ("message_id", message_id),
        ("sent_subject_sha256", subject_sha256),
        ("sent_body_sha256", body_sha256),
    )
    if any(any(character in value for character in "\r\n") for _name, value in values):
        raise ValueError("ordinary completion evidence must be single-line")
    return "".join(f"{name}={value}\n" for name, value in values)


def ordinary_completion_is_reconciled(plan: CompletionEmail) -> bool:
    if plan.outcome == "task done":
        return False
    marker = completion_email_state_dir() / "ordinary-completion-by-notice" / plan.notice_key
    try:
        payload = owned_private_file(marker, "ordinary completion reconciliation", 16_384).decode()
    except FileNotFoundError:
        return False
    values = ordinary_completion_record_values(payload)
    if (
        values["semantic_key"] != plan.notice_semantic_key
        or values["notice_key"] != plan.notice_key
        or values["root"] != str(plan.root)
        or values["task"] != plan.task.relative_to(plan.root).as_posix()
        or canonical_tmux_target(values["owner"]) != canonical_tmux_target(plan.target)
        or canonical_tmux_target(values["manager_owner"]) != canonical_tmux_target(plan.manager_target)
    ):
        raise OSError("ordinary completion reconciliation does not match the current task")
    message_marker = completion_email_state_dir() / "ordinary-completion-by-message" / hashlib.sha256(
        values["message_id"].encode()
    ).hexdigest()
    message_payload = owned_private_file(message_marker, "ordinary completion message evidence", 16_384).decode()
    if message_payload != payload:
        transition_line, separator, ordinary_payload = message_payload.partition("\n")
        transition_key = transition_line.removeprefix("transition_key=")
        transition = read_transition_record(transition_key) if separator and SHA256_RE.fullmatch(transition_key) else None
        if transition is None:
            raise OSError("ordinary completion message evidence is missing or ambiguous")
        transition_values, transition_payload = transition
        transition_request = validate_ordinary_pending_transition_record(
            transition_key,
            transition_values,
            transition_payload,
        )
        expected_ordinary_payload = ordinary_completion_record(
            plan,
            transition_request.message_id,
            transition_request.sent_subject_sha256,
            transition_request.sent_body_sha256,
        )
        if (
            transition_values["status"] != "committed"
            or transition_values["root"] != str(plan.root)
            or transition_values["task"] != plan.task.relative_to(plan.root).as_posix()
            or transition_values["owner"] != plan.target
            or transition_values["manager_owner"] != plan.manager_target
            or transition_values["before_task_sha256"] != plan.task_sha256
            or transition_values["outcome_sha256"] != hashlib.sha256(plan.outcome.encode()).hexdigest()
            or transition_values["canonical_subject_sha256"] != hashlib.sha256(plan.subject.encode()).hexdigest()
            or transition_values["canonical_body_sha256"] != hashlib.sha256(plan.body.encode()).hexdigest()
            or transition_values["plan_notice_key"] != plan.notice_key
            or ordinary_payload != expected_ordinary_payload
            or payload != expected_ordinary_payload
            or transition_values["ordinary_record"] != expected_ordinary_payload
            or transition_values["message_record"] != message_payload
        ):
            raise OSError("ordinary completion message evidence is missing or ambiguous")
    return True


# 🧑 Human: "Add a supported reconciliation path that binds an already-sent ordinary direct Human completion email Message-ID and exact evidence to a new lifecycle completion key without sending a duplicate"
def reconcile_ordinary_sent_completion(
    root: Path,
    task: Path,
    outcome: str,
    message_id: str,
    subject_sha256: str,
    body_sha256: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
    semantic_key: str,
) -> None:
    """Bind exact Sent-Mail evidence to one owner task without sending mail."""

    if outcome == "task done":
        raise ValueError("ordinary Sent-Mail reconciliation cannot satisfy the exact automatic task-close email")
    if re.fullmatch(r"<[^<>\s]+>", message_id) is None:
        raise ValueError("ordinary completion Message-ID is invalid")
    if SHA256_RE.fullmatch(subject_sha256) is None or SHA256_RE.fullmatch(body_sha256) is None:
        raise ValueError("ordinary completion subject and body digests must be lowercase SHA-256 values")
    root = root.resolve()
    task = task.resolve()
    with task_file_lock(task):
        text = task.read_text(encoding="utf-8")
        plan = plan_completion_email(
            root,
            task,
            text,
            outcome,
            items=items,
            evidence=evidence,
            semantic_key=semantic_key,
        )
        if plan is None:
            raise OSError("ordinary completion reconciliation requires the exact active task owner")
        if not verify_ordinary_completion_in_sent(message_id, subject_sha256, body_sha256):
            raise OSError("ordinary completion message is not exact verified Sent-Mail evidence")
        record = ordinary_completion_record(plan, message_id, subject_sha256, body_sha256)
        state = completion_email_state_dir()
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        state.chmod(0o700)
        notice_dir = state / "ordinary-completion-by-notice"
        message_dir = state / "ordinary-completion-by-message"
        notice_dir.mkdir(mode=0o700, exist_ok=True)
        message_dir.mkdir(mode=0o700, exist_ok=True)
        notice_marker = notice_dir / plan.notice_key
        message_marker = message_dir / hashlib.sha256(message_id.encode()).hexdigest()
        lock_paths = sorted(
            (state / "completion-email-claims.lock", state / "ordinary-completion-reconcile.lock"),
            key=str,
        )
        with ExitStack() as locks:
            for lock_path in lock_paths:
                _ = locks.enter_context(task_file_lock_at_path(lock_path))
            for directory, label in (
                (state, "completion state"),
                (notice_dir, "ordinary completion notice directory"),
                (message_dir, "ordinary completion message directory"),
            ):
                require_private_directory(directory, label)
            structured_paths = (
                state / "completion-email-delivered" / plan.key,
                state / "completion-notice-delivered" / plan.notice_key,
                state / "completion-email-requests" / plan.key,
                state / "completion-email-authorizations" / plan.key,
                state / "completion-email-authorization-used" / plan.key,
            )
            try:
                claims = owned_private_file(
                    state / "completion-email-claims.tsv", "completion claims ledger", 8_000_000
                ).decode().splitlines()
            except FileNotFoundError:
                claims = []
            if any(len(line.split("\t")) not in {3, 5, 6, 7} for line in claims):
                raise OSError("completion claims ledger is malformed")
            matching_claims = [
                fields
                for fields in (line.split("\t") for line in claims)
                if fields
                and (
                    fields[0] == plan.key
                    or len(fields) >= 6
                    and fields[5] == plan.notice_key
                )
            ]
            authorization_dir = state / "completion-email-authorizations"
            matching_authorizations: list[Path] = []
            authorizations: set[str] = set()
            if authorization_dir.exists():
                require_private_directory(authorization_dir, "completion email authorization directory")
                for authorization in authorization_dir.iterdir():
                    if SHA256_RE.fullmatch(authorization.name) is None:
                        raise OSError("completion email authorization entry is malformed")
                    payload = owned_private_file(authorization, "completion email authorization", 4096).decode()
                    try:
                        values = dict(line.split("=", 1) for line in payload.splitlines())
                    except ValueError as exc:
                        raise OSError("completion email authorization is malformed") from exc
                    current_fields = {
                        "version",
                        "target",
                        "root",
                        "task",
                        "task_sha256",
                        "notice_key",
                        "semantic_key",
                        "subject_sha256",
                        "body_sha256",
                    }
                    legacy_fields = current_fields - {"task_sha256", "semantic_key"}
                    if (
                        len(values) != len(payload.splitlines())
                        or frozenset(values) not in {frozenset(current_fields), frozenset(legacy_fields)}
                        or values["version"] != "1"
                    ):
                        raise OSError("completion email authorization is malformed")
                    authorizations.add(authorization.name)
                    if values["notice_key"] == plan.notice_key and values.get("semantic_key", plan.notice_semantic_key) == plan.notice_semantic_key:
                        matching_authorizations.append(authorization)
            used_dir = state / "completion-email-authorization-used"
            if used_dir.exists():
                require_private_directory(used_dir, "completion authorization use directory")
                for used in used_dir.iterdir():
                    if SHA256_RE.fullmatch(used.name) is None or used.name not in authorizations:
                        raise OSError("completion authorization use has no exact authorization")
                    _ = owned_private_file(used, "completion authorization use", 4096)
            if matching_claims or matching_authorizations or any(path.exists() for path in structured_paths):
                raise OSError("ordinary completion cannot replace existing structured completion state")
            markers = (
                (notice_marker, "ordinary completion reconciliation"),
                (message_marker, "ordinary completion message evidence"),
            )
            existing: dict[Path, str] = {}
            for marker, label in markers:
                try:
                    recorded = owned_private_file(marker, label, 16_384).decode()
                except FileNotFoundError:
                    continue
                if recorded != record:
                    raise OSError(f"{label} is already bound to different evidence")
                existing[marker] = recorded
            for marker, _label in markers:
                if marker not in existing:
                    exclusive_record(marker, record)


def claimed_completion_notice(plan: CompletionEmail) -> tuple[str, str, str, str] | None:
    """Find the single atomic claim for this semantic notice."""

    ledger = completion_email_state_dir() / "completion-email-claims.tsv"
    try:
        claims = owned_private_file(ledger, "completion claims ledger", 8_000_000).decode().splitlines()
    except FileNotFoundError:
        return None
    matches: list[tuple[str, str, str, str]] = []
    for line in claims:
        fields = line.split("\t")
        if len(fields) not in {3, 5, 6, 7}:
            raise OSError("completion claims ledger is malformed")
        if len(fields) == 7 and fields[5] == plan.notice_key and fields[6] == plan.notice_semantic_key:
            key, target, task, manager_target, _task_sha256, _notice_key, _semantic_key = fields
            if SHA256_RE.fullmatch(key) is None:
                raise OSError("completion notice claim is malformed")
            matches.append((key, target, task, manager_target))
    if len(matches) > 1:
        raise OSError("completion notice claim is ambiguous")
    return matches[0] if matches else None


def claimed_completion_receipt(plan: CompletionEmail) -> tuple[str, str] | None:
    """Find an exact-task receipt claim without rejecting a semantic sibling."""

    claim = claimed_completion_notice(plan)
    if claim is None:
        return None
    key, target, task, manager_target = claim
    if (
        canonical_tmux_target(target) != canonical_tmux_target(plan.target)
        or task != plan.task.name
        or canonical_tmux_target(manager_target) != canonical_tmux_target(plan.manager_target)
    ):
        return None
    return key, target


def has_exact_completion_claim(plan: CompletionEmail, key: str, target: str) -> bool:
    """Accept historical exact claims without treating them as semantic claims."""

    ledger = completion_email_state_dir() / "completion-email-claims.tsv"
    claims = owned_private_file(ledger, "completion claims ledger", 8_000_000).decode().splitlines()
    matches = []
    for line in claims:
        fields = line.split("\t")
        if len(fields) not in {3, 5, 6, 7}:
            raise OSError("completion claims ledger is malformed")
        if (
            fields[0] == key
            and canonical_tmux_target(fields[1]) == canonical_tmux_target(target)
            and fields[2] == plan.task.name
        ):
            matches.append(fields)
    return len(matches) == 1


def exact_completion_claim_sha(plan: CompletionEmail, key: str, target: str) -> str | None:
    ledger = completion_email_state_dir() / "completion-email-claims.tsv"
    claims = owned_private_file(ledger, "completion claims ledger", 8_000_000).decode().splitlines()
    matches = []
    for line in claims:
        fields = line.split("\t")
        if len(fields) not in {3, 5, 6, 7}:
            raise OSError("completion claims ledger is malformed")
        if fields[0] == key and canonical_tmux_target(fields[1]) == canonical_tmux_target(target) and fields[2] == plan.task.name:
            matches.append(fields[4] if len(fields) >= 5 else "")
    return matches[0] if len(matches) == 1 else None


def completion_email_is_delivered(plan: CompletionEmail) -> bool:
    state_dir = completion_email_state_dir()
    exact_marker = state_dir / "completion-email-delivered" / plan.key
    notice_marker = state_dir / "completion-notice-delivered" / plan.notice_key
    if exact_marker.is_file():
        mark_completion_email_delivered(plan, plan.key)
        return True
    if notice_marker.is_file():
        recorded_notice = owned_private_file(notice_marker, "completion notice delivery", 4096).decode()
        try:
            recorded_key, recorded_target, recorded_task, recorded_task_sha256 = recorded_notice.rstrip("\n").split("\t")
        except ValueError as exc:
            raise OSError("completion notice delivery is malformed") from exc
        ledger = owned_private_file(state_dir / "completion-email-claims.tsv", "completion claims ledger", 8_000_000).decode()
        matching_claims = [
            row
            for row in (line.split("\t") for line in ledger.splitlines())
            if len(row) == 7
            and row[0] == recorded_key
            and canonical_tmux_target(row[1]) == canonical_tmux_target(recorded_target)
            and row[2] == recorded_task
            and row[4] == recorded_task_sha256
            and row[5] == plan.notice_key
            and row[6] == plan.notice_semantic_key
        ]
        if len(matching_claims) != 1:
            raise OSError("completion notice semantic key has no atomic claim")
        if (
            recorded_task != plan.task.name
            or canonical_tmux_target(recorded_target) != canonical_tmux_target(plan.target)
        ):
            raise OSError("completion notice delivery does not match the current task")
        if recorded_task_sha256 != plan.task_sha256:
            # A stable task id in notice_key permits the expected pending-to-done
            # byte transition without weakening either exact keyed receipt.
            return True
        mark_completion_email_delivered(plan)
        return True
    if ordinary_completion_is_reconciled(plan):
        return True
    claimed_receipt = claimed_completion_receipt(plan)
    if claimed_receipt is not None:
        receipt = state_dir / "completion-email-delivered" / claimed_receipt[0]
        if receipt.is_file():
            payload = owned_private_file(receipt, "completion delivery", 4096).decode()
            current = f"{claimed_receipt[1]}\t{plan.task.name}\t{plan.task_sha256}\n"
            legacy = f"{claimed_receipt[1]}\t{plan.task.name}\n"
            claim_sha = exact_completion_claim_sha(plan, *claimed_receipt)
            claimed = f"{claimed_receipt[1]}\t{plan.task.name}\t{claim_sha}\n" if claim_sha else ""
            if payload not in (current, legacy, claimed):
                raise OSError("completion delivery does not match the task")
            if payload in (current, legacy):
                mark_completion_email_delivered(plan, *claimed_receipt)
            return True
    # A sibling owner may have claimed the shared semantic notice but not yet
    # reached SMTP.  It owns the in-flight attempt; this route must not raise or
    # create a second claim.
    if claimed_completion_notice(plan) is not None:
        return False
    return reconciled_completion_is_delivered(plan)


def validate_completion_notice_delivery(plan: CompletionEmail) -> str:
    """Authenticate a delivered semantic notice without creating completion state."""

    if SHA256_RE.fullmatch(plan.semantic_key) is None:
        raise OSError("completion delivery recovery requires a semantic completion key")
    state = completion_email_state_dir().resolve()
    require_private_directory(state, "completion state")
    notice_directory = state / "completion-notice-delivered"
    delivery_directory = state / "completion-email-delivered"
    authorization_directory = state / "completion-email-authorizations"
    used_directory = state / "completion-email-authorization-used"
    for directory, label in (
        (notice_directory, "completion notice delivery directory"),
        (delivery_directory, "completion delivery directory"),
        (authorization_directory, "completion email authorization directory"),
        (used_directory, "completion authorization use directory"),
    ):
        require_private_directory(directory, label)
    notice = owned_private_file(notice_directory / plan.notice_key, "completion notice delivery", 4096).decode()
    try:
        key, target, task_name, task_sha256 = notice.rstrip("\n").split("\t")
    except ValueError as exc:
        raise OSError("completion notice delivery is malformed") from exc
    if (
        SHA256_RE.fullmatch(key) is None
        or SHA256_RE.fullmatch(task_sha256) is None
        or canonical_tmux_target(target) != canonical_tmux_target(plan.target)
        or task_name != plan.task.name
    ):
        raise OSError("completion notice delivery does not match the recovery task")
    claims = owned_private_file(state / "completion-email-claims.tsv", "completion claims ledger", 8_000_000).decode().splitlines()
    rows = [line.split("\t") for line in claims]
    if any(len(row) not in {3, 5, 6, 7} for row in rows):
        raise OSError("completion claims ledger is malformed")
    expected_claim = [key, target, task_name, plan.manager_target, task_sha256, plan.notice_key, plan.notice_semantic_key]
    if (
        [row for row in rows if row[0] == key] != [expected_claim]
        or [row for row in rows if len(row) >= 6 and row[5] == plan.notice_key] != [expected_claim]
        or [row for row in rows if len(row) == 7 and row[6] == plan.notice_semantic_key] != [expected_claim]
    ):
        raise OSError("completion notice delivery lacks one exact semantic claim")
    authorization_payload = owned_private_file(authorization_directory / key, "completion email authorization", 4096).decode()
    try:
        authorization = dict(line.split("=", 1) for line in authorization_payload.splitlines())
    except ValueError as exc:
        raise OSError("completion email authorization is malformed") from exc
    expected_authorization = {
        "version": "1",
        "target": target,
        "root": str(plan.root.resolve()),
        "task": plan.task.resolve().relative_to(plan.root.resolve()).as_posix(),
        "task_sha256": task_sha256,
        "notice_key": plan.notice_key,
        "semantic_key": plan.notice_semantic_key,
        "subject_sha256": hashlib.sha256(plan.subject.encode()).hexdigest(),
        "body_sha256": hashlib.sha256(plan.body.encode()).hexdigest(),
    }
    if len(authorization) != len(authorization_payload.splitlines()) or authorization != expected_authorization:
        raise OSError("completion email authorization does not match the recovery task")
    delivered = owned_private_file(delivery_directory / key, "completion delivery", 4096).decode()
    used = owned_private_file(used_directory / key, "completion authorization use", 4096).decode()
    if delivered != f"{target}\t{task_name}\t{task_sha256}\n" or used != f"{target}\t{task_name}\n":
        raise OSError("completion delivery or authorization use does not match the recovery task")
    return task_sha256


def completion_email_request_is_queued(plan: CompletionEmail) -> bool:
    return (completion_email_state_dir() / "completion-email-requests" / plan.key).is_file()


def fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def require_private_directory(path: Path, label: str) -> None:
    state = path.lstat()
    if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
        raise OSError(f"{label} must be an owner-private directory: {path}")


def owned_private_file(path: Path, label: str, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise OSError(f"{label} must be an owner-private regular file: {path}")
        payload = b""
        while chunk := os.read(fd, min(65_536, maximum_bytes + 1 - len(payload))):
            payload += chunk
            if len(payload) > maximum_bytes:
                raise OSError(f"{label} exceeds {maximum_bytes} bytes")
        after = os.fstat(fd)
    finally:
        os.close(fd)
    bound = path.lstat()
    if len(payload) > maximum_bytes:
        raise OSError(f"{label} exceeds {maximum_bytes} bytes")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or (bound.st_dev, bound.st_ino) != (before.st_dev, before.st_ino):
        raise OSError(f"{label} changed while read")
    return payload


def exclusive_record(path: Path, payload: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def replace_record(path: Path, expected: str, updated: str) -> None:
    if owned_private_file(path, "completion reconciliation consumption", 32_768).decode() != expected:
        raise OSError("completion reconciliation consumption changed")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            _ = handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


# 🧑 Human: "Treat `<manager_delegation from=\"TARGET\">` as an agent-authored task specification, never as the human's words."
def reconcile_delivered_completion(
    root: Path,
    task: Path,
    outcome: str,
    owner: str,
    task_sha256: str,
    receipt: Path,
    receipt_sha256: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
    semantic_key: str = "",
) -> None:
    """Consume one exact cross-state delivery receipt into the caller's state."""

    if not receipt.is_absolute() or receipt.parent.name != "completion-email-delivered" or receipt.resolve() != receipt:
        raise ValueError("completion receipt must be an absolute completion-email-delivered entry")
    if SHA256_RE.fullmatch(task_sha256) is None or SHA256_RE.fullmatch(receipt_sha256) is None:
        raise ValueError("task and receipt SHA-256 values must be lowercase hexadecimal")
    root = root.resolve()
    task = task.resolve()
    configured_target_state = completion_email_state_dir()
    if not configured_target_state.is_absolute():
        raise ValueError("target completion state must be absolute")
    target_state = configured_target_state.resolve()
    source_state = receipt.parent.parent.resolve()
    if source_state == target_state:
        raise ValueError("source and target completion states must differ")
    require_private_directory(target_state, "target completion state")
    with task_file_lock(task):
        task_payload = task.read_bytes()
        if hashlib.sha256(task_payload).hexdigest() != task_sha256:
            raise OSError("task bytes do not match --task-sha256")
        text = task_payload.decode()
        plan = build_completion_email(root, task, text, outcome, items=items, evidence=evidence, semantic_key=semantic_key)
        if plan is None or plan.target != owner or plan.task_sha256 != task_sha256:
            raise OSError("task, owner, or completion outcome does not match the receipt")
        if receipt.name != plan.key:
            raise OSError("completion receipt does not match the canonical completion message")
        record = reconciliation_record(plan, task_sha256, receipt, receipt_sha256, target_state)
        prepared = f"status=prepared\n{record}"
        completed = f"status=completed\n{record}"
        consume_dir = source_state / "completion-email-reconciliations"
        reconciled_dir = target_state / "completion-email-reconciled"
        consume = consume_dir / plan.key
        destination = reconciled_dir / plan.key
        lock_paths = sorted(
            {source_state / "completion-email-reconcile.lock", target_state / "completion-email-reconcile.lock"},
            key=str,
        )
        with ExitStack() as locks:
            for lock_path in lock_paths:
                _ = locks.enter_context(task_file_lock_at_path(lock_path))
            require_private_directory(source_state, "source completion state")
            require_private_directory(receipt.parent, "source delivery directory")
            require_private_directory(target_state, "target completion state")
            receipt_payload = owned_private_file(receipt, "completion receipt", 4096)
            if hashlib.sha256(receipt_payload).hexdigest() != receipt_sha256:
                raise OSError("completion receipt does not match --receipt-sha256")
            if receipt_payload.decode() not in (
                f"{owner}\t{task.name}\n",
                f"{owner}\t{task.name}\t{task_sha256}\n",
            ):
                raise OSError("completion receipt task or owner is wrong")
            claims = owned_private_file(source_state / "completion-email-claims.tsv", "completion claims ledger", 8_000_000).decode()
            matching_claims = [line for line in claims.splitlines() if line.split("\t", 1)[0] == plan.key]
            legacy_claim = f"{plan.key}\t{owner}\t{task.name}"
            detailed_claim = f"{legacy_claim}\t{plan.manager_target}\t{task_sha256}"
            current_claim = f"{detailed_claim}\t{plan.notice_key}"
            purpose_claim = f"{current_claim}\t{plan.notice_semantic_key}"
            if matching_claims not in ([legacy_claim], [detailed_claim], [current_claim], [purpose_claim]):
                raise OSError("completion receipt has missing or ambiguous claim evidence")
            if hashlib.sha256(task.read_bytes()).hexdigest() != task_sha256:
                raise OSError("task bytes changed during completion reconciliation")
            consume_dir.mkdir(mode=0o700, exist_ok=True)
            reconciled_dir.mkdir(mode=0o700, exist_ok=True)
            require_private_directory(consume_dir, "source reconciliation directory")
            require_private_directory(reconciled_dir, "target reconciliation directory")
            try:
                consumption = owned_private_file(consume, "completion reconciliation consumption", 32_768).decode()
            except FileNotFoundError:
                if destination.exists():
                    raise OSError("completion reconciliation destination is ambiguous")
                exclusive_record(consume, prepared)
                consumption = prepared
            if consumption == completed:
                raise OSError("completion receipt was already consumed")
            if consumption != prepared:
                raise OSError("completion receipt has an ambiguous prior consumption")
            try:
                destination_payload = owned_private_file(destination, "reconciled completion", 16_384).decode()
            except FileNotFoundError:
                exclusive_record(destination, record)
            else:
                if destination_payload != record:
                    raise OSError("completion reconciliation destination is ambiguous")
            replace_record(consume, prepared, completed)


def mark_completion_email_request_queued(plan: CompletionEmail) -> None:
    directory = completion_email_state_dir() / "completion-email-requests"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = directory / plan.key
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        _ = handle.write(f"{plan.target}\t{plan.task}\t{plan.subject}\n")
        handle.flush()
        os.fsync(handle.fileno())
    fsync_directory(directory)


def mark_completion_email_delivered(plan: CompletionEmail, receipt_key: str | None = None, receipt_target: str | None = None) -> None:
    if receipt_key is not None and SHA256_RE.fullmatch(receipt_key) is None:
        raise OSError("completion receipt key is malformed")
    receipt_payload = f"{receipt_target or plan.target}\t{plan.task.name}\t{plan.task_sha256}\n"
    if receipt_key is not None:
        existing_receipt = completion_email_state_dir() / "completion-email-delivered" / receipt_key
        if existing_receipt.is_file():
            recorded_receipt = owned_private_file(existing_receipt, "completion delivery", 4096).decode()
            legacy_receipt = f"{receipt_target or plan.target}\t{plan.task.name}\n"
            if recorded_receipt == legacy_receipt and has_exact_completion_claim(
                plan, receipt_key, receipt_target or plan.target
            ):
                receipt_payload = recorded_receipt
            elif recorded_receipt != receipt_payload:
                raise OSError("completion delivery does not match the task")
    notice_directory = completion_email_state_dir() / "completion-notice-delivered"
    notice_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_private_directory(notice_directory, "completion notice delivery directory")
    notice_marker = notice_directory / plan.notice_key
    notice_payload = f"{receipt_key or plan.key}\t{receipt_target or plan.target}\t{plan.task.name}\t{plan.task_sha256}\n"
    try:
        exclusive_record(notice_marker, notice_payload)
    except FileExistsError:
        recorded_notice = owned_private_file(notice_marker, "completion notice delivery", 4096).decode()
        try:
            recorded_key, recorded_target, recorded_task, recorded_task_sha256 = recorded_notice.rstrip("\n").split("\t")
        except ValueError as exc:
            raise OSError("completion notice delivery is malformed") from exc
        if (
            SHA256_RE.fullmatch(recorded_key) is None
            or canonical_tmux_target(recorded_target) != canonical_tmux_target(plan.target)
            or recorded_task != plan.task.name
            or recorded_task_sha256 != plan.task_sha256
        ):
            raise OSError("completion notice delivery does not match the task")
        claimed_receipt = claimed_completion_receipt(plan)
        claimed_key = claimed_receipt[0] if claimed_receipt is not None else None
        legacy_exact = receipt_key == plan.key == recorded_key and existing_receipt.is_file()
        if recorded_key != claimed_key and not legacy_exact:
            raise OSError("completion notice delivery does not match its atomic claim")
        receipt_payload = f"{recorded_target}\t{plan.task.name}\t{plan.task_sha256}\n"
    else:
        recorded_key = receipt_key or plan.key
    directory = completion_email_state_dir() / "completion-email-delivered"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_private_directory(directory, "completion delivery directory")
    marker = directory / recorded_key
    try:
        exclusive_record(marker, receipt_payload)
    except FileExistsError:
        if owned_private_file(marker, "completion delivery", 4096).decode() != receipt_payload:
            raise OSError("completion delivery does not match the task")


def completion_authorization_payload(plan: CompletionEmail) -> str:
    relative_task = plan.task.resolve().relative_to(plan.root.resolve()).as_posix()
    return (
        f"version=1\n"
        f"target={plan.target}\n"
        f"root={plan.root.resolve()}\n"
        f"task={relative_task}\n"
        f"task_sha256={plan.task_sha256}\n"
        f"notice_key={plan.notice_key}\n"
        f"semantic_key={plan.notice_semantic_key}\n"
        f"subject_sha256={hashlib.sha256(plan.subject.encode()).hexdigest()}\n"
        f"body_sha256={hashlib.sha256(plan.body.encode()).hexdigest()}\n"
    )


# 🧑 "Continue until each item is complete or cancelled."
def refresh_unattempted_completion_claim(plan: CompletionEmail, previous_key: str) -> None:
    """Replace one changed same-owner claim only when it provably never reached SMTP."""

    if SHA256_RE.fullmatch(previous_key) is None:
        raise ValueError("previous completion claim key must be a lowercase SHA-256 digest")
    if previous_key == plan.key:
        raise ValueError("previous completion claim has unchanged message identity")
    state_dir = completion_email_state_dir()
    authorization_dir = state_dir / "completion-email-authorizations"
    require_private_directory(state_dir, "completion state directory")
    require_private_directory(authorization_dir, "completion authorization directory")
    ledger = state_dir / "completion-email-claims.tsv"
    lock_path = state_dir / "completion-email-claims.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous = owned_private_file(ledger, "completion claims ledger", 8_000_000).decode()
        rows = [line.split("\t") for line in previous.splitlines()]
        if any(len(row) not in {3, 5, 6, 7} for row in rows):
            raise OSError("completion claims ledger is malformed")
        keyed = [row for row in rows if row[0] == previous_key]
        if len(keyed) != 1 or len(keyed[0]) != 7:
            raise OSError("previous completion claim is missing or ambiguous")
        old = keyed[0]
        old_notices = [row for row in rows if len(row) == 7 and row[5] == old[5]]
        old_semantic = [row for row in rows if len(row) == 7 and row[6] == old[6]]
        current_notices = [row for row in rows if len(row) == 7 and row[5] == plan.notice_key]
        current_semantic = [row for row in rows if len(row) == 7 and row[6] == plan.notice_semantic_key]
        same_notice = old[5] == plan.notice_key and old[6] == plan.notice_semantic_key
        if keyed != old_notices or keyed != old_semantic or (
            same_notice and (keyed != current_notices or keyed != current_semantic)
        ) or (not same_notice and (current_notices or current_semantic)):
            raise OSError("previous completion claim is missing or ambiguous")
        if (
            canonical_tmux_target(old[1]) != canonical_tmux_target(plan.target)
            or old[2] != plan.task.name
            or canonical_tmux_target(old[3]) != canonical_tmux_target(plan.manager_target)
            or SHA256_RE.fullmatch(old[4]) is None
        ):
            raise OSError("previous completion claim belongs to a different task or owner")
        old_authorization = authorization_dir / previous_key
        payload = owned_private_file(old_authorization, "previous completion authorization", 4096).decode()
        try:
            values = dict(line.split("=", 1) for line in payload.splitlines())
        except ValueError as exc:
            raise OSError("previous completion authorization is malformed") from exc
        expected_fields = {
            "version",
            "target",
            "root",
            "task",
            "task_sha256",
            "notice_key",
            "semantic_key",
            "subject_sha256",
            "body_sha256",
        }
        relative_task = plan.task.resolve().relative_to(plan.root.resolve()).as_posix()
        if (
            len(values) != len(payload.splitlines())
            or set(values) != expected_fields
            or values["version"] != "1"
            or canonical_tmux_target(values["target"]) != canonical_tmux_target(plan.target)
            or values["root"] != str(plan.root.resolve())
            or values["task"] != relative_task
            or values["task_sha256"] != old[4]
            or values["notice_key"] != old[5]
            or values["semantic_key"] != old[6]
            or SHA256_RE.fullmatch(values["subject_sha256"]) is None
            or SHA256_RE.fullmatch(values["body_sha256"]) is None
        ):
            raise OSError("previous completion authorization does not match its claim")
        current_payload = completion_authorization_payload(plan)
        current_values = dict(line.split("=", 1) for line in current_payload.splitlines())
        if not same_notice and (
            plan.outcome != "task done"
            or values["subject_sha256"] != current_values["subject_sha256"]
            or values["body_sha256"] != current_values["body_sha256"]
        ):
            raise OSError("previous completion claim belongs to a different semantic notice")
        forbidden = (
            state_dir / "completion-email-authorization-used" / previous_key,
            state_dir / "completion-email-delivered" / previous_key,
            state_dir / "completion-email-reconciled" / previous_key,
            state_dir / "completion-email-requests" / previous_key,
            state_dir / "completion-email-authorization-used" / plan.key,
            state_dir / "completion-email-delivered" / plan.key,
            state_dir / "completion-email-reconciled" / plan.key,
            state_dir / "completion-email-requests" / plan.key,
            state_dir / "completion-notice-delivered" / old[5],
            state_dir / "ordinary-completion-by-notice" / old[5],
            state_dir / "completion-notice-delivered" / plan.notice_key,
            state_dir / "ordinary-completion-by-notice" / plan.notice_key,
        )
        if any(path.exists() for path in forbidden):
            raise OSError("previous completion claim may have been used, delivered, reconciled, or queued")
        current_authorization = authorization_dir / plan.key
        try:
            recorded = owned_private_file(current_authorization, "current completion authorization", 4096).decode()
        except FileNotFoundError:
            exclusive_record(current_authorization, current_payload)
        else:
            if recorded != current_payload:
                raise OSError("current completion authorization is ambiguous")
        replacement = [plan.key, plan.target, plan.task.name, plan.manager_target, plan.task_sha256, plan.notice_key, plan.notice_semantic_key]
        updated_rows = [replacement if row == old else row for row in rows]
        updated = "".join("\t".join(row) + "\n" for row in updated_rows)
        temporary = ledger.with_name(f".{ledger.name}.{os.getpid()}.tmp")
        try:
            temporary_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
                _ = handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, ledger)
            fsync_directory(state_dir)
        finally:
            temporary.unlink(missing_ok=True)


def claim_completion_email(plan: CompletionEmail, *, recover_existing: bool = False) -> bool:
    """Prepare the exact capability before reserving its Human notice."""

    if not plan.send_allowed:
        raise OSError("Sent-Mail recovery plans cannot authorize email")
    state_dir = completion_email_state_dir()
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    ledger = state_dir / "completion-email-claims.tsv"
    authorization_directory = state_dir / "completion-email-authorizations"
    authorization_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_private_directory(authorization_directory, "completion email authorization directory")
    authorization = authorization_directory / plan.key
    authorization_payload = completion_authorization_payload(plan)
    lock_path = state_dir / "completion-email-claims.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        ordinary_marker = state_dir / "ordinary-completion-by-notice" / plan.notice_key
        if ordinary_marker.exists() and plan.outcome != "task done":
            if not ordinary_completion_is_reconciled(plan):
                raise OSError("ordinary completion reconciliation is invalid")
            return False
        try:
            previous = ledger.read_text(encoding="utf-8")
        except FileNotFoundError:
            previous = ""
        fields = [line.split("\t") for line in previous.splitlines()]
        if any(len(row) not in {3, 5, 6, 7} for row in fields):
            raise OSError("completion claims ledger is malformed")
        expected = [plan.key, plan.target, plan.task.name, plan.manager_target, plan.task_sha256, plan.notice_key, plan.notice_semantic_key]
        matching_keys = [row for row in fields if row[0] == plan.key]
        matching_notices = [row for row in fields if len(row) >= 6 and row[5] == plan.notice_key]
        matching_semantic_keys = [row for row in fields if len(row) == 7 and row[6] == plan.notice_semantic_key]
        if matching_keys or matching_notices or matching_semantic_keys:
            if matching_keys != [expected] or matching_notices != [expected] or matching_semantic_keys != [expected]:
                return False
            try:
                recorded = owned_private_file(authorization, "completion email authorization", 4096).decode()
            except FileNotFoundError:
                raise OSError("completion email claim has no authorization")
            if recorded != authorization_payload:
                raise OSError("completion email authorization is ambiguous")
            used = state_dir / "completion-email-authorization-used" / plan.key
            return recover_existing and not used.exists()
        try:
            exclusive_record(authorization, authorization_payload)
        except FileExistsError:
            if owned_private_file(authorization, "completion email authorization", 4096).decode() != authorization_payload:
                raise OSError("completion email authorization is ambiguous")
        temporary = ledger.with_name(f".{ledger.name}.{os.getpid()}.tmp")
        try:
            temporary_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
                _ = handle.write(
                    f"{previous}{plan.key}\t{plan.target}\t{plan.task.name}\t{plan.manager_target}\t{plan.task_sha256}\t{plan.notice_key}\t{plan.notice_semantic_key}\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, ledger)
            fsync_directory(state_dir)
        finally:
            temporary.unlink(missing_ok=True)
    return True


def send_completion_email(plan: CompletionEmail | None) -> bool:
    """Send a claimed message once; an uncertain outcome remains claimed."""

    if plan is None:
        return False
    if not plan.send_allowed:
        raise OSError("Sent-Mail recovery plans cannot send email")
    if not plan.semantic_key:
        raise ValueError("semantic completion key is required before email delivery")
    if completion_email_is_delivered(plan):
        return False
    try:
        require_completion_entrypoint()
        task_payload = stable_owned_file(plan.task, 8_000_000)
        if hashlib.sha256(task_payload).hexdigest() != plan.task_sha256:
            raise OSError("task bytes changed before delivery")
        if plan.contact_policy is not None:
            source_payload = stable_owned_file(plan.contact_policy.source, 8_000_000)
            if hashlib.sha256(task_payload).hexdigest() != plan.contact_policy.task_sha256 or (
                hashlib.sha256(source_payload).hexdigest() != plan.contact_policy.source_sha256
            ):
                raise OSError("contact-policy authority changed before delivery")
    except OSError as exc:
        print(f"automatic completion email blocked before claim: {exc}", file=sys.stderr)
        return False
    if not claim_completion_email(plan, recover_existing=True):
        return False
    with tempfile.TemporaryDirectory(prefix="omo-completion-email-") as tmp:
        body = Path(tmp) / "body.txt"
        body.write_text(plan.body, encoding="utf-8")
        command = [str(EMAIL_HELPER), "--manager-human", "--completion-authorization", plan.key]
        if plan.subject:
            subject = Path(tmp) / "subject.txt"
            subject.write_text(plan.subject + "\n", encoding="utf-8")
            command.extend(("--subject-file", str(subject)))
        command.extend(("--message-file", str(body)))
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"automatic completion email failed or is uncertain; replay suppressed: {exc}", file=sys.stderr)
            return False
    mark_completion_email_delivered(plan)
    return True


def require_owner_completion(
    root: Path,
    task: Path,
    text: str,
    outcome: str,
    *,
    items: tuple[str, ...] = (),
    evidence: str = "",
    human_subject: str = "",
    human_body: str = "",
    owner_may_mutate_after_delivery: bool = False,
    semantic_key: str = "",
    pending_item_owner: bool = False,
) -> bool:
    """Require exact-owner delivery before a manager-driven mutation proceeds."""

    if not semantic_key:
        raise ValueError("semantic completion key is required")
    canonical = build_completion_email(root, task, text, outcome, items=items, evidence=evidence, semantic_key=semantic_key)
    if canonical is None:
        return True
    owner_plan = plan_completion_email(
        root,
        task,
        text,
        outcome,
        items=items,
        evidence=evidence,
        human_subject=human_subject,
        human_body=human_body,
        semantic_key=canonical.semantic_key if canonical is not None else semantic_key,
        pending_item_owner=pending_item_owner,
    )
    effective = owner_plan or canonical
    require_completion_entrypoint()
    if completion_email_is_delivered(effective):
        return True
    if owner_plan is not None:
        if not send_completion_email(owner_plan):
            raise OSError("responsible-owner completion email was not confirmed delivered")
        return owner_may_mutate_after_delivery
    if completion_email_request_is_queued(canonical):
        return False
    command = [str(COMPLETION_ENTRYPOINT), "--root", str(root), "--task", str(task), "--outcome", outcome]
    command.extend(("--semantic-key", canonical.semantic_key))
    for item in items:
        command.extend(("--item", item))
    if evidence:
        command.extend(("--evidence", evidence))
    message = (
        "Before this mutation can complete, send its single owner-authenticated completion notice. "
        "Run this exact command, then report completion to your manager:\n"
        f"{shlex.join(command)}"
    )
    from omo_manager.omo_tmux_send import send_system_to_codex

    send_system_to_codex(canonical.target, message)
    mark_completion_email_request_queued(canonical)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one owner-authenticated completion notice.")
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task", type=Path, required=True)
    _ = parser.add_argument("--outcome", required=True)
    _ = parser.add_argument("--item", action="append", default=[])
    _ = parser.add_argument("--evidence", default="")
    _ = parser.add_argument("--semantic-key", default="", help="Exact shared SHA-256 identity for this semantic Human completion notice.")
    _ = parser.add_argument(
        "--reconcile-delivered",
        action="store_true",
        help="Consume one exact delivered receipt from another owner-private completion state.",
    )
    _ = parser.add_argument(
        "--reconcile-ordinary-sent",
        action="store_true",
        help="Bind one exact ordinary agent-to-Human Sent-Mail message to this completion without sending again.",
    )
    _ = parser.add_argument("--owner", default="", help="Exact task owner required by --reconcile-delivered.")
    _ = parser.add_argument("--task-sha256", default="", help="Exact current task digest required by --reconcile-delivered.")
    _ = parser.add_argument("--receipt", type=Path, help="Exact delivered marker required by --reconcile-delivered.")
    _ = parser.add_argument("--receipt-sha256", default="", help="Exact delivered-marker digest required by --reconcile-delivered.")
    _ = parser.add_argument("--message-id", default="", help="Exact RFC Message-ID required by --reconcile-ordinary-sent.")
    _ = parser.add_argument("--sent-subject-sha256", default="", help="Exact decoded Sent-Mail subject digest.")
    _ = parser.add_argument("--sent-body-sha256", default="", help="Exact decoded plain-text Sent-Mail body digest.")
    _ = parser.add_argument("--answer-subject-file", type=Path, help="One-line subject for a combined Human answer.")
    _ = parser.add_argument("--answer-message-file", type=Path, help="Body for a combined Human answer.")
    _ = parser.add_argument(
        "--refresh-unattempted-claim",
        default="",
        help="Exact prior claim key to replace after task bytes changed and proof its authorization never reached SMTP.",
    )
    parsed = parser.parse_args(argv)
    root = parsed.root.resolve()
    task = parsed.task if parsed.task.is_absolute() else root / parsed.task
    try:
        reconciliation_values = (parsed.owner, parsed.task_sha256, parsed.receipt, parsed.receipt_sha256)
        answer_values = (parsed.answer_subject_file, parsed.answer_message_file)
        if bool(answer_values[0]) != bool(answer_values[1]):
            parser.error("a combined Human answer requires both answer files.")
        if not parsed.semantic_key:
            parser.error("--semantic-key is required for a completion notice.")
        if parsed.reconcile_delivered and parsed.reconcile_ordinary_sent:
            parser.error("completion reconciliation modes are mutually exclusive.")
        if (parsed.reconcile_delivered or parsed.reconcile_ordinary_sent) and (any(answer_values) or parsed.refresh_unattempted_claim):
            parser.error("claim refresh and Human answer options cannot be combined with reconciliation.")
        ordinary_values = (parsed.message_id, parsed.sent_subject_sha256, parsed.sent_body_sha256)
        if parsed.reconcile_delivered:
            if not all(reconciliation_values):
                parser.error("--reconcile-delivered requires owner, task digest, receipt, and receipt digest.")
            if any(ordinary_values):
                parser.error("ordinary Sent-Mail evidence requires --reconcile-ordinary-sent.")
            reconcile_delivered_completion(
                root,
                task,
                parsed.outcome,
                parsed.owner,
                parsed.task_sha256,
                parsed.receipt,
                parsed.receipt_sha256,
                items=tuple(parsed.item),
                evidence=parsed.evidence,
                semantic_key=parsed.semantic_key,
            )
            print(f"Reconciled delivered completion receipt for {task.name} into {completion_email_state_dir()}.")
            return 0
        if parsed.reconcile_ordinary_sent:
            if not all(ordinary_values):
                parser.error("--reconcile-ordinary-sent requires Message-ID, subject digest, and body digest.")
            if any(reconciliation_values):
                parser.error("delivery receipt options require --reconcile-delivered.")
            reconcile_ordinary_sent_completion(
                root,
                task,
                parsed.outcome,
                parsed.message_id,
                parsed.sent_subject_sha256,
                parsed.sent_body_sha256,
                items=tuple(parsed.item),
                evidence=parsed.evidence,
                semantic_key=parsed.semantic_key,
            )
            print(f"Reconciled ordinary Sent-Mail completion for {task.name} without sending email.")
            return 0
        if any(reconciliation_values):
            parser.error("owner, task digest, and receipt options require --reconcile-delivered.")
        if any(ordinary_values):
            parser.error("Message-ID and Sent-Mail digests require --reconcile-ordinary-sent.")
        answer_subject = parsed.answer_subject_file.read_text(encoding="utf-8").rstrip("\n") if parsed.answer_subject_file else ""
        answer_body = parsed.answer_message_file.read_text(encoding="utf-8") if parsed.answer_message_file else ""
        text = task.read_text(encoding="utf-8")
        plan = plan_completion_email(
            root,
            task,
            text,
            parsed.outcome,
            items=tuple(parsed.item),
            evidence=parsed.evidence,
            human_subject=answer_subject,
            human_body=answer_body,
            semantic_key=parsed.semantic_key,
        )
    except (OSError, TaskFrontmatterError, ValueError) as exc:
        print(f"omo_completion_email.py: {exc}", file=sys.stderr)
        return 2
    if plan is None:
        print("omo_completion_email.py: caller is not the exact owner or reporting is suppressed", file=sys.stderr)
        return 2
    if parsed.refresh_unattempted_claim:
        try:
            refresh_unattempted_completion_claim(plan, parsed.refresh_unattempted_claim)
        except (OSError, ValueError) as exc:
            print(f"omo_completion_email.py: {exc}", file=sys.stderr)
            return 2
    if completion_email_is_delivered(plan):
        return 0
    return 0 if send_completion_email(plan) else 2


if __name__ == "__main__":
    raise SystemExit(main())
