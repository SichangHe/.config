from __future__ import annotations

import unittest
from pathlib import Path


DOCS = Path(__file__).parents[1] / "docs" / "mail"


class MailWorkflowDocumentationTests(unittest.TestCase):
    def test_cleanup_preserves_scope_and_decision_guards(self) -> None:
        cleanup = (DOCS / "cleanup.md").read_text()

        for rule in (
            "Use [compression.md](compression.md) for every manager-human cleanup run",
            "Do not substitute rolling boundaries, repeated scans, Gmail labels, or a big-bang mailbox mutation",
            "thresholds and body length trigger review only; neither makes mail eligible for Trash",
            "completed, resolved, or `previous` evidence overrides stale active-looking status",
            "generic filename or path list is not task evidence",
            "complete exported context newest-to-oldest",
            "ask a focused human question only when a material disposition remains uncertain",
            "send and verify any replacement before Trash",
            "move only independently reviewed fixed-start UIDs",
            "never expunge or permanently delete",
            "additive later identities remain outside the run",
            "preserve immutable receipts and source evidence",
        ):
            self.assertIn(rule, cleanup)

    def test_compression_is_the_terminating_fixed_start_workflow(self) -> None:
        compression = (DOCS / "compression.md").read_text()

        for rule in (
            "one immutable fixed-start source set",
            "A threshold may start review",
            "run's only candidate discovery",
            "later arrivals are outside the run",
            "never rerun discovery because of them",
            "`OMO_HUMAN_EMAIL_CONFIG_PATH`",
            "deterministic disjoint thread batches",
            "reviewer distinct from the batch owner",
            "digest is bound into the immutable intent and outcome",
            "authoritative task/TODO evidence",
            "keep at most one useful manager email per task",
            "audit metadata, never retention or Trash criteria",
            "send and record it before Trash",
            "only the reviewed fixed-start UIDs",
            "additive later thread identities are allowed but never moved",
            "immutable intent before mutation and an immutable outcome only after exact Trash verification",
            "fails closed for that thread",
            "this read-only path must prove every frozen source",
            "interrupted partial move only by rerunning `trash-superseded` with the identical intent inputs",
            "moves only the intact `INBOX` remainder",
            "every fixed-start source to have exactly one verified retained or Trash disposition",
            "no repeated candidate scan or full live mailbox scan",
            "never expunge, permanently delete, or mutate `\\All`",
            "report concisely",
        ):
            self.assertIn(rule, compression)

    def test_index_names_compression_as_the_canonical_execution_policy(self) -> None:
        index = (DOCS / "index.md").read_text()

        self.assertIn("canonical fixed-start execution and recovery procedure for every manager-human cleanup run", index)
        self.assertNotIn("compress only fully superseded manager-sent mail", index)


if __name__ == "__main__":
    unittest.main()
