#include "tape.hpp"

#include <algorithm>
#include <cstring>
#include <stdexcept>

namespace engine {
namespace {

constexpr std::size_t kInputChunk = std::size_t{1} << 20;  // compressed bytes read at a time
constexpr std::size_t kTextStep = std::size_t{1} << 20;    // most text produced by one fill()
constexpr std::size_t kPadding = simdjson::SIMDJSON_PADDING;

}  // namespace

TapeReader::TapeReader(const std::filesystem::path& path)
    : file_(std::fopen(path.c_str(), "rb"), &std::fclose), input_(kInputChunk) {
  if (!file_) {
    throw std::runtime_error("cannot open " + path.string());
  }
  // 16 + MAX_WBITS tells zlib to expect the gzip format: a header, then the compressed data,
  // then a checksum and the length of the original text.
  if (inflateInit2(&zlib_, 16 + MAX_WBITS) != Z_OK) {
    throw std::runtime_error("zlib could not start");
  }
  zlib_ready_ = true;
}

TapeReader::~TapeReader() {
  if (zlib_ready_) {
    inflateEnd(&zlib_);
  }
}

bool TapeReader::next(Record& record) {
  while (true) {
    // Look for the end of a line in the text not searched yet.
    if (searched_ < end_) {
      const void* found = std::memchr(text_.data() + searched_, '\n', end_ - searched_);
      if (found != nullptr) {
        const auto line_end =
            static_cast<std::size_t>(static_cast<const char*>(found) - text_.data());
        const std::size_t line_begin = begin_;
        begin_ = searched_ = line_end + 1;
        if (parse_line(line_begin, line_end, record)) {
          return true;
        }
        // A line that is not a record: nothing after it is trusted.
        damaged_ = finished_ = true;
        begin_ = searched_ = end_;
        return false;
      }
      searched_ = end_;
    }
    // No complete line is left. At the end, any text after the last line break is a line the
    // recorder never finished writing, and is dropped.
    if (!fill()) {
      return false;
    }
  }
}

bool TapeReader::fill() {
  if (finished_) {
    return false;
  }
  // Move the unread text to the front, then make room for one step plus the padding.
  std::copy(text_.begin() + static_cast<std::ptrdiff_t>(begin_),
            text_.begin() + static_cast<std::ptrdiff_t>(end_), text_.begin());
  end_ -= begin_;
  searched_ -= begin_;
  begin_ = 0;
  if (text_.size() < end_ + kTextStep + kPadding) {
    text_.resize(end_ + kTextStep + kPadding);
  }

  const std::size_t before = end_;
  while (end_ == before) {
    if (zlib_.avail_in == 0) {
      const std::size_t got = std::fread(input_.data(), 1, input_.size(), file_.get());
      if (got == 0) {  // the file ends before the gzip end: the recorder was cut off
        damaged_ = std::ferror(file_.get()) != 0;
        finished_ = true;
        return false;
      }
      zlib_.next_in = input_.data();
      zlib_.avail_in = static_cast<uInt>(got);
    }
    const std::size_t room = text_.size() - kPadding - end_;
    zlib_.next_out = reinterpret_cast<Bytef*>(text_.data() + end_);
    zlib_.avail_out = static_cast<uInt>(room);
    const int status = inflate(&zlib_, Z_NO_FLUSH);
    end_ += room - zlib_.avail_out;  // text written before any error is kept
    if (status == Z_STREAM_END) {    // the gzip end, with its checksum verified
      complete_ = finished_ = true;
      break;
    }
    if (status != Z_OK && status != Z_BUF_ERROR) {  // bytes that cannot be decompressed
      damaged_ = finished_ = true;
      break;
    }
  }
  return end_ > before;
}

bool TapeReader::parse_line(std::size_t begin, std::size_t end, Record& record) {
  record = Record{};
  event_.clear();
  body_.clear();
  bool has_wall = false;
  bool has_mono = false;
  bool has_payload = false;
  try {
    // The line is parsed where it lies: text_ always extends past it by at least the padding.
    const simdjson::padded_string_view line(text_.data() + begin, end - begin,
                                            text_.size() - begin);
    simdjson::ondemand::document doc = parser_.iterate(line);
    for (auto field : doc.get_object()) {
      const std::string_view key = field.unescaped_key();
      if (key == "wall_ns") {
        record.wall_ns = field.value().get_int64();
        has_wall = true;
      } else if (key == "mono_ns") {
        record.mono_ns = field.value().get_int64();
        has_mono = true;
      } else if (key == "raw") {  // the JSON string is unescaped back to the exchange's text
        body_.assign(std::string_view(field.value().get_string()));
        has_payload = true;
      } else if (key == "raw_b64") {
        record.binary = true;
        has_payload = true;
      } else if (key == "event") {
        event_.assign(std::string_view(field.value().get_string()));
        record.kind = Record::Kind::event;
      }
      // Other fields are event details the engine does not need.
    }
    if (!doc.at_end()) {
      return false;
    }
  } catch (const simdjson::simdjson_error&) {
    return false;
  }
  if (!has_wall || !has_mono || (record.kind == Record::Kind::message && !has_payload)) {
    return false;
  }
  body_.reserve(body_.size() + kPadding);
  record.event = event_;
  record.body = simdjson::padded_string_view(body_.data(), body_.size(), body_.capacity());
  return true;
}

}  // namespace engine
