"""Read-only helpers for Kalshi's public market-data REST API.

Verified against https://docs.kalshi.com on 2026-07-07: market-data reads
need no authentication, prices arrive as fixed-point decimal strings in
`_dollars` fields, and quantities arrive as `_fp` strings that may be
fractional.

Parsing rules: fields that identify an object (ticker, close_time) are
required and raise KeyError when absent; fields describing market state
that can be legitimately empty parse to None. Tradable prices live in
[1, 99] cents, so a bid of "0.0000" or an ask of "1.0000" is the
exchange's "no such order" sentinel and also parses to None.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal

import requests

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"


@dataclass(frozen=True)
class ApiResponse:
    """One HTTP exchange, stamped with both clocks on receipt.

    Wall time aligns observations to calendar time (release schedules);
    monotonic time is for durations and never jumps. Stamped as soon as
    the response returns, before any parsing.
    """

    path: str
    params: dict | None
    http_status: int
    body_text: str
    recv_wall_ns: int
    recv_mono_ns: int
    elapsed_ms: float

    def json(self) -> dict:
        return json.loads(self.body_text)


def get(session: requests.Session, path: str, params: dict | None = None) -> ApiResponse:
    """GET a public market-data endpoint.

    Returns an ApiResponse for every completed HTTP exchange, success or
    error status alike, so the caller can record the observation before
    deciding what to do with it. Transport failures (timeout, DNS,
    connection reset) still raise requests.RequestException. No retries
    by design: this backs a hand-run probe, so failures should be loud
    and immediate. Resilience (backoff, reconnect) belongs to the
    collector, not here.
    """
    response = session.get(BASE_URL + path, params=params, timeout=10)
    recv_wall_ns = time.time_ns()
    recv_mono_ns = time.monotonic_ns()
    return ApiResponse(
        path=path,
        params=params,
        http_status=response.status_code,
        body_text=response.text,
        recv_wall_ns=recv_wall_ns,
        recv_mono_ns=recv_mono_ns,
        elapsed_ms=response.elapsed.total_seconds() * 1000.0,
    )


def dollars_str_to_cents(dollars: str) -> int:
    """Convert a fixed-point dollar string like "0.8900" to integer cents.

    Decimal keeps this exact: 0.07 has no finite binary representation, so
    going through float would corrupt prices. Sub-cent values raise so a
    future tick-size change fails loudly here instead of rounding silently.
    """
    cents = Decimal(dollars) * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"sub-cent price {dollars!r}; expected whole cents")
    return int(cents)


def _quote_or_none(dollars: str | None, *, sentinel: int) -> int | None:
    if dollars is None:
        return None
    cents = dollars_str_to_cents(dollars)
    return None if cents == sentinel else cents


@dataclass(frozen=True)
class MarketSummary:
    ticker: str
    event_ticker: str
    title: str
    status: str
    close_time: str
    floor_strike: float | None
    yes_bid_cents: int | None
    yes_ask_cents: int | None
    volume: Decimal

    @property
    def mid_cents(self) -> float | None:
        if self.yes_bid_cents is None or self.yes_ask_cents is None:
            return None
        return (self.yes_bid_cents + self.yes_ask_cents) / 2


def parse_market(raw: dict) -> MarketSummary:
    return MarketSummary(
        ticker=raw["ticker"],
        event_ticker=raw["event_ticker"],
        title=raw.get("title", ""),
        status=raw.get("status", ""),
        close_time=raw["close_time"],
        floor_strike=raw.get("floor_strike"),
        yes_bid_cents=_quote_or_none(raw.get("yes_bid_dollars"), sentinel=0),
        yes_ask_cents=_quote_or_none(raw.get("yes_ask_dollars"), sentinel=100),
        volume=Decimal(raw.get("volume_fp", "0")),
    )


@dataclass(frozen=True)
class ConsolidatedTop:
    """Top of the consolidated YES book.

    Kalshi's orderbook returns resting bids on both sides (YES bids and NO
    bids). Buying NO at q cents is economically identical to selling YES at
    100 - q, so the best NO bid is the consolidated YES ask -- that one
    conversion is the whole merge.
    """

    yes_bid_cents: int | None
    yes_ask_cents: int | None
    mid_cents: float | None
    spread_cents: int | None
    depth_yes: Decimal
    depth_no: Decimal


def consolidate_yes_top(orderbook: dict) -> ConsolidatedTop:
    book = orderbook["orderbook_fp"]
    yes_levels = book.get("yes_dollars") or []
    no_levels = book.get("no_dollars") or []

    yes_bid = max((dollars_str_to_cents(price) for price, _ in yes_levels), default=None)
    best_no_bid = max((dollars_str_to_cents(price) for price, _ in no_levels), default=None)
    yes_ask = None if best_no_bid is None else 100 - best_no_bid

    mid = spread = None
    if yes_bid is not None and yes_ask is not None:
        mid = (yes_bid + yes_ask) / 2
        spread = yes_ask - yes_bid

    return ConsolidatedTop(
        yes_bid_cents=yes_bid,
        yes_ask_cents=yes_ask,
        mid_cents=mid,
        spread_cents=spread,
        depth_yes=sum((Decimal(size) for _, size in yes_levels), Decimal(0)),
        depth_no=sum((Decimal(size) for _, size in no_levels), Decimal(0)),
    )
