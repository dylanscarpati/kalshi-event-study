#include "csv.hpp"

#include <array>
#include <charconv>

#include "utc_time.hpp"

namespace engine {
namespace {

void append_integer(std::string& out, std::int64_t value) {
  std::array<char, 24> digits{};
  const auto written = std::to_chars(digits.data(), digits.data() + digits.size(), value);
  out.append(digits.data(), written.ptr);
}

// The best level of a side as "price,size", or "," if the side is empty.
void append_best(std::string& out, const Side* side) {
  if (side != nullptr && !side->empty()) {
    append_fixed8(out, side->level(0).price);
    out += ',';
    append_fixed8(out, side->level(0).qty);
  } else {
    out += ',';
  }
}

}  // namespace

void append_csv_rows(std::string& out, std::int64_t second_ns, std::span<const Sample> samples) {
  for (const Sample& sample : samples) {
    append_integer(out, second_ns / kSecondNs);
    out += ',';
    out += sample.feed;
    out += ',';
    out += name_of(sample.state);
    out += ',';
    append_best(out, sample.book != nullptr ? &sample.book->bids : nullptr);
    out += ',';
    append_best(out, sample.book != nullptr ? &sample.book->asks : nullptr);
    out += ',';
    append_integer(
        out, sample.book != nullptr ? static_cast<std::int64_t>(sample.book->bids.size()) : 0);
    out += ',';
    append_integer(
        out, sample.book != nullptr ? static_cast<std::int64_t>(sample.book->asks.size()) : 0);
    out += sample.crossed ? ",1" : ",0";
    out += sample.gap ? ",1" : ",0";
    out += sample.clock_step ? ",1," : ",0,";
    append_integer(out, sample.last_wall_ns);
    out += ',';
    append_integer(out, sample.last_exchange_ns);
    out += '\n';
  }
}

}  // namespace engine
