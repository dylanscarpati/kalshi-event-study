"""Snapshot one live Kalshi macro market.

Discovers the nearest open event in a series (never constructs event
tickers by hand -- the naming convention is observed, not contracted),
prints its strike ladder and the consolidated YES top of book, and tapes
every raw HTTP response verbatim, on receipt and before parsing, to a
per-run JSONL file under data/snapshots/.

Probe-class instrument: hand-run, nothing perishable, so failures are
loud and immediate (no retries). The capture instruments are the ones
that must survive; see release_poller.py.

Usage: python analysis/snapshot_probe.py [--series KXCPI] [--depth 5]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests

from kalshi_rest import consolidate_yes_top, get
from market_discovery import event_ladder, fetch_open_markets, nearest_event, require_ok
from tape import Tape, new_run_id, rest_record

SCHEMA = "snapshot_probe/2"


def fmt(value: int | float | None) -> str:
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", default="KXCPI", help="series ticker to probe")
    parser.add_argument("--depth", type=int, default=5, help="orderbook levels per side")
    parser.add_argument("--out-dir", type=Path, default=Path("data/snapshots"))
    args = parser.parse_args()

    run_id = new_run_id()
    with Tape(args.out_dir / f"{run_id}.jsonl") as tape, requests.Session() as session:
        on_response = lambda r: tape.append(rest_record(SCHEMA, run_id, r))

        markets = fetch_open_markets(session, args.series, on_response)
        if not markets:
            print(f"no open markets for series {args.series!r}", file=sys.stderr)
            return 2

        event = nearest_event(markets)
        ladder = event_ladder(markets, event)

        print(
            f"series {args.series}: {len(markets)} open markets; "
            f"nearest event {event} closes {ladder[0].close_time}"
        )
        print(f"{'ticker':<22} {'bid':>4} {'ask':>4} {'mid':>6} {'volume':>12}")
        for m in ladder:
            print(
                f"{m.ticker:<22} {fmt(m.yes_bid_cents):>4} {fmt(m.yes_ask_cents):>4} "
                f"{fmt(m.mid_cents):>6} {m.volume:>12}"
            )

        target = max(ladder, key=lambda m: m.volume)
        book_resp = get(session, f"/markets/{target.ticker}/orderbook", {"depth": args.depth})
        on_response(book_resp)
        require_ok(book_resp)
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
    except RuntimeError as exc:
        print(f"{exc} (exchange recorded to tape)", file=sys.stderr)
        sys.exit(1)
