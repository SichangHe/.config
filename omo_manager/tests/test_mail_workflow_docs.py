from __future__ import annotations

import unittest
from pathlib import Path


DOCS = Path(__file__).parents[1] / "docs" / "mail"


class MailWorkflowDocumentationTests(unittest.TestCase):
    def test_cleanup_redirects_to_canonical_location(self) -> None:
        cleanup = (DOCS / "cleanup.md").read_text()

        self.assertIn("This document has moved to its canonical location", cleanup)
        self.assertIn("https://github.com/SichangHe/personal_browser_setup/blob/main/docs/mail_compression/cleanup.md", cleanup)

    def test_compression_redirects_to_canonical_location(self) -> None:
        compression = (DOCS / "compression.md").read_text()

        self.assertIn("This document has moved to its canonical location", compression)
        self.assertIn("https://github.com/SichangHe/personal_browser_setup/blob/main/docs/mail_compression/compression.md", compression)

    def test_index_names_compression_as_the_canonical_execution_policy(self) -> None:
        index = (DOCS / "index.md").read_text()

        self.assertIn("canonical task-level compression procedure", index)
        self.assertIn("redirect to canonical location", index)
        self.assertIn("https://github.com/SichangHe/personal_browser_setup/blob/main/docs/mail_compression", index)

if __name__ == "__main__":
    unittest.main()
