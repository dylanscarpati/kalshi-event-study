// Bitstamp (diff_order_book_btcusd, plus a full book downloaded each minute). The feed sends net
// changes about three times a second, carries no sequence numbers or checksums, and sends no full
// book itself: the recorder downloads one and writes it as a rest_snapshot event. Mirrors
// BitstampCheck in recorder/check.py (changes must arrive in time order) and adds the splice and
// repairs of decision 4 in notes/PROJECT_LOG.md:
//
// 1. Splice. A downloaded book is a little older than the moment it is written down, so the
//    changes received since its own time are applied to it again. Recent changes are kept for
//    that.
// 2. Crossing repair. Orders that appear and vanish within one netting interval can sit in a
//    downloaded book although no change ever reports them ("phantoms"). When a change makes the
//    book cross, the crossing level this change did not set is such a phantom, and is removed.
// 3. Deletion-only resync. Phantoms come only from downloads, so the levels a download brought in
//    stay "unconfirmed" until a change sets them. At each later download, an unconfirmed level
//    the new download does not have is removed. A level a change has set is never removed this
//    way: an order cancelled and re-placed within one netting interval can be missing from a
//    download although it is real. Levels only in a download are never added: they are the
//    likeliest phantoms of all. Every difference is counted, as Bitstamp's integrity evidence.
#include <algorithm>
#include <deque>
#include <map>
#include <optional>
#include <set>
#include <string>

#include "feeds/feeds.hpp"

namespace engine::feeds {
namespace {

// How long changes are kept for splicing, in microseconds (Bitstamp's own clock). Downloads
// were 0 to 0.7 s old when written down in the sample recordings.
constexpr std::int64_t kKeepUs = 10'000'000;

struct Change {
  std::int64_t micro = 0;  // the exchange's time for this change, in microseconds
  std::vector<Level> bids;
  std::vector<Level> asks;
};

struct Download {
  std::int64_t micro = 0;
  std::vector<Level> bids;
  std::vector<Level> asks;
};

bool sets_price(const std::vector<Level>& levels, Fixed8 price) {
  return std::any_of(levels.begin(), levels.end(),
                     [price](const Level& level) { return level.price == price; });
}

class Bitstamp final : public Feed {
 public:
  Bitstamp() : Feed(/*change_only=*/true) {}

 private:
  void on_message(std::int64_t wall_ns, simdjson::ondemand::document& doc) override {
    if (find_string(doc, "event") != "data") {
      return;  // the subscription reply, or a request to reconnect
    }
    simdjson::ondemand::object data = doc["data"];
    Change change;
    change.micro = integer_in_string(data["microtimestamp"]);
    read_level_arrays(data["bids"], change.bids);
    read_level_arrays(data["asks"], change.asks);

    if (last_micro_ && change.micro <= *last_micro_) {
      broken(wall_ns, "changes arrived out of time order");
    }
    last_micro_ = change.micro;
    mutable_integrity().count("changes");
    if (!first_micro_) {
      first_micro_ = change.micro;
    }
    if (!have_book_) {  // remembered until the first download is spliced
      for (const Level& level : change.bids) {
        last_bids_[level.price] = level.qty;
      }
      for (const Level& level : change.asks) {
        last_asks_[level.price] = level.qty;
      }
    }

    if (have_book_) {
      apply(change);
      applied(wall_ns, checked_mul(change.micro, 1'000));
    }
    changes_.push_back(std::move(change));
    while (changes_.front().micro < changes_.back().micro - kKeepUs) {
      changes_.pop_front();
    }
  }

  void on_event(std::int64_t wall_ns, std::string_view kind,
                simdjson::padded_string_view body) override {
    if (kind == "rest_snapshot_failed") {
      mutable_integrity().count("book_download_failures");
      return;
    }
    if (kind != "rest_snapshot") {
      return;
    }
    mutable_integrity().count("book_downloads");
    Download download;
    try {
      simdjson::ondemand::document doc = parser_.iterate(body);
      download.micro = integer_in_string(doc["microtimestamp"]);
      read_level_arrays(doc["bids"], download.bids);
      read_level_arrays(doc["asks"], download.asks);
    } catch (const std::exception&) {  // simdjson_error or BadMessage: the download is not used
      mutable_integrity().count("book_downloads_unreadable");
      return;
    }
    if (!usable(download)) {
      mutable_integrity().count("book_downloads_unusable");
      return;
    }
    if (have_book_) {
      resync(download);
    } else {
      splice(wall_ns, download);
    }
  }

  // A download can be brought up to date only if every change after its time was received:
  // the connection's first change is not newer than the download, and the changes since the
  // download are still kept.
  bool usable(const Download& download) const {
    return first_micro_ && *first_micro_ <= download.micro && !changes_.empty() &&
           download.micro >= changes_.back().micro - kKeepUs;
  }

  void splice(std::int64_t wall_ns, const Download& download) {
    book_.bids.assign(download.bids);
    book_.asks.assign(download.asks);
    unconfirmed_bids_ = unconfirmed_of(book_.bids, last_bids_);
    unconfirmed_asks_ = unconfirmed_of(book_.asks, last_asks_);
    for (const Change& change : changes_) {
      if (change.micro > download.micro) {
        apply(change);
      }
    }
    have_book_ = true;
    last_bids_.clear();
    last_asks_.clear();
    ready(wall_ns);
    applied(wall_ns, checked_mul(changes_.back().micro, 1'000));
  }

  // The downloaded levels that no change has confirmed: those whose latest change, if any, did
  // not set a size.
  static std::set<Fixed8> unconfirmed_of(const Side& side, const std::map<Fixed8, Fixed8>& last) {
    std::set<Fixed8> prices;
    for (std::size_t i = 0; i < side.size(); ++i) {
      const Fixed8 price = side.level(i).price;
      const auto found = last.find(price);
      if (found == last.end() || found->second == 0) {
        prices.insert(price);
      }
    }
    return prices;
  }

  void apply(const Change& change) {
    for (const Level& level : change.bids) {
      book_.bids.set(level.price, level.qty);
      unconfirmed_bids_.erase(level.price);  // a change has now set it
    }
    for (const Level& level : change.asks) {
      book_.asks.set(level.price, level.qty);
      unconfirmed_asks_.erase(level.price);
    }
    repair_crossing(change);
  }

  void repair_crossing(const Change& change) {
    while (book_.crossed()) {
      const Fixed8 bid = book_.bids.level(0).price;
      const Fixed8 ask = book_.asks.level(0).price;
      const bool set_bid = sets_price(change.bids, bid);
      const bool set_ask = sets_price(change.asks, ask);
      if (set_bid && !set_ask) {
        book_.asks.set(ask, 0);
        unconfirmed_asks_.erase(ask);
      } else if (set_ask && !set_bid) {
        book_.bids.set(bid, 0);
        unconfirmed_bids_.erase(bid);
      } else {  // this change set both crossing levels, or neither: left for the record
        mutable_integrity().count("crossings_not_repaired");
        return;
      }
      mutable_integrity().count("crossing_levels_removed");
    }
  }

  void resync(const Download& download) {
    mutable_integrity().count("resyncs");
    resync_side(book_.bids, unconfirmed_bids_, download.bids, &Change::bids, download.micro);
    resync_side(book_.asks, unconfirmed_asks_, download.asks, &Change::asks, download.micro);
  }

  void resync_side(Side& side, std::set<Fixed8>& unconfirmed, std::vector<Level> downloaded,
                   std::vector<Level> Change::*levels_of, std::int64_t since) {
    std::erase_if(downloaded, [](const Level& level) { return level.qty == 0; });
    std::sort(downloaded.begin(), downloaded.end(),
              [](const Level& a, const Level& b) { return a.price < b.price; });
    const auto downloaded_qty = [&downloaded](Fixed8 price) -> std::optional<Fixed8> {
      const auto at =
          std::lower_bound(downloaded.begin(), downloaded.end(), price,
                           [](const Level& level, Fixed8 p) { return level.price < p; });
      if (at != downloaded.end() && at->price == price) {
        return at->qty;
      }
      return std::nullopt;
    };

    // Phantoms: unconfirmed levels the new download does not have either.
    std::vector<Fixed8> phantoms;
    for (const Fixed8 price : unconfirmed) {
      if (!downloaded_qty(price)) {
        phantoms.push_back(price);
      }
    }
    for (const Fixed8 price : phantoms) {
      side.set(price, 0);
      unconfirmed.erase(price);
    }
    mutable_integrity().count("phantom_levels_removed", static_cast<std::int64_t>(phantoms.size()));

    // Differences with the download are counted, never applied. Prices a change has set since
    // the download's time are skipped: there the book is newer than the download.
    std::vector<Fixed8> touched;
    for (const Change& change : changes_) {
      if (change.micro > since) {
        for (const Level& level : change.*levels_of) {
          touched.push_back(level.price);
        }
      }
    }
    std::sort(touched.begin(), touched.end());
    for (const Level& level : downloaded) {
      if (std::binary_search(touched.begin(), touched.end(), level.price)) {
        continue;
      }
      const auto qty = side.qty_at(level.price);
      if (!qty) {
        mutable_integrity().count("download_only_levels");
      } else if (*qty != level.qty) {
        mutable_integrity().count("size_differences");
      }
    }
  }

  std::deque<Change> changes_;  // recent changes, oldest first
  std::optional<std::int64_t> first_micro_;
  std::optional<std::int64_t> last_micro_;
  bool have_book_ = false;
  // Prices that came from a download and that no change has set since.
  std::set<Fixed8> unconfirmed_bids_;
  std::set<Fixed8> unconfirmed_asks_;
  // Before the first splice: the latest size each change set at each price.
  std::map<Fixed8, Fixed8> last_bids_;
  std::map<Fixed8, Fixed8> last_asks_;
};

}  // namespace

std::unique_ptr<Feed> bitstamp() { return std::make_unique<Bitstamp>(); }

}  // namespace engine::feeds
