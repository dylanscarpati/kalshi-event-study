"""Tests for market_discovery selection rules and the shared Tape."""

import json
from decimal import Decimal

from kalshi_rest import MarketSummary
from market_discovery import atm_strike, event_ladder, nearest_event
from tape import Tape


def mk(ticker, event, close, strike, bid, ask, vol):
    return MarketSummary(
        ticker=ticker,
        event_ticker=event,
        title="",
        status="active",
        close_time=close,
        floor_strike=strike,
        yes_bid_cents=bid,
        yes_ask_cents=ask,
        volume=Decimal(vol),
    )


LADDER = [
    mk("E1-T0.1", "E1", "2026-07-14T12:25:00Z", 0.1, 11, 12, "100"),
    mk("E1-T0.0", "E1", "2026-07-14T12:25:00Z", 0.0, 37, 38, "200"),
    mk("E1-T-0.1", "E1", "2026-07-14T12:25:00Z", -0.1, 47, 53, "50"),
    mk("E1-T0.2", "E1", "2026-07-14T12:25:00Z", 0.2, None, 2, "999"),
]
LATER = mk("E2-T0.0", "E2", "2026-08-12T12:25:00Z", 0.0, 60, 62, "10")


def test_nearest_event_picks_soonest_close():
    assert nearest_event(LADDER + [LATER]) == "E1"


def test_event_ladder_sorts_by_strike_and_filters():
    ladder = event_ladder(LADDER + [LATER], "E1")
    assert [m.floor_strike for m in ladder] == [-0.1, 0.0, 0.1, 0.2]


def test_atm_strike_prefers_mid_closest_to_50():
    # -0.1 strike: mid 50.0 exactly; one-sided 0.2 strike never wins despite volume
    assert atm_strike(LADDER).ticker == "E1-T-0.1"


def test_atm_strike_volume_breaks_ties():
    a = mk("A", "E", "t", 0.0, 45, 51, "10")   # mid 48
    b = mk("B", "E", "t", 0.1, 49, 55, "500")  # mid 52, same |mid-50|
    assert atm_strike([a, b]).ticker == "B"


def test_atm_strike_falls_back_to_volume_when_no_mids():
    a = mk("A", "E", "t", 0.0, None, 2, "10")
    b = mk("B", "E", "t", 0.1, None, 1, "500")
    assert atm_strike([a, b]).ticker == "B"


def test_tape_roundtrip_and_count(tmp_path):
    path = tmp_path / "run.jsonl"
    with Tape(path) as tape:
        tape.append({"schema": "test/1", "n": 1, "raw": '{"a": 1}'})
        tape.append({"schema": "test/1", "n": 2, "raw": "x\ny"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and tape.count == 2
    records = [json.loads(l) for l in lines]
    assert records[0]["raw"] == '{"a": 1}'   # verbatim through the roundtrip
    assert records[1]["raw"] == "x\ny"       # embedded newline survives escaping
