#include "feed.hpp"

#include <charconv>
#include <cstdlib>

#include "feeds/feeds.hpp"
#include "utc_time.hpp"

namespace engine {
namespace {

// A jump of the wall clock, relative to the monotonic clock, larger than this between two
// records is a clock step (a correction), not the slow drift a clock always has.
constexpr std::int64_t kClockStepNs = 50'000'000;

// How a connection's file can end; the names are the recorder's event names.
bool is_closing(std::string_view kind) {
  return kind == "planned_reconnect" || kind == "requested_reconnect" || kind == "stopped" ||
         kind == "stale" || kind == "disconnected" || kind == "error";
}

}  // namespace

void Integrity::count(std::string_view name, std::int64_t by) {
  const auto found = counts.find(name);
  if (found == counts.end()) {
    counts.emplace(std::string(name), by);
  } else {
    found->second += by;
  }
}

bool Integrity::unplanned() const {
  return ended == "stale" || ended == "disconnected" || ended == "error";
}

void Feed::handle(const Record& record) {
  const std::int64_t wall_ns = record.wall_ns;
  if (!integrity_.start_ns) {
    integrity_.start_ns = wall_ns;
  }
  integrity_.end_ns = wall_ns;

  const std::int64_t offset = record.wall_ns - record.mono_ns;
  if (clock_offset_ns_ && std::abs(offset - *clock_offset_ns_) > kClockStepNs) {
    integrity_.clock_step_ns.push_back(wall_ns);
    integrity_.count("clock_steps");
  }
  clock_offset_ns_ = offset;

  if (record.kind == Record::Kind::event) {
    if (is_closing(record.event)) {
      integrity_.ended = std::string(record.event);
      if (!integrity_.closed_ns) {
        integrity_.closed_ns = wall_ns;
      }
    }
    try {
      on_event(wall_ns, record.event, record.body);
    } catch (const std::exception& error) {  // simdjson_error or BadMessage
      integrity_.problem(wall_ns, std::string("an event could not be read (") + error.what() + ")");
    }
    return;
  }

  integrity_.count("messages");
  if (record.binary) {
    broken(wall_ns, "a message is not valid text");
    return;
  }
  try {
    simdjson::ondemand::document doc = parser_.iterate(record.body);
    on_message(wall_ns, doc);
  } catch (const simdjson::simdjson_error& error) {
    broken(wall_ns, std::string("a message could not be read (") + error.what() + ")");
  } catch (const BadMessage& error) {
    broken(wall_ns, std::string("a message could not be read (") + error.what() + ")");
  }
}

void Feed::finish(bool cut_off) {
  on_finish();
  if (cut_off) {
    integrity_.problem(integrity_.end_ns.value_or(0),
                       "file cut off (the recorder was stopped abruptly)");
  }
}

void Feed::broken(std::int64_t wall_ns, std::string text) {
  integrity_.problem(wall_ns, std::move(text));
  if (!change_only_) {
    integrity_.break_ns.push_back(wall_ns);
  } else if (!integrity_.invalid_from_ns) {
    integrity_.invalid_from_ns = wall_ns;
  }
}

void Feed::ready(std::int64_t wall_ns) {
  if (!integrity_.ready_ns) {
    integrity_.ready_ns = wall_ns;
  }
}

void Feed::applied(std::int64_t wall_ns, std::int64_t exchange_ns) {
  last_book_wall_ns_ = wall_ns;
  if (exchange_ns != 0) {
    last_exchange_ns_ = exchange_ns;
  }
}

std::unique_ptr<Feed> make_feed(std::string_view name) {
  if (name == "coinbase") {
    return feeds::coinbase();
  }
  if (name == "kraken") {
    return feeds::kraken();
  }
  if (name == "bitstamp") {
    return feeds::bitstamp();
  }
  if (name == "gemini") {
    return feeds::gemini();
  }
  if (name == "cryptocom") {
    return feeds::cryptocom();
  }
  if (name == "bullish") {
    return feeds::bullish();
  }
  if (name == "lmax") {
    return feeds::lmax();
  }
  return nullptr;
}

// ---------------------------------------------------------------------------------------------
// Helpers for the parsers
// ---------------------------------------------------------------------------------------------

std::optional<simdjson::ondemand::value> find(simdjson::ondemand::document& doc,
                                              std::string_view key) {
  simdjson::ondemand::value value;
  const simdjson::error_code error = doc[key].get(value);
  if (error == simdjson::NO_SUCH_FIELD) {
    return std::nullopt;
  }
  if (error != simdjson::SUCCESS) {
    throw simdjson::simdjson_error(error);
  }
  return value;
}

std::optional<std::string_view> find_string(simdjson::ondemand::document& doc,
                                            std::string_view key) {
  auto value = find(doc, key);
  if (!value) {
    return std::nullopt;
  }
  return std::string_view(value->get_string());
}

Fixed8 decimal(simdjson::ondemand::value value) {
  std::string_view text;
  const simdjson::ondemand::json_type type = value.type();
  if (type == simdjson::ondemand::json_type::string) {
    text = value.get_string();
  } else if (type == simdjson::ondemand::json_type::number) {
    // The number exactly as written (it may be followed by spaces), never via a double.
    text = value.raw_json_token();
    while (!text.empty() && (text.back() == ' ' || text.back() == '\t' || text.back() == '\n' ||
                             text.back() == '\r')) {
      text.remove_suffix(1);
    }
  } else {
    throw BadMessage("a price or size is neither a string nor a number");
  }
  const auto parsed = parse_fixed8(text);
  if (!parsed) {
    throw BadMessage("not a plain decimal: " + std::string(text));
  }
  return *parsed;
}

std::int64_t integer_in_string(simdjson::ondemand::value value) {
  const std::string_view text = value.get_string();
  std::int64_t result = 0;
  const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), result);
  if (error != std::errc{} || end != text.data() + text.size() || text.empty() ||
      text.front() == '-') {
    throw BadMessage("not a whole number: " + std::string(text));
  }
  return result;
}

std::int64_t utc_ns(simdjson::ondemand::value value) {
  const std::string_view text = value.get_string();
  const auto parsed = parse_utc_ns(text);
  if (!parsed) {
    throw BadMessage("not a UTC timestamp: " + std::string(text));
  }
  return *parsed;
}

void read_level_arrays(simdjson::ondemand::array levels, std::vector<Level>& out) {
  out.clear();
  for (auto entry : levels) {
    Level level;
    int index = 0;
    for (auto element : entry.get_array()) {
      if (index == 0) {
        level.price = decimal(element.value());
      } else if (index == 1) {
        level.qty = decimal(element.value());
      }
      ++index;  // later values (Crypto.com's order count) are not needed
    }
    if (index < 2) {
      throw BadMessage("a level has fewer than two values");
    }
    out.push_back(level);
  }
}

}  // namespace engine
