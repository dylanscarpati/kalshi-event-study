// Test helpers: hand messages and events to a feed without writing a tape.
#pragma once

#include <simdjson.h>

#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include "feed.hpp"
#include "fixed.hpp"

namespace engine::testing {

// A price or size from its text; throws if the text is not a plain decimal.
inline Fixed8 fx(std::string_view text) {
  const auto value = parse_fixed8(text);
  if (!value) {
    throw std::invalid_argument("not a decimal: " + std::string(text));
  }
  return *value;
}

// The n best levels of a side (fewer if the side is shorter), best first.
inline std::vector<Level> best(const Side& side, std::size_t n = 1000000) {
  std::vector<Level> out;
  for (std::size_t i = 0; i < side.size() && i < n; ++i) {
    out.push_back(side.level(i));
  }
  return out;
}

// One feed, fed by hand. By default the monotonic clock equals the wall clock, so no clock
// step is seen unless a test asks for one.
class Fed {
 public:
  explicit Fed(std::string_view name) : feed_(make_feed(name)) {
    if (!feed_) {
      throw std::invalid_argument("no feed called " + std::string(name));
    }
  }

  void message(std::int64_t wall_ns, std::string_view text, std::int64_t mono_offset_ns = 0) {
    const simdjson::padded_string body(text);
    Record record;
    record.kind = Record::Kind::message;
    record.wall_ns = wall_ns;
    record.mono_ns = wall_ns - mono_offset_ns;
    record.body = body;
    feed_->handle(record);
  }

  void event(std::int64_t wall_ns, std::string_view kind, std::string_view raw = {}) {
    const simdjson::padded_string body(raw);
    Record record;
    record.kind = Record::Kind::event;
    record.wall_ns = wall_ns;
    record.mono_ns = wall_ns;
    record.event = kind;
    record.body = body;
    feed_->handle(record);
  }

  void binary(std::int64_t wall_ns) {
    const simdjson::padded_string body(std::string_view{});
    Record record;
    record.wall_ns = wall_ns;
    record.mono_ns = wall_ns;
    record.binary = true;
    record.body = body;
    feed_->handle(record);
  }

  Feed* operator->() { return feed_.get(); }
  const Integrity& integrity() const { return feed_->integrity(); }
  std::int64_t count(std::string_view name) const {
    const auto found = feed_->integrity().counts.find(name);
    return found == feed_->integrity().counts.end() ? 0 : found->second;
  }

 private:
  std::unique_ptr<Feed> feed_;
};

}  // namespace engine::testing
