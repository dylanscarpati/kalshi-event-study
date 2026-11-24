// Replays the recorded tapes and reports every exchange's book at each whole second.
#pragma once

#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "feed.hpp"
#include "tape.hpp"

namespace engine {

// Whether a book can be used at a given second, and if not, why.
enum class State : std::uint8_t {
  ok,         // a current book
  no_data,    // no file covers this second
  not_ready,  // connected, but no full book has arrived yet
  invalid,    // a missed message spoiled the book (feeds that send changes), until the file ends
  closed,     // the connection was closed, and the next one has no data yet
  not_open,   // the exchange says its market is not open
};
std::string_view name_of(State state);

// One exchange's book at one second: the state after every message received up to and
// including that second (wall clock).
struct Sample {
  std::string_view feed;
  State state = State::no_data;
  const Book* book = nullptr;     // the latest book, also when it cannot be used; null if none
  std::size_t depth_limit = 0;    // the most levels a side the exchange shows; 0 for the whole book
  bool crossed = false;           // best bid at or above best ask
  bool gap = false;               // full-book feeds: a message was missed in the past second
  bool clock_step = false;        // the wall clock jumped in the past second
  std::int64_t last_wall_ns = 0;  // when the latest book message was received; 0 if none
  std::int64_t last_exchange_ns = 0;  // the exchange's own time for it; 0 if none
};

// What one file showed, once it has been read to its end.
struct FileSummary {
  std::string feed;
  std::string file;  // the file's name
  Integrity integrity;
  bool complete = false;  // the file ended properly
  bool damaged = false;   // unreadable bytes were found
};

// One exchange's tapes, oldest first: <data_dir>/<feed>/<UTC date>/<feed>_<start>.jsonl.gz.
std::vector<std::filesystem::path> list_tapes(const std::filesystem::path& data_dir,
                                              std::string_view feed);

// The measured windows of recorder/check.py: the quarter hours q whose 60 seconds [q - 60 s, q]
// lie within [start_ns, end_ns].
std::vector<std::int64_t> measured_windows(std::int64_t start_ns, std::int64_t end_ns);
// True if one of an exchange's files holds a valid book from q - 60 s to q with no break, as
// window_covered() in recorder/check.py.
bool window_covered(std::int64_t q_ns, std::span<const FileSummary> files);

class Replay {
 public:
  using OnSecond = std::function<void(std::int64_t second_ns, std::span<const Sample> samples)>;

  // `feeds` are names from kFeedNames, in the order samples are given.
  Replay(const std::filesystem::path& data_dir, const std::vector<std::string>& feeds);
  ~Replay();
  Replay(const Replay&) = delete;
  Replay& operator=(const Replay&) = delete;
  Replay(Replay&&) = delete;
  Replay& operator=(Replay&&) = delete;

  // Reads every tape and calls on_second for each whole second from the first record of any
  // tape to the last, in order, with one sample per feed. Only seconds within [from_ns, to_ns]
  // are reported, but every file is read in full, so the summaries are complete.
  void run(const OnSecond& on_second, std::optional<std::int64_t> from_ns = std::nullopt,
           std::optional<std::int64_t> to_ns = std::nullopt);

  // After run(): one summary per file, by feed in the order given, then oldest first.
  std::vector<FileSummary> summaries() const;

 private:
  class Cursor;
  std::vector<std::unique_ptr<Cursor>> cursors_;
};

}  // namespace engine
