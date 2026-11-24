// Crypto.com (book.BTC_USD.50): a 50-level book, then changes every 100 ms. Mirrors
// CryptocomCheck in recorder/check.py: each change names the one before it (pu equals the
// previous u), and every heartbeat must have been answered by the recorder. Messages also carry
// a "cs" field. Its formula is not published, and none of 200 common CRC32 layouts matched it
// on the sample recordings (2026-11-24), so it is not checked.
#include <optional>
#include <string>

#include "feeds/feeds.hpp"

namespace engine::feeds {
namespace {

constexpr std::size_t kDepth = 50;

class Cryptocom final : public Feed {
 public:
  Cryptocom() : Feed(/*change_only=*/true) {}
  std::size_t depth_limit() const override { return kDepth; }

 private:
  void on_message(std::int64_t wall_ns, simdjson::ondemand::document& doc) override {
    if (find_string(doc, "method") == "public/heartbeat") {
      mutable_integrity().count("heartbeats_received");
      return;
    }
    auto found = find(doc, "result");
    if (!found) {
      return;  // the subscription reply has no result
    }
    simdjson::ondemand::object result = found->get_object();
    const std::string_view channel = result["channel"];
    int seen = 0;
    for (auto entry : result["data"]) {  // holds exactly one book or change
      if (seen++ == 0) {
        simdjson::ondemand::object data = entry;
        apply(wall_ns, channel == "book", data);
      }
    }
    if (seen != 1) {
      throw BadMessage("data does not hold exactly one entry");
    }
  }

  void apply(std::int64_t wall_ns, bool snapshot, simdjson::ondemand::object& data) {
    if (snapshot) {
      read_level_arrays(data["bids"], bids_scratch_);
      read_level_arrays(data["asks"], asks_scratch_);
      book_.bids.assign(bids_scratch_);
      book_.asks.assign(asks_scratch_);
      ready(wall_ns);
    } else {
      const std::int64_t previous = data["pu"].get_int64();
      if (!last_u_ || previous != *last_u_) {
        broken(wall_ns, "an update does not follow the previous one (pu)");
      }
      simdjson::ondemand::object update = data["update"];
      read_level_arrays(update["bids"], bids_scratch_);
      read_level_arrays(update["asks"], asks_scratch_);
      for (const Level& level : bids_scratch_) {
        book_.bids.set(level.price, level.qty);
      }
      for (const Level& level : asks_scratch_) {
        book_.asks.set(level.price, level.qty);
      }
      // The exchange removes levels that fall below the 50 it shows, so this never happens
      // in a correct book; it is counted, and the book is cut back to what the exchange shows.
      if (book_.bids.size() > kDepth || book_.asks.size() > kDepth) {
        mutable_integrity().count("books_beyond_50_levels");
        book_.bids.keep_best(kDepth);
        book_.asks.keep_best(kDepth);
      }
    }
    last_u_ = data["u"].get_int64();
    applied(wall_ns, data["t"].get_int64() * 1'000'000);  // t is in milliseconds
  }

  void on_event(std::int64_t /*wall_ns*/, std::string_view kind,
                simdjson::padded_string_view body) override {
    if (kind == "sent" &&
        std::string_view(body).find("respond-heartbeat") != std::string_view::npos) {
      mutable_integrity().count("heartbeats_answered");
    }
  }

  void on_finish() override {
    const auto& counts = integrity().counts;
    const auto received = counts.find("heartbeats_received");
    const auto answered = counts.find("heartbeats_answered");
    const std::int64_t r = received == counts.end() ? 0 : received->second;
    const std::int64_t a = answered == counts.end() ? 0 : answered->second;
    if (a < r) {
      mutable_integrity().problem(integrity().end_ns.value_or(0),
                                  std::to_string(r - a) + " heartbeat(s) not answered");
    }
  }

  std::optional<std::int64_t> last_u_;
};

}  // namespace

std::unique_ptr<Feed> cryptocom() { return std::make_unique<Cryptocom>(); }

}  // namespace engine::feeds
