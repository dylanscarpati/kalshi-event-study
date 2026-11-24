# Reconstructing Kalshi's Bitcoin settlement price from exchange order books

Kalshi's 15-minute Bitcoin contracts settle on a 60-second average of the CF Benchmarks Bitcoin Real-Time Index (BRTI), an order-book-weighted price computed from several spot exchanges. How closely can that settlement value be reproduced from public order-book data alone, and where does the residual error come from: unobserved venues, unspecified methodology details, or timing?

## Data

| Exchange | Feed | Integrity check |
|---|---|---|
| Coinbase | Full book, then changes | Sequence numbers |
| Kraken | Top 1,000 levels, then changes | CRC32 checksum |
| Gemini | Full book, then changes | Update numbers; top-five snapshots |
| Crypto.com | Top 50 levels, then changes | Update chain |
| Bitstamp | Changes; full book each minute | None on the feed |
| Bullish | Full book, ~10 per second | Sequence ranges |
| LMAX Digital | Full book, once per second | Consecutive timestamps |

Recordings are checked daily for integrity breaks and for coverage of the measured windows (the 60 seconds before each quarter hour). Raw data is not distributed.

## Repository

| Path | Contents |
|---|---|
| `recorder/` | Capture, file format and integrity checker, with tests against simulated exchanges |
| `engine/` | C++20 order-book reconstruction for all seven exchanges, sampled every second; integrity checks match the recorder's checker, and output is tested byte for byte against synthetic tapes |
| `ops/` | Server deployment, health report, daily check |

```bash
cmake --workflow --preset dev    # engine: build and test (CMake 3.25+, Ninja, zlib)
```

```bash
python3 -m venv .venv && .venv/bin/pip install "websockets==15.0.1" "pytest>=8,<9"
.venv/bin/python -m pytest -q    # recorder
```
