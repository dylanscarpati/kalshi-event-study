// Tests from the independent review (2026-11-24). Each one failed before its fix.
#include <gtest/gtest.h>

#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "csv.hpp"
#include "feed.hpp"
#include "feed_helpers.hpp"
#include "replay.hpp"
#include "test_tapes.hpp"
#include "utc_time.hpp"

namespace engine {
namespace {

using testing::best;
using testing::event_line;
using testing::Fed;
using testing::fx;
using testing::GzipWriter;
using testing::message_line;
using testing::test_path;
using testing::write_file;

constexpr std::int64_t kT0 = 1'795'248'300LL * kSecondNs;  // 2026-11-21T08:05:00Z
constexpr std::int64_t at(double seconds) { return kT0 + static_cast<std::int64_t>(seconds * 1e9); }

void write_tape(const std::filesystem::path& data_dir, const std::string& feed,
                const std::string& name, const std::vector<std::string>& lines) {
  const auto dir = data_dir / feed / "2026-11-21";
  std::filesystem::create_directories(dir);
  GzipWriter gz;
  for (const auto& line : lines) {
    gz.write(line);
  }
  write_file(dir / name, gz.finish());
}

void write_raw(const std::filesystem::path& data_dir, const std::string& feed,
               const std::string& name, const std::string& bytes) {
  const auto dir = data_dir / feed / "2026-11-21";
  std::filesystem::create_directories(dir);
  write_file(dir / name, bytes);
}

std::string coinbase(int sequence, const std::string& type, const std::string& price,
                     const std::string& qty) {
  return R"({"channel":"l2_data","timestamp":"2026-11-21T08:05:00Z","sequence_num":)" +
         std::to_string(sequence) + R"(,"events":[{"type":")" + type +
         R"(","product_id":"BTC-USD","updates":[{"side":"bid","event_time":"2026-11-21T08:05:00Z",)"
         R"("price_level":")" +
         price + R"(","new_quantity":")" + qty + R"("}]}]})";
}

std::map<std::int64_t, std::string> rows_by_second(Replay& replay) {
  std::map<std::int64_t, std::string> rows;
  replay.run([&](std::int64_t s, std::span<const Sample> samples) {
    std::string out;
    append_csv_rows(out, s, samples);
    rows[(s - kT0) / kSecondNs] = out;
  });
  return rows;
}

// -------------------------------------------------------------------------------------------
// Replay: a file with no records (a crash before the recorder's first flush leaves a 0-byte
// file). make_tapes.py's sample_row() says the sample comes from "the last file whose first
// record has been applied", so a file with no records must not change what is shown.
// -------------------------------------------------------------------------------------------

TEST(ReviewReplay, AnEmptyFileBetweenTwoConnectionsDoesNotChangeTheState) {
  const auto data_dir = test_path("data");
  write_tape(data_dir, "coinbase", "coinbase_20261121T080500Z.jsonl.gz",
             {event_line(at(0.5), "connected"),
              message_line(at(1.2), coinbase(0, "snapshot", "100.00", "1")),
              event_line(at(3.5), "planned_reconnect")});
  write_raw(data_dir, "coinbase", "coinbase_20261121T080504Z.jsonl.gz", "");  // 0 bytes
  write_tape(data_dir, "coinbase", "coinbase_20261121T080507Z.jsonl.gz",
             {event_line(at(7.5), "connected"),
              message_line(at(8.2), coinbase(0, "snapshot", "99.00", "1"))});
  Replay replay(data_dir, {"coinbase"});
  const auto rows = rows_by_second(replay);
  // Seconds 4 to 7: the first connection was closed at 3.5 and the third has no record yet.
  for (const std::int64_t second : {4, 5, 6, 7}) {
    EXPECT_NE(rows.at(second).find(",coinbase,closed,100,1,"), std::string::npos)
        << "second " << second << ": " << rows.at(second);
  }
}

TEST(ReviewReplay, AFeedWhoseOnlyFileIsEmptyHasNoData) {
  const auto data_dir = test_path("data");
  write_tape(data_dir, "coinbase", "coinbase_20261121T080500Z.jsonl.gz",
             {event_line(at(0.5), "connected"),
              message_line(at(1.2), coinbase(0, "snapshot", "100.00", "1")),
              event_line(at(3.5), "stopped")});
  write_raw(data_dir, "lmax", "lmax_20261121T080500Z.jsonl.gz", "");  // 0 bytes
  Replay replay(data_dir, {"coinbase", "lmax"});
  std::vector<State> lmax;
  replay.run(
      [&](std::int64_t, std::span<const Sample> samples) { lmax.push_back(samples[1].state); });
  ASSERT_FALSE(lmax.empty());
  for (std::size_t i = 0; i < lmax.size(); ++i) {
    EXPECT_EQ(name_of(lmax[i]), "no_data") << "second " << i + 1;
  }
}

// -------------------------------------------------------------------------------------------
// Bitstamp: the refined decision 4 ("only levels that came from a download and were never set
// by a change may be removed as phantoms") and the file's own comment ("A level a change has set
// is never removed this way").
// -------------------------------------------------------------------------------------------

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

TEST(ReviewBitstamp, ALevelSetByAChangeBeforeTheFirstDownloadIsNeverAPhantom) {
  Fed bitstamp("bitstamp");
  // A change sets the bid at 99.00; the first download (newer) holds it, as it should.
  bitstamp.message(10, bitstamp_change(1000, R"(["99.00","1"])", ""));
  bitstamp.event(20, "rest_snapshot",
                 bitstamp_download(1500, R"(["100.00","1"],["99.00","1"])", R"(["101.00","1"])"));
  bitstamp.message(30, bitstamp_change(2000, R"(["98.00","1"])", ""));
  // The second download lacks 99.00 (an order cancelled and re-placed inside one netting
  // interval). No change has removed it.
  bitstamp.event(40, "rest_snapshot",
                 bitstamp_download(4000, R"(["100.00","1"],["98.00","1"])", R"(["101.00","1"])"));
  EXPECT_EQ(bitstamp.count("phantom_levels_removed"), 0);
  EXPECT_EQ(bitstamp->book().bids.qty_at(fx("99")), fx("1"));
}

// Integers from an exchange that do not fit: a break, never undefined behaviour.
TEST(ReviewUtcTime, AYearOutsideTheInt64RangeGivesNoValue) {
  EXPECT_FALSE(parse_utc_ns("2263-01-01T00:00:00Z").has_value());
}

TEST(ReviewUtcTime, AnLmaxBookWithAFarFutureStampIsABreakNotACrash) {
  testing::Fed lmax("lmax");
  lmax.message(
      10,
      R"({"type":"ORDER_BOOK","instrument_id":"btc-usd","timestamp":"9999-12-31T23:59:59.000Z","status":"OPEN","bids":[],"asks":[]})");
  EXPECT_EQ(lmax.integrity().break_ns.size(), 1U);  // a break, not undefined behaviour
}

// The same class of fault in the parsers: arithmetic on an exchange's integer that can overflow.
TEST(ReviewOverflow, CoinbaseSequenceAtInt64MaxIsNotUndefined) {
  testing::Fed coinbase("coinbase");
  coinbase.message(10, R"({"channel":"heartbeats","sequence_num":9223372036854775807})");
  EXPECT_EQ(coinbase.integrity().problems.size(), 1U);  // sequence 0 was expected: one break
}

TEST(ReviewOverflow, BitstampMicrotimestampTimesAThousandIsNotUndefined) {
  testing::Fed bitstamp("bitstamp");
  bitstamp.message(
      10, R"({"data":{"microtimestamp":"1000","bids":[["90.00","1"]],"asks":[]},"event":"data"})");
  bitstamp.event(20, "rest_snapshot",
                 R"({"microtimestamp":"1500","bids":[["100.00","1"]],"asks":[["101.00","1"]]})");
  bitstamp.message(
      30, R"({"data":{"microtimestamp":"9999999999999999","bids":[],"asks":[]},"event":"data"})");
  EXPECT_EQ(bitstamp.integrity().invalid_from_ns, 30);
}

TEST(ReviewOverflow, CryptocomMillisecondsTimesAMillionIsNotUndefined) {
  testing::Fed cryptocom("cryptocom");
  cryptocom.message(
      10,
      R"({"id":-1,"method":"subscribe","code":0,"result":{"channel":"book","data":[{"asks":[],"bids":[],"t":99999999999999,"u":5}]}})");
  EXPECT_EQ(cryptocom.integrity().invalid_from_ns, 10);
}

}  // namespace
}  // namespace engine
