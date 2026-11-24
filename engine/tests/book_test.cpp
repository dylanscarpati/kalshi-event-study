#include "book.hpp"

#include <gtest/gtest.h>

#include <cstdint>
#include <functional>
#include <map>
#include <vector>

namespace engine {
namespace {

std::vector<Level> best_first(const Side& side) {
  std::vector<Level> out;
  for (std::size_t i = 0; i < side.size(); ++i) {
    out.push_back(side.level(i));
  }
  return out;
}

TEST(Side, BidsAreKeptHighestPriceFirst) {
  Side bids(Side::Kind::bids);
  bids.set(100, 1);
  bids.set(102, 2);
  bids.set(101, 3);
  EXPECT_EQ(best_first(bids), (std::vector<Level>{{102, 2}, {101, 3}, {100, 1}}));
}

TEST(Side, AsksAreKeptLowestPriceFirst) {
  Side asks(Side::Kind::asks);
  asks.set(100, 1);
  asks.set(102, 2);
  asks.set(101, 3);
  EXPECT_EQ(best_first(asks), (std::vector<Level>{{100, 1}, {101, 3}, {102, 2}}));
}

TEST(Side, SettingAPriceAgainReplacesItsSizeAndZeroRemovesIt) {
  Side bids(Side::Kind::bids);
  bids.set(100, 1);
  bids.set(101, 1);
  bids.set(100, 5);
  EXPECT_EQ(best_first(bids), (std::vector<Level>{{101, 1}, {100, 5}}));
  bids.set(101, 0);
  EXPECT_EQ(best_first(bids), (std::vector<Level>{{100, 5}}));
  bids.set(99, 0);  // not in the book: nothing happens
  EXPECT_EQ(best_first(bids), (std::vector<Level>{{100, 5}}));
}

TEST(Side, AssignReplacesEverythingWhateverTheOrder) {
  Side asks(Side::Kind::asks);
  asks.set(50, 1);
  const std::vector<Level> given = {{103, 1}, {101, 2}, {102, 0}, {101, 7}, {104, 4}};
  asks.assign(given);
  // 101 keeps the size given last (7), 102 has size zero and is left out, 50 is gone.
  EXPECT_EQ(best_first(asks), (std::vector<Level>{{101, 7}, {103, 1}, {104, 4}}));
}

TEST(Side, KeepBestDropsTheWorstLevels) {
  Side bids(Side::Kind::bids);
  Side asks(Side::Kind::asks);
  for (Fixed8 price = 1; price <= 5; ++price) {
    bids.set(price, 1);
    asks.set(price, 1);
  }
  bids.keep_best(2);
  asks.keep_best(2);
  EXPECT_EQ(best_first(bids), (std::vector<Level>{{5, 1}, {4, 1}}));
  EXPECT_EQ(best_first(asks), (std::vector<Level>{{1, 1}, {2, 1}}));
  bids.keep_best(10);  // fewer levels than that: nothing happens
  EXPECT_EQ(bids.size(), 2U);
}

TEST(Side, QtyAtFindsAPriceOrNothing) {
  Side bids(Side::Kind::bids);
  bids.set(100, 3);
  EXPECT_EQ(bids.qty_at(100), 3);
  EXPECT_EQ(bids.qty_at(101), std::nullopt);
}

TEST(Book, IsCrossedWhenTheBestBidReachesTheBestAsk) {
  Book book;
  EXPECT_FALSE(book.crossed());  // an empty side cannot cross
  book.bids.set(99, 1);
  book.asks.set(100, 1);
  EXPECT_FALSE(book.crossed());
  book.bids.set(100, 1);  // equal prices: crossed
  EXPECT_TRUE(book.crossed());
  book.bids.set(100, 0);
  book.bids.set(101, 1);
  EXPECT_TRUE(book.crossed());
}

// Applies the same random changes to a Side and to std::map, and compares them after each one.
template <class Compare>
void compare_with_map(Side::Kind kind) {
  Side side(kind);
  std::map<Fixed8, Fixed8, Compare> reference;  // ordered best first by Compare
  std::uint64_t state = 12345;                  // a fixed linear congruential generator
  auto next = [&state](std::uint64_t n) {
    state = (state * 6364136223846793005ULL) + 1442695040888963407ULL;
    return static_cast<Fixed8>((state >> 33) % n);
  };
  for (int step = 0; step < 20000; ++step) {
    const Fixed8 price = 1000 + next(200);
    const Fixed8 qty = next(3) == 0 ? 0 : 1 + next(50);
    side.set(price, qty);
    if (qty == 0) {
      reference.erase(price);
    } else {
      reference[price] = qty;
    }
    if (step % 97 == 0) {
      side.keep_best(150);
      while (reference.size() > 150) {
        reference.erase(std::prev(reference.end()));
      }
    }
    ASSERT_EQ(side.size(), reference.size()) << "step " << step;
    std::size_t i = 0;
    for (const auto& [p, q] : reference) {
      ASSERT_EQ(side.level(i), (Level{p, q})) << "step " << step << " level " << i;
      ++i;
    }
  }
}

TEST(Side, AgreesWithStdMapOverRandomChanges) {
  compare_with_map<std::greater<>>(Side::Kind::bids);
  compare_with_map<std::less<>>(Side::Kind::asks);
}

}  // namespace
}  // namespace engine
