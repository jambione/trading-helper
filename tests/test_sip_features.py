"""sip_features.shape must reproduce the live logger's formulas and gap rules (ai_entry_watch._range_shape_fields)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_entry_watch as ew  # noqa: E402
import ob_observe  # noqa: E402
from tools.studies import sip_features as sf  # noqa: E402

T = 1_793_900_000.0 - (1_793_900_000.0 % 60)


def _bars(closes, end=T - 60):
    n = len(closes)
    return [(end - 60 * (n - 1 - i), c, c + 0.02, c - 0.02, c, 1000.0) for i, c in enumerate(closes)]


def test_matches_the_live_logger_on_dense_and_choppy_bars(monkeypatch):
    for closes in ([10.0 + 0.01 * i for i in range(60)], [10.0 + 0.05 * ((i * 7) % 5) for i in range(60)], [10.5] * 45 + [10.45 + 0.001 * i for i in range(15)]):
        b = _bars(closes)
        px = closes[-1] + 0.01
        monkeypatch.setattr(ob_observe, "bars", lambda s, b=b: b)
        live = ew._range_shape_fields("ABC", px, T + 20, {"ai_watch_range_shape_log": True})
        mine = sf.shape(b, px, T + 20)
        for k in sf.MEASURES:
            assert live.get(k) == mine.get(k), (k, live.get(k), mine.get(k))


def test_gappy_sparse_bars_drop_the_window_like_live():
    closes = [10.0 + 0.01 * i for i in range(60)]
    b = _bars(closes)
    sparse = b[:20] + b[40:]                          # a 20-minute hole inside the 30-bar window
    out = sf.shape(sparse, 10.5, T + 20)
    assert "rs_pos30" not in out and "rs_er15" in out    # 30-bar window spans too long, the last 15 are fine
    stale = _bars(closes, end=T - 600)
    assert sf.shape(stale, 10.0, T + 20) == {} and sf.shape(b[:10], 10.0, T + 20) == {}
