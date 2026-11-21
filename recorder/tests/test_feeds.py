"""Tests for the settings of each exchange in feeds.py. Nothing here uses the network."""

import json
import re

import pytest

from feeds import FEEDS


def test_every_exchange_that_was_verified_is_present():
    assert {"coinbase", "kraken", "bitstamp", "gemini", "cryptocom"} <= set(FEEDS)


@pytest.mark.parametrize("name", sorted(FEEDS))
def test_settings_are_sound(name):
    feed = FEEDS[name]
    assert feed.name == name
    assert re.fullmatch(r"[a-z0-9]+", name)  # safe as a folder and file name
    assert feed.url.startswith("wss://")  # always encrypted
    assert feed.subscribe, "a feed must subscribe to something"
    for text in feed.subscribe:
        json.loads(text)  # each subscribe message is valid JSON
    assert 0 <= feed.reconnect_offset_s < 3600
    assert feed.stale_after_s >= 3


@pytest.mark.parametrize("name", sorted(FEEDS))
def test_planned_reconnects_stay_clear_of_the_measured_minutes(name):
    # Kalshi averages the 60 seconds before each quarter hour. A reconnect takes
    # a few seconds, so it must start well away from those minutes.
    seconds_into_quarter = FEEDS[name].reconnect_offset_s % 900
    assert 60 <= seconds_into_quarter <= 900 - 180


def test_cryptocom_answers_its_heartbeat_with_the_same_id():
    reply = FEEDS["cryptocom"].reply
    heartbeat = '{"id":1795228071457,"method":"public/heartbeat","code":0}'

    answer = json.loads(reply(heartbeat))

    assert answer == {"id": 1795228071457, "method": "public/respond-heartbeat"}


def test_cryptocom_does_not_answer_book_messages():
    reply = FEEDS["cryptocom"].reply
    update = (
        '{"id":-1,"method":"subscribe","code":0,"result":{"instrument_name":"BTC_USD",'
        '"channel":"book.update","data":[{"update":{"asks":[],"bids":[]}}]}}'
    )
    assert reply(update) is None
    assert reply("not JSON") is None


def test_cryptocom_heartbeat_must_be_noticed_before_the_connection_looks_dead():
    # Its heartbeat comes every 30 s; a silence limit above that would never fire.
    assert FEEDS["cryptocom"].stale_after_s < 30


def test_bitstamp_downloads_its_full_book_and_obeys_reconnect_requests():
    feed = FEEDS["bitstamp"]
    assert feed.rest_snapshot_url.startswith("https://")
    assert feed.rest_snapshot_every_s > 0
    assert feed.reconnect_when('{"event":"bts:request_reconnect","channel":"","data":""}')
    assert not feed.reconnect_when('{"data":{"bids":[]},"event":"data"}')


def test_gemini_asks_for_a_full_book_on_subscribe():
    assert "snapshot=-1" in FEEDS["gemini"].url


def test_feeds_that_send_their_own_book_do_not_download_one():
    for name in ("coinbase", "kraken", "gemini", "cryptocom"):
        assert FEEDS[name].rest_snapshot_url == ""
