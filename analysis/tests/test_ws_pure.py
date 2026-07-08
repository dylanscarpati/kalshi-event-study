"""Tests for the pure WebSocket helpers (no sockets).

The signature test proves the PSS parameters (SHA-256, MGF1-SHA256,
salt = digest length, base64) round-trip against the cryptography
library's own verifier -- it catches wrong-salt/wrong-MGF/seconds-vs-
milliseconds mistakes. What it cannot prove: that the message FORMAT
(timestamp + "GET" + path) is what Kalshi's server expects; only the
live handshake falsifies that.
"""

import json

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_ws import (
    Envelope,
    GapDetector,
    backoff_delay,
    build_ws_auth_headers,
    get_snapshot_cmd,
    parse_envelope,
    sign,
    subscribe_cmd,
    verify,
)


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def test_sign_verify_roundtrip(keypair):
    private, public = keypair
    msg = "1783500000000GET/trade-api/ws/v2"
    verify(public, msg, sign(private, msg))


def test_signature_binds_message(keypair):
    private, public = keypair
    good = sign(private, "1783500000000GET/trade-api/ws/v2")
    with pytest.raises(InvalidSignature):
        verify(public, "1783500000001GET/trade-api/ws/v2", good)


def test_auth_headers_shape(keypair):
    private, public = keypair
    headers = build_ws_auth_headers("my-key-id", private, 1783500000000)
    assert headers["KALSHI-ACCESS-KEY"] == "my-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1783500000000"
    verify(public, "1783500000000GET/trade-api/ws/v2", headers["KALSHI-ACCESS-SIGNATURE"])


def test_parse_envelope_data_message():
    raw = json.dumps(
        {"type": "orderbook_delta", "sid": 3, "seq": 42,
         "msg": {"market_ticker": "X", "price_dollars": "0.1100", "delta_fp": "5.00", "side": "yes", "ts_ms": 1783500000123}}
    )
    env = parse_envelope(raw)
    assert env == Envelope("orderbook_delta", None, 3, 42, env.msg)
    assert env.msg["ts_ms"] == 1783500000123


def test_parse_envelope_command_response_without_sid_seq():
    env = parse_envelope('{"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":7}}')
    assert env.type == "subscribed"
    assert env.id == 1
    assert env.sid is None and env.seq is None  # envelope-level, absent on responses
    assert env.msg["sid"] == 7


def test_gap_detector_detects_all_anomalies():
    d = GapDetector()
    assert d.observe("s1", 1) is None          # first seq = baseline
    assert d.observe("s1", 2) is None
    gap = d.observe("s1", 5)
    assert gap.kind == "gap" and gap.expected == 3 and gap.received == 5
    dup = d.observe("s1", 5)
    assert dup.kind == "duplicate"
    reg = d.observe("s1", 2)
    assert reg.kind == "regression"


def test_gap_detector_keys_are_isolated_and_resync():
    d = GapDetector()
    d.observe("a", 10)
    assert d.observe("b", 1) is None            # other key unaffected
    d.resync("a", 100)
    assert d.observe("a", 101) is None          # new baseline accepted
    d.forget_all()
    assert d.observe("a", 7) is None            # fresh connection, no false gap


def test_command_builders_exact_json():
    assert json.loads(subscribe_cmd(1, ["orderbook_delta"], ["T1"])) == {
        "id": 1, "cmd": "subscribe",
        "params": {"channels": ["orderbook_delta"], "market_tickers": ["T1"]},
    }
    assert json.loads(get_snapshot_cmd(2, [7], ["T1"])) == {
        "id": 2, "cmd": "update_subscription",
        "params": {"sids": [7], "market_tickers": ["T1"], "action": "get_snapshot"},
    }


def test_backoff_bounds_and_jitter():
    for attempt, nominal in [(0, 1), (1, 2), (2, 4), (10, 30)]:
        for _ in range(50):
            d = backoff_delay(attempt)
            assert 0.75 * nominal <= d <= 1.25 * nominal
