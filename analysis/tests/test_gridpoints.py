"""Tests for the A3 price-source hierarchy and gridpoint candle selection."""

from gridpoints import gridpoint_row, mid_admissible, pick_candle


def candle(ts, bid=None, ask=None, trade=None):
    c = {"end_period_ts": ts, "yes_bid": {}, "yes_ask": {}, "price": {}}
    if bid is not None:
        c["yes_bid"] = {"close": bid}
    if ask is not None:
        c["yes_ask"] = {"close": ask}
    if trade is not None:
        c["price"] = {"close": trade}
    return c


def test_pick_candle_last_within_staleness():
    cs = [candle(100), candle(200), candle(290), candle(310)]
    assert pick_candle(cs, 300, 150)["end_period_ts"] == 290  # not the future 310
    assert pick_candle(cs, 300, 5) is None                    # 290 too stale for 5s
    assert pick_candle(cs, 300, 300)["end_period_ts"] == 290


def test_mid_admissibility_a3():
    assert mid_admissible(45, 47, 10)
    assert not mid_admissible(0, 47, 10)     # absent-bid sentinel
    assert not mid_admissible(45, 100, 10)   # absent-ask sentinel
    assert not mid_admissible(0, 0, 10)      # both-zero empty candle encoding
    assert not mid_admissible(47, 45, 10)    # crossed
    assert not mid_admissible(45, 45, 10)    # locked
    assert not mid_admissible(40, 52, 10)    # spread 12 > cap
    assert mid_admissible(40, 50, 10)        # spread == cap admissible
    assert not mid_admissible(None, 50, 10)


def test_hierarchy_mid_then_trade_then_skip():
    # MID: clean two-sided book at the gridpoint candle
    cs = [candle(90, bid="0.4500", ask="0.4700", trade="0.4600")]
    row = gridpoint_row(cs, 100, 60)
    assert row["source"] == "MID" and row["price_c"] == 46.0 and row["spread_c"] == 2

    # Wide spread at the gridpoint candle -> falls to TRADE, and the trade may
    # come from an EARLIER candle within staleness
    cs = [candle(50, trade="0.3000"), candle(90, bid="0.1000", ask="0.4000")]
    row = gridpoint_row(cs, 100, 60)
    assert row["source"] == "TRADE" and row["price_c"] == 30.0
    assert row["candle_ts"] == 50
    assert row["bid_close_c"] == 10 and row["ask_close_c"] == 40  # kept for sensitivity grid

    # Sentinel book, no trades anywhere -> SKIP
    cs = [candle(90, bid="0.0000", ask="1.0000")]
    assert gridpoint_row(cs, 100, 60) is None

    # Nothing within staleness -> SKIP even though older candles exist
    cs = [candle(10, bid="0.4500", ask="0.4600", trade="0.4500")]
    assert gridpoint_row(cs, 100, 60) is None


def test_trade_close_recorded_alongside_mid():
    cs = [candle(90, bid="0.4400", ask="0.4600", trade="0.4500")]
    row = gridpoint_row(cs, 100, 60)
    assert row["source"] == "MID"
    assert row["trade_close_c"] == 45  # TRADE-only artifact-check figure needs this
