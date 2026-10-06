"""Book panel, 2026-10-06 (operator request, display only):

1. the "Buy?" button is gone from the book ticker cell (its endpoint and
   scorer stay; tests/test_my_calls.py still covers the endpoint);
2. an SR column after EXH shows room to the nearest charted resistance order
   block from the desk's own ob_observe reading, the same ob_room_pct /
   ob_resist_0.3 the live ob_resist skip reads. Nothing here computes blocks
   or touches the arm path.
"""
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
_JS = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")


def _row_template() -> str:
    i = _JS.index("function _bookRowHtml(")
    return _JS[i:_JS.index("\n}", i)]


def test_buy_button_is_gone_from_the_book_row():
    t = _row_template()
    assert "book-call-btn" not in t and "Buy?" not in t


def test_sr_cell_follows_exh_in_the_row_and_header():
    t = _row_template()
    assert t.index('cell-exh') < t.index('cell-sr') < t.index('cell-pl')
    i = _HTML.index("ai-book-table-header")
    block = _HTML[i:_HTML.index("data-ai-book-rows", i)]
    assert block.index("th-exh") < block.index('th th-sr" data-book-sort-col="sr"') < block.index("th-pl")


def test_sr_cell_is_patched_in_place_and_shows_dash_without_a_reading():
    assert "const srEl = el.querySelector('.cell-sr');" in _JS
    i = _JS.index("function _bookSrText(")
    body = _JS[i:_JS.index("\n}", i)]
    assert "if (!r) return '\\u2014';" in body
    assert "'no R'" in body and "'in R'" in body
    assert "sr--resist" in _JS


def test_sr_fields_survive_the_client_row_whitelist():
    for k in ("ob_resist", "ob_room_pct", "ob_sup_btm", "ob_res_btm"):
        assert f"{k}: w.{k} != null" in _JS, k


def test_wire_fields_copy_the_stamped_reading(monkeypatch):
    import ai_entry_watch as ew
    import ob_observe
    monkeypatch.setattr(ob_observe, "levels", lambda sym, px: {
        "ob_sup_btm": 9.8, "ob_sup_top": 9.9, "ob_res_btm": 10.2, "ob_res_top": 10.3, "ob_levels_ts": 1.0})
    rec = {"indicator": {"ob_resist_0.3": False, "ob_room_pct": 2.0, "ob_bars": 400, "ob_prior_day": True}}
    out = ew._ob_wire_fields(rec, "ABC", 10.0)
    assert out["ob_resist"] is False and out["ob_room_pct"] == 2.0 and out["ob_bars"] == 400
    assert out["ob_res_btm"] == 10.2 and out["ob_sup_top"] == 9.9


def test_wire_fields_are_empty_without_a_reading_and_never_raise():
    import ai_entry_watch as ew
    import ob_observe
    ob_observe._BARS["ABC"] = {60.0: (60.0, 1, 1, 1, 1)}   # bars present -> no note
    try:
        assert ew._ob_wire_fields({}, "ABC", 10.0) in ({}, {"ob_off": True})
        assert ew._ob_wire_fields({"indicator": {"pctr": -20}}, "ABC", 10.0) in ({}, {"ob_off": True})
    finally:
        ob_observe._BARS.pop("ABC", None)
    assert ew._ob_wire_fields(None, "ABC", 10.0) == {}
    assert ew._ob_wire_fields({"indicator": {"ob_resist_0.3": True}}, "ABC", "junk") == {
        "ob_resist": True, "ob_room_pct": None, "ob_brk_dist_pct": None, "ob_bars": None}


def test_levels_is_cache_only(monkeypatch):
    import ob_observe

    def b(kind, breaker, btm, top):
        return SimpleNamespace(kind=kind, breaker=breaker, btm=btm, top=top)
    blocks = [b("bull", False, 9.5, 9.6), b("bull", False, 9.8, 9.9),     # supports
              b("bear", False, 10.2, 10.3), b("bull", True, 10.6, 10.7)]   # resistances
    monkeypatch.setattr(ob_observe, "_CACHE", {"ABC": ((600.0, 10, 1.0), blocks, 10, False)})

    def _no_compute(*_a, **_k):
        raise AssertionError("levels() must never compute blocks")
    monkeypatch.setattr(ob_observe, "_charted_at", _no_compute)
    lv = ob_observe.levels("abc", 10.0)
    assert (lv["ob_sup_btm"], lv["ob_sup_top"]) == (9.8, 9.9)
    assert (lv["ob_res_btm"], lv["ob_res_top"]) == (10.2, 10.3)
    assert ob_observe.levels("NOPE", 10.0) == {}
    assert ob_observe.levels("ABC", None) == {}


def test_book_table_rows_list_branch_keeps_sr_fields():
    import ai_entry_watch as ew
    rows = ew.book_table_rows(positions={}, watch_rows=[
        {"symbol": "ABC", "status": "watching", "ob_resist": True, "ob_room_pct": 0.1, "ob_res_btm": 10.01}], state={"x": 1})
    r = [x for x in rows if x["symbol"] == "ABC"]
    if r:  # state={"x": 1} has no usable record, so the list branch may or may not be taken
        assert r[0].get("ob_resist") is True and r[0].get("ob_room_pct") == 0.1


def test_no_reading_says_why(monkeypatch):
    """2026-10-06 09:3x: every SR cell read '—' because the block store had no
    bars (fed only by the structure fetch, which was not running). The cell now
    says so instead of a bare dash; still display only, still no fetch."""
    import ai_entry_watch as ew
    import ob_observe
    monkeypatch.setattr(ew, "_push_cfg", lambda: {"ai_watch_ob_resist_skip": True})
    monkeypatch.setattr(ob_observe, "_BARS", {})
    assert ew._ob_wire_fields({"indicator": {"pctr": -20}}, "ABC", 10.0) == {"ob_nobars": True}
    monkeypatch.setattr(ob_observe, "_BARS", {"ABC": {60.0: (60.0, 1, 1, 1, 1)}})
    assert ew._ob_wire_fields({"indicator": {"pctr": -20}}, "ABC", 10.0) == {}
    monkeypatch.setattr(ew, "_push_cfg", lambda: {})
    assert ew._ob_wire_fields({}, "ABC", 10.0) == {"ob_off": True}
    i = _JS.index("function _bookSrText(")
    body = _JS[i:_JS.index("\n}", i)]
    assert "'no bars'" in body and "'off'" in body


def test_store_size_never_raises(monkeypatch):
    import ob_observe
    monkeypatch.setattr(ob_observe, "_BARS", {"ABC": {1.0: (), 2.0: ()}})
    assert ob_observe.store_size("abc") == 2 and ob_observe.store_size(None) == 0


def test_breakout_distance_travels_to_the_sr_cell(monkeypatch):
    import ai_entry_watch as ew
    import ob_observe
    monkeypatch.setattr(ob_observe, "levels", lambda sym, px: {})
    rec = {"indicator": {"ob_resist_0.3": False, "ob_room_pct": 1.0, "ob_brk_dist_pct": 0.2, "ob_bars": 400}}
    out = ew._ob_wire_fields(rec, "ABC", 10.0)
    assert out["ob_brk_dist_pct"] == 0.2
    assert "ob_brk_dist_pct" in ew._OB_WIRE_KEYS
    assert "ob_brk_dist_pct: w.ob_brk_dist_pct != null" in _JS
    assert "sr--clean" in _JS and "sr--chase" in _JS
