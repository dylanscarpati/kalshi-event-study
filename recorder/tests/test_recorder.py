"""Tests for the recorder, run against a pretend exchange on this machine.

The pretend exchange is a small WebSocket server that follows a script for
each connection. Nothing here reaches the internet.
"""

import gzip
import http.server
import json
import threading
import time

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

import recorder
import tape as tape_module
from feeds import Feed
from tape import read_tape

SUBSCRIBE = '{"op":"subscribe","book":"BTC-USD"}'


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class PretendExchange:
    """A WebSocket server that runs one script per connection, in order.

    The last script is reused for any further connections.
    """

    def __init__(self, *scripts):
        self.scripts = scripts
        self.connections = 0
        self.received: list[str] = []  # everything clients sent
        self._lock = threading.Lock()
        self._server = None

    def __enter__(self):
        self._server = serve(self._handle, "127.0.0.1", 0)
        port = self._server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()  # stops accepting connections
        self._server.socket.close()  # and frees the listening socket

    def _handle(self, connection):
        with self._lock:
            index = self.connections
            self.connections += 1
        script = self.scripts[min(index, len(self.scripts) - 1)]
        try:
            script(connection, self)
        except ConnectionClosed:
            pass

    def note(self, text):
        with self._lock:
            self.received.append(text)


def send_then_wait(*messages):
    """Script: read the subscribe, send the messages, keep the line open."""

    def script(connection, exchange):
        exchange.note(connection.recv())
        for message in messages:
            connection.send(message)
        for text in connection:  # runs until the client goes away
            exchange.note(text)

    return script


def send_then_hang_up(*messages):
    """Script: read the subscribe, send the messages, close the connection."""

    def script(connection, exchange):
        exchange.note(connection.recv())
        for message in messages:
            connection.send(message)

    return script


def wait_for(condition, timeout=5.0):
    """Wait until condition() is true. Fail the test if it never is."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the condition")


class Recording:
    """Runs the recorder in the background for the length of a `with` block."""

    def __init__(self, feed, data_dir, reconnect_period_s=3600):
        self.feed = feed
        self.data_dir = data_dir
        self.stop = threading.Event()
        self._thread = threading.Thread(
            target=recorder.run,
            args=(feed, data_dir, self.stop),
            kwargs={"reconnect_period_s": reconnect_period_s},
            daemon=True,
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "the recorder did not stop"

    def files(self):
        """Every tape written so far, oldest first, each as a list of records."""
        paths = sorted((self.data_dir / self.feed.name).rglob("*.jsonl.gz"))
        return [list(read_tape(path)) for path in paths]


def summary(records):
    """Each record as a short label: its event name, or its message text."""
    return [record.get("event", record.get("raw")) for record in records]


# Kept before any test replaces it, so that its own test checks the real one.
REAL_MAY_RECONNECT_ON_PURPOSE = recorder.may_reconnect_on_purpose


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch):
    """Shrink the recorder's waits so the tests take milliseconds, not minutes."""
    monkeypatch.setattr(recorder, "POLL_S", 0.02)
    monkeypatch.setattr(recorder, "BACKOFF_BASE_S", 0.02)
    monkeypatch.setattr(recorder, "DISK_FULL_WAIT_S", 0.02)
    monkeypatch.setattr(recorder, "MIN_CONNECTION_S", 0)
    # The real rule depends on the time of day; tests must not.
    monkeypatch.setattr(recorder, "may_reconnect_on_purpose", lambda wall_s: True)


def feed_for(exchange, **changes):
    return Feed(name="pretend", url=exchange.url, subscribe=(SUBSCRIBE,), **changes)


# --------------------------------------------------------------------------
# Timing rules (no network)
# --------------------------------------------------------------------------


def test_planned_reconnect_is_the_next_offset_past_the_hour():
    noon = 1_800_000_000 - (1_800_000_000 % 3600)  # some exact hour, in seconds

    assert recorder.next_planned_reconnect(noon, 3600, 300) == noon + 300
    assert recorder.next_planned_reconnect(noon + 299.9, 3600, 300) == noon + 300
    assert recorder.next_planned_reconnect(noon + 301, 3600, 300) == noon + 3900


def test_planned_reconnect_is_never_now():
    noon = 1_800_000_000 - (1_800_000_000 % 3600)

    # Exactly on the mark: the next one is a full hour away, not zero seconds.
    assert recorder.next_planned_reconnect(noon + 300, 3600, 300) == noon + 3900


def test_backoff_doubles_then_stops_growing():
    def bounds(attempt):
        delays = [recorder.backoff_delay(attempt) for _ in range(200)]
        return min(delays), max(delays)

    base = recorder.BACKOFF_BASE_S
    assert bounds(0)[0] >= 0.75 * base and bounds(0)[1] <= 1.25 * base
    assert bounds(3)[0] >= 0.75 * 8 * base and bounds(3)[1] <= 1.25 * 8 * base
    cap = recorder.BACKOFF_CAP_S
    assert bounds(50)[1] <= 1.25 * cap  # no overflow, no ever-longer waits


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_records_what_the_exchange_sends_in_order(tmp_path):
    messages = ('{"book":"snapshot"}', '{"book":"update 1"}', '{"book":"update 2"}')
    with PretendExchange(send_then_wait(*messages)) as exchange:
        with Recording(feed_for(exchange), tmp_path) as recording:
            wait_for(lambda: exchange.connections == 1 and exchange.received)
            time.sleep(0.2)  # let the three messages arrive

    (records,) = recording.files()
    assert summary(records) == ["connected", "sent", *messages, "stopped"]
    assert records[1]["raw"] == SUBSCRIBE  # the "sent" event holds what was sent
    assert exchange.received == [SUBSCRIBE]


def test_each_message_is_stamped_when_it_arrives(tmp_path):
    before_wall = time.time_ns()
    with PretendExchange(send_then_wait("one", "two", "three")) as exchange:
        with Recording(feed_for(exchange), tmp_path) as recording:
            wait_for(lambda: exchange.received)
            time.sleep(0.2)
    after_wall = time.time_ns()

    (records,) = recording.files()
    walls = [record["wall_ns"] for record in records]
    monos = [record["mono_ns"] for record in records]
    assert all(before_wall <= wall <= after_wall for wall in walls)
    assert monos == sorted(monos)  # the forward-only clock never goes back


def test_a_five_megabyte_message_is_recorded_whole(tmp_path):
    huge = "x" * (5 * 1024 * 1024)
    with PretendExchange(send_then_wait(huge)) as exchange:
        with Recording(feed_for(exchange), tmp_path) as recording:
            wait_for(lambda: exchange.received)
            time.sleep(0.5)

    (records,) = recording.files()
    assert records[2]["raw"] == huge


def test_reconnects_when_the_exchange_hangs_up(tmp_path):
    with PretendExchange(
        send_then_hang_up("first"), send_then_wait("second")
    ) as exchange:
        with Recording(feed_for(exchange), tmp_path) as recording:
            wait_for(lambda: len(exchange.received) == 2)
            time.sleep(0.2)

    first, second = recording.files()
    assert summary(first) == ["connected", "sent", "first", "disconnected"]
    assert summary(second) == ["connected", "sent", "second", "stopped"]
    assert exchange.received == [SUBSCRIBE, SUBSCRIBE]  # subscribed both times


def test_replaces_a_connection_that_goes_silent(tmp_path):
    with PretendExchange(send_then_wait("only message")) as exchange:
        feed = feed_for(exchange, stale_after_s=0.2)
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: exchange.connections >= 2)

    first = recording.files()[0]
    assert summary(first) == ["connected", "sent", "only message", "stale"]


def test_reconnects_on_schedule_and_starts_a_new_file(tmp_path):
    with PretendExchange(send_then_wait("book")) as exchange:
        with Recording(
            feed_for(exchange, reconnect_offset_s=0), tmp_path, reconnect_period_s=1
        ) as recording:
            wait_for(lambda: exchange.connections >= 3)

    files = recording.files()
    assert len(files) >= 3
    for records in files[:2]:
        assert summary(records)[:2] == ["connected", "sent"]
        assert summary(records)[-1] == "planned_reconnect"
    # With a one-second period, a connection that opens just before a second
    # boundary can be replaced before its first message arrives; most are not.
    assert sum("book" in summary(records) for records in files) >= 2


def test_a_planned_reconnect_is_never_right_after_connecting(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "MIN_CONNECTION_S", 3600)  # the real rule, scaled up
    with PretendExchange(send_then_wait("book")) as exchange:
        with Recording(
            feed_for(exchange, reconnect_offset_s=0), tmp_path, reconnect_period_s=1
        ):
            time.sleep(1.5)  # a one-second period would have reconnected by now

    assert exchange.connections == 1


def test_waits_before_subscribing_when_the_feed_asks_for_it(tmp_path):
    with PretendExchange(send_then_wait()) as exchange:
        feed = feed_for(exchange, subscribe_delay_s=0.3)
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: exchange.received)

    (records,) = recording.files()
    connected, sent = records[0], records[1]
    assert sent["mono_ns"] - connected["mono_ns"] >= 0.3 * 10**9


# --------------------------------------------------------------------------
# Staying connected
# --------------------------------------------------------------------------


def test_replies_when_the_exchange_expects_an_answer(tmp_path):
    def answer_heartbeats(raw):
        if "heartbeat" not in raw:
            return None
        return json.dumps({"id": json.loads(raw)["id"], "method": "respond"})

    heartbeat = '{"id":42,"method":"heartbeat"}'
    with PretendExchange(send_then_wait("book", heartbeat)) as exchange:
        feed = feed_for(exchange, reply=answer_heartbeats)
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: len(exchange.received) == 2)

    assert json.loads(exchange.received[1]) == {"id": 42, "method": "respond"}
    (records,) = recording.files()
    assert summary(records) == [
        "connected",
        "sent",
        "book",
        heartbeat,
        "sent",
        "stopped",
    ]
    assert json.loads(records[4]["raw"]) == {"id": 42, "method": "respond"}


def test_sends_keepalive_messages_on_schedule(tmp_path):
    with PretendExchange(send_then_wait("book")) as exchange:
        feed = feed_for(exchange, keepalive_message="ping", keepalive_every_s=0.1)
        with Recording(feed, tmp_path):
            wait_for(lambda: exchange.received.count("ping") >= 3)

    assert exchange.received[0] == SUBSCRIBE


# --------------------------------------------------------------------------
# The full-book download some feeds need
# --------------------------------------------------------------------------


class PretendWebServer:
    """Serves one fixed text at any address, and counts the requests.

    delay_s makes every answer take that long, like a slow website.
    """

    def __init__(self, body, delay_s=0.0):
        outer = self
        self.requests = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests += 1
                time.sleep(delay_s)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, *args):  # keep the test output quiet
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/order_book"

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()  # frees the listening socket


def test_downloads_and_records_the_full_book(tmp_path):
    body = '{"bids":[["58888.08","0.37"]],"asks":[["58888.09","0.12"]]}'
    with (
        PretendWebServer(body) as web,
        PretendExchange(send_then_wait("u")) as exchange,
    ):
        feed = feed_for(exchange, rest_snapshot_url=web.url, rest_snapshot_after_s=0.1)
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: web.requests >= 1)
            time.sleep(0.2)

    (records,) = recording.files()
    (snapshot,) = [r for r in records if r.get("event") == "rest_snapshot"]
    assert snapshot["raw"] == body
    assert snapshot["url"] == web.url
    assert web.requests == 1  # once per connection unless asked to repeat


def test_repeats_the_full_book_download_when_asked(tmp_path):
    with (
        PretendWebServer("{}") as web,
        PretendExchange(send_then_wait("u")) as exchange,
    ):
        feed = feed_for(
            exchange,
            rest_snapshot_url=web.url,
            rest_snapshot_after_s=0.05,
            rest_snapshot_every_s=0.1,
        )
        with Recording(feed, tmp_path):
            wait_for(lambda: web.requests >= 3)


def test_a_failed_download_is_recorded_and_tried_again(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "SNAPSHOT_RETRY_S", 0.05)
    with PretendExchange(send_then_wait("update")) as exchange:
        feed = feed_for(
            exchange,
            rest_snapshot_url="http://127.0.0.1:9/nothing-listens-here",
            rest_snapshot_after_s=0.05,
        )
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: exchange.received)
            time.sleep(0.4)

    (records,) = recording.files()
    labels = summary(records)
    assert labels.count("rest_snapshot_failed") >= 2  # it kept trying
    assert "update" in labels  # and kept recording meanwhile


def test_messages_keep_flowing_while_the_full_book_downloads(tmp_path):
    def script(connection, exchange):
        exchange.note(connection.recv())
        for number in range(10):  # one message every 0.1 s, for about a second
            connection.send(f"update {number}")
            time.sleep(0.1)
        for text in connection:
            exchange.note(text)

    with PretendWebServer("{}", delay_s=0.6) as web, PretendExchange(script) as exchange:
        feed = feed_for(exchange, rest_snapshot_url=web.url, rest_snapshot_after_s=0.05)
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: web.requests >= 1)
            time.sleep(1.3)

    (records,) = recording.files()
    labels = summary(records)
    snapshot = records[labels.index("rest_snapshot")]
    stamped_during_download = [
        r for r in records
        if "raw" in r and r.get("event") is None
        and snapshot["requested_wall_ns"] < r["wall_ns"] < snapshot["received_wall_ns"]
    ]
    # The download took 0.6 s; messages arriving meanwhile were taken in at
    # once, not held back until the download finished.
    assert len(stamped_during_download) >= 3
    assert snapshot["received_wall_ns"] - snapshot["requested_wall_ns"] >= 0.6 * 10**9


def test_connects_with_room_for_a_backlog_and_no_size_limit(tmp_path, monkeypatch):
    seen = {}

    def pretend_connect(url, **options):
        seen.update(options)
        raise OSError("stop here")

    monkeypatch.setattr(recorder, "connect", pretend_connect)
    feed = Feed(name="pretend", url="ws://unused", subscribe=())
    with Recording(feed, tmp_path):
        wait_for(lambda: seen)

    # If the library's waiting room fills, it stops reading the network and
    # stops answering the exchange's pings, and the exchange hangs up.
    assert seen["max_queue"] >= 1024
    assert seen["max_size"] is None  # a full book can be several megabytes


def test_reconnects_at_once_when_the_exchange_asks(tmp_path):
    request = '{"event":"please reconnect"}'
    with PretendExchange(send_then_wait("book", request), send_then_wait("book")) as exchange:
        feed = feed_for(exchange, reconnect_when=lambda raw: "please reconnect" in raw)
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: exchange.connections >= 2 and len(exchange.received) >= 2)
            time.sleep(0.1)

    first = recording.files()[0]
    assert summary(first) == ["connected", "sent", "book", request, "requested_reconnect"]


# --------------------------------------------------------------------------
# Trouble
# --------------------------------------------------------------------------


def test_keeps_trying_while_the_exchange_is_unreachable(tmp_path):
    feed = Feed(name="pretend", url="ws://127.0.0.1:9", subscribe=(SUBSCRIBE,))
    with Recording(feed, tmp_path) as recording:
        time.sleep(0.3)  # several failed attempts happen here

    assert recording.files() == []  # nothing to record, and no crash


def test_waits_instead_of_recording_onto_a_full_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(tape_module, "MIN_FREE_BYTES", 10**18)  # more than any disk
    with PretendExchange(send_then_wait("book")) as exchange:
        with Recording(feed_for(exchange), tmp_path) as recording:
            wait_for(lambda: exchange.connections >= 2)

    assert recording.files() == []


def test_stopping_leaves_a_complete_file(tmp_path):
    with PretendExchange(send_then_wait("book")) as exchange:
        with Recording(feed_for(exchange), tmp_path) as recording:
            wait_for(lambda: exchange.received)
            time.sleep(0.1)

    (path,) = sorted((tmp_path / "pretend").rglob("*.jsonl.gz"))
    with gzip.open(path, "rt", encoding="utf-8") as plain:
        lines = plain.read().splitlines()  # raises if the file's end is missing
    assert json.loads(lines[-1])["event"] == "stopped"
    assert summary(recording.files()[0])[-1] == "stopped"


# --------------------------------------------------------------------------
# Nothing received may be lost when a connection ends
# --------------------------------------------------------------------------


def stream_until_closed(counts):
    """Script: after the subscribe, send numbered messages as fast as possible
    until the client closes. counts[i] is how many connection i got out."""

    def script(connection, exchange):
        index = len(counts)
        counts.append(0)
        exchange.note(connection.recv())
        while True:
            try:
                connection.send(f"m{counts[index]}")
            except ConnectionClosed:
                return
            counts[index] += 1
            if counts[index] % 50 == 0:
                time.sleep(0.001)  # let the client keep up a little

    return script


def test_a_planned_reconnect_loses_nothing_already_received(tmp_path):
    counts = []
    with PretendExchange(stream_until_closed(counts)) as exchange:
        with Recording(
            feed_for(exchange, reconnect_offset_s=0), tmp_path, reconnect_period_s=1
        ) as recording:
            wait_for(lambda: exchange.connections >= 3, timeout=10)

    files = recording.files()
    assert len(files) >= 3
    for sent, records in zip(counts, files):
        written = [r["raw"] for r in records if "event" not in r]
        # Every message the exchange got out is on the tape, once, in order.
        assert written == [f"m{n}" for n in range(sent)]
    # The first two connections each ran a full period, so plenty was in flight
    # (the last one is cut short when the test stops).
    assert counts[0] > 100 and counts[1] > 100


def test_stopping_loses_nothing_already_received(tmp_path):
    counts = []
    with PretendExchange(stream_until_closed(counts)) as exchange:
        with Recording(feed_for(exchange), tmp_path) as recording:
            wait_for(lambda: counts and counts[0] > 500)

    (records,) = recording.files()
    written = [r["raw"] for r in records if "event" not in r]
    assert written == [f"m{n}" for n in range(counts[0])]


def test_a_reconnect_request_loses_nothing_already_received(tmp_path):
    request = '{"event":"please reconnect"}'
    counts = []
    streaming = stream_until_closed(counts)

    def ask_then_stream(connection, exchange):
        connection.send(request)  # sent before the subscribe is even read
        streaming(connection, exchange)

    with PretendExchange(ask_then_stream, send_then_wait()) as exchange:
        feed = feed_for(exchange, reconnect_when=lambda raw: "please reconnect" in raw)
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: exchange.connections >= 2)
            time.sleep(0.2)

    first = recording.files()[0]
    written = [r["raw"] for r in first if "event" not in r]
    # Everything the exchange got out before it saw the recorder's goodbye.
    assert written == [request, *[f"m{n}" for n in range(counts[0])]]


def test_a_failed_reply_loses_nothing_already_received(tmp_path):
    def slow_reply(raw):
        if raw == "heartbeat":
            time.sleep(0.3)  # the exchange hangs up before the answer goes out
            return "answer"
        return None

    burst = [f"after {n}" for n in range(500)]
    with PretendExchange(send_then_hang_up("heartbeat", *burst)) as exchange:
        feed = feed_for(exchange, reply=slow_reply)
        with Recording(feed, tmp_path) as recording:
            wait_for(lambda: exchange.connections >= 2)
            time.sleep(0.2)

    first = recording.files()[0]
    written = [r["raw"] for r in first if "event" not in r]
    assert written == ["heartbeat", *burst]


def test_repeated_reconnect_requests_slow_down(tmp_path):
    request = '{"event":"please reconnect"}'
    with PretendExchange(send_then_wait(request)) as exchange:
        feed = feed_for(exchange, reconnect_when=lambda raw: "please reconnect" in raw)
        with Recording(feed, tmp_path):
            time.sleep(1.0)

    # Waits of 0.02, 0.04, 0.08 ... s (shrunk for the test): about six
    # attempts in a second, not hundreds.
    assert exchange.connections <= 10


def test_a_message_that_is_not_valid_text_is_kept_and_recording_continues(tmp_path):
    def script(connection, exchange):
        exchange.note(connection.recv())
        connection.send(b"\xff\xfe not UTF-8", text=True)  # a broken text message
        connection.send("after")
        for text in connection:
            exchange.note(text)

    with PretendExchange(script) as exchange:
        with Recording(feed_for(exchange), tmp_path) as recording:
            wait_for(lambda: exchange.received)
            time.sleep(0.2)

    (records,) = recording.files()
    messages = [tape_module.payload_of(r) for r in records if "event" not in r]
    assert messages == [b"\xff\xfe not UTF-8", "after"]
    assert exchange.connections == 1  # the connection survived


def test_a_full_disk_mid_file_waits_instead_of_reconnecting(tmp_path, monkeypatch, capsys):
    import errno

    def no_space(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tape_module.Tape, "message", no_space)
    monkeypatch.setattr(recorder, "DISK_FULL_WAIT_S", 0.5)
    with PretendExchange(send_then_wait("book")) as exchange:
        with Recording(feed_for(exchange), tmp_path):
            time.sleep(0.4)

    assert exchange.connections == 1  # it waited; it did not hammer the exchange
    assert "not recording" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Planned reconnects never fall in the measured minutes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds_into_quarter, allowed",
    [(0, True), (300, True), (719, True), (720, False), (870, False), (899, False)],
)
def test_planned_reconnects_avoid_the_minutes_before_each_quarter_hour(
    seconds_into_quarter, allowed
):
    quarter = 1_800_000_000 - (1_800_000_000 % 900)
    assert REAL_MAY_RECONNECT_ON_PURPOSE(quarter + seconds_into_quarter) is allowed
