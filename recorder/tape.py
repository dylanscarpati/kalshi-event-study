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
        """Finish the current file and make it read-only. Safe to call twice.

        If finishing fails (for example because the disk is full), the error
        is passed on, but the file is still closed and forgotten, so the next
        open_new() starts cleanly. read_tape() reads such a file up to its
        last complete record, and is_complete() reports it as unfinished.
        """
        if self._gzip is None:
            return
        compressor, file, path = self._gzip, self._file, self._path
        self._gzip = self._file = None  # forget them first, whatever happens below
        try:
            compressor.close()  # writes the gzip trailer; does not close file
            file.flush()
            os.fsync(file.fileno())
        finally:
            file.close()
            os.chmod(path, 0o444)

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


# How much decompressed text read_tape produces per step. Small steps mean that
# when a damaged end is reached, only one small step has to be re-read slowly.
_STEP_BYTES = 64 * 1024


def _decompressed(path: Path) -> Iterator[bytes]:
    """Yield a tape's decompressed bytes, in order, as far as they are readable.

    A tape cut off mid-write simply ends. A tape whose end holds junk (which a
    power cut can leave behind) is read up to the last byte that decompresses:
    the step that hits the junk is replayed one byte at a time from a saved
    copy of the decompressor, so nothing readable before the junk is lost.
    """
    decompressor = zlib.decompressobj(wbits=31)  # 31 selects the gzip format
    pending = b""
    with open(path, "rb") as compressed:
        while True:
            if not pending:
                pending = compressed.read(1 << 20)
                if not pending:
                    return
            saved = decompressor.copy()
            try:
                out = decompressor.decompress(pending, _STEP_BYTES)
            except zlib.error:
                yield from _salvage(saved, pending)
                return
            pending = decompressor.unconsumed_tail
            if out:
                yield out
            if decompressor.eof:  # the end of the gzip data; anything after is ignored
                return


def _salvage(decompressor, data: bytes) -> Iterator[bytes]:
    """Replay one failing step, feeding the input a byte at a time.

    Feeding the input (not limiting the output) is what matters: zlib reads on
    past the last real byte within the same call, reaches the junk, and the
    whole call's output is discarded with the error. Fed one input byte at a
    time, the call that produces the last real byte never sees the junk.
    """
    for position in range(len(data)):
        try:
            piece = decompressor.decompress(data[position : position + 1])
        except zlib.error:
            return
        if piece:
            yield piece
        if decompressor.eof:
            return


def read_tape(path: Path) -> Iterator[dict]:
    """Yield each complete record in a tape, in the order it was written.

    A tape whose end is missing or damaged, because the recorder was killed or
    the machine lost power, is read as far as its last complete line. No error
    is raised for the missing end; use is_complete() to find out.
    """
    unfinished = b""
    for chunk in _decompressed(path):
        lines = (unfinished + chunk).split(b"\n")
        unfinished = lines.pop()  # text after the last line break, if any
        for line in lines:
            yield json.loads(line)


def is_complete(path: Path) -> bool:
    """True if the tape was finished properly, False if it was cut off."""
    decompressor = zlib.decompressobj(wbits=31)
    try:
        with open(path, "rb") as compressed:
            while chunk := compressed.read(1 << 20):
                decompressor.decompress(chunk)
                while decompressor.unconsumed_tail:
                    decompressor.decompress(decompressor.unconsumed_tail)
    except zlib.error:
        return False
    return decompressor.eof


def payload_of(record: dict) -> str | bytes | None:
    """The message a record holds, as received. None if the record is an event."""
    if "event" in record:
        return None
    if "raw" in record:
        return record["raw"]
    if "raw_b64" in record:
        return base64.b64decode(record["raw_b64"])
    return None
