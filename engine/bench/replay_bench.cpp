// How fast the whole replay runs: reading the tapes, decompressing, parsing every message,
// rebuilding all seven books and sampling them each second.
//
// BM_ReplaySyntheticTapes replays the committed test tapes. BM_ReplayDataDir replays a real data
// folder when ENGINE_BENCH_DATA names one (for example a copy of the recordings on the MacBook);
// otherwise it is skipped.
#include <benchmark/benchmark.h>

#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <vector>

#include "feed.hpp"
#include "replay.hpp"

namespace engine {
namespace {

// Replays a folder once; returns the number of messages read.
std::int64_t replay_once(const std::filesystem::path& data_dir) {
  const std::vector<std::string> feeds(kFeedNames.begin(), kFeedNames.end());
  Replay replay(data_dir, feeds);
  std::int64_t seconds = 0;
  replay.run([&seconds](std::int64_t, std::span<const Sample> samples) {
    benchmark::DoNotOptimize(samples.data());
    ++seconds;
  });
  std::int64_t messages = 0;
  for (const FileSummary& file : replay.summaries()) {
    const auto found = file.integrity.counts.find("messages");
    messages += found == file.integrity.counts.end() ? 0 : found->second;
  }
  return messages;
}

void run(benchmark::State& state, const std::filesystem::path& data_dir) {
  std::int64_t messages = 0;
  for (auto _ : state) {
    messages += replay_once(data_dir);
  }
  state.counters["messages_per_second"] =
      benchmark::Counter(static_cast<double>(messages), benchmark::Counter::kIsRate);
}

void BM_ReplaySyntheticTapes(benchmark::State& state) { run(state, ENGINE_TEST_TAPES); }

void BM_ReplayDataDir(benchmark::State& state) {
  const char* dir = std::getenv("ENGINE_BENCH_DATA");
  if (dir == nullptr) {
    state.SkipWithMessage("set ENGINE_BENCH_DATA to a data folder to run this");
    return;
  }
  run(state, dir);
}

BENCHMARK(BM_ReplaySyntheticTapes)->Unit(benchmark::kMillisecond);
BENCHMARK(BM_ReplayDataDir)->Unit(benchmark::kMillisecond)->Iterations(1);

}  // namespace
}  // namespace engine
