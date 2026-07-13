"""Replay recorder frames tapes into order-book state and top-of-book series.

Book semantics (verified against the live venue 2026-07-08): both returned
sides are BIDS — YES bids and NO bids — so the consolidated YES ask is
100 minus the best NO bid. An orderbook_snapshot REPLACES the market's book
(checkpoint snapshots arrive mid-stream by design); an orderbook_delta
applies one signed quantity change at one price level, removing the level
when quantity reaches zero.

Chain integrity: the per-sid envelope sequence is consumed during replay
exactly the way the recorder consumed it live (every sid+seq envelope
observed; snapshots re-baseline). Any anomaly is recorded with its receipt
timestamp so callers can apply the pre-registered inclusion rule: an
analysis window is usable only if no anomaly falls inside it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from kalshi_rest import dollars_str_to_cents
from kalshi_ws import GapDetector, parse_envelope


@dataclass(frozen=True)
class TopQuote:
    recv_wall_ns: int
    ts_ms: int | None
    market: str
    bid_c: int | None  # best YES bid
    ask_c: int | None  # 100 - best NO bid

    @property
    def mid_c(self) -> float | None:
        if self.bid_c is None or self.ask_c is None:
            return None
        return (self.bid_c + self.ask_c) / 2

    @property
    def spread_c(self) -> int | None:
        if self.bid_c is None or self.ask_c is None:
            return None
        return self.ask_c - self.bid_c


class Book:
    """One market's resting bids on both sides: price (cents) -> quantity."""

    def __init__(self) -> None:
        self.yes: dict[int, float] = {}
        self.no: dict[int, float] = {}

    @staticmethod
    def _levels(pairs) -> dict[int, float]:
        return {dollars_str_to_cents(p): float(q) for p, q in (pairs or [])}

    def load_snapshot(self, msg: dict) -> None:
        self.yes = self._levels(msg.get("yes_dollars_fp"))
        self.no = self._levels(msg.get("no_dollars_fp"))

    def apply_delta(self, msg: dict) -> None:
        side = self.yes if msg.get("side") == "yes" else self.no
        price = dollars_str_to_cents(msg["price_dollars"])
        qty = side.get(price, 0.0) + float(msg["delta_fp"])
        if qty <= 1e-9:
            side.pop(price, None)
        else:
            side[price] = qty

    def top(self) -> tuple[int | None, int | None]:
        bid = max(self.yes) if self.yes else None
        best_no = max(self.no) if self.no else None
        ask = (100 - best_no) if best_no is not None else None
        return bid, ask


@dataclass
class ReplayResult:
    tops: dict[str, list[TopQuote]]  # per market, in receipt order
    anomalies: list[dict]
    frames_in: int

    def clean_window(self, start_wall_ns: int, end_wall_ns: int) -> bool:
        """The section-6 inclusion rule: usable iff no chain anomaly lands
        inside the window."""
        return not any(start_wall_ns <= a["recv_wall_ns"] <= end_wall_ns
                       for a in self.anomalies)

    def asof(self, market: str, wall_ns: int) -> TopQuote | None:
        """Last top-of-book at or before wall_ns (linear from bisect point)."""
        series = self.tops.get(market, [])
        lo, hi = 0, len(series)
        while lo < hi:
            mid = (lo + hi) // 2
            if series[mid].recv_wall_ns <= wall_ns:
                lo = mid + 1
            else:
                hi = mid
        return series[lo - 1] if lo else None


def replay_frames(lines: Iterable[str]) -> ReplayResult:
    books: dict[str, Book] = {}
    gaps = GapDetector()
    last_top: dict[str, tuple] = {}
    tops: dict[str, list[TopQuote]] = defaultdict(list)
    anomalies: list[dict] = []
    frames_in = 0

    for line in lines:
        rec = json.loads(line)
        if rec.get("direction") != "in":
            continue
        frames_in += 1
        env = parse_envelope(rec["raw"])

        if env.sid is not None and env.seq is not None:
            key = f"sid:{env.sid}"
            if env.type == "orderbook_snapshot":
                gaps.resync(key, env.seq)  # snapshots re-baseline the chain
            else:
                anomaly = gaps.observe(key, env.seq)
                if anomaly is not None:
                    anomalies.append({
                        "recv_wall_ns": rec["recv_wall_ns"], "sid": env.sid,
                        "kind": anomaly.kind.name if hasattr(anomaly.kind, "name") else str(anomaly.kind),
                        "expected": anomaly.expected, "received": anomaly.received,
                    })

        market = env.msg.get("market_ticker")
        if not market:
            continue
        if env.type == "orderbook_snapshot":
            books.setdefault(market, Book()).load_snapshot(env.msg)
        elif env.type == "orderbook_delta":
            books.setdefault(market, Book()).apply_delta(env.msg)
        else:
            continue  # trades and acks do not mutate the book

        top = books[market].top()
        if top != last_top.get(market):
            last_top[market] = top
            tops[market].append(TopQuote(rec["recv_wall_ns"], env.msg.get("ts_ms"),
                                         market, top[0], top[1]))

    return ReplayResult(dict(tops), anomalies, frames_in)


def replay_tape(path: str | Path) -> ReplayResult:
    with open(path, encoding="utf-8") as f:
        return replay_frames(f)
