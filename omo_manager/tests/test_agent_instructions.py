from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_agent_instructions as instructions


class AgentInstructionsTests(unittest.TestCase):
    def test_worker_transcript_shows_command_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "getagentsmd"
            command.write_text("#!/bin/sh\nprintf 'root instructions\\n'\n", encoding="utf-8")
            command.chmod(0o700)
            with patch.object(instructions, "GETAGENTSMD", command):
                result = instructions.launch_instructions()
        self.assertEqual(f"$ {command}\nroot instructions\n".encode(), result)

    def test_manager_transcript_loads_common_and_role_documents_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "getagentsmd"
            command.write_text("#!/bin/sh\nprintf '%s\\n' \"${*:-root}\"\n", encoding="utf-8")
            command.chmod(0o700)
            with patch.object(instructions, "GETAGENTSMD", command):
                result = instructions.launch_instructions("submanager").decode()
        self.assertEqual(
            f"$ {command}\nroot\n\n$ {command} get agent_manager\nget agent_manager\n\n$ {command} get submanager\nget submanager\n",
            result,
        )

    def test_failure_and_empty_output_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "getagentsmd"
            command.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            command.chmod(0o700)
            with patch.object(instructions, "GETAGENTSMD", command), self.assertRaisesRegex(instructions.AgentInstructionsError, "exited 7"):
                instructions.launch_instructions()

            command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            with patch.object(instructions, "GETAGENTSMD", command), self.assertRaisesRegex(instructions.AgentInstructionsError, "produced no agent instructions"):
                instructions.launch_instructions()

    def test_unknown_manager_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown manager role"):
            instructions.launch_instructions("manager")


if __name__ == "__main__":
    unittest.main()
