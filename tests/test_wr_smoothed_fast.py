"""The vectorized wr_smoothed / presquare_bars (11/19) must equal the original loop versions exactly."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import bar_signal_family as B  # noqa: E402
import evening_family as E  # noqa: E402


def wr_ref(h, l, c, n=21, span=7):  # noqa: E741 - the pre-11/19 loop, verbatim
    h, l, c = (np.asarray(x, dtype=float) for x in (h, l, c))  # noqa: E741
    out = np.full(len(c), np.nan)
    a = 2.0 / (span + 1.0)
    prev = None
    for i in range(n - 1, len(c)):
        hh, ll = np.nanmax(h[i - n + 1:i + 1]), np.nanmin(l[i - n + 1:i + 1])
        if not np.isfinite(hh) or not np.isfinite(ll) or hh <= ll or not np.isfinite(c[i]):
            continue
        raw = -100.0 * (hh - c[i]) / (hh - ll)
        prev = raw if prev is None else a * raw + (1 - a) * prev
        out[i] = prev
    return out


def presq_ref(cc, hh, ll, off, L):
    fast = wr_ref(hh, ll, cc, n=L, span=7)
    slow = wr_ref(hh, ll, cc, n=112, span=3)
    ok = np.zeros(len(cc), dtype=bool)
    for i in range(1, len(cc)):
        f, s, fp = fast[i], slow[i], fast[i - 1]
        if np.isnan(f) or np.isnan(s) or np.isnan(fp):
            continue
        ok[i] = f >= -35 and s >= -35 and abs(f - s) <= 15 and not (f >= -20 and s >= -20) and f - fp > 0.05
    return [i - off for i in range(off + 2, len(cc)) if ok[i] and ok[i - 1] and not ok[i - 2] and E.FIRST <= i - off <= E.LAST]


def _series(seed, n=502, gaps=True):
    rng = np.random.default_rng(seed)
    c = 50 + np.cumsum(rng.normal(0, 0.05, n))
    h = c + rng.uniform(0, 0.05, n)
    l = c - rng.uniform(0, 0.05, n)  # noqa: E741
    if gaps:
        for k in rng.choice(n, 25, replace=False):
            c[k] = h[k] = l[k] = np.nan
        h[rng.choice(n, 5)] = np.nan
    return c, h, l


def test_wr_smoothed_matches_reference():
    for seed in range(30):
        c, h, l = _series(seed)  # noqa: E741
        for n, span in ((21, 7), (112, 3), (5, 2)):
            np.testing.assert_array_equal(np.isnan(B.wr_smoothed(h, l, c, n, span)), np.isnan(wr_ref(h, l, c, n, span)))
            np.testing.assert_allclose(np.nan_to_num(B.wr_smoothed(h, l, c, n, span)), np.nan_to_num(wr_ref(h, l, c, n, span)),
                                       rtol=0, atol=1e-9)
    assert np.isnan(B.wr_smoothed([1.0] * 3, [0.5] * 3, [0.7] * 3, 21, 7)).all()


def test_presquare_matches_reference():
    hits = 0
    for seed in range(60):
        c, h, l = _series(seed, gaps=seed % 2 == 0)  # noqa: E741
        got, ref = E.presquare_bars(c, h, l, 112, 21), presq_ref(c, h, l, 112, 21)
        assert got == ref
        hits += len(ref)
    assert hits > 0
