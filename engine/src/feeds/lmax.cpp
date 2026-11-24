// LMAX Digital: a full book once a second, stamped with that whole second. Mirrors LmaxCheck in
// recorder/check.py: the second must rise by exactly one, or a message was missed.
#include <optional>
#include <string>

#include "feeds/feeds.hpp"
#include "utc_time.hpp"

namespace engine::feeds {
namespace {

class Lmax final : public Feed {
 public:
  Lmax() : Feed(/*change_only=*/false) {}
  bool market_open() const override { return open_; }

 private:
  void on_message(std::int64_t wall_ns, simdjson::ondemand::document& doc) override {
    if (find_string(doc, "type") != "ORDER_BOOK") {
      return;  // the subscription reply, or a status announcement
    }
    const std::string_view stamp = doc["timestamp"];
    const auto exchange_ns = parse_utc_ns(stamp);
    if (!exchange_ns) {
      throw BadMessage("not a UTC timestamp: " + std::string(stamp));
    }
    const std::int64_t second = *exchange_ns / kSecondNs;
    if (last_second_ && second != *last_second_ + 1) {
      if (second > *last_second_) {
        broken(wall_ns, std::to_string(second - *last_second_ - 1) + " second(s) skipped before " +
                            std::string(stamp));
      } else {
        broken(wall_ns, "book for " + std::string(stamp) + " repeated or out of order");
      }
    }
    last_second_ = second;

    const std::string_view status = doc["status"];
    open_ = status == "OPEN";
    if (!open_) {
      mutable_integrity().count("books_not_open");
    }
    read_levels(doc["bids"], bids_scratch_);
    read_levels(doc["asks"], asks_scratch_);
    book_.bids.assign(bids_scratch_);
    book_.asks.assign(asks_scratch_);
    applied(wall_ns, *exchange_ns);
    ready(wall_ns);
  }

  // Levels are objects: [{"price":"60278.500000","quantity":"0.1700"}, ...].
  static void read_levels(simdjson::ondemand::array levels, std::vector<Level>& out) {
    out.clear();
    for (auto entry : levels) {
      simdjson::ondemand::object level = entry;
      out.push_back({decimal(level["price"]), decimal(level["quantity"])});
    }
  }

  std::optional<std::int64_t> last_second_;
  bool open_ = true;
};

}  // namespace

std::unique_ptr<Feed> lmax() { return std::make_unique<Lmax>(); }

}  // namespace engine::feeds
