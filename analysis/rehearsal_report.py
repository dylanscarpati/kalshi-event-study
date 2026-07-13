"""July-9 claims rehearsal analysis -> private/rehearsal-report-jul09.md.

Aggregate-only console output (data-governance rule #10): exact per-strike
values, price levels, and the price-path figure go into the PRIVATE report;
the console and any AI-assisted session see only counts, medians, verdicts.

Components: (a) RQ1b pre-close informativeness on the claims ladder;
(b) poller-vs-recorder fidelity at matched timestamps; (c) venue coalescing
+ latency (closes methodology section-8 item 4); (d) A2 response-instrument
dry run on the KXFED ladder; (e) price-path figure (rendered, not displayed).

Usage: python analysis/rehearsal_report.py [--synthetic-figure]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import urllib.request
from pathlib import Path

from replay import ReplayResult, replay_tape

T0 = dt.datetime(2026, 7, 9, 12, 30, tzinfo=dt.timezone.utc)  # claims 8:30 ET
T0_NS = int(T0.timestamp() * 1e9)
CLOSE_NS = T0_NS - 5 * 60 * 10**9  # ladder closed 12:25Z
CLAIMS_EVENT = "KXJOBLESSCLAIMS-26JUL09"
FED_EVENT = "KXFED-26JUL"
MIN = 60 * 10**9

INK = "#0b0b0b"
INK2 = "#52514e"
SURFACE = "#fcfcfb"
BLUE = "#2a78d6"
AQUA = "#1baf7a"


def find_tapes() -> dict[str, Path]:
    """Identify tier tapes by the tickers in their run_start events."""
    out = {}
    for ev_path in Path("data/ws").glob("20260709*.events.jsonl"):
        start = json.loads(ev_path.read_text(encoding="utf-8").splitlines()[0])
        tickers = start.get("tickers", [])
        key = "claims" if any(t.startswith("KXJOBLESSCLAIMS") for t in tickers) else "fed"
        out[key] = ev_path.with_name(ev_path.name.replace(".events.", ".frames."))
    return out


def fetch_outcomes(event: str) -> dict[str, float]:
    url = f"https://external-api.kalshi.com/trade-api/v2/markets?event_ticker={event}&limit=200"
    with urllib.request.urlopen(url, timeout=20) as r:
        body = json.loads(r.read().decode())
    return {m["ticker"]: 1.0 if m["result"] == "yes" else 0.0
            for m in body.get("markets", []) if m.get("result") in ("yes", "no")}


def sample_minutes(rep: ReplayResult, market: str, start_ns: int, end_ns: int) -> list:
    """As-of top-of-book sampled at 1-minute grid points."""
    return [q for t in range(start_ns, end_ns + 1, MIN)
            if (q := rep.asof(market, t)) is not None]


def a2_dry_run(rep: ReplayResult, market: str) -> dict:
    """A2-final admissibility on one response contract around t0."""
    pre = sample_minutes(rep, market, T0_NS - 60 * MIN, T0_NS - 5 * MIN)
    spreads = sorted(q.spread_c for q in pre if q.spread_c is not None and q.spread_c > 0)
    s_bar_half = (spreads[len(spreads) // 2] / 2) if spreads else None
    last_pre = rep.asof(market, T0_NS - 1)
    stability = [q.mid_c for q in sample_minutes(rep, market, T0_NS + 20 * MIN, T0_NS + 30 * MIN)
                 if q.mid_c is not None]
    result = {"market": market, "s_bar_half_c": s_bar_half,
              "pre_samples": len(pre), "stability_samples": len(stability)}
    if not (s_bar_half and last_pre and last_pre.mid_c is not None and len(stability) >= 5):
        result["verdict"] = "unmeasurable (insufficient samples)"
        return result
    m_star = statistics.median(stability)
    half1 = statistics.median(stability[: len(stability) // 2])
    half2 = statistics.median(stability[len(stability) // 2:])
    trendless = abs(half1 - half2) <= s_bar_half
    delta = m_star - last_pre.mid_c
    threshold = max(3.0, 2 * s_bar_half)
    result.update({"trendless": trendless, "abs_delta_c": abs(delta),
                   "threshold_c": threshold,
                   "admissible": trendless and abs(delta) >= threshold,
                   "delta_c": delta, "m_star_c": m_star, "pre_mid_c": last_pre.mid_c})
    result["verdict"] = ("ADMISSIBLE" if result["admissible"]
                         else "inadmissible (|delta| below threshold)" if trendless
                         else "unsettled (trendless check failed)")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-figure", action="store_true",
                        help="render the figure from rule-generated data for layout checking")
    args = parser.parse_args()

    tapes = find_tapes()
    rep_claims = replay_tape(tapes["claims"])
    rep_fed = replay_tape(tapes["fed"])
    print(f"replay: claims tape {rep_claims.frames_in} frames, "
          f"{len(rep_claims.anomalies)} anomalies; fed tape {rep_fed.frames_in} frames, "
          f"{len(rep_fed.anomalies)} anomalies")
    window_ok = {
        "claims [-90m, close]": rep_claims.clean_window(T0_NS - 90 * MIN, CLOSE_NS),
        "fed [-60m, +30m]": rep_fed.clean_window(T0_NS - 60 * MIN, T0_NS + 30 * MIN),
    }
    print("inclusion windows clean:", window_ok)

    report = ["# Rehearsal report — 2026-07-09 weekly jobless claims (private)",
              "", f"t0 = {T0.isoformat()}  |  ladder close = t0 - 5 min", ""]

    # (a) RQ1b: pre-close informativeness on the claims ladder
    outcomes = fetch_outcomes(CLAIMS_EVENT)
    pairs = []
    for market, y in sorted(outcomes.items()):
        q = rep_claims.asof(market, CLOSE_NS - 1)
        if q and q.mid_c is not None:
            pairs.append((market, q.mid_c / 100.0, y))
    if pairs:
        brier = sum((p - y) ** 2 for _, p, y in pairs) / len(pairs)
        correct = sum(1 for _, p, y in pairs if (p >= 0.5) == (y == 1.0))
        print(f"(a) RQ1b: {len(pairs)} strikes with pre-close quotes; "
              f"close-price Brier={brier:.4f}; correct-side {correct}/{len(pairs)}")
        report += ["## (a) RQ1b — informativeness at the halt",
                   f"strikes quoted at close: {len(pairs)}/{len(outcomes)}  |  "
                   f"Brier at close: {brier:.4f}  |  correct side: {correct}/{len(pairs)}", "",
                   "| strike | mid at close | outcome |", "|---|---|---|"]
        report += [f"| {m} | {p:.3f} | {'YES' if y else 'NO'} |" for m, p, y in pairs]
        report.append("")

    # (b) fidelity: poller ladder tops vs recorder replay, matched timestamps
    print("(b) fidelity vs poller:")
    report += ["## (b) Cross-instrument fidelity (poller vs recorder replay)", ""]
    poller_tapes = sorted(Path("data/poller").glob("20260709*.jsonl"))
    from kalshi_rest import parse_market
    for event, rep in ((CLAIMS_EVENT, rep_claims), (FED_EVENT, rep_fed)):
        diffs, exact, matched = [], 0, 0
        for tape in poller_tapes:
            for line in tape.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("kind") != "response" or rec.get("fetch") != "ladder" \
                        or rec.get("target") != event or rec.get("http_status") != 200:
                    continue
                for m in json.loads(rec["body_text"])["markets"]:
                    pm = parse_market(m)
                    if pm.mid_cents is None:
                        continue
                    q = rep.asof(pm.ticker, rec["recv_wall_ns"])
                    if q is None or q.mid_c is None:
                        continue
                    matched += 1
                    d = abs(q.mid_c - pm.mid_cents)
                    diffs.append(d)
                    exact += d == 0
        if diffs:
            diffs.sort()
            line = (f"{event}: {matched} matched quotes, exact-agreement "
                    f"{exact / matched:.1%}, median |dmid| {diffs[len(diffs)//2]:.1f}c, "
                    f"p99 {diffs[int(len(diffs)*0.99)]:.1f}c")
            print("   ", line)
            report += [f"- {line}"]
    report.append("")

    # (c) coalescing + latency (section-8 item 4)
    print("(c) coalescing/latency:")
    report += ["## (c) Venue coalescing + latency (closes section-8 item 4)", ""]
    for name, tape_path in tapes.items():
        deltas_mono, lats = [], []
        prev_mono = {}
        for line in Path(tape_path).read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("direction") != "in":
                continue
            raw = json.loads(rec["raw"])
            if raw.get("type") != "orderbook_delta":
                continue
            sid = raw.get("sid")
            if sid in prev_mono:
                deltas_mono.append((rec["recv_mono_ns"] - prev_mono[sid]) / 1e6)
            prev_mono[sid] = rec["recv_mono_ns"]
            if raw.get("msg", {}).get("ts_ms"):
                lats.append(rec["recv_wall_ns"] / 1e6 - raw["msg"]["ts_ms"])
        if deltas_mono:
            deltas_mono.sort()
            lats.sort()
            burst = sum(1 for d in deltas_mono if d < 50) / len(deltas_mono)
            line = (f"{name}: {len(deltas_mono) + 1} deltas; inter-arrival median "
                    f"{deltas_mono[len(deltas_mono)//2]:.0f}ms, p95 "
                    f"{deltas_mono[int(len(deltas_mono)*0.95)]:.0f}ms, {burst:.1%} under 50ms; "
                    f"recv-ts_ms median {lats[len(lats)//2]:.0f}ms, p95 {lats[int(len(lats)*0.95)]:.0f}ms")
            print("   ", line)
            report += [f"- {line}"]
    report.append("")

    # (d) A2 dry run on the KXFED ATM contract
    atm = None
    best = None
    for market in rep_fed.tops:
        q = rep_fed.asof(market, T0_NS - 10 * MIN)
        if q and q.mid_c is not None:
            d = abs(q.mid_c - 50)
            if best is None or d < best:
                best, atm = d, market
    if atm:
        res = a2_dry_run(rep_fed, atm)
        print(f"(d) A2 dry run on {FED_EVENT} ATM: verdict = {res['verdict']}; "
              f"s_bar(half) present: {res['s_bar_half_c'] is not None}; "
              f"samples pre/stability: {res['pre_samples']}/{res['stability_samples']}")
        report += ["## (d) A2 response-instrument dry run (KXFED ATM)", "",
                   "```", json.dumps(res, indent=2, default=str), "```", ""]

    # (e) price-path figure -- rendered, never displayed in an AI session
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(True, color="#e8e8e6", linewidth=0.6, zorder=0)
    if args.synthetic_figure:
        import math
        xs = list(range(-90, 46))
        ax.plot(xs, [50 + 18 * math.tanh(x / 40) for x in xs], lw=2, color=BLUE,
                label="own-ladder ATM (synthetic)")
        ax.plot(xs, [42 + (6 if x > 0 else 0) + x / 30 for x in xs], lw=2, color=AQUA,
                label="response ATM (synthetic)")
        out_name = "private/fig-layout-check-SYNTHETIC.png"
    else:
        claims_atm = max(rep_claims.tops,
                         key=lambda m: len(rep_claims.tops[m]))
        for market, rep, color, label in ((claims_atm, rep_claims, BLUE, "claims ladder (most active strike)"),
                                          (atm, rep_fed, AQUA, "KXFED ATM (response)")):
            qs = sample_minutes(rep, market, T0_NS - 90 * MIN, T0_NS + 45 * MIN)
            xs = [(q.recv_wall_ns - T0_NS) / (60 * 1e9) for q in qs]
            ys = [q.mid_c for q in qs]
            ax.plot(xs, ys, lw=2, color=color, label=label)
        out_name = "private/fig-jul09-claims-morning.png"
    ax.axvline(0, color="#b9b8b4", lw=1, ls="--")
    ax.annotate("t0", (0.3, ax.get_ylim()[1] * 0.97), fontsize=8, color=INK2)
    ax.axvline(-5, color="#d9d8d4", lw=1, ls=":")
    ax.set_xlabel("minutes from release", fontsize=9, color=INK2)
    ax.set_ylabel("mid (cents)", fontsize=9, color=INK2)
    ax.set_title("2026-07-09 weekly claims morning — 1-minute sampled mids",
                 fontsize=10, color=INK)
    ax.legend(fontsize=8, frameon=False, loc="best")
    fig.tight_layout()
    fig.savefig(out_name, facecolor=SURFACE)
    plt.close(fig)
    print(f"(e) figure -> {out_name}")
    report += ["## (e) Price-path figure", "", f"![claims morning]({Path(out_name).name})", ""]

    if not args.synthetic_figure:
        Path("private/rehearsal-report-jul09.md").write_text("\n".join(report), encoding="utf-8")
        print("report -> private/rehearsal-report-jul09.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
