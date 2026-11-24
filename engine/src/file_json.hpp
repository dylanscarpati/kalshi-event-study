// What one file showed, as one line of JSON: the format of the replay command's --files output.
// engine/tests/make_tapes.py writes the same format for the files it expects.
#pragma once

#include <string>

#include "replay.hpp"

namespace engine {

std::string file_json(const FileSummary& file);

}  // namespace engine
