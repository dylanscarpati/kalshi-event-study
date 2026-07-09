"""Derive gridpoint prices from the candle tapes, per amendment A3.

For each (settled market, TTR gridpoint) pair: locate the last candle at or
before the gridpoint within the section-3.3 staleness limit, then apply the
locked price-source hierarchy -- MID (bid/ask closes, both sides in [1,99]
cents, bid < ask, spread <= cap) else TRADE (most recent price.close within
staleness) else SKIP. Every emitted row carries the source tag, era tag, raw
bid/ask/trade values (so the spread-cap sensitivity grid re-derives without
refetching), and staleness metadata.

Re-runnable: reads all tapes under data/candles/, writes
data/derived/gridpoint_prices.csv. Never mutates tapes.

Usage: python analysis/gridpoints.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from kalshi_rest import dollars_str_to_cents

# (label, seconds before t0, staleness limit seconds, candle interval to use)
GRIDPOINTS = [
    ("30d", 30 * 86400, 86400, 60),
    ("14d", 14 * 86400, 86400, 60),
    ("7d", 7 * 86400, 86400, 60),
    ("3d", 3 * 86400, 86400, 60),
    ("1d", 1 * 86400, 86400, 60),
    ("12h", 12 * 3600, 2 * 3600, 60),
    ("4h", 4 * 3600, 900, 1),
    ("2h", 2 * 3600, 900, 1),
    ("1h", 1 * 3600, 900, 1),
]
HEADLINE_SPREAD_CAP_C = 10

FIELDS = ["event_ticker", "ticker", "gridpoint", "t0_utc", "gridpoint_ts", "candle_ts",
          "staleness_s", "source", "price_c", "bid_close_c", "ask_close_c",
          "trade_close_c", "spread_c", "era"]


def cents_or_none(dollars: str | None) -> int | None:
    if dollars is None:
        return None
    try:
        return dollars_str_to_cents(dollars)
    except Exception:
        return None


def pick_candle(candles: list[dict], target_ts: int, staleness_s: int) -> dict | None:
    """Last candle with end_period_ts <= target within the staleness window.
    Candle lists are small (<= ~750); a linear scan keeps this obvious."""
    best = None
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None or ts > target_ts or ts < target_ts - staleness_s:
            continue
        if best is None or ts > best["end_period_ts"]:
            best = c
    return best


def latest_trade(candles: list[dict], target_ts: int, staleness_s: int) -> dict | None:
    """Most recent candle within the window that carries a trade close."""
    best = None
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None or ts > target_ts or ts < target_ts - staleness_s:
            continue
        if cents_or_none((c.get("price") or {}).get("close")) is None:
            continue
        if best is None or ts > best["end_period_ts"]:
            best = c
    return best


def mid_admissible(bid_c: int | None, ask_c: int | None, cap_c: int) -> bool:
    """A3: both sides live in [1, 99] cents (subsumes the 0/100 sentinels and
    the observed both-zero empty-candle encoding), book not crossed/locked,
    spread within the cap."""
    return (bid_c is not None and ask_c is not None
            and 1 <= bid_c <= 99 and 1 <= ask_c <= 99
            and bid_c < ask_c and (ask_c - bid_c) <= cap_c)


def gridpoint_row(candles: list[dict], target_ts: int, staleness_s: int,
                  cap_c: int = HEADLINE_SPREAD_CAP_C) -> dict | None:
    """Apply the A3 hierarchy at one gridpoint. Returns partial row or None."""
    candle = pick_candle(candles, target_ts, staleness_s)
    bid_c = ask_c = None
    if candle is not None:
        bid_c = cents_or_none((candle.get("yes_bid") or {}).get("close"))
        ask_c = cents_or_none((candle.get("yes_ask") or {}).get("close"))
        if mid_admissible(bid_c, ask_c, cap_c):
            return {"source": "MID", "price_c": (bid_c + ask_c) / 2,
                    "bid_close_c": bid_c, "ask_close_c": ask_c,
                    "trade_close_c": cents_or_none((candle.get("price") or {}).get("close")),
                    "spread_c": ask_c - bid_c, "candle_ts": candle["end_period_ts"]}
    trade = latest_trade(candles, target_ts, staleness_s)
    if trade is not None:
        return {"source": "TRADE",
                "price_c": cents_or_none((trade.get("price") or {}).get("close")),
                "bid_close_c": bid_c, "ask_close_c": ask_c,
                "trade_close_c": cents_or_none((trade.get("price") or {}).get("close")),
                "spread_c": "", "candle_ts": trade["end_period_ts"]}
    return None


def load_candles_by_ticker() -> dict[tuple[str, int], list[dict]]:
    """Union of all candle tapes, keyed (ticker, interval), deduped by ts."""
    store: dict[tuple[str, int], dict[int, dict]] = {}
    for tape in sorted(Path("data/candles").glob("*.jsonl")):
        for line in tape.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("kind") != "candles" or rec.get("http_status") != 200:
                continue
            key = (rec["ticker"], rec["interval"])
            by_ts = store.setdefault(key, {})
            for c in json.loads(rec["body_text"]).get("candlesticks", []):
                if c.get("end_period_ts") is not None:
                    by_ts[c["end_period_ts"]] = c
    return {k: sorted(v.values(), key=lambda c: c["end_period_ts"]) for k, v in store.items()}


def main() -> int:
    t0_by_event = {}
    for r in csv.DictReader(open("data/derived/events.csv", encoding="utf-8")):
        t0_by_event[r["event_ticker"]] = (
            r["t0_utc"], int(datetime.fromisoformat(r["t0_utc"].replace("Z", "+00:00")).timestamp()))
    markets = [r for r in csv.DictReader(open("data/derived/settled_markets.csv", encoding="utf-8"))
               if r["event_ticker"] in t0_by_event]
    candles = load_candles_by_ticker()

    rows, skips = [], 0
    for m in markets:
        t0_iso, t0_ts = t0_by_event[m["event_ticker"]]
        for label, before_s, staleness_s, interval in GRIDPOINTS:
            series = candles.get((m["ticker"], interval), [])
            partial = gridpoint_row(series, t0_ts - before_s, staleness_s)
            if partial is None:
                skips += 1
                continue
            rows.append({
                "event_ticker": m["event_ticker"], "ticker": m["ticker"],
                "gridpoint": label, "t0_utc": t0_iso,
                "gridpoint_ts": t0_ts - before_s,
                "staleness_s": t0_ts - before_s - partial["candle_ts"],
                "era": "pre_collector",
                **partial,
            })
    out = Path("data/derived/gridpoint_prices.csv")
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    src = Counter(r["source"] for r in rows)
    by_gp = Counter(r["gridpoint"] for r in rows)
    print(f"{len(rows)} observations -> {out}  sources: {dict(src)}  skipped: {skips}")
    print("per gridpoint:", dict(sorted(by_gp.items())))

    # A3.1 item 6: verification diagnostics attached to every build.
    sentinel_sided = 0
    for m in markets:
        _, t0_ts = t0_by_event[m["event_ticker"]]
        for label, before_s, staleness_s, interval in GRIDPOINTS:
            c = pick_candle(candles.get((m["ticker"], interval), []), t0_ts - before_s, staleness_s)
            if c is None:
                continue
            b = cents_or_none((c.get("yes_bid") or {}).get("close"))
            a = cents_or_none((c.get("yes_ask") or {}).get("close"))
            if b == 0 or a == 100 or a == 0:
                sentinel_sided += 1
    n = len(rows)
    spread_hist = Counter(int(r["spread_c"]) for r in rows if r["source"] == "MID")
    diag = [
        f"A3.1 verification diagnostics — build {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}",
        f"observations: {n}  (MID {src['MID']}, TRADE {src['TRADE']}, skipped pairs {skips})",
        f"fallback-usage fraction (TRADE share): {src['TRADE'] / n:.4f}",
        "admitted-MID spread distribution (cents: count): "
        + ", ".join(f"{s}: {spread_hist[s]}" for s in sorted(spread_hist)),
        f"sentinel-sided candles at gridpoint selection: {sentinel_sided}",
    ]
    diag_path = Path("data/derived/gridpoint_diagnostics.txt")
    diag_path.write_text("\n".join(diag) + "\n", encoding="utf-8")
    print("\n".join(diag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
