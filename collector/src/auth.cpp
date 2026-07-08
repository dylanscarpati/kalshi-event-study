#include "auth.hpp"

#include <openssl/bio.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>

#include <memory>
#include <stdexcept>
#include <vector>

namespace kc {
namespace {

using EvpKey = std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)>;

EvpKey load_private_key(const std::string& path) {
    std::unique_ptr<BIO, decltype(&BIO_free)> bio(BIO_new_file(path.c_str(), "rb"), BIO_free);
    if (!bio) throw std::runtime_error("cannot open private key: " + path);
    EVP_PKEY* raw = PEM_read_bio_PrivateKey(bio.get(), nullptr, nullptr, nullptr);
    if (!raw) throw std::runtime_error("cannot parse PEM private key: " + path);
    return EvpKey(raw, EVP_PKEY_free);
}

void set_pss_params(EVP_PKEY_CTX* pctx) {
    // Salt length must equal the digest length (32 for SHA-256); library
    // defaults differ and fail server-side verification.
    if (EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING) <= 0 ||
        EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST) <= 0 ||
        EVP_PKEY_CTX_set_rsa_mgf1_md(pctx, EVP_sha256()) <= 0) {
        throw std::runtime_error("cannot configure RSA-PSS parameters");
    }
}

std::string b64_encode(const unsigned char* data, size_t len) {
    std::string out;
    out.resize(4 * ((len + 2) / 3));
    const int n = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(out.data()), data,
                                  static_cast<int>(len));
    out.resize(static_cast<size_t>(n));
    return out;
}

std::vector<unsigned char> b64_decode(const std::string& text) {
    std::vector<unsigned char> out(3 * (text.size() / 4) + 3);
    const int n = EVP_DecodeBlock(out.data(), reinterpret_cast<const unsigned char*>(text.data()),
                                  static_cast<int>(text.size()));
    if (n < 0) throw std::runtime_error("invalid base64 signature");
    size_t pad = 0;
    for (auto it = text.rbegin(); it != text.rend() && *it == '='; ++it) ++pad;
    out.resize(static_cast<size_t>(n) - pad);
    return out;
}

}  // namespace

std::string sign_pss_b64(const std::string& private_key_pem_path, const std::string& message) {
    EvpKey key = load_private_key(private_key_pem_path);
    std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> ctx(EVP_MD_CTX_new(), EVP_MD_CTX_free);
    EVP_PKEY_CTX* pctx = nullptr;
    if (EVP_DigestSignInit(ctx.get(), &pctx, EVP_sha256(), nullptr, key.get()) <= 0)
        throw std::runtime_error("EVP_DigestSignInit failed");
    set_pss_params(pctx);
    size_t sig_len = 0;
    if (EVP_DigestSign(ctx.get(), nullptr, &sig_len,
                       reinterpret_cast<const unsigned char*>(message.data()),
                       message.size()) <= 0)
        throw std::runtime_error("EVP_DigestSign sizing failed");
    std::vector<unsigned char> sig(sig_len);
    if (EVP_DigestSign(ctx.get(), sig.data(), &sig_len,
                       reinterpret_cast<const unsigned char*>(message.data()),
                       message.size()) <= 0)
        throw std::runtime_error("EVP_DigestSign failed");
    return b64_encode(sig.data(), sig_len);
}

bool verify_pss_b64(const std::string& private_key_pem_path, const std::string& message,
                    const std::string& signature_b64) {
    EvpKey key = load_private_key(private_key_pem_path);
    const std::vector<unsigned char> sig = b64_decode(signature_b64);
    std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> ctx(EVP_MD_CTX_new(), EVP_MD_CTX_free);
    EVP_PKEY_CTX* pctx = nullptr;
    if (EVP_DigestVerifyInit(ctx.get(), &pctx, EVP_sha256(), nullptr, key.get()) <= 0)
        throw std::runtime_error("EVP_DigestVerifyInit failed");
    set_pss_params(pctx);
    return EVP_DigestVerify(ctx.get(), sig.data(), sig.size(),
                            reinterpret_cast<const unsigned char*>(message.data()),
                            message.size()) == 1;
}

WsAuthHeaders build_ws_auth_headers(const std::string& key_id,
                                    const std::string& private_key_pem_path,
                                    long long timestamp_ms) {
    const std::string ts = std::to_string(timestamp_ms);
    return WsAuthHeaders{key_id, sign_pss_b64(private_key_pem_path, ts + "GET/trade-api/ws/v2"),
                         ts};
}

}  // namespace kc
