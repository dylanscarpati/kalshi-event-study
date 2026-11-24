#include "replay.hpp"

#include <algorithm>

#include "utc_time.hpp"

namespace engine {
namespace {

constexpr std::int64_t kQuarterNs = 900 * kSecondNs;
constexpr std::int64_t kWindowNs = 60 * kSecondNs;

// The first whole second at or after t (t is positive).
std::int64_t ceil_to_second(std::int64_t t) {
  return ((t + kSecondNs - 1) / kSecondNs) * kSecondNs;
}

// True if the latest moment in `moments` (kept in time order) lies in (s - 1 s, s].
bool in_past_second(const std::vector<std::int64_t>& moments, std::int64_t s) {
  return !moments.empty() && moments.back() > s - kSecondNs && moments.back() <= s;
}

}  // namespace

std::string_view name_of(State state) {
  switch (state) {
    case State::ok:
      return "ok";
    case State::no_data:
      return "no_data";
    case State::not_ready:
      return "not_ready";
    case State::invalid:
      return "invalid";
    case State::closed:
      return "closed";
    case State::not_open:
      return "not_open";
  }
  return "unknown";
}

std::vector<std::filesystem::path> list_tapes(const std::filesystem::path& data_dir,
                                              std::string_view feed) {
  std::vector<std::filesystem::path> files;
  const std::filesystem::path dir = data_dir / feed;
  if (!std::filesystem::is_directory(dir)) {
    return files;
  }
  for (const auto& day : std::filesystem::directory_iterator(dir)) {
    if (!day.is_directory()) {
      continue;
    }
    for (const auto& entry : std::filesystem::directory_iterator(day.path())) {
      if (entry.is_regular_file() && entry.path().filename().string().ends_with(".jsonl.gz")) {
        files.push_back(entry.path());
      }
    }
  }
  // The date folder, then the start time in the name: oldest first, as check.py sorts them.
  std::sort(files.begin(), files.end());
  return files;
}

std::vector<std::int64_t> measured_windows(std::int64_t start_ns, std::int64_t end_ns) {
  std::vector<std::int64_t> windows;
  // The first quarter hour whose window starts at or after start_ns (rounded up).
  const std::int64_t first = ((start_ns + kWindowNs + kQuarterNs - 1) / kQuarterNs) * kQuarterNs;
  for (std::int64_t q = first; q <= end_ns; q += kQuarterNs) {
    windows.push_back(q);
  }
  return windows;
}

bool window_covered(std::int64_t q_ns, std::span<const FileSummary> files) {
  const std::int64_t start = q_ns - kWindowNs;
  for (const FileSummary& file : files) {
    const Integrity& in = file.integrity;
    if (!in.ready_ns || *in.ready_ns > start || !in.end_ns || *in.end_ns < q_ns) {
      continue;
    }
    if (in.invalid_from_ns && *in.invalid_from_ns <= q_ns) {
      continue;
    }
    if (std::any_of(in.break_ns.begin(), in.break_ns.end(),
                    [&](std::int64_t moment) { return start <= moment && moment <= q_ns; })) {
      continue;
    }
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------------------------
// One exchange's files, read in order
// ---------------------------------------------------------------------------------------------

class Replay::Cursor {
 public:
  Cursor(std::string name, std::vector<std::filesystem::path> files)
      : name_(std::move(name)), files_(std::move(files)) {}

  // The time of the next record not yet applied, or nothing if every file has been read.
  std::optional<std::int64_t> next_wall() {
    if (!fetch()) {
      return std::nullopt;
    }
    return pending_.wall_ns;
  }

  // Applies every record received at or before s, moving on to later files as needed.
  void advance_to(std::int64_t s) {
    while (fetch() && pending_.wall_ns <= s) {
      reading_->handle(pending_);
      reading_started_ = true;
      last_wall_ = std::max(last_wall_, pending_.wall_ns);
      has_pending_ = false;
    }
  }

  std::int64_t last_wall() const { return last_wall_; }

  Sample sample(std::int64_t s) const {
    Sample out;
    out.feed = name_;
    // The file whose records were applied last: the one being read once it has started,
    // otherwise the one before it.
    const Feed* feed = reading_started_ ? reading_.get() : finished_.get();
    if (feed == nullptr) {
      return out;  // no data yet
    }
    const Integrity& in = feed->integrity();
    out.book = &feed->book();
    out.depth_limit = feed->depth_limit();
    out.crossed = feed->book().crossed();
    out.gap = !feed->change_only() && in_past_second(in.break_ns, s);
    out.clock_step = in_past_second(in.clock_step_ns, s);
    out.last_wall_ns = feed->last_book_wall_ns();
    out.last_exchange_ns = feed->last_exchange_ns();

    const bool file_ended = feed == finished_.get();
    if (in.closed_ns && *in.closed_ns <= s) {
      out.state = State::closed;
    } else if (file_ended && in.end_ns && *in.end_ns < s) {
      out.state = State::no_data;  // the file stopped without a closing event
    } else if (!in.ready_ns) {
      out.state = State::not_ready;
    } else if (feed->change_only() && in.invalid_from_ns) {
      out.state = State::invalid;
    } else if (!feed->market_open()) {
      out.state = State::not_open;
    } else {
      out.state = State::ok;
    }
    return out;
  }

  const std::vector<FileSummary>& summaries() const { return summaries_; }

 private:
  // Makes sure a record is waiting in pending_, opening the next file when one ends. Returns
  // false when every file has been read.
  bool fetch() {
    while (!has_pending_) {
      if (reader_ && reader_->next(pending_)) {
        has_pending_ = true;
      } else if (reader_) {
        finish_file();
      } else if (!open_next_file()) {
        return false;
      }
    }
    return true;
  }

  bool open_next_file() {
    if (next_file_ == files_.size()) {
      return false;
    }
    reader_ = std::make_unique<TapeReader>(files_[next_file_]);
    reading_ = make_feed(name_);
    reading_started_ = false;
    ++next_file_;
    return true;
  }

  void finish_file() {
    // As check.py: only the newest file may lack its end, because the recorder may still be
    // writing it.
    const bool newest = next_file_ == files_.size();
    reading_->finish(!reader_->complete() && !newest);
    summaries_.push_back({name_, files_[next_file_ - 1].filename().string(), reading_->integrity(),
                          reader_->complete(), reader_->damaged()});
    // A file with no records (a crash before the recorder's first flush) changes nothing shown.
    if (reading_started_) {
      finished_ = std::move(reading_);
    }
    reading_.reset();
    reader_.reset();
    reading_started_ = false;
  }

  std::string name_;
  std::vector<std::filesystem::path> files_;
  std::size_t next_file_ = 0;
  std::unique_ptr<TapeReader> reader_;  // the file being read, if any
  std::unique_ptr<Feed> reading_;       // its feed
  bool reading_started_ = false;        // reading_ has applied at least one record
  std::unique_ptr<Feed> finished_;      // the last file read to its end
  Record pending_;                      // the next record, not yet applied
  bool has_pending_ = false;
  std::int64_t last_wall_ = 0;
  std::vector<FileSummary> summaries_;
};

// ---------------------------------------------------------------------------------------------
// All exchanges, second by second
// ---------------------------------------------------------------------------------------------

Replay::Replay(const std::filesystem::path& data_dir, const std::vector<std::string>& feeds) {
  for (const std::string& name : feeds) {
    cursors_.push_back(std::make_unique<Cursor>(name, list_tapes(data_dir, name)));
  }
}

Replay::~Replay() = default;

void Replay::run(const OnSecond& on_second, std::optional<std::int64_t> from_ns,
                 std::optional<std::int64_t> to_ns) {
  std::optional<std::int64_t> first;
  for (const auto& cursor : cursors_) {
    if (const auto wall = cursor->next_wall()) {
      first = first ? std::min(*first, *wall) : *wall;
    }
  }
  if (!first) {
    return;  // no tapes at all
  }

  // The books never affect each other, so each is brought up to second s on its own; no rule
  // is needed for messages of different exchanges received in the same nanosecond.
  std::vector<Sample> samples(cursors_.size());
  for (std::int64_t s = ceil_to_second(*first);; s += kSecondNs) {
    bool more = false;
    std::int64_t last = 0;
    for (const auto& cursor : cursors_) {
      cursor->advance_to(s);
      more = more || cursor->next_wall().has_value();
      last = std::max(last, cursor->last_wall());
    }
    if (!more && s > last) {
      break;  // every record has been applied and no later second has data
    }
    if ((from_ns && s < *from_ns) || (to_ns && s > *to_ns)) {
      continue;
    }
    for (std::size_t i = 0; i < cursors_.size(); ++i) {
      samples[i] = cursors_[i]->sample(s);
    }
    on_second(s, samples);
  }
}

std::vector<FileSummary> Replay::summaries() const {
  std::vector<FileSummary> all;
  for (const auto& cursor : cursors_) {
    all.insert(all.end(), cursor->summaries().begin(), cursor->summaries().end());
  }
  return all;
}

}  // namespace engine
