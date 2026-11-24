#include "replay.hpp"

#include <gtest/gtest.h>

#include <algorithm>
#include <filesystem>
#include <map>
#include <string>
#include <vector>

#include "csv.hpp"
#include "fnv.hpp"
#include "test_tapes.hpp"
#include "utc_time.hpp"

namespace engine {
namespace {

using testing::event_line;
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

std::string coinbase(int sequence, const std::string& type, const std::string& price,
                     const std::string& qty) {
  return R"({"channel":"l2_data","timestamp":"2026-11-21T08:05:00Z","sequence_num":)" +
         std::to_string(sequence) + R"(,"events":[{"type":")" + type +
         R"(","product_id":"BTC-USD","updates":[{"side":"bid","event_time":"2026-11-21T08:05:00Z",)"
         R"("price_level":")" +
         price + R"(","new_quantity":")" + qty + R"("}]}]})";
}

std::string lmax(int second) {
  return R"({"type":"ORDER_BOOK","instrument_id":"btc-usd","timestamp":"2026-11-21T08:05:0)" +
         std::to_string(second) +
         R"(.000Z","status":"OPEN","bids":[{"price":"100","quantity":"1"}],"asks":[{"price":"101","quantity":"1"}]})";
}

// Two exchanges over ten seconds, with a sequence gap, a planned reconnect, a missed full book,
// and a file that stops without a closing event.
std::filesystem::path write_scenario() {
  const auto data_dir = test_path("data");
  write_tape(data_dir, "coinbase", "coinbase_20261121T080500Z.jsonl.gz",
             {event_line(at(0.5), "connected"),
              message_line(at(1.2), coinbase(0, "snapshot", "100.00", "1")),
              message_line(at(2.5), coinbase(1, "update", "100.00", "2")),
              message_line(at(4.5), coinbase(3, "update", "100.00", "3")),  // 2 is missing
              event_line(at(5.5), "planned_reconnect")});
  write_tape(data_dir, "coinbase", "coinbase_20261121T080507Z.jsonl.gz",
             {event_line(at(7.5), "connected"),
              message_line(at(8.2), coinbase(0, "snapshot", "99.00", "1")),
              message_line(at(9.5), coinbase(1, "update", "99.00", "4"))});
  write_tape(data_dir, "lmax", "lmax_20261121T080500Z.jsonl.gz",
             {event_line(at(0.2), "connected"), message_line(at(1.5), lmax(1)),
              message_line(at(2.5), lmax(2)),
              message_line(at(4.5), lmax(4)),  // the book for second 3 never came
              message_line(at(6.5), lmax(5))});
  return data_dir;
}

struct Seen {
  std::map<std::int64_t, std::vector<State>> states;  // second -> one state per feed
  std::map<std::int64_t, std::vector<bool>> gaps;
};

Seen replay_all(Replay& replay) {
  Seen seen;
  replay.run([&](std::int64_t s, std::span<const Sample> samples) {
    for (const Sample& sample : samples) {
      seen.states[s].push_back(sample.state);
      seen.gaps[s].push_back(sample.gap);
    }
  });
  return seen;
}

TEST(Replay, ReportsEachBooksStateAtEverySecond) {
  const auto data_dir = write_scenario();
  Replay replay(data_dir, {"coinbase", "lmax"});
  const Seen seen = replay_all(replay);

  using S = State;
  const std::map<std::int64_t, std::vector<State>> expected = {
      {at(1), {S::not_ready, S::not_ready}},  // connected, no book yet
      {at(2), {S::ok, S::ok}},
      {at(3), {S::ok, S::ok}},
      {at(4), {S::ok, S::ok}},
      {at(5), {S::invalid, S::ok}},         // Coinbase missed message 2
      {at(6), {S::closed, S::ok}},          // Coinbase reconnects on purpose
      {at(7), {S::closed, S::no_data}},     // LMAX's file stopped at 6.5 without a closing event
      {at(8), {S::not_ready, S::no_data}},  // the new Coinbase connection has no book yet
      {at(9), {S::ok, S::no_data}},
  };
  EXPECT_EQ(seen.states, expected);
  // LMAX's missed book is a gap at that second only.
  EXPECT_EQ(seen.gaps.at(at(5)), (std::vector<bool>{false, true}));
  EXPECT_EQ(seen.gaps.at(at(6)), (std::vector<bool>{false, false}));
}

TEST(Replay, SummarisesEveryFileInFeedThenTimeOrder) {
  const auto data_dir = write_scenario();
  Replay replay(data_dir, {"coinbase", "lmax"});
  replay.run([](std::int64_t, std::span<const Sample>) {});
  const auto files = replay.summaries();
  ASSERT_EQ(files.size(), 3U);
  EXPECT_EQ(files[0].file, "coinbase_20261121T080500Z.jsonl.gz");
  EXPECT_EQ(files[0].integrity.invalid_from_ns, at(4.5));
  EXPECT_EQ(files[0].integrity.ended, "planned_reconnect");
  EXPECT_EQ(files[1].file, "coinbase_20261121T080507Z.jsonl.gz");
  EXPECT_EQ(files[2].feed, "lmax");
  EXPECT_EQ(files[2].integrity.break_ns, (std::vector<std::int64_t>{at(4.5)}));
  EXPECT_TRUE(files[2].complete);
}

TEST(Replay, ReportsOnlyTheSecondsAskedFor) {
  const auto data_dir = write_scenario();
  Replay replay(data_dir, {"coinbase", "lmax"});
  std::vector<std::int64_t> seconds;
  replay.run([&](std::int64_t s, std::span<const Sample>) { seconds.push_back(s); }, at(3), at(5));
  EXPECT_EQ(seconds, (std::vector<std::int64_t>{at(3), at(4), at(5)}));
  EXPECT_EQ(replay.summaries().size(), 3U);  // every file is still read in full
}

TEST(Replay, AnExchangeWithoutTapesHasNoData) {
  const auto data_dir = write_scenario();
  Replay replay(data_dir, {"lmax", "kraken"});
  std::vector<State> kraken;
  replay.run(
      [&](std::int64_t, std::span<const Sample> samples) { kraken.push_back(samples[1].state); });
  ASSERT_FALSE(kraken.empty());
  EXPECT_TRUE(
      std::all_of(kraken.begin(), kraken.end(), [](State s) { return s == State::no_data; }));
}

TEST(Csv, WritesOneExactRowPerExchange) {
  Book book;
  book.bids.set(5'774'810'000'000, 20'000);       // 57748.1, 0.0002
  book.asks.set(5'774'820'000'000, 150'000'000);  // 57748.2, 1.5
  Sample sample;
  sample.feed = "kraken";
  sample.state = State::ok;
  sample.book = &book;
  sample.gap = true;
  sample.last_wall_ns = 7;
  sample.last_exchange_ns = 6;
  Sample empty;
  empty.feed = "lmax";
  std::string out;
  const std::vector<Sample> samples = {sample, empty};
  append_csv_rows(out, kT0, samples);
  EXPECT_EQ(out,
            "1795248300,kraken,ok,57748.1,0.0002,57748.2,1.5,1,1,0,1,0,7,6\n"
            "1795248300,lmax,no_data,,,,,0,0,0,0,0,0,0\n");
}

TEST(Fnv1a64, MatchesThePublishedTestValues) {
  const auto hash = [](std::string_view text) {
    Fnv1a64 fnv;
    fnv.add(text);
    return fnv.value();
  };
  EXPECT_EQ(hash(""), 0xcbf29ce484222325ULL);
  EXPECT_EQ(hash("a"), 0xaf63dc4c8601ec8cULL);
  EXPECT_EQ(hash("foobar"), 0x85944171f73967e8ULL);
}

TEST(MeasuredWindows, AreTheQuarterHoursWhoseMinuteLiesInTheRecording) {
  const std::int64_t q = 1'795'248'900LL * kSecondNs;  // 08:15:00Z
  EXPECT_EQ(measured_windows(q - 61 * kSecondNs, q), (std::vector<std::int64_t>{q}));
  EXPECT_EQ(measured_windows(q - 60 * kSecondNs, q), (std::vector<std::int64_t>{q}));
  EXPECT_TRUE(measured_windows(q - 59 * kSecondNs, q).empty());
  EXPECT_TRUE(measured_windows(q - 61 * kSecondNs, q - 1).empty());
}

TEST(WindowCovered, NeedsOneFileWithABookAcrossTheWholeMinute) {
  const std::int64_t q = 1'795'248'900LL * kSecondNs;
  FileSummary file;
  file.integrity.ready_ns = q - 61 * kSecondNs;
  file.integrity.end_ns = q;
  EXPECT_TRUE(window_covered(q, std::vector<FileSummary>{file}));

  FileSummary late = file;
  late.integrity.ready_ns = q - 59 * kSecondNs;  // the book arrived after the minute began
  EXPECT_FALSE(window_covered(q, std::vector<FileSummary>{late}));

  FileSummary broken = file;
  broken.integrity.break_ns = {q - 30 * kSecondNs};
  EXPECT_FALSE(window_covered(q, std::vector<FileSummary>{broken}));

  FileSummary spoiled = file;
  spoiled.integrity.invalid_from_ns = q;
  EXPECT_FALSE(window_covered(q, std::vector<FileSummary>{spoiled}));
  EXPECT_TRUE(window_covered(q, std::vector<FileSummary>{spoiled, file}));
}

}  // namespace
}  // namespace engine
