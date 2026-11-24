// Kraken (v2 book, depth 1000): one full book, then changes. Mirrors KrakenCheck in
// recorder/check.py: the book is cut back to 1000 levels a side after every message, then its
// CRC32 is compared with the checksum Kraken sends of its own top ten levels.
#include <zlib.h>

#include <array>
#include <charconv>
#include <string>

#include "feeds/feeds.hpp"

namespace engine::feeds {
namespace {

constexpr std::size_t kDepth = 1000;

class Kraken final : public Feed {
 public:
  Kraken() : Feed(/*change_only=*/true) {}
  std::size_t depth_limit() const override { return kDepth; }

 private:
  void on_message(std::int64_t wall_ns, simdjson::ondemand::document& doc) override {
    if (find_string(doc, "channel") != "book") {
      return;  // status, heartbeats and the subscription reply
    }
    const std::string_view type = doc["type"];
    const bool snapshot = type == "snapshot";
    int seen = 0;
    for (auto entry : doc["data"]) {  // holds exactly one book or change
      if (seen++ == 0) {
        simdjson::ondemand::object data = entry;
        apply(wall_ns, snapshot, data);
      }
    }
    if (seen != 1) {
      throw BadMessage("data does not hold exactly one entry");
    }
  }

  void apply(std::int64_t wall_ns, bool snapshot, simdjson::ondemand::object& data) {
    if (snapshot) {
      book_.clear();
      have_book_ = true;
      ready(wall_ns);
    }
    if (!have_book_ || integrity().invalid_from_ns) {
      return;  // as check.py: nothing is checked before the first book or after a break
    }
    read_levels(data["bids"], bids_scratch_);
    read_levels(data["asks"], asks_scratch_);
    if (snapshot) {
      book_.bids.assign(bids_scratch_);
      book_.asks.assign(asks_scratch_);
    } else {
      for (const Level& level : bids_scratch_) {
        book_.bids.set(level.price, level.qty);
      }
      for (const Level& level : asks_scratch_) {
        book_.asks.set(level.price, level.qty);
      }
    }
    book_.bids.keep_best(kDepth);  // Kraken leaves this to the client
    book_.asks.keep_best(kDepth);
    if (checksum() == data["checksum"].get_uint64()) {
      mutable_integrity().count("checksums_matched");
    } else {
      broken(wall_ns, "checksum of the rebuilt book does not match Kraken's");
    }
    applied(wall_ns, utc_ns(data["timestamp"]));
  }

  // Levels are objects whose numbers are JSON numbers: [{"price":57706.3,"qty":0.0002}, ...].
  static void read_levels(simdjson::ondemand::array levels, std::vector<Level>& out) {
    out.clear();
    for (auto entry : levels) {
      simdjson::ondemand::object level = entry;
      out.push_back({decimal(level["price"]), decimal(level["qty"])});
    }
  }

  // Kraken's checksum: CRC32 of the ten best asks (lowest first), then the ten best bids
  // (highest first). Each level adds its price, then its size, written with the pair's
  // precision (1 decimal for the price, 8 for the size), without the point or leading zeros:
  // 57706.3 gives "577063" and 0.00020000 gives "20000".
  std::uint64_t checksum() {
    text_.clear();
    auto add = [this](const Level& level) {
      append_digits(level.price / 10'000'000);  // 8 implied decimals down to 1
      append_digits(level.qty);                 // already 8 decimals
    };
    for (std::size_t i = 0; i < std::min<std::size_t>(10, book_.asks.size()); ++i) {
      add(book_.asks.level(i));
    }
    for (std::size_t i = 0; i < std::min<std::size_t>(10, book_.bids.size()); ++i) {
      add(book_.bids.level(i));
    }
    return crc32(0L, reinterpret_cast<const Bytef*>(text_.data()), static_cast<uInt>(text_.size()));
  }

  void append_digits(std::int64_t value) {
    std::array<char, 24> digits{};
    const auto written = std::to_chars(digits.data(), digits.data() + digits.size(), value);
    text_.append(digits.data(), written.ptr);
  }

  bool have_book_ = false;
  std::string text_;  // reused for each checksum
};

}  // namespace

std::unique_ptr<Feed> kraken() { return std::make_unique<Kraken>(); }

}  // namespace engine::feeds
