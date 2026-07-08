"""Pull the settled-market archive for the macro series (the calibration
sample).

Probe-class instrument: hand-run, fully re-runnable, nothing perishable,
so failures are loud and immediate. Every raw page is taped before
parsing, and the derived CSV holds verbatim API strings only -- no float
conversion, no statistics. Markets settled recently live on /markets;
older ones are archived under /historical/markets; both are pulled and
deduplicated by ticker with the live endpoint winning, and any field
disagreement between the two is logged as a data-integrity finding.

Usage: python analysis/settled_pull.py
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from pathlib import Path

import requests

from instrument_util import run_metadata, stamp
from kalshi_rest import SETTLED_ROW_FIELDS, get, parse_settled_row
from market_discovery import require_ok
from tape import Tape, new_run_id, rest_record

SCHEMA = "settled_pull/1"
SERIES = ["KXCPI", "KXCPIYOY", "KXPAYROLLS", "KXFED", "KXFEDDECISION"]
PACING_S = 1.0


def fetch_settled_pages(
    session: requests.Session, tape: Tape, run_id: str, series: str, endpoint: str
) -> list[dict]:
    markets: list[dict] = []
    cursor = ""
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        resp = get(session, endpoint, params)
        tape.append({**rest_record(SCHEMA, run_id, resp), "kind": "page", "series": series, "endpoint": endpoint})
        require_ok(resp)
        body = resp.json()
        markets.extend(body["markets"])
        cursor = body.get("cursor", "")
        if not cursor:
            return markets
        time.sleep(PACING_S)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("data/settled"))
    parser.add_argument("--csv", type=Path, default=Path("data/derived/settled_markets.csv"))
    parser.add_argument("--series", nargs="+", default=SERIES)
    args = parser.parse_args()

    run_id = new_run_id()
    rows: dict[str, dict] = {}
    diffs = 0

    with Tape(args.out_dir / f"{run_id}.jsonl") as tape, requests.Session() as session:
        tape.append({"schema": SCHEMA, "run_id": run_id, "kind": "run_start", **stamp(), **run_metadata(), "series": args.series})
        for series in args.series:
            # historical first so the live endpoint wins the merge
            for endpoint, source in [("/historical/markets", "historical"), ("/markets", "live")]:
                markets = fetch_settled_pages(session, tape, run_id, series, endpoint)
                for m in markets:
                    row = {**parse_settled_row(m, series), "source": source, "run_id": run_id}
                    prev = rows.get(row["ticker"])
                    if prev is None:
                        rows[row["ticker"]] = row
                        continue
                    changed = [
                        k for k in row
                        if k not in ("source", "run_id") and prev.get(k) != row.get(k)
                    ]
                    if changed:
                        diffs += 1
                        tape.append(
                            {
                                "schema": SCHEMA, "run_id": run_id, "kind": "dedup_diff", **stamp(),
                                "ticker": row["ticker"], "fields": changed,
                                "first_seen": {k: prev.get(k) for k in changed},
                                "second_seen": {k: row.get(k) for k in changed},
                            }
                        )
                    merged = row if source == "live" else prev
                    rows[row["ticker"]] = {**merged, "source": "both"}
                time.sleep(PACING_S)
        tape.append({"schema": SCHEMA, "run_id": run_id, "kind": "run_end", **stamp(), "rows": len(rows), "dedup_diffs": diffs})

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SETTLED_ROW_FIELDS)
        writer.writeheader()
        for ticker in sorted(rows):
            writer.writerow(rows[ticker])

    print(f"{len(rows)} settled markets -> {args.csv} (raw tape: {run_id}, {diffs} live/archive field diffs)")
    for series in args.series:
        srows = [r for r in rows.values() if r["series_ticker"] == series]
        if not srows:
            print(f"  {series:<14} 0 markets")
            continue
        closes = sorted(r["close_time"] for r in srows if r["close_time"])
        results = Counter(r["result"] or "(empty)" for r in srows)
        print(
            f"  {series:<14} {len(srows):>4} markets  {closes[0][:10]} .. {closes[-1][:10]}  results: "
            + ", ".join(f"{k}={v}" for k, v in sorted(results.items()))
        )
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
