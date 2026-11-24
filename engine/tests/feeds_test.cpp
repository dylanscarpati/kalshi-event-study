// One block of tests per exchange. Each message is written in that exchange's format (see
// notes/research/feed-formats.md), with made-up prices.
#include <gtest/gtest.h>
#include <zlib.h>

#include <algorithm>
#include <string>
#include <utility>
#include <vector>

#include "feed.hpp"
#include "feed_helpers.hpp"

namespace engine {
namespace {

using testing::best;
using testing::Fed;
using testing::fx;
using Levels = std::vector<Level>;

// ---------------------------------------------------------------------------------------------
// LMAX Digital
// ---------------------------------------------------------------------------------------------

std::string lmax_book(const std::string& second, const std::string& status = "OPEN") {
  return R"({"type":"ORDER_BOOK","instrument_id":"btc-usd","timestamp":"2026-11-21T21:51:)" +
         second + R"(.000Z","status":")" + status +
         R"(","bids":[{"price":"100.500000","quantity":"1.0000"},{"price":"100.100000","quantity":"2.0000"}],)"
         R"("asks":[{"price":"101.000000","quantity":"3.0000"}]})";
}

TEST(Lmax, EachMessageReplacesTheBook) {
  Fed lmax("lmax");
  lmax.message(10, R"({"type":"SUBSCRIPTIONS","channels":[]})");
  EXPECT_FALSE(lmax.integrity().ready_ns);
  lmax.message(20, lmax_book("01"));
  EXPECT_EQ(lmax.integrity().ready_ns, 20);
  EXPECT_EQ(best(lmax->book().bids), (Levels{{fx("100.5"), fx("1")}, {fx("100.1"), fx("2")}}));
  EXPECT_EQ(best(lmax->book().asks), (Levels{{fx("101"), fx("3")}}));
  EXPECT_EQ(lmax->last_exchange_ns(), 1'795'297'861'000'000'000);
  EXPECT_EQ(lmax->last_book_wall_ns(), 20);
  lmax.message(30, lmax_book("02"));
  EXPECT_TRUE(lmax.integrity().break_ns.empty());
}

TEST(Lmax, ASkippedOrRepeatedSecondIsABreakAtThatMomentOnly) {
  Fed lmax("lmax");
  lmax.message(10, lmax_book("01"));
  lmax.message(20, lmax_book("03"));  // second 02 is missing
  lmax.message(30, lmax_book("03"));  // the same second again
  EXPECT_EQ(lmax.integrity().break_ns, (std::vector<std::int64_t>{20, 30}));
  EXPECT_FALSE(lmax.integrity().invalid_from_ns);  // a full-book feed repairs itself
  ASSERT_EQ(lmax.integrity().problems.size(), 2U);
  EXPECT_EQ(lmax.integrity().problems[0].second,
            "1 second(s) skipped before 2026-11-21T21:51:03.000Z");
  EXPECT_EQ(lmax.integrity().problems[1].second,
            "book for 2026-11-21T21:51:03.000Z repeated or out of order");
}

TEST(Lmax, AClosedMarketIsCountedAndShown) {
  Fed lmax("lmax");
  lmax.message(10, lmax_book("01", "CLOSED"));
  EXPECT_FALSE(lmax->market_open());
  EXPECT_EQ(lmax.count("books_not_open"), 1);
  lmax.message(20, lmax_book("02"));
  EXPECT_TRUE(lmax->market_open());
}

// ---------------------------------------------------------------------------------------------
// Bullish
// ---------------------------------------------------------------------------------------------

std::string bullish_book(int lower, int upper,
                         const std::string& bids = R"("100.5000","1","100.1000","2")") {
  return R"({"type":"snapshot","dataType":"V1TALevel2","data":{"symbol":"BTCUSD","bids":[)" + bids +
         R"(],"asks":["101.0000","3"],"sequenceNumberRange":[)" + std::to_string(lower) + "," +
         std::to_string(upper) +
         R"(],"datetime":"2026-11-21T21:50:43.165Z","timestamp":"1795297843165"}})";
}

TEST(Bullish, EachMessageReplacesTheBook) {
  Fed bullish("bullish");
  bullish.message(5, R"({"jsonrpc": "2.0","id": "1","result": {"responseCode": "200"}})");
  bullish.message(10, bullish_book(10, 12));
  bullish.message(20, bullish_book(13, 13));
  EXPECT_EQ(bullish.integrity().ready_ns, 10);
  EXPECT_TRUE(bullish.integrity().problems.empty());
  EXPECT_EQ(best(bullish->book().bids), (Levels{{fx("100.5"), fx("1")}, {fx("100.1"), fx("2")}}));
  EXPECT_EQ(bullish->last_exchange_ns(), 1'795'297'843'165'000'000);
}

TEST(Bullish, AGapInTheSequenceRangesIsABreak) {
  Fed bullish("bullish");
  bullish.message(10, bullish_book(10, 12));
  bullish.message(20, bullish_book(15, 15));
  EXPECT_EQ(bullish.integrity().break_ns, (std::vector<std::int64_t>{20}));
  EXPECT_EQ(bullish.integrity().problems[0].second, "sequence numbers 13 to 14 missing");
}

TEST(Bullish, APriceWithoutASizeIsABreak) {
  Fed bullish("bullish");
  bullish.message(10, bullish_book(10, 10, R"("100.5000","1","100.1000")"));
  EXPECT_EQ(bullish.integrity().break_ns, (std::vector<std::int64_t>{10}));
}

// ---------------------------------------------------------------------------------------------
// Coinbase
// ---------------------------------------------------------------------------------------------

std::string coinbase(int sequence, const std::string& type, const std::string& updates) {
  return R"({"channel":"l2_data","timestamp":"2026-11-21T08:05:00.601135158Z","sequence_num":)" +
         std::to_string(sequence) + R"(,"events":[{"type":")" + type +
         R"(","product_id":"BTC-USD","updates":[)" + updates + "]}]}";
}

std::string coinbase_level(const std::string& side, const std::string& price,
                           const std::string& qty) {
  return R"({"side":")" + side + R"(","event_time":"2026-11-21T08:05:00.31426Z","price_level":")" +
         price + R"(","new_quantity":")" + qty + R"("})";
}

TEST(Coinbase, LoadsTheFullBookThenAppliesChanges) {
  Fed coinbase_feed("coinbase");
  coinbase_feed.message(10, coinbase(0, "snapshot",
                                     coinbase_level("bid", "99.00", "2") + "," +
                                         coinbase_level("bid", "100.00", "1") + "," +
                                         coinbase_level("offer", "101.00", "3")));
  EXPECT_EQ(coinbase_feed.integrity().ready_ns, 10);
  coinbase_feed.message(20, coinbase(1, "update",
                                     coinbase_level("bid", "99.00", "0") + "," +
                                         coinbase_level("offer", "100.50", "4")));
  coinbase_feed.message(30, R"({"channel":"subscriptions","timestamp":"2026-11-21T08:05:00.6Z",)"
                            R"("sequence_num":2,"events":[]})");
  EXPECT_EQ(best(coinbase_feed->book().bids), (Levels{{fx("100"), fx("1")}}));
  EXPECT_EQ(best(coinbase_feed->book().asks),
            (Levels{{fx("100.5"), fx("4")}, {fx("101"), fx("3")}}));
  EXPECT_TRUE(coinbase_feed.integrity().problems.empty());
  EXPECT_EQ(coinbase_feed->last_exchange_ns(), 1'795'248'300'601'135'158);
}

TEST(Coinbase, ASequenceJumpSpoilsTheRestOfTheFile) {
  Fed coinbase_feed("coinbase");
  coinbase_feed.message(10, coinbase(0, "snapshot", coinbase_level("bid", "100.00", "1")));
  coinbase_feed.message(20, R"({"type":"error","message":"no sequence number"})");  // ignored
  coinbase_feed.message(30, coinbase(2, "update", coinbase_level("bid", "100.00", "5")));
  EXPECT_EQ(coinbase_feed.integrity().invalid_from_ns, 30);
  EXPECT_EQ(coinbase_feed.integrity().problems[0].second, "sequence number 1 expected, 2 received");
}

// ---------------------------------------------------------------------------------------------
// Crypto.com
// ---------------------------------------------------------------------------------------------

std::string cryptocom_snapshot(int u, const std::string& bids = R"(["100.00","2.00000","1"])") {
  return R"({"id":-1,"method":"subscribe","code":0,"result":{"instrument_name":"BTC_USD",)"
         R"("subscription":"book.BTC_USD.50","channel":"book","depth":50,"data":[{"asks":)"
         R"([["101.00","1.00000","1"]],"bids":[)" +
         bids + R"(],"t":1795248384976,"tt":1795248384973,"u":)" + std::to_string(u) +
         R"(,"cs":-1}]}})";
}

std::string cryptocom_update(int u, int pu, const std::string& bids, const std::string& asks) {
  return R"({"id":-1,"method":"subscribe","code":0,"result":{"instrument_name":"BTC_USD",)"
         R"("subscription":"book.BTC_USD.50","channel":"book.update","depth":50,"data":[{)"
         R"("update":{"asks":[)" +
         asks + R"(],"bids":[)" + bids + R"(]},"t":1795248385181,"tt":1795248385174,"u":)" +
         std::to_string(u) + R"(,"pu":)" + std::to_string(pu) + R"(,"cs":1}]}})";
}

TEST(Cryptocom, LoadsTheBookThenFollowsTheChainOfUpdates) {
  Fed crypto("cryptocom");
  crypto.message(10, R"({"id":1,"method":"subscribe","code":0,"channel":"book.BTC_USD.50"})");
  crypto.message(20, cryptocom_snapshot(10));
  crypto.message(30,
                 cryptocom_update(20, 10, "", R"(["101.00","0","0"],["102.00","3.00000","1"])"));
  EXPECT_EQ(crypto.integrity().ready_ns, 20);
  EXPECT_FALSE(crypto.integrity().invalid_from_ns);
  EXPECT_EQ(best(crypto->book().asks), (Levels{{fx("102"), fx("3")}}));
  EXPECT_EQ(crypto->last_exchange_ns(), 1'795'248'385'181'000'000);
  crypto.message(40, cryptocom_update(30, 25, "", ""));  // names an update that never came
  EXPECT_EQ(crypto.integrity().invalid_from_ns, 40);
}

TEST(Cryptocom, EveryHeartbeatMustBeAnswered) {
  Fed crypto("cryptocom");
  crypto.message(10, R"({"id":1795248391819,"method":"public/heartbeat","code":0})");
  crypto.event(11, "sent", R"({"id":1795248391819,"method":"public/respond-heartbeat"})");
  crypto.message(20, R"({"id":1795248391820,"method":"public/heartbeat","code":0})");
  crypto->finish(/*cut_off=*/false);
  EXPECT_EQ(crypto.count("heartbeats_received"), 2);
  EXPECT_EQ(crypto.count("heartbeats_answered"), 1);
  ASSERT_EQ(crypto.integrity().problems.size(), 1U);
  EXPECT_EQ(crypto.integrity().problems[0].second, "1 heartbeat(s) not answered");
}

TEST(Cryptocom, NeverShowsMoreThanFiftyLevels) {
  std::string bids;
  for (int i = 0; i < 50; ++i) {
    bids += (i ? "," : "") + std::string(R"([")") + std::to_string(50 + i) + R"(.00","1","1"])";
  }
  Fed crypto("cryptocom");
  crypto.message(10, cryptocom_snapshot(10, bids));
  crypto.message(20, cryptocom_update(20, 10, R"(["100.50","1","1"])", ""));
  EXPECT_EQ(crypto.count("books_beyond_50_levels"), 1);
  EXPECT_EQ(crypto->book().bids.size(), 50U);
  EXPECT_EQ(crypto->book().bids.level(0).price, fx("100.5"));
  EXPECT_EQ(crypto->book().bids.level(49).price, fx("51"));  // the lowest bid, 50.00, was cut
}

// ---------------------------------------------------------------------------------------------
// Gemini
// ---------------------------------------------------------------------------------------------

std::string gemini_update(int first, int last, const std::string& bids, const std::string& asks) {
  return R"({"e":"depthUpdate","E":1795248360498205395,"s":"btcusd","U":)" + std::to_string(first) +
         R"(,"u":)" + std::to_string(last) + R"(,"b":[)" + bids + R"(],"a":[)" + asks + "]}";
}

std::string gemini_top(int id, const std::string& bids, const std::string& asks) {
  return R"({"lastUpdateId":)" + std::to_string(id) + R"(,"symbol":"btcusd","bids":[)" + bids +
         R"(],"asks":[)" + asks + "]}";
}

TEST(Gemini, FollowsOverlappingUpdatesAndSkipsStaleOnes) {
  Fed gemini("gemini");
  gemini.message(
      10, gemini_update(100, 100, R"(["100.00000","1.0000000000"],["99.00000","2.0000000000"])",
                        R"(["101.00000","3.0000000000"])"));
  gemini.message(20, R"({"id":"1","status":200})");
  gemini.message(
      30,
      gemini_update(100, 105, R"(["99.00000","0.0000000000"],["99.50000","4.0000000000"])", ""));
  gemini.message(40, gemini_update(90, 104, R"(["1.00000","9.0000000000"])", ""));  // stale
  EXPECT_EQ(gemini.integrity().ready_ns, 10);
  EXPECT_EQ(gemini.count("stale_updates_skipped"), 1);
  EXPECT_EQ(best(gemini->book().bids), (Levels{{fx("100"), fx("1")}, {fx("99.5"), fx("4")}}));
  EXPECT_EQ(gemini->last_exchange_ns(), 1'795'248'360'498'205'395);

  gemini.message(50, gemini_top(105, R"(["100.00000","1.0000000000"],["99.50000","4.0000000000"])",
                                R"(["101.00000","3.0000000000"])"));
  gemini.message(60, gemini_top(999, "", ""));  // not about the last update: ignored
  EXPECT_EQ(gemini.count("top_five_compared"), 1);
  EXPECT_EQ(gemini.count("top_five_matched"), 1);
  EXPECT_TRUE(gemini.integrity().problems.empty());

  gemini.message(70, gemini_top(105, R"(["100.00000","1.0000000000"])", ""));
  EXPECT_EQ(gemini.integrity().invalid_from_ns, 70);
}

TEST(Gemini, AGapInTheUpdateNumbersSpoilsTheRestOfTheFile) {
  Fed gemini("gemini");
  gemini.message(10, gemini_update(100, 100, R"(["100.00000","1"])", ""));
  gemini.message(20, gemini_update(103, 110, "", ""));
  EXPECT_EQ(gemini.integrity().invalid_from_ns, 20);
  EXPECT_EQ(gemini.integrity().problems[0].second, "update numbers 101 to 102 missing");
}

TEST(Gemini, TheFirstUpdateMustBeAFullBook) {
  Fed gemini("gemini");
  gemini.message(10, gemini_update(90, 100, R"(["100.00000","1"])", ""));
  EXPECT_EQ(gemini.integrity().invalid_from_ns, 10);
}

// ---------------------------------------------------------------------------------------------
// Kraken
// ---------------------------------------------------------------------------------------------

using TextLevels = std::vector<std::pair<std::string, std::string>>;  // best first

// Kraken's checksum as recorder/check.py computes it: from each level's text, with the point
// removed and leading zeros stripped. Deliberately not the engine's way (from the values).
std::uint64_t kraken_checksum(const TextLevels& asks, const TextLevels& bids) {
  const auto digits = [](std::string text) {
    text.erase(std::remove(text.begin(), text.end(), '.'), text.end());
    return text.substr(text.find_first_not_of('0'));
  };
  std::string text;
  for (std::size_t i = 0; i < std::min<std::size_t>(10, asks.size()); ++i) {
    text += digits(asks[i].first) + digits(asks[i].second);
  }
  for (std::size_t i = 0; i < std::min<std::size_t>(10, bids.size()); ++i) {
    text += digits(bids[i].first) + digits(bids[i].second);
  }
  return crc32(0L, reinterpret_cast<const Bytef*>(text.data()), static_cast<uInt>(text.size()));
}

std::string kraken_levels(const TextLevels& levels) {
  std::string out;
  for (const auto& [price, qty] : levels) {
    out +=
        (out.empty() ? "" : ",") + std::string(R"({"price":)") + price + R"(,"qty":)" + qty + "}";
  }
  return out;
}

std::string kraken(const std::string& type, const TextLevels& bids, const TextLevels& asks,
                   std::uint64_t checksum) {
  return R"({"channel":"book","type":")" + type + R"(","data":[{"symbol":"BTC/USD","bids":[)" +
         kraken_levels(bids) + R"(],"asks":[)" + kraken_levels(asks) + R"(],"checksum":)" +
         std::to_string(checksum) + R"(,"timestamp":"2026-11-21T08:05:20.816690Z"}]})";
}

TEST(Kraken, ChecksMatchingChecksumsAndIgnoresOtherChannels) {
  Fed kraken_feed("kraken");
  kraken_feed.message(5, R"({"channel":"status","type":"update","data":[{"system":"online",)"
                         R"("connection_id":17834348137351763235}]})");  // too large for int64
  const TextLevels bids = {{"99.1", "0.50000000"}};
  const TextLevels asks = {{"100.5", "1.25000000"}};
  kraken_feed.message(10, kraken("snapshot", bids, asks, kraken_checksum(asks, bids)));
  EXPECT_EQ(kraken_feed.integrity().ready_ns, 10);
  EXPECT_EQ(kraken_feed.count("checksums_matched"), 1);
  EXPECT_TRUE(kraken_feed.integrity().problems.empty());
  EXPECT_EQ(kraken_feed->last_exchange_ns(), 1'795'248'320'816'690'000);
}

TEST(Kraken, AWrongChecksumSpoilsTheRestOfTheFile) {
  Fed kraken_feed("kraken");
  const TextLevels bids = {{"99.1", "0.50000000"}};
  const TextLevels asks = {{"100.5", "1.25000000"}};
  kraken_feed.message(10, kraken("snapshot", bids, asks, kraken_checksum(asks, bids)));
  kraken_feed.message(20, kraken("update", {{"99.2", "1.00000000"}}, {}, 12345));
  kraken_feed.message(30, kraken("update", {}, {}, 0));  // no longer checked
  EXPECT_EQ(kraken_feed.integrity().invalid_from_ns, 20);
  EXPECT_EQ(kraken_feed.count("checksums_matched"), 1);
}

TEST(Kraken, KeepsTheBestThousandLevelsASide) {
  TextLevels bids;  // 1000 bids from 1000.0 down to 900.1
  for (int tenths = 10000; tenths > 9000; --tenths) {
    bids.emplace_back(std::to_string(tenths / 10) + "." + std::to_string(tenths % 10),
                      "1.00000000");
  }
  const TextLevels asks = {{"2000.0", "1.00000000"}};
  Fed kraken_feed("kraken");
  kraken_feed.message(10, kraken("snapshot", bids, asks, kraken_checksum(asks, bids)));
  // A better bid enters, so the worst one (900.1) falls out of the thousand.
  TextLevels after = bids;
  after.insert(after.begin(), {"1000.1", "2.00000000"});
  after.pop_back();
  kraken_feed.message(
      20, kraken("update", {{"1000.1", "2.00000000"}}, {}, kraken_checksum(asks, after)));
  EXPECT_EQ(kraken_feed.count("checksums_matched"), 2);
  EXPECT_EQ(kraken_feed->book().bids.size(), 1000U);
  EXPECT_EQ(kraken_feed->book().bids.level(999).price, fx("900.2"));
}

// ---------------------------------------------------------------------------------------------
// Bitstamp
// ---------------------------------------------------------------------------------------------

std::string bitstamp_change(std::int64_t micro, const std::string& bids, const std::string& asks) {
  return R"({"data":{"timestamp":"1795248341","microtimestamp":")" + std::to_string(micro) +
         R"(","bids":[)" + bids + R"(],"asks":[)" + asks +
         R"(]},"channel":"diff_order_book_btcusd","event":"data"})";
}

std::string bitstamp_download(std::int64_t micro, const std::string& bids,
                              const std::string& asks) {
  return R"({"timestamp":"1795248341","microtimestamp":")" + std::to_string(micro) +
         R"(","bids":[)" + bids + R"(],"asks":[)" + asks + "]}";
}

TEST(Bitstamp, SplicesADownloadWithTheChangesSinceItsTime) {
  Fed bitstamp("bitstamp");
  bitstamp.message(10, bitstamp_change(1000, R"(["100.00","1"])", ""));
  bitstamp.message(20, bitstamp_change(2000, "", R"(["101.00","5"])"));
  EXPECT_FALSE(bitstamp.integrity().ready_ns);  // no book yet
  bitstamp.event(30, "rest_snapshot",
                 bitstamp_download(1500, R"(["100.00","1"],["99.00","2"])",
                                   R"(["101.00","3"],["102.00","4"])"));
  EXPECT_EQ(bitstamp.integrity().ready_ns, 30);
  // The change at 2000 is newer than the download (1500), so it is applied on top of it.
  EXPECT_EQ(best(bitstamp->book().asks), (Levels{{fx("101"), fx("5")}, {fx("102"), fx("4")}}));
  EXPECT_EQ(bitstamp.count("book_downloads"), 1);
  EXPECT_EQ(bitstamp->last_exchange_ns(), 2'000'000);
}

TEST(Bitstamp, ADownloadOlderThanTheFirstChangeCannotBeUsed) {
  Fed bitstamp("bitstamp");
  bitstamp.message(10, bitstamp_change(2000, R"(["100.00","1"])", ""));
  bitstamp.event(20, "rest_snapshot", bitstamp_download(1500, R"(["100.00","1"])", ""));
  EXPECT_FALSE(bitstamp.integrity().ready_ns);
  EXPECT_EQ(bitstamp.count("book_downloads_unusable"), 1);
}

TEST(Bitstamp, RemovesAPhantomThatAChangeCrosses) {
  Fed bitstamp("bitstamp");
  bitstamp.message(10, bitstamp_change(1000, R"(["90.00","1"])", ""));
  bitstamp.event(20, "rest_snapshot",
                 bitstamp_download(1500, R"(["100.00","1"])", R"(["100.10","1"],["100.50","1"])"));
  // A bid at 100.20 would cross the ask at 100.10, which this change did not set: a phantom.
  bitstamp.message(30, bitstamp_change(3000, R"(["100.20","1"])", ""));
  EXPECT_EQ(bitstamp.count("crossing_levels_removed"), 1);
  EXPECT_FALSE(bitstamp->book().crossed());
  EXPECT_EQ(bitstamp->book().asks.level(0).price, fx("100.5"));
}

TEST(Bitstamp, LeavesACrossingWhenTheChangeSetBothSides) {
  Fed bitstamp("bitstamp");
  bitstamp.message(10, bitstamp_change(1000, R"(["90.00","1"])", ""));
  bitstamp.event(20, "rest_snapshot",
                 bitstamp_download(1500, R"(["100.00","1"])", R"(["100.50","1"])"));
  bitstamp.message(30, bitstamp_change(3000, R"(["100.50","1"])", R"(["100.50","2"])"));
  EXPECT_EQ(bitstamp.count("crossings_not_repaired"), 1);
  EXPECT_TRUE(bitstamp->book().crossed());
}

TEST(Bitstamp, ALaterDownloadRemovesOnlyUnconfirmedPhantoms) {
  Fed bitstamp("bitstamp");
  bitstamp.message(10, bitstamp_change(1000, R"(["90.00","1"])", ""));
  bitstamp.event(20, "rest_snapshot",
                 bitstamp_download(1500, R"(["100.00","1"],["99.00","2"],["98.00","3"])",
                                   R"(["101.00","1"])"));
  bitstamp.message(30, bitstamp_change(2000, R"(["97.00","1"])", ""));  // confirmed by a change
  bitstamp.message(40, bitstamp_change(5000, R"(["96.00","1"])", ""));  // newer than download 2
  bitstamp.event(50, "rest_snapshot",
                 bitstamp_download(4000, R"(["100.00","1"],["98.00","5"],["95.00","1"])",
                                   R"(["101.00","1"])"));
  // 99.00 came from the first download, no change ever set it, and the second download does
  // not have it: a phantom. 97.00 is missing from the second download too, but a change set it,
  // so it stays. 95.00 is only in the download: counted, not added. 98.00 differs in size:
  // counted, not changed.
  EXPECT_EQ(
      best(bitstamp->book().bids),
      (Levels{
          {fx("100"), fx("1")}, {fx("98"), fx("3")}, {fx("97"), fx("1")}, {fx("96"), fx("1")}}));
  EXPECT_EQ(bitstamp.count("resyncs"), 1);
  EXPECT_EQ(bitstamp.count("phantom_levels_removed"), 1);
  EXPECT_EQ(bitstamp.count("download_only_levels"), 1);
  EXPECT_EQ(bitstamp.count("size_differences"), 1);
}

TEST(Bitstamp, ChangesOutOfTimeOrderSpoilTheRestOfTheFile) {
  Fed bitstamp("bitstamp");
  bitstamp.message(10, bitstamp_change(2000, "", ""));
  bitstamp.message(20, bitstamp_change(2000, "", ""));
  EXPECT_EQ(bitstamp.integrity().invalid_from_ns, 20);
  EXPECT_EQ(bitstamp.count("changes"), 2);
}

TEST(Bitstamp, CountsFailedAndUnreadableDownloads) {
  Fed bitstamp("bitstamp");
  bitstamp.event(10, "rest_snapshot_failed");
  bitstamp.event(20, "rest_snapshot", "<html>not a book</html>");
  EXPECT_EQ(bitstamp.count("book_download_failures"), 1);
  EXPECT_EQ(bitstamp.count("book_downloads_unreadable"), 1);
  EXPECT_TRUE(bitstamp.integrity().problems.empty());
}

}  // namespace
}  // namespace engine
