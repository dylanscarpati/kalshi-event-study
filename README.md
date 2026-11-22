# Reconstructing Kalshi's Bitcoin settlement price from exchange order books

*Status: data collection in progress since 2026-11-22. No results yet.*

Kalshi's 15-minute Bitcoin contracts settle on a 60-second average of the CF Benchmarks Bitcoin Real-Time Index, an order-book-weighted price computed from several spot exchanges. This project reconstructs that settlement value from public order-book data alone and measures the residual error, attributing it to unobserved venues, unspecified methodology details and timing.

**Method.** Raw order-book feeds from the index's constituent exchanges are recorded continuously and verified against each exchange's integrity data. Books are then rebuilt offline, the published index methodology is applied once per second in a deterministic C++ engine, and the 60-second averages are compared with Kalshi's published settlement values.

**Data.** Coinbase, Kraken, Gemini, Crypto.com, Bitstamp, Bullish and LMAX Digital, BTC/USD. Raw data is not distributed.

**Repository.** `recorder/` holds the capture and verification code with its tests; `ops/` holds server deployment. The engine and analysis will follow.
