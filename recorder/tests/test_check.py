"""Tests for check.py, the checker for recorded data.

All recordings here are made up by the tests themselves. Times are chosen so
that each test controls exactly which measured windows a file spans.
"""

import gzip
import json
import zlib
from datetime import datetime, timezone

import pytest

import check
from check import FileResult, measured_windows, window_covered

S = 10**9  # one second in nanoseconds
# A quarter hour (12:15:00 UTC on 2026-11-22), in nanoseconds since 1970.
Q = int(datetime(2026, 11, 22, 12, 15, tzinfo=timezone.utc).timestamp()) * S


def write_file(data_dir, feed, start_ns, lines, complete=True):
    """Write a tape file by hand. lines are (wall_ns, record-without-stamps)."""
    started = datetime.fromtimestamp(start_ns / S, timezone.utc)
    folder = data_dir / feed / started.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{feed}_{started.strftime('%Y%m%dT%H%M%SZ')}.jsonl.gz"
    text = "".join(
        json.dumps({"wall_ns": wall, "mono_ns": wall, **record}) + "\n"
        for wall, record in lines
    ).encode()
    data = gzip.compress(text)
    if not complete:
        data = data[: len(data) - 8]  # no gzip trailer: the file is still open
    path.write_bytes(data)
    return path


def messages(start_ns, step_ns, raws):
    """Messages at start, start+step, ... as (wall_ns, record)."""
    return [(start_ns + i * step_ns, {"raw": raw}) for i, raw in enumerate(raws)]


def event(wall_ns, kind, **details):
    return (wall_ns, {"event": kind, **details})


def run_check(path_or_paths, feed):
    paths = path_or_paths if isinstance(path_or_paths, list) else [path_or_paths]
    return [check.check_file(feed, p, newest=(p == paths[-1])) for p in paths]


# --------------------------------------------------------------------------
# Measured windows
# --------------------------------------------------------------------------


def test_measured_windows_are_the_quarter_hours_whose_minute_fits():
    # From 12:13:30 to 12:46:00: the windows ending 12:15, 12:30 and 12:45.
    assert list(measured_windows(Q - 90 * S, Q + 1860 * S)) == [
        Q,
        Q + 900 * S,
        Q + 1800 * S,
    ]
    # A span starting inside a window does not count that window.
    assert list(measured_windows(Q - 30 * S, Q + 10 * S)) == []


def test_a_window_is_covered_only_by_one_file_spanning_all_of_it():
    whole = FileResult(name="a", ready_ns=Q - 100 * S, end_ns=Q + 5 * S)
    late = FileResult(name="b", ready_ns=Q - 30 * S, end_ns=Q + 100 * S)
    early = FileResult(name="c", ready_ns=Q - 300 * S, end_ns=Q - 10 * S)
    assert window_covered(Q, [whole])
    assert not window_covered(Q, [late])
    assert not window_covered(Q, [early, late])  # two files with a reconnect between


def test_a_break_inside_a_window_spoils_it():
    broken = FileResult(
        name="a", ready_ns=Q - 100 * S, end_ns=Q + 100 * S, break_ns=[Q - 20 * S]
    )
    assert not window_covered(Q, [broken])


def test_an_invalid_book_spoils_every_later_window_in_the_file():
    invalid = FileResult(
        name="a",
        ready_ns=Q - 1000 * S,
        end_ns=Q + 1000 * S,
        invalid_from_ns=Q - 500 * S,
    )
    assert not window_covered(Q, [invalid])
    assert window_covered(
        Q - 900 * S,
        [
            FileResult(
                name="b", ready_ns=Q - 1000 * S, end_ns=Q, invalid_from_ns=Q - 500 * S
            )
        ],
    )


# --------------------------------------------------------------------------
# How files ended
# --------------------------------------------------------------------------


def test_reports_how_each_file_ended(tmp_path):
    a = write_file(
        tmp_path,
        "lmax",
        Q - 200 * S,
        [event(Q - 200 * S, "connected"), event(Q - 100 * S, "stale")],
    )
    b = write_file(
        tmp_path,
        "lmax",
        Q - 90 * S,
        [event(Q - 90 * S, "connected"), event(Q, "planned_reconnect")],
    )
    a_result, b_result = run_check([a, b], "lmax")
    assert a_result.ended == "stale" and a_result.unplanned
    assert b_result.ended == "planned_reconnect" and not b_result.unplanned


def test_a_cut_off_file_is_a_problem_unless_it_is_the_one_being_written(tmp_path):
    lines = [event(Q - 200 * S, "connected")]
    cut = write_file(tmp_path, "lmax", Q - 200 * S, lines, complete=False)
    newest = write_file(
        tmp_path, "lmax", Q - 100 * S, [event(Q - 100 * S, "connected")], complete=False
    )
    cut_result, newest_result = run_check([cut, newest], "lmax")
    assert any("cut off" in text for _, text in cut_result.problems)
    assert newest_result.problems == []


# --------------------------------------------------------------------------
# Coinbase: sequence numbers
# --------------------------------------------------------------------------


def coinbase(seq, kind="update"):
    events = [{"type": kind, "product_id": "BTC-USD", "updates": []}]
    return json.dumps(
        {"channel": "l2_data", "timestamp": "x", "sequence_num": seq, "events": events}
    )


def test_coinbase_clean_sequence(tmp_path):
    raws = [coinbase(0, "snapshot")] + [coinbase(n) for n in range(1, 50)]
    path = write_file(
        tmp_path, "coinbase", Q - 200 * S, messages(Q - 200 * S, 5 * S, raws)
    )
    (result,) = run_check(path, "coinbase")
    assert result.ready_ns == Q - 200 * S
    assert result.break_ns == [] and result.invalid_from_ns is None
    assert result.counts["messages"] == 50


def test_coinbase_missing_message_invalidates_the_rest_of_the_file(tmp_path):
    raws = [coinbase(0, "snapshot"), coinbase(1), coinbase(3), coinbase(4)]
    path = write_file(tmp_path, "coinbase", Q - 200 * S, messages(Q - 200 * S, S, raws))
    (result,) = run_check(path, "coinbase")
    assert result.invalid_from_ns == Q - 198 * S
    assert any("sequence" in text for _, text in result.problems)


# --------------------------------------------------------------------------
# Kraken: checksum of the rebuilt book
# --------------------------------------------------------------------------


def kraken_checksum(bids, asks):
    """The documented rule, written out plainly for the test: top ten asks
    (low to high) then top ten bids (high to low); price then quantity, each
    with the decimal point removed and leading zeros dropped."""

    def digits(text):
        return text.replace(".", "").lstrip("0")

    top_asks = sorted(asks, key=lambda level: float(level[0]))[:10]
    top_bids = sorted(bids, key=lambda level: -float(level[0]))[:10]
    text = "".join(digits(p) + digits(q) for p, q in top_asks + top_bids)
    return zlib.crc32(text.encode()) & 0xFFFFFFFF


def kraken(kind, bids, asks, book_bids, book_asks, checksum=None):
    """A Kraken book message. bids/asks are the levels in the message;
    book_* are the full book after it, used for the checksum."""

    def levels(pairs):
        return "[" + ",".join(f'{{"price":{p},"qty":{q}}}' for p, q in pairs) + "]"

    value = kraken_checksum(book_bids, book_asks) if checksum is None else checksum
    return (
        f'{{"channel":"book","type":"{kind}","data":[{{"symbol":"BTC/USD",'
        f'"bids":{levels(bids)},"asks":{levels(asks)},"checksum":{value}}}]}}'
    )


def test_kraken_checksums_match_on_a_correct_book(tmp_path):
    bids = [("57748.0", "0.36244207"), ("57741.7", "0.00102538")]
    asks = [("57748.1", "1.50000000"), ("57750.0", "0.00010000")]
    after_update = [("57748.0", "0.30000000"), ("57741.7", "0.00102538")]
    raws = [
        '{"channel":"heartbeat"}',
        kraken("snapshot", bids, asks, bids, asks),
        kraken("update", [("57748.0", "0.30000000")], [], after_update, asks),
        kraken("update", [], [("57750.0", "0.00000000")], after_update, asks[:1]),
    ]
    path = write_file(tmp_path, "kraken", Q - 200 * S, messages(Q - 200 * S, S, raws))
    (result,) = run_check(path, "kraken")
    assert result.counts["checksums_matched"] == 3
    assert result.break_ns == [] and result.invalid_from_ns is None


def test_kraken_wrong_checksum_invalidates_the_rest_of_the_file(tmp_path):
    bids = [("57748.0", "0.36244207")]
    asks = [("57748.1", "1.50000000")]
    raws = [
        kraken("snapshot", bids, asks, bids, asks),
        kraken("update", [], [], bids, asks, checksum=12345),
    ]
    path = write_file(tmp_path, "kraken", Q - 200 * S, messages(Q - 200 * S, S, raws))
    (result,) = run_check(path, "kraken")
    assert result.invalid_from_ns == Q - 199 * S


# --------------------------------------------------------------------------
# Gemini: overlapping update numbers and the top-five check
# --------------------------------------------------------------------------


def gemini(first, last, bids=(), asks=()):
    return json.dumps(
        {
            "e": "depthUpdate",
            "E": 1,
            "s": "btcusd",
            "U": first,
            "u": last,
            "b": [list(level) for level in bids],
            "a": [list(level) for level in asks],
        }
    )


def test_gemini_overlapping_numbers_and_a_matching_top_five(tmp_path):
    raws = [
        gemini(100, 100, [("10.00", "1.0")], [("11.00", "2.0")]),  # the full book
        gemini(100, 130, [("10.50", "0.5")]),
        gemini(130, 150, [], [("11.00", "0.0"), ("12.00", "3.0")]),
        json.dumps(
            {
                "lastUpdateId": 150,
                "symbol": "btcusd",
                "bids": [["10.50", "0.5"], ["10.00", "1.0"]],
                "asks": [["12.00", "3.0"]],
            }
        ),
    ]
    path = write_file(tmp_path, "gemini", Q - 200 * S, messages(Q - 200 * S, S, raws))
    (result,) = run_check(path, "gemini")
    assert result.invalid_from_ns is None
    assert (
        result.counts["top_five_matched"] == 1
        and result.counts["top_five_compared"] == 1
    )


def test_gemini_skipped_numbers_invalidate_the_rest_of_the_file(tmp_path):
    raws = [gemini(100, 100, [("10", "1")], [("11", "1")]), gemini(140, 150)]
    path = write_file(tmp_path, "gemini", Q - 200 * S, messages(Q - 200 * S, S, raws))
    (result,) = run_check(path, "gemini")
    assert result.invalid_from_ns == Q - 199 * S


def test_gemini_top_five_mismatch_is_a_problem(tmp_path):
    raws = [
        gemini(100, 100, [("10.00", "1.0")], [("11.00", "2.0")]),
        json.dumps(
            {
                "lastUpdateId": 100,
                "symbol": "btcusd",
                "bids": [["10.00", "9.0"]],
                "asks": [["11.00", "2.0"]],
            }
        ),
    ]
    path = write_file(tmp_path, "gemini", Q - 200 * S, messages(Q - 200 * S, S, raws))
    (result,) = run_check(path, "gemini")
    assert (
        result.counts["top_five_compared"] == 1
        and result.counts.get("top_five_matched", 0) == 0
    )
    assert result.invalid_from_ns == Q - 199 * S


# --------------------------------------------------------------------------
# Crypto.com: each update names the previous one; heartbeats answered
# --------------------------------------------------------------------------


def cryptocom(channel, u, pu=None):
    data = {"t": 1, "u": u}
    if channel == "book":
        data.update(bids=[["10.00", "1", "1"]], asks=[["11.00", "1", "1"]])
    else:
        data.update(pu=pu, update={"bids": [], "asks": []})
    return json.dumps(
        {
            "id": -1,
            "method": "subscribe",
            "code": 0,
            "result": {
                "instrument_name": "BTC_USD",
                "channel": channel,
                "data": [data],
            },
        }
    )


def test_cryptocom_chain_and_heartbeats(tmp_path):
    heartbeat = '{"id":17,"method":"public/heartbeat","code":0}'
    lines = messages(
        Q - 200 * S,
        S,
        [cryptocom("book", 32), cryptocom("book.update", 96, 32), heartbeat],
    )
    lines.append(
        event(Q - 197 * S, "sent", raw='{"id":17,"method":"public/respond-heartbeat"}')
    )
    lines += messages(Q - 196 * S, S, [cryptocom("book.update", 160, 96)])
    path = write_file(tmp_path, "cryptocom", Q - 200 * S, lines)
    (result,) = run_check(path, "cryptocom")
    assert result.invalid_from_ns is None
    assert (
        result.counts["heartbeats_received"] == 1
        and result.counts["heartbeats_answered"] == 1
    )


def test_cryptocom_broken_chain_invalidates_the_rest_of_the_file(tmp_path):
    raws = [cryptocom("book", 32), cryptocom("book.update", 160, 96)]
    path = write_file(
        tmp_path, "cryptocom", Q - 200 * S, messages(Q - 200 * S, S, raws)
    )
    (result,) = run_check(path, "cryptocom")
    assert result.invalid_from_ns == Q - 199 * S


def test_cryptocom_unanswered_heartbeat_is_a_problem(tmp_path):
    raws = [cryptocom("book", 32), '{"id":17,"method":"public/heartbeat","code":0}']
    path = write_file(
        tmp_path, "cryptocom", Q - 200 * S, messages(Q - 200 * S, S, raws)
    )
    (result,) = run_check(path, "cryptocom")
    assert any("heartbeat" in text for _, text in result.problems)


# --------------------------------------------------------------------------
# Bullish and LMAX: full books, so a break spoils only its own moment
# --------------------------------------------------------------------------


def bullish(lower, upper):
    return json.dumps(
        {
            "type": "snapshot",
            "dataType": "V1TALevel2",
            "data": {
                "symbol": "BTCUSD",
                "bids": ["10.0", "1.0"],
                "asks": ["11.0", "1.0"],
                "sequenceNumberRange": [lower, upper],
            },
        }
    )


def test_bullish_sequence_ranges(tmp_path):
    raws = [bullish(1, 1), bullish(2, 5), bullish(9, 9), bullish(10, 12)]
    path = write_file(tmp_path, "bullish", Q - 200 * S, messages(Q - 200 * S, S, raws))
    (result,) = run_check(path, "bullish")
    assert result.break_ns == [Q - 198 * S]  # 6 to 8 were missed
    assert result.invalid_from_ns is None  # the next full book repairs it


def lmax(stamp):
    return json.dumps(
        {
            "type": "ORDER_BOOK",
            "instrument_id": "btc-usd",
            "timestamp": stamp,
            "status": "OPEN",
            "bids": [],
            "asks": [],
        }
    )


def test_lmax_a_skipped_second_is_a_break(tmp_path):
    stamps = [
        "2026-11-22T12:11:40.000Z",
        "2026-11-22T12:11:41.000Z",
        "2026-11-22T12:11:43.000Z",
    ]
    path = write_file(
        tmp_path,
        "lmax",
        Q - 200 * S,
        messages(Q - 200 * S, S, [lmax(s) for s in stamps]),
    )
    (result,) = run_check(path, "lmax")
    assert result.break_ns == [Q - 198 * S]


# --------------------------------------------------------------------------
# Bitstamp: no integrity data, so only order and downloads are checked
# --------------------------------------------------------------------------


def bitstamp(micro):
    return json.dumps(
        {
            "data": {
                "timestamp": "1",
                "microtimestamp": str(micro),
                "bids": [],
                "asks": [],
            },
            "channel": "diff_order_book_btcusd",
            "event": "data",
        }
    )


def test_bitstamp_ready_at_the_first_downloaded_book(tmp_path):
    lines = messages(Q - 200 * S, S, [bitstamp(100), bitstamp(200)])
    lines.append(
        event(
            Q - 197 * S,
            "rest_snapshot",
            raw='{"microtimestamp":"150","bids":[],"asks":[]}',
        )
    )
    lines += messages(Q - 196 * S, S, [bitstamp(300)])
    path = write_file(tmp_path, "bitstamp", Q - 200 * S, lines)
    (result,) = run_check(path, "bitstamp")
    assert result.ready_ns == Q - 197 * S
    assert result.counts["book_downloads"] == 1


def test_bitstamp_changes_out_of_order_are_a_break(tmp_path):
    lines = [
        event(
            Q - 201 * S,
            "rest_snapshot",
            raw='{"microtimestamp":"50","bids":[],"asks":[]}',
        )
    ]
    lines += messages(Q - 200 * S, S, [bitstamp(100), bitstamp(90)])
    path = write_file(tmp_path, "bitstamp", Q - 201 * S, lines)
    (result,) = run_check(path, "bitstamp")
    assert result.invalid_from_ns == Q - 199 * S


# --------------------------------------------------------------------------
# The whole command
# --------------------------------------------------------------------------


def lmax_file(data_dir, start_ns, seconds, skip_at=None):
    stamps, lines = [], []
    for i in range(seconds):
        if i == skip_at:
            continue
        stamp = datetime.fromtimestamp(start_ns / S + i, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        lines.append((start_ns + i * S, {"raw": lmax(stamp)}))
    lines.append(event(start_ns + seconds * S, "planned_reconnect"))
    return write_file(
        data_dir, "lmax", start_ns, [event(start_ns - S, "connected")] + lines
    )


def test_command_reports_coverage_and_exits_zero_when_clean(tmp_path, capsys):
    lmax_file(tmp_path, Q - 120 * S, 1100)  # spans the windows ending Q and Q+900 s
    code = check.main(["--data-dir", str(tmp_path), "--all"])
    out = capsys.readouterr().out
    assert code == 0
    assert "lmax" in out and "2/2" in out


def test_command_exits_one_and_lists_the_problem_when_a_window_is_broken(
    tmp_path, capsys
):
    lmax_file(tmp_path, Q - 120 * S, 1100, skip_at=90)  # a skipped second at Q-30 s
    code = check.main(["--data-dir", str(tmp_path), "--all"])
    out = capsys.readouterr().out
    assert code == 1
    assert "1/2" in out and "skipped" in out
