// Proves the toolchain works: the two libraries the engine depends on can be called.
#include <gtest/gtest.h>
#include <simdjson.h>
#include <zlib.h>

#include <array>
#include <cstdint>
#include <string>

TEST(Toolchain, SimdjsonParsesAnObject) {
  simdjson::ondemand::parser parser;
  simdjson::padded_string json(std::string(R"({"sequence_num":7})"));
  simdjson::ondemand::document doc = parser.iterate(json);
  EXPECT_EQ(std::int64_t{doc["sequence_num"]}, 7);
}

TEST(Toolchain, ZlibComputesTheStandardCrc32) {
  // The CRC32 of "123456789" is 0xCBF43926 (the published check value).
  const std::string text = "123456789";
  const auto* bytes = reinterpret_cast<const Bytef*>(text.data());
  EXPECT_EQ(crc32(0L, bytes, static_cast<uInt>(text.size())), 0xCBF43926UL);
}
