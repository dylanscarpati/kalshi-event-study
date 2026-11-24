// What every feed shares: the time parser, the value helpers and Feed::handle().
#include "feed.hpp"

#include <gtest/gtest.h>

#include "feed_helpers.hpp"
#include "utc_time.hpp"

namespace engine {
namespace {

using testing::Fed;

// Expected values computed with Python's datetime (see the commit that added this test).
TEST(ParseUtcNs, ReadsTheExchangesTimestamps) {
  EXPECT_EQ(parse_utc_ns("2026-11-21T08:05:00Z"), 1'795'248'300'000'000'000);
  EXPECT_EQ(parse_utc_ns("2026-11-21T08:05:00.601135158Z"), 1'795'248'300'601'135'158);  // Coinbase
  EXPECT_EQ(parse_utc_ns("2026-11-21T08:05:00.31426Z"), 1'795'248'300'314'260'000);
  EXPECT_EQ(parse_utc_ns("2026-11-21T08:05:00.000Z"), 1'795'248'300'000'000'000);  // LMAX
  EXPECT_EQ(parse_utc_ns("1970-01-01T00:00:00Z"), 0);
  EXPECT_EQ(parse_utc_ns("2024-02-29T23:59:59Z"), 1'709'251'199'000'000'000);
  EXPECT_EQ(parse_utc_ns("2000-03-01T00:00:00Z"), 951'868'800'000'000'000);
}

TEST(ParseUtcNs, RejectsAnythingElse) {
  for (const char* bad : {"", "2026-11-21T08:05:00", "2026-11-21 08:05:00Z", "2026-13-01T00:00:00Z",
                          "2026-02-30T00:00:00Z", "2026-11-21T24:00:00Z", "2026-11-21T08:05:60Z",
                          "2026-11-21T08:05:00.Z", "2026-11-21T08:05:00.1234567891Z",
                          "2026-11-21T08:05:00+00:00", "2026-11-21T08:05:0aZ"}) {
    EXPECT_EQ(parse_utc_ns(bad), std::nullopt) << bad;
  }
}

constexpr std::string_view kLmaxBook =
    R"({"type":"ORDER_BOOK","instrument_id":"btc-usd","timestamp":"2026-11-21T21:51:01.000Z",)"
    R"("status":"OPEN","bids":[{"price":"100.5","quantity":"1"}],"asks":[{"price":"101","quantity":"2"}]})";

TEST(Feed, RecordsWhenTheFileStartsEndsAndHowItEnded) {
  Fed lmax("lmax");
  lmax.event(100, "connected");
  lmax.message(200, kLmaxBook);
  lmax.event(300, "planned_reconnect");
  lmax.message(400, R"({"type":"HEARTBEAT"})");  // taken from the queue after the close
  EXPECT_EQ(lmax.integrity().start_ns, 100);
  EXPECT_EQ(lmax.integrity().end_ns, 400);
  EXPECT_EQ(lmax.integrity().ended, "planned_reconnect");
  EXPECT_EQ(lmax.integrity().closed_ns, 300);
  EXPECT_FALSE(lmax.integrity().unplanned());
  EXPECT_EQ(lmax.count("messages"), 2);
}

TEST(Feed, AStaleOrBrokenConnectionIsUnplanned) {
  for (const char* ending : {"stale", "disconnected", "error"}) {
    Fed lmax("lmax");
    lmax.event(1, ending);
    EXPECT_TRUE(lmax.integrity().unplanned()) << ending;
  }
}

TEST(Feed, AMessageThatIsNotTextOrNotReadableIsABreak) {
  Fed lmax("lmax");
  lmax.binary(10);
  lmax.message(20, R"({"type":"ORDER_BOOK", this is not JSON)");
  ASSERT_EQ(lmax.integrity().problems.size(), 2U);
  EXPECT_EQ(lmax.integrity().problems[0].second, "a message is not valid text");
  EXPECT_NE(lmax.integrity().problems[1].second.find("could not be read"), std::string::npos);
  EXPECT_EQ(lmax.integrity().break_ns, (std::vector<std::int64_t>{10, 20}));  // full-book feed
}

TEST(Feed, CountsAJumpOfTheWallClockAsAClockStep) {
  Fed lmax("lmax");
  lmax.message(1'000'000'000, R"({"type":"X"})");
  lmax.message(1'100'000'000, R"({"type":"X"})", 10'000'000);   // 10 ms of drift: normal
  lmax.message(1'200'000'000, R"({"type":"X"})", 900'000'000);  // the wall clock jumped
  EXPECT_EQ(lmax.count("clock_steps"), 1);
  EXPECT_EQ(lmax.integrity().clock_step_ns, (std::vector<std::int64_t>{1'200'000'000}));
}

TEST(Feed, AFileCutOffIsAProblem) {
  Fed lmax("lmax");
  lmax.message(5, kLmaxBook);
  lmax->finish(/*cut_off=*/true);
  ASSERT_EQ(lmax.integrity().problems.size(), 1U);
  EXPECT_EQ(lmax.integrity().problems[0].first, 5);
}

TEST(MakeFeed, KnowsTheSevenExchangesAndNothingElse) {
  for (const std::string_view name : kFeedNames) {
    EXPECT_NE(make_feed(name), nullptr) << name;
  }
  EXPECT_EQ(make_feed("binance"), nullptr);
}

}  // namespace
}  // namespace engine
