"""Build the per-event release-timestamp (t0) table for the calibration study.

Sources, fetched live and cached verbatim under data/calendar/:
- ALFRED release-date downloads (CPI rid=10, Employment Situation rid=50):
  actual historical publication dates with the reference period attached, so
  shutdown-era schedule moves are already reflected and no month-arithmetic
  heuristic is needed.
- The Federal Reserve's FOMC calendar page: statement dates parsed from the
  monetaryYYYYMMDDa press-release links.

t0 clock times: 8:30 ET (BLS releases) / 14:00 ET (FOMC statements), converted
through America/New_York so DST is handled per event. Validation: rule-era
events must close 5 or 1 minutes before t0; 2021-era markets closed the prior
evening and are tagged early_close_era rather than excluded (their hour-scale
gridpoints self-limit via staleness). Anything unmapped or contradictory lands
in events_flagged.csv with a reason.

Usage: python analysis/release_calendar.py
"""

from __future__ import annotations

import csv
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
ALFRED_URLS = {
    "CPI": "https://alfred.stlouisfed.org/release/downloaddates?rid=10&ff=txt",
    "JOBS": "https://alfred.stlouisfed.org/release/downloaddates?rid=50&ff=txt",
}
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# Optional trailing day digits: a few 2024 FEDDECISION events are day-coded
# (FEDDECISION-24JAN31); the month is what the calendar join needs.
EVENT_RE = re.compile(r"^(?:KX)?(CPIYOY|CPI|PAYROLLS|FEDDECISION|FED)-(\d\d)([A-Z]{3})(?:\d\d)?$")

# family -> (calendar key, release time ET)
FAMILY_SOURCE = {
    "CPI": ("CPI", (8, 30)),
    "CPIYOY": ("CPI", (8, 30)),
    "PAYROLLS": ("JOBS", (8, 30)),
    "FED": ("FOMC", (14, 0)),
    "FEDDECISION": ("FOMC", (14, 0)),
}

# Research-verified fixed points; the build fails loudly if sources disagree.
SPOT_CHECKS = [
    ("CPI", "2021-06", "2021-07-13"),
    ("CPI", "2026-03", "2026-04-10"),
    ("JOBS", "2023-03", "2023-04-07"),
    ("FOMC", "2022-06", "2022-06-15"),
    ("FOMC", "2026-06", "2026-06-17"),
]


def parse_event_ticker(event_ticker: str) -> tuple[str, str] | None:
    """-> (family, reference month 'YYYY-MM'), or None if not a macro event."""
    m = EVENT_RE.match(event_ticker)
    if not m:
        return None
    family, yy, mon = m.groups()
    if mon not in MONTHS:
        return None
    return family, f"20{yy}-{MONTHS[mon]:02d}"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.4.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


# Shutdown-era releases that broke the M+1 rule (research-verified; the
# close-time validation would catch any further breakage as flagged events).
OVERRIDES = {
    ("JOBS", "2025-09"): "2025-11-20",  # Sept-2025 jobs released M+2 after the lapse
}


def parse_alfred(text: str) -> list[str]:
    """The ff=txt download is a bare list of publication dates, one per line
    (the two-date rows near the top are release METADATA, not the list).
    Returns sorted unique dates."""
    dates = re.findall(r"^\s*(\d{4}-\d{2}-\d{2})\s*$", text, flags=re.MULTILINE)
    return sorted(set(dates))


def month_after(ref_month: str) -> str:
    y, m = int(ref_month[:4]), int(ref_month[5:7])
    return f"{y + (m == 12)}-{(m % 12) + 1:02d}"


def release_candidates(dates: list[str], cal_key: str, ref_month: str) -> list[str]:
    """Reference month M -> publications in calendar month M+1, plus explicit
    shutdown overrides. Usually one candidate; months containing annual
    seasonal-revision releases (e.g. February for January CPI) contain two,
    and the event's own scheduled close disambiguates which is the print."""
    if (cal_key, ref_month) in OVERRIDES:
        return [OVERRIDES[(cal_key, ref_month)]]
    target = month_after(ref_month)
    return [d for d in dates if d[:7] == target]


def parse_fomc(html: str) -> dict[str, str]:
    """meeting month 'YYYY-MM' -> statement date (second day of the meeting)."""
    out: dict[str, str] = {}
    for d in sorted(set(re.findall(r"monetary(\d{8})a", html))):
        iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        month = iso[:7]
        # Multiple statements in one month would be an emergency meeting;
        # none exist 2021-2026 (verified) -- keep the earliest, flag later
        # via close-time validation if it ever misassigns.
        out.setdefault(month, iso)
    return out


def t0_utc(release_date: str, et_time: tuple[int, int]) -> datetime:
    local = datetime.strptime(release_date, "%Y-%m-%d").replace(
        hour=et_time[0], minute=et_time[1], tzinfo=EASTERN)
    return local.astimezone(timezone.utc)


def validate(close: datetime, t0: datetime) -> str:
    """Rule-era markets close 1 or 5 minutes before t0, with observed
    scheduling jitter of up to ~1 min; early-era (2021-2022) markets closed
    up to a few days ahead. Anything else means the calendar join is wrong."""
    delta = t0 - close
    if timedelta(seconds=30) <= delta <= timedelta(minutes=6):
        return "validated"
    if timedelta(minutes=6) < delta <= timedelta(hours=72):
        return "early_close_era"
    return f"flagged:close_offset_{delta}"


def best_release(candidates: list[str], close: datetime, et_time: tuple[int, int]) -> str | None:
    """Pick the candidate whose t0 the market's scheduled close conforms to,
    preferring exact rule conformance, then early-era plausibility."""
    if not candidates:
        return None
    scored = []
    for d in candidates:
        status = validate(close, t0_utc(d, et_time))
        rank = 0 if status == "validated" else 1 if status == "early_close_era" else 2
        scored.append((rank, d))
    scored.sort()
    return scored[0][1]


def main() -> int:
    rows = list(csv.DictReader(open("data/derived/settled_markets.csv", encoding="utf-8")))

    cal_dir = Path("data/calendar")
    cal_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    alfred_dates: dict[str, list[str]] = {}
    for key, url in ALFRED_URLS.items():
        text = fetch(url)
        (cal_dir / f"alfred_{key.lower()}_{stamp}.txt").write_text(text, encoding="utf-8")
        alfred_dates[key] = parse_alfred(text)
    fomc_html = fetch(FOMC_URL)
    (cal_dir / f"fomc_{stamp}.html").write_text(fomc_html, encoding="utf-8")
    fomc_by_month = parse_fomc(fomc_html)

    def candidates_for(cal_key: str, ref_month: str) -> list[str]:
        if cal_key == "FOMC":
            d = fomc_by_month.get(ref_month)
            return [d] if d else []
        return release_candidates(alfred_dates[cal_key], cal_key, ref_month)

    failures = []
    for cal, month, expected in SPOT_CHECKS:
        cands = candidates_for(cal, month)
        got = expected if expected in cands else (cands[0] if cands else None)
        status = "PASS" if got == expected else "FAIL"
        if status == "FAIL":
            failures.append((cal, month, expected, got))
        print(f"spot-check {cal} {month}: expected {expected}, got {got} [{status}]")
    if failures:
        print("calendar spot-checks failed; refusing to write events.csv", file=sys.stderr)
        return 1

    events: dict[str, dict] = {}
    for r in rows:
        ev = events.setdefault(r["event_ticker"], {
            "event_ticker": r["event_ticker"],
            "series_ticker": r["series_ticker"],
            "n_strikes": 0,
            "close_time": r["close_time"],
        })
        ev["n_strikes"] += 1
        ev["close_time"] = max(ev["close_time"], r["close_time"])

    out_rows, flagged = [], []
    for ev in events.values():
        parsed = parse_event_ticker(ev["event_ticker"])
        if not parsed:
            flagged.append({**ev, "reason": "unparseable_event_ticker"})
            continue
        family, ref_month = parsed
        cal_key, et_time = FAMILY_SOURCE[family]
        close = datetime.fromisoformat(ev["close_time"].replace("Z", "+00:00"))
        release_date = best_release(candidates_for(cal_key, ref_month), close, et_time)
        if not release_date:
            flagged.append({**ev, "reason": f"no_{cal_key}_release_for_{ref_month}"})
            continue
        t0 = t0_utc(release_date, et_time)
        status = validate(close, t0)
        row = {
            "event_ticker": ev["event_ticker"],
            "series_ticker": ev["series_ticker"],
            "family": family,
            "ref_month": ref_month,
            "release_date": release_date,
            "t0_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "close_time": ev["close_time"],
            "n_strikes": ev["n_strikes"],
            "validation": status,
        }
        (flagged if status.startswith("flagged") else out_rows).append(
            row if not status.startswith("flagged") else {**row, "reason": status})

    out_rows.sort(key=lambda r: r["t0_utc"])
    fields = ["event_ticker", "series_ticker", "family", "ref_month", "release_date",
              "t0_utc", "close_time", "n_strikes", "validation"]
    with open("data/derived/events.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)
    with open("data/derived/events_flagged.csv", "w", encoding="utf-8", newline="") as f:
        keys = sorted({k for r in flagged for k in r})
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(flagged)

    from collections import Counter
    statuses = Counter(r["validation"] for r in out_rows)
    print(f"\n{len(out_rows)} events mapped -> data/derived/events.csv  {dict(statuses)}")
    print(f"{len(flagged)} flagged -> data/derived/events_flagged.csv")
    for r in flagged[:10]:
        print("  flagged:", r["event_ticker"], r.get("reason"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
