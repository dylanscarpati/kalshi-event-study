// Coinbase (Advanced Trade, level2 and heartbeats): one full book, then changes. Mirrors
// CoinbaseCheck in recorder/check.py: every message on the connection carries sequence_num,
// counting from 0 by exactly one, so a jump means a message was lost.
#include <string>

#include "feeds/feeds.hpp"

namespace engine::feeds {
namespace {

class Coinbase final : public Feed {
 public:
  Coinbase() : Feed(/*change_only=*/true) {}

 private:
  void on_message(std::int64_t wall_ns, simdjson::ondemand::document& doc) override {
    auto number = find(doc, "sequence_num");
    if (!number) {
      return;  // check.py ignores a message without one
    }
    const std::int64_t sequence = number->get_int64();
    const std::int64_t next = checked_add(sequence, 1);  // first, so an overflow is one break
    if (sequence != expected_) {
      broken(wall_ns, "sequence number " + std::to_string(expected_) + " expected, " +
                          std::to_string(sequence) + " received");
    }
    expected_ = next;

    const std::string_view channel = doc["channel"];
    if (channel != "l2_data") {
      return;  // heartbeats and subscription replies
    }
    const std::int64_t exchange_ns = utc_ns(doc["timestamp"]);
    for (auto entry : doc["events"]) {
      simdjson::ondemand::object event = entry;
      const std::string_view type = event["type"];
      const bool snapshot = type == "snapshot";
      if (snapshot) {
        bids_scratch_.clear();
        asks_scratch_.clear();
      }
      for (auto item : event["updates"]) {
        simdjson::ondemand::object update = item;
        const std::string_view side = update["side"];
        const Level level{decimal(update["price_level"]), decimal(update["new_quantity"])};
        const bool bid = side == "bid";
        if (!bid && side != "offer") {
          throw BadMessage("unknown side: " + std::string(side));
        }
        if (snapshot) {
          (bid ? bids_scratch_ : asks_scratch_).push_back(level);
        } else {
          (bid ? book_.bids : book_.asks).set(level.price, level.qty);
        }
      }
      if (snapshot) {  // about 33,000 levels: loaded in one sort, not one insert at a time
        book_.bids.assign(bids_scratch_);
        book_.asks.assign(asks_scratch_);
        ready(wall_ns);
      }
    }
    applied(wall_ns, exchange_ns);
  }

  std::int64_t expected_ = 0;
};

}  // namespace

std::unique_ptr<Feed> coinbase() { return std::make_unique<Coinbase>(); }

}  // namespace engine::feeds
