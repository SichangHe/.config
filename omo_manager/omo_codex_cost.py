#!/usr/bin/env python3
"""Estimate Codex raw session token cost from JSONL token_count events."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
DEFAULT_PRICE_TABLE = {
    "source": "https://openai.com/api/pricing/",
    "source_accessed": "2026-06-30",
    "unit": "usd_per_1m_tokens",
    "notes": [
        "OpenAI pricing page read via browser tool on 2026-06-30; local urllib received HTTP 403.",
        "Page lists GPT-5.5 with Input $5.00, Cached input $0.50, Output $30.00 per 1M tokens.",
        "Reasoning output tokens are output tokens in Codex token_count totals; the table does not price them separately.",
    ],
    "prices": [
        {
            "provider": "openai",
            "model": "gpt-5.5",
            "effective_date": "2026-06-30",
            "input_tokens": "5.00",
            "cached_input_tokens": "0.50",
            "output_tokens": "30.00",
            "reasoning_output_tokens": "0",
        },
    ],
}


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_output_tokens + other.reasoning_output_tokens,
        )

    def delta_from(self, previous: Usage) -> Usage | None:
        values = {key: getattr(self, key) - getattr(previous, key) for key in TOKEN_KEYS}
        if all(value == 0 for value in values.values()):
            return None
        if all(value >= 0 for value in values.values()):
            return Usage(**values)
        if all(value <= 0 for value in values.values()):
            return self
        return Usage(**{key: max(0, value) for key, value in values.items()})


@dataclass(frozen=True)
class Snapshot:
    timestamp: str
    path: Path
    line_no: int
    session_key: str
    provider: str
    model: str
    usage: Usage


@dataclass(frozen=True)
class Price:
    provider: str
    model: str
    effective_date: date
    input_per_1m: Decimal
    cached_input_per_1m: Decimal
    output_per_1m: Decimal


@dataclass(frozen=True)
class Args:
    jsonl_paths: tuple[Path, ...]
    provider: str
    model: str
    price_date: date
    prices_path: Path | None
    dump_default_prices: bool
    json_output: bool


class ParsedArgs(argparse.Namespace):
    jsonl_paths: list[Path]
    provider: str = "openai"
    model: str = ""
    price_date: str = ""
    prices: Path | None = None
    dump_default_prices: bool = False
    json: bool = False


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("jsonl_paths", nargs="*", type=Path, help="Codex raw session JSONL files.")
    _ = parser.add_argument("--provider", default="openai", help="Pricing provider key.")
    _ = parser.add_argument("--model", default="", help="Override missing or changing model names.")
    _ = parser.add_argument("--price-date", default="", help="Use the latest price effective on or before this YYYY-MM-DD date.")
    _ = parser.add_argument("--prices", type=Path, help="JSON price table with the same shape as --dump-default-prices.")
    _ = parser.add_argument("--dump-default-prices", action="store_true", help="Print the bundled editable price table and exit.")
    _ = parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.dump_default_prices and not parsed.jsonl_paths:
        parser.error("provide at least one JSONL path, or use --dump-default-prices.")
    price_date = date.fromisoformat(parsed.price_date or str_field(DEFAULT_PRICE_TABLE.get("source_accessed")) or date.today().isoformat())
    return Args(tuple(parsed.jsonl_paths), parsed.provider, parsed.model, price_date, parsed.prices, parsed.dump_default_prices, parsed.json)


def str_field(value: Any) -> str:
    return value if isinstance(value, str) else ""


def int_field(mapping: dict[str, Any], key: str) -> int:
    value = mapping.get(key, 0)
    return value if isinstance(value, int) else 0


def usage_from(raw: Any) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    return Usage(*(int_field(raw, key) for key in TOKEN_KEYS))


def session_key_from_meta(path: Path, payload: dict[str, Any], current: str) -> str:
    meta = payload.get("payload")
    if isinstance(meta, dict):
        session_id = str_field(meta.get("id")) or str_field(meta.get("session_id"))
        if session_id:
            return session_id
    return current or str(path)


def snapshots_from_file(path: Path, provider_arg: str, model_arg: str) -> list[Snapshot]:
    snapshots: list[Snapshot] = []
    session_key = str(path)
    provider = provider_arg
    model = model_arg
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "session_meta":
                session_key = session_key_from_meta(path, record, session_key)
                provider = provider_arg or str_field(payload.get("model_provider")) or provider
                model = model_arg or str_field(payload.get("model")) or model
                continue
            if record.get("type") == "turn_context":
                provider = provider_arg
                model = model_arg or str_field(payload.get("model")) or model
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = usage_from(info.get("total_token_usage"))
            if usage is None:
                continue
            snapshots.append(Snapshot(str_field(record.get("timestamp")), path, line_no, session_key, provider, model or "unknown", usage))
    return snapshots


def snapshot_sort_key(snapshot: Snapshot) -> tuple[datetime, str, int]:
    try:
        timestamp = datetime.fromisoformat(snapshot.timestamp.replace("Z", "+00:00"))
    except ValueError:
        timestamp = datetime.min.replace(tzinfo=UTC)
    return timestamp, str(snapshot.path), snapshot.line_no


def aggregate(snapshots: list[Snapshot]) -> dict[tuple[str, str], Usage]:
    previous_by_session: dict[str, Usage] = {}
    totals: dict[tuple[str, str], Usage] = {}
    for snapshot in sorted(snapshots, key=snapshot_sort_key):
        previous = previous_by_session.get(snapshot.session_key)
        delta = snapshot.usage if previous is None else snapshot.usage.delta_from(previous)
        previous_by_session[snapshot.session_key] = snapshot.usage
        if delta is None:
            continue
        key = (snapshot.provider, snapshot.model)
        totals[key] = totals.get(key, Usage()).add(delta)
    return totals


def load_price_table(path: Path | None) -> dict[str, Any]:
    if path is None:
        return DEFAULT_PRICE_TABLE
    with path.open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: price table must be a JSON object.")
    return loaded


def load_prices(path: Path | None) -> list[Price]:
    table = load_price_table(path)
    rows = table.get("prices")
    if not isinstance(rows, list):
        raise ValueError("price table must contain a `prices` list.")
    prices: list[Price] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"price row {idx} must be an object.")
        prices.append(
            Price(
                str_field(row.get("provider")).lower(),
                str_field(row.get("model")).lower(),
                date.fromisoformat(str_field(row.get("effective_date"))),
                Decimal(str_field(row.get("input_tokens"))),
                Decimal(str_field(row.get("cached_input_tokens"))),
                Decimal(str_field(row.get("output_tokens"))),
            )
        )
    return prices


def select_price(prices: list[Price], provider: str, model: str, price_date: date) -> Price | None:
    candidates = [price for price in prices if price.provider == provider.lower() and price.model == model.lower() and price.effective_date <= price_date]
    return max(candidates, key=lambda price: price.effective_date) if candidates else None


def cost_usd(usage: Usage, price: Price) -> Decimal:
    uncached_input_tokens = max(0, usage.input_tokens - usage.cached_input_tokens)
    return (
        Decimal(uncached_input_tokens) * price.input_per_1m
        + Decimal(usage.cached_input_tokens) * price.cached_input_per_1m
        + Decimal(usage.output_tokens) * price.output_per_1m
    ) / Decimal(1_000_000)


def report_json(totals: dict[tuple[str, str], Usage], prices: list[Price], price_date: date) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_cost = Decimal("0")
    for provider_model, usage in sorted(totals.items()):
        provider, model = provider_model
        price = select_price(prices, provider, model, price_date)
        row: dict[str, Any] = {
            "provider": provider,
            "model": model,
            **{key: getattr(usage, key) for key in TOKEN_KEYS},
        }
        if price is not None:
            cost = cost_usd(usage, price)
            total_cost += cost
            row["effective_price_date"] = price.effective_date.isoformat()
            row["cost_usd"] = f"{cost:.6f}"
        else:
            row["price_error"] = f"no price for provider={provider} model={model} date<={price_date.isoformat()}"
        rows.append(row)
    return {
        "aggregation_rule": aggregation_rule(),
        "price_date": price_date.isoformat(),
        "total_cost_usd": f"{total_cost:.6f}",
        "has_price_errors": any("price_error" in row for row in rows),
        "rows": rows,
    }


def aggregation_rule() -> str:
    return "Sort token_count total_token_usage snapshots by timestamp; per logical session key, add positive cumulative deltas. Duplicate snapshots add zero; all-counter decreases start a new segment; mixed decreases add only positive field deltas."


def print_text(report: dict[str, Any]) -> None:
    print(f"aggregation: {report['aggregation_rule']}")
    print(f"price_date: {report['price_date']}")
    print(f"total_cost_usd: {report['total_cost_usd']}")
    for row in report["rows"]:
        print(
            "row: "
            f"provider={row['provider']} model={row['model']} "
            f"input={row['input_tokens']} cached_input={row['cached_input_tokens']} "
            f"output={row['output_tokens']} reasoning_output={row['reasoning_output_tokens']} "
            f"cost_usd={row.get('cost_usd', 'unknown')} "
            f"price_date={row.get('effective_price_date', 'missing')}"
        )
        if "price_error" in row:
            print(f"price_error: {row['price_error']}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        if args.dump_default_prices:
            print(json.dumps(DEFAULT_PRICE_TABLE, indent=2, sort_keys=True))
            return 0
        snapshots = [snapshot for path in args.jsonl_paths for snapshot in snapshots_from_file(path, args.provider, args.model)]
        report = report_json(aggregate(snapshots), load_prices(args.prices_path), args.price_date)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text(report)
    return 1 if report["has_price_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
