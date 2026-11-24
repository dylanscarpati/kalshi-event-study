// The part of the engine that turns one exchange's messages into its order book.
#pragma once

#include <simdjson.h>

#include <array>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "book.hpp"
#include "fixed.hpp"
#include "tape.hpp"

namespace engine {

// The seven exchanges, in the order the engine writes them out.
inline constexpr std::array<std::string_view, 7> kFeedNames = {
    "coinbase", "kraken", "bitstamp", "gemini", "cryptocom", "bullish", "lmax"};

// What one file (one connection) showed. Mirrors FileResult in recorder/check.py, so the two
// programs can be compared on the same files.
struct Integrity {
  std::optional<std::int64_t> start_ns;  // first record
  std::optional<std::int64_t> end_ns;    // last record
  std::optional<std::int64_t> ready_ns;  // when the first full book arrived
  // Feeds that send changes after one full book: from this moment the rebuilt book is wrong
  // until the file ends, because only a new connection brings a new full book.
  std::optional<std::int64_t> invalid_from_ns;
  // Feeds whose every message is a full book: moments at which a message was missing. The
  // next message repairs the book, so only these moments are spoiled.
  std::vector<std::int64_t> break_ns;
  std::optional<std::int64_t> closed_ns;    // the first closing event; the book ends there
  std::string ended;                        // the last closing event's name, as in check.py
  std::vector<std::int64_t> clock_step_ns;  // moments the wall clock jumped
  std::map<std::string, std::int64_t, std::less<>> counts;
  std::vector<std::pair<std::int64_t, std::string>> problems;

  void count(std::string_view name, std::int64_t by = 1);
  void problem(std::int64_t wall_ns, std::string text) {
    problems.emplace_back(wall_ns, std::move(text));
  }
  bool unplanned() const;  // the connection ended by going stale, disconnecting or an error
};

// A message that does not have the shape its exchange documents.
class BadMessage : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

// One exchange's parser for one file. Each file is one connection and starts with an empty
// book, so the replay makes a new Feed for every file.
class Feed {
 public:
  virtual ~Feed() = default;
  Feed(const Feed&) = delete;
  Feed& operator=(const Feed&) = delete;
  Feed(Feed&&) = delete;
  Feed& operator=(Feed&&) = delete;

  // Takes one record, in file order. Mirrors the loop in check_file() in recorder/check.py.
  void handle(const Record& record);
  // Called once after the last record. `cut_off` says the file did not end properly and is not
  // the one the recorder may still be writing.
  void finish(bool cut_off);

  const Book& book() const { return book_; }
  const Integrity& integrity() const { return integrity_; }
  // True for feeds that send one full book and then changes; false for full-book feeds.
  bool change_only() const { return change_only_; }
  // The deepest the exchange shows each side (levels), or 0 if it shows the whole book.
  virtual std::size_t depth_limit() const { return 0; }
  // False while the exchange says its market is not open.
  virtual bool market_open() const { return true; }
  // When the last book message was received (wall clock) and stamped by the exchange; 0 if
  // none yet. Both in ns since 1970 UTC.
  std::int64_t last_book_wall_ns() const { return last_book_wall_ns_; }
  std::int64_t last_exchange_ns() const { return last_exchange_ns_; }

 protected:
  explicit Feed(bool change_only) : change_only_(change_only) {}

  // A message from the exchange, already parsed. Throws simdjson_error or BadMessage if the
  // message does not have the expected shape.
  virtual void on_message(std::int64_t wall_ns, simdjson::ondemand::document& doc) = 0;
  // A recorder event. `body` is the event's "raw" field, or empty.
  virtual void on_event(std::int64_t /*wall_ns*/, std::string_view /*kind*/,
                        simdjson::padded_string_view /*body*/) {}
  virtual void on_finish() {}

  // An integrity break, as broken() in check.py: it spoils the rest of the file for
  // change-only feeds, and only that moment for full-book feeds.
  void broken(std::int64_t wall_ns, std::string text);
  void ready(std::int64_t wall_ns);
  // Notes that a book message was applied, with the exchange's own time for it (0 if none).
  void applied(std::int64_t wall_ns, std::int64_t exchange_ns);
  Integrity& mutable_integrity() { return integrity_; }

  Book book_;
  simdjson::ondemand::parser parser_;  // used for messages and for events' bodies
  std::vector<Level> bids_scratch_;    // reused buffers for reading whole books
  std::vector<Level> asks_scratch_;

 private:
  Integrity integrity_;
  bool change_only_;
  std::int64_t last_book_wall_ns_ = 0;
  std::int64_t last_exchange_ns_ = 0;
  std::optional<std::int64_t> clock_offset_ns_;  // wall_ns - mono_ns of the previous record
};

// A parser for the named exchange (one of kFeedNames), or nullptr for an unknown name.
std::unique_ptr<Feed> make_feed(std::string_view name);

// ---------------------------------------------------------------------------------------------
// Helpers for the parsers. Each throws BadMessage or simdjson_error on unexpected input.
// ---------------------------------------------------------------------------------------------

// A field of a message, or nothing if the message has no such field. Any other error (a message
// that is not valid JSON) is thrown, so it counts as a break, as json.loads failing does in
// check.py. Messages are checked as far as they are read.
std::optional<simdjson::ondemand::value> find(simdjson::ondemand::document& doc,
                                              std::string_view key);
// The same for a field holding text.
std::optional<std::string_view> find_string(simdjson::ondemand::document& doc,
                                            std::string_view key);
// A decimal written as a JSON string ("57748.12") or a JSON number (57748.1), read exactly.
Fixed8 decimal(simdjson::ondemand::value value);
// A whole number written as a JSON string ("1795248341205038").
std::int64_t integer_in_string(simdjson::ondemand::value value);
// A UTC timestamp written as a JSON string ("2026-11-21T08:05:00.601Z"), in ns since 1970.
std::int64_t utc_ns(simdjson::ondemand::value value);
// Levels written as arrays whose first two values are price and size: [["57748.1","0.5"],...].
// Further values in each array (Crypto.com's order count) are ignored.
void read_level_arrays(simdjson::ondemand::array levels, std::vector<Level>& out);

}  // namespace engine
