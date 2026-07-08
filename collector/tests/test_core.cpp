#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <openssl/bio.h>
#include <openssl/evp.h>
#include <openssl/pem.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include <random>
#include <string>

#include "auth.hpp"
#include "backoff.hpp"
#include "env.hpp"
#include "gap_detector.hpp"
#include "tape.hpp"

namespace fs = std::filesystem;
using kc::GapDetector;
using kc::SeqAnomaly;

TEST_CASE("gap detector: baseline, ok, gap, duplicate, regression") {
    GapDetector d;
    CHECK_FALSE(d.observe("s1", 1).has_value());  // first seq = baseline
    CHECK_FALSE(d.observe("s1", 2).has_value());
    auto gap = d.observe("s1", 5);
    REQUIRE(gap.has_value());
    CHECK(gap->kind == SeqAnomaly::Kind::gap);
    CHECK(gap->expected == 3);
    CHECK(gap->received == 5);
    auto dup = d.observe("s1", 5);
    REQUIRE(dup.has_value());
    CHECK(dup->kind == SeqAnomaly::Kind::duplicate);
    auto reg = d.observe("s1", 2);
    REQUIRE(reg.has_value());
    CHECK(reg->kind == SeqAnomaly::Kind::regression);
}

TEST_CASE("gap detector: key isolation, resync, forget_all") {
    GapDetector d;
    d.observe("a", 10);
    CHECK_FALSE(d.observe("b", 1).has_value());
    d.resync("a", 100);
    CHECK_FALSE(d.observe("a", 101).has_value());
    d.forget_all();
    CHECK_FALSE(d.observe("a", 7).has_value());  // fresh connection, no false gap
}

TEST_CASE("backoff: bounds, cap, jitter range") {
    std::mt19937 rng(42);
    const struct { int attempt; double nominal; } cases[] = {{0, 1}, {1, 2}, {2, 4}, {10, 30}};
    for (const auto& c : cases) {
        for (int i = 0; i < 50; ++i) {
            const double v = kc::backoff_delay_s(c.attempt, rng);
            CHECK(v >= 0.75 * c.nominal);
            CHECK(v <= 1.25 * c.nominal);
        }
    }
}

TEST_CASE("tape: raw is byte-exact through escaping, prerendered, count") {
    const fs::path path = fs::temp_directory_path() / "kc_tape_test.jsonl";
    std::error_code ec;
    fs::remove(path, ec);
    // Trailing newline matters: live Kalshi frames end with '\n', which must
    // never split a JSONL line (the bug the first live smoke run caught).
    const std::string frame = "{\"type\":\"trade\",\"seq\":7}\n";
    {
        kc::Tape tape(path.string());
        const kc::Stamp s{111, 222};
        tape.append_raw("\"k\":\"in\"", s, frame);
        tape.append_raw("\"k\":\"in\"", s, "plain text\nwith \"quotes\"");
        tape.append_prerendered(R"({"kind":"event"})");
        CHECK(tape.count() == 3);
    }
    std::ifstream in(path);
    std::string l1, l2, l3, extra;
    std::getline(in, l1);
    std::getline(in, l2);
    std::getline(in, l3);
    CHECK_FALSE(std::getline(in, extra));  // exactly 3 physical lines
    const auto r1 = nlohmann::json::parse(l1);
    CHECK(r1["recv_wall_ns"] == 111);
    CHECK(r1["raw"].get<std::string>() == frame);  // byte-exact incl. trailing \n
    const auto r2 = nlohmann::json::parse(l2);
    CHECK(r2["raw"] == "plain text\nwith \"quotes\"");
    CHECK(nlohmann::json::parse(l3)["kind"] == "event");
    fs::remove(path, ec);
}

TEST_CASE("tape escape: control characters") {
    CHECK(kc::Tape::escape("a\"b\\c\nd\te") == "a\\\"b\\\\c\\nd\\te");
    CHECK(kc::Tape::escape(std::string(1, '\x01')) == "\\u0001");
}

TEST_CASE("auth: PSS sign/verify round-trip and tamper detection") {
    // Throwaway key generated at test time -- never a committed key file.
    EVP_PKEY* key = EVP_PKEY_Q_keygen(nullptr, nullptr, "RSA", static_cast<size_t>(2048));
    REQUIRE(key != nullptr);
    const fs::path pem = fs::temp_directory_path() / "kc_test_key.pem";
    {
        // BIO, not FILE*: passing a CRT FILE* into an OpenSSL DLL requires
        // the applink shim on Windows; BIOs sidestep that entirely.
        BIO* bio = BIO_new_file(pem.string().c_str(), "wb");
        REQUIRE(bio != nullptr);
        REQUIRE(PEM_write_bio_PrivateKey(bio, key, nullptr, nullptr, 0, nullptr, nullptr) == 1);
        BIO_free(bio);
    }
    EVP_PKEY_free(key);

    const std::string msg = "1783500000000GET/trade-api/ws/v2";
    const std::string sig = kc::sign_pss_b64(pem.string(), msg);
    CHECK(kc::verify_pss_b64(pem.string(), msg, sig));
    CHECK_FALSE(kc::verify_pss_b64(pem.string(), "1783500000001GET/trade-api/ws/v2", sig));

    const auto headers = kc::build_ws_auth_headers("key-id", pem.string(), 1783500000000LL);
    CHECK(headers.key == "key-id");
    CHECK(headers.timestamp == "1783500000000");
    CHECK(kc::verify_pss_b64(pem.string(), msg, headers.signature));
    std::error_code ec;
    fs::remove(pem, ec);
}

TEST_CASE("env loader: comments, CRLF, values with equals") {
    const fs::path path = fs::temp_directory_path() / "kc_env_test.env";
    std::ofstream(path) << "# comment\nKEY_A=value-a\r\nKEY_B=x=y\n\nNOEQUALS\n";
    const auto env = kc::load_env_file(path.string());
    CHECK(env.at("KEY_A") == "value-a");
    CHECK(env.at("KEY_B") == "x=y");
    CHECK(env.count("NOEQUALS") == 0);
    std::error_code ec;
    fs::remove(path, ec);
}
