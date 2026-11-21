"""Record one exchange's public order-book feed to compressed files, unchanged.

Usage:
    python recorder.py <feed>     record that feed until stopped
    python recorder.py --list     print the feeds this program knows

The recorder does not interpret what it receives. It stamps each message with
the time it arrived and writes it down as it came. Working out what the
messages mean is done later, by other programs, from these files. A mistake in
that later work can be fixed by running it again; a message that was never
written down is gone, so this program does as little as possible.

One copy of this program records one exchange. The server runs one copy per
exchange, so trouble with one exchange cannot disturb the others.
"""

from __future__ import annotations

import argparse
import math
import random
import signal
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from feeds import FEEDS, Feed
from tape import DiskFull, Tape

# The recorder reconnects on purpose once per period, so that every file
# begins with a full order book and can be read without the files before it.
RECONNECT_PERIOD_S = 3600

# How long one wait for a message lasts. The recorder checks its other duties
# (stopping, reconnecting, keep-alives) at least this often.
POLL_S = 1.0

OPEN_TIMEOUT_S = 20.0  # give up on a connection attempt after this long
CLOSE_TIMEOUT_S = 5.0  # wait this long for a polite goodbye when leaving

# After a failed connection the recorder waits before trying again: 2, 4, 8 ...
# times BACKOFF_BASE_S, never more than BACKOFF_CAP_S. A connection that
# lasted STABLE_AFTER_S counts as healthy and resets the wait.
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0
STABLE_AFTER_S = 60.0

SNAPSHOT_TIMEOUT_S = 5.0  # give up on one full-book download after this long
SNAPSHOT_RETRY_S = 10.0  # try a failed download again after this long
DISK_FULL_WAIT_S = 60.0  # look again for free disk space after this long

USER_AGENT = "order-book-recorder"


def next_planned_reconnect(now_s: float, period_s: int, offset_s: int) -> float:
    """The next moment, strictly after now_s, that is offset_s into a period.

    With a period of 3600 and an offset of 300 this is the next "five minutes
    past the hour". Times are seconds since 1970, as time.time() returns them.
    """
    periods_done = math.floor((now_s - offset_s) / period_s)
    return (periods_done + 1) * period_s + offset_s


def backoff_delay(failures: int) -> float:
    """Seconds to wait before reconnecting, given how many tries just failed.

    The wait doubles with each failure up to a cap. It is then varied by a
    quarter either way at random, so that many clients cut off at the same
    moment do not all come back at the same moment.
    """
    doubled = BACKOFF_BASE_S * 2 ** min(failures, 30)
    return min(doubled, BACKOFF_CAP_S) * random.uniform(0.75, 1.25)


def log(feed: Feed, text: str) -> None:
    """Print a line for the server's log. flush=True sends it at once."""
    print(f"{feed.name}: {text}", flush=True)


def download_full_book(feed: Feed, tape: Tape) -> float | None:
    """Fetch the full order book over the web and write it to the tape.

    Returns when to do it next, on the forward-only clock, or None for never.
    While this waits for the download, arriving messages queue up and are
    stamped a little late; SNAPSHOT_TIMEOUT_S bounds how late.
    """
    requested_wall_ns = time.time_ns()
    try:
        request = urllib.request.Request(
            feed.rest_snapshot_url, headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=SNAPSHOT_TIMEOUT_S) as response:
            body = response.read().decode("utf-8")
    except (OSError, ValueError) as error:  # network trouble, or not valid text
        tape.event(
            "rest_snapshot_failed", url=feed.rest_snapshot_url, detail=repr(error)
        )
        return time.monotonic() + SNAPSHOT_RETRY_S
    tape.event(
        "rest_snapshot",
        url=feed.rest_snapshot_url,
        requested_wall_ns=requested_wall_ns,
        raw=body,
    )
    if feed.rest_snapshot_every_s > 0:
        return time.monotonic() + feed.rest_snapshot_every_s
    return None


def record_messages(
    feed: Feed, tape: Tape, connection, reconnect_at_s: float, stop: threading.Event
) -> str:
    """Write down everything one open connection delivers. Returns why it ended."""

    def send(text: str) -> None:
        connection.send(text)
        tape.event("sent", raw=text)

    tape.event("connected", url=feed.url)
    if feed.subscribe_delay_s > 0:
        stop.wait(feed.subscribe_delay_s)
    for text in feed.subscribe:
        send(text)

    now_mono = time.monotonic()
    last_message_mono = now_mono
    next_keepalive_mono = now_mono + feed.keepalive_every_s
    next_snapshot_mono = None
    if feed.rest_snapshot_url:
        next_snapshot_mono = now_mono + feed.rest_snapshot_after_s

    while True:
        if stop.is_set():
            tape.event("stopped")
            return "stopped"
        if time.time() >= reconnect_at_s:
            tape.event("planned_reconnect")
            return "planned"

        try:
            message = connection.recv(timeout=POLL_S)
        except TimeoutError:
            message = None
        # Both clocks are read at once, before anything else is done.
        wall_ns = time.time_ns()
        mono_ns = time.monotonic_ns()
        now_mono = time.monotonic()

        if message is not None:
            tape.message(wall_ns, mono_ns, message)
            last_message_mono = now_mono
            if feed.reply is not None and isinstance(message, str):
                answer = feed.reply(message)
                if answer is not None:
                    send(answer)
        elif now_mono - last_message_mono > feed.stale_after_s:
            tape.event("stale", silent_s=round(now_mono - last_message_mono, 3))
            return "stale"

        if feed.keepalive_message and now_mono >= next_keepalive_mono:
            send(feed.keepalive_message)
            next_keepalive_mono = now_mono + feed.keepalive_every_s

        if next_snapshot_mono is not None and now_mono >= next_snapshot_mono:
            next_snapshot_mono = download_full_book(feed, tape)


def record_connection(
    feed: Feed, tape: Tape, reconnect_at_s: float, stop: threading.Event
) -> str:
    """Open one connection, record it into a new file, and close both.

    Returns why the connection ended. Raises if it could not be opened.
    """
    with connect(
        feed.url,
        open_timeout=OPEN_TIMEOUT_S,
        close_timeout=CLOSE_TIMEOUT_S,
        ping_interval=feed.ping_interval_s,
        max_size=None,  # no limit: a full order book can be several megabytes
    ) as connection:
        tape.open_new()
        try:
            return record_messages(feed, tape, connection, reconnect_at_s, stop)
        except ConnectionClosed as closed:
            tape.event("disconnected", reason=str(closed))
            return "disconnected"
        except Exception as error:
            tape.event("error", detail=repr(error))
            raise
        finally:
            tape.close()  # runs however the block above ends


def run(
    feed: Feed,
    data_dir: Path,
    stop: threading.Event,
    reconnect_period_s: int = RECONNECT_PERIOD_S,
) -> None:
    """Record a feed until stop is set, reconnecting whenever a connection ends."""
    tape = Tape(data_dir, feed.name)
    failures = 0
    while not stop.is_set():
        reconnect_at_s = next_planned_reconnect(
            time.time(), reconnect_period_s, feed.reconnect_offset_s
        )
        started_mono = time.monotonic()
        try:
            outcome = record_connection(feed, tape, reconnect_at_s, stop)
        except DiskFull as full:
            log(feed, f"not recording: {full}")
            stop.wait(DISK_FULL_WAIT_S)
            continue
        except (OSError, WebSocketException) as error:  # ordinary network trouble
            outcome = f"connection failed ({error!r})"
        except Exception:  # a bug: say so loudly, but keep recording
            outcome = "unexpected error"
            log(feed, traceback.format_exc())

        if outcome in ("planned", "stopped"):
            failures = 0
            continue  # reconnect at once; nothing went wrong
        lasted_s = time.monotonic() - started_mono
        failures = 0 if lasted_s >= STABLE_AFTER_S else failures + 1
        delay_s = backoff_delay(failures)
        log(feed, f"{outcome} after {lasted_s:.0f} s; retrying in {delay_s:.1f} s")
        stop.wait(delay_s)  # like sleep, but ends early if stop is set


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record one exchange's public order-book feed, unchanged."
    )
    parser.add_argument("feed", nargs="?", choices=sorted(FEEDS))
    parser.add_argument("--list", action="store_true", help="print the known feeds")
    parser.add_argument("--data-dir", type=Path, default=Path("/data"))
    args = parser.parse_args()
    if args.list:
        print("\n".join(sorted(FEEDS)))
        return 0
    if args.feed is None:
        parser.error("name a feed to record, or use --list")

    # When the server asks the program to stop (SIGTERM) or Ctrl-C is pressed
    # (SIGINT), only raise a flag. The recording loop sees the flag within
    # POLL_S seconds and closes its file properly before the program ends.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stop.set())

    feed = FEEDS[args.feed]
    log(feed, f"recording {feed.url} into {args.data_dir}")
    run(feed, args.data_dir, stop)
    log(feed, "stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
