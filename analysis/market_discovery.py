"""Market discovery shared by the probe and the capture instruments.

Every fetch hands the raw response to an on_response callback (taping)
before any parsing -- capture first, interpret later. Discovery is
setup-phase work, so failures here raise loudly; capture-phase
resilience lives in the instruments themselves.
"""

from __future__ import annotations

from typing import Callable

import requests

from kalshi_rest import ApiResponse, MarketSummary, get, parse_market

OnResponse = Callable[[ApiResponse], None]


def require_ok(resp: ApiResponse) -> None:
    if resp.http_status != 200:
        raise RuntimeError(f"HTTP {resp.http_status} from {resp.path}")


def _fetch_markets(
    session: requests.Session, params: dict, on_response: OnResponse
) -> list[MarketSummary]:
    """Page through /markets until the cursor is exhausted; an empty
    cursor marks the last page."""
    markets: list[MarketSummary] = []
    cursor = ""
    while True:
        page_params = dict(params, limit=1000)
        if cursor:
            page_params["cursor"] = cursor
        resp = get(session, "/markets", page_params)
        on_response(resp)
        require_ok(resp)
        body = resp.json()
        markets.extend(parse_market(m) for m in body["markets"])
        cursor = body.get("cursor", "")
        if not cursor:
            return markets


def fetch_open_markets(
    session: requests.Session, series: str, on_response: OnResponse
) -> list[MarketSummary]:
    return _fetch_markets(session, {"series_ticker": series, "status": "open"}, on_response)


def fetch_event_markets(
    session: requests.Session, event_ticker: str, on_response: OnResponse
) -> list[MarketSummary]:
    return _fetch_markets(session, {"event_ticker": event_ticker}, on_response)


def nearest_event(markets: list[MarketSummary]) -> str:
    """The event whose markets close soonest is the next scheduled release."""
    return min(markets, key=lambda m: m.close_time).event_ticker


def event_ladder(markets: list[MarketSummary], event_ticker: str) -> list[MarketSummary]:
    return sorted(
        (m for m in markets if m.event_ticker == event_ticker),
        key=lambda m: m.floor_strike if m.floor_strike is not None else float("-inf"),
    )


def atm_strike(ladder: list[MarketSummary]) -> MarketSummary:
    """Primary-contract rule: the strike whose mid is closest to 50 cents
    (maximum information sensitivity), higher volume breaking ties.
    Falls back to highest volume when no strike has a two-sided quote."""

    def key(m: MarketSummary):
        dist = abs(m.mid_cents - 50) if m.mid_cents is not None else float("inf")
        return (dist, -m.volume)

    return min(ladder, key=key)
