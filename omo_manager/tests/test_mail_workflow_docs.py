from __future__ import annotations

import unittest
from pathlib import Path


DOCS = Path(__file__).parents[1] / "docs" / "mail"


class MailWorkflowDocumentationTests(unittest.TestCase):
    def test_cleanup_delegates_to_compression(self) -> None:
        cleanup = (DOCS / "cleanup.md").read_text()

        self.assertEqual(
            cleanup,
            """# manager-human mail cleanup

Use `compression.md` for every manager-human Inbox cleanup. Its current-view preparation, human-facing consolidation, independent review, replacement verification, recoverable Trash, drift handling, protected reports, count target, and final verification are the complete procedure.
""",
        )

    def test_compression_is_the_reviewed_live_mailbox_workflow(self) -> None:
        compression = (DOCS / "compression.md").read_text()

        for rule in (
            "smallest truthful human-readable set; aim near 20",
            "configured manager-mail boundary as the count denominator",
            "leave no more than 30 total accepted manager-sent Inbox messages",
            "represent each actual task with one concise, self-contained overview",
            "retain each distinct current question or decision",
            "retain protected recurring reports separately from task overviews",
            "order instances by numeric Gmail message identity",
            "PB news, PB stock watch, and PB urgent mail are excluded",
            "Treat a count threshold only as a reason to review mail.",
            "Add every other relevant arrival to a newly frozen explicit source set",
            "Group messages only when they belong to one authoritative task identity",
            "When one source covers several tasks, bind it to every covered task",
            "A question is independently actionable",
            "give a distinct reviewer the current read-only view",
            "Use the task's documented current owner",
            "A missing, conflicting, or inferred-only target blocks sending and movement",
            "Do not send a duplicate while delivery lookup is pending.",
            "Any source identity change requires a new frozen source set",
            "move the stale overview only when it is explicitly bound and fully superseded",
            "Move only explicitly bound, fully superseded sources to `[Gmail]/Trash`.",
            "protected recurring reports accepted by the manager-mail boundary remain present",
            "Do not mark messages read as cleanup.",
            "Do not expunge, permanently delete, mutate Gmail All Mail, or move unreviewed mail.",
        ):
            self.assertIn(rule, compression)

    def test_index_names_compression_as_the_canonical_execution_policy(self) -> None:
        index = (DOCS / "index.md").read_text()

        self.assertIn("canonical task-level compression procedure", index)
        self.assertIn("points all cleanup runs to `compression.md`", index)

if __name__ == "__main__":
    unittest.main()
