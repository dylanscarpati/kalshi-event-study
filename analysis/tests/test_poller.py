"""Tests for the release poller's pure scheduling logic."""

from datetime import datetime
from zoneinfo import ZoneInfo

from instrument_util import until_to_duration_s
from release_poller import next_tick, plan_tick, skip_penalty


def test_plan_tick_round_robins_and_substitutes_book():
    # 2 events, book every 5th tick (n % 5 == 4)
    kinds = [plan_tick(n, 2, 5) for n in range(10)]
    assert kinds[0] == ("ladder", 0)
    assert kinds[1] == ("ladder", 1)
    assert kinds[4] == ("book", 0)   # 5th tick is a book fetch, still one request
    assert kinds[9] == ("book", 1)


def test_plan_tick_book_disabled():
    assert all(plan_tick(n, 1, 0)[0] == "ladder" for n in range(20))


def test_next_tick_normal_progression():
    n, due = next_tick(now_s=100.4, start_s=100.0, tick_s=1.0, last_n=0)
    assert n == 1 and due == 101.0


def test_next_tick_skips_missed_slots_never_bursts():
    # Loop stalled 7.3 s: jump straight to the next future slot.
    n, due = next_tick(now_s=107.3, start_s=100.0, tick_s=1.0, last_n=1)
    assert n == 8 and due == 108.0
    assert due > 107.3


def test_skip_penalty_doubles_and_caps():
    seq = []
    p = 0
    for _ in range(6):
        p = skip_penalty(p)
        seq.append(p)
    assert seq == [1, 2, 4, 8, 8, 8]


def test_until_to_duration_same_day_and_rollover():
    eastern = ZoneInfo("America/New_York")
    morning = datetime(2026, 7, 14, 7, 0, tzinfo=eastern)
    assert until_to_duration_s("09:45", now=morning) == 2.75 * 3600
    evening = datetime(2026, 7, 14, 23, 0, tzinfo=eastern)
    assert until_to_duration_s("01:00", now=evening) == 2 * 3600  # tomorrow
