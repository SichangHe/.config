from __future__ import annotations

import unittest
from pathlib import Path


DOCS = Path(__file__).parents[1] / "docs" / "mail"


class MailWorkflowDocumentationTests(unittest.TestCase):
    def test_cleanup_preserves_scope_and_decision_guards(self) -> None:
        cleanup = (DOCS / "cleanup.md").read_text()

        for rule in (
            "Use [compression.md](compression.md) for every manager-human cleanup run",
            "does not require an evidence directory or persisted evidence artifacts",
            "thresholds and body length trigger review only; neither makes mail eligible for Trash",
            "completed, resolved, or `previous` evidence overrides stale active-looking status",
            "generic filename or path list is not task evidence",
            "complete current context, including its reported date and From/To direction",
            "independently review the proposed task/source grouping and disposition",
            "ask a focused human question only when a material grouping or disposition remains uncertain",
            "send and verify exactly one self-contained replacement per task when needed before Trash",
            "explicitly selected, independently reviewed, superseded sources",
            "exactly one useful, self-contained manager email",
            "never expunge or permanently delete",
            "always use `\\All` as read-only context and never mutate it",
        ):
            self.assertIn(rule, cleanup)

        for conflicting_rule in ("fixed-start", "owner-only evidence directory", "persisted evidence artifacts are required"):
            self.assertNotIn(conflicting_rule, cleanup)

    def test_compression_is_the_reviewed_live_mailbox_workflow(self) -> None:
        compression = (DOCS / "compression.md").read_text()

        for rule in (
            "Manager-mail compression does not require an evidence directory or persisted evidence artifacts.",
            "Select explicit source messages from a current read-only mailbox view",
            "independently review the proposed task grouping",
            "send and verify one self-contained replacement per task when needed",
            "then move only superseded sources to recoverable Gmail Trash",
            "Never expunge or permanently delete.",
            "A threshold may start review",
            "later arrivals and unselected messages remain outside the run",
            "`OMO_HUMAN_EMAIL_CONFIG_PATH`",
            "reviewer distinct from the preparer",
            "authoritative task/TODO state",
            "every task must end with exactly one useful, self-contained manager email",
            "audit metadata, never retention or Trash criteria",
            "send exactly one self-contained replacement for the task",
            "verify that exact message is uniquely present",
            "only the explicitly selected, independently reviewed sources",
            "current read-only mailbox view to recheck",
            "`selected_source_sender_tmux_target=`",
            "`email_me.py --sender-tmux-target ORIGINAL_TARGET`",
            "plus `--subject-file` and `--message-file` as needed",
            "without defaulting to the compression worker's target",
            "blocks Trash unless every replacement preserves its task's target through the final mutation gate",
            "distinct reviewer confirm both each exact task identity and its one authoritative current target",
            "`--route-resolution 'TASK-ID=TARGET'` exactly once for every task",
            "explicit reviewer-controlled override of historical subject targets",
            "never the compression worker's default target",
            "match the resolved target at both the initial and final mutation gates",
            "never expunge, permanently delete, or mutate `\\All`",
            "report concisely",
        ):
            self.assertIn(rule, compression)

        for conflicting_rule in (
            "fixed-start",
            "owner-only evidence directory",
            "manifest.tsv",
            "task-evidence file",
            "immutable intent",
            "immutable outcome",
            "email_me.py --manager-human",
        ):
            self.assertNotIn(conflicting_rule, compression)

    def test_index_names_compression_as_the_canonical_execution_policy(self) -> None:
        index = (DOCS / "index.md").read_text()

        self.assertIn("canonical no-persisted-evidence, independently reviewed execution procedure", index)
        self.assertNotIn("fixed-start", index)

if __name__ == "__main__":
    unittest.main()
