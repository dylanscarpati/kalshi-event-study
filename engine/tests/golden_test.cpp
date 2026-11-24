// The replay of the synthetic tapes in tests/tapes/ must equal, byte for byte, what
// tests/make_tapes.py says it must be. That script writes the tapes and the expected output;
// after changing it, run: python3 engine/tests/make_tapes.py
#include <gtest/gtest.h>

#include <array>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "csv.hpp"
#include "feed.hpp"
#include "file_json.hpp"
#include "fnv.hpp"
#include "replay.hpp"

namespace engine {
namespace {

const std::filesystem::path kTapes = ENGINE_TEST_TAPES;  // set in engine/CMakeLists.txt

std::string read_text(const std::filesystem::path& path) {
  std::ifstream in(path, std::ios::binary);
  std::stringstream text;
  text << in.rdbuf();
  return text.str();
}

std::vector<std::string> lines_of(const std::string& text) {
  std::vector<std::string> lines;
  std::stringstream stream(text);
  for (std::string line; std::getline(stream, line);) {
    lines.push_back(line);
  }
  return lines;
}

struct Output {
  std::string csv;
  std::string files;
};

Output replay_tapes() {
  const std::vector<std::string> feeds(kFeedNames.begin(), kFeedNames.end());
  Replay replay(kTapes, feeds);
  Output out;
  out.csv = kCsvHeader;
  replay.run([&](std::int64_t second_ns, std::span<const Sample> samples) {
    append_csv_rows(out.csv, second_ns, samples);
  });
  for (const FileSummary& file : replay.summaries()) {
    out.files += file_json(file);
    out.files += '\n';
  }
  return out;
}

// Compares line by line, so that a failure shows the first line that differs.
void expect_same_lines(const std::string& expected, const std::string& actual,
                       const std::string& what) {
  const auto want = lines_of(expected);
  const auto have = lines_of(actual);
  for (std::size_t i = 0; i < want.size() && i < have.size(); ++i) {
    if (want[i] != have[i]) {
      ADD_FAILURE() << what << ", line " << i + 1 << "\n  expected: " << want[i]
                    << "\n  replayed: " << have[i];
      return;
    }
  }
  EXPECT_EQ(want.size(), have.size()) << what << ": number of lines";
}

TEST(Golden, EverySecondEqualsWhatTheGeneratorExpects) {
  expect_same_lines(read_text(kTapes / "expected.csv"), replay_tapes().csv, "expected.csv");
}

TEST(Golden, EveryFileEqualsWhatTheGeneratorExpects) {
  expect_same_lines(read_text(kTapes / "expected_files.ndjson"), replay_tapes().files,
                    "expected_files.ndjson");
}

// The deterministic replay test: two replays in one process write exactly the same bytes, and
// their hash is the one committed with the tapes, on every machine.
TEST(Golden, TwoReplaysGiveTheSameBytesAndTheCommittedHash) {
  Fnv1a64 first;
  Fnv1a64 second;
  first.add(replay_tapes().csv);
  second.add(replay_tapes().csv);
  EXPECT_EQ(first.value(), second.value());
  std::array<char, 24> hex{};
  std::snprintf(hex.data(), hex.size(), "%016llx\n",
                static_cast<unsigned long long>(first.value()));
  EXPECT_EQ(std::string(hex.data()), read_text(kTapes / "expected.fnv"));
}

}  // namespace
}  // namespace engine
