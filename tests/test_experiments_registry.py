import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import experiments as ex


def rec(**k):
    r = {"id": "A", "title": "t", "layer": "exit", "source": "s", "status": "idea", "evidence": "e", "next": "n", "blocked_on": "", "gates": {}}
    r.update(k)
    return r


def test_blocked_needs_reason():
    assert any("blocked_on" in e for e in ex.validate([rec(status="blocked")]))


def test_running_needs_smoke():
    g = {"prereg": True, "tool": True, "smoke": False}
    assert any("smoke" in e for e in ex.validate([rec(status="running", gates=g)]))


def test_verdict_needs_data_check():
    g = {"prereg": True, "tool": True, "smoke": True, "data_check": False}
    assert any("data_check" in e for e in ex.validate([rec(status="verdict_fail", gates=g, next="")]))


def test_legacy_gates_are_grandfathered():
    g = {"prereg": True, "tool": True, "smoke": "legacy", "data_check": "legacy"}
    assert ex.validate([rec(status="verdict_fail", gates=g, next="")]) == []


def test_duplicate_and_bad_status():
    errs = ex.validate([rec(), rec(status="weird")])
    assert any("duplicate" in e for e in errs) and any("bad status" in e for e in errs)


def test_real_registry_is_valid():
    assert ex.validate(ex.load()["items"]) == []


def test_pending_smoke_is_a_warning_not_an_error():
    g = {"prereg": True, "tool": True, "smoke": "pending"}
    items = [rec(status="running", gates=g)]
    assert ex.validate(items) == [] and len(ex.warnings(items)) == 1


def test_edge_verdict_is_a_verdict_and_component_rules():
    g = {"prereg": True, "tool": True, "smoke": "legacy", "data_check": "legacy"}
    assert ex.validate([rec(status="verdict_edge", gates=g, next="", component=True)]) == []
    assert any("component" in e for e in ex.validate([rec(status="idea", component=True)]))
