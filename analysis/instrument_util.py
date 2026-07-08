"""Small utilities shared by the capture instruments (poller, recorder)."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def until_to_duration_s(until_hhmm: str, now: datetime | None = None) -> float:
    """Seconds from now until HH:MM Eastern today (tomorrow if already past)."""
    eastern = ZoneInfo("America/New_York")
    now = now or datetime.now(tz=eastern)
    hour, minute = (int(p) for p in until_hhmm.split(":"))
    target = now.astimezone(eastern).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def inhibit_sleep() -> None:
    """Keep Windows awake for the duration of a capture run. Does NOT
    prevent Windows Update restarts -- that's a runbook item."""
    if hasattr(ctypes, "windll"):
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def restore_sleep() -> None:
    if hasattr(ctypes, "windll"):
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def git_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def stamp() -> dict:
    return {"wall_ns": time.time_ns(), "mono_ns": time.monotonic_ns()}


def run_metadata() -> dict:
    return {"argv": sys.argv[1:], "git_hash": git_hash()}
