#pragma once
#include <cstdio>
#include <mutex>
#include <string>

#include "stamp.hpp"

namespace kc {

// Append-only JSONL tape: one record per line, one fwrite per line, fflush
// per record, OS-level commit (fsync) every ~1 s -- crash cost is bounded by
// that window, and a torn final line is detected by a failed parse at
// replay. The raw payload is stored as a JSON STRING holding the exact
// original bytes (JSON-escaped, byte-recoverable) -- the same tape contract
// as the Python instruments. Never embedded as a JSON value: live Kalshi
// frames carry a trailing '\n' (verified 2026-07-08), which would split a
// JSONL line. Writes are serialized internally; any thread may append.
class Tape {
public:
    explicit Tape(const std::string& path);
    ~Tape();
    Tape(const Tape&) = delete;
    Tape& operator=(const Tape&) = delete;

    // fields: pre-rendered JSON members WITHOUT surrounding braces,
    // e.g. "\"schema\":\"collector/1\",\"direction\":\"in\"".
    void append_raw(const std::string& fields, const Stamp& s, const std::string& raw);
    // For event records whose whole body the caller already rendered.
    void append_prerendered(const std::string& json_object);

    long long count() const { return count_; }
    static std::string escape(const std::string& text);

private:
    void write_line(const std::string& line);
    std::mutex m_;
    std::FILE* f_ = nullptr;
    long long count_ = 0;
    int64_t last_fsync_mono_ns_ = 0;
};

}  // namespace kc
