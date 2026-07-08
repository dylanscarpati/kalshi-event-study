# Kalshi Macro-Release Event Study

An empirical study of price discovery in Kalshi prediction markets around scheduled U.S. macroeconomic releases: CPI, the jobs report, and FOMC rate decisions. Built on market data collected by this repository's own instruments. In progress, July 2026.

## Questions

1. **Pre-announcement behavior.** Do contract prices drift ahead of scheduled release timestamps, or hold steady until the news lands?
2. **Adjustment speed.** After the release timestamp, how quickly do prices converge to their new level, and how does that vary by release type?
3. **Calibration.** Treating prices as probabilities, how well calibrated are they as a function of time-to-resolution, measured across the full history of settled markets?
4. **Does the market learn?** (stretch) Is price discovery faster or better calibrated for high-surprise versus low-surprise releases, and does adjustment speed change over the sample period?

## Method

A long-running C++ daemon collects the raw order-book and trade tape over Kalshi's WebSocket API, timestamping every message on receipt with both wall and monotonic clocks. A Python layer aligns events on their scheduled release timestamps and produces event-study price paths, empirical distributions of post-release adjustment times, and calibration curves built from historical settled markets. Uncertainty is quantified with bootstrap confidence intervals, resampled at the event level to respect the correlation between contracts that settle on the same print.

## Results

Forthcoming. Every figure will state its sample size; null results will be reported as nulls.

## Limitations

Stated up front, before any results:

- **Latency floor.** Every recorded timestamp is the true event time plus a positive network delay, so measured adjustment times are upper bounds. The delay is measured (RTT logging) and reported, not assumed away.
- **Clock error.** Wall-clock error is bounded by logged NTP offset estimates; durations are computed on the monotonic clock.
- **Small live-capture sample.** Scheduled macro releases arrive roughly monthly per series; the number of live-captured events will be small, and confidence intervals will be wide and honest about it.
- **Single venue.** Results describe Kalshi, not prediction markets in general.
- **Liquidity, spreads, and fees.** Wide or one-sided books are an error bar on any mid-price, and any deviation must clear round-trip trading costs before it can be called exploitable rather than merely detectable.
- **Regime dependence.** A sample collected in one macro regime may not generalize; resampling methods quantify noise, not representativeness.

## Repository layout

| Path         | Contents                                              |
| ------------ | ----------------------------------------------------- |
| `collector/` | C++ market-data collector daemon (in progress)        |
| `analysis/`  | Python analysis layer and data probes                 |
| `data/`      | Local captures — not tracked, see compliance below    |

## Data and compliance

This project collects market data for research only. No trading, ever. Raw collected data is not redistributed in this repository, per Kalshi's data terms; the collection code lets anyone regenerate the dataset themselves. API credentials live in a local `.env` file and are never committed.

## Setup

```
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
.venv/bin/python -m pip install -r requirements.txt       # macOS/Linux
```

## License

MIT. The license covers the code and text in this repository; no license is granted to Kalshi market data, none of which is included here.
