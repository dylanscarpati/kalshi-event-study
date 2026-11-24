// Bullish: every message is a full book of up to 200 levels a side, about ten a second. Mirrors
// BullishCheck in recorder/check.py: each message's sequence range must start right after the
// previous one's, or a message was missed.
#include <optional>
#include <string>

#include "feeds/feeds.hpp"

namespace engine::feeds {
namespace {

class Bullish final : public Feed {
 public:
  Bullish() : Feed(/*change_only=*/false) {}
  std::size_t depth_limit() const override { return 200; }

 private:
  void on_message(std::int64_t wall_ns, simdjson::ondemand::document& doc) override {
    if (find_string(doc, "type") != "snapshot") {
      return;  // the subscription reply has no type
    }
    simdjson::ondemand::object data = doc["data"];

    // The range comes after the levels in the message; it is read first, as check.py does.
    std::int64_t lower = 0;
    std::int64_t upper = 0;
    int count = 0;
    for (auto number : data["sequenceNumberRange"].get_array()) {
      (count == 0 ? lower : upper) = number.get_int64();
      ++count;
    }
    if (count != 2) {
      throw BadMessage("sequenceNumberRange does not hold two numbers");
    }
    if (last_upper_ && lower != *last_upper_ + 1) {
      broken(wall_ns, "sequence numbers " + std::to_string(*last_upper_ + 1) + " to " +
                          std::to_string(lower - 1) + " missing");
    }
    last_upper_ = upper;

    const std::int64_t exchange_ns = integer_in_string(data["timestamp"]) * 1'000'000;  // ms
    read_flat_levels(data["bids"], bids_scratch_);
    read_flat_levels(data["asks"], asks_scratch_);
    book_.bids.assign(bids_scratch_);
    book_.asks.assign(asks_scratch_);
    applied(wall_ns, exchange_ns);
    ready(wall_ns);
  }

  // Levels come as one flat array of text: [price0, size0, price1, size1, ...].
  static void read_flat_levels(simdjson::ondemand::array values, std::vector<Level>& out) {
    out.clear();
    Level level;
    bool have_price = false;
    for (auto item : values) {
      if (!have_price) {
        level.price = decimal(item.value());
      } else {
        level.qty = decimal(item.value());
        out.push_back(level);
      }
      have_price = !have_price;
    }
    if (have_price) {
      throw BadMessage("a price without a size (an odd number of values)");
    }
  }

  std::optional<std::int64_t> last_upper_;
};

}  // namespace

std::unique_ptr<Feed> bullish() { return std::make_unique<Bullish>(); }

}  // namespace engine::feeds
