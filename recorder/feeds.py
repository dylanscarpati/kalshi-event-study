"""The exchanges the recorder knows how to record.

Each Feed holds the facts needed to connect to one exchange's public
order-book feed and stay connected. The recorder itself contains no
exchange-specific code: everything that differs between exchanges is here.

Every setting below was established on 2026-11-21 from the exchange's
documentation and from live connections of 4 to 25 minutes each. The notes
next to each feed say what was observed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Feed:
    """How to record one exchange. frozen=True makes it read-only, like a const struct."""

    name: str
    url: str
    # The exact texts sent, in order, once connected.
    subscribe: tuple[str, ...]
    # Seconds past each hour at which the recorder reconnects on purpose.
    reconnect_offset_s: int = 300
    # Seconds between the library's keep-alive pings. None switches them off.
    ping_interval_s: float | None = 20.0
    # Seconds of silence after which the connection is treated as dead.
    stale_after_s: float = 30.0
    # Seconds to wait after connecting before subscribing.
    subscribe_delay_s: float = 0.0
    # A function that looks at a received message and returns the reply the
    # exchange expects, or None when no reply is needed.
    reply: Callable[[str], str | None] | None = None
    # A function that returns True when a message asks the client to reconnect.
    reconnect_when: Callable[[str], bool] | None = None
    # A text sent every keepalive_every_s seconds. Empty means none.
    keepalive_message: str = ""
    keepalive_every_s: float = 0.0
    # A web address returning the full order book, for feeds that send only
    # changes. Fetched rest_snapshot_after_s seconds after connecting, then
    # again every rest_snapshot_every_s seconds (0 means only once).
    rest_snapshot_url: str = ""
    rest_snapshot_after_s: float = 2.0
    rest_snapshot_every_s: float = 0.0


def _cryptocom_heartbeat_reply(raw: str) -> str | None:
    """Crypto.com closes the connection unless each heartbeat is answered.

    The heartbeat is {"id":<number>,"method":"public/heartbeat","code":0}, sent
    every 30 s; the answer must repeat the same id. Only short messages are
    parsed, so the book messages, which never contain this text, cost nothing.
    """
    if len(raw) > 200 or "public/heartbeat" not in raw:
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if message.get("method") != "public/heartbeat" or "id" not in message:
        return None
    return json.dumps(
        {"id": message["id"], "method": "public/respond-heartbeat"},
        separators=(",", ":"),
    )


def _bitstamp_asks_to_reconnect(raw: str) -> bool:
    """Bitstamp sends this event before maintenance; clients should reconnect."""
    return len(raw) < 300 and "bts:request_reconnect" in raw


FEEDS: dict[str, Feed] = {
    # Advanced Trade feed. Full book on subscribe (about 4 MB, 35,000 levels);
    # every message carries sequence_num, counting from 0 on each connection, so
    # a missed message can be detected. The heartbeats channel sends one
    # message a second and keeps a quiet connection open; the documentation
    # says idle channels are closed after 60 to 90 s. Pings are unnecessary:
    # the 1-second heartbeat detects a dead connection sooner.
    "coinbase": Feed(
        name="coinbase",
        url="wss://advanced-trade-ws.coinbase.com",
        subscribe=(
            '{"type":"subscribe","product_ids":["BTC-USD"],"channel":"level2"}',
            '{"type":"subscribe","channel":"heartbeats"}',
        ),
        reconnect_offset_s=300,
        ping_interval_s=None,
        stale_after_s=10,
    ),
    # Version 2 book channel at the deepest public depth, 1000 levels a side.
    # Full book on subscribe; each message carries a CRC32 checksum of the top
    # ten levels. A heartbeat arrives every second once subscribed.
    "kraken": Feed(
        name="kraken",
        url="wss://ws.kraken.com/v2",
        subscribe=(
            '{"method":"subscribe","params":{"channel":"book","symbol":["BTC/USD"],"depth":1000}}',
        ),
        reconnect_offset_s=320,
        ping_interval_s=20,
        stale_after_s=5,
    ),
    # Changes only, with no sequence number and no snapshot on the feed. The
    # full book is downloaded a few seconds after subscribing (so that changes
    # are already being recorded when it is taken) and once a minute after
    # that, giving check points for the rebuilt book. The server pings every
    # 54 s and drops a client that does not answer; the library answers.
    "bitstamp": Feed(
        name="bitstamp",
        url="wss://ws.bitstamp.net",
        subscribe=(
            '{"event":"bts:subscribe","data":{"channel":"diff_order_book_btcusd"}}',
        ),
        reconnect_offset_s=340,
        ping_interval_s=20,
        stale_after_s=15,
        reconnect_when=_bitstamp_asks_to_reconnect,
        rest_snapshot_url="https://www.bitstamp.net/api/v2/order_book/btcusd/",
        rest_snapshot_after_s=3,
        rest_snapshot_every_s=60,
    ),
    # The current Gemini API (versions 1 and 2 were archived in February 2026).
    # snapshot=-1 asks for the full book on subscribe. Book messages carry
    # update numbers U and u for detecting a missed message. depth5 adds a
    # top-five snapshot every second, which both keeps the line busy and
    # gives a once-a-second check of the rebuilt book.
    "gemini": Feed(
        name="gemini",
        url="wss://ws.gemini.com/?snapshot=-1",
        subscribe=(
            '{"id":"1","method":"subscribe","params":["btcusd@depth@100ms","btcusd@depth5"]}',
        ),
        reconnect_offset_s=360,
        ping_interval_s=20,
        stale_after_s=10,
    ),
    # 50 levels a side is the deepest book offered. Full book on subscribe,
    # then changes carrying u and pu (the previous u) for detecting a missed
    # message. The documentation asks for a one-second wait before the first
    # request. "USD" on this exchange is a combined US dollar and USDC balance.
    "cryptocom": Feed(
        name="cryptocom",
        url="wss://stream.crypto.com/exchange/v1/market",
        subscribe=(
            '{"id":1,"method":"subscribe","params":{"channels":["book.BTC_USD.50"],'
            '"book_subscription_type":"SNAPSHOT_AND_UPDATE","book_update_frequency":100}}',
        ),
        reconnect_offset_s=380,
        ping_interval_s=None,
        stale_after_s=15,
        subscribe_delay_s=1.0,
        reply=_cryptocom_heartbeat_reply,
    ),
    # Every message is a full book of up to 200 levels a side, about ten a
    # second, numbered by sequenceNumberRange so a missed one is detectable.
    # The far end cuts a connection that carries nothing for 100 s, so the
    # library's pings stay on as a safeguard for quiet periods.
    "bullish": Feed(
        name="bullish",
        url="wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook",
        subscribe=(
            '{"jsonrpc":"2.0","type":"command","method":"subscribe",'
            '"params":{"topic":"l2Orderbook","symbol":"BTCUSD"},"id":"1"}',
        ),
        reconnect_offset_s=400,
        ping_interval_s=20,
        stale_after_s=10,
    ),
    # Public data feed: a full book once a second (all levels it publishes,
    # 19 to 37 a side when checked). No sequence number; each message's
    # whole-second timestamp rises by exactly one second, so a missed one
    # shows as a skipped second. The server pings every 3 s and expects an
    # answer within 1 s, which the library gives. The client must NOT ping
    # more than once every 30 s or the server closes the connection.
    "lmax": Feed(
        name="lmax",
        url="wss://public-data-api.london-digital.lmax.com/v1/web-socket",
        subscribe=(
            '{"type":"SUBSCRIBE","channels":[{"name":"ORDER_BOOK","instruments":["btc-usd"]}]}',
        ),
        reconnect_offset_s=420,
        ping_interval_s=None,
        stale_after_s=5,
    ),
}
