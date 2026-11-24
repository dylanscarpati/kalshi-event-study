// A 64-bit FNV-1a hash: a short fingerprint of the replay's output, so a test can tell whether
// two runs (or two machines) wrote exactly the same bytes. Not for security.
#pragma once

#include <cstdint>
#include <string_view>

namespace engine {

class Fnv1a64 {
 public:
  void add(std::string_view bytes) {
    for (const char c : bytes) {
      hash_ ^= static_cast<unsigned char>(c);  // mix in one byte...
      hash_ *= kPrime;                         // ...then spread it over all 64 bits
    }
  }
  std::uint64_t value() const { return hash_; }

 private:
  static constexpr std::uint64_t kPrime = 1'099'511'628'211ULL;
  std::uint64_t hash_ = 14'695'981'039'346'656'037ULL;  // the published starting value
};

}  // namespace engine
