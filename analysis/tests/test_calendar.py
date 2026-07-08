"""Tests for release-calendar parsing and t0 construction."""

from datetime import datetime, timedelta, timezone

from release_calendar import (
    best_release,
    month_after,
    parse_alfred,
    parse_event_ticker,
    parse_fomc,
    release_candidates,
    t0_utc,
    validate,
)


def test_event_ticker_regex_both_eras():
    assert parse_event_ticker("CPI-21AUG") == ("CPI", "2021-08")
    assert parse_event_ticker("KXCPI-26MAR") == ("CPI", "2026-03")
    assert parse_event_ticker("KXCPIYOY-26NOV") == ("CPIYOY", "2026-11")
    assert parse_event_ticker("KXPAYROLLS-26AUG") == ("PAYROLLS", "2026-08")
    assert parse_event_ticker("KXFED-27JAN") == ("FED", "2027-01")
    assert parse_event_ticker("KXFEDDECISION-25SEP") == ("FEDDECISION", "2025-09")
    assert parse_event_ticker("FEDDECISION-24JAN31") == ("FEDDECISION", "2024-01")  # day-coded
    assert parse_event_ticker("KXBTCD-26JUL0805") is None
    assert parse_event_ticker("KXCPI-26MAR-T1.3") is None  # market, not event


def test_alfred_parser_takes_only_bare_date_lines():
    text = (
        "Name  Real Time Start  Period End\n"
        "Consumer Price Index  1949-03-24  2026-06-10\n"  # metadata row: excluded
        "\n2021-07-13\n2021-08-11\n 2021-09-14 \n2021-08-11\n"
    )
    assert parse_alfred(text) == ["2021-07-13", "2021-08-11", "2021-09-14"]


def test_release_mapping_m_plus_one_and_overrides():
    dates = ["2025-09-11", "2025-10-24", "2025-12-18", "2026-02-13"]
    assert month_after("2025-12") == "2026-01"
    assert release_candidates(dates, "CPI", "2025-08") == ["2025-09-11"]
    assert release_candidates(dates, "CPI", "2025-09") == ["2025-10-24"]  # shutdown, still M+1
    assert release_candidates(dates, "CPI", "2025-10") == []              # never released
    assert release_candidates(dates, "JOBS", "2025-09") == ["2025-11-20"]  # override (M+2)


def test_best_release_uses_close_to_disambiguate_revision_releases():
    # February holds the annual seasonal-revision release (Feb 9) AND the
    # January-CPI print (Feb 13); the market closed 8:25 ET on the 13th.
    candidates = ["2024-02-09", "2024-02-13"]
    close = t0_utc("2024-02-13", (8, 30)) - timedelta(minutes=5)
    assert best_release(candidates, close, (8, 30)) == "2024-02-13"
    # An early-era close (evening before the print) still picks the print.
    early_close = t0_utc("2024-02-13", (8, 30)) - timedelta(hours=13)
    assert best_release(candidates, early_close, (8, 30)) == "2024-02-13"


def test_validation_jitter_tolerance():
    t0 = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)
    assert validate(t0 - timedelta(minutes=4, seconds=2), t0) == "validated"  # observed jitter
    assert validate(t0 - timedelta(seconds=30), t0) == "validated"


def test_fomc_parser_dedupes_and_maps_month():
    html = (
        '<a href="/monetarypolicy/files/monetary20220615a1.pdf">PDF</a>'
        '<a href="/newsevents/pressreleases/monetary20220615a.htm">HTML</a>'
        '<a href="/newsevents/pressreleases/monetary20260617a.htm">HTML</a>'
    )
    cal = parse_fomc(html)
    assert cal["2022-06"] == "2022-06-15"
    assert cal["2026-06"] == "2026-06-17"


def test_t0_handles_dst_both_directions():
    # July: 8:30 ET = 12:30Z (EDT). January: 8:30 ET = 13:30Z (EST).
    assert t0_utc("2026-07-14", (8, 30)).strftime("%H:%M") == "12:30"
    assert t0_utc("2026-01-13", (8, 30)).strftime("%H:%M") == "13:30"
    assert t0_utc("2026-06-17", (14, 0)).strftime("%H:%M") == "18:00"


def test_validation_rules():
    t0 = datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc)
    assert validate(t0 - timedelta(minutes=5), t0) == "validated"
    assert validate(t0 - timedelta(minutes=1), t0) == "validated"
    assert validate(t0 - timedelta(hours=13, minutes=30), t0) == "early_close_era"
    assert validate(t0 + timedelta(minutes=10), t0).startswith("flagged")
    assert validate(t0 - timedelta(days=2), t0) == "early_close_era"  # within 72h window
    assert validate(t0 - timedelta(days=9), t0).startswith("flagged")  # shutdown pathology
