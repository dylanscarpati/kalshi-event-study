"""REST polling instrument for scheduled-release mornings.

Round-robins a watchlist of event tickers at a fixed grid rate (default
1 Hz), taping every raw ladder response; every book-every-th tick fetches
the ATM strike's orderbook instead, so the request rate never exceeds one
per tick. The grid lives on the monotonic clock and skips missed slots
rather than bursting to catch up -- a burst at 08:30 is rate-limit abuse
at the worst possible moment.

Failure discipline, in two phases: setup (discovery) fails loud -- a
misconfigured instrument should die at 07:05, not 08:29. The capture loop
never exits: transport errors become taped request_error records and the
next grid tick is the retry; 429 responses skip 1, 2, 4... ticks (capped);
after 5 consecutive transport errors the HTTP session is rebuilt. Parsing
is best-effort inside the loop -- the raw response is already on tape
before any parse runs.

Usage:
  python analysis/release_poller.py --events KXCPI-26JUN KXFED-26JUL --duration-min 90
  python analysis/release_poller.py --series KXCPI --until 09:45
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

from instrument_util import git_hash, inhibit_sleep, restore_sleep, stamp, until_to_duration_s
from kalshi_rest import get, parse_market
from market_discovery import atm_strike, event_ladder, fetch_open_markets, nearest_event
from tape import Tape, new_run_id, rest_record

SCHEMA = "release_poller/1"
MAX_SKIP_PENALTY = 8
SESSION_REBUILD_AFTER = 5


def plan_tick(n: int, n_events: int, book_every: int) -> tuple[str, int]:
    """What tick n does: ("ladder"|"book", event_index). One request per
    tick, always -- the book fetch replaces the ladder fetch, never adds."""
    kind = "book" if book_every > 0 and n % book_every == book_every - 1 else "ladder"
    return kind, n % n_events


def next_tick(now_s: float, start_s: float, tick_s: float, last_n: int) -> tuple[int, float]:
    """Next tick index and its absolute monotonic deadline. If the loop
    fell behind, skip the missed slots (never burst)."""
    n = last_n + 1
    due = start_s + n * tick_s
    if now_s > due:
        n = int((now_s - start_s) / tick_s) + 1
        due = start_s + n * tick_s
    return n, due


def skip_penalty(previous: int) -> int:
    """429 backoff in units of grid ticks: 1, 2, 4 ... capped."""
    return min(max(1, previous * 2), MAX_SKIP_PENALTY)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", nargs="+", help="event tickers to poll round-robin")
    parser.add_argument("--series", help="discover the nearest open event of this series")
    parser.add_argument("--tick-sec", type=float, default=1.0)
    parser.add_argument("--book-every", type=int, default=10, help="every Nth tick fetches the ATM book")
    parser.add_argument("--book-depth", type=int, default=10)
    parser.add_argument("--duration-min", type=float, default=90.0)
    parser.add_argument("--until", help="stop at HH:MM Eastern (overrides --duration-min)")
    parser.add_argument("--out-dir", type=Path, default=Path("data/poller"))
    args = parser.parse_args()

    run_id = new_run_id()
    session = requests.Session()

    # Setup phase: fail loud.
    if args.events:
        events = list(args.events)
        setup_records = []
    elif args.series:
        setup_records = []
        markets = fetch_open_markets(session, args.series, setup_records.append)
        if not markets:
            print(f"no open markets for series {args.series!r}", file=sys.stderr)
            return 2
        events = [nearest_event(markets)]
    else:
        print("need --events or --series", file=sys.stderr)
        return 2

    duration_s = until_to_duration_s(args.until) if args.until else args.duration_min * 60.0

    tape = Tape(args.out_dir / f"{run_id}.jsonl")
    for r in setup_records:
        tape.append(rest_record(SCHEMA, run_id, r))
    tape.append(
        {
            "schema": SCHEMA,
            "run_id": run_id,
            "kind": "run_start",
            **stamp(),
            "argv": sys.argv[1:],
            "git_hash": git_hash(),
            "events": events,
            "tick_sec": args.tick_sec,
            "book_every": args.book_every,
            "book_depth": args.book_depth,
            "duration_s": duration_s,
        }
    )
    print(f"polling {events} every {args.tick_sec}s for {duration_s/60:.1f} min -> {tape.path}")

    ladders: dict[str, list] = {}
    inhibit_sleep()
    start = time.monotonic()
    n = -1
    penalty = 0
    skip_until_n = -1
    consecutive_errors = 0
    ok_count = err_count = 0

    try:
        # Capture phase: never exits before the deadline.
        while True:
            now = time.monotonic()
            if now - start >= duration_s:
                break
            n, due = next_tick(now, start, args.tick_sec, n)
            time.sleep(max(0.0, due - time.monotonic()))

            if n <= skip_until_n:
                continue

            kind, idx = plan_tick(n, len(events), args.book_every)
            event = events[idx]
            ladder = ladders.get(event)
            if kind == "book" and ladder:
                target = atm_strike(ladder)
                path, params = f"/markets/{target.ticker}/orderbook", {"depth": args.book_depth}
            else:
                kind = "ladder"
                path, params = "/markets", {"event_ticker": event, "limit": 200}

            try:
                resp = get(session, path, params)
            except requests.RequestException as exc:
                err_count += 1
                consecutive_errors += 1
                tape.append(
                    {
                        "schema": SCHEMA,
                        "run_id": run_id,
                        "kind": "request_error",
                        **stamp(),
                        "tick": n,
                        "target": event,
                        "fetch": kind,
                        "error": repr(exc),
                    }
                )
                if consecutive_errors >= SESSION_REBUILD_AFTER:
                    session.close()
                    session = requests.Session()
                    consecutive_errors = 0
                    tape.append(
                        {"schema": SCHEMA, "run_id": run_id, "kind": "session_rebuild", **stamp(), "tick": n}
                    )
                continue

            tape.append(
                {**rest_record(SCHEMA, run_id, resp), "kind": "response", "tick": n, "target": event, "fetch": kind}
            )
            consecutive_errors = 0

            if resp.http_status == 429:
                penalty = skip_penalty(penalty)
                skip_until_n = n + penalty
                tape.append(
                    {"schema": SCHEMA, "run_id": run_id, "kind": "throttle_skip", **stamp(), "tick": n, "skip_ticks": penalty}
                )
                continue
            if resp.http_status != 200:
                err_count += 1
                continue

            penalty = 0
            ok_count += 1
            if kind == "ladder":
                try:
                    markets = [parse_market(m) for m in resp.json()["markets"]]
                    ladders[event] = event_ladder(markets, event) or markets
                except Exception as exc:  # raw already taped; parsing is best-effort here
                    tape.append(
                        {"schema": SCHEMA, "run_id": run_id, "kind": "parse_error", **stamp(), "tick": n, "error": repr(exc)}
                    )
    except KeyboardInterrupt:
        print("\ninterrupted; closing tape cleanly", file=sys.stderr)
    finally:
        tape.append(
            {"schema": SCHEMA, "run_id": run_id, "kind": "run_end", **stamp(), "ok": ok_count, "errors": err_count, "ticks": n + 1}
        )
        tape.close()
        restore_sleep()

    print(f"done: {ok_count} ok responses, {err_count} errors, {tape.count} records -> {tape.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
