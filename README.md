# Kalshi Macro-Release Event Study

An empirical study of price discovery in Kalshi prediction markets around scheduled U.S. macroeconomic releases: CPI, the jobs report, and FOMC rate decisions. Built on market data collected by this repository's own instruments. In progress, July 2026.

## Questions

1. **Pre-announcement behavior.** Do contract prices drift ahead of scheduled release timestamps, or hold steady until the news lands?
2. **Adjustment speed.** After the release timestamp, how quickly do prices converge to their new level, and how does that vary by release type?
3. **Calibration.** Treating prices as probabilities, how well calibrated are they as a function of time-to-resolution, measured across the full history of settled markets?
4. **Does the market learn?** (stretch) Is price discovery faster or better calibrated for high-surprise versus low-surprise releases, and does adjustment speed change over the sample period?

## Method

A long-running C++ daemon will collect the raw order-book and trade tape over Kalshi's WebSocket API, timestamping every message on receipt with both wall and monotonic clocks. A Python layer will align events on their scheduled release timestamps and produce event-study price paths, empirical distributions of post-release adjustment times, and calibration curves built from historical settled markets. Uncertainty will be quantified with bootstrap confidence intervals, resampled at the event level to respect the correlation between contracts that settle on the same print.

What exists today: a REST probe that discovers the nearest open event in a macro series and prints its consolidated YES order-book view; a release-morning polling instrument (drift-free 1 Hz grid, survives transient failures and rate limiting); a WebSocket recorder that tapes every frame verbatim with sequence-gap detection and automatic reconnect; and a puller that has archived the full settled-market history of the five macro series for the calibration study. Every instrument appends raw, receipt-timestamped (wall + monotonic) records to append-only JSONL tapes. The C++ collector and the analysis layer build on the same conventions.

## Results

Forthcoming. Every figure will state its sample size; null results will be reported as nulls.

## Limitations

Stated up front, before any results:

- **Latency floor.** Every recorded timestamp is the true event time plus a positive network delay, so measured adjustment times are upper bounds. The delay will be measured (RTT logging) and reported, not assumed away.
- **Clock error.** Wall-clock error will be bounded by logged NTP offset estimates; durations are computed on the monotonic clock.
- **Small live-capture sample.** Scheduled macro releases arrive roughly monthly per series; the number of live-captured events will be small, and confidence intervals will be wide and honest about it.
- **Single venue.** Results describe Kalshi, not prediction markets in general.
- **Liquidity, spreads, and fees.** Wide or one-sided books are an error bar on any mid-price, and any deviation must clear round-trip trading costs before it can be called exploitable rather than merely detectable.
- **Regime dependence.** A sample collected in one macro regime may not generalize; resampling methods quantify noise, not representativeness.

## Repository layout

| Path         | Contents                                                  |
| ------------ | --------------------------------------------------------- |
| `collector/` | Planned C++ collector daemon (not started yet)             |
| `analysis/`  | Data probes, capture instruments, API parsing; analysis layer forthcoming |
| `data/`      | Local captures — not tracked, see compliance below         |

## Data and compliance

Market data is collected through Kalshi's documented public API for non-commercial academic research. No trading, ever. **Raw market data is not included in this repository and is not redistributed in any form**; only aggregated statistical outputs (figures, tables, summary statistics) appear here, and the collection code lets anyone regenerate the underlying dataset themselves. Market data is provided by and attributed to [Kalshi](https://kalshi.com). Public market-data endpoints require no credentials; the collector's API credentials live in a local `.env` file, which is gitignored from the start.

## Pre-registration

The complete methodology — research questions, statistical machinery, exclusion rules, and the dated amendment log — is published in [METHODOLOGY.md](METHODOLOGY.md) **before any analysis results exist**. The public commit timestamp is deliberate: analysis choices (event-inclusion rules, price-source hierarchy, adjustment-time definitions, sensitivity grids) were fixed and published before any reliability curve or event-study figure was rendered, so they cannot have been tuned to the results.

## Setup

Windows:

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python analysis\snapshot_probe.py    # snapshot one live macro market
.venv\Scripts\python -m pytest                     # run the tests
```

macOS/Linux:

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python analysis/snapshot_probe.py
.venv/bin/python -m pytest
```

## License

MIT. The license covers the code and text in this repository; no license is granted to Kalshi market data, none of which is included here.
