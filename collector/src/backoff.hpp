#pragma once
#include <algorithm>
#include <cmath>
#include <random>

namespace kc {

// Exponential backoff with jitter: 1, 2, 4 ... capped at 30 s, each wait
// randomized +/- jitter_frac so mass reconnections cannot synchronize.
inline double backoff_delay_s(int attempt, std::mt19937& rng, double base_s = 1.0,
                              double cap_s = 30.0, double jitter_frac = 0.25) {
    // Parenthesized to survive Windows.h's min/max macros in any consumer.
    const double nominal = (std::min)(base_s * std::ldexp(1.0, (std::max)(attempt, 0)), cap_s);
    std::uniform_real_distribution<double> jitter(1.0 - jitter_frac, 1.0 + jitter_frac);
    return nominal * jitter(rng);
}

}  // namespace kc
