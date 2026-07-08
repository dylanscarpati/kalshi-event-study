"""Append-only JSONL tape shared by every capture instrument.

One file per run so concurrent processes can never interleave (Windows
append mode is seek-then-write, not atomic). One JSON record per line,
emitted in a single write() call and flushed per record, so an OS crash
costs at most the record in flight; fsync every ~1 s bounds loss on
power failure to that window. Records carry a "schema" field owned by
the instrument that wrote them.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def rest_record(schema: str, run_id: str, r) -> dict:
    """Standard record for one taped REST exchange (an ApiResponse).
    body_text is the verbatim response text, never re-encoded."""
    return {
        "schema": schema,
        "run_id": run_id,
        "recv_wall_ns": r.recv_wall_ns,
        "recv_mono_ns": r.recv_mono_ns,
        "elapsed_ms": r.elapsed_ms,
        "server_date": r.server_date,
        "request": {"path": r.path, "params": r.params},
        "http_status": r.http_status,
        "body_text": r.body_text,
    }


class Tape:
    def __init__(self, path: Path, fsync_interval_s: float = 1.0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.count = 0
        self._fsync_interval_s = fsync_interval_s
        self._last_fsync_mono = time.monotonic()
        self._f = path.open("a", encoding="utf-8")

    def append(self, record: dict) -> None:
        self._f.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._f.flush()
        self.count += 1
        now = time.monotonic()
        if now - self._last_fsync_mono >= self._fsync_interval_s:
            os.fsync(self._f.fileno())
            self._last_fsync_mono = now

    def close(self) -> None:
        if not self._f.closed:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()

    def __enter__(self) -> "Tape":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
