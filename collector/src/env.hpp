#pragma once
#include <fstream>
#include <string>
#include <unordered_map>

namespace kc {

// Minimal .env reader: KEY=VALUE lines, '#' comments, tolerant of CRLF.
inline std::unordered_map<std::string, std::string> load_env_file(const std::string& path) {
    std::unordered_map<std::string, std::string> out;
    std::ifstream in(path);
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line[0] == '#') continue;
        const auto eq = line.find('=');
        if (eq == std::string::npos) continue;
        out[line.substr(0, eq)] = line.substr(eq + 1);
    }
    return out;
}

}  // namespace kc
