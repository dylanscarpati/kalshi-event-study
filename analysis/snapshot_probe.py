"""Snapshot one live Kalshi macro market.

Discovers the nearest open event in a series (never constructs event
tickers by hand -- the naming convention is observed, not contracted),
prints its strike ladder and the consolidated YES top of book, and tapes
every raw HTTP response verbatim, on receipt and before parsing, to a
per-run JSONL file under data/snapshots/.

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


class Tape:
    """Append-only JSONL tape, one file per run so concurrent probes can
    never interleave. Each record is the verbatim response text, never
    re-encoded (re-dumping parsed JSON would alter key order and
    whitespace), written the moment the response arrives -- capture
    first, interpret later.
    """

    def __init__(self, out_dir: Path, run_id: str) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / f"{run_id}.jsonl"
        self.run_id = run_id
        self.count = 0

    def append(self, r: ApiResponse) -> None:
        record = {
            "schema": SCHEMA,
            "probe_run_id": self.run_id,
            "recv_wall_ns": r.recv_wall_ns,
            "recv_mono_ns": r.recv_mono_ns,
            "elapsed_ms": r.elapsed_ms,
            "request": {"path": r.path, "params": r.params},
            "http_status": r.http_status,
            "body_text": r.body_text,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.count += 1


def taped_get(session: requests.Session, tape: Tape, path: str, params: dict) -> ApiResponse:
    """Fetch, tape the exchange unconditionally, then fail loudly on a
    non-200 -- the observation is preserved either way."""
    resp = get(session, path, params)
    tape.append(resp)
    if resp.http_status != 200:
        print(f"HTTP {resp.http_status} from {path} (exchange recorded to tape)", file=sys.stderr)
        sys.exit(1)
    return resp


def fetch_open_markets(session: requests.Session, tape: Tape, series: str) -> list:
    """Follow the pagination cursor until exhausted; an empty cursor marks
    the last page. Stopping early would silently truncate the ladder."""
    markets = []
    cursor = ""
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        resp = taped_get(session, tape, "/markets", params)
        body = resp.json()
        markets.extend(parse_market(m) for m in body["markets"])
        cursor = body.get("cursor", "")
        if not cursor:
            return markets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", default="KXCPI", help="series ticker to probe")
    parser.add_argument("--depth", type=int, default=5, help="orderbook levels per side")
    parser.add_argument("--out-dir", type=Path, default=Path("data/snapshots"))
    args = parser.parse_args()

    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    tape = Tape(args.out_dir, run_id)

    with requests.Session() as session:
        markets = fetch_open_markets(session, tape, args.series)
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
        book_resp = taped_get(
            session, tape, f"/markets/{target.ticker}/orderbook", {"depth": args.depth}
        )
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

    print(f"\ntaped {tape.count} raw records to {tape.path} (run {run_id})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        sys.exit(1)
