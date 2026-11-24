// An order book: for each side, the total size waiting at each price.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <vector>

#include "fixed.hpp"

namespace engine {

struct Level {
  Fixed8 price = 0;
  Fixed8 qty = 0;
  friend bool operator==(const Level&, const Level&) = default;
};

// One side of a book. The levels are kept in a vector sorted from the worst price to the best,
// so the best level is at the back. Most changes happen near the best price, and inserting or
// erasing near the end of a vector moves only the few elements after that point.
class Side {
 public:
  enum class Kind : std::uint8_t { bids, asks };
  explicit Side(Kind kind) : kind_(kind) {}

  // Sets the total size at a price. A size of zero removes the level; removing a price that is
  // not in the book does nothing.
  void set(Fixed8 price, Fixed8 qty);
  // Replaces every level. The levels may come in any order; a price given twice keeps the size
  // given last, and zero sizes are left out.
  void assign(std::span<const Level> levels);
  // Keeps only the n best levels.
  void keep_best(std::size_t n);
  void clear() { levels_.clear(); }

  std::size_t size() const { return levels_.size(); }
  bool empty() const { return levels_.empty(); }
  // The i-th best level; level(0) is the best price. i must be less than size().
  const Level& level(std::size_t i) const;
  // The size at a price, or nothing if no level is there.
  std::optional<Fixed8> qty_at(Fixed8 price) const;
  // True if price a is better than price b on this side: higher for bids, lower for asks.
  bool better(Fixed8 a, Fixed8 b) const { return kind_ == Kind::bids ? a > b : a < b; }

 private:
  // The first level whose price is not worse than `price` (where `price` would be inserted).
  std::vector<Level>::iterator position(Fixed8 price);
  std::vector<Level>::const_iterator position(Fixed8 price) const;

  Kind kind_;
  std::vector<Level> levels_;  // worst price first, best price last
};

struct Book {
  Side bids{Side::Kind::bids};
  Side asks{Side::Kind::asks};

  // True if the best bid is at or above the best ask, which a correct book never shows.
  bool crossed() const;
  void clear() {
    bids.clear();
    asks.clear();
  }
};

}  // namespace engine
