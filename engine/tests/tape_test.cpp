#include "tape.hpp"

#include <gtest/gtest.h>

#include <stdexcept>
#include <string>
#include <vector>

#include "test_tapes.hpp"

namespace engine {
namespace {

using testing::event_line;
using testing::GzipWriter;
using testing::message_line;
using testing::test_path;
using testing::write_file;

struct Seen {
  Record::Kind kind;
  std::int64_t wall_ns;
  std::int64_t mono_ns;
  std::string event;
  std::string body;
  bool binary;
};

std::vector<Seen> read_all(TapeReader& reader) {
  std::vector<Seen> seen;
  Record record;
  while (reader.next(record)) {
    EXPECT_GE(record.body.padding(), simdjson::SIMDJSON_PADDING);
    seen.push_back({record.kind, record.wall_ns, record.mono_ns, std::string(record.event),
                    std::string(record.body), record.binary});
  }
  return seen;
}

TEST(TapeReader, ReadsMessagesAndEventsInOrder) {
  const std::string tricky = R"({"text":"a \"quoted\" word, a back\\slash, caf)"
                             "\xc3\xa9"  // é as UTF-8
                             R"("})";
  GzipWriter gz;
  gz.write(event_line(100, "connected"));
  gz.write(message_line(200, tricky));
  gz.write(event_line(300, "rest_snapshot", R"({"bids":[["1.5","2"]]})"));
  gz.write(R"({"wall_ns":400,"mono_ns":7,"raw_b64":"/w=="})"
           "\n");
  const auto path = test_path("tape.jsonl.gz");
  write_file(path, gz.finish());

  TapeReader reader(path);
  const auto seen = read_all(reader);
  ASSERT_EQ(seen.size(), 4U);
  EXPECT_EQ(seen[0].kind, Record::Kind::event);
  EXPECT_EQ(seen[0].event, "connected");
  EXPECT_EQ(seen[0].body, "");
  EXPECT_EQ(seen[1].kind, Record::Kind::message);
  EXPECT_EQ(seen[1].wall_ns, 200);
  EXPECT_EQ(seen[1].mono_ns, 200 - 1'000'000'000);
  EXPECT_EQ(seen[1].body, tricky);  // unescaped back to exactly what the exchange sent
  EXPECT_EQ(seen[2].event, "rest_snapshot");
  EXPECT_EQ(seen[2].body, R"({"bids":[["1.5","2"]]})");
  EXPECT_EQ(seen[3].kind, Record::Kind::message);
  EXPECT_TRUE(seen[3].binary);
  EXPECT_EQ(seen[3].mono_ns, 7);
  EXPECT_TRUE(reader.complete());
  EXPECT_FALSE(reader.damaged());
}

TEST(TapeReader, ReadsAMessageLargerThanItsBuffers) {
  // Coinbase's first message is a 4 MB book.
  std::string big = R"({"levels":[)";
  while (big.size() < 5'000'000) {
    big += R"(["57748.12","0.00012"],)";
  }
  big += R"(["1","1"]]})";
  GzipWriter gz;
  gz.write(message_line(1, big));
  gz.write(message_line(2, "{}"));
  const auto path = test_path("big.jsonl.gz");
  write_file(path, gz.finish());

  TapeReader reader(path);
  const auto seen = read_all(reader);
  ASSERT_EQ(seen.size(), 2U);
  EXPECT_EQ(seen[0].body, big);
  EXPECT_EQ(seen[1].body, "{}");
}

TEST(TapeReader, ReadsACutOffFileUpToItsLastCompleteLine) {
  GzipWriter gz;
  gz.write(message_line(1, "{}"));
  gz.write(message_line(2, "{}"));
  gz.write(R"({"wall_ns":3,"mono_ns":3,"raw":"{\"half)");  // a line the crash cut short
  const std::size_t flushed = gz.sync_flush();
  const auto path = test_path("cut.jsonl.gz");
  write_file(path, gz.bytes().substr(0, flushed));

  TapeReader reader(path);
  const auto seen = read_all(reader);
  ASSERT_EQ(seen.size(), 2U);
  EXPECT_EQ(seen[1].wall_ns, 2);
  EXPECT_FALSE(reader.complete());
  EXPECT_FALSE(reader.damaged());
}

TEST(TapeReader, ReadsAFileCutInsideCompressedData) {
  GzipWriter gz;
  gz.write(message_line(1, "{}"));
  const std::size_t flushed = gz.sync_flush();
  for (int i = 2; i < 200; ++i) {
    gz.write(message_line(i, R"({"filler":"some text to compress"})"));
  }
  gz.sync_flush();
  const auto path = test_path("cut-inside.jsonl.gz");
  write_file(path, gz.bytes().substr(0, flushed + 40));  // stops inside the next block

  TapeReader reader(path);
  const auto seen = read_all(reader);
  ASSERT_GE(seen.size(), 1U);
  EXPECT_LT(seen.size(), 199U);
  for (std::size_t i = 0; i < seen.size(); ++i) {
    EXPECT_EQ(seen[i].wall_ns, static_cast<std::int64_t>(i + 1));
  }
  EXPECT_FALSE(reader.complete());
}

TEST(TapeReader, StopsQuietlyAtDamagedBytesAtTheEnd) {
  GzipWriter gz;
  gz.write(message_line(1, "{}"));
  gz.write(message_line(2, "{}"));
  const std::size_t flushed = gz.sync_flush();
  const auto path = test_path("junk.jsonl.gz");
  write_file(path, gz.bytes().substr(0, flushed) + std::string(64, '\0'));  // a power cut

  TapeReader reader(path);
  const auto seen = read_all(reader);
  EXPECT_EQ(seen.size(), 2U);
  EXPECT_FALSE(reader.complete());
  EXPECT_TRUE(reader.damaged());
}

TEST(TapeReader, AWrongGzipChecksumMeansDamagedNotComplete) {
  GzipWriter gz;
  gz.write(message_line(1, "{}"));
  std::string bytes = gz.finish();
  bytes[bytes.size() - 8] = static_cast<char>(bytes[bytes.size() - 8] ^ 0x01);  // CRC32 field
  const auto path = test_path("bad-crc.jsonl.gz");
  write_file(path, bytes);

  TapeReader reader(path);
  read_all(reader);
  EXPECT_FALSE(reader.complete());
  EXPECT_TRUE(reader.damaged());
}

TEST(TapeReader, AnUnreadableLineEndsTheTapeAsDamaged) {
  GzipWriter gz;
  gz.write(message_line(1, "{}"));
  gz.write("this is not a record\n");
  gz.write(message_line(3, "{}"));
  const auto path = test_path("bad-line.jsonl.gz");
  write_file(path, gz.finish());

  TapeReader reader(path);
  const auto seen = read_all(reader);
  EXPECT_EQ(seen.size(), 1U);
  EXPECT_TRUE(reader.damaged());
}

TEST(TapeReader, AnEmptyFileHasNoRecords) {
  const auto path = test_path("empty.jsonl.gz");
  write_file(path, "");
  TapeReader reader(path);
  EXPECT_TRUE(read_all(reader).empty());
  EXPECT_FALSE(reader.complete());
  EXPECT_FALSE(reader.damaged());
}

TEST(TapeReader, AMissingFileThrows) {
  EXPECT_THROW(TapeReader(test_path("missing.jsonl.gz")), std::runtime_error);
}

}  // namespace
}  // namespace engine
