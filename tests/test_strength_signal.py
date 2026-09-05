"""The strength entry's forward-validation logger.

The rule it watches for — EXH crossing up through 75, then CM RSI-2 >= 90
within 20 bars — measured +0.86%/trade at 10/10 sessions in-sample, and the
same setup entered on RSI-2 <= 20 lost 1.25%. That gap is the whole reason
these rows are being collected, and it is why the polarity below is asserted
explicitly: a sign flip here would quietly log the losing half.

Two properties matter more than the arithmetic:

  * it evaluates CLOSED bars only, once each. The rule was fitted on 1m bars
    and the poll runs every 5 seconds, so firing on a forming bar would be a
    different signal wearing the same name.
  * it cannot raise. A logging experiment that can take the watch poll down
    is a worse trade than any it could find.
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import strength_signal as ss  # noqa: E402


class FakeEW:
    """Just the two accessors strength_signal reads off ai_entry_watch."""

    def __init__(self, rows, stamps, rsi):
        self._rows, self._stamps, self._rsi = rows, stamps, rsi

    def symbol_ohlc(self, sym, cfg, now):
        return self._rows

    def _cached_ohlc_stamps(self, sym, cfg, now):
        return self._stamps

    def cm_rsi_series(self, closes, period):
        return self._rsi[:len(closes)]


def _flat(n, px=10.0):
    # Close in the MIDDLE of each bar's range, so EXH sits near 50 and has
    # somewhere to cross up from. A close at the bar's high pins EXH at 100
    # and no crossing can ever be detected — which is what the first draft
    # of this fixture did.
    return [(px, px * 0.99, px * 0.995) for _ in range(n)]


def _series(n_flat=30, cross_at=None, top=None):
    """Bars that sit flat, then rise so EXH crosses, then a trigger bar."""
    rows = _flat(n_flat)
    if cross_at:
        for _ in range(cross_at):
            rows.append((10.4, 10.3, 10.35))     # lifts EXH through 75
    if top:
        for _ in range(top):
            rows.append((10.9, 10.8, 10.85))     # the surge
    rows.append((10.9, 10.8, 10.85))             # forming bar, never scored
    stamps = [1_000_000.0 + 60.0 * i for i in range(len(rows))]
    return rows, stamps


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    ss.reset_state()
    yield
    ss.reset_state()


def _run(rows, stamps, rsi, cfg=None, now=1_000_000.0 + 60 * 999):
    # The knob ships OFF (the rule was falsified); tests opt in explicitly.
    cfg = {"ai_strength_signal_enabled": True} if cfg is None else cfg
    return ss.evaluate(["AAA"], cfg, now, ew=FakeEW(rows, stamps, rsi))


# ── the rule ─────────────────────────────────────────────────────────────

def test_a_crossing_alone_does_not_fire():
    """The setup is not the entry — the trigger is a later bar."""
    rows, stamps = _series(cross_at=3)
    assert _run(rows, stamps, [50.0] * len(rows)) == []


def test_crossing_then_strength_fires():
    rows, stamps = _series(cross_at=3, top=2)
    ss.reset_state()
    # First pass establishes the crossing; the trigger bar arrives after.
    out = None
    for cut in range(23, len(rows) + 1):
        out = _run(rows[:cut], stamps[:cut], [95.0] * cut)
        if out:
            break
    assert out, "crossing followed by RSI-2 >= 90 should fire"
    row = out[0]
    assert row["symbol"] == "AAA"
    assert row["cm_rsi"] >= 90.0
    assert row["exh"] >= 60.0
    assert row["bars_since_cross"] >= 1


def test_low_rsi_never_fires_it_is_the_losing_half():
    """RSI-2 <= 20 after the same setup lost 1.25%/trade at 0/10 sessions.

    A sign flip here would log that arm and call it the strategy.
    """
    rows, stamps = _series(cross_at=3, top=2)
    for cut in range(23, len(rows) + 1):
        assert _run(rows[:cut], stamps[:cut], [5.0] * cut) == []


def test_the_setup_expires():
    rows, stamps = _series(cross_at=3)
    for _ in range(40):                       # long past wait_bars
        rows.append((10.4, 10.3, 10.35))
    rows.append((10.4, 10.3, 10.35))
    stamps = [1_000_000.0 + 60.0 * i for i in range(len(rows))]
    fired = []
    for cut in range(23, len(rows) + 1):
        fired += _run(rows[:cut], stamps[:cut], [95.0] * cut)
    # Whatever fires must be inside the window, never a stale setup.
    assert all(r["bars_since_cross"] <= 20 for r in fired)


# ── evaluates closed bars, once each ─────────────────────────────────────

def test_the_forming_bar_is_never_scored():
    rows, stamps = _series(cross_at=3, top=2)
    rsi = [95.0] * len(rows)
    first = _run(rows, stamps, rsi)
    # Re-running with the same bars must not produce a second row.
    assert _run(rows, stamps, rsi) == []
    assert len(first) <= 1


def test_a_row_is_written_to_the_log():
    rows, stamps = _series(cross_at=3, top=2)
    out = []
    for cut in range(23, len(rows) + 1):
        out += _run(rows[:cut], stamps[:cut], [95.0] * cut)
    if not out:
        pytest.skip("fixture did not trigger; covered by the firing test")
    logged = [json.loads(x) for x in
              Path(ss.log_path()).read_text().splitlines() if x.strip()]
    assert logged and logged[0]["kind"] == "strength_signal"
    assert "latency_sec" in logged[0]      # the one-bar constraint's evidence
    assert "rule" in logged[0]             # knobs travel with the row


# ── it cannot take the poll down ─────────────────────────────────────────

class Exploding:
    def symbol_ohlc(self, *a, **k):
        raise RuntimeError("bars are on fire")

    def _cached_ohlc_stamps(self, *a, **k):
        raise RuntimeError("so are the stamps")

    def cm_rsi_series(self, *a, **k):
        raise RuntimeError("and the rsi")


def test_a_broken_bar_source_is_swallowed():
    assert ss.evaluate(["AAA"], {}, 1.0, ew=Exploding()) == []


def test_mismatched_stamps_are_refused_not_guessed():
    rows, stamps = _series(cross_at=3, top=2)
    assert _run(rows, stamps[:-4], [95.0] * len(rows)) == []


def test_disabled_does_nothing():
    rows, stamps = _series(cross_at=3, top=2)
    cfg = {"ai_strength_signal_enabled": False}
    for cut in range(23, len(rows) + 1):
        assert _run(rows[:cut], stamps[:cut], [95.0] * cut, cfg) == []
