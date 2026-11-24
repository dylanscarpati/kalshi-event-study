// Gemini (btcusd@depth@100ms and btcusd@depth5): the first update is the full book, then
// changes. Mirrors GeminiCheck in recorder/check.py: consecutive updates overlap (U equals the
// previous u); once a second the top five levels are compared with Gemini's own.
#include <algorithm>
#include <optional>
#include <string>

#include "feeds/feeds.hpp"

namespace engine::feeds {
namespace {

class Gemini final : public Feed {
 public:
  Gemini() : Feed(/*change_only=*/true) {}

 private:
  void on_message(std::int64_t wall_ns, simdjson::ondemand::document& doc) override {
    if (find_string(doc, "e") == "depthUpdate") {
      depth_update(wall_ns, doc);
      return;
    }
    auto id = find(doc, "lastUpdateId");
    if (id && last_u_ && id->get_int64() == *last_u_) {
      top_five(wall_ns, doc);
    }
  }

  void depth_update(std::int64_t wall_ns, simdjson::ondemand::document& doc) {
    const std::int64_t exchange_ns = doc["E"].get_int64();  // nanoseconds, 19 digits
    const std::int64_t first = doc["U"].get_int64();
    const std::int64_t last = doc["u"].get_int64();
    if (!last_u_) {  // the first one is the full book
      if (first != last) {
        broken(wall_ns, "the first update is not a full book (U differs from u)");
      }
      read_level_arrays(doc["b"], bids_scratch_);
      read_level_arrays(doc["a"], asks_scratch_);
      book_.bids.assign(bids_scratch_);
      book_.asks.assign(asks_scratch_);
      ready(wall_ns);
    } else if (last <= *last_u_) {  // older than what is applied: skipped
      mutable_integrity().count("stale_updates_skipped");
      return;
    } else {
      if (first > *last_u_) {
        broken(wall_ns, "update numbers " + std::to_string(*last_u_ + 1) + " to " +
                            std::to_string(first - 1) + " missing");
      }
      read_level_arrays(doc["b"], bids_scratch_);
      read_level_arrays(doc["a"], asks_scratch_);
      for (const Level& level : bids_scratch_) {
        book_.bids.set(level.price, level.qty);
      }
      for (const Level& level : asks_scratch_) {
        book_.asks.set(level.price, level.qty);
      }
    }
    last_u_ = last;
    applied(wall_ns, exchange_ns);
  }

  void top_five(std::int64_t wall_ns, simdjson::ondemand::document& doc) {
    if (integrity().invalid_from_ns) {
      return;
    }
    mutable_integrity().count("top_five_compared");
    read_level_arrays(doc["bids"], bids_scratch_);
    read_level_arrays(doc["asks"], asks_scratch_);
    if (top_matches(book_.bids, bids_scratch_) && top_matches(book_.asks, asks_scratch_)) {
      mutable_integrity().count("top_five_matched");
    } else {
      broken(wall_ns, "rebuilt top five differs from Gemini's snapshot");
    }
  }

  // True if the side's best levels (at most five) equal the expected ones exactly.
  static bool top_matches(const Side& side, const std::vector<Level>& expected) {
    const std::size_t n = std::min<std::size_t>(5, side.size());
    if (n != expected.size()) {
      return false;
    }
    for (std::size_t i = 0; i < n; ++i) {
      if (!(side.level(i) == expected[i])) {
        return false;
      }
    }
    return true;
  }

  std::optional<std::int64_t> last_u_;
};

}  // namespace

std::unique_ptr<Feed> gemini() { return std::make_unique<Gemini>(); }

}  // namespace engine::feeds
