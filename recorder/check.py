"""Check recorded data against each exchange's own integrity information.

Usage (on the recording server):
    python check.py                  files written in the last 24 hours
    python check.py --hours 2        files written in the last 2 hours
    python check.py --date 2026-11-22
    python check.py --all --feed kraken

For every exchange it reports how connections ended, every integrity break it
finds, and the headline figure: the share of measured windows covered. A
measured window is the 60 seconds before a quarter hour, the seconds Kalshi
averages to settle its 15-minute markets. A window counts as covered only if
one connection spans all of it with a valid book and no break inside it.

Exit code 0 means no problem was found; 1 means at least one is listed.

This program only reads. It is separate from the recorder on purpose: a bug
here can be fixed and the check run again, while the recorder must not fail.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tape import is_complete, payload_of, read_tape

SECOND_NS = 10**9
QUARTER_NS = 900 * SECOND_NS
WINDOW_NS = 60 * SECOND_NS

PLANNED_ENDINGS = {"planned_reconnect", "requested_reconnect", "stopped"}
UNPLANNED_ENDINGS = {"stale", "disconnected", "error"}


@dataclass
class FileResult:
    """What one file (one connection) showed."""

    name: str
    start_ns: int | None = None  # first record
    end_ns: int | None = None  # last record
    ready_ns: int | None = None  # when the first full book arrived
    # Change-only feeds: from this moment the rebuilt book is wrong until the
    # file ends, because only a new connection brings a new full book.
    invalid_from_ns: int | None = None
    # Full-book feeds: moments at which a message was missing; the next
    # message repairs the book, so only these moments are spoiled.
    break_ns: list[int] = field(default_factory=list)
    ended: str = ""
    unplanned: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[tuple[int, str]] = field(default_factory=list)

    def count(self, name: str, by: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + by

    def problem(self, wall_ns: int, text: str) -> None:
        self.problems.append((wall_ns, text))


def measured_windows(start_ns: int, end_ns: int) -> range:
    """The quarter-hour times q whose window [q - 60 s, q] lies in [start, end]."""
    first = -(-(start_ns + WINDOW_NS) // QUARTER_NS) * QUARTER_NS  # rounded up
    return range(first, end_ns + 1, QUARTER_NS)


def window_covered(q: int, files: list[FileResult]) -> bool:
    """True if one file holds a valid book from q - 60 s to q with no break."""
    start = q - WINDOW_NS
    for f in files:
        if f.ready_ns is None or f.ready_ns > start or f.end_ns is None or f.end_ns < q:
            continue
        if f.invalid_from_ns is not None and f.invalid_from_ns <= q:
            continue
        if any(start <= moment <= q for moment in f.break_ns):
            continue
        return True
    return False


# --------------------------------------------------------------------------
# A sorted side of an order book, for the feeds whose book must be rebuilt
# --------------------------------------------------------------------------


class Side:
    """Price levels of one side, kept sorted. Prices are Decimals, so "57748.0"
    and "57748.00000" are the same level; the original text is kept too,
    because Kraken's checksum is computed from the text."""

    def __init__(self, is_bid: bool) -> None:
        self.is_bid = is_bid
        self.levels: dict[
            Decimal, tuple[str, str]
        ] = {}  # price -> (price text, size text)
        self.prices: list[Decimal] = []  # ascending

    def set(self, price: str, size: str) -> None:
        key = Decimal(price)
        if Decimal(size) == 0:
            if key in self.levels:
                del self.levels[key]
                del self.prices[bisect.bisect_left(self.prices, key)]
            return
        if key not in self.levels:
            bisect.insort(self.prices, key)
        self.levels[key] = (price, size)

    def best(self, n: int) -> list[Decimal]:
        """The n best prices: highest first for bids, lowest first for asks."""
        return self.prices[: -n - 1 : -1] if self.is_bid else self.prices[:n]

    def keep_best(self, n: int) -> None:
        """Drop all but the n best levels."""
        while len(self.prices) > n:
            del self.levels[self.prices.pop(0) if self.is_bid else self.prices.pop()]


# --------------------------------------------------------------------------
# One checker per exchange
# --------------------------------------------------------------------------


class FeedCheck:
    """Shared behaviour. A subclass looks at messages (and, where useful,
    recorder events) and reports breaks through broken()."""

    # True for feeds that send changes after one full book: a break spoils
    # the rest of the file. False for feeds whose every message is a full book.
    change_only = True
    note = ""

    def __init__(self, result: FileResult) -> None:
        self.result = result

    def broken(self, wall_ns: int, text: str) -> None:
        self.result.problem(wall_ns, text)
        if not self.change_only:
            self.result.break_ns.append(wall_ns)
        elif self.result.invalid_from_ns is None:
            self.result.invalid_from_ns = wall_ns

    def ready(self, wall_ns: int) -> None:
        if self.result.ready_ns is None:
            self.result.ready_ns = wall_ns

    def message(self, wall_ns: int, raw: str) -> None:
        raise NotImplementedError

    def event(self, wall_ns: int, kind: str, record: dict) -> None:
        pass

    def finish(self) -> None:
        pass


class CoinbaseCheck(FeedCheck):
    """Every message carries sequence_num, counting from 0 on each connection."""

    def __init__(self, result):
        super().__init__(result)
        self.expected = 0

    def message(self, wall_ns, raw):
        m = json.loads(raw)
        seq = m.get("sequence_num")
        if seq is None:
            return
        if seq != self.expected:
            self.broken(
                wall_ns, f"sequence number {self.expected} expected, {seq} received"
            )
        self.expected = seq + 1
        if m.get("channel") == "l2_data" and any(
            e.get("type") == "snapshot" for e in m.get("events", [])
        ):
            self.ready(wall_ns)


class KrakenCheck(FeedCheck):
    """Rebuild the book to depth 1000 and compare Kraken's checksum of the
    top ten levels a side after every book message."""

    DEPTH = 1000

    def __init__(self, result):
        super().__init__(result)
        self.bids, self.asks = Side(True), Side(False)
        self.have_book = False

    def checksum(self) -> int:
        def digits(text):
            return text.replace(".", "").lstrip("0")

        parts = [
            digits(self.asks.levels[p][0]) + digits(self.asks.levels[p][1])
            for p in self.asks.best(10)
        ]
        parts += [
            digits(self.bids.levels[p][0]) + digits(self.bids.levels[p][1])
            for p in self.bids.best(10)
        ]
        return zlib.crc32("".join(parts).encode("ascii")) & 0xFFFFFFFF

    def message(self, wall_ns, raw):
        m = json.loads(raw, parse_float=str)  # keep prices as their exact text
        if m.get("channel") != "book":
            return
        data = m["data"][0]
        if m.get("type") == "snapshot":
            self.bids, self.asks = Side(True), Side(False)
            self.have_book = True
            self.ready(wall_ns)
        if not self.have_book or self.result.invalid_from_ns is not None:
            return
        for level in data.get("bids", []):
            self.bids.set(str(level["price"]), str(level["qty"]))
        for level in data.get("asks", []):
            self.asks.set(str(level["price"]), str(level["qty"]))
        self.bids.keep_best(self.DEPTH)
        self.asks.keep_best(self.DEPTH)
        if self.checksum() == int(data["checksum"]):
            self.result.count("checksums_matched")
        else:
            self.broken(wall_ns, "checksum of the rebuilt book does not match Kraken's")


class BitstampCheck(FeedCheck):
    """The feed has no sequence numbers or checksums. Only the order of the
    changes and the full-book downloads can be checked."""

    note = "no integrity data on this feed; book correctness is checked later"

    def __init__(self, result):
        super().__init__(result)
        self.last = None

    def message(self, wall_ns, raw):
        m = json.loads(raw)
        if m.get("event") != "data":
            return
        micro = int(m["data"]["microtimestamp"])
        if self.last is not None and micro <= self.last:
            self.broken(wall_ns, "changes arrived out of time order")
        self.last = micro
        self.result.count("changes")

    def event(self, wall_ns, kind, record):
        if kind == "rest_snapshot":
            self.result.count("book_downloads")
            self.ready(wall_ns)
        elif kind == "rest_snapshot_failed":
            self.result.count("book_download_failures")


class GeminiCheck(FeedCheck):
    """Consecutive book messages overlap: U equals the previous u. Once a
    second, the top five levels are compared with the rebuilt book."""

    def __init__(self, result):
        super().__init__(result)
        self.bids, self.asks = Side(True), Side(False)
        self.last_u = None

    def message(self, wall_ns, raw):
        m = json.loads(raw)
        if m.get("e") == "depthUpdate":
            first, last = int(m["U"]), int(m["u"])
            if self.last_u is None:  # the first one is the full book
                self.ready(wall_ns)
            elif last <= self.last_u:
                self.result.count("stale_updates_skipped")
                return
            elif first > self.last_u:
                self.broken(
                    wall_ns, f"update numbers {self.last_u + 1} to {first - 1} missing"
                )
            for price, size in m["b"]:
                self.bids.set(price, size)
            for price, size in m["a"]:
                self.asks.set(price, size)
            self.last_u = last
        elif "lastUpdateId" in m and int(m["lastUpdateId"]) == self.last_u:
            if self.result.invalid_from_ns is not None:
                return
            self.result.count("top_five_compared")
            expected_bids = [(Decimal(p), Decimal(q)) for p, q in m["bids"]]
            expected_asks = [(Decimal(p), Decimal(q)) for p, q in m["asks"]]
            rebuilt_bids = [
                (p, Decimal(self.bids.levels[p][1])) for p in self.bids.best(5)
            ]
            rebuilt_asks = [
                (p, Decimal(self.asks.levels[p][1])) for p in self.asks.best(5)
            ]
            if rebuilt_bids == expected_bids and rebuilt_asks == expected_asks:
                self.result.count("top_five_matched")
            else:
                self.broken(wall_ns, "rebuilt top five differs from Gemini's snapshot")


class CryptocomCheck(FeedCheck):
    """Each update names the previous one (pu equals the previous u); every
    heartbeat must have been answered."""

    def __init__(self, result):
        super().__init__(result)
        self.last_u = None

    def message(self, wall_ns, raw):
        m = json.loads(raw)
        if m.get("method") == "public/heartbeat":
            self.result.count("heartbeats_received")
            return
        if not m.get("result"):
            return
        data = m["result"]["data"][0]
        if m["result"]["channel"] == "book":
            self.ready(wall_ns)
        elif self.last_u is None or int(data["pu"]) != self.last_u:
            self.broken(wall_ns, "an update does not follow the previous one (pu)")
        self.last_u = int(data["u"])

    def event(self, wall_ns, kind, record):
        if kind == "sent" and "respond-heartbeat" in record.get("raw", ""):
            self.result.count("heartbeats_answered")

    def finish(self):
        received = self.result.counts.get("heartbeats_received", 0)
        answered = self.result.counts.get("heartbeats_answered", 0)
        if answered < received:
            self.result.problem(
                self.result.end_ns or 0,
                f"{received - answered} heartbeat(s) not answered",
            )


class BullishCheck(FeedCheck):
    """Every message is a full book; sequence ranges must join up."""

    change_only = False

    def __init__(self, result):
        super().__init__(result)
        self.last_upper = None

    def message(self, wall_ns, raw):
        m = json.loads(raw)
        if m.get("type") != "snapshot":
            return
        lower, upper = (int(n) for n in m["data"]["sequenceNumberRange"])
        if self.last_upper is not None and lower != self.last_upper + 1:
            self.broken(
                wall_ns,
                f"sequence numbers {self.last_upper + 1} to {lower - 1} missing",
            )
        self.last_upper = upper
        self.ready(wall_ns)


class LmaxCheck(FeedCheck):
    """A full book every second; the whole-second timestamp must rise by one."""

    change_only = False

    def __init__(self, result):
        super().__init__(result)
        self.last_second = None

    def message(self, wall_ns, raw):
        m = json.loads(raw)
        if m.get("type") != "ORDER_BOOK":
            return
        second = int(
            datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00")).timestamp()
        )
        if self.last_second is not None and second != self.last_second + 1:
            if second > self.last_second:
                self.broken(
                    wall_ns,
                    f"{second - self.last_second - 1} second(s) skipped before {m['timestamp']}",
                )
            else:
                self.broken(
                    wall_ns, f"book for {m['timestamp']} repeated or out of order"
                )
        self.last_second = second
        if m.get("status") != "OPEN":
            self.result.count("books_not_open")
        self.ready(wall_ns)


# The integrity evidence printed for each feed: (count name, what it means).
EVIDENCE = {
    "coinbase": [],
    "kraken": [("checksums_matched", "Kraken checksums matched")],
    "bitstamp": [("changes", "changes in time order"), ("book_downloads", "full books downloaded"),
                 ("book_download_failures", "downloads failed")],
    "gemini": [("top_five_matched", "top-five snapshots matched"), ("top_five_compared", "compared")],
    "cryptocom": [("heartbeats_answered", "heartbeats answered"), ("heartbeats_received", "received")],
    "bullish": [],
    "lmax": [("books_not_open", "books with the market not open")],
}

CHECKS = {
    "coinbase": CoinbaseCheck,
    "kraken": KrakenCheck,
    "bitstamp": BitstampCheck,
    "gemini": GeminiCheck,
    "cryptocom": CryptocomCheck,
    "bullish": BullishCheck,
    "lmax": LmaxCheck,
}


def check_file(feed: str, path: Path, newest: bool) -> FileResult:
    """Check one file. newest marks the file the recorder may still be writing."""
    result = FileResult(name=path.name)
    checker = CHECKS[feed](result)
    for record in read_tape(path):
        wall_ns = record["wall_ns"]
        if result.start_ns is None:
            result.start_ns = wall_ns
        result.end_ns = wall_ns
        if "event" in record:
            kind = record["event"]
            if kind in PLANNED_ENDINGS or kind in UNPLANNED_ENDINGS:
                result.ended = kind
            checker.event(wall_ns, kind, record)
            continue
        result.count("messages")
        payload = payload_of(record)
        if isinstance(payload, bytes):
            checker.broken(wall_ns, "a message is not valid text")
            continue
        try:
            checker.message(wall_ns, payload)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
            checker.broken(wall_ns, f"a message could not be read ({error!r})")
    checker.finish()
    result.unplanned = result.ended in UNPLANNED_ENDINGS
    if not newest and not is_complete(path):
        result.problem(
            result.end_ns or 0, "file cut off (the recorder was stopped abruptly)"
        )
    return result


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def utc(wall_ns: int) -> str:
    return datetime.fromtimestamp(wall_ns / SECOND_NS, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def select_files(feed_dir: Path, args) -> list[Path]:
    files = sorted(feed_dir.glob("*/*.jsonl.gz"))
    if args.date:
        return [f for f in files if f.parent.name == args.date]
    if args.all:
        return files
    since = time.time() - args.hours * 3600
    return [f for f in files if f.stat().st_mtime >= since]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check recorded order-book data.")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--hours",
        type=float,
        default=24,
        help="files written in the last N hours (default 24)",
    )
    group.add_argument("--date", help="files started on this UTC date, YYYY-MM-DD")
    group.add_argument("--all", action="store_true", help="every file")
    parser.add_argument(
        "--feed", action="append", choices=sorted(CHECKS), help="only these feeds"
    )
    args = parser.parse_args(argv)

    feeds = args.feed or sorted(
        p.name for p in args.data_dir.iterdir() if p.name in CHECKS
    )
    rows, problems = [], []
    for feed in feeds:
        everything = sorted((args.data_dir / feed).glob("*/*.jsonl.gz"))
        selected = select_files(args.data_dir / feed, args)
        if not selected:
            rows.append((feed, 0, 0, 0, "no files", CHECKS[feed].note))
            continue
        results = [check_file(feed, f, newest=(f == everything[-1])) for f in selected]
        start = min(r.start_ns for r in results if r.start_ns is not None)
        end = max(r.end_ns for r in results if r.end_ns is not None)
        windows = list(measured_windows(start, end))
        covered = sum(window_covered(q, results) for q in windows)
        share = f"{covered}/{len(windows)}" + (
            f" ({100 * covered / len(windows):.1f}%)" if windows else ""
        )
        messages = sum(r.counts.get("messages", 0) for r in results)
        unplanned = sum(r.unplanned for r in results)
        totals: dict[str, int] = {}
        for r in results:
            for name, value in r.counts.items():
                totals[name] = totals.get(name, 0) + value
        evidence = ", ".join(f"{totals.get(name, 0):,} {label}" for name, label in EVIDENCE[feed])
        note = "; ".join(text for text in (evidence, CHECKS[feed].note) if text)
        rows.append((feed, len(results), messages, unplanned, share, note))
        for r in results:
            problems += [(feed, wall, r.name, text) for wall, text in r.problems]

    print(f"Recording check of {args.data_dir}")
    print(
        f"{'feed':<10} {'files':>5} {'messages':>12} {'unplanned':>9}  windows covered"
    )
    for feed, files, messages, unplanned, share, note in rows:
        print(
            f"{feed:<10} {files:>5} {messages:>12,} {unplanned:>9}  {share}"
            + (f"  ({note})" if note else "")
        )
    if problems:
        print(f"\n{len(problems)} problem(s):")
        shown: dict[str, int] = {}
        for feed, wall, name, text in sorted(problems, key=lambda p: (p[0], p[1])):
            shown[feed] = shown.get(feed, 0) + 1
            if shown[feed] <= 10:
                print(f"  {feed:<10} {utc(wall)}  {text}  [{name}]")
            elif shown[feed] == 11:
                print(f"  {feed:<10} ... more not shown")
        return 1
    print("\nNo problems found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
