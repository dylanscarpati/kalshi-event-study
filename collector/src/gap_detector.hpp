#pragma once
#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>

namespace kc {

struct SeqAnomaly {
    enum class Kind { gap, duplicate, regression };
    Kind kind;
    int64_t expected;
    int64_t received;
};

// Envelope-seq continuity per key (one key per subscription sid). The live
// trial (2026-07-08) showed every envelope type on a sid consumes a seq
// value and the chain continues across snapshots, so resync() is normally a
// no-op -- it stays because a venue-side change here must degrade to a
// logged re-baseline, not silent corruption.
class GapDetector {
public:
    std::optional<SeqAnomaly> observe(const std::string& key, int64_t seq) {
        auto it = last_.find(key);
        if (it == last_.end()) {
            last_.emplace(key, seq);
            return std::nullopt;
        }
        const int64_t prev = it->second;
        it->second = seq;
        const int64_t expected = prev + 1;
        if (seq == expected) return std::nullopt;
        if (seq == prev) return SeqAnomaly{SeqAnomaly::Kind::duplicate, expected, seq};
        if (seq < prev) return SeqAnomaly{SeqAnomaly::Kind::regression, expected, seq};
        return SeqAnomaly{SeqAnomaly::Kind::gap, expected, seq};
    }

    void resync(const std::string& key, int64_t seq) { last_[key] = seq; }
    void forget_all() { last_.clear(); }

private:
    std::unordered_map<std::string, int64_t> last_;
};

}  // namespace kc
