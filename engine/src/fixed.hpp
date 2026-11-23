// Prices and quantities as exact integers.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace engine {

// A price or quantity held as a whole number of 10^-8 units, so "57748.1" is held as
// 5'774'810'000'000. Integer arithmetic is exact and gives the same result on every machine;
// binary floating point cannot even hold 0.1 exactly. The largest value is about 9.2e10.
using Fixed8 = std::int64_t;
inline constexpr Fixed8 kFixedOne = 100'000'000;  // the value 1.0

// Reads unsigned decimal text such as "57748.10", "0.00012000" or "3". Digits beyond the
// eighth decimal are accepted only if they are zeros (Gemini sends ten decimals). Anything else
// gives no value: empty text, a sign, an exponent, a missing digit on either side of the point,
// spaces, or a value too large to hold.
std::optional<Fixed8> parse_fixed8(std::string_view text);

// Appends the shortest exact text for a value: "57748.1", "0.00012", "3", "-0.5".
void append_fixed8(std::string& out, Fixed8 value);

}  // namespace engine
