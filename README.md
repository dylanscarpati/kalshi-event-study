# Replicating Kalshi's Bitcoin settlement index from exchange order books

**Status: data collection in progress (since 2026-11-22). The analysis engine is under construction; no results yet.**

## Question

Kalshi's 15-minute Bitcoin markets settle on the average of 60 one-second values of the CF Benchmarks Bitcoin Real-Time Index (BRTI), taken over the minute before each quarter hour. The BRTI is computed from the order books of several spot exchanges under a published methodology.

This project asks how closely that settlement value can be reproduced from public order-book data alone, and where the residual error comes from: exchanges whose books are not observed, unstated details of the method, and timing.

## Approach

1. **Record.** Capture the public BTC/USD order-book feeds of the index's constituent exchanges continuously, storing every message unchanged with its receive time.
2. **Rebuild.** Reconstruct each exchange's order book offline, verifying it against the exchange's own integrity data (sequence numbers, checksums, snapshots).
3. **Compute.** Apply the published index methodology to the combined books once per second (C++, deterministic).
4. **Compare.** Average the 60 seconds before each quarter hour and compare with Kalshi's published settlement values; report the error and attribute it to its sources.

Steps 1 and 2 (verification only) are built and running. Steps 3 and 4 are in progress.

## Data collection

| Exchange | Feed | Integrity check |
|---|---|---|
| Coinbase | Full book, then changes | Sequence number on every message |
| Kraken | Top 1,000 levels, then changes | CRC32 checksum of the top ten levels |
| Gemini | Full book, then changes | Overlapping update numbers; top-five snapshot each second |
| Crypto.com | Top 50 levels, then changes | Each update names its predecessor |
| Bitstamp | Changes only; full book downloaded each minute | None available on the feed |
| Bullish | Full book, about ten times a second | Sequence ranges |
| LMAX Digital | Full book, once a second | One book per second, none skipped |

Each exchange is recorded by its own process on a dedicated server, reconnecting automatically and once an hour on a schedule that avoids the measured minutes. Recorded data is checked daily for integrity breaks and for coverage of the measured windows (the 60 seconds before each quarter hour).

Raw market data is not included in this repository.

## Repository

| Path | Contents |
|---|---|
| `recorder/recorder.py` | Records one exchange's feed to compressed files |
| `recorder/feeds.py` | Connection settings for each exchange |
| `recorder/tape.py` | File format: writing, crash-tolerant reading |
| `recorder/check.py` | Integrity and window-coverage check of recorded data |
| `recorder/tests/` | Tests, run against simulated exchanges |
| `ops/` | Server installation, health report, daily check |

## Running the tests

```bash
python3 -m venv .venv
.venv/bin/pip install "websockets==15.0.1" "pytest>=8,<9"
.venv/bin/python -m pytest -q
```
