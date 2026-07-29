from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_vl_experiment_preflight import check_vlh


class VlExperimentPreflightTests(unittest.TestCase):
    def test_vlh_help_cannot_write_to_callers_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            caller = root / "caller"
            caller.mkdir()
            vlh = root / "vlh"
            vlh.write_text("#!/bin/sh\n: > cwd-written\n", encoding="utf-8")
            vlh.chmod(0o700)

            with patch.dict(os.environ, {"PATH": str(root)}), contextlib.chdir(caller):
                result = check_vlh(vlh)

            self.assertFalse((caller / "cwd-written").exists())
            self.assertIn("help_state=fresh_scratch_removed", result.details)


if __name__ == "__main__":
    unittest.main()
