"""Tests for the tape: the compressed file the recorder writes.

Every test uses made-up messages. Nothing here touches the network.
"""

import gzip
import json
import os
import shutil
import stat

import pytest

import tape as tape_module
from tape import DiskFull, Tape, read_tape

AWKWARD_MESSAGES = [
    '{"type":"snapshot","bids":[["58892.28","0.0524"]]}',
    'a line break\ninside, a "quote", a back\\slash and a tab\t',
    "not JSON at all",
    "",
    "price in euros: 54 000 €, and an emoji \U0001f4c8",
]


def test_messages_come_back_exactly_as_written(tmp_path):
    tape = Tape(tmp_path, "coinbase")
    path = tape.open_new()
    for number, text in enumerate(AWKWARD_MESSAGES):
        tape.message(wall_ns=1000 + number, mono_ns=5000 + number, payload=text)
    tape.close()

    records = list(read_tape(path))

    assert [record["raw"] for record in records] == AWKWARD_MESSAGES
    assert [record["wall_ns"] for record in records] == [1000, 1001, 1002, 1003, 1004]
    assert [record["mono_ns"] for record in records] == [5000, 5001, 5002, 5003, 5004]


def test_binary_messages_come_back_exactly(tmp_path):
    payload = bytes(range(256))
    tape = Tape(tmp_path, "coinbase")
    path = tape.open_new()
    tape.message(wall_ns=1, mono_ns=2, payload=payload)
    tape.close()

    (record,) = read_tape(path)

    assert "raw" not in record
    assert tape_module.payload_of(record) == payload


def test_one_record_per_line_in_an_ordinary_gzip_file(tmp_path):
    tape = Tape(tmp_path, "kraken")
    path = tape.open_new()
    for text in AWKWARD_MESSAGES:
        tape.message(wall_ns=1, mono_ns=2, payload=text)
    tape.close()

    with gzip.open(path, "rt", encoding="utf-8") as plain:
        lines = plain.read().splitlines()

    assert len(lines) == len(AWKWARD_MESSAGES)
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_events_carry_both_clocks_and_their_details(tmp_path):
    tape = Tape(tmp_path, "kraken")
    path = tape.open_new()
    tape.event("connected", url="wss://example.invalid")
    tape.close()

    (record,) = read_tape(path)

    assert record["event"] == "connected"
    assert record["url"] == "wss://example.invalid"
    assert record["wall_ns"] > 1_600_000_000 * 10**9  # a real calendar time
    assert record["mono_ns"] > 0
    assert "raw" not in record


def test_file_is_named_by_feed_and_start_time(tmp_path):
    tape = Tape(tmp_path, "bitstamp")
    path = tape.open_new()
    tape.close()

    assert path.parent.parent == tmp_path / "bitstamp"
    assert len(path.parent.name) == len("2026-11-21")  # one folder per UTC day
    assert path.name.startswith("bitstamp_")
    assert path.name.endswith(".jsonl.gz")


def test_a_new_file_never_replaces_an_existing_one(tmp_path):
    tape = Tape(tmp_path, "bitstamp")
    paths = []
    for _ in range(3):  # three files started within the same second
        paths.append(tape.open_new())
        tape.message(wall_ns=1, mono_ns=2, payload="kept")
        tape.close()

    assert len(set(paths)) == 3
    for path in paths:
        assert [record["raw"] for record in read_tape(path)] == ["kept"]


def test_opening_a_new_file_closes_the_previous_one(tmp_path):
    tape = Tape(tmp_path, "gemini")
    first = tape.open_new()
    tape.message(wall_ns=1, mono_ns=2, payload="in the first file")
    second = tape.open_new()
    tape.message(wall_ns=3, mono_ns=4, payload="in the second file")
    tape.close()

    assert [r["raw"] for r in read_tape(first)] == ["in the first file"]
    assert [r["raw"] for r in read_tape(second)] == ["in the second file"]


def test_a_closed_file_is_read_only(tmp_path):
    tape = Tape(tmp_path, "gemini")
    path = tape.open_new()
    tape.close()

    mode = stat.S_IMODE(os.stat(path).st_mode)

    assert mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0


def test_closing_twice_is_harmless(tmp_path):
    tape = Tape(tmp_path, "gemini")
    tape.open_new()
    tape.close()
    tape.close()


def test_writing_without_an_open_file_is_an_error(tmp_path):
    tape = Tape(tmp_path, "gemini")
    with pytest.raises(RuntimeError):
        tape.message(wall_ns=1, mono_ns=2, payload="nowhere to go")


def test_a_killed_recorder_leaves_a_readable_file(tmp_path, monkeypatch):
    # Flush after every record, so what reached the disk is predictable.
    monkeypatch.setattr(tape_module, "FLUSH_EVERY_S", 0.0)
    tape = Tape(tmp_path, "coinbase")
    path = tape.open_new()
    for number in range(200):
        tape.message(wall_ns=number, mono_ns=number, payload=f"message {number}")

    # The process dies here: the file is copied as it stands, never closed.
    killed = tmp_path / "killed.jsonl.gz"
    shutil.copyfile(path, killed)
    tape.close()

    survivors = [record["raw"] for record in read_tape(killed)]

    assert survivors == [f"message {number}" for number in range(200)]


def test_a_file_cut_off_mid_write_yields_its_complete_records(tmp_path, monkeypatch):
    monkeypatch.setattr(tape_module, "FLUSH_EVERY_S", 0.0)
    tape = Tape(tmp_path, "coinbase")
    path = tape.open_new()
    for number in range(200):
        tape.message(wall_ns=number, mono_ns=number, payload=f"message {number}")
    tape.close()

    whole = path.read_bytes()
    for keep in (len(whole) // 2, len(whole) - 1, 40):
        cut = tmp_path / f"cut_{keep}.jsonl.gz"
        cut.write_bytes(whole[:keep])

        survivors = [record["raw"] for record in read_tape(cut)]

        # Whatever survives is an unbroken run from the start; nothing is garbled.
        assert survivors == [f"message {number}" for number in range(len(survivors))]
    assert len(list(read_tape(tmp_path / f"cut_{len(whole) // 2}.jsonl.gz"))) > 0


def test_data_reaches_the_disk_while_the_file_is_still_open(tmp_path, monkeypatch):
    monkeypatch.setattr(tape_module, "FLUSH_EVERY_S", 0.0)
    tape = Tape(tmp_path, "kraken")
    path = tape.open_new()
    tape.message(wall_ns=1, mono_ns=2, payload="x" * 10_000)

    still_open = [record["raw"] for record in read_tape(path)]
    tape.close()

    assert still_open == ["x" * 10_000]


def test_refuses_to_start_a_file_when_the_disk_is_nearly_full(tmp_path, monkeypatch):
    almost_full = shutil._ntuple_diskusage(total=100, used=99, free=1)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: almost_full)
    tape = Tape(tmp_path, "kraken")

    with pytest.raises(DiskFull):
        tape.open_new()
