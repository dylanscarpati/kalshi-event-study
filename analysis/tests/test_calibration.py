"""Tests for the calibration engine: Murphy identity on a hand-computed
fixture, bootstrap reproducibility, bucket edges. All fixtures synthetic."""

import numpy as np
import pytest

from calibration import bucket_of, clustered_bootstrap, event_bucket_matrices, murphy


def test_murphy_hand_computed_fixture():
    # Two buckets, worked by hand:
    # bucket 2 (0.2, 0.2) outcomes (0, 1): pbar=0.2 f=0.5
    # bucket 8 (0.8, 0.8) outcomes (1, 1): pbar=0.8 f=1.0
    # BS = (0.04 + 0.64 + 0.04 + 0.04)/4 = 0.19 ; ybar = 0.75
    # REL = .5*(0.3)^2 + .5*(0.2)^2 = 0.065
    # RES = .5*(0.25)^2 + .5*(0.25)^2 = 0.0625 ; UNC = 0.1875
    p = np.array([0.2, 0.2, 0.8, 0.8])
    y = np.array([0.0, 1.0, 1.0, 1.0])
    st = murphy(p, y)
    assert st["bs"] == pytest.approx(0.19)
    assert st["rel"] == pytest.approx(0.065)
    assert st["res"] == pytest.approx(0.0625)
    assert st["unc"] == pytest.approx(0.1875)
    # identity is asserted inside murphy(); reaching here means it held


def test_murphy_identity_on_random_data():
    rng = np.random.default_rng(7)
    p = rng.uniform(0.01, 0.99, size=500)
    y = (rng.uniform(size=500) < p).astype(float)
    st = murphy(p, y)  # internal assert guards the binned score exactly
    assert st["bs_star"] == pytest.approx(st["rel"] - st["res"] + st["unc"], abs=1e-14)
    # with heterogeneous prices inside buckets, raw BS differs from the
    # binned score by the within-bucket discretization component
    assert st["gap"] != 0.0
    assert abs(st["gap"]) < 0.02  # small relative to BS at 10-cent buckets


def test_bucket_edges():
    assert bucket_of(0.01) == 0
    assert bucket_of(0.09) == 0
    assert bucket_of(0.10) == 1
    assert bucket_of(0.95) == 9
    assert bucket_of(0.99) == 9


def _synthetic_rows(n_events=30, per_event=6, seed=11):
    rng = np.random.default_rng(seed)
    rows = []
    for e in range(n_events):
        for _ in range(per_event):
            p = float(rng.uniform(0.05, 0.95))
            rows.append({"event": f"E{e}", "gridpoint": "7d", "p": p,
                         "y": float(rng.uniform() < p), "source": "MID",
                         "spread_c": 2, "trade_c": None})
    return rows


def test_bootstrap_reproducible_and_cluster_counted():
    rows = _synthetic_rows()
    a = clustered_bootstrap(rows, np.random.default_rng(123))
    b = clustered_bootstrap(rows, np.random.default_rng(123))
    assert a["m"] == 30
    assert np.allclose(a["rel_ci"], b["rel_ci"])  # same seed, same CIs
    assert np.allclose(a["f_ci"], b["f_ci"], equal_nan=True)
    lo, hi = a["bs_ci"]
    assert lo < hi


def test_event_matrices_sum_to_totals():
    rows = _synthetic_rows(n_events=5, per_event=4)
    events, counts, sum_y, sum_p, sq_err = event_bucket_matrices(rows)
    assert len(events) == 5
    assert counts.sum() == 20
    assert sum_y.sum() == sum(r["y"] for r in rows)
    assert sum_p.sum() == pytest.approx(sum(r["p"] for r in rows))
    assert sq_err.sum() == pytest.approx(sum((r["p"] - r["y"]) ** 2 for r in rows))
