// Reading the UTC timestamps exchanges put in their messages.
#pragma once

#include <cstdint>
#include <optional>
#include <string_view>

namespace engine {

inline constexpr std::int64_t kSecondNs = 1'000'000'000;

// Reads "2026-11-21T08:05:00.601135158Z" (0 to 9 decimals, ending in Z) as nanoseconds since
// 1970-01-01 UTC. Anything else gives no value. Written here because the C++ library on macOS
// does not have std::chrono::parse.
std::optional<std::int64_t> parse_utc_ns(std::string_view text);

}  // namespace engine
