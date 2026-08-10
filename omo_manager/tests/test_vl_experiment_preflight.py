from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_vl_experiment_preflight import check_midas_lex


class VlExperimentPreflightTests(unittest.TestCase):
    def test_midas_lex_help_cannot_write_to_callers_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            caller = root / "caller"
            caller.mkdir()
            midas_lex = root / "midas-lex"
            _ = midas_lex.write_text('#!/bin/sh\n[ "$1" = help ] || exit 64\n: > cwd-written\n', encoding="utf-8")
            midas_lex.chmod(0o700)

            with patch.dict(os.environ, {"PATH": str(root)}), contextlib.chdir(caller):
                result = check_midas_lex(midas_lex)

            self.assertFalse((caller / "cwd-written").exists())
            self.assertEqual("midas-lex", result.label)
            self.assertIn("help_state=fresh_scratch_removed", result.details)

    def test_relative_path_entry_survives_scratch_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            midas_lex = binary_dir / "midas-lex"
            _ = midas_lex.write_text('#!/bin/sh\n[ "$1" = help ]\n', encoding="utf-8")
            midas_lex.chmod(0o700)

            with patch.dict(os.environ, {"PATH": "bin"}), contextlib.chdir(root):
                result = check_midas_lex(midas_lex)

            self.assertEqual("midas-lex", result.label)

    def test_legacy_name_symlink_cannot_satisfy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current_bin = root / "current-bin"
            current_bin.mkdir()
            midas_lex = current_bin / "midas-lex"
            _ = midas_lex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            midas_lex.chmod(0o700)
            legacy_bin = root / "legacy-bin"
            legacy_bin.mkdir()
            (legacy_bin / "vlh").symlink_to(midas_lex)

            with patch.dict(os.environ, {"PATH": str(legacy_bin)}):
                with self.assertRaisesRegex(ValueError, "`midas-lex` is not on PATH"):
                    _ = check_midas_lex(midas_lex)


if __name__ == "__main__":
    unittest.main()
