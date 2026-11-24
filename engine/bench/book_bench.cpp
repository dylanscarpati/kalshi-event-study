// How fast one side of a book takes changes: the engine's sorted vector (best price at the back)
// against std::map, on the same stream of changes.
//
// The side and the changes are made up, shaped like Coinbase's book: about 25,000 levels, and
// changes whose distance from the best level follows a log-normal curve with a median of 54
// levels and a 99th percentile near 3,900 (what the sample recordings showed). Changes are half
// new sizes for existing levels, a quarter removals and a quarter new levels.
#include <benchmark/benchmark.h>

#include <cmath>
#include <cstdint>
#include <map>
#include <numbers>
#include <random>
#include <vector>

#include "book.hpp"

namespace engine {
namespace {

constexpr std::size_t kLevels = 25'000;
constexpr std::size_t kChanges = 100'000;
constexpr Fixed8 kTick = 1'000'000;  // 0.01

struct Change {
  Fixed8 price;
  Fixed8 qty;  // zero removes the level
};

// A uniform number in (0, 1) from the generator's raw bits, the same on every platform.
double uniform(std::mt19937_64& bits) {
  return (static_cast<double>(bits() >> 11) + 0.5) * 0x1.0p-53;
}

// The starting asks (lowest first), two ticks apart so new levels can go in between, and the
// changes, worked out once against a std::map so that both containers replay the same ones.
struct Workload {
  std::vector<Level> start;
  std::vector<Change> changes;
};

const Workload& workload() {
  static const Workload made = [] {
    Workload w;
    std::map<Fixed8, Fixed8> asks;
    const Fixed8 best = 5'770'000'000'000;  // 57700.00
    for (std::size_t i = 0; i < kLevels; ++i) {
      const Fixed8 price = best + (static_cast<Fixed8>(2 * i) * kTick);
      asks[price] = 100'000;
      w.start.push_back({price, 100'000});
    }
    std::mt19937_64 bits(20261124);
    const double mu = std::log(54.0);
    const double sigma = std::log(3'881.0 / 54.0) / 2.326;  // the 99th percentile of a normal
    for (std::size_t n = 0; n < kChanges; ++n) {
      // Box-Muller: a standard normal number from two uniform ones.
      const double normal = std::sqrt(-2.0 * std::log(uniform(bits))) *
                            std::cos(2.0 * std::numbers::pi * uniform(bits));
      auto depth = static_cast<std::size_t>(std::exp(mu + (sigma * normal)));
      depth = std::min(depth, asks.size() - 2);
      auto level = std::next(asks.begin(), static_cast<std::ptrdiff_t>(depth));
      const double kind = uniform(bits);
      const Fixed8 qty = 1 + static_cast<Fixed8>(uniform(bits) * 1e6);
      if (kind < 0.5) {
        w.changes.push_back({level->first, qty});
        level->second = qty;
      } else if (kind < 0.75 && asks.size() > kLevels / 2) {
        w.changes.push_back({level->first, 0});
        asks.erase(level);
      } else {
        const Fixed8 between = level->first + kTick;  // a free price, or an existing level
        w.changes.push_back({between, qty});
        asks[between] = qty;
      }
    }
    return w;
  }();
  return made;
}

void BM_SortedVector(benchmark::State& state) {
  const Workload& w = workload();
  for (auto _ : state) {
    state.PauseTiming();
    Side asks(Side::Kind::asks);
    asks.assign(w.start);
    state.ResumeTiming();
    for (const Change& change : w.changes) {
      asks.set(change.price, change.qty);
    }
    Level best = asks.level(0);
    benchmark::DoNotOptimize(best);
  }
  state.SetItemsProcessed(state.iterations() * static_cast<std::int64_t>(w.changes.size()));
}

void BM_StdMap(benchmark::State& state) {
  const Workload& w = workload();
  for (auto _ : state) {
    state.PauseTiming();
    std::map<Fixed8, Fixed8> asks;
    for (const Level& level : w.start) {
      asks.emplace(level.price, level.qty);
    }
    state.ResumeTiming();
    for (const Change& change : w.changes) {
      if (change.qty == 0) {
        asks.erase(change.price);
      } else {
        asks[change.price] = change.qty;
      }
    }
    Fixed8 best = asks.begin()->second;
    benchmark::DoNotOptimize(best);
  }
  state.SetItemsProcessed(state.iterations() * static_cast<std::int64_t>(w.changes.size()));
}

BENCHMARK(BM_SortedVector)->Unit(benchmark::kMillisecond);
BENCHMARK(BM_StdMap)->Unit(benchmark::kMillisecond);

}  // namespace
}  // namespace engine
