#include "fixed.hpp"

#include <array>
#include <charconv>
#include <cstddef>
#include <limits>

namespace engine {
namespace {

bool is_digit(char c) { return c >= '0' && c <= '9'; }

}  // namespace

std::optional<Fixed8> parse_fixed8(std::string_view text) {
  constexpr std::int64_t kMax = std::numeric_limits<std::int64_t>::max();
  std::size_t i = 0;

  // The whole part: at least one digit.
  if (text.empty() || !is_digit(text[0])) {
    return std::nullopt;
  }
  std::int64_t whole = 0;
  for (; i < text.size() && is_digit(text[i]); ++i) {
    const int digit = text[i] - '0';
    if (whole > (kMax - digit) / 10) {  // whole * 10 + digit would not fit
      return std::nullopt;
    }
    whole = whole * 10 + digit;
  }

  // The fraction, in units of 10^-8. The first decimal is worth 10^7 units, the eighth 1 unit.
  std::int64_t fraction = 0;
  if (i < text.size()) {
    if (text[i] != '.' || i + 1 == text.size()) {  // only a point may follow, then a digit
      return std::nullopt;
    }
    std::int64_t place = kFixedOne / 10;
    for (++i; i < text.size(); ++i) {
      if (!is_digit(text[i])) {
        return std::nullopt;
      }
      const int digit = text[i] - '0';
      if (place > 0) {
        fraction += digit * place;
        place /= 10;
      } else if (digit != 0) {  // a ninth or later decimal that is not zero cannot be held
        return std::nullopt;
      }
    }
  }

  if (whole > (kMax - fraction) / kFixedOne) {  // whole * 10^8 + fraction would not fit
    return std::nullopt;
  }
  return whole * kFixedOne + fraction;
}

void append_fixed8(std::string& out, Fixed8 value) {
  // Work with the size of the value as an unsigned number. This also handles the most negative
  // int64, whose size does not fit in int64 itself.
  constexpr auto kOne = static_cast<std::uint64_t>(kFixedOne);
  const std::uint64_t size =
      value < 0 ? 0 - static_cast<std::uint64_t>(value) : static_cast<std::uint64_t>(value);
  if (value < 0) {
    out += '-';
  }

  std::array<char, 24> whole{};
  const auto written = std::to_chars(whole.data(), whole.data() + whole.size(), size / kOne);
  out.append(whole.data(), written.ptr);

  const std::uint64_t fraction = size % kOne;
  if (fraction == 0) {
    return;
  }
  // Adding 10^8 makes to_chars write exactly nine digits: a "1", then all eight decimals with
  // their leading zeros. The "1" is skipped and trailing zeros are dropped.
  std::array<char, 9> digits{};
  std::to_chars(digits.data(), digits.data() + digits.size(), fraction + kOne);
  std::size_t length = digits.size();
  while (digits[length - 1] == '0') {
    --length;
  }
  out += '.';
  out.append(digits.data() + 1, length - 1);
}

}  // namespace engine
