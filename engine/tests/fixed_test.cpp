#include "fixed.hpp"

#include <gtest/gtest.h>

#include <cstdint>
#include <limits>
#include <optional>
#include <string>

namespace engine {
namespace {

std::string text_of(Fixed8 value) {
  std::string out;
  append_fixed8(out, value);
  return out;
}

TEST(ParseFixed8, ReadsTheFormatsEachExchangeSends) {
  EXPECT_EQ(parse_fixed8("57748.1"), 5'774'810'000'000);          // Kraken price
  EXPECT_EQ(parse_fixed8("0.00020000"), 20'000);                  // Kraken quantity
  EXPECT_EQ(parse_fixed8("57667.72"), 5'766'772'000'000);         // Coinbase, Bitstamp
  EXPECT_EQ(parse_fixed8("0.4851628800"), 48'516'288);            // Gemini: ten decimals
  EXPECT_EQ(parse_fixed8("60278.500000"), 6'027'850'000'000);     // LMAX
  EXPECT_EQ(parse_fixed8("60283.8000"), 6'028'380'000'000);       // Bullish
  EXPECT_EQ(parse_fixed8("98562100.00"), 9'856'210'000'000'000);  // a far-away Bitstamp ask
  EXPECT_EQ(parse_fixed8("0"), 0);
  EXPECT_EQ(parse_fixed8("3"), 300'000'000);
  EXPECT_EQ(parse_fixed8("0.00000001"), 1);
}

TEST(ParseFixed8, AcceptsZerosBeyondTheEighthDecimalOnly) {
  EXPECT_EQ(parse_fixed8("1.0000000100"), 100'000'001);
  EXPECT_EQ(parse_fixed8("1.000000001"), std::nullopt);  // a ninth nonzero digit cannot be held
}

TEST(ParseFixed8, RejectsAnythingThatIsNotPlainDecimalText) {
  for (const char* bad : {"", "-1", "+1", "1e-05", "1E5", "1.", ".5", "1.2.3", "abc", " 1", "1 ",
                          "0x10", "1,5", "nan"}) {
    EXPECT_EQ(parse_fixed8(bad), std::nullopt) << "text: \"" << bad << '"';
  }
}

TEST(ParseFixed8, RejectsValuesTooLargeToHold) {
  // The largest int64 is 9223372036854775807, which is 92233720368.54775807 here.
  EXPECT_EQ(parse_fixed8("92233720368.54775807"), std::numeric_limits<std::int64_t>::max());
  EXPECT_EQ(parse_fixed8("92233720368.54775808"), std::nullopt);
  EXPECT_EQ(parse_fixed8("92233720369"), std::nullopt);
  EXPECT_EQ(parse_fixed8("99999999999999999999999"), std::nullopt);
}

TEST(AppendFixed8, WritesTheShortestExactText) {
  EXPECT_EQ(text_of(5'774'810'000'000), "57748.1");
  EXPECT_EQ(text_of(20'000), "0.0002");
  EXPECT_EQ(text_of(1), "0.00000001");
  EXPECT_EQ(text_of(0), "0");
  EXPECT_EQ(text_of(300'000'000), "3");
  EXPECT_EQ(text_of(-50'000'000), "-0.5");
  EXPECT_EQ(text_of(std::numeric_limits<std::int64_t>::min()), "-92233720368.54775808");
}

TEST(AppendFixed8, AppendsWithoutTouchingWhatIsAlreadyThere) {
  std::string out = "bid=";
  append_fixed8(out, 150'000'000);
  EXPECT_EQ(out, "bid=1.5");
}

TEST(Fixed8, TextRoundTripsExactly) {
  for (Fixed8 value : {Fixed8{1}, Fixed8{9}, Fixed8{10}, Fixed8{12'345'678}, kFixedOne,
                       Fixed8{5'774'810'000'000}, std::numeric_limits<Fixed8>::max()}) {
    EXPECT_EQ(parse_fixed8(text_of(value)), value) << value;
  }
}

}  // namespace
}  // namespace engine
