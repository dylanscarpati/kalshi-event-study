"""Tests for the pure parsing helpers in kalshi_rest.

Fixtures are frozen from real API responses captured 2026-07-07
(KXCPI-26JUN-T-0.3, the June 2026 CPI event released 2026-07-14).
"""

from decimal import Decimal

import pytest

import pytest as _pytest

from kalshi_rest import consolidate_yes_top, dollars_str_to_cents, parse_market, parse_settled_row

REAL_SETTLED = {  # /historical/markets, KXCPI-26MAR-T1.3, frozen 2026-07-08
    "ticker": "KXCPI-26MAR-T1.3",
    "event_ticker": "KXCPI-26MAR",
    "status": "finalized",
    "result": "no",
    "settlement_value_dollars": "0.0000",
    "settlement_ts": "2026-04-10T13:06:04.496954Z",
    "expiration_value": "0.9",
    "open_time": "2026-03-17T15:00:00Z",
    "close_time": "2026-04-10T12:25:00Z",
    "expiration_time": "2026-07-10T13:56:00Z",
    "floor_strike": 1.3,
    "strike_type": "greater",
    "volume_fp": "46415.00",
}


def test_parse_settled_row_real_fixture():
    row = parse_settled_row(REAL_SETTLED, "KXCPI")
    assert row["ticker"] == "KXCPI-26MAR-T1.3"
    assert row["result"] == "no"
    assert row["settlement_value_dollars"] == "0.0000"  # verbatim string, no floats
    assert row["settlement_ts"] == "2026-04-10T13:06:04.496954Z"
    assert row["cap_strike"] == ""  # absent on greater-type strikes, never imputed


def test_parse_settled_row_missing_identity_raises():
    raw = dict(REAL_SETTLED)
    del raw["event_ticker"]
    with _pytest.raises(KeyError):
        parse_settled_row(raw, "KXCPI")

REAL_MARKET = {
    "ticker": "KXCPI-26JUN-T-0.3",
    "event_ticker": "KXCPI-26JUN",
    "title": "Will CPI rise more than -0.3% in June 2026?",
    "status": "active",
    "close_time": "2026-07-14T12:25:00Z",
    "floor_strike": -0.3,
    "yes_bid_dollars": "0.8900",
    "yes_ask_dollars": "0.9000",
    "no_bid_dollars": "0.1000",
    "no_ask_dollars": "0.1100",
    "volume_fp": "46645.47",
}

REAL_BOOK = {
    "orderbook_fp": {
        "no_dollars": [
            ["0.0600", "112.41"],
            ["0.0700", "8.68"],
            ["0.0800", "7.59"],
            ["0.0900", "6.75"],
            ["0.1000", "7.07"],
        ],
        "yes_dollars": [
            ["0.8500", "32.35"],
            ["0.8600", "36.97"],
            ["0.8700", "36.60"],
            ["0.8800", "5.00"],
            ["0.8900", "1.00"],
        ],
    }
}


def test_dollars_str_to_cents_is_exact():
    # 0.07 has no exact binary-float representation; Decimal must not care.
    assert dollars_str_to_cents("0.07") == 7
    assert dollars_str_to_cents("0.8900") == 89
    assert dollars_str_to_cents("1.0000") == 100
    with pytest.raises(ValueError):
        dollars_str_to_cents("0.0750")


def test_parse_market_real_fixture():
    m = parse_market(REAL_MARKET)
    assert m.ticker == "KXCPI-26JUN-T-0.3"
    assert m.event_ticker == "KXCPI-26JUN"
    assert m.yes_bid_cents == 89
    assert m.yes_ask_cents == 90
    assert m.mid_cents == 89.5
    assert m.volume == Decimal("46645.47")  # volumes can be fractional


def test_parse_market_sentinel_quotes_are_none():
    # Tradable prices are 1-99 cents: bid 0 means no bids, ask 100 no asks.
    quiet = dict(REAL_MARKET, yes_bid_dollars="0.0000", yes_ask_dollars="1.0000")
    m = parse_market(quiet)
    assert m.yes_bid_cents is None
    assert m.yes_ask_cents is None
    assert m.mid_cents is None


def test_parse_market_missing_identity_raises():
    raw = dict(REAL_MARKET)
    del raw["ticker"]
    with pytest.raises(KeyError):
        parse_market(raw)


def test_consolidate_two_sided_book():
    top = consolidate_yes_top(REAL_BOOK)
    assert top.yes_bid_cents == 89  # best native YES bid
    assert top.yes_ask_cents == 90  # 100 - best NO bid (10)
    assert top.mid_cents == 89.5
    assert top.spread_cents == 1
    assert top.depth_yes == Decimal("111.92")
    assert top.depth_no == Decimal("142.50")


def test_consolidate_one_sided_and_empty_book():
    one_sided = {"orderbook_fp": {"yes_dollars": [["0.1900", "51.00"]], "no_dollars": []}}
    top = consolidate_yes_top(one_sided)
    assert top.yes_bid_cents == 19
    assert top.yes_ask_cents is None
    assert top.mid_cents is None
    assert top.spread_cents is None

    empty = {"orderbook_fp": {"yes_dollars": None, "no_dollars": []}}
    top = consolidate_yes_top(empty)
    assert top.yes_bid_cents is None
    assert top.yes_ask_cents is None
    assert top.depth_yes == Decimal(0)
    assert top.depth_no == Decimal(0)
