// Replays recorded tapes: writes every exchange's book at each second, and reports what each
// file showed in the same terms as recorder/check.py.
//
//   replay <data-dir> [--feed NAME]... [--from UTC] [--to UTC] [--csv PATH] [--files PATH]
//
//   <data-dir>    the recorder's data folder, one folder per exchange
//   --feed NAME   only these exchanges, in this order (default: all seven)
//   --from --to   report only the seconds in this range, e.g. 2026-11-21T08:00:00Z
//   --csv PATH    write one row per second per exchange (columns in engine/src/csv.hpp)
//   --files PATH  write what each file showed, one JSON object per line
//
// Exit code 0 means no problem was found; 1 means at least one is listed; 2 means bad usage.
#include "replay.hpp"

#include <array>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "csv.hpp"
#include "utc_time.hpp"

namespace {

using engine::FileSummary;

struct Options {
  std::string data_dir;
  std::vector<std::string> feeds;
  std::optional<std::int64_t> from_ns;
  std::optional<std::int64_t> to_ns;
  std::string csv_path;
  std::string files_path;
};

std::optional<Options> parse_options(int argc, char** argv) {
  Options options;
  for (int i = 1; i < argc; ++i) {
    const std::string_view arg = argv[i];
    const bool has_value = i + 1 < argc;
    if (arg == "--feed" && has_value) {
      options.feeds.emplace_back(argv[++i]);
    } else if ((arg == "--from" || arg == "--to") && has_value) {
      const auto ns = engine::parse_utc_ns(argv[++i]);
      if (!ns) {
        std::cerr << "not a UTC time such as 2026-11-21T08:00:00Z: " << argv[i] << '\n';
        return std::nullopt;
      }
      (arg == "--from" ? options.from_ns : options.to_ns) = ns;
    } else if (arg == "--csv" && has_value) {
      options.csv_path = argv[++i];
    } else if (arg == "--files" && has_value) {
      options.files_path = argv[++i];
    } else if (!arg.starts_with("--") && options.data_dir.empty()) {
      options.data_dir = arg;
    } else {
      return std::nullopt;
    }
  }
  if (options.data_dir.empty()) {
    return std::nullopt;
  }
  if (options.feeds.empty()) {
    options.feeds.assign(engine::kFeedNames.begin(), engine::kFeedNames.end());
  }
  for (const std::string& feed : options.feeds) {
    if (!engine::make_feed(feed)) {
      std::cerr << "unknown exchange: " << feed << '\n';
      return std::nullopt;
    }
  }
  return options;
}

// 1234567 as "1,234,567", as Python's {:,} writes it.
std::string with_commas(std::int64_t value) {
  std::string digits = std::to_string(value);
  for (auto at = static_cast<std::ptrdiff_t>(digits.size()) - 3; at > 0; at -= 3) {
    digits.insert(static_cast<std::size_t>(at), ",");
  }
  return digits;
}

std::string json_string(std::string_view text) {
  std::string out = "\"";
  for (const char c : text) {
    if (c == '"' || c == '\\') {
      out += '\\';
      out += c;
    } else if (static_cast<unsigned char>(c) < 0x20) {
      std::array<char, 8> code{};
      std::snprintf(code.data(), code.size(), "\\u%04x", static_cast<unsigned>(c));
      out += code.data();
    } else {
      out += c;
    }
  }
  return out + '"';
}

std::string json_number(const std::optional<std::int64_t>& value) {
  return value ? std::to_string(*value) : "null";
}

std::string file_json(const FileSummary& file) {
  const engine::Integrity& in = file.integrity;
  std::string out = R"({"feed":)" + json_string(file.feed) + R"(,"file":)" + json_string(file.file);
  out += R"(,"start_ns":)" + json_number(in.start_ns) + R"(,"end_ns":)" + json_number(in.end_ns);
  out += R"(,"ready_ns":)" + json_number(in.ready_ns);
  out += R"(,"invalid_from_ns":)" + json_number(in.invalid_from_ns) + R"(,"break_ns":[)";
  for (std::size_t i = 0; i < in.break_ns.size(); ++i) {
    out += (i > 0 ? "," : "") + std::to_string(in.break_ns[i]);
  }
  out += R"(],"closed_ns":)" + json_number(in.closed_ns) + R"(,"ended":)" + json_string(in.ended);
  out += R"(,"unplanned":)" + std::string(in.unplanned() ? "true" : "false");
  out += R"(,"complete":)" + std::string(file.complete ? "true" : "false");
  out += R"(,"damaged":)" + std::string(file.damaged ? "true" : "false") + R"(,"counts":{)";
  bool first = true;
  for (const auto& [name, count] : in.counts) {
    out += (first ? "" : ",") + json_string(name) + ":" + std::to_string(count);
    first = false;
  }
  out += R"(},"problems":[)";
  for (std::size_t i = 0; i < in.problems.size(); ++i) {
    out += (i > 0 ? "," : "") + std::string("[") + std::to_string(in.problems[i].first) + "," +
           json_string(in.problems[i].second) + "]";
  }
  return out + "]}";
}

// The table check.py prints, from the same per-file results.
int print_summary(const Options& options, const std::vector<FileSummary>& files) {
  std::cout << "Replay of " << options.data_dir << '\n'
            << "feed       files     messages unplanned  windows covered\n";
  std::vector<std::string> problems;
  std::int64_t problem_count = 0;
  for (const std::string& feed : options.feeds) {
    int shown = 0;
    std::vector<FileSummary> mine;
    for (const FileSummary& file : files) {
      if (file.feed == feed) {
        mine.push_back(file);
      }
    }
    std::array<char, 128> row{};
    if (mine.empty()) {
      std::snprintf(row.data(), row.size(), "%-10s %5d %12d %9d  no files\n", feed.c_str(), 0, 0,
                    0);
      std::cout << row.data();
      continue;
    }
    std::int64_t start = 0;
    std::int64_t end = 0;
    std::int64_t unplanned = 0;
    std::map<std::string, std::int64_t> totals;
    bool have_range = false;
    for (const FileSummary& file : mine) {
      const engine::Integrity& in = file.integrity;
      if (in.start_ns && in.end_ns) {
        start = have_range ? std::min(start, *in.start_ns) : *in.start_ns;
        end = have_range ? std::max(end, *in.end_ns) : *in.end_ns;
        have_range = true;
      }
      unplanned += in.unplanned() ? 1 : 0;
      for (const auto& [name, count] : in.counts) {
        totals[name] += count;
      }
      for (const auto& [wall, text] : in.problems) {
        ++problem_count;
        if (++shown <= 10) {  // as check.py: at most ten for each exchange
          std::string line = feed;
          line += "  ";
          line += std::to_string(wall);
          line += "  ";
          line += text;
          line += "  [";
          line += file.file;
          line += ']';
          problems.push_back(std::move(line));
        } else if (shown == 11) {
          problems.push_back(feed + "  ... more not shown");
        }
      }
    }
    const auto windows =
        have_range ? engine::measured_windows(start, end) : std::vector<std::int64_t>{};
    std::int64_t covered = 0;
    for (const std::int64_t q : windows) {
      covered += engine::window_covered(q, mine) ? 1 : 0;
    }
    std::string share = std::to_string(covered) + "/" + std::to_string(windows.size());
    if (!windows.empty()) {
      std::array<char, 32> percent{};
      std::snprintf(percent.data(), percent.size(), " (%.1f%%)",
                    100.0 * static_cast<double>(covered) / static_cast<double>(windows.size()));
      share += percent.data();
    }
    std::string evidence;
    for (const auto& [name, count] : totals) {
      if (name != "messages") {
        evidence += (evidence.empty() ? "" : ", ") + with_commas(count) + " " + name;
      }
    }
    std::snprintf(row.data(), row.size(), "%-10s %5zu %12s %9lld  ", feed.c_str(), mine.size(),
                  with_commas(totals["messages"]).c_str(), static_cast<long long>(unplanned));
    std::cout << row.data() << share << (evidence.empty() ? "" : "  (" + evidence + ")") << '\n';
  }
  if (problem_count == 0) {
    std::cout << "\nNo problems found.\n";
    return 0;
  }
  std::cout << '\n' << problem_count << " problem(s):\n";
  for (const std::string& line : problems) {
    std::cout << "  " << line << '\n';
  }
  return 1;
}

}  // namespace

int main(int argc, char** argv) {
  const auto options = parse_options(argc, argv);
  if (!options) {
    std::cerr << "usage: replay <data-dir> [--feed NAME]... [--from UTC] [--to UTC]"
                 " [--csv PATH] [--files PATH]\n";
    return 2;
  }

  std::ofstream csv;
  if (!options->csv_path.empty()) {
    csv.open(options->csv_path, std::ios::binary | std::ios::trunc);
    csv << engine::kCsvHeader;
  }
  engine::Replay replay(options->data_dir, options->feeds);
  std::string rows;
  replay.run(
      [&](std::int64_t second_ns, std::span<const engine::Sample> samples) {
        if (csv.is_open()) {
          rows.clear();
          engine::append_csv_rows(rows, second_ns, samples);
          csv << rows;
        }
      },
      options->from_ns, options->to_ns);

  const std::vector<FileSummary> files = replay.summaries();
  if (!options->files_path.empty()) {
    std::ofstream out(options->files_path, std::ios::binary | std::ios::trunc);
    for (const FileSummary& file : files) {
      out << file_json(file) << '\n';
    }
  }
  return print_summary(*options, files);
}
