"""Recorder tests against a local fake Kalshi WebSocket server.

The fake scripts the exact sequence the real server could produce:
subscribe acks, an initial snapshot, deltas with a deliberate seq gap,
then a dropped connection. The assertions are the reconnect-logic
contract: gap taped + get_snapshot sent, backoff, reconnect, resubscribe.
"""

import asyncio
import json
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

import websockets
from cryptography.hazmat.primitives.asymmetric import rsa

from ws_recorder import RecorderConfig, planned_subscriptions, record, server_offset_s


def _cfg(**kw):
    return RecorderConfig(
        url=kw.get("url", "ws://127.0.0.1:1"),
        key_id="test-key",
        private_key=kw.get("key"),
        tickers=kw.get("tickers", ["T1"]),
        channels=["orderbook_delta", "trade"],
        sub_shape=kw.get("sub_shape", "per-market"),
        duration_s=30.0,
        checkpoint_s=0.0,
        out_dir=kw["out_dir"],
        recv_timeout_s=0.2,
    )


def test_planned_subscriptions_per_market_vs_shared(tmp_path):
    cfg = _cfg(out_dir=tmp_path, tickers=["T1", "T2"])
    assert planned_subscriptions(cfg) == [
        ("orderbook_delta", ["T1"]),
        ("orderbook_delta", ["T2"]),
        ("trade", ["T1", "T2"]),
    ]
    cfg.sub_shape = "shared"
    assert planned_subscriptions(cfg) == [
        ("orderbook_delta", ["T1", "T2"]),
        ("trade", ["T1", "T2"]),
    ]


def test_server_offset_from_date_header():
    server_time = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
    header = format_datetime(server_time, usegmt=True)
    local_ns = int((server_time.timestamp() + 3.0) * 1e9)  # local 3 s ahead
    assert abs(server_offset_s(local_ns, header) - 3.0) < 0.001
    assert server_offset_s(local_ns, None) is None


def _delta(sid, seq, price="0.5100"):
    return json.dumps(
        {"type": "orderbook_delta", "sid": sid, "seq": seq,
         "msg": {"market_ticker": "T1", "price_dollars": price, "delta_fp": "1.00", "side": "yes", "ts_ms": 1}}
    )


def _snapshot(sid, seq):
    return json.dumps(
        {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
         "msg": {"market_ticker": "T1", "yes_dollars_fp": [["0.5000", "1.00"]]}}
    )


async def test_gap_snapshot_reconnect_resubscribe(tmp_path):
    connections = []
    second_conn_streaming = asyncio.Event()

    async def handler(ws):
        conn = {"subscribes": 0, "get_snapshots": 0}
        connections.append(conn)
        first = len(connections) == 1
        try:
            sids = {}
            while conn["subscribes"] < 2:  # recorder sends 2 subs: delta[T1] + trade shared
                cmd = json.loads(await ws.recv())
                assert cmd["cmd"] == "subscribe"
                conn["subscribes"] += 1
                sid = 100 + conn["subscribes"]
                sids[cmd["params"]["channels"][0]] = sid
                await ws.send(json.dumps(
                    {"id": cmd["id"], "type": "subscribed",
                     "msg": {"channel": cmd["params"]["channels"][0], "sid": sid}}
                ))
            delta_sid = sids["orderbook_delta"]
            await ws.send(_snapshot(delta_sid, 1))
            await ws.send(_delta(delta_sid, 2))
            if first:
                await ws.send(_delta(delta_sid, 4))  # seq 3 skipped: the gap
                cmd = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert cmd["cmd"] == "update_subscription"
                assert cmd["params"]["action"] == "get_snapshot"
                conn["get_snapshots"] += 1
                await ws.send(_snapshot(delta_sid, 5))
                await asyncio.sleep(0.1)
                await ws.close()  # force the reconnect path
            else:
                await ws.send(_delta(delta_sid, 3))
                second_conn_streaming.set()
                await asyncio.sleep(30)  # hold open until the test stops the recorder
        except (websockets.ConnectionClosed, asyncio.IncompleteReadError):
            pass

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    stop = asyncio.Event()
    cfg = _cfg(url=f"ws://127.0.0.1:{port}", key=key, out_dir=tmp_path)

    task = asyncio.create_task(record(cfg, stop_event=stop))
    try:
        await asyncio.wait_for(second_conn_streaming.wait(), timeout=20)
    finally:
        stop.set()
        result = await asyncio.wait_for(task, timeout=10)
        server.close()
        await server.wait_closed()

    assert len(connections) == 2, "recorder must reconnect after the drop"
    assert connections[0]["get_snapshots"] == 1, "gap must trigger exactly one get_snapshot"
    assert connections[1]["subscribes"] == 2, "recorder must resubscribe on the new connection"

    events = [json.loads(l) for l in Path(result["events_path"]).read_text(encoding="utf-8").splitlines()]
    kinds = [e["kind"] for e in events]
    assert kinds.count("connected") == 2
    assert "seq_anomaly" in kinds and "snapshot_requested" in kinds
    assert "disconnected" in kinds and "backoff" in kinds
    anomaly = next(e for e in events if e["kind"] == "seq_anomaly")
    assert anomaly["anomaly"] == "gap" and anomaly["expected"] == 3 and anomaly["received"] == 4
    # after the mid-stream snapshot resync (seq 5) and reconnect, no further anomalies
    assert kinds.count("seq_anomaly") == 1

    frames = [json.loads(l) for l in Path(result["frames_path"]).read_text(encoding="utf-8").splitlines()]
    inbound = [f for f in frames if f["direction"] == "in"]
    outbound = [f for f in frames if f["direction"] == "out"]
    assert all("recv_wall_ns" in f and "recv_mono_ns" in f for f in inbound)
    assert any("get_snapshot" in f["raw"] for f in outbound)
    inbound_types = {json.loads(f["raw"])["type"] for f in inbound}
    assert {"subscribed", "orderbook_snapshot", "orderbook_delta"} <= inbound_types
