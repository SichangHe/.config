from __future__ import annotations

import unittest
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime

from omo_manager.email_idle_watcher import manager_mail_counts


class EmailCleanupExclusionTests(unittest.TestCase):
    def test_pb_subjects_do_not_contribute_to_cleanup_counts(self) -> None:
        now = datetime.now().astimezone()
        messages = {
            "1": "ordinary update",
            "2": "PB newsletter",
            "3": "Re: PB news setup",
            "4": "[a] PB stock watch: NVDA",
            "5": "Re: [omo_manager] [wl:9] PB urgent",
        }

        class Client:
            def __init__(self) -> None:
                self.fetches: list[str] = []
                self.searches: list[tuple[object, ...]] = []

            def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
                if command == "search":
                    self.searches.append(args)
                    return "OK", [b"1 2 3 4 5"]
                if command == "fetch":
                    uid = str(args[0])
                    self.fetches.append(uid)
                    msg = EmailMessage()
                    msg["From"] = "Agent <agent@example.test>"
                    msg["To"] = "Human <human@example.test>"
                    msg["Subject"] = messages[uid]
                    msg["Date"] = format_datetime(now)
                    return "OK", [(b"HEADER", msg.as_bytes())]
                raise AssertionError(command)

        client = Client()
        counts = manager_mail_counts(
            client,  # type: ignore[arg-type]
            "agent@example.test",
            24 * 60 * 60,
            64,
            now,
            recipient_email="human@example.test",
            require_subject_tags=False,
        )

        self.assertEqual(2, counts.total)
        self.assertEqual(2, counts.unread)
        self.assertEqual(2, counts.recent_total)
        self.assertEqual(["1", "2", "3", "4", "5"], client.fetches)
        self.assertEqual(3, len(client.searches))
        for search in client.searches:
            self.assertNotIn("NOT", search)
            self.assertFalse(any(isinstance(value, str) and value.startswith('"PB ') for value in search))


if __name__ == "__main__":
    unittest.main()
