"""WebSocket tape recorder for Kalshi market data.

Deliberately minimal: tape + gap log + re-snapshot requests. It keeps NO
order-book replica and derives nothing -- parsing exists only to route
control messages and check sequence continuity. Every inbound frame is
clock-stamped the moment recv() returns and written verbatim before any
parsing; every outbound command is taped too. Interpretation is a
replay-time concern (capture first, parse later).

Subscription shape (verified against the live server 2026-07-08): the
server keeps ONE subscription per channel per connection -- repeated
same-channel subscribes are merged into the existing sid and acked with
"ok", so per-market sequence isolation is impossible on a single
connection. Each channel is therefore subscribed once with the full
ticker list. The per-sid envelope chain is a complete loss detector
(every envelope type, including "ok" responses, consumes a seq value);
instrument-TIER isolation is achieved by running one recorder process
per tier -- separate connection, separate sid chain, separate tape.
A sequence gap triggers a rate-limited get_snapshot; false positives are
benign -- they just add a snapshot to the tape.

Usage:
  python analysis/ws_recorder.py --events KXCPI-26JUN --until 09:45
  python analysis/ws_recorder.py --tickers KXCPI-26JUN-T-0.1 --duration-min 60
"""

from __future__ import annotations

import argparse
import asyncio
import email.utils
import itertools
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import websockets
from dotenv import load_dotenv

from instrument_util import inhibit_sleep, restore_sleep, run_metadata, stamp, until_to_duration_s
from kalshi_rest import get
from kalshi_ws import (
    WS_URL,
    GapDetector,
    backoff_delay,
    build_ws_auth_headers,
    get_snapshot_cmd,
    load_private_key,
    parse_envelope,
    sign,
    subscribe_cmd,
    verify,
)
from market_discovery import fetch_event_markets
from tape import Tape, new_run_id

SCHEMA = "ws_recorder/1"
PLACEHOLDER = "paste-your-key-id-here"
SNAPSHOT_MIN_INTERVAL_S = 5.0
SNAPSHOT_BURSTS_BEFORE_MUTE = 3
AUTH_FAILURE_LIMIT = 3
STABLE_CONNECTION_S = 60.0
MAX_CLOCK_OFFSET_S = 5.0


@dataclass
class RecorderConfig:
    url: str
    key_id: str
    private_key: object
    tickers: list[str]
    channels: list[str]
    duration_s: float
    checkpoint_s: float
    out_dir: Path
    recv_timeout_s: float = 2.0


def planned_subscriptions(cfg: RecorderConfig) -> list[tuple[str, list[str]]]:
    # One subscribe per channel, full ticker list: the server merges
    # same-channel subscribes into one sid anyway (verified live).
    return [(channel, list(cfg.tickers)) for channel in cfg.channels]


def server_offset_s(local_wall_ns: int, date_header: str | None) -> float | None:
    """Local clock minus server clock, from an HTTP Date header (1 s
    granularity -- fine for catching multi-second skew that would break
    the signed-timestamp handshake)."""
    if not date_header:
        return None
    server = email.utils.parsedate_to_datetime(date_header)
    return local_wall_ns / 1e9 - server.timestamp()


async def record(cfg: RecorderConfig, stop_event: asyncio.Event | None = None) -> dict:
    run_id = new_run_id()
    frames = Tape(cfg.out_dir / f"{run_id}.frames.jsonl")
    events = Tape(cfg.out_dir / f"{run_id}.events.jsonl")

    def ev(kind: str, **kw) -> None:
        events.append({"schema": SCHEMA, "run_id": run_id, "kind": kind, **stamp(), **kw})

    def tape_out(raw: str) -> None:
        frames.append({"schema": SCHEMA, "run_id": run_id, "direction": "out", **stamp(), "raw": raw})

    ev(
        "run_start",
        **run_metadata(),
        tickers=cfg.tickers,
        channels=cfg.channels,
        duration_s=cfg.duration_s,
        checkpoint_s=cfg.checkpoint_s,
    )

    gaps = GapDetector()
    cmd_ids = itertools.count(1)
    counters = {"frames_in": 0, "gaps": 0, "reconnects": 0, "snapshots_requested": 0}
    start_mono = time.monotonic()
    deadline = start_mono + cfg.duration_s
    attempt = 0
    auth_failures = 0

    def stopping() -> bool:
        return (stop_event is not None and stop_event.is_set()) or time.monotonic() >= deadline

    while not stopping():
        connected_at: float | None = None
        try:
            timestamp_ms = time.time_ns() // 1_000_000  # fresh per attempt
            headers = build_ws_auth_headers(cfg.key_id, cfg.private_key, timestamp_ms)
            async with websockets.connect(
                cfg.url, additional_headers=headers, max_queue=None, open_timeout=15
            ) as ws:
                connected_at = time.monotonic()
                auth_failures = 0  # a successful handshake proves the credentials
                ev("connected", attempt=attempt)
                gaps.forget_all()  # new connection = new chains
                pending: dict[int, tuple[str, list[str]]] = {}
                sids: dict[int, tuple[str, list[str]]] = {}
                snap_last: dict[int, float] = {}
                snap_rapid: dict[int, int] = {}
                next_checkpoint = (
                    time.monotonic() + cfg.checkpoint_s if cfg.checkpoint_s > 0 else None
                )

                async def request_snapshot(sid: int, reason: str) -> None:
                    now = time.monotonic()
                    if now - snap_last.get(sid, -1e9) < SNAPSHOT_MIN_INTERVAL_S:
                        snap_rapid[sid] = snap_rapid.get(sid, 0) + 1
                        if snap_rapid[sid] >= SNAPSHOT_BURSTS_BEFORE_MUTE:
                            ev("snapshot_muted", sid=sid, reason=reason)
                        return
                    snap_last[sid] = now
                    snap_rapid[sid] = 0
                    channel, tickers = sids[sid]
                    cmd = get_snapshot_cmd(next(cmd_ids), [sid], tickers)
                    await ws.send(cmd)
                    tape_out(cmd)
                    counters["snapshots_requested"] += 1
                    ev("snapshot_requested", sid=sid, reason=reason)

                for channel, tickers in planned_subscriptions(cfg):
                    cid = next(cmd_ids)
                    pending[cid] = (channel, tickers)
                    cmd = subscribe_cmd(cid, [channel], tickers)
                    await ws.send(cmd)
                    tape_out(cmd)

                while not stopping():
                    if next_checkpoint and time.monotonic() >= next_checkpoint:
                        next_checkpoint += cfg.checkpoint_s
                        for sid, (channel, _) in list(sids.items()):
                            if channel == "orderbook_delta":
                                await request_snapshot(sid, "checkpoint")
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=cfg.recv_timeout_s)
                    except asyncio.TimeoutError:
                        continue
                    recv_wall_ns = time.time_ns()
                    recv_mono_ns = time.monotonic_ns()
                    counters["frames_in"] += 1
                    frames.append(
                        {
                            "schema": SCHEMA,
                            "run_id": run_id,
                            "direction": "in",
                            "recv_wall_ns": recv_wall_ns,
                            "recv_mono_ns": recv_mono_ns,
                            "raw": raw if isinstance(raw, str) else raw.decode("utf-8", "replace"),
                        }
                    )
                    try:
                        env = parse_envelope(raw)
                    except Exception as exc:  # raw is on tape; routing is best-effort
                        ev("unparseable_frame", error=repr(exc))
                        continue

                    if env.type == "subscribed":
                        sid = env.msg.get("sid")
                        channel = env.msg.get("channel", "")
                        _, tickers = pending.pop(env.id, (channel, []))
                        if sid is not None:
                            sids[sid] = (channel, tickers)
                            ev("subscribed", sid=sid, channel=channel, tickers=tickers)
                    elif env.type == "error":
                        ev("server_error", cmd_id=env.id, msg=env.msg)
                    elif env.type == "orderbook_snapshot" and env.sid is not None and env.seq is not None:
                        gaps.resync(f"sid:{env.sid}", env.seq)
                        ev("snapshot_resync", sid=env.sid, seq=env.seq)
                    elif env.sid is not None and env.seq is not None:
                        anomaly = gaps.observe(f"sid:{env.sid}", env.seq)
                        if anomaly:
                            counters["gaps"] += 1
                            ev(
                                "seq_anomaly",
                                anomaly=anomaly.kind,
                                sid=env.sid,
                                expected=anomaly.expected,
                                received=anomaly.received,
                            )
                            if env.sid in sids:
                                await request_snapshot(env.sid, f"seq_{anomaly.kind}")
        except websockets.InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            ev("handshake_rejected", status=status, error=repr(exc))
            if status in (401, 403):
                auth_failures += 1
                if auth_failures >= AUTH_FAILURE_LIMIT:
                    ev("auth_giving_up", failures=auth_failures)
                    break
        except (websockets.WebSocketException, OSError, TimeoutError) as exc:
            ev("disconnected", error=repr(exc))

        if stopping():
            break
        # Stability resets the ladder; a flaky hour must not ratchet to permanent max waits.
        attempt = 0 if (connected_at and time.monotonic() - connected_at >= STABLE_CONNECTION_S) else attempt + 1
        counters["reconnects"] += 1
        delay = backoff_delay(attempt)
        ev("backoff", attempt=attempt, delay_s=round(delay, 3))
        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            else:
                await asyncio.sleep(delay)
        except asyncio.TimeoutError:
            pass

    ev("run_end", **counters)
    frames.close()
    events.close()
    return {**counters, "frames_path": str(frames.path), "events_path": str(events.path)}


def preflight_credentials() -> tuple[str, object]:
    load_dotenv()
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not key_id or key_id == PLACEHOLDER:
        sys.exit(
            "no API key id: open .env and replace the placeholder on the "
            "KALSHI_API_KEY_ID line with the key id from kalshi.com account settings"
        )
    private_key = load_private_key(key_path)
    probe_msg = f"{time.time_ns() // 1_000_000}GET/trade-api/ws/v2"
    verify(private_key.public_key(), probe_msg, sign(private_key, probe_msg))
    return key_id, private_key


def preflight_clock() -> float:
    with requests.Session() as session:
        resp = get(session, "/markets", {"limit": 1})
    offset = server_offset_s(resp.recv_wall_ns, resp.server_date)
    if offset is None:
        print("warning: no Date header; cannot check clock skew", file=sys.stderr)
        return 0.0
    if abs(offset) > MAX_CLOCK_OFFSET_S:
        sys.exit(
            f"local clock is {offset:+.1f}s from server time; the signed handshake "
            f"will fail. Run as admin: w32tm /resync"
        )
    return offset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", help="explicit market tickers")
    parser.add_argument("--events", nargs="+", help="record every market of these events")
    parser.add_argument("--channels", default="orderbook_delta,trade")
    parser.add_argument("--duration-min", type=float, default=60.0)
    parser.add_argument("--until", help="stop at HH:MM Eastern (overrides --duration-min)")
    parser.add_argument("--checkpoint-min", type=float, default=15.0)
    parser.add_argument("--out-dir", type=Path, default=Path("data/ws"))
    parser.add_argument("--url", default=WS_URL)
    args = parser.parse_args()

    key_id, private_key = preflight_credentials()
    offset = preflight_clock()
    print(f"clock offset vs server: {offset:+.1f}s (ok)")

    tickers = list(args.tickers or [])
    if args.events:
        with requests.Session() as session:
            for event in args.events:
                markets = fetch_event_markets(session, event, lambda r: None)
                tickers.extend(m.ticker for m in markets)
    if not tickers:
        print("need --tickers or --events", file=sys.stderr)
        return 2

    cfg = RecorderConfig(
        url=args.url,
        key_id=key_id,
        private_key=private_key,
        tickers=tickers,
        channels=[c.strip() for c in args.channels.split(",") if c.strip()],
        duration_s=until_to_duration_s(args.until) if args.until else args.duration_min * 60.0,
        checkpoint_s=args.checkpoint_min * 60.0,
        out_dir=args.out_dir,
    )
    print(f"recording {len(tickers)} markets for {cfg.duration_s/60:.1f} min")

    inhibit_sleep()
    try:
        result = asyncio.run(record(cfg))
    except KeyboardInterrupt:
        print("\ninterrupted; tapes closed cleanly", file=sys.stderr)
        return 0
    finally:
        restore_sleep()

    print(
        f"done: {result['frames_in']} frames, {result['gaps']} seq anomalies, "
        f"{result['reconnects']} reconnects -> {result['frames_path']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
