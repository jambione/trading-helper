import importlib.util, os, sys
_d = os.path.join(os.path.dirname(__file__), "..", "tools", "studies")
sys.path.insert(0, _d)
_s = importlib.util.spec_from_file_location("ai_hold_check", os.path.join(_d, "ai_hold_check.py"))
A = importlib.util.module_from_spec(_s)
_s.loader.exec_module(A)
BS = A.BS

DAY = "2026-10-13"


def bar(hm, o, h, l, c):
    return [BS.et_ts(DAY, hm // 60, hm % 60), o, h, l, c, 1000]


def test_parse_normalizes_markdown_and_case():
    assert A.parse_answer("**HOLD**\nconfidence 4\nno news found") == ("HOLD", 4)
    assert A.parse_answer("exit.\n2\nGuidance cut") == ("EXIT", 2)
    assert A.parse_answer("I would hold") == (None, None)
    assert A.parse_answer("") == (None, None)


def test_pick_checked_qualifiers_first_then_seeded_eligible_capped():
    pool = [{"sym": f"E{i}", "q": False, "eligible": True, "families": 1, "dv": 1e7} for i in range(40)]
    pool += [{"sym": "Q1", "q": True, "eligible": True, "families": 1, "dv": 6e6},
             {"sym": "Q2", "q": True, "eligible": True, "families": 3, "dv": 5e6},
             {"sym": "X", "q": False, "eligible": False, "families": 3, "dv": 1e9}]
    a = A.pick_checked(pool, DAY)
    assert [p["sym"] for p in a[:2]] == ["Q2", "Q1"] and len(a) == 30 and "X" not in {p["sym"] for p in a}
    assert [p["sym"] for p in a] == [p["sym"] for p in A.pick_checked(list(reversed(pool)), DAY)]   # seeded, order-free


def path(prices, start=630):
    return [bar(570, 100, 100, 99, 100)] + [bar(start + k, p, p, p, p) for k, p in enumerate(prices)]


def test_held_before_and_remaining_from_the_answer_bar():
    b = path([100, 100.5, 101, 101, 101.5, 102, 102, 102])
    ok, entry = A.held_before(b, ml=99.0, i0=5)
    assert ok and entry == 100
    assert round(A.remaining(b, 99.0, entry, 5), 2) == round(1e4 * (102 / 101.5 - 1), 2)   # time exit at the last close


def test_loss_limit_before_answer_means_not_held():
    b = path([100, 96.5, 96.5, 96.5])                     # low 96.5 <= 97 stop on the 10:31 bar
    assert A.held_before(b, ml=90.0, i0=3)[0] is False


def test_twin_labels_the_worst_n_with_symbol_tiebreak():
    items = [{"sym": "B", "pct": -0.01}, {"sym": "A", "pct": -0.01}, {"sym": "C", "pct": 0.02}]
    hold, ex = A.twin_split(items, 1, "pct")
    assert [x["sym"] for x in ex] == ["A"] and {x["sym"] for x in hold} == {"B", "C"}


def test_cites_needs_a_phrase_or_a_distinctive_headline_token():
    items = [{"headline": "Fermus Wanted $30 Billion Valuation on Just $51 Million in Revenue", "summary": ""}]
    assert A.cites("EXIT\n3\nFermus valuation concerns weigh on the name", items, "NVDA", "NVIDIA") in ("fermus", "valuation")
    assert A.cites("HOLD\n3\nno news found", items, "NVDA", "NVIDIA") is None
    assert A.says_no_news("HOLD\n2\nNo news; price is holding")


def test_twin_news_exits_had_news_names_first():
    items = [{"sym": "A", "news": 0, "pct": -0.05}, {"sym": "B", "news": 1, "pct": 0.02}, {"sym": "C", "news": 0, "pct": 0.01}]
    for i in items:
        i["news_key"] = (-i["news"], i["pct"])
    hold, ex = A.twin_split(items, 1, "news_key")
    assert [x["sym"] for x in ex] == ["B"]
