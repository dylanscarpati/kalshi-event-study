"""The exchanges the recorder knows how to record.

Each Feed holds the facts needed to connect to one exchange's public
order-book feed and stay connected. The recorder itself contains no
exchange-specific code: everything that differs between exchanges is here.
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
    # A text sent every keepalive_every_s seconds. Empty means none.
    keepalive_message: str = ""
    keepalive_every_s: float = 0.0
    # A web address returning the full order book, for feeds that send only
    # changes. Fetched rest_snapshot_after_s seconds after connecting, then
    # again every rest_snapshot_every_s seconds (0 means only once).
    rest_snapshot_url: str = ""
    rest_snapshot_after_s: float = 2.0
    rest_snapshot_every_s: float = 0.0


FEEDS: dict[str, Feed] = {}
