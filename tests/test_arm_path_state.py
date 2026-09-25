"""The arm decision's process state: who may write it.

2026-09-25: the trader's book paint called should_arm_buy every publish and
advanced the mid-rise latch the poll depends on; it ate ~2/3 of the day's
arms (replay 56 -> 19 opens). These tests keep that class of bug visible.
"""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_entry_watch as ew  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_entry_watch.py")

# Module state the arm path is allowed to write, and why. Adding to this list
# is a decision: if the state changes what a LATER arm decision sees, only the
# poll may advance it (see _MID_RISE_PEEK), and the replay must run every
# caller that touches it.
ALLOWED = {
    "_MID_RISE_STATE": "decision state; paint is read-only via _MID_RISE_PEEK",
    "_LAST_QUOTE_TS": "decision state (price clock); paint restamps it — open "
                      "question in docs/AFTER_CLOSE_2026-09-25.md item 21",
    "_GAP_CACHE": "data cache",
    "_SIP_SPREAD_CACHE": "data cache",
    "_RVOL_PACE_CACHE": "data cache",
    "_AVG_VOL_CACHE": "data cache",
    "_RVOL_OBS_LOGGED": "log de-duplication",
}


def _arm_path_writes() -> dict[str, set[str]]:
    tree = ast.parse(open(SRC).read())
    mutable = set()
    for n in tree.body:
        targets = n.targets if isinstance(n, ast.Assign) else (
            [n.target] if isinstance(n, ast.AnnAssign) else [])
        for t in targets:
            if not (isinstance(t, ast.Name) and re.match(r"^_[A-Z0-9_]+$", t.id)):
                continue
            v = n.value
            ctor = isinstance(v, ast.Call) and getattr(v.func, "id", getattr(v.func, "attr", "")) in (
                "dict", "set", "defaultdict", "OrderedDict", "deque")
            if isinstance(v, (ast.Dict, ast.Set, ast.List)) or ctor:
                mutable.add(t.id)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def writes(fn):
        out = set()
        for n in ast.walk(fn):
            if isinstance(n, (ast.Assign, ast.AugAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                            and t.value.id in mutable:
                        out.add(t.value.id)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in mutable \
                    and n.func.attr in ("pop", "setdefault", "update", "clear", "add",
                                        "append", "discard", "popitem"):
                out.add(n.func.value.id)
        return out

    seen, stack, found = set(), ["should_arm_buy"], {}
    while stack:
        f = stack.pop()
        if f in seen or f not in funcs:
            continue
        seen.add(f)
        for g in writes(funcs[f]):
            found.setdefault(g, set()).add(f)
        stack += [n.func.id for n in ast.walk(funcs[f])
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    return found


def test_no_new_state_is_written_on_the_arm_path():
    found = _arm_path_writes()
    new = {k: sorted(v) for k, v in found.items() if k not in ALLOWED}
    assert not new, (
        f"should_arm_buy now writes module state {new}. If a later decision reads "
        "it, only the poll may advance it and the replay must run every caller; "
        "then add it to ALLOWED with the reason.")


def test_the_book_paint_leaves_the_latch_alone(monkeypatch):
    """apply_tape_blocker is the paint's per-row entry (book_table_rows and the
    dashboard overlay). Whatever the arm check does inside it, the latch the
    poll depends on must come out unchanged."""
    on = {"ai_watch_exh_mid_rise_arm": True, "ai_watch_mid_rise_level": -50.0,
          "ai_watch_mid_rise_max_age_sec": 60.0}
    ew._MID_RISE_STATE.clear()
    poll = {"symbol": "SMCI", "indicator": {"pctr": -56.6, "pctr_slow_rising": True}}
    ew._mid_rise_allows_buy(poll, on, now=100)
    before = dict(ew._MID_RISE_STATE)
    reached = []

    def arm(rec, **kw):                     # the real latch, fed the paint's view
        reached.append(1)
        return ew._mid_rise_allows_buy(
            {"symbol": "SMCI", "indicator": {"pctr": -49.6, "pctr_slow_rising": False}},
            on, now=101)
    monkeypatch.setattr(ew, "should_arm_buy", arm)
    monkeypatch.setattr(ew, "_push_cfg", lambda: dict(on))
    monkeypatch.setattr(ew, "_row_tape_stale", lambda *a, **k: False)
    monkeypatch.setattr(ew, "arm_at_last", lambda *a, **k: True)
    row = {"symbol": "SMCI", "ticker": "SMCI", "entry_low": 42.0, "entry_high": 43.0,
           "pctr": -49.6, "last_ask_src": "stream"}
    try:
        ew.apply_tape_blocker(row, 42.85)
        after = dict(ew._MID_RISE_STATE)
    finally:
        ew._MID_RISE_STATE.clear()
    assert reached, "the paint must reach the arm check for this test to mean anything"
    assert after == before
