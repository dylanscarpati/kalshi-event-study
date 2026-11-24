#include "book.hpp"

#include <algorithm>
#include <cassert>

namespace engine {

std::vector<Level>::iterator Side::position(Fixed8 price) {
  // levels_ runs from worst to best, so "a is before b" means "a is worse than b".
  return std::lower_bound(levels_.begin(), levels_.end(), price,
                          [this](const Level& level, Fixed8 p) { return better(p, level.price); });
}

std::vector<Level>::const_iterator Side::position(Fixed8 price) const {
  return std::lower_bound(levels_.begin(), levels_.end(), price,
                          [this](const Level& level, Fixed8 p) { return better(p, level.price); });
}

void Side::set(Fixed8 price, Fixed8 qty) {
  const auto at = position(price);
  const bool present = at != levels_.end() && at->price == price;
  if (qty == 0) {
    if (present) {
      levels_.erase(at);
    }
  } else if (present) {
    at->qty = qty;
  } else {
    levels_.insert(at, Level{price, qty});
  }
}

void Side::assign(std::span<const Level> levels) {
  levels_.assign(levels.begin(), levels.end());
  // A stable sort keeps levels with the same price in the order given, so the last of them is
  // the one given last.
  std::stable_sort(levels_.begin(), levels_.end(),
                   [this](const Level& a, const Level& b) { return better(b.price, a.price); });
  auto kept = levels_.begin();  // [begin, kept) holds one level per price so far
  for (const Level& level : levels_) {
    if (kept != levels_.begin() && std::prev(kept)->price == level.price) {
      *std::prev(kept) = level;  // the same price again: the later size wins
    } else {
      *kept++ = level;
    }
  }
  levels_.erase(kept, levels_.end());
  std::erase_if(levels_, [](const Level& level) { return level.qty == 0; });
}

void Side::keep_best(std::size_t n) {
  if (levels_.size() > n) {  // the worst levels are at the front
    levels_.erase(levels_.begin(), levels_.end() - static_cast<std::ptrdiff_t>(n));
  }
}

const Level& Side::level(std::size_t i) const {
  assert(i < levels_.size());
  return levels_[levels_.size() - 1 - i];
}

std::optional<Fixed8> Side::qty_at(Fixed8 price) const {
  const auto at = position(price);
  if (at != levels_.end() && at->price == price) {
    return at->qty;
  }
  return std::nullopt;
}

bool Book::crossed() const {
  return !bids.empty() && !asks.empty() && bids.level(0).price >= asks.level(0).price;
}

}  // namespace engine
