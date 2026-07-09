"""RQ3 calibration analysis: reliability curves, Brier + Murphy decomposition,
clustered bootstrap — per methodology sections 3.3/4.4-4.6 and amendments
A3/A3.1 (headline price source: MID-with-cap hierarchy).

Inputs: data/derived/gridpoint_prices.csv (+ cap5/cap20 sensitivity variants),
data/derived/settled_markets.csv (outcomes). Outputs: figures/*.png,
data/derived/calibration_tables.csv, appendix sensitivity table, and an
aggregate-only console summary. Every figure states n (observations),
m (event clusters), and B.

Reproducibility: RNG seed fixed at SEED and printed with every run;
resampling unit is the EVENT (all of an event's strikes and gridpoints move
together), per section 4.6.

Usage: python analysis/calibration.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

SEED = 20260708
B = 10_000
BANDS = ["30d", "14d", "7d", "3d", "1d", "12h", "4h", "2h", "1h"]
MIN_BUCKET_N = 25
N_BUCKETS = 10

INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e8e8e6"
SURFACE = "#fcfcfb"
BLUE = "#2a78d6"
AQUA = "#1baf7a"
YELLOW = "#eda100"
GREEN = "#008300"


def load_observations(gridpoint_csv: str) -> list[dict]:
    outcomes = {}
    for r in csv.DictReader(open("data/derived/settled_markets.csv", encoding="utf-8")):
        if r["result"] in ("yes", "no"):
            outcomes[r["ticker"]] = 1.0 if r["result"] == "yes" else 0.0
    rows = []
    for r in csv.DictReader(open(gridpoint_csv, encoding="utf-8")):
        y = outcomes.get(r["ticker"])
        if y is None:
            continue  # scalar/void results are not binary outcomes
        rows.append({
            "event": r["event_ticker"],
            "gridpoint": r["gridpoint"],
            "p": float(r["price_c"]) / 100.0,
            "y": y,
            "source": r["source"],
            "spread_c": int(r["spread_c"]) if r["spread_c"] else None,
            "trade_c": int(r["trade_close_c"]) if r["trade_close_c"] else None,
        })
    return rows


def bucket_of(p: float) -> int:
    return min(int(p * N_BUCKETS), N_BUCKETS - 1)


def murphy(p: np.ndarray, y: np.ndarray) -> dict:
    """Brier + Murphy decomposition with fixed 10-cent buckets.

    The exact identity REL - RES + UNC applies to the BUCKET-DISCRETIZED
    score bs_star = mean((pbar_bucket - y)^2): with heterogeneous forecasts
    inside a bucket, raw BS additionally carries a within-bucket
    discretization component (the within-bucket cross-term does not vanish
    for continuous prices). We report raw bs, the exactly-decomposed
    bs_star, and their gap; the machine-precision assert guards bs_star."""
    n = len(p)
    bs = float(np.mean((p - y) ** 2))
    ybar = float(np.mean(y))
    rel = res = 0.0
    buckets = np.minimum((p * N_BUCKETS).astype(int), N_BUCKETS - 1)
    p_binned = np.empty_like(p)
    for k in range(N_BUCKETS):
        mask = buckets == k
        nk = int(mask.sum())
        if nk == 0:
            continue
        pbar_k = float(np.mean(p[mask]))
        f_k = float(np.mean(y[mask]))
        p_binned[mask] = pbar_k
        rel += (nk / n) * (pbar_k - f_k) ** 2
        res += (nk / n) * (f_k - ybar) ** 2
    unc = ybar * (1 - ybar)
    bs_star = float(np.mean((p_binned - y) ** 2))
    assert abs(bs_star - (rel - res + unc)) < 1e-12, "Murphy identity violated"
    return {"n": n, "bs": bs, "bs_star": bs_star, "gap": bs - bs_star,
            "rel": rel, "res": res, "unc": unc, "ybar": ybar}


def event_bucket_matrices(rows: list[dict]):
    """Per-event-per-bucket sufficient statistics. Bucket membership is fixed
    per observation, so event-level resampling reduces to a multiplicity-
    weighted sum of these matrices — the whole bootstrap is matrix algebra."""
    events = sorted({r["event"] for r in rows})
    idx = {e: i for i, e in enumerate(events)}
    counts = np.zeros((len(events), N_BUCKETS))
    sum_y = np.zeros((len(events), N_BUCKETS))
    sum_p = np.zeros((len(events), N_BUCKETS))
    sq_err = np.zeros(len(events))  # per-event sum of (p - y)^2 for raw BS
    for r in rows:
        i, k = idx[r["event"]], bucket_of(r["p"])
        counts[i, k] += 1
        sum_y[i, k] += r["y"]
        sum_p[i, k] += r["p"]
        sq_err[i] += (r["p"] - r["y"]) ** 2
    return events, counts, sum_y, sum_p, sq_err


def clustered_bootstrap(rows: list[dict], rng: np.random.Generator) -> dict:
    """Resample EVENTS with replacement, B times, vectorized.
    Returns percentile CIs for per-bucket realized frequency and for each
    decomposition term."""
    events, counts, sum_y, sum_p, sq_err = event_bucket_matrices(rows)
    m = len(events)
    draws = rng.integers(0, m, size=(B, m))
    mult = np.zeros((B, m))
    for b in range(B):  # multiplicity of each event in replicate b
        np.add.at(mult[b], draws[b], 1)
    n_rep = mult @ counts          # (B, buckets)
    sy_rep = mult @ sum_y
    sp_rep = mult @ sum_p
    with np.errstate(invalid="ignore", divide="ignore"):
        f_rep = sy_rep / n_rep
        pbar_rep = sp_rep / n_rep
    n_tot = n_rep.sum(axis=1)
    ybar_rep = sy_rep.sum(axis=1) / n_tot
    w = n_rep / n_tot[:, None]
    rel_rep = np.nansum(w * (pbar_rep - f_rep) ** 2, axis=1)
    res_rep = np.nansum(w * (f_rep - ybar_rep[:, None]) ** 2, axis=1)
    unc_rep = ybar_rep * (1 - ybar_rep)
    bs_rep = (mult @ sq_err) / n_tot  # raw Brier per replicate

    def ci(a: np.ndarray, axis=0):
        return np.nanpercentile(a, [2.5, 97.5], axis=axis)

    return {"m": m, "f_ci": ci(f_rep), "rel_ci": ci(rel_rep), "res_ci": ci(res_rep),
            "unc_ci": ci(unc_rep), "bs_ci": ci(bs_rep)}


def band_table(rows: list[dict]) -> list[dict]:
    """Per-bucket reporting per A3.1 item 3: n, mean stated, realized,
    source composition, median admitted-MID spread."""
    out = []
    for k in range(N_BUCKETS):
        sub = [r for r in rows if bucket_of(r["p"]) == k]
        if not sub:
            continue
        mids = [r for r in sub if r["source"] == "MID"]
        spreads = sorted(r["spread_c"] for r in mids if r["spread_c"] is not None)
        out.append({
            "bucket": f"{k * 10}-{k * 10 + 10}c",
            "n": len(sub),
            "mean_stated": float(np.mean([r["p"] for r in sub])),
            "realized": float(np.mean([r["y"] for r in sub])),
            "pct_mid": len(mids) / len(sub),
            "median_mid_spread_c": spreads[len(spreads) // 2] if spreads else None,
        })
    return out


def style_axes(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#d0cfcc")
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6, zorder=0)


def main() -> int:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rng = np.random.default_rng(SEED)
    print(f"seed={SEED}  B={B}  resampling unit=event")

    rows_all = load_observations("data/derived/gridpoint_prices.csv")
    by_band = {b: [r for r in rows_all if r["gridpoint"] == b] for b in BANDS}
    figures = Path("figures")
    figures.mkdir(exist_ok=True)

    # ---------------- per-band stats + bootstrap ----------------
    band_stats, band_boot, tables = {}, {}, []
    for band in BANDS:
        rows = by_band[band]
        p = np.array([r["p"] for r in rows])
        y = np.array([r["y"] for r in rows])
        band_stats[band] = murphy(p, y)
        band_boot[band] = clustered_bootstrap(rows, rng)
        for t in band_table(rows):
            tables.append({"band": band, **t})

    with open("data/derived/calibration_tables.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(tables[0].keys()))
        w.writeheader()
        w.writerows(tables)

    # ---------------- figure 1: reliability small multiples ----------------
    fig, axes = plt.subplots(3, 3, figsize=(10.5, 10.0), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    for ax, band in zip(axes.flat, BANDS):
        style_axes(ax)
        rows = by_band[band]
        st, bt = band_stats[band], band_boot[band]
        ax.plot([0, 1], [0, 1], ls="--", lw=1, color="#b9b8b4", zorder=1)
        table = band_table(rows)
        shown = hidden = 0
        for t in table:
            k = int(t["bucket"].split("-")[0]) // 10
            if t["n"] < MIN_BUCKET_N:
                hidden += 1
                continue
            shown += 1
            lo, hi = bt["f_ci"][0][k], bt["f_ci"][1][k]
            ax.errorbar(t["mean_stated"], t["realized"],
                        yerr=[[t["realized"] - lo], [hi - t["realized"]]],
                        fmt="o", ms=5.5, color=BLUE, ecolor=BLUE,
                        elinewidth=1.2, capsize=2.5, zorder=3)
            ax.annotate(f"{t['n']}", (t["mean_stated"], -0.06), ha="center",
                        fontsize=6, color=INK2, annotation_clip=False)
        ax.set_xlim(0, 1)
        ax.set_ylim(-0.02, 1.02)
        ax.set_title(f"TTR {band} — n={st['n']}, m={bt['m']} events", fontsize=8.5, color=INK)
        if hidden:
            ax.annotate(f"{hidden} buckets under n={MIN_BUCKET_N} not shown",
                        (0.5, 0.62), ha="center", fontsize=7, color=INK2)
        ax.set_xlabel("stated probability", fontsize=8, color=INK2)
        ax.set_ylabel("realized frequency", fontsize=8, color=INK2)
    fig.suptitle("Calibration by time-to-resolution — Kalshi macro markets 2021–2026\n"
                 f"points: buckets with n ≥ {MIN_BUCKET_N}; whiskers: 95% event-clustered "
                 f"bootstrap (B={B:,}); small figures under points: bucket n",
                 fontsize=10.5, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(figures / "calibration_curves.png", facecolor=SURFACE)
    plt.close(fig)

    # ---------------- figure 2: Brier decomposition across bands ----------------
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax)
    x = np.arange(len(BANDS))
    series = [("Brier score", "bs", "bs_ci", GREEN),
              ("Reliability (miscalibration, 0 = perfect)", "rel", "rel_ci", BLUE),
              ("Resolution (higher = sharper)", "res", "res_ci", AQUA),
              ("Uncertainty (base-rate bound)", "unc", "unc_ci", YELLOW)]
    for label, key, ci_key, color in series:
        vals = np.array([band_stats[b][key] for b in BANDS])
        lo = np.array([band_boot[b][ci_key][0] for b in BANDS])
        hi = np.array([band_boot[b][ci_key][1] for b in BANDS])
        ax.plot(x, vals, lw=2, marker="o", ms=5, color=color, label=label, zorder=3)
        ax.fill_between(x, lo, hi, color=color, alpha=0.14, linewidth=0, zorder=2)
        ax.annotate(label.split(" (")[0], (x[-1] + 0.12, vals[-1]),
                    fontsize=8, color=INK, va="center")
    ax.set_xticks(x, BANDS)
    ax.set_xlim(-0.3, len(BANDS) + 1.6)
    ax.set_xlabel("time to resolution (t0 approaching →)", fontsize=9, color=INK2)
    ax.set_ylabel("score", fontsize=9, color=INK2)
    n_tot = sum(band_stats[b]["n"] for b in BANDS)
    max_gap = max(abs(band_stats[b]["gap"]) for b in BANDS)
    ax.set_title(f"Brier decomposition by time-to-resolution — n={n_tot}, "
                 f"95% event-clustered bootstrap (B={B:,})\n"
                 f"REL−RES+UNC decomposes the bucket-discretized score exactly "
                 f"(gap vs raw BS ≤ {max_gap:.4f})",
                 fontsize=9.5, color=INK, loc="left")
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(figures / "brier_decomposition.png", facecolor=SURFACE)
    plt.close(fig)

    # -------- figure 3: extreme-bucket artifact check (A3.1 item 4) --------
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.6), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    for ax, (k, title) in zip(axes, [(0, "longshots: stated < 10c"),
                                     (9, "favorites: stated > 90c")]):
        style_axes(ax)
        for dx, (label, color, use_trade) in enumerate(
                [("hierarchy (headline)", BLUE, False), ("TRADE-only", AQUA, True)]):
            gaps, ns = [], []
            for band in BANDS:
                sub = []
                for r in by_band[band]:
                    if use_trade:
                        if r["trade_c"] is None:
                            continue
                        price = r["trade_c"] / 100.0
                    else:
                        price = r["p"]
                    if bucket_of(price) == k:
                        sub.append((price, r["y"]))
                if len(sub) >= MIN_BUCKET_N:
                    arr = np.array(sub)
                    gaps.append(float(arr[:, 1].mean() - arr[:, 0].mean()))
                    ns.append(len(sub))
                else:
                    gaps.append(np.nan)
                    ns.append(len(sub))
            xs = np.arange(len(BANDS)) + (dx - 0.5) * 0.22
            ax.plot(xs, gaps, "o", ms=6, color=color, label=label, zorder=3)
        ax.axhline(0, color="#b9b8b4", lw=1, ls="--", zorder=1)
        ax.set_xticks(np.arange(len(BANDS)), BANDS, fontsize=7.5)
        ax.set_title(title, fontsize=9.5, color=INK)
        ax.set_xlabel("time to resolution", fontsize=8.5, color=INK2)
        ax.set_ylabel("realized − stated (reliability gap)", fontsize=8.5, color=INK2)
        ax.legend(fontsize=8, frameon=False)
    fig.suptitle("Extreme-bucket artifact check: price-source hierarchy vs TRADE-only "
                 f"(bands with bucket n ≥ {MIN_BUCKET_N}; divergence ⇒ quoting artifact)",
                 fontsize=10, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(figures / "artifact_check.png", facecolor=SURFACE)
    plt.close(fig)

    # ---------------- sensitivity appendix (spread caps) ----------------
    sens_rows = []
    for cap, path in [(5, "data/derived/gridpoint_prices_cap5.csv"),
                      (10, "data/derived/gridpoint_prices.csv"),
                      (20, "data/derived/gridpoint_prices_cap20.csv")]:
        rows = load_observations(path)
        p = np.array([r["p"] for r in rows])
        y = np.array([r["y"] for r in rows])
        st = murphy(p, y)
        mid_share = sum(1 for r in rows if r["source"] == "MID") / len(rows)
        sens_rows.append({"spread_cap_c": cap, "n": st["n"], "pct_mid": round(mid_share, 4),
                          "brier": round(st["bs"], 5), "reliability": round(st["rel"], 5),
                          "resolution": round(st["res"], 5)})
    with open("data/derived/calibration_sensitivity.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(sens_rows[0].keys()))
        w.writeheader()
        w.writerows(sens_rows)

    # ---------------- aggregate-only summary ----------------
    print(f"\n{'band':>4}  {'n':>4} {'m':>4}  {'Brier':>7}  {'REL':>7}  {'RES':>7}  {'UNC':>7}")
    for band in BANDS:
        st, bt = band_stats[band], band_boot[band]
        print(f"{band:>4}  {st['n']:>4} {bt['m']:>4}  {st['bs']:.4f}  {st['rel']:.4f}"
              f"  {st['res']:.4f}  {st['unc']:.4f}")
    print("\nsensitivity (pooled):", *(f"cap{r['spread_cap_c']}: BS={r['brier']} REL={r['reliability']}"
                                       for r in sens_rows))
    print("figures ->", ", ".join(p.name for p in sorted(figures.glob("*.png"))))
    print("era composition: 100% pre_collector (single era; per-era appendix not applicable yet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
