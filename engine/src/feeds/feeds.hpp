// One parser per exchange. The message formats are summarised in
// notes/research/feed-formats.md and each parser mirrors its checker in recorder/check.py.
#pragma once

#include <memory>

#include "feed.hpp"

namespace engine::feeds {

std::unique_ptr<Feed> coinbase();
std::unique_ptr<Feed> kraken();
std::unique_ptr<Feed> bitstamp();
std::unique_ptr<Feed> gemini();
std::unique_ptr<Feed> cryptocom();
std::unique_ptr<Feed> bullish();
std::unique_ptr<Feed> lmax();

}  // namespace engine::feeds
