"""Pure helpers for Kalshi's WebSocket market-data API: auth signing,
message parsing, sequence-gap detection, command building, backoff.

No sockets here -- everything is unit-testable offline. The I/O lives in
ws_recorder.py. Contract verified against docs.kalshi.com/asyncapi.yaml
on 2026-07-08: data messages arrive in an envelope {type, sid, seq, msg}
with seq at the envelope level; command responses (subscribed/ok/error)
may omit sid/seq.
"""

from __future__ import annotations

import base64
import json
import random
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"


def load_private_key(path: str) -> rsa.RSAPrivateKey:
    """Load the RSA private key PEM. Loud failure naming the path: the key
    was shown exactly once at creation and cannot be re-downloaded, so a
    mangled save (CRLF/BOM/truncation) must be caught before release day."""
    try:
        with open(path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    except Exception as exc:
        raise RuntimeError(f"cannot load private key from {path}: {exc}") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise RuntimeError(f"{path} is not an RSA private key")
    return key


def _pss_padding() -> padding.PSS:
    # Salt length must equal the digest length (32 for SHA-256); the
    # library default (MAX_LENGTH) is a different value and fails auth.
    return padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size)


def sign(private_key: rsa.RSAPrivateKey, message: str) -> str:
    signature = private_key.sign(message.encode("utf-8"), _pss_padding(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def verify(public_key, message: str, signature_b64: str) -> None:
    """Raises InvalidSignature on mismatch. Used by tests to prove the
    signing parameters round-trip; only a live handshake can prove the
    message FORMAT (path string, method casing) is what the server expects."""
    public_key.verify(
        base64.b64decode(signature_b64), message.encode("utf-8"), _pss_padding(), hashes.SHA256()
    )


def build_ws_auth_headers(key_id: str, private_key: rsa.RSAPrivateKey, timestamp_ms: int) -> dict:
    """timestamp_ms is a parameter so callers must mint a fresh one per
    connection attempt -- reusing a stale signature on reconnect is the
    classic auth bug."""
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, f"{timestamp_ms}GET{WS_SIGN_PATH}"),
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
    }


@dataclass(frozen=True)
class Envelope:
    type: str
    id: int | None  # command id, echoed on subscribed/ok/error responses
    sid: int | None
    seq: int | None
    msg: dict


def parse_envelope(raw: str) -> Envelope:
    obj = json.loads(raw)
    return Envelope(
        type=obj["type"], id=obj.get("id"), sid=obj.get("sid"), seq=obj.get("seq"), msg=obj.get("msg") or {}
    )


@dataclass(frozen=True)
class SeqAnomaly:
    kind: str  # "gap" | "duplicate" | "regression"
    key: str
    expected: int
    received: int


class GapDetector:
    """Envelope-seq continuity per key (one key per subscription sid).

    resync() re-baselines after a snapshot: whether seq continues or
    restarts after get_snapshot is not documented, so the first
    post-snapshot seq is accepted as a new baseline and the transition
    is the caller's to log.
    """

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def observe(self, key: str, seq: int) -> SeqAnomaly | None:
        last = self._last.get(key)
        self._last[key] = seq
        if last is None:
            return None
        expected = last + 1
        if seq == expected:
            return None
        if seq == last:
            return SeqAnomaly("duplicate", key, expected, seq)
        if seq < last:
            return SeqAnomaly("regression", key, expected, seq)
        return SeqAnomaly("gap", key, expected, seq)

    def resync(self, key: str, seq: int) -> None:
        self._last[key] = seq

    def forget_all(self) -> None:
        """New connection = new chains; fresh subscriptions bring fresh
        snapshots, so old baselines must not produce false gaps."""
        self._last.clear()


def subscribe_cmd(cmd_id: int, channels: list[str], tickers: list[str]) -> str:
    return json.dumps(
        {"id": cmd_id, "cmd": "subscribe", "params": {"channels": channels, "market_tickers": tickers}},
        separators=(",", ":"),
    )


def get_snapshot_cmd(cmd_id: int, sids: list[int], tickers: list[str]) -> str:
    return json.dumps(
        {
            "id": cmd_id,
            "cmd": "update_subscription",
            "params": {"sids": sids, "market_tickers": tickers, "action": "get_snapshot"},
        },
        separators=(",", ":"),
    )


def backoff_delay(attempt: int, base_s: float = 1.0, cap_s: float = 30.0, jitter_frac: float = 0.25) -> float:
    """Exponential backoff with jitter: 1, 2, 4 ... capped, then +/- up to
    jitter_frac so mass reconnections cannot synchronize."""
    delay = min(base_s * (2 ** max(0, attempt)), cap_s)
    return delay * (1.0 + random.uniform(-jitter_frac, jitter_frac))
