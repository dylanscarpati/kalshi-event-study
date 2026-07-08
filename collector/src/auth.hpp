#pragma once
#include <string>

namespace kc {

struct WsAuthHeaders {
    std::string key;        // KALSHI-ACCESS-KEY
    std::string signature;  // KALSHI-ACCESS-SIGNATURE
    std::string timestamp;  // KALSHI-ACCESS-TIMESTAMP (milliseconds)
};

// RSA-PSS(SHA-256, MGF1-SHA256, salt = digest length), base64 encoded --
// the exact recipe verified against the live handshake on 2026-07-08.
// Message format: "{timestamp_ms}GET/trade-api/ws/v2". Timestamp is a
// parameter so every reconnect attempt must mint a fresh one.
std::string sign_pss_b64(const std::string& private_key_pem_path, const std::string& message);
bool verify_pss_b64(const std::string& private_key_pem_path, const std::string& message,
                    const std::string& signature_b64);
WsAuthHeaders build_ws_auth_headers(const std::string& key_id,
                                    const std::string& private_key_pem_path,
                                    long long timestamp_ms);

}  // namespace kc
