#include "tape.hpp"

#include <filesystem>
#include <stdexcept>

#ifdef _WIN32
#include <io.h>
#define KC_COMMIT _commit
#define KC_FILENO _fileno
#else
#include <unistd.h>
#define KC_COMMIT fsync
#define KC_FILENO fileno
#endif

namespace kc {

Tape::Tape(const std::string& path) {
    const auto parent = std::filesystem::path(path).parent_path();
    if (!parent.empty()) {
        std::error_code ec;
        std::filesystem::create_directories(parent, ec);
    }
    f_ = std::fopen(path.c_str(), "ab");
    if (!f_) throw std::runtime_error("cannot open tape: " + path);
}

Tape::~Tape() {
    if (f_) {
        std::fflush(f_);
        KC_COMMIT(KC_FILENO(f_));
        std::fclose(f_);
    }
}

std::string Tape::escape(const std::string& text) {
    std::string out;
    out.reserve(text.size() + 16);
    for (unsigned char c : text) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

void Tape::append_raw(const std::string& fields, const Stamp& s, const std::string& raw) {
    std::string line;
    line.reserve(fields.size() + raw.size() + 96);
    line += '{';
    line += fields;
    line += ",\"recv_wall_ns\":";
    line += std::to_string(s.wall_ns);
    line += ",\"recv_mono_ns\":";
    line += std::to_string(s.mono_ns);
    line += ",\"raw\":\"";
    line += escape(raw);
    line += "\"}\n";
    write_line(line);
}

void Tape::append_prerendered(const std::string& json_object) {
    write_line(json_object + "\n");
}

void Tape::write_line(const std::string& line) {
    std::lock_guard<std::mutex> lock(m_);
    std::fwrite(line.data(), 1, line.size(), f_);
    std::fflush(f_);
    ++count_;
    const int64_t now = stamp_now().mono_ns;
    if (now - last_fsync_mono_ns_ >= 1'000'000'000LL) {
        KC_COMMIT(KC_FILENO(f_));
        last_fsync_mono_ns_ = now;
    }
}

}  // namespace kc
