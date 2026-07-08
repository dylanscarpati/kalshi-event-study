"""Fetch historical candles for every settled market with a mapped t0.

Two taped requests per market: hourly candles over [t0-31d, t0] (day-scale
and 12h gridpoints) and 1-minute candles over [t0-5h, t0] (hour-scale
gridpoints). Historical endpoint first, live endpoint as fallback for
recently settled markets.

Probe-class (hand-run, re-runnable) but RESUMABLE: each fully fetched market
is recorded in a manifest; a re-run skips completed markets and fills gaps,
so transient failures cost a log line, not a restart.

Usage: python analysis/candle_pull.py [--pace-sec 0.75]
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from instrument_util import run_metadata, stamp
from kalshi_rest import get
from tape import Tape, new_run_id, rest_record

SCHEMA = "candle_pull/1"
HOURLY_LOOKBACK_S = 31 * 86400
MINUTE_LOOKBACK_S = 5 * 3600


def load_t0_by_event(path: str = "data/derived/events.csv") -> dict[str, int]:
    out = {}
    for r in csv.DictReader(open(path, encoding="utf-8")):
        t0 = datetime.fromisoformat(r["t0_utc"].replace("Z", "+00:00"))
        out[r["event_ticker"]] = int(t0.timestamp())
    return out


def fetch_candles(session, tape, run_id, series, ticker, start_ts, end_ts, interval):
    """Historical endpoint first, live fallback. Returns candle list or None."""
    paths = (
        f"/historical/markets/{ticker}/candlesticks",
        f"/series/{series}/markets/{ticker}/candlesticks",
    )
    for path in paths:
        resp = get(session, path, {"start_ts": start_ts, "end_ts": end_ts, "period_interval": interval})
        tape.append({**rest_record(SCHEMA, run_id, resp), "kind": "candles",
                     "ticker": ticker, "interval": interval})
        if resp.http_status == 200:
            return resp.json().get("candlesticks", [])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pace-sec", type=float, default=0.75)
    parser.add_argument("--out-dir", type=Path, default=Path("data/candles"))
    args = parser.parse_args()

    t0_by_event = load_t0_by_event()
    markets = [r for r in csv.DictReader(open("data/derived/settled_markets.csv", encoding="utf-8"))
               if r["event_ticker"] in t0_by_event]
    skipped_unmapped = 1933 - len(markets)

    manifest_path = args.out_dir / "manifest.txt"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    done = set(manifest_path.read_text(encoding="utf-8").splitlines()) if manifest_path.exists() else set()
    todo = [m for m in markets if m["ticker"] not in done]
    print(f"{len(markets)} markets mapped ({skipped_unmapped} on unmapped events skipped); "
          f"{len(done)} already fetched; {len(todo)} to go "
          f"(~{len(todo) * 2 * args.pace_sec / 60:.0f} min)")

    run_id = new_run_id()
    failures = 0
    with Tape(args.out_dir / f"{run_id}.jsonl") as tape, requests.Session() as session, \
            manifest_path.open("a", encoding="utf-8") as manifest:
        tape.append({"schema": SCHEMA, "run_id": run_id, "kind": "run_start", **stamp(),
                     **run_metadata(), "n_todo": len(todo)})
        for i, m in enumerate(todo):
            t0 = t0_by_event[m["event_ticker"]]
            ok = True
            for start, interval in ((t0 - HOURLY_LOOKBACK_S, 60), (t0 - MINUTE_LOOKBACK_S, 1)):
                candles = fetch_candles(session, tape, run_id, m["series_ticker"],
                                        m["ticker"], start, t0, interval)
                if candles is None:
                    ok = False
                    failures += 1
                    print(f"  FAILED {m['ticker']} interval={interval}", file=sys.stderr)
                time.sleep(args.pace_sec)
            if ok:
                manifest.write(m["ticker"] + "\n")
                manifest.flush()
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(todo)} markets fetched ({failures} failures)")
        tape.append({"schema": SCHEMA, "run_id": run_id, "kind": "run_end", **stamp(),
                     "fetched": len(todo) - failures, "failures": failures})

    print(f"done: {len(todo) - failures} fetched, {failures} failures -> re-run to fill gaps")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
