"""Write and read the recorder's files, called tapes.

A tape is a gzip-compressed text file with one JSON object per line. A line is
either a message exactly as an exchange sent it:

    {"wall_ns": ..., "mono_ns": ..., "raw": "<the message, unchanged>"}

or an event the recorder wrote about itself:

    {"wall_ns": ..., "mono_ns": ..., "event": "connected", ...details}

wall_ns is the calendar clock in nanoseconds since 1970 (UTC). It can jump when
the machine corrects its clock. mono_ns is a clock that only moves forward, so
it is the one to use for measuring how long something took.

A tape is never reopened or appended to. Each connection gets a new file, so a
file that was cut short by a crash cannot damage what is written afterwards.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import shutil
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# How often written data is forced onto the disk. If the machine loses power,
# at most this many seconds of messages are lost.
FLUSH_EVERY_S = 2.0

# A new file is not started when less free disk than this remains.
MIN_FREE_BYTES = 2 * 1024**3


class DiskFull(RuntimeError):
    """Raised instead of starting a file on a disk that is nearly full."""


class Tape:
    """The file currently being written for one feed."""

    def __init__(self, data_dir: Path, feed_name: str) -> None:
        self._data_dir = Path(data_dir)
        self._feed_name = feed_name
        self._file = None  # the file on disk
        self._gzip = None  # the compressor writing into that file
        self._path: Path | None = None
        self._last_flush_mono = 0.0

    def open_new(self) -> Path:
        """Close the current file, if any, and start a new one. Returns its path."""
        self.close()
        now = datetime.now(timezone.utc)
        folder = self._data_dir / self._feed_name / now.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
        if free < MIN_FREE_BYTES:
            raise DiskFull(f"only {free} bytes free under {folder}")

        stem = f"{self._feed_name}_{now.strftime('%Y%m%dT%H%M%SZ')}"
        attempt = 1
        while True:
            suffix = "" if attempt == 1 else f"_{attempt}"
            path = folder / f"{stem}{suffix}.jsonl.gz"
            try:
                # "x" refuses to open a file that already exists, so an
                # existing tape can never be overwritten.
                self._file = open(path, "xb")
                break
            except FileExistsError:
                attempt += 1
        self._gzip = gzip.GzipFile(fileobj=self._file, mode="wb", compresslevel=6)
        self._path = path
        self._last_flush_mono = time.monotonic()
        return path

    def message(self, wall_ns: int, mono_ns: int, payload: str | bytes) -> None:
        """Write one message from the exchange, with the time it was received."""
        record: dict = {"wall_ns": wall_ns, "mono_ns": mono_ns}
        if isinstance(payload, str):
            record["raw"] = payload
        else:
            # JSON cannot hold arbitrary bytes, so binary messages are stored
            # as base64 text and turned back into bytes by payload_of().
            record["raw_b64"] = base64.b64encode(payload).decode("ascii")
        self._write(record)

    def event(self, kind: str, **details) -> None:
        """Write a note from the recorder itself, stamped with the current time."""
        record = {"wall_ns": time.time_ns(), "mono_ns": time.monotonic_ns()}
        record["event"] = kind
        record.update(details)
        self._write(record)

    def close(self) -> None:
        """Finish the current file and make it read-only. Safe to call twice."""
        if self._gzip is None:
            return
        self._gzip.close()  # writes the gzip trailer; does not close self._file
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        os.chmod(self._path, 0o444)
        self._gzip = None
        self._file = None

    def _write(self, record: dict) -> None:
        if self._gzip is None:
            raise RuntimeError("no tape is open; call open_new() first")
        line = json.dumps(record, separators=(",", ":")) + "\n"
        self._gzip.write(line.encode("utf-8"))
        if time.monotonic() - self._last_flush_mono >= FLUSH_EVERY_S:
            self._flush()

    def _flush(self) -> None:
        # Three layers hold data back: the compressor, Python's file buffer and
        # the operating system. Each must be told to pass its data on.
        self._gzip.flush(zlib.Z_SYNC_FLUSH)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._last_flush_mono = time.monotonic()


def read_tape(path: Path) -> Iterator[dict]:
    """Yield each complete record in a tape, in the order it was written.

    A tape whose end is missing, because the recorder was killed or the machine
    lost power, is read as far as its last complete line. No error is raised
    for the missing end.
    """
    decompressor = zlib.decompressobj(wbits=31)  # 31 selects the gzip format
    unfinished = b""
    with open(path, "rb") as compressed:
        while True:
            chunk = compressed.read(1 << 20)
            if not chunk:
                break
            lines = (unfinished + decompressor.decompress(chunk)).split(b"\n")
            unfinished = lines.pop()  # text after the last line break, if any
            for line in lines:
                yield json.loads(line)


def payload_of(record: dict) -> str | bytes | None:
    """The message a record holds, as received. None if the record is an event."""
    if "raw" in record:
        return record["raw"]
    if "raw_b64" in record:
        return base64.b64decode(record["raw_b64"])
    return None
