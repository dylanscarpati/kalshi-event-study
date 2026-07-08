// Kalshi market-data collector.
//
// Hot-path discipline: the WebSocket callback does exactly two things per
// inbound message -- dual-clock stamp, then enqueue the raw bytes for the
// writer thread. Control parsing (sequence continuity, snapshot requests)
// runs only after the record is safely queued; a parsing bug can never cost
// an observation. The tape is written by a dedicated thread (flush per
// record, fsync ~1 s).
//
// Reconnects mint FRESH auth headers every attempt: IXWebSocket's automatic
// reconnection is disabled because it would replay the original handshake
// headers, whose signed timestamp goes stale -- the venue would 401 forever.

#ifdef _WIN32
#define NOMINMAX
#endif
#include <ixwebsocket/IXNetSystem.h>
#include <ixwebsocket/IXWebSocket.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <iostream>
#include <mutex>
#include <nlohmann/json.hpp>
#include <random>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "auth.hpp"
#include "backoff.hpp"
#include "env.hpp"
#include "gap_detector.hpp"
#include "stamp.hpp"
#include "tape.hpp"

namespace kc {
namespace {

constexpr const char* kWsUrl = "wss://external-api-ws.kalshi.com/trade-api/ws/v2";
constexpr const char* kWsHost = "external-api-ws.kalshi.com";
constexpr const char* kSchema = "collector/1";
constexpr int64_t kSnapshotMinIntervalNs = 5'000'000'000LL;

struct Record {
    Stamp stamp;
    std::string raw;
    bool outbound;
};

// Unbounded with a warning threshold: blocking would stall the network
// callback (receipt must never wait on disk) and dropping would lose the
// one thing this program exists to keep. Depth is telemetry instead.
class RecordQueue {
public:
    void push(Record r) {
        std::lock_guard<std::mutex> lock(m_);
        q_.push_back(std::move(r));
        cv_.notify_one();
    }
    bool pop(Record& out) {
        std::unique_lock<std::mutex> lock(m_);
        cv_.wait_for(lock, std::chrono::milliseconds(200),
                     [this] { return !q_.empty() || closed_; });
        if (q_.empty()) return false;
        out = std::move(q_.front());
        q_.pop_front();
        return true;
    }
    size_t depth() {
        std::lock_guard<std::mutex> lock(m_);
        return q_.size();
    }
    void close() {
        std::lock_guard<std::mutex> lock(m_);
        closed_ = true;
        cv_.notify_all();
    }
    bool closed() {
        std::lock_guard<std::mutex> lock(m_);
        return closed_ && q_.empty();
    }

private:
    std::mutex m_;
    std::condition_variable cv_;
    std::deque<Record> q_;
    bool closed_ = false;
};

std::string make_run_id() {
    const auto now = std::chrono::system_clock::now();
    const std::time_t t = std::chrono::system_clock::to_time_t(now);
    std::tm tm{};
#ifdef _WIN32
    gmtime_s(&tm, &t);
#else
    gmtime_r(&t, &tm);
#endif
    char ts[32];
    std::strftime(ts, sizeof ts, "%Y%m%dT%H%M%SZ", &tm);
    std::random_device rd;
    std::ostringstream os;
    os << ts << "-" << std::hex << (static_cast<uint64_t>(rd()) << 16 | (rd() & 0xffff));
    return os.str();
}

struct Config {
    std::vector<std::string> tickers;
    double duration_min = 60.0;
    std::string out_dir = "data/ws-cpp";
    std::string env_path = ".env";
    std::string url = kWsUrl;
};

}  // namespace

int run(const Config& cfg) {
    const auto env = load_env_file(cfg.env_path);
    const auto key_it = env.find("KALSHI_API_KEY_ID");
    const auto pem_it = env.find("KALSHI_PRIVATE_KEY_PATH");
    if (key_it == env.end() || pem_it == env.end() ||
        key_it->second.empty() || key_it->second.rfind("paste-", 0) == 0) {
        std::cerr << "missing credentials: set KALSHI_API_KEY_ID and "
                     "KALSHI_PRIVATE_KEY_PATH in " << cfg.env_path << "\n";
        return 2;
    }
    const std::string key_id = key_it->second;
    const std::string pem_path = pem_it->second;

    // Pre-flight: the key was shown once at creation; a mangled save must
    // surface now, not on release morning.
    const std::string probe = "0GET/trade-api/ws/v2";
    if (!verify_pss_b64(pem_path, probe, sign_pss_b64(pem_path, probe))) {
        std::cerr << "PEM sign/verify round-trip failed: " << pem_path << "\n";
        return 2;
    }

    const std::string run_id = make_run_id();
    Tape frames(cfg.out_dir + "/" + run_id + ".frames.jsonl");
    Tape events(cfg.out_dir + "/" + run_id + ".events.jsonl");
    const std::string base_fields =
        std::string("\"schema\":\"") + kSchema + "\",\"run_id\":\"" + run_id + "\"";

    auto emit_event = [&](const std::string& kind, const nlohmann::json& extra) {
        nlohmann::json e = extra;
        const Stamp s = stamp_now();
        e["schema"] = kSchema;
        e["run_id"] = run_id;
        e["kind"] = kind;
        e["wall_ns"] = s.wall_ns;
        e["mono_ns"] = s.mono_ns;
        events.append_prerendered(e.dump());
    };

    RecordQueue queue;
    std::thread writer([&] {
        Record r;
        while (!queue.closed()) {
            if (!queue.pop(r)) continue;
            frames.append_raw(base_fields + (r.outbound ? ",\"direction\":\"out\""
                                                        : ",\"direction\":\"in\""),
                              r.stamp, r.raw);
        }
    });

    emit_event("run_start", {{"tickers", cfg.tickers}, {"duration_min", cfg.duration_min}});

    ix::initNetSystem();
    GapDetector gaps;
    std::mutex control_mutex;  // guards gaps + snapshot limiter across callbacks
    std::unordered_map<int64_t, int64_t> snap_last_mono;
    std::atomic<int> cmd_id{1};
    std::atomic<bool> connection_up{false};

    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::duration<double>(cfg.duration_min * 60.0);
    std::mt19937 rng(std::random_device{}());
    int attempt = 0;

    while (std::chrono::steady_clock::now() < deadline) {
        ix::WebSocket ws;
        ws.setUrl(cfg.url);
        ws.disableAutomaticReconnection();  // fresh signed headers per attempt, or 401 forever
        ws.setPingInterval(30);

        const auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::system_clock::now().time_since_epoch())
                                .count();
        const WsAuthHeaders auth = build_ws_auth_headers(key_id, pem_path, now_ms);
        ix::WebSocketHttpHeaders headers;
        headers["KALSHI-ACCESS-KEY"] = auth.key;
        headers["KALSHI-ACCESS-SIGNATURE"] = auth.signature;
        headers["KALSHI-ACCESS-TIMESTAMP"] = auth.timestamp;
        headers["User-Agent"] = "kalshi-collector/0.1";
        // IXWebSocket fabricates "Origin: wss://<host>" and "Host: <host>:443"
        // when the caller doesn't supply them. Kalshi's edge enforces a
        // browser-origin allow-list: any non-allowed Origin value draws 403,
        // while absent or EMPTY Origin passes (verified against the live edge
        // 2026-07-08). Supplying Origin="" suppresses the library's fabricated
        // value; Host is pinned to drop the ":443" suffix.
        headers["Host"] = kWsHost;
        headers["Origin"] = "";
        ws.setExtraHeaders(headers);

        auto send_taped = [&](const std::string& text) {
            queue.push(Record{stamp_now(), text, true});
            ws.send(text);
        };

        ws.setOnMessageCallback([&](const ix::WebSocketMessagePtr& msg) {
            if (msg->type == ix::WebSocketMessageType::Message) {
                const Stamp s = stamp_now();                      // 1: stamp
                queue.push(Record{s, msg->str, false});           // 2: enqueue
                // Control path -- the observation is already safe above.
                try {
                    const auto env_json = nlohmann::json::parse(msg->str);
                    const std::string type = env_json.value("type", "");
                    if (!env_json.contains("sid") || !env_json.contains("seq")) return;
                    const int64_t sid = env_json["sid"].get<int64_t>();
                    const int64_t seq = env_json["seq"].get<int64_t>();
                    std::lock_guard<std::mutex> lock(control_mutex);
                    const std::string key = "sid:" + std::to_string(sid);
                    if (type == "orderbook_snapshot") {
                        gaps.resync(key, seq);
                        return;
                    }
                    if (const auto anomaly = gaps.observe(key, seq)) {
                        emit_event("seq_anomaly",
                                   {{"sid", sid},
                                    {"expected", anomaly->expected},
                                    {"received", anomaly->received}});
                        const int64_t now_ns = stamp_now().mono_ns;
                        auto& last = snap_last_mono[sid];
                        if (now_ns - last >= kSnapshotMinIntervalNs) {
                            last = now_ns;
                            nlohmann::json cmd = {
                                {"id", cmd_id.fetch_add(1)},
                                {"cmd", "update_subscription"},
                                {"params",
                                 {{"sids", {sid}},
                                  {"market_tickers", cfg.tickers},
                                  {"action", "get_snapshot"}}}};
                            send_taped(cmd.dump());
                            emit_event("snapshot_requested", {{"sid", sid}});
                        }
                    }
                } catch (const std::exception&) {
                    emit_event("unparseable_frame", {});
                }
            } else if (msg->type == ix::WebSocketMessageType::Open) {
                connection_up = true;
                emit_event("connected", {{"attempt", attempt}});
                {
                    std::lock_guard<std::mutex> lock(control_mutex);
                    gaps.forget_all();  // new connection = new chains
                }
                // One subscription per channel (server merges same-channel
                // subscribes anyway -- verified live 2026-07-08).
                for (const char* channel : {"orderbook_delta", "trade"}) {
                    nlohmann::json cmd = {{"id", cmd_id.fetch_add(1)},
                                          {"cmd", "subscribe"},
                                          {"params",
                                           {{"channels", {channel}},
                                            {"market_tickers", cfg.tickers}}}};
                    send_taped(cmd.dump());
                }
            } else if (msg->type == ix::WebSocketMessageType::Close ||
                       msg->type == ix::WebSocketMessageType::Error) {
                connection_up = false;
                emit_event("disconnected",
                           {{"reason", msg->type == ix::WebSocketMessageType::Close
                                           ? msg->closeInfo.reason
                                           : msg->errorInfo.reason}});
            }
        });

        ws.start();
        // Wait for the Open event (readyState right after start() still says
        // Closed while the connect thread spins up -- polling it here would
        // kill the handshake before it begins).
        const auto wait_start = std::chrono::steady_clock::now();
        while (std::chrono::steady_clock::now() < deadline && !connection_up &&
               std::chrono::steady_clock::now() - wait_start < std::chrono::seconds(20)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        const auto connected_at = std::chrono::steady_clock::now();
        const bool opened = connection_up;
        while (std::chrono::steady_clock::now() < deadline && connection_up) {
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
        }
        ws.stop();
        if (std::chrono::steady_clock::now() >= deadline) break;

        const bool was_stable = opened && std::chrono::steady_clock::now() - connected_at >
                                              std::chrono::seconds(60);
        connection_up = false;
        attempt = was_stable ? 0 : attempt + 1;
        const double delay = backoff_delay_s(attempt, rng);
        emit_event("backoff", {{"attempt", attempt}, {"delay_s", delay}});
        std::this_thread::sleep_for(std::chrono::duration<double>(delay));
    }

    emit_event("run_end", {{"queue_depth_at_end", queue.depth()}});
    queue.close();
    writer.join();
    ix::uninitNetSystem();
    std::cout << "taped " << frames.count() << " frame records -> run " << run_id << "\n";
    return 0;
}

}  // namespace kc

int main(int argc, char** argv) {
    kc::Config cfg;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--tickers") {
            while (i + 1 < argc && argv[i + 1][0] != '-') cfg.tickers.emplace_back(argv[++i]);
        } else if (arg == "--duration-min" && i + 1 < argc) {
            cfg.duration_min = std::stod(argv[++i]);
        } else if (arg == "--out-dir" && i + 1 < argc) {
            cfg.out_dir = argv[++i];
        } else if (arg == "--env" && i + 1 < argc) {
            cfg.env_path = argv[++i];
        } else if (arg == "--url" && i + 1 < argc) {
            cfg.url = argv[++i];
        }
    }
    for (int i = 1; i < argc; ++i) {
        if (std::string(argv[i]) == "--auth-probe") {
            // Diagnostic: emit one timestamp+signature pair for offline
            // cross-verification against the Python implementation.
            const auto env = kc::load_env_file(cfg.env_path);
            const auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                                    std::chrono::system_clock::now().time_since_epoch())
                                    .count();
            const auto h = kc::build_ws_auth_headers(env.at("KALSHI_API_KEY_ID"),
                                                     env.at("KALSHI_PRIVATE_KEY_PATH"), now_ms);
            std::cout << h.timestamp << "\n" << h.signature << "\n";
            return 0;
        }
    }
    if (cfg.tickers.empty()) {
        std::cerr << "usage: collector --tickers T1 [T2 ...] [--duration-min N]"
                     " [--out-dir DIR] [--env PATH] [--auth-probe]\n";
        return 2;
    }
    try {
        return kc::run(cfg);
    } catch (const std::exception& e) {
        std::cerr << "fatal: " << e.what() << "\n";
        return 1;
    }
}
