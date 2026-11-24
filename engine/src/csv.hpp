// The per-second output as CSV text, the same on every machine.
#pragma once

#include <cstdint>
#include <span>
#include <string>
#include <string_view>

#include "replay.hpp"

namespace engine {

// One row per second per exchange. Prices and sizes are exact decimals; times are integer
// nanoseconds since 1970 UTC; an empty side leaves its four cells empty.
inline constexpr std::string_view kCsvHeader =
    "second,feed,state,bid,bid_qty,ask,ask_qty,bid_levels,ask_levels,crossed,gap,clock_step,"
    "last_wall_ns,last_exchange_ns\n";

// Appends the rows for one second.
void append_csv_rows(std::string& out, std::int64_t second_ns, std::span<const Sample> samples);

}  // namespace engine
