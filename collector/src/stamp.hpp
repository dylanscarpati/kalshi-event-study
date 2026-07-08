#pragma once
#include <chrono>
#include <cstdint>

namespace kc {

// Both clocks, always together: wall (system_clock) aligns observations to
// calendar time but can step under NTP; mono (steady_clock) never jumps and
// is the only clock durations may be computed from.
struct Stamp {
    int64_t wall_ns;
    int64_t mono_ns;
};

inline Stamp stamp_now() {
    using namespace std::chrono;
    return Stamp{
        duration_cast<nanoseconds>(system_clock::now().time_since_epoch()).count(),
        duration_cast<nanoseconds>(steady_clock::now().time_since_epoch()).count(),
    };
}

}  // namespace kc
