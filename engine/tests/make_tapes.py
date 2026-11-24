#!/usr/bin/env python3
"""Write the synthetic test tapes, and the output the engine must produce from them.

    python3 engine/tests/make_tapes.py           writes engine/tests/tapes/
    python3 engine/tests/make_tapes.py --check   checks that the committed files are what
                                                 this script writes, and that every tape in
                                                 the repository is synthetic

Each exchange gets a made-up book that changes at random (with a fixed seed, so every run writes
the same tapes). The book is written in that exchange's real message format (see
notes/research/feed-formats.md), with the faults the engine must handle: missed messages, a
wrong checksum, a stale update, phantom levels, a reconnect, a clock step, a file cut off by a
crash and a file with damaged bytes at its end. The tapes cover 08:13:50 to 08:15:30 UTC, which
contains one measured window (08:14:00 to 08:15:00).

Because this script knows the true book, it also writes what the replay must output: every
second's samples (expected.csv), what each file showed (expected_files.ndjson) and the CSV's
FNV-1a hash (expected.fnv). That makes it the reference the engine is tested against (decision 5
in the project log). The rules for the engine's states mirror engine/src/replay.cpp.

Nothing here is recorded data. Every tape starts with a "synthetic" event, and --check rejects
any committed tape without one.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import io
import json
import random
import subprocess
import sys
import time
import zlib
from decimal import Decimal
from pathlib import Path

SECOND = 10**9
MS = 10**6
START = 1_795_248_830 * SECOND  # 2026-11-21T08:13:50Z
END_MS = 100_000  # the tapes end 100 s later, at 08:15:30Z
BOOT = (
    1_795_000_000 * SECOND
)  # wall clock minus monotonic clock: when the "machine" started
FEEDS = ["coinbase", "kraken", "bitstamp", "gemini", "cryptocom", "bullish", "lmax"]
CLOSING = {
    "planned_reconnect",
    "requested_reconnect",
    "stopped",
    "stale",
    "disconnected",
    "error",
}
UNPLANNED = {"stale", "disconnected", "error"}
CSV_HEADER = (
    "second,feed,state,bid,bid_qty,ask,ask_qty,bid_levels,ask_levels,crossed,gap,clock_step,"
    "last_wall_ns,last_exchange_ns\n"
)
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def at(ms: int) -> int:
    """The wall-clock time `ms` milliseconds after START, in nanoseconds."""
    return START + ms * MS


def iso(ns: int, decimals: int) -> str:
    """A UTC timestamp with exactly `decimals` decimals (0 for none) and a final Z."""
    seconds, rest = divmod(ns, SECOND)
    text = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
    if decimals:
        text += "." + f"{rest:09d}"[:decimals]
    return text + "Z"


def text(value: Decimal, places: int) -> str:
    """A decimal with exactly `places` decimals, as the exchanges write them."""
    return f"{value:.{places}f}"


def shortest(value: Decimal) -> str:
    """The shortest exact text for a value, as append_fixed8 in engine/src/fixed.cpp writes it."""
    return "0" if value == 0 else format(value.normalize(), "f")


# ---------------------------------------------------------------------------------------------
# A made-up order book
# ---------------------------------------------------------------------------------------------


class TrueBook:
    """Price -> size on each side, changed at random near the best prices."""

    def __init__(self, rng: random.Random, mid: str, tick: str, levels: int, unit: str):
        self.rng = rng
        self.tick = Decimal(tick)
        self.unit = Decimal(unit)
        middle = Decimal(mid)
        self.bids = {middle - self.tick * (i + 3): self.size() for i in range(levels)}
        self.asks = {middle + self.tick * (i + 3): self.size() for i in range(levels)}

    def size(self) -> Decimal:
        return self.unit * (1 + int(self.rng.random() * 5000))

    def best_first(self, side: str) -> list[tuple[Decimal, Decimal]]:
        return sorted(getattr(self, side).items(), reverse=(side == "bids"))

    def change(self, away: int = 0, near: int = 8) -> tuple[str, Decimal, Decimal]:
        """Changes one level `away` to `near` ticks from the best price of a random side. Returns
        (side, price, new size); a size of zero means the level was removed. With away > 0 the
        best prices never move."""
        side = "bids" if self.rng.random() < 0.5 else "asks"
        levels = getattr(self, side)
        best_bid, best_ask = max(self.bids), min(self.asks)
        ticks = away + int(self.rng.random() * (near - away))
        improve = away == 0 and self.rng.random() < 0.15
        if side == "bids":
            price = best_bid - self.tick * ticks
            if improve and best_bid + self.tick < best_ask:
                price = best_bid + self.tick
        else:
            price = best_ask + self.tick * ticks
            if improve and best_ask - self.tick > best_bid:
                price = best_ask - self.tick
        if price in levels and self.rng.random() < 0.3 and len(levels) > 5:
            del levels[price]
            return side, price, Decimal(0)
        levels[price] = self.size()
        return side, price, levels[price]


# ---------------------------------------------------------------------------------------------
# One tape, and what the engine should hold after each of its records
# ---------------------------------------------------------------------------------------------


class Tape:
    """One file (one connection). After each record it notes the state the engine should show:
    the same fields engine/src/replay.cpp reads to make a sample."""

    def __init__(self, feed: str, start_ms: int, change_only: bool):
        self.feed = feed
        self.change_only = change_only
        stamp = iso(at(start_ms), 0).replace("-", "").replace(":", "")
        self.name = f"{feed}_{stamp}.jsonl.gz"
        self.lines: list[str] = []
        self.walls: list[int] = []
        self.states: list[tuple] = []
        self.mono_offset = BOOT
        self.step_pending = False
        # What the engine's Integrity should hold.
        self.ready_ns: int | None = None
        self.invalid_ns: int | None = None
        self.closed_ns: int | None = None
        self.breaks: list[int] = []
        self.steps: list[int] = []
        self.ended = ""
        self.counts: dict[str, int] = {}
        self.problems: list[tuple[int, str]] = []
        # The engine's book (best first) and the other fields of a sample.
        self.bids: list[tuple[Decimal, Decimal]] = []
        self.asks: list[tuple[Decimal, Decimal]] = []
        self.open = True
        self.last_wall = 0
        self.last_exchange = 0
        # Faults of the file itself.
        self.cut_off = (
            False  # the file ends after its last record, without the gzip end
        )
        self.junk = False  # damaged bytes follow its last record
        self._write(
            at(start_ms) - MS,
            {"event": "synthetic", "generator": "engine/tests/make_tapes.py"},
        )

    def count(self, name: str, by: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + by

    def step_clock(self, by_ns: int) -> None:
        """The machine's clock is corrected: from the next record on, wall - mono changes."""
        self.mono_offset += by_ns
        self.step_pending = True

    def message(
        self, wall: int, raw: str, applied: bool = False, exchange_ns: int = 0
    ) -> None:
        self.count("messages")
        if applied:
            self.last_wall = wall
            if exchange_ns:
                self.last_exchange = exchange_ns
        self._write(wall, {"raw": raw})

    def event(self, wall: int, kind: str, **details) -> None:
        if kind in CLOSING:
            self.ended = kind
            if self.closed_ns is None:
                self.closed_ns = wall
        self._write(wall, {"event": kind, **details})

    def set_book(self, bids, asks) -> None:
        self.bids, self.asks = list(bids), list(asks)

    def ready(self, wall: int) -> None:
        if self.ready_ns is None:
            self.ready_ns = wall

    def broken(self, wall: int, problem: str) -> None:
        self.problems.append((wall, problem))
        if not self.change_only:
            self.breaks.append(wall)
        elif self.invalid_ns is None:
            self.invalid_ns = wall

    def _write(self, wall: int, body: dict) -> None:
        if self.step_pending:
            self.steps.append(wall)
            self.count("clock_steps")
            self.step_pending = False
        record = {"wall_ns": wall, "mono_ns": wall - self.mono_offset, **body}
        self.lines.append(json.dumps(record, separators=(",", ":")))
        self.walls.append(wall)
        best_bid = self.bids[0] if self.bids else None
        best_ask = self.asks[0] if self.asks else None
        crossed = bool(best_bid and best_ask and best_bid[0] >= best_ask[0])
        self.states.append(
            (
                best_bid,
                best_ask,
                len(self.bids),
                len(self.asks),
                crossed,
                self.ready_ns is not None,
                self.invalid_ns is not None,
                self.open,
                self.last_wall,
                self.last_exchange,
            )
        )

    def gzip_bytes(self) -> bytes:
        raw = io.BytesIO()
        gz = gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0)
        for line in self.lines:
            gz.write((line + "\n").encode())
        if self.cut_off or self.junk:
            # As a crash leaves it: everything flushed, the gzip end never written.
            gz.flush(zlib.Z_SYNC_FLUSH)
            data = raw.getvalue()
            return data + (b"\0" * 64 if self.junk else b"")
        gz.close()
        return raw.getvalue()


# ---------------------------------------------------------------------------------------------
# The seven exchanges. Each returns its tapes, oldest first.
# ---------------------------------------------------------------------------------------------


def coinbase(rng: random.Random) -> list[Tape]:
    """Two connections: a clock step in the first, which ends with a planned reconnect after
    the measured window; a missed message (a sequence jump) in the second."""
    book = TrueBook(rng, "57700.00", "0.01", 40, "0.0001")
    tapes = []

    def message_text(seq: int, wall: int, kind: str, levels) -> str:
        stamp = iso(wall - 50 * MS, 9)
        updates = [
            {
                "side": side,
                "event_time": iso(wall - 60 * MS, 6),
                "price_level": text(price, 2),
                "new_quantity": "0" if qty == 0 else text(qty, 8),
            }
            for side, price, qty in levels
        ]
        return json.dumps(
            {
                "channel": "l2_data",
                "timestamp": stamp,
                "sequence_num": seq,
                "events": [{"type": kind, "product_id": "BTC-USD", "updates": updates}],
            },
            separators=(",", ":"),
        )

    def connection(
        start_ms: int, end_ms: int | None, closing: str, step_ms=None, jump_ms=None
    ):
        tape = Tape("coinbase", start_ms, change_only=True)
        tape.event(
            at(start_ms), "connected", url="wss://advanced-trade-ws.coinbase.com"
        )
        tape.event(
            at(start_ms) + MS,
            "sent",
            raw='{"type":"subscribe","product_ids":["BTC-USD"],"channel":"level2"}',
        )
        tape.event(
            at(start_ms) + 2 * MS,
            "sent",
            raw='{"type":"subscribe","channel":"heartbeats"}',
        )
        wall = at(start_ms + 400)
        levels = [("bid", p, q) for p, q in book.best_first("bids")]
        levels += [("offer", p, q) for p, q in book.best_first("asks")]
        tape.set_book(book.best_first("bids"), book.best_first("asks"))
        tape.ready(wall)
        tape.message(
            wall, message_text(0, wall, "snapshot", levels), True, wall - 50 * MS
        )
        seq = 1
        for _ in range(2):  # the subscription replies come after the book
            wall += MS
            tape.message(
                wall,
                json.dumps(
                    {
                        "channel": "subscriptions",
                        "timestamp": iso(wall, 9),
                        "sequence_num": seq,
                        "events": [{"subscriptions": {"level2": ["BTC-USD"]}}],
                    },
                    separators=(",", ":"),
                ),
            )
            seq += 1
        ms = start_ms + 600
        while ms < end_ms:
            if step_ms is not None and ms >= step_ms:
                tape.step_clock(200 * MS)
                step_ms = None
            wall = at(ms)
            if ms % 1000 < 250:  # a heartbeat once a second
                tape.message(
                    wall,
                    json.dumps(
                        {
                            "channel": "heartbeats",
                            "timestamp": iso(wall, 9),
                            "sequence_num": seq,
                            "events": [
                                {
                                    "current_time": iso(wall, 9),
                                    "heartbeat_counter": ms // 1000,
                                }
                            ],
                        },
                        separators=(",", ":"),
                    ),
                )
                seq += 1
            else:
                changes = [book.change() for _ in range(1 + int(rng.random() * 3))]
                if jump_ms is not None and ms >= jump_ms:
                    tape.broken(
                        wall, f"sequence number {seq} expected, {seq + 1} received"
                    )
                    seq += 1  # one message number is skipped: as if it was lost
                    jump_ms = None
                levels = [
                    ("bid" if s == "bids" else "offer", p, q) for s, p, q in changes
                ]
                tape.set_book(book.best_first("bids"), book.best_first("asks"))
                tape.message(
                    wall,
                    message_text(seq, wall, "update", levels),
                    True,
                    wall - 50 * MS,
                )
                seq += 1
            ms += 250
        tape.event(at(end_ms), closing)
        return tape

    tapes.append(connection(100, 80_000, "planned_reconnect", step_ms=40_000))
    tapes.append(connection(82_000, END_MS, "stopped", jump_ms=90_000))
    return tapes


def kraken(rng: random.Random) -> list[Tape]:
    """One connection with a 1000-level book a side; a wrong checksum after the window."""
    book = TrueBook(rng, "57700.0", "0.1", 1010, "0.0001")
    depth = 1000

    def visible(side: str):
        return book.best_first(side)[:depth]

    def checksum() -> int:
        # From the text, as recorder/check.py computes it (the engine computes it from values).
        def digits(value: Decimal, places: int) -> str:
            return text(value, places).replace(".", "").lstrip("0")

        parts = [digits(p, 1) + digits(q, 8) for p, q in visible("asks")[:10]]
        parts += [digits(p, 1) + digits(q, 8) for p, q in visible("bids")[:10]]
        return zlib.crc32("".join(parts).encode()) & 0xFFFFFFFF

    def levels_json(levels) -> str:
        return ",".join(
            f'{{"price":{text(p, 1)},"qty":{text(q, 8)}}}' for p, q in levels
        )

    def book_text(kind: str, bids, asks, sum_: int, wall: int) -> str:
        return (
            f'{{"channel":"book","type":"{kind}","data":[{{"symbol":"BTC/USD",'
            f'"bids":[{levels_json(bids)}],"asks":[{levels_json(asks)}],'
            f'"checksum":{sum_},"timestamp":"{iso(wall - 30 * MS, 6)}"}}]}}'
        )

    tape = Tape("kraken", 200, change_only=True)
    tape.event(at(200), "connected", url="wss://ws.kraken.com/v2")
    tape.event(
        at(200) + MS,
        "sent",
        raw='{"method":"subscribe","params":{"channel":"book","symbol":["BTC/USD"],"depth":1000}}',
    )
    tape.message(
        at(250),
        '{"channel":"status","type":"update","data":[{"version":"2.0.10","system":"online","api_version":"v2","connection_id":17834348137351763235}]}',
    )
    tape.message(
        at(300),
        '{"method":"subscribe","result":{"channel":"book","depth":1000,"snapshot":true,"symbol":"BTC/USD"},"success":true}',
    )
    wall = at(600)
    tape.set_book(visible("bids"), visible("asks"))
    tape.ready(wall)
    tape.count("checksums_matched")
    tape.message(
        wall,
        book_text("snapshot", visible("bids"), visible("asks"), checksum(), wall),
        True,
        wall - 30 * MS,
    )
    frozen = False  # after the wrong checksum the engine stops applying changes
    ms = 750
    while ms < END_MS:
        wall = at(ms)
        if ms % 1000 == 0:
            tape.message(wall, '{"channel":"heartbeat"}')
        else:
            sides = {"bids": [], "asks": []}
            for _ in range(1 + int(rng.random() * 3)):
                before = {s: dict(visible(s)) for s in ("bids", "asks")}
                side, price, qty = book.change()
                after = dict(visible(side))
                sides[side].append((price, qty))
                # A removal inside the 1000 lets a deeper level in, which Kraken sends; a level
                # pushed out by an insertion is not sent (the client cuts the book back to 1000).
                sides[side] += [
                    (p, q)
                    for p, q in after.items()
                    if p not in before[side] and p != price
                ]
            wrong = ms == 85_050
            sum_ = checksum() ^ (1 if wrong else 0)
            if not frozen:
                tape.set_book(visible("bids"), visible("asks"))
                if wrong:
                    tape.broken(
                        wall, "checksum of the rebuilt book does not match Kraken's"
                    )
                else:
                    tape.count("checksums_matched")
            tape.message(
                wall,
                book_text("update", sides["bids"], sides["asks"], sum_, wall),
                not frozen,
                wall - 30 * MS,
            )
            frozen = frozen or wrong
        ms += 150
    tape.event(at(END_MS), "stopped")
    return [tape]


def bitstamp(rng: random.Random) -> list[Tape]:
    """Changes three times a second and two downloaded books. The first download holds two
    phantoms: an ask inside the spread, which a later bid crosses (crossing repair), and a deep
    bid, which the second download lacks (deletion-only resync). The second download holds a
    phantom of its own, which must not be added."""
    book = TrueBook(rng, "57700.00", "0.01", 30, "0.0001")
    tape = Tape("bitstamp", 300, change_only=True)
    tape.event(at(300), "connected", url="wss://ws.bitstamp.net")
    tape.event(
        at(300) + MS,
        "sent",
        raw='{"event":"bts:subscribe","data":{"channel":"diff_order_book_btcusd"}}',
    )
    tape.message(
        at(400),
        '{"event":"bts:subscription_succeeded","channel":"diff_order_book_btcusd","data":{}}',
    )

    def micro_of(wall: int) -> int:  # the exchange's clock runs 80 ms ahead of receipt
        return (wall - 80 * MS) // 1000

    def pairs(levels) -> list[list[str]]:
        return [[text(p, 2), text(q, 8)] for p, q in levels]

    phantom_ask = phantom_bid = None  # phantoms in the engine's book
    have_book = False
    last_micro = 0
    downloads = {
        3_100: None,
        33_100: None,
    }  # download time (ms) -> its content, once taken
    events = {3_700: 3_100, 33_700: 33_100}  # when each download is written down
    crossing_ms = 8_190  # the change whose bid crosses the phantom ask

    def engine_book():
        bids = dict(book.bids)
        asks = dict(book.asks)
        if phantom_bid:
            bids[phantom_bid[0]] = phantom_bid[1]
        if phantom_ask:
            asks[phantom_ask[0]] = phantom_ask[1]
        return sorted(bids.items(), reverse=True), sorted(asks.items())

    ms = 600
    pending_events = sorted(events)
    while ms < END_MS:
        # Downloads are taken between changes, and written down a little later.
        for taken in downloads:
            if downloads[taken] is None and taken <= ms:
                bids = book.best_first("bids")
                asks = book.best_first("asks")
                if taken == 3_100:
                    phantom_ask = (max(book.bids) + book.tick, Decimal("0.5"))
                    phantom_bid = (min(book.bids) - book.tick * 50, Decimal("0.25"))
                    bids = bids + [phantom_bid]
                    asks = [phantom_ask] + asks
                else:
                    bids = bids + [
                        (min(book.bids) - book.tick * 60, Decimal("0.75"))
                    ]  # never added
                downloads[taken] = json.dumps(
                    {
                        "timestamp": str(micro_of(at(taken)) // 1_000_000),
                        "microtimestamp": str(micro_of(at(taken))),
                        "bids": pairs(bids),
                        "asks": pairs(asks),
                    },
                    separators=(",", ":"),
                )
        while pending_events and pending_events[0] <= ms:
            written = pending_events.pop(0)
            taken = events[written]
            wall = at(written)
            tape.count("book_downloads")
            if not have_book:
                have_book = True
                tape.ready(wall)
                tape.last_wall, tape.last_exchange = wall, last_micro * 1000
            else:
                tape.count("resyncs")
                phantom_bid = (
                    None  # unconfirmed and missing from this download: removed
                )
                tape.count("phantom_levels_removed")
                tape.count("download_only_levels")
            tape.set_book(*engine_book())
            tape.event(
                wall,
                "rest_snapshot",
                url="https://www.bitstamp.net/api/v2/order_book/btcusd/",
                requested_wall_ns=at(taken - 100),
                received_wall_ns=wall - 50 * MS,
                raw=downloads[taken],
            )
        wall = at(ms)
        # The best prices stay still until the phantom ask is crossed, so the spread stays
        # wide enough for a phantom inside it.
        quiet = ms < crossing_ms
        if ms == crossing_ms:
            price = phantom_ask[0]
            book.bids[price] = book.size()
            changes = [("bids", price, book.bids[price])]
            phantom_ask = None
            tape.count("crossing_levels_removed")
        else:
            changes = [
                book.change(away=2 if quiet else 0)
                for _ in range(1 + int(rng.random() * 3))
            ]
        last = {}
        for (
            side,
            price,
            qty,
        ) in changes:  # a change reports the net result for each price
            last[(side, price)] = qty
        bids = [(p, q) for (s, p), q in last.items() if s == "bids"]
        asks = [(p, q) for (s, p), q in last.items() if s == "asks"]
        last_micro = micro_of(wall)
        diff = json.dumps(
            {
                "data": {
                    "timestamp": str(last_micro // 1_000_000),
                    "microtimestamp": str(last_micro),
                    "bids": pairs(bids),
                    "asks": pairs(asks),
                },
                "channel": "diff_order_book_btcusd",
                "event": "data",
            },
            separators=(",", ":"),
        )
        tape.count("changes")
        if have_book:
            tape.set_book(*engine_book())
        tape.message(wall, diff, have_book, last_micro * 1000)
        ms += 330
    tape.event(at(END_MS), "stopped")
    return [tape]


def gemini(rng: random.Random) -> list[Tape]:
    """Overlapping updates, a stale one, top-five checks each second, then a missed update
    after the window."""
    book = TrueBook(rng, "57700.00", "0.01", 30, "0.0001")
    tape = Tape("gemini", 400, change_only=True)
    tape.event(at(400), "connected", url="wss://ws.gemini.com/?snapshot=-1")
    tape.event(
        at(400) + MS,
        "sent",
        raw='{"id":"1","method":"subscribe","params":["btcusd@depth@100ms","btcusd@depth5"]}',
    )

    def pairs(levels):
        return [[text(p, 5), text(q, 10)] for p, q in levels]

    def update(wall: int, first: int, last: int, bids, asks) -> str:
        return json.dumps(
            {
                "e": "depthUpdate",
                "E": wall - 20 * MS,
                "s": "btcusd",
                "U": first,
                "u": last,
                "b": pairs(bids),
                "a": pairs(asks),
            },
            separators=(",", ":"),
        )

    last_u = 1_000
    wall = at(600)
    tape.set_book(book.best_first("bids"), book.best_first("asks"))
    tape.ready(wall)
    tape.message(
        wall,
        update(wall, last_u, last_u, book.best_first("bids"), book.best_first("asks")),
        True,
        wall - 20 * MS,
    )
    tape.message(at(605), '{"id":"1","status":200}')
    ms = 750
    while ms < END_MS:
        wall = at(ms)
        if ms % 1000 == 50:  # Gemini's own top five, once a second
            if tape.invalid_ns is None:
                tape.count("top_five_compared")
                tape.count("top_five_matched")
            tape.message(
                wall,
                json.dumps(
                    {
                        "lastUpdateId": last_u,
                        "symbol": "btcusd",
                        "bids": pairs(book.best_first("bids")[:5]),
                        "asks": pairs(book.best_first("asks")[:5]),
                    },
                    separators=(",", ":"),
                ),
            )
        elif ms == 40_200:  # an old update arriving late: skipped
            tape.count("stale_updates_skipped")
            tape.message(
                wall,
                update(wall, last_u - 10, last_u - 1, book.best_first("bids")[:1], []),
            )
        else:
            changes = [book.change() for _ in range(1 + int(rng.random() * 3))]
            last = {}
            for side, price, qty in changes:
                last[(side, price)] = qty
            first = last_u
            if ms == 90_200:  # update numbers jump: as if one update was lost
                first = last_u + 3
                tape.broken(wall, f"update numbers {last_u + 1} to {first - 1} missing")
            new_u = max(first, last_u) + 1 + int(rng.random() * 5)
            tape.set_book(book.best_first("bids"), book.best_first("asks"))
            tape.message(
                wall,
                update(
                    wall,
                    first,
                    new_u,
                    [(p, q) for (s, p), q in last.items() if s == "bids"],
                    [(p, q) for (s, p), q in last.items() if s == "asks"],
                ),
                True,
                wall - 20 * MS,
            )
            last_u = new_u
        ms += 150 if ms % 1000 != 950 else 100
    tape.event(at(END_MS), "stopped")
    return [tape]


def cryptocom(rng: random.Random) -> list[Tape]:
    """A 50-level book, a chain of updates, answered heartbeats, then a broken chain after the
    window."""
    book = TrueBook(rng, "57700.00", "0.01", 56, "0.00001")
    depth = 50
    tape = Tape("cryptocom", 500, change_only=True)
    tape.event(at(500), "connected", url="wss://stream.crypto.com/exchange/v1/market")
    tape.event(
        at(1_500),
        "sent",
        raw='{"id":1,"method":"subscribe","params":{"channels":["book.BTC_USD.50"],"book_subscription_type":"SNAPSHOT_AND_UPDATE","book_update_frequency":100}}',
    )
    tape.message(
        at(1_600), '{"id":1,"method":"subscribe","code":0,"channel":"book.BTC_USD.50"}'
    )

    def visible(side):
        return book.best_first(side)[:depth]

    def triples(levels):
        return [
            [text(p, 2), "0" if q == 0 else text(q, 5), "0" if q == 0 else "1"]
            for p, q in levels
        ]

    def message(channel: str, data: dict) -> str:
        return json.dumps(
            {
                "id": -1,
                "method": "subscribe",
                "code": 0,
                "result": {
                    "instrument_name": "BTC_USD",
                    "subscription": "book.BTC_USD.50",
                    "channel": channel,
                    "depth": depth,
                    "data": [data],
                },
            },
            separators=(",", ":"),
        )

    u = 5_000
    wall = at(2_600)
    t_ms = (wall - 10 * MS) // MS
    tape.set_book(visible("bids"), visible("asks"))
    tape.ready(wall)
    tape.message(
        wall,
        message(
            "book",
            {
                "asks": triples(visible("asks")),
                "bids": triples(visible("bids")),
                "t": t_ms,
                "tt": t_ms,
                "u": u,
                "cs": 0,
            },
        ),
        True,
        t_ms * MS,
    )
    ms = 2_750
    while ms < END_MS:
        wall = at(ms)
        if ms % 5_000 == 0:  # a heartbeat, answered at once
            tape.count("heartbeats_received")
            tape.message(
                wall, f'{{"id":{wall // MS},"method":"public/heartbeat","code":0}}'
            )
            tape.count("heartbeats_answered")
            tape.event(
                wall + MS,
                "sent",
                raw=f'{{"id":{wall // MS},"method":"public/respond-heartbeat"}}',
            )
        else:
            sides = {"bids": [], "asks": []}
            for _ in range(1 + int(rng.random() * 3)):
                before = {s: dict(visible(s)) for s in ("bids", "asks")}
                side, price, qty = book.change()
                after = dict(visible(side))
                sides[side].append((price, qty))
                # The exchange keeps its 50 levels itself: a level entering is sent, and so is
                # the removal of a level pushed out.
                sides[side] += [
                    (p, q)
                    for p, q in after.items()
                    if p not in before[side] and p != price
                ]
                sides[side] += [
                    (p, Decimal(0))
                    for p in before[side]
                    if p not in after and p != price
                ]
            previous = u
            if ms == 85_150:  # names an update that never came
                previous = u - 7
                tape.broken(wall, "an update does not follow the previous one (pu)")
            u += 1 + int(rng.random() * 1000)
            t_ms = (wall - 10 * MS) // MS
            tape.set_book(visible("bids"), visible("asks"))
            tape.message(
                wall,
                message(
                    "book.update",
                    {
                        "update": {
                            "asks": triples(sides["asks"]),
                            "bids": triples(sides["bids"]),
                        },
                        "t": t_ms,
                        "tt": t_ms,
                        "u": u,
                        "pu": previous,
                        "cs": 0,
                    },
                ),
                True,
                t_ms * MS,
            )
        ms += 150 if ms % 5_000 != 4_950 else 50
    tape.event(at(END_MS), "stopped")
    return [tape]


def bullish(rng: random.Random) -> list[Tape]:
    """Full books twice a second. A sequence gap inside the measured window, then the first
    file ends in damaged bytes (a power cut) and a second connection follows."""
    book = TrueBook(rng, "57700.0000", "0.1", 20, "0.00000001")
    tapes = []
    seq = 2_653_751_000

    def flat(levels) -> list[str]:
        return [v for p, q in levels for v in (text(p, 4), text(q, 8))]

    def connection(start_ms: int, end_ms: int, gap_ms=None, junk=False) -> Tape:
        nonlocal seq
        tape = Tape("bullish", start_ms, change_only=False)
        tape.event(
            at(start_ms),
            "connected",
            url="wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook",
        )
        tape.event(
            at(start_ms) + MS,
            "sent",
            raw='{"jsonrpc":"2.0","type":"command","method":"subscribe","params":{"topic":"l2Orderbook","symbol":"BTCUSD"},"id":"1"}',
        )
        tape.message(
            at(start_ms + 200),
            '{"jsonrpc": "2.0","id": "1","result": {"responseCode": "200","responseCodeName": "OK","message": "Successfully subscribed ( BTCUSD 2)"}}',
        )
        ms = start_ms + 500
        while ms <= end_ms:
            wall = at(ms)
            for _ in range(int(rng.random() * 4)):
                book.change()
            lower = seq + 1
            if gap_ms is not None and ms >= gap_ms:
                lower = seq + 3
                tape.broken(wall, f"sequence numbers {seq + 1} to {lower - 1} missing")
                gap_ms = None
            seq = lower + int(rng.random() * 3)
            stamp = (wall - 40 * MS) // MS
            tape.set_book(book.best_first("bids"), book.best_first("asks"))
            tape.ready(wall)
            tape.message(
                wall,
                json.dumps(
                    {
                        "type": "snapshot",
                        "dataType": "V1TALevel2",
                        "data": {
                            "symbol": "BTCUSD",
                            "bids": flat(book.best_first("bids")),
                            "asks": flat(book.best_first("asks")),
                            "sequenceNumberRange": [lower, seq],
                            "datetime": iso(stamp * MS, 3),
                            "timestamp": str(stamp),
                            "publishedAtTimestamp": str(stamp + 5),
                        },
                    },
                    separators=(",", ":"),
                ),
                True,
                stamp * MS,
            )
            ms += 500
        if junk:
            tape.junk = True
        else:
            tape.event(at(END_MS), "stopped")
        return tape

    tapes.append(connection(500, 75_000, gap_ms=50_000, junk=True))
    tapes.append(connection(78_000, END_MS - 500))
    return tapes


def lmax(rng: random.Random) -> list[Tape]:
    """A full book once a second. The first file is cut off by a crash; in the second, one
    second's book never comes and one book has the market closed."""
    book = TrueBook(rng, "57700.000000", "0.5", 12, "0.0001")
    tapes = []

    def levels(side):
        return [
            {"price": text(p, 6), "quantity": text(q, 4)}
            for p, q in book.best_first(side)
        ]

    def connection(
        start_ms: int, end_ms: int, cut=False, skip_ms=None, closed_ms=None
    ) -> Tape:
        tape = Tape("lmax", start_ms, change_only=False)
        tape.event(
            at(start_ms),
            "connected",
            url="wss://public-data-api.london-digital.lmax.com/v1/web-socket",
        )
        tape.event(
            at(start_ms) + MS,
            "sent",
            raw='{"type":"SUBSCRIBE","channels":[{"name":"ORDER_BOOK","instruments":["btc-usd"]}]}',
        )
        tape.message(
            at(start_ms + 100),
            '{"type":"SUBSCRIPTIONS","channels":[{"name":"ORDER_BOOK","instruments":["btc-usd"]}]}',
        )
        previous = None
        ms = (
            start_ms // 1000 + 1
        ) * 1000 + 500  # each book half a second after its whole second
        while ms <= end_ms:
            for _ in range(int(rng.random() * 3)):
                book.change()
            second = at(ms - 500)
            if skip_ms is not None and ms >= skip_ms:  # this second's book never comes
                skip_ms = None
                ms += 1000
                continue
            wall = at(ms)
            if previous is not None and second != previous + SECOND:
                tape.broken(
                    wall,
                    f"{(second - previous) // SECOND - 1} second(s) skipped before {iso(second, 3)}",
                )
            previous = second
            status = "CLOSED" if ms == closed_ms else "OPEN"
            tape.open = status == "OPEN"
            if not tape.open:
                tape.count("books_not_open")
            tape.set_book(book.best_first("bids"), book.best_first("asks"))
            tape.ready(wall)
            tape.message(
                wall,
                json.dumps(
                    {
                        "type": "ORDER_BOOK",
                        "instrument_id": "btc-usd",
                        "timestamp": iso(second, 3),
                        "status": status,
                        "bids": levels("bids"),
                        "asks": levels("asks"),
                    },
                    separators=(",", ":"),
                ),
                True,
                second,
            )
            ms += 1000
        if cut:
            tape.cut_off = True
        else:
            tape.event(at(END_MS), "stopped")
        return tape

    tapes.append(connection(0, 30_500, cut=True))
    tapes.append(connection(35_000, END_MS - 500, skip_ms=60_500, closed_ms=80_500))
    return tapes


# ---------------------------------------------------------------------------------------------
# What the replay must output
# ---------------------------------------------------------------------------------------------


def sample_row(feed: str, tapes: list[Tape], s: int) -> str:
    """One CSV row: mirrors Replay's sample() in engine/src/replay.cpp."""
    second = s // SECOND
    started = [t for t in tapes if t.walls[0] <= s]
    if not started:
        return f"{second},{feed},no_data,,,,,0,0,0,0,0,0,0\n"
    tape = started[-1]  # the last file whose first record has been applied
    i = bisect.bisect_right(tape.walls, s) - 1
    (
        best_bid,
        best_ask,
        n_bids,
        n_asks,
        crossed,
        ready,
        invalid,
        open_,
        last_wall,
        last_exchange,
    ) = tape.states[i]
    if tape.closed_ns is not None and tape.closed_ns <= s:
        state = "closed"
    elif tape.walls[-1] < s:
        state = "no_data"  # the file stopped without a closing event
    elif not ready:
        state = "not_ready"
    elif tape.change_only and invalid:
        state = "invalid"
    elif not open_:
        state = "not_open"
    else:
        state = "ok"
    breaks = [b for b in tape.breaks if b <= s]
    steps = [w for w in tape.steps if w <= s]
    gap = int(not tape.change_only and bool(breaks) and breaks[-1] > s - SECOND)
    step = int(bool(steps) and steps[-1] > s - SECOND)
    bid = f"{shortest(best_bid[0])},{shortest(best_bid[1])}" if best_bid else ","
    ask = f"{shortest(best_ask[0])},{shortest(best_ask[1])}" if best_ask else ","
    return (
        f"{second},{feed},{state},{bid},{ask},{n_bids},{n_asks},{int(crossed)},{gap},{step},"
        f"{last_wall},{last_exchange}\n"
    )


def expected_csv(all_tapes: dict[str, list[Tape]]) -> str:
    walls = [w for tapes in all_tapes.values() for t in tapes for w in t.walls]
    first, last = min(walls), max(walls)
    out = [CSV_HEADER]
    s = -(-first // SECOND) * SECOND  # the first whole second, rounded up
    while s <= last:  # as Replay::run: every second up to the last record
        for feed in FEEDS:
            out.append(sample_row(feed, all_tapes[feed], s))
        s += SECOND
    return "".join(out)


def file_json(tape: Tape, newest: bool) -> str:
    """What the engine must report for one file: the JSON the replay command writes."""
    complete = not (tape.cut_off or tape.junk)
    problems = list(tape.problems)
    if not complete and not newest:
        problems.append(
            (tape.walls[-1], "file cut off (the recorder was stopped abruptly)")
        )
    return json.dumps(
        {
            "feed": tape.feed,
            "file": tape.name,
            "start_ns": tape.walls[0],
            "end_ns": tape.walls[-1],
            "ready_ns": tape.ready_ns,
            "invalid_from_ns": tape.invalid_ns,
            "break_ns": tape.breaks,
            "closed_ns": tape.closed_ns,
            "ended": tape.ended,
            "unplanned": tape.ended in UNPLANNED,
            "complete": complete,
            "damaged": tape.junk,
            "counts": dict(sorted(tape.counts.items())),
            "problems": [[wall, text_] for wall, text_ in problems],
        },
        separators=(",", ":"),
    )


def fnv1a64(data: bytes) -> int:
    value = 14_695_981_039_346_656_037
    for byte in data:
        value = ((value ^ byte) * 1_099_511_628_211) & 0xFFFF_FFFF_FFFF_FFFF
    return value


def build() -> dict[str, bytes]:
    """Every file to write, by its path under tapes/."""
    makers = {
        "coinbase": coinbase,
        "kraken": kraken,
        "bitstamp": bitstamp,
        "gemini": gemini,
        "cryptocom": cryptocom,
        "bullish": bullish,
        "lmax": lmax,
    }
    all_tapes = {
        feed: makers[feed](random.Random(f"engine-tests-{feed}")) for feed in FEEDS
    }
    files: dict[str, bytes] = {}
    summaries = []
    for feed in FEEDS:
        for i, tape in enumerate(all_tapes[feed]):
            files[f"{feed}/2026-11-21/{tape.name}"] = tape.gzip_bytes()
            summaries.append(file_json(tape, newest=(i == len(all_tapes[feed]) - 1)))
    csv = expected_csv(all_tapes).encode()
    files["expected.csv"] = csv
    files["expected.fnv"] = f"{fnv1a64(csv):016x}\n".encode()
    files["expected_files.ndjson"] = ("\n".join(summaries) + "\n").encode()
    return files


# ---------------------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------------------


def readable(data: bytes) -> bytes:
    """A tape's text, as far as it can be read (zlib versions differ in the bytes they write,
    so tapes are compared by content)."""
    try:
        return zlib.decompressobj(wbits=31).decompress(data)
    except zlib.error:  # damaged bytes at the end: read up to them, one byte at a time
        reader, out = zlib.decompressobj(wbits=31), bytearray()
        for i in range(len(data)):
            try:
                out += reader.decompress(data[i : i + 1])
            except zlib.error:
                break
        return bytes(out)


def check(out_dir: Path) -> int:
    failures = []
    expected = build()
    on_disk = {
        str(p.relative_to(out_dir)): p.read_bytes()
        for p in out_dir.rglob("*")
        if p.is_file()
    }
    for name in sorted(set(expected) | set(on_disk)):
        want, have = expected.get(name), on_disk.get(name)
        if want is None or have is None:
            failures.append(
                f"{name}: {'not written by this script' if want is None else 'missing'}"
            )
        elif name.endswith(".jsonl.gz"):
            if readable(want) != readable(have):
                failures.append(f"{name}: content differs from what this script writes")
        elif want != have:
            failures.append(f"{name}: differs from what this script writes")
    # Every tape committed anywhere in the repository must be synthetic.
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.jsonl.gz", "*.jsonl"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    for name in filter(None, listed):
        data = (ROOT / name.decode()).read_bytes()
        first = (readable(data) if name.endswith(b".gz") else data).split(b"\n", 1)[0]
        if b'"event":"synthetic"' not in first:
            failures.append(f"{name.decode()}: a committed tape that is not synthetic")
    for failure in failures:
        print(failure)
    print("tapes are up to date" if not failures else f"{len(failures)} problem(s)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=HERE / "tapes")
    parser.add_argument(
        "--check", action="store_true", help="compare instead of writing"
    )
    args = parser.parse_args()
    if args.check:
        return check(args.out)
    for name, data in build().items():
        path = args.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
