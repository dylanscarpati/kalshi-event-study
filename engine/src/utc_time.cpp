#include "utc_time.hpp"

#include <chrono>

namespace engine {
namespace {

// Reads `count` digits starting at `at` as a number; returns -1 if any is not a digit.
int digits_at(std::string_view text, std::size_t at, std::size_t count) {
  int value = 0;
  for (std::size_t i = at; i < at + count; ++i) {
    if (i >= text.size() || text[i] < '0' || text[i] > '9') {
      return -1;
    }
    value = (value * 10) + (text[i] - '0');
  }
  return value;
}

}  // namespace

std::optional<std::int64_t> parse_utc_ns(std::string_view text) {
  // Fixed positions: YYYY-MM-DDTHH:MM:SS
  //                  0123456789012345678
  if (text.size() < 20 || text[4] != '-' || text[7] != '-' || text[10] != 'T' || text[13] != ':' ||
      text[16] != ':' || text.back() != 'Z') {
    return std::nullopt;
  }
  const int year = digits_at(text, 0, 4);
  const int month = digits_at(text, 5, 2);
  const int day = digits_at(text, 8, 2);
  const int hour = digits_at(text, 11, 2);
  const int minute = digits_at(text, 14, 2);
  const int second = digits_at(text, 17, 2);
  if (year < 0 || month < 0 || day < 0 || hour < 0 || hour > 23 || minute < 0 || minute > 59 ||
      second < 0 || second > 59) {
    return std::nullopt;
  }
  const std::chrono::year_month_day date{std::chrono::year{year},
                                         std::chrono::month{static_cast<unsigned>(month)},
                                         std::chrono::day{static_cast<unsigned>(day)}};
  if (!date.ok()) {  // rejects 2026-02-30 and the like
    return std::nullopt;
  }

  // An optional fraction of a second, then the final Z.
  std::int64_t fraction_ns = 0;
  const std::size_t end = text.size() - 1;  // the position of Z
  if (end > 19) {
    const std::size_t decimals = end - 20;
    if (text[19] != '.' || decimals == 0 || decimals > 9) {
      return std::nullopt;
    }
    const int value = digits_at(text, 20, decimals);
    if (value < 0) {
      return std::nullopt;
    }
    fraction_ns = value;
    for (std::size_t i = decimals; i < 9; ++i) {
      fraction_ns *= 10;
    }
  }

  // sys_days counts days since 1970-01-01, the same calendar arithmetic as Python's datetime.
  const std::int64_t days = std::chrono::sys_days{date}.time_since_epoch().count();
  const std::int64_t seconds =
      (days * 86'400) + (std::int64_t{hour} * 3'600) + (std::int64_t{minute} * 60) + second;
  return (seconds * kSecondNs) + fraction_ns;
}

}  // namespace engine
