from __future__ import annotations

import unittest
from pathlib import Path


DOCS = Path(__file__).parents[1] / "docs" / "mail"


class MailWorkflowDocumentationTests(unittest.TestCase):
    def test_cleanup_preserves_decision_and_recovery_guards(self) -> None:
        cleanup = (DOCS / "cleanup.md").read_text()

        for rule in (
            "freeze exact configured addresses, mutation-candidate mailboxes, inclusive start, and exclusive end before candidate discovery",
            "thresholds and body length trigger review only; neither makes mail eligible for Trash",
            "`previous`, completed, or resolved evidence overrides a stale active-looking status",
            "generic filename or path list is not task evidence by itself",
            "live-pending, or unresolved human-decision work unless explicit authoritative closure permits removal",
            "complete private context newest-to-oldest before deciding",
            "resolve each candidate Gmail thread through `\\All` and preserve a thread with any out-of-scope message or time context",
            "ask focused human questions only when a material disposition is genuinely uncertain",
            "private immutable source map before mutation: mailbox-scoped UID, message and thread identity, source labels, task evidence, reason, and disposition",
            "external drift separately instead of attributing it to cleanup",
            "ordered operation plan, then fsync a paired intent and outcome receipt for every mutation",
            "not only aggregate counts",
            "recovery evidence sufficient to restore each moved message",
            "finish with live verification",
            "never expunge or permanently delete it",
            "use `\\Sent` and `\\All` as read-only context unless authoritative human text names them as mutation sources",
        ):
            self.assertIn(rule, cleanup)

    def test_compression_requires_replacement_before_trash(self) -> None:
        compression = (DOCS / "compression.md").read_text()

        for rule in (
            "Follow [cleanup.md](cleanup.md)'s source-boundary, task-authority, evidence, recovery, and final-verification safeguards first",
            "A threshold starts this review",
            "do not apply to a compression source that its recorded replacement fully supersedes",
            "fully replaceable topics",
            "retain an unread report and any flagged, saved, read-later, full-read, or uncertain memo or report",
            "record their delivery before listing superseded UIDs",
            "explicit UID list",
            "never expunge",
        ):
            self.assertIn(rule, compression)


if __name__ == "__main__":
    unittest.main()
