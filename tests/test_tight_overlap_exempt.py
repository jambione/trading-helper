"""ai_watch_tight_overlap_exempt: a tight-qualified name that movers/trending listed first keeps tight's rvol/float
exemptions (Argon 11/18: 354 thin_rvol + 132 float_too_big refusals of top-400 signals were movers rows)."""
import ai_entry_watch as ew


def _row(**kw):
    r = {"symbol": "ABC", "price": 50.0, "pct_change": 2.0, "spread_pct": 0.03, "dollar_volume": 5e7}
    r.update(kw)
    return r


KW = dict(max_px=400.0, min_px=20.0, min_pct=1.5, max_sp=0.05, min_dv=2e6)


def test_qualifies_on_tight_rules_only():
    assert ew.tight_overlap_qualifies(_row(), {}, {}, **KW)
    assert not ew.tight_overlap_qualifies(_row(spread_pct=0.08), {}, {}, **KW)     # not tight
    assert not ew.tight_overlap_qualifies(_row(spread_pct=None), {}, {}, **KW)     # unknown is not tight
    assert not ew.tight_overlap_qualifies(_row(pct_change=1.0), {}, {}, **KW)
    assert not ew.tight_overlap_qualifies(_row(price=500.0), {}, {}, **KW)
    assert not ew.tight_overlap_qualifies(_row(dollar_volume=1e6), {}, {}, **KW)
    # the live desk quote overrides the file's price / % change, as in the seed loop
    assert not ew.tight_overlap_qualifies(_row(), {"price": 50.0, "pct_change": 0.5}, {}, **KW)


def test_gate_source_only_with_the_switch_and_the_list(monkeypatch):
    monkeypatch.setattr(ew, "_TIGHT_SYMS", {"ABC"})
    on, off = {"ai_watch_tight_overlap_exempt": True}, {}
    assert ew.tight_gate_source("ABC", "movers", on) == "tight"
    assert ew.tight_gate_source("abc", "trending", on) == "tight"
    assert ew.tight_gate_source("ABC", "movers", off) == "movers"      # default off: unchanged
    assert ew.tight_gate_source("XYZ", "movers", on) == "movers"       # not on the tight list
    assert ew.tight_gate_source("XYZ", "tight", off) == "tight"


def test_rvol_floor_follows_the_gate_source(monkeypatch):
    monkeypatch.setattr(ew, "_TIGHT_SYMS", {"ABC"})
    cfg = {"ai_watch_tight_overlap_exempt": True, "ai_watch_movers_min_rvol": 0.65, "ai_watch_tight_min_rvol": 0.0}
    src = ew.tight_gate_source("ABC", "movers", cfg)
    assert ew.rvol_blocks_admit(0.4, 2.0, cfg, source=src) is None
    assert ew.rvol_blocks_admit(0.4, 2.0, cfg, source="movers") == "thin_rvol"
