# Kalshi Macro-Release Event Study — Methodology Reference

> **Purpose.** Complete operational specification of the study. A future reader should be able to implement the entire project from this document alone. This document governs *method*; process notes and working materials are archived privately.
>
> **Volatile facts.** Anything that depends on Kalshi's current API surface or fee schedule is never trusted from memory — see §8 (build-time verification list).
>
> **Pre-registration.** §6 fixes exclusion rules and analysis choices *before* results are seen. Changes after data collection begins require a dated amendment recorded in this file, with motivation, made before analyzing the affected data.

---

## 1. Object of study: Kalshi market mechanics

### 1.1 Binary contracts, price as probability
- Every contract is a yes/no question settling at **$1.00 (YES)** or **$0.00 (NO)**. Prices quoted in cents, 1–99.
- Expected-value argument: to a trader with subjective probability p, a YES contract is worth `1·p + 0·(1−p) = p` dollars. Buying below p and selling above p pushes the price toward the aggregate p.
- **Consequence:** the price is a falsifiable probability forecast. This is what makes RQ3 (calibration) definable at all — settlement provides the outcome y ∈ {0,1} against which the stated probability is scored.

### 1.2 Hierarchy: series → event → strike contracts
- **Series** = recurring question (e.g., monthly CPI). **Event** = one instance (June CPI). **Strikes** = the ladder of contracts on one event ("above 2.8%", "above 3.0%", "above 3.2%").
- All strikes of one event resolve on the **same underlying print** → their outcomes are strongly correlated → they are **not independent observations**. This drives the clustered bootstrap (§4.6) and the resampling unit everywhere (§3.3, §3.1).

### 1.3 Two timestamps per event: t₀ vs. settlement
- **t₀** = scheduled information release (BLS prints at 8:30:00 AM ET; FOMC statement at 2:00:00 PM ET). Uncertainty about the outcome effectively collapses at t₀.
- **Settlement** = Kalshi's formal resolution/payout, which may lag t₀ by hours.
- **Usage rules:**
  - RQ1 and RQ2 align all analysis on **t₀** (τ = t − t₀).
  - The outcome **y** comes from official **settlement**.
  - **Time-to-resolution (TTR)** for RQ3 is measured **to t₀**, not to settlement (post-t₀ prices are trivially ≈ 0/1 and would fake good calibration).
  - Observations at or after t₀ are **excluded** from the calibration sample.

### 1.4 Order book
- Central limit order book: resting limit orders at price levels; **best bid / best ask**; **mid = (best_bid + best_ask)/2** is the working probability estimate for all analyses.
- **Spread = ignorance interval.** Nobody currently believes strongly enough to trade inside it; the market's consensus lives somewhere within [bid, ask]. **Half-spread = the error bar on the mid.** Expect the spread to blow out in the seconds around t₀ (market makers pull quotes); the spread trajectory around t₀ is itself a reported result (§3.2).
- **Depth** (size resting at top k levels) is recorded to interpret jumps: a large price move on a thin book is one impatient trader, not necessarily information.

### 1.5 YES/NO duality — mandatory normalization
Kalshi exposes both YES and NO sides; they are two views of one economic book. All analysis operates in a **single consolidated YES-probability frame**.
- **Identity (prices in cents):** buying NO at q ≡ selling YES at (100 − q). Proof: buy NO at q → net (100 − q) if NO, (−q) if YES; sell YES at s → net s if NO, (s − 100) if YES; identical iff s = 100 − q.
- **Book merge rules:**
  - NO **bid** at q → YES **ask** at (100 − q)
  - NO **ask** at q → YES **bid** at (100 − q)
  - Consolidated YES best bid = max(native YES bids, converted NO-ask bids); consolidated YES best ask = min(native YES asks, converted NO-bid asks).
- **Trade normalization (price AND direction):**
  - `price_yes = price` if trade is YES-side, else `100 − price`
  - `aggressor_yes = +1` if aggressor **bought YES or sold NO** (upward pressure on YES probability); `−1` if aggressor **sold YES or bought NO**.
- Rationale: without direction normalization, the tape around t₀ cannot answer "who moved the price" (RQ2 interpretation).

### 1.6 Fees (benchmark for economic significance)
- Verified against the Kalshi Fee Schedule effective 2026-07-07 (https://kalshi.com/docs/kalshi-fee-schedule.pdf, reviewed in full 2026-07-08): per-order fees are **taker = round-up(M_t × 0.07 × C × P × (1−P))** and **maker = round-up(M_m × 0.0175 × C × P × (1−P))**, with C = contracts and P = price in dollars; maximal near 50¢, shrinking toward the extremes. No settlement fees.
- **Per-series multipliers for this study's series:** M_t = M_m = 1 for KXCPI, KXCPIYOY, KXPAYROLLS, KXFED, KXFEDDECISION (also KXU3, KXGDP, KXRATECUTCOUNT). **KXJOBLESSCLAIMS is absent from the schedule's non-standard table, so its maker multiplier defaults to 0 — resting orders are fee-free — while taker M = 1.** Fee statements must therefore always name the series AND the maker/taker role.
- Re-verify against the live schedule whenever fees are computed (§8); the schedule is cited by URL and never committed to the repository.
- Role in this study: fees + spread define the threshold any calibration deviation must exceed before it can be called *exploitable* rather than merely *statistically detectable* (§7).

---

## 2. Data architecture: the instrument

The collector is a scientific instrument. Every mechanism in this section exists to control or measure a source of error, and its logs are dataset metadata that travel into the writeup.

### 2.1 Principle: capture first, parse later
- The network thread does exactly two things per inbound message: **timestamp** (§2.5) and **append raw bytes to disk**. No parsing, no book-building in the hot path.
- The raw tape is **immutable** — the original observation is never modified or discarded.
- All parsing, book reconstruction, normalization, and analysis happen **downstream** and are **re-runnable**: a parser bug discovered in week five costs a re-derivation, not the dataset.

### 2.2 Two ingestion paths
- **REST:** market/series discovery; historical candles and **settled markets** (the RQ3 sample); the initial order-book **snapshot** on every (re)subscribe.
- **WebSocket:** live order-book **deltas** and **trades** — the only source with the resolution RQ1/RQ2 require (the entire adjustment happens inside a single historical candle).

### 2.3 Snapshot-then-delta replica
- On subscribe: receive full book snapshot once; thereafter receive only numbered deltas ("level 27¢ now holds 400").
- The collector maintains a **local replica** by applying deltas in sequence. Every mid, spread, and depth value in the study derives from this replica.
- **Integrity condition:** an **unbroken, correctly-numbered delta chain since the last verified snapshot**. This phrase is the answer to "how do you know your book was correct at t₀?"

### 2.4 Sequence-gap protocol (execute immediately on detection, ≤ 5 s)
On every message, check `seq == last_seq + 1`. On any violation:
1. **Mark the stream dirty** (replica no longer trusted).
2. **Log the gap:** wall time, expected vs. received sequence range, market ticker, connection id.
3. **Discard the replica** entirely.
4. **Request a fresh snapshot.**
5. **Rebuild and resume** delta application.

- **Never continue on a broken book.** A missed delta corrupts state silently — the book still renders plausibly and computes mids happily while being wrong forever after.
- The **gap log is analysis metadata**: it feeds the pre-registered exclusion rules (§6) and appears in the writeup's limitations section.

### 2.5 Dual-clock discipline
- **Stamp every inbound message with BOTH clocks at receipt, in the network thread, before parsing or queueing:**
  - `recv_wall_ns` — wall clock (`std::chrono::system_clock`), NTP-disciplined, can step/jump.
  - `recv_mono_ns` — monotonic clock (`std::chrono::steady_clock`), never jumps, arbitrary origin.
- **Usage rules:**
  - All **durations** (Tᵢ, dwell checks, backoff timing) → **monotonic only**.
  - All **alignment to t₀** (τ computation, calendar joins) → **wall clock**.
- Run **chrony/NTP**; **log its offset estimate periodically** (e.g., every 60 s) → clock error becomes a measured curve in the appendix, not an assumption.
- Known artifact if violated: a backward NTP step shortens (or makes negative) durations spanning the step and produces a **non-monotonic tape** that silently breaks dwell logic. The bias is small in magnitude (hundreds of ms against a seconds-to-minutes phenomenon) but **invisible** unless both clocks were stamped.

### 2.6 Connection resilience
- **Liveness detection:** WebSocket ping/pong (or venue heartbeat). A missed pong deadline ⇒ the connection is declared dead — silence alone is ambiguous (quiet market vs. zombie connection look identical).
- **Reconnect: exponential backoff with jitter.** Base 1 s, doubling (1, 2, 4, 8, …), capped ≈ 60 s, each wait randomized by a jitter fraction so mass reconnections don't synchronize.
- Every reconnect necessarily produces a **gap-log entry** (the downtime) and triggers the **snapshot-rebuild path** (§2.4) — one system, two triggers.
- **Preemptive refresh:** scheduled reconnect + fresh snapshot at **t₀ − 5 min** for every watched release, so each event begins on a fresh, verified chain (margin included in case the refresh itself hiccups).

### 2.7 Storage: required records
Container format: append-only JSONL for every collector-written stream, one file per stream per run/day (decided at project start; derived re-runnable layers use columnar formats). **What** is stored is fixed here:
- `raw_frames`: {recv_wall_ns, recv_mono_ns, conn_id, channel, market_ticker, seq (if present), raw_payload}
- `gap_log`: {wall_time, market, expected_seq, received_seq, conn_id}
- `conn_log`: {connect/disconnect/backoff events with both timestamps}
- `rtt_log`: {wall_time, RTT to API host} (§5.1)
- `clock_offset_log`: {wall_time, chrony offset estimate} (§2.5)
- **Derived, re-runnable layers** (stamped with parser git hash):
  - `quotes_yes`: {event_id, market, wall, mono, τ, bid_yes, ask_yes, mid_yes, spread, depth_top_k}
  - `trades_yes`: {event_id, market, wall, mono, τ, price_yes, size, aggressor_yes}
  - `events`: {event_id, series, t₀, settlement_ts, outcome_y, strike list, primary_contract (§3.0)}
- **Reproducibility:** all RNG seeds (bootstrap, permutation) fixed and logged; derived data versioned by parser hash.

### 2.8 Release calendar
- Ingest official schedules (BLS release calendar for CPI and the employment situation; Federal Reserve calendar for FOMC) into the `events` table with exact t₀ per event.
- The calendar drives: the collector watchlist, the preemptive-refresh schedule (§2.6), τ-alignment, and control-window eligibility (§3.1).

---

## 3. Research questions: full operational specifications

### 3.0 Common preprocessing
- All series in the consolidated YES frame (§1.5); all event-relative times as **τ = t − t₀(event)**.
- **Primary contract per event (for RQ1, RQ2, RQ4):** the strike whose mid at **τ = −H** (start of the RQ1 window) is **closest to 50¢**; ties broken by higher prior-day volume.
  - Rationale: near-extreme strikes cannot move much and carry little information; the at-the-money strike is maximally sensitive.
  - Selection uses **only information available before the analysis windows open** (no conditioning on the outcome).
  - Robustness check: repeat RQ2 headline numbers on the second-nearest strike.

### 3.1 RQ1 — Pre-announcement drift
- **Statistic per event:** `Dᵢ = pᵢ(−ε) − pᵢ(−H)` — the mid change over the final pre-release window. Defaults: **ε = 60 s**, **H = 4 h**. Sensitivity: H ∈ {2 h, 4 h, 8 h}.
- **Leakage-sensitive quantity: |Dᵢ|.** Signed drift averages toward zero regardless (up- and down-drifts cancel); the anomaly, if any, is in the magnitude.
- **Matched control windows:** same contract, same wall-clock interval, on eligible quiet days: weekdays with **no scheduled release** in the interval ± H (checked against the §2.8 calendar). Up to **K = 5** nearest eligible days per event.
- **Test:** Δ = mean|D_event| − mean|D_control| (medians reported alongside). Uncertainty via **clustered bootstrap**: resample events *with their attached control windows*, B = 10,000, percentile 95% CI. Evidence of pre-announcement drift ⇔ CI on Δ excludes 0.
- **Endpoint data requirement:** valid quotes within a staleness limit at both window endpoints (interior micro-gaps are tolerable for a two-endpoint statistic; see §6).
- **Interpretation guardrail:** drift ≠ proof of leakage (public-information repricing and position-squaring are alternative explanations). Reported as an anomaly measurement, not an accusation.

### 3.2 RQ2 — Post-release adjustment speed
- **Settled-down level:** `p∞ = median(mid over τ ∈ [+10 min, +30 min])`.
- **Adjustment time:** `Tᵢ = min{ τ > 0 : |p(τ′) − p∞| ≤ δ for all τ′ ∈ [τ, τ + w] }` with **δ = 5¢**, **w = 30 s** (the dwell requirement kills overshoot flickers).
- **Clock rule:** Tᵢ computed from **monotonic** timestamps (§2.5).
- **Outputs:** ECDF of {Tᵢ} (§4.2); median with bootstrap 95% CI (§4.4); the **spread trajectory** around t₀ (mean spread vs. τ with bootstrap band) reported as a companion result.
- **Mandatory sensitivity grid:** δ ∈ {3, 5, 10}¢ × w ∈ {10, 30, 60} s × p∞ window ∈ {[+10, +30], [+15, +45]} min → table of medians. Conclusions must be **qualitatively stable** across the grid; instability is itself a finding to report.
- **Edge cases (specified in advance):**
  - **Right-censoring:** if no dwell by τ = +30 min, record `T > 30 min` (censored), report the censored count; the median is reported only if < 50% of events are censored.
  - **No-move events:** if `|p∞ − p(t₀ − ε)| < δ`, the event is uninformative for T (the "adjustment" is smaller than the threshold); counted and reported separately, excluded from the T sample.

### 3.3 RQ3 — Calibration by time-to-resolution
- **Sample:** all settled markets in the target macro series (CPI, employment situation, FOMC; expandable to adjacent series for sample size, reported separately if so).
- **Observation grid (TTR measured to t₀):** {30 d, 14 d, 7 d, 3 d, 1 d, 12 h, 4 h, 2 h, 1 h}. At each gridpoint take the last available price at or before that time, subject to staleness limits: within 24 h for day-scale points; within 2 h for 12 h; within 15 min for the hour-scale points. Skip the (contract, gridpoint) pair if no fresh-enough price exists.
- **Price source honesty:** live-collected observations use consolidated **mids**; historical observations (pre-collector) use **candle closes / last-trade prices** because historical books cannot be reconstructed — a fidelity difference (trade prices bounce between bid and ask) flagged in the writeup.
- **Pairs (p, y):** p = observed price as probability; y = settlement outcome. Post-t₀ observations excluded (§1.3).
- **Calibration curves:** per TTR band, fixed 10¢ buckets; plot realized YES frequency vs. mean stated probability; annotate per-bucket n; a bucket is plotted only if **n ≥ 25**.
- **Brier score:** `BS = (1/N) Σ (pᵢ − yᵢ)²`. Reference points: perfect foresight → 0; constant 0.5 → 0.25.
- **Decomposition (Murphy):** with buckets k of size n_k, mean forecast p̄_k, realized frequency f_k, and base rate ȳ:
  - `Reliability = Σ (n_k/N)(p̄_k − f_k)²` (miscalibration; 0 is perfect)
  - `Resolution  = Σ (n_k/N)(f_k − ȳ)²` (boldness that pays)
  - `Uncertainty = ȳ(1 − ȳ)` (irreducible; property of reality)
  - `BS = Reliability − Resolution + Uncertainty`
  - Derivation (one-bucket identity `Σ(p̄ − y)² = n[(p̄ − f)² + f(1 − f)]`, then sum over buckets) is a required whiteboard exercise under the defensibility protocol.
- **Hypothesis:** as TTR shrinks, **Resolution rises** (forecasts sharpen toward 0/1) while **Reliability stays ≈ 0** (calibration holds).
- **Uncertainty on everything: clustered bootstrap** — resample **events** (each carrying all its strikes and all their observations), B = 10,000, percentile bands on curves, Brier, and each decomposition term.
  - Rationale: strikes within an event share the print; contract-level resampling pretends each strike is fresh information → **anti-conservative** (falsely tight) error bars.

### 3.4 RQ4 — Surprise-conditioned comparison (post-Milestone-1 stretch)
- **Surprise proxy per event:** `Sᵢ = |p∞ − pᵢ(t₀ − ε)|` on the primary contract (size of the realized jump; no external economics feed required).
- **Grouping:** median split into high-/low-surprise (terciles top-vs-bottom if n permits).
- **Primary test:** `Δmed = median(T | high S) − median(T | low S)`; bootstrap within groups, B = 10,000, 95% percentile CI on Δmed; groups differ ⇔ CI excludes 0.
- **Robustness (permutation test):** pool all T, shuffle group labels 10,000×, recompute Δmed each time; `p = fraction of |Δmed_perm| ≥ |Δmed_observed|`.
- **Known confound (flagged in advance):** T and S derive from the same path, and with fixed δ a larger jump has mechanically farther to travel. Robustness definition: **fractional adjustment time** `T90ᵢ = min{τ : |p(τ) − p(t₀−ε)| ≥ 0.9·Sᵢ, with dwell w}` — measures speed relative to the event's own move size. Report both.
- **Secondary cut ("does the market learn over time"):** early-half vs. late-half of the capture period, same machinery.

---

## 4. Statistical machinery

### 4.1 Event alignment
τ = t − t₀(i) puts every event on a common clock where τ = 0 is the information arrival. Stacking aligned paths and averaging is the **event study** — the engine of RQ1/RQ2. Patterns invisible in one noisy event emerge in the average across repetitions.

### 4.2 Empirical CDF (ECDF)
- `F̂(t) = #{i : Tᵢ ≤ t} / n` — the fraction of events adjusted by time t. A staircase from 0 to 1, jumping 1/n at each observation.
- Percentiles read directly: median = where the staircase crosses 0.5.
- **Why ECDF over histogram at small n:** zero tuning parameters (a histogram at n ≈ 10–15 is mostly an artifact of bin choice) and every data point stays visible. Small-n honesty is a design principle of this study.

### 4.3 Law of Large Numbers (the one theorem used)
"Proportions stabilize": as n grows, F̂(t) → F(t) = P(T ≤ t), the long-run fraction under infinite repetition. The ECDF is a finite-n approximation of the truth; the bootstrap quantifies *how* approximate.

### 4.4 Bootstrap (plug-in principle)
- **Problem:** the error bar on a statistic means the spread of that statistic across hypothetical re-runs of the whole experiment — which cannot be re-run.
- **Move:** the ECDF is the best available estimate of the true distribution, so simulate re-runs by sampling from it: **draw n values from the observed data with replacement** (with replacement is essential; without it every "re-run" is the same sample shuffled).
- **Recipe:** resample size n with replacement → compute the statistic → repeat **B = 10,000** → the middle 95% of the B values (2.5th–97.5th percentiles) is the 95% CI.
- Works identically for median, mean, any percentile, any computable statistic — same four lines of code.
- **Pointwise bands on averaged paths:** resample *events*, recompute the mean path, take per-τ percentiles.
- **Explicit limitations (stated wherever used):**
  1. **Precision, not accuracy** — quantifies sampling noise; cannot detect or fix bias (a sample from an unrepresentative regime yields a tight interval around the wrong answer).
  2. **Independence assumption** across resampled units — plausible for releases weeks apart; **false for strikes within an event** (→ §4.6).
  3. **Unreliable for extreme statistics** (min/max) at small n.
  4. At small n, resampled medians take few distinct values → **lumpy CIs**; expected, reported, not hidden.

### 4.5 Reporting rules
Every figure states its **n** (and cluster count where applicable); every CI states **B** and the method (percentile bootstrap); all seeds logged (§2.7).

### 4.6 Clustered bootstrap
The **resampling unit is the event**, never the contract or the observation, whenever observations share an event (RQ1 controls, RQ3 strikes/gridpoints). Resampling below the cluster level is anti-conservative.

### 4.7 Permutation test
For two-group comparisons (RQ4): pool observations, randomly reassign group labels, recompute the group difference; repeat 10,000×; the p-value is the fraction of permuted differences at least as extreme as observed. Complements the bootstrap CI as a cheap robustness check.

---

## 5. Error budget

### 5.1 Latency floor
- Delay chain: matching engine → (possible venue-side coalescing/batching — verify, §8) → internet → kernel → network-thread stamp. Every recorded time = true time + L with **L > 0** (a message can only arrive after it happens; delay adds, never subtracts).
- **Consequence:** measured adjustment times are **upper bounds** on true adjustment times.
- **Measurement, not assumption:**
  - RTT to the API host logged every 60 s (`rtt_log`).
  - If messages carry server-side timestamps (verify, §8): histogram of `(recv_wall − server_ts)` across all captured messages — an empirical bound bundling network latency and residual clock offset. Appendix figure.
- **Writeup framing (verbatim):** "Measured adjustment times are upper bounds accurate to within our empirically measured latency floor of ~X ms, negligible at the seconds-to-minutes scale of our findings."
- If venue coalescing exists, the floor = max(latency, batch interval), stated as such.
- **REST polling instrument (rehearsal/redundancy captures):** its resolution floor is the **1 Hz polling cadence** (round-robined across watched events), stated wherever poller-sourced observations are used. Each poller response records the server's HTTP `Date` header, which doubles as a per-request clock-offset log **with 1-second resolution** — sufficient to catch multi-second skew, not sub-second offsets; the WebSocket instruments' server-side `ts_ms` stamps carry the finer latency histogram.

### 5.2 Clock error
- Wall-clock error bounded by logged NTP-discipline offsets (§2.5) — a measured curve, not a claim. Current Windows deployment: `w32tm` resync per the capture runbook + pre/post `stripchart` samples per event; continuous per-response `Date`-header offsets (1-second resolution, §5.1) and periodic in-instrument clock/latency events fill the between-resync record. (chrony applies to any future Linux deployment.)
- Durations protected by the monotonic clock.
- **Constant offsets cancel in differences:** a constant lag in t₀ (or in the feed) shifts every event's measured T identically — it slides the ECDF without distorting cross-event comparisons, so RQ4 (and any between-group contrast) is immune to it.

---

## 6. Pre-registered exclusion and inclusion rules

Fixed now, before any results are visible. Rationale: exclusions chosen after seeing outcomes let motivated reasoning in through the data-quality door; criteria fixed in advance cannot correlate with outcomes.

- **RQ2 inclusion:** the event's **primary contract** has an **unbroken sequence chain from t₀ − 10 min to t₀ + 30 min** (verified against the gap log). Otherwise the event is excluded **from RQ2 only**.
- **RQ1 inclusion:** valid (non-dirty, staleness-compliant) quotes at both window endpoints (−H and −ε). Interior micro-gaps tolerated (two-endpoint statistic).
- **RQ3:** gaps are irrelevant at snapshot resolution; events remain in the calibration sample regardless of RQ2/RQ1 status. Post-t₀ observations excluded (§1.3).
- **No-move events** (|jump| < δ): excluded from the T sample, counted and reported (§3.2).
- **Every exclusion is logged with its reason; counts appear in the writeup.**
- The same defect can be fatal to one RQ and irrelevant to another — exclusion rules are **per research question**, never global.

---

## 7. Statistical vs. economic significance

- A detected deviation (e.g., a calibration gap of 2¢ in a bucket) is **statistically real** if its clustered-bootstrap CI excludes zero — and **economically exploitable** only if it exceeds the **round-trip cost** of trading it:
  - `round-trip cost ≈ fee(P)_entry + fee(P)_exit + half-spread_entry + half-spread_exit`, with fee(P) from the live schedule (§1.6, §8) and spreads measured from the data at the relevant TTR.
  - Round-trip costs are **series- and role-specific** (per-series multipliers, §1.6): every exploitability statement names the series and the maker/taker assumption. Notably, KXJOBLESSCLAIMS maker-side round trips carry zero fees, so its exploitability threshold is spread-only on that side.
- Both statements are reported; conflating them is the canonical amateur error. A visible-but-not-eatable deviation is a *finding about market efficiency*, not a trade.
- No trading is performed in this project under any circumstances (a standing rule of this project); the exploitability calculation is analysis, not action.

---

## 8. Build-time verification list (volatile facts — never from memory)

Verify against live Kalshi documentation each time the integration layer is touched:
1. WebSocket channel names, subscription and auth flow.
2. Snapshot mechanism and delta sequence-number semantics.
3. Whether messages carry **server-side timestamps** (enables the §5.1 histogram).
4. Whether the venue **coalesces/batches** book updates, and at what interval.
5. REST endpoints and granularity for historical candles and **settled markets** (the RQ3 sample).
6. Rate limits (shape the REST poller and backoff behavior).
7. **Fee schedule**: current coefficients for taker/maker and any per-category variants.
8. Settlement/resolution timestamp and outcome fields.
9. Official release-calendar sources and formats (BLS, Federal Reserve).

---

## 9. Amendments (dated; see preamble for the amendment rule)

### A1 — 2026-07-08: Release markets close before t₀; RQ2 measured on response contracts

**Status: pre-data.** No live release has been captured yet; this amendment precedes all capture and all analysis.

**Fact (verified against the live API 2026-07-08):** Kalshi closes every macro ladder before the scheduled release, by rule ("The market will always close at 8:25 AM ET on the scheduled day of the data release"). Measured close offsets: KXCPI and KXFED close at **t₀ − 5 min**; KXCPIYOY, KXPAYROLLS, and KXFEDDECISION at **t₀ − 1 min**. Confirmed on all open events and on the settled June-17 FOMC ladders (closed 17:55Z/17:59Z vs. the 18:00Z announcement).

**Motivation:** §3.2 as pre-registered measures post-t₀ adjustment on the event's own primary contract — which is a closed book at t₀. The phenomenon is observable instead on sibling events that remain open through the print and reprice on it (verified live: on 2026-07-14 at 12:30Z, KXCPI-26JUL, KXCPIYOY-26JUL, KXPAYROLLS-26JUL, KXFED-26JUL, KXFEDDECISION-26JUL all trade through the release).

**Amendment:**
1. **RQ2 response contracts.** RQ2 statistics (Tᵢ, p∞, dwell, spread trajectory) are computed on a designated response contract per release: **primary** = the ATM strike (mid closest to 50¢ at τ = −10 min, higher volume breaking ties, no outcome conditioning) of the **nearest-closing OPEN event in the same series family** — CPI print → next KXCPIYOY event; jobs print → next KXPAYROLLS event; FOMC → next KXFED meeting event. **Secondary** (reported alongside): the ATM strike of the nearest open KXFED event, for CPI and jobs prints (the policy-expectations channel).
2. **RQ1 endpoint.** RQ1 stays on the release's own ladder; the near endpoint moves from τ = −ε to **60 s before the scheduled close** (τ_close = −5 or −1 min per series, recorded per event): Dᵢ = pᵢ(τ_close − 60 s) − pᵢ(−H). The spread trajectory into the trading halt is a reported companion result.
3. **Inclusion rule mapping.** The §6 RQ2 unbroken-chain requirement applies to the **response contract's** feed over [t₀ − 10 min, t₀ + 30 min].
4. **Instrument provenance.** Every tape record carries its instrument schema (`ws_recorder/1`, `release_poller/1`, C++ collector schemas later); the instrument(s) behind each captured event are reported in the writeup. Jul-14 capture is rehearsal-grade (Python instruments); the C++ collector is the production instrument from the Jul-29 FOMC onward.
5. **RQ3 unaffected** (all TTR gridpoints are ≥ 1 h before t₀, well before any close).

### A2 — 2026-07-08: RQ2 re-anchored on the forward Fed ladder; fractional adjustment metric; cross-market-diffusion framing

**Status: pre-data.** No live release has been captured. Adopted following an independent methodological review (archived privately); supersedes A1's instrument designation while keeping A1's RQ1 endpoint, inclusion-rule mapping, and instrument-provenance items.

**Framing.** The halt discovery converts RQ2 from "how fast does the market for X price X" into **cross-market information diffusion**: how fast a scheduled release propagates into pre-registered *response instruments* that verifiably trade through it. This is the prediction-market analogue of the fed-funds-futures announcement tradition (Kuttner 2001; Gürkaynak–Sack–Swanson 2005) and the real-time macro-announcement literature (Ederington–Lee 1993/1995; Andersen–Bollerslev–Diebold–Vega 2003). We do NOT estimate Hasbrouck information shares or any VAR/cointegration model; that literature is cited as motivation only. We do not call this a trading-halt study of the primary contract.

**Pre-registered instruments and release→instrument mapping (fixed before inspecting any post-release path; all series verified live via API 2026-07-08):**
- **CPI / jobs report / weekly jobless claims → PRIMARY:** the nearest-open **KXFED** meeting ladder and the same meeting's **KXFEDDECISION** market (both reported). **SECONDARY (triangulation):** the next-period same-series ladder, and the same-day **KXINXU** (S&P 500 at 10 am) event — chosen over KXINX (4 pm) for 8:30 releases because its shorter horizon tightens attribution.
- **FOMC decision → PRIMARY:** the **next** meeting's KXFED/KXFEDDECISION ladder (the announcing meeting's own market expires at the first 2:05 PM ET after the statement). **SECONDARY:** same-day KXINX (4 pm range).
- **Weekly jobless claims (KXJOBLESSCLAIMS, closes 8:25 ET before the 8:30 print — verified live) are added to the event set** for statistical power (~weekly observations).
- Response-contract strike selection: ATM rule at τ = −10 min (mid closest to 50¢, volume tiebreak, no outcome conditioning — A1 rule unchanged).

**Operational RQ2 definition (drop-in, adopted):** For each release event e and response instrument i, let mᵢ(t) be the consolidated order-book mid, t₀ the official release timestamp, mᵢ(t₀⁻) the last mid strictly before t₀, and mᵢ* the settled-down level = mean mid over the **stability window [+20, +30] min**, admissible as "trendless" iff the means of the window's two halves differ by ≤ 1 median half-spread. Realized move Δᵢ = mᵢ* − mᵢ(t₀⁻). An event–instrument pair is **admissible** iff |Δᵢ| ≥ max(**δ_floor = 3¢**, **k = 2** × median pre-release half-spread, measured over [−60, −10] min). For admissible pairs the **fractional adjustment time** is τᵢ,p = min{ t − t₀ : |mᵢ(t) − mᵢ(t₀⁻)| ≥ p·|Δᵢ| and mᵢ stays within **one half-spread** of that level through the stability window }, for **p ∈ {50%, 90%, 95%}**. Report the ECDF of τᵢ,p with clustered-bootstrap bands, **per instrument** (never pooled across instruments); report the excluded small-move fraction per instrument and analyze excluded events separately ("the release was not news for this instrument"). Time-weighted quoted spread and depth-within-5¢ reported as covariates. **Sensitivity grid (mandatory, §3.2 tradition):** δ_floor ∈ {2, 3, 5}¢ × k ∈ {1.5, 2, 3} × stability window ∈ {[+15, +25], [+20, +30]} min.
- Rationale for the fractional metric: attenuation on a correlated instrument rescales Δᵢ but not τᵢ,p — the metric never needs the ex-ante "correct" price, only the instrument's own realized repricing (Ederington–Lee logic).

**RQ1 (updated by A1, refined here):** drift windows terminate at each event's **API-recorded `close_time`** (captured per event — Kalshi can adjust it; the `market_lifecycle_v2` `close_date_updated` event exists for this). Control windows truncate at the same clock time. New sub-question **RQ1b (pre-close informativeness):** how much of the eventually-realized outcome is already priced at the halt — reported as a complement to RQ2, not a replacement.

**RQ4 (updated):** surprise is measured **exogenously**: Sᵢ = (actual print − consensus median) / historical σ of that series' surprises (ABDV/Kuttner convention). Consensus source: TODO — must be a citable, freely accessible archive (candidates to verify; Bloomberg is not accessible). **Pre-registered co-proxy** (usable regardless): the change in the own-ladder implied probability from τ = −4 h to the halt. Both reported when available.

**A2 citations (defense set):**
1. Ederington & Lee (1993, *J. Finance* 48(4):1161–1191; 1995, *JFQA* 30(1):117–134) — adjustment-speed benchmarks on correlated futures ("bulk within the first minute"; "basically completed within 40 seconds").
2. Kuttner (2001, *J. Monetary Economics* 47:523–544) — reading news off a forward/correlated instrument; the intellectual justification for the response-contract design.
3. Andersen, Bollerslev, Diebold & Vega (2003, *AER* 93:38–62) — real-time price discovery on correlated prices around macro announcements; sign/size effects for RQ4.
4. Gürkaynak, Sack & Swanson (2005) — 30-minute windows around FOMC on rate futures.
5. Hasbrouck (1995, *J. Finance* 50:1175–1199) — cross-market price discovery; **motivation only, no estimation**.
6. Diercks, Katz & Wright (2026, NBER WP 34702) — Kalshi macro contracts: Fed-ladder liquidity, news-day distributional moves, accuracy vs. fed funds futures; documents daily/qualitative responsiveness only → our intraday order-book path is the novel contribution.
7. Angelini & De Angelis (2026, arXiv 2606.07811) — Kalshi prices underreact on impact (~0.64-for-one) with liquidity-dependent drift; the direct high-frequency hypothesis our capture can test.

### A2-final — approved and locked 2026-07-08 (supersedes the draft parameters in A2 above)

Pre-registered and locked. Any change requires a dated amendment recorded before analyzing affected data.

1. **Spread scale s̄ (single definition, used everywhere A2 references spreads):** the median ATM half-spread of the instrument over **[t₀ − 60 min, t₀ − 5 min]** — the window ends at −5 min to exclude anticipatory widening into the halt/release.
2. **Admissibility (PRIMARY cell):** an event–instrument pair enters the τ sample iff |Δ| ≥ max(**δ_floor = 3¢**, **k·s̄ with k = 2**).
3. **Settled-down level:** m* = **MEDIAN** mid over the stability window (median convention consistent with §3.2). Primary stability window **[+20, +30] min**. Trendless check: |median(first half) − median(second half)| ≤ **1·s̄**.
4. **Failure handling (slide rule):** if the trendless check fails at the primary window, slide once to **[+30, +40] min**; if it fails again, the event is marked **"unsettled"** — excluded from τ estimates, counted, and reported.
5. **FOMC exception:** the press conference begins 2:30 PM ET, so FOMC stability windows may **never extend past t₀ + 29 min**; the slide rule is **disabled**; FOMC primary window = **[+15, +25] min**; trendless failure → unsettled.
6. **Metric:** τ_p = min{ t − t₀ : |m(t) − m(t₀⁻)| ≥ p·|Δ| and m stays within 1·s̄ of that level through the stability window }. **τ_50 and τ_90 are co-primary**; τ_95 reported. **Writeup note (mandatory wherever τ_90/τ_95 appear):** at |Δ| near δ_floor, tick quantization compresses τ_90/τ_95 toward the completion time.
7. **Sensitivity grid (appendix only; headline numbers always from the primary cell):** δ_floor ∈ {3, 5, 10}¢ × k ∈ {1, 2, 3} × stability window ∈ {[+15,+30], [+20,+30], [+30,+40]} (FOMC rows truncated at +29 min per item 5). Criterion: conclusions must be qualitatively stable across cells; instability is itself a reported finding.
8. **Item-7 pre-lock verification record (2026-07-08, quiet-period data only, no post-release paths — none exist):** From the 10-min 1 Hz dress-run tape (6 days before the nearest release): KXFED-26JUL ATM half-spread median **0.5¢** (IQR degenerate at 0.5¢), KXFEDDECISION-26JUL **0.5¢**, own-ladder KXCPI-26JUN **1.0¢** → on the primary instruments the 3¢ floor binds and k = 2 is comfortably satisfied. Overnight (~02:40 ET) single snapshots of secondaries: KXCPIYOY-26JUL 9.5¢, KXCPI-26JUL 42.5¢ (near-empty book a month out), KXINXU 3.5¢, KXJOBLESSCLAIMS-26JUL09 6.5¢ — wide off-hours books, the exact regime the admissibility filter and per-instrument reporting are designed to handle; production s̄ is measured from the [−60, −5] window on release morning. **Spread regime consistent with assumptions on the primary → no revision proposed; parameters locked as approved.**

**A2 caveats (carried into the writeup's limitations):**
- DKW's intraday evidence is qualitative/daily; in one comparison Kalshi adjusted *more gradually* than OIS/futures — no claims of Kalshi speed leadership; the contribution is measurement.
- The Fed ladder's reaction to CPI/jobs is state-dependent (a predictable rate path shrinks the admissible sample) — that is why triangulation with index-range markets is built in, and why weekly claims are added for power. Pre-registered promotion rule: if fewer than ~40–50% of events are admissible on the primary, promote KXINXU/KXINX to co-primary rather than adding noisier instruments.
- Index-range binaries are multi-driver: they reprice on all equity news, not just the release — attribute only within a tight window around t₀; corroboration, not primary.
- Close-time strings came from indexed page content; **each event's close_time is verified from the REST API at capture time** and tabulated (a reportable methods contribution).
- Strictly a measurement study: any post-release drift is checked against bid–ask costs (§7) before efficiency language; never presented as a tradable strategy.
- Event counts are small per series within one semester; weekly claims and continued capture into 2027 are the power plan.

### A3 — 2026-07-08: RQ3 historical price-source hierarchy (approved and locked)

**Status: locked before any reliability curve has been inspected.** As of this recording, no calibration curve, reliability diagram, or bucket table has been rendered from this dataset by anyone or anything; the acquisition pipeline does not yet exist. Any exploratory calibration output produced during subsequent pipeline development will be disclosed here by date.

§3.3's "candle closes / last-trade prices" for pre-collector observations is refined into the following locked hierarchy, per (contract, gridpoint), applied in order:
1. **MID** — mid of `yes_bid.close` / `yes_ask.close` from the gridpoint candle. Admissible only if: both sides present (no sentinels), not crossed/locked (bid < ask), and **spread ≤ 10¢** (one calibration bucket width).
2. **TRADE** — `price.close` of the most recent candle within the gridpoint's §3.3 staleness limit, used when MID is uncomputable or fails the spread cap.
3. **SKIP** — no observation otherwise.

Every observation is tagged MID or TRADE; headline curves use the hierarchy. **Spread-cap sensitivity {5¢, 10¢, 20¢}: appendix only; headline = 10¢.** Required reporting per TTR band × price bucket: n, source composition (% MID / % TRADE), median spread of admitted MID observations. **Required artifact-check figure:** extreme-bucket reliability (<10¢, >90¢) compared between the hierarchy and a TRADE-only version; divergence is interpreted as a quoting artifact, not miscalibration. **Era tag** on every observation (pre-collector candle vs live book mid); if source composition shifts across eras, calibration by era in the appendix. Clustered bootstrap unchanged (resampling unit = event).

**Sentinel verification (required pre-lock check, performed 2026-07-08 on live candle data):** 478 hourly candles across 6 markets spanning both eras (2021 legacy + 2026 KX; OTM and 99¢-pinned ITM): zero nulls in `yes_bid.close`/`yes_ask.close`; **absent-bid = "0.0000"** (181+ occurrences on OTM books) and **absent-ask = "1.0000"** (48 consecutive occurrences on a 99¢-pinned market) confirmed as the sentinels; one stray `yes_ask.close = "0.0000"` observed (an empty candle with both sides zero — a degenerate no-quotes encoding). Implementation therefore requires **both sides in [1¢, 99¢]** before a MID is computed, which subsumes all observed sentinel and degenerate encodings.

### A3.1 — 2026-07-08: supersedes A3 (post-independent-review revision)

An independent methodological review of the A3 hierarchy (archived privately) completed 2026-07-08. A3's operative content — the MID→TRADE→SKIP hierarchy, sentinel handling, 10¢ spread cap, crossed/locked guard, {5,10,20}¢ sensitivity grid, per-band source-composition reporting, extreme-bucket artifact-check figure, era tagging, and event-clustered bootstrap — is confirmed and carried forward unchanged. A3.1 adds two requirements:

1. **Verification diagnostics attached to every derived-dataset build:** (i) the fallback-usage fraction (share of observations sourced TRADE rather than MID); (ii) the spread distribution of admitted MID observations; (iii) the count of sentinel-sided candles encountered at gridpoint selection.
2. **Recorded rationale for the spread cap:** near the price boundaries the spread is structurally asymmetric — it cannot extend past 1¢ or 99¢ — so wide-quoted mids are mechanically dragged toward 50¢. Left uncapped, this manufactures a spurious favorite-longshot miscalibration pattern in the reliability curve. The cap, together with the artifact-check figure, is what makes the headline curve defensible against the quoting-artifact objection.

Integrity note: as of this recording, no reliability curve, diagram, or bucket table has been rendered or inspected; A3.1 locks under the same pre-inspection discipline as A3.

**Implementation note (2026-07-08, recorded before any reliability curve was inspected):** the exact Murphy identity BS = REL − RES + UNC holds when all forecasts within a bucket are identical; with continuous prices binned into fixed 10¢ buckets, the within-bucket cross-term does not vanish. Implementation therefore reports the **raw** Brier score, the decomposition terms computed from bucket quantities exactly as §3.3 defines them — which decompose the **bucket-discretized** score BS* = (1/N)Σ(p̄_bucket(i) − yᵢ)² to machine precision (asserted in code) — and the raw-vs-discretized gap per band, stated on the figure.

---

## 10. Execution order (cross-reference: project milestones)

1. **Immediately, in parallel:** collector build (§2) + **RQ3 on historical settled data** (needs no collector; Milestone-1 workhorse).
2. **As live capture accumulates:** RQ1 and RQ2 (each captured release adds one observation; ECDFs and CIs update).
3. **Post-Milestone-1:** RQ4 (same data, one additional analysis notebook).
4. **Writeup limitations checklist** (the union of the three error domains): liquidity, spreads, and fees; small n, clustering, and censoring; gaps, latency floor, clock error, and single-venue scope; sample-period regime dependence (bootstrap cannot correct for an unrepresentative summer).
