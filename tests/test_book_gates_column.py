"""Book panel Gates column (operator 2026-10-15, display only).

Replaces the SR column (the ob_resist skip is off and S/R did not rescue the arm). Shows the arm's live gate
readings while they pass: RVOL vs its floor, SIP spread vs the cap, last-print age vs the tape ceiling, plus a
gap-down block or the after-square wait. Values come from the trader's own caches (_gate_wire_fields); nothing
here fetches or touches the arm path. ob_observe keeps logging for the S/R breakout forward test.
"""
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
_JS = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
_KEYS = ("g_rvol", "g_rvol_min", "g_spread", "g_spread_max", "g_tape_age", "g_tape_max", "g_gap", "g_gap_block",
         "g_sq_wait")


def _row_template() -> str:
    i = _JS.index("function _bookRowHtml(")
    return _JS[i:_JS.index("\n}", i)]


def test_buy_button_is_gone_from_the_book_row():
    t = _row_template()
    assert "book-call-btn" not in t and "Buy?" not in t


def test_gates_cell_replaces_sr_after_exh_in_row_and_header():
    t = _row_template()
    assert "cell-sr" not in t and "_bookSr" not in _JS
    assert t.index("cell-exh") < t.index("cell-gates") < t.index("cell-pl")
    i = _HTML.index("ai-book-table-header")
    block = _HTML[i:_HTML.index("data-ai-book-rows", i)]
    assert "th-sr" not in block
    assert block.index("th-exh") < block.index('th th-gates" data-book-sort-col="gates"') < block.index("th-pl")


def test_gate_fields_survive_the_client_row_whitelist_and_are_patched_in_place():
    for k in _KEYS:
        assert f"{k}: w.{k} != null" in _JS, k
    assert "const gatesEl = el.querySelector('.cell-gates');" in _JS


def test_wire_fields_read_caches_only(monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_watch_arm_min_rvol": 0.45, "ai_watch_max_sip_spread_pct": 0.15, "ai_watch_decision_max_age_sec": 8.0,
        "ai_watch_gap_down_block_pct": 1.0, "ai_watch_presquare_after_square_min": 10})
    monkeypatch.setattr(ew, "live_print", lambda s: (10.0, 4.2))
    monkeypatch.setitem(ew._SIP_SPREAD_CACHE, "ABC", (0.08, 1.0))
    monkeypatch.setitem(ew._GAP_CACHE, "ABC", (-2.5, 1.0, "sip"))
    monkeypatch.setitem(ew._LAST_SQUARE, "ABC", [1000.0, 1000.0])
    out = ew._gate_wire_fields({"rvol": 0.3, "source": "movers"}, "ABC", now=1300.0)
    assert set(out) == set(_KEYS)
    assert out["g_rvol"] == 0.3 and out["g_rvol_min"] == 0.45
    assert out["g_spread"] == 0.08 and out["g_spread_max"] == 0.15
    assert out["g_tape_age"] == 4.2 and out["g_tape_max"] >= 8.0
    assert out["g_gap"] == -2.5 and out["g_gap_block"] == 1.0
    assert out["g_sq_wait"] == 300


def test_wire_fields_never_raise_and_unknown_is_none(monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "live_print", lambda s: None)
    out = ew._gate_wire_fields(None, "NOPE")
    assert out.get("g_spread") is None and out.get("g_tape_age") is None and out.get("g_sq_wait") is None
    monkeypatch.setattr(ew, "_push_cfg", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ew._gate_wire_fields({}, "ABC") == {}


def test_book_rows_carry_gate_fields(monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "live_print", lambda s: (10.0, 3.0))
    rows = ew.book_table_rows(positions={}, state={"ABC": {"symbol": "ABC", "status": "watching", "rvol": 1.2}})
    r = [x for x in rows if x["symbol"] == "ABC"][0]
    assert r["g_rvol"] == 1.2 and r["g_tape_age"] == 3.0
    assert not any(k.startswith("ob_") for k in r)


def test_gates_text_and_class_mark_each_check():
    i = _JS.index("function _bookGateChecks(")
    body = _JS[i:_JS.index("\nfunction _bookGatesText(", i)]
    for frag in ("rv + 1e-12 >= rvMin", "sp <= spMax + 1e-12", "age <= ageMax + 1e-9", "gap < -block"):
        assert frag in body, frag
    assert "gates--fail" in _JS and "gates--ok" in _JS and "gates--unknown" in _JS


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


def test_status_stale_uses_the_arms_reading_and_fails_sort_first():
    i = _JS.index("function _bookTapeStale(")
    body = _JS[i:_JS.index("\n}", i)]
    assert body.index("r.g_tape_age") < body.index("last_ask_src")
    j = _JS.index("function _bookGatesText(")
    assert "x.ok === false ? 0" in _JS[j:_JS.index("\n}", j)]
