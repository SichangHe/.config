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
            "private immutable source map before mutation: mailbox-scoped UID, message and thread identity, content digest, task evidence, reason, and disposition",
            "external drift separately instead of attributing it to cleanup",
            "ordered operation plan, then fsync a paired intent and outcome receipt for every mutation",
            "not only aggregate counts",
            "recovery evidence sufficient to locate each moved message",
            "finish with live verification",
            "never expunge or permanently delete it",
            "use `\\Sent` and `\\All` as read-only context unless authoritative human text names them as mutation sources",
        ):
            self.assertIn(rule, cleanup)

    def test_compression_requires_replacement_before_trash(self) -> None:
        compression = (DOCS / "compression.md").read_text()

        for rule in (
            "fixed-start source set",
            "A threshold starts review",
            "run's only candidate discovery",
            "optional diagnostics before the run",
            "later arrivals are outside the run",
            "`OMO_HUMAN_EMAIL_CONFIG_PATH`",
            "deterministic batches",
            "claims are exclusive",
            "move across batches",
            "locate corresponding task records",
            "ignore Gmail state signals",
            "signal-only drift never blocks progress or changes disposition",
            "move only irrelevant intermediate fixed-start messages",
            "send and record it before Trash",
            "writes an outcome only after immediate Trash verification",
            "fails closed for that thread",
            "every fixed-start source to be classified exactly once",
            "no full live candidate scan",
            "one claimed thread per mutation",
            "never expunge or permanently delete it",
        ):
            self.assertIn(rule, compression)


if __name__ == "__main__":
    unittest.main()
