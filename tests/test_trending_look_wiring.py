"""trending_stocks.json must carry look/look_reason.

That file is what AI Watch admission reads. It previously held the raw rows:
apply_look_highlights only ran inside StocktwitsTrending.snapshot(), which the
screener loop never calls, so look_reason never reached passes_inclusion and
the EXT requirement could not pass for any name, ever — the gate was inert
while looking enforced.

Three consumers disagreed about which names are LOOK: the terminal (via
snapshot), the browser (its own JS reimplementation in feeds.js — which is why
CHYM showed "LOOK EXT" on screen while the server saw look=None), and
admission, which saw nothing at all.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from stocktwits_trending import apply_look_highlights  # noqa: E402


def _row(sym, *, price, lo, hi, score, chg, vol=5_000_000.0, rvol=None):
    r = {"symbol": sym, "price": price, "low_52w": lo, "high_52w": hi,
         "trending_score": score, "pct_change": chg, "vol_session": vol}
    if rvol is not None:
        r["rvol"] = rvol
    return r


def test_green_near_the_52w_high_tags_ext():
    """The CHYM shape on 2026-08-06: up big, near its 52-week high."""
    rows = apply_look_highlights(
        [_row("CHYM", price=29.0, lo=10.0, hi=32.0, score=7.7, chg=20.6),
         _row("MEH", price=11.0, lo=10.0, hi=32.0, score=7.0, chg=0.2)],
        min_abs_chg=3.0, max_looks=20, near_high=0.70, near_low=0.30,
    )
    by = {r["symbol"]: r for r in rows}
    assert by["CHYM"]["look"] is True
    assert by["CHYM"]["look_reason"] == "EXT"
    assert by["MEH"]["look"] is False


def test_red_near_the_52w_low_tags_wash_not_ext():
    """WASH must not satisfy an EXT gate — it is the opposite condition on a
    long-only desk."""
    rows = apply_look_highlights(
        [_row("DOWN", price=11.0, lo=10.0, hi=32.0, score=12.0, chg=-9.0),
         _row("OTHER", price=20.0, lo=10.0, hi=32.0, score=11.0, chg=-4.0)],
        min_abs_chg=3.0, max_looks=20, near_high=0.70, near_low=0.30,
    )
    by = {r["symbol"]: r for r in rows}
    assert by["DOWN"]["look_reason"] == "WASH"
    assert by["DOWN"]["look_reason"] != "EXT"


def test_max_looks_no_longer_pins_the_panel_to_two():
    """The default cap of 2 made LOOK a UI spotlight. As an admission filter it
    has to be able to tag everything that qualifies, or admission becomes
    'top 2 by priority' rather than 'met the conditions'."""
    rows = [
        _row(f"S{chr(65+i)}", price=29.0 + i, lo=10.0, hi=32.0 + i,
             score=12.0 + i, chg=10.0 + i, vol=5_000_000.0 + i)
        for i in range(8)
    ]
    capped = apply_look_highlights([dict(r) for r in rows], min_abs_chg=3.0,
                                   max_looks=2, near_high=0.70, near_low=0.30)
    wide = apply_look_highlights([dict(r) for r in rows], min_abs_chg=3.0,
                                 max_looks=20, near_high=0.70, near_low=0.30)
    assert sum(1 for r in capped if r["look"]) == 2
    assert sum(1 for r in wide if r["look"]) > 2


def test_unknown_rvol_neither_passes_nor_blocks():
    """The file carries rvol=None until the volume refresh resolves. Treating
    absence as failure emptied the book once already (see passes_inclusion)."""
    rows = apply_look_highlights(
        [_row("NORV", price=29.0, lo=10.0, hi=32.0, score=12.0, chg=10.0)],
        min_abs_chg=3.0, max_looks=20, near_high=0.70, near_low=0.30,
        min_rvol=1.5,
    )
    assert rows[0]["look"] is True, "unknown rvol must not block the tag"


def test_known_low_rvol_does_block():
    rows = apply_look_highlights(
        [_row("LOWRV", price=29.0, lo=10.0, hi=32.0, score=12.0, chg=10.0,
              rvol=0.4)],
        min_abs_chg=3.0, max_looks=20, near_high=0.70, near_low=0.30,
        min_rvol=1.5,
    )
    assert rows[0]["look"] is False


def test_screener_writes_look_fields(monkeypatch, tmp_path):
    """End-to-end on the write path: whatever lands in trending_stocks.json is
    exactly what admission reads, so the tag has to survive serialisation."""
    import json

    import trending_screener as ts

    rows = [_row("CHYM", price=29.0, lo=10.0, hi=32.0, score=7.7, chg=20.6)]
    tagged = apply_look_highlights([dict(r) for r in rows], min_abs_chg=3.0,
                                   max_looks=20, near_high=0.70, near_low=0.30)

    out = tmp_path / "trending_stocks.json"
    ts._write_json(out, {"updated": 1.0, "rows": tagged})

    written = json.loads(out.read_text(encoding="utf-8"))["rows"]
    assert written[0]["look"] is True
    assert written[0]["look_reason"] == "EXT"
