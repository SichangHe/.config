import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path

from omo_manager.omo_codex_cost import Usage, aggregate, load_prices, main, report_json, snapshots_from_file


class CodexCostTests(unittest.TestCase):
    def test_aggregates_cumulative_snapshots_without_duplicate_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            records = [
                {"timestamp": "2026-06-30T00:00:00Z", "type": "session_meta", "payload": {"id": "s1", "session_id": "parent-thread", "model_provider": "openai", "model": "gpt-5.6-terra"}},
                {
                    "timestamp": "2026-06-30T00:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 200, "reasoning_output_tokens": 50},
                            "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 200, "reasoning_output_tokens": 50},
                        },
                    },
                },
                {"timestamp": "2026-06-30T00:00:02Z", "type": "turn_context", "payload": {"turn_id": "turn-2", "model": "gpt-5.6-terra"}},
                {
                    "timestamp": "2026-06-30T00:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 200, "reasoning_output_tokens": 50},
                            "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 200, "reasoning_output_tokens": 50},
                        },
                    },
                },
                {
                    "timestamp": "2026-06-30T00:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"input_tokens": 1700, "cached_input_tokens": 300, "output_tokens": 250, "reasoning_output_tokens": 70},
                            "last_token_usage": {"input_tokens": 700, "cached_input_tokens": 200, "output_tokens": 50, "reasoning_output_tokens": 20},
                        },
                    },
                },
            ]
            _ = path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            totals = aggregate(snapshots_from_file(path, "openai", ""))
            usage = totals[("openai", "gpt-5.6-terra")]
            self.assertEqual(1700, usage.input_tokens)
            self.assertEqual(300, usage.cached_input_tokens)
            self.assertEqual(250, usage.output_tokens)
            self.assertEqual(70, usage.reasoning_output_tokens)

    def test_session_meta_id_keeps_child_sessions_separate_from_parent_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.jsonl"
            second = Path(tmp) / "second.jsonl"
            _ = first.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        {"timestamp": "2026-06-30T00:00:00Z", "type": "session_meta", "payload": {"id": "child-1", "session_id": "parent", "model_provider": "openai", "model": "gpt-5.6-terra"}},
                        {"timestamp": "2026-06-30T00:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10, "reasoning_output_tokens": 1}}}},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            _ = second.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        {"timestamp": "2026-06-30T00:00:00Z", "type": "session_meta", "payload": {"id": "child-2", "session_id": "parent", "model_provider": "openai", "model": "gpt-5.6-terra"}},
                        {"timestamp": "2026-06-30T00:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 200, "cached_input_tokens": 0, "output_tokens": 20, "reasoning_output_tokens": 2}}}},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            totals = aggregate([*snapshots_from_file(first, "openai", ""), *snapshots_from_file(second, "openai", "")])
            self.assertEqual(300, totals[("openai", "gpt-5.6-terra")].input_tokens)

    def test_mixed_counter_decrease_adds_only_positive_field_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            records = [
                {"timestamp": "2026-06-30T00:00:00Z", "type": "session_meta", "payload": {"id": "s1", "model_provider": "openai", "model": "gpt-5.6-terra"}},
                {"timestamp": "2026-06-30T00:00:01Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 50, "reasoning_output_tokens": 10}}}},
                {"timestamp": "2026-06-30T00:00:02Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 150, "cached_input_tokens": 30, "output_tokens": 40, "reasoning_output_tokens": 8}}}},
            ]
            _ = path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
            usage = aggregate(snapshots_from_file(path, "openai", ""))[("openai", "gpt-5.6-terra")]
            self.assertEqual(150, usage.input_tokens)
            self.assertEqual(30, usage.cached_input_tokens)
            self.assertEqual(50, usage.output_tokens)
            self.assertEqual(10, usage.reasoning_output_tokens)

    def test_human_example_price_case(self) -> None:
        usage_report = report_json({("openai", "gpt-5.6-terra"): Usage(input_tokens=2_000_000, cached_input_tokens=1_000_000, output_tokens=1_000_000)}, load_prices(None), date(2026, 7, 12))
        self.assertEqual("17.750000", usage_report["total_cost_usd"])
        self.assertEqual("17.750000", usage_report["rows"][0]["cost_usd"])

    def test_historical_gpt_55_price_still_available(self) -> None:
        usage_report = report_json({("openai", "gpt-5.5"): Usage(input_tokens=2_000_000, cached_input_tokens=1_000_000, output_tokens=1_000_000)}, load_prices(None), date(2026, 6, 30))
        self.assertEqual("35.500000", usage_report["total_cost_usd"])
        self.assertEqual("35.500000", usage_report["rows"][0]["cost_usd"])

    def test_cost_uses_gpt_56_terra_default_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            record = {
                "timestamp": "2026-06-30T00:00:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 2_000_000, "cached_input_tokens": 1_000_000, "output_tokens": 1_000_000, "reasoning_output_tokens": 100}},
                },
            }
            _ = path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            out = StringIO()
            with redirect_stdout(out):
                status = main(["--model", "gpt-5.6-terra", "--price-date", "2026-07-12", str(path)])
            self.assertEqual(0, status)
            self.assertIn("total_cost_usd: 17.750000", out.getvalue())
