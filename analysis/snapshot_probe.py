"""Snapshot one live Kalshi macro market.

Discovers the nearest open event in a series (never constructs event
tickers by hand -- the naming convention is observed, not contracted),
prints its strike ladder and the consolidated YES top of book, and appends
every raw HTTP response verbatim to a JSONL tape under data/snapshots/.

Usage: python analysis/snapshot_probe.py [--series KXCPI] [--depth 5]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import requests

from kalshi_rest import ApiResponse, consolidate_yes_top, get, parse_market

SCHEMA = "snapshot_probe/1"


def fmt(value: int | float | None) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def append_jsonl(out_dir: Path, run_id: str, responses: list[ApiResponse]) -> Path:
    """One line per HTTP response. body_text is the verbatim response text,
    never re-encoded: re-dumping parsed JSON would alter key order and
    whitespace, and the raw record is the one thing we never modify."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{time.strftime('%Y-%m-%d', time.gmtime())}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        for r in responses:
            record = {
                "schema": SCHEMA,
                "probe_run_id": run_id,
                "recv_wall_ns": r.recv_wall_ns,
                "recv_mono_ns": r.recv_mono_ns,
                "elapsed_ms": r.elapsed_ms,
                "request": {"path": r.path, "params": r.params},
                "http_status": r.http_status,
                "body_text": r.body_text,
            }
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", default="KXCPI", help="series ticker to probe")
    parser.add_argument("--depth", type=int, default=5, help="orderbook levels per side")
    parser.add_argument("--out-dir", type=Path, default=Path("data/snapshots"))
    args = parser.parse_args()

    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    responses: list[ApiResponse] = []

    with requests.Session() as session:
        markets_resp = get(
            session,
            "/markets",
            {"series_ticker": args.series, "status": "open", "limit": 1000},
        )
        responses.append(markets_resp)
        markets = [parse_market(m) for m in markets_resp.json()["markets"]]
        if not markets:
            print(f"no open markets for series {args.series!r}", file=sys.stderr)
            return 2

        # The event whose markets close soonest is the next scheduled release.
        nearest = min(markets, key=lambda m: m.close_time)
        ladder = sorted(
            (m for m in markets if m.event_ticker == nearest.event_ticker),
            key=lambda m: m.floor_strike if m.floor_strike is not None else float("-inf"),
        )

        print(
            f"series {args.series}: {len(markets)} open markets; "
            f"nearest event {nearest.event_ticker} closes {nearest.close_time}"
        )
        print(f"{'ticker':<22} {'bid':>4} {'ask':>4} {'mid':>6} {'volume':>12}")
        for m in ladder:
            print(
                f"{m.ticker:<22} {fmt(m.yes_bid_cents):>4} {fmt(m.yes_ask_cents):>4} "
                f"{fmt(m.mid_cents):>6} {m.volume:>12}"
            )

        target = max(ladder, key=lambda m: m.volume)
        book_resp = get(session, f"/markets/{target.ticker}/orderbook", {"depth": args.depth})
        responses.append(book_resp)
        top = consolidate_yes_top(book_resp.json())

        print()
        print(f"orderbook {target.ticker} (top {args.depth} levels per side)")
        print(
            f"  consolidated YES top: bid {fmt(top.yes_bid_cents)} / ask {fmt(top.yes_ask_cents)}"
            f"  (ask = 100 - best NO bid)"
        )
        print(f"  mid {fmt(top.mid_cents)}, spread {fmt(top.spread_cents)}")
        print(f"  market-object quotes: bid {fmt(target.yes_bid_cents)} / ask {fmt(target.yes_ask_cents)}")
        print(f"  resting depth: {top.depth_yes} YES / {top.depth_no} NO contracts")

    out_path = append_jsonl(args.out_dir, run_id, responses)
    print(f"\nappended {len(responses)} raw records to {out_path} (run {run_id})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        sys.exit(1)
