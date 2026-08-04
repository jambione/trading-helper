"""Agent command bus (v1) — pure dispatch without pyautogui."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_bus import (  # noqa: E402
    ALL_ACTIONS,
    BusDeps,
    append_journal,
    dispatch,
    legacy_add_to_action,
    list_actions,
    normalize_action,
    parse_request,
    publish_focus_file,
    validate_symbol,
)


def test_normalize_and_aliases():
    assert normalize_action("load_tv") == "load_tv"
    assert normalize_action("TV") == "load_tv"
    assert normalize_action("add-tv") == "add_tv"
    assert normalize_action("focus_tv") == "focus"
    assert legacy_add_to_action("tv") == "add_tv"
    assert legacy_add_to_action("both") == "add"
    assert validate_symbol("SOFI")
    assert validate_symbol("BRK.B")
    assert not validate_symbol("")
    assert not validate_symbol("1ABC")


def test_parse_request_symbol_aliases():
    a, s, src, meta = parse_request({
        "action": "load_tv",
        "ticker": "sofi",
        "source": "toast",
        "reason": "burst",
    })
    assert a == "load_tv"
    assert s == "SOFI"
    assert src == "toast"
    assert meta.get("reason") == "burst"


def test_list_actions_covers_catalog():
    names = {a["action"] for a in list_actions()}
    assert names == set(ALL_ACTIONS)
    assert all("description" in a for a in list_actions())


def test_dispatch_ping():
    deps = BusDeps(enqueue=lambda *_: None)
    r = dispatch(deps, {"action": "ping"})
    assert r.ok and r.result == "pong"
    assert r.http_status == 200


def test_dispatch_load_tv_queues():
    q: list[tuple[str, str]] = []
    deps = BusDeps(enqueue=lambda sym, mode: q.append((sym, mode)))
    r = dispatch(deps, {
        "action": "load_tv",
        "symbol": "SMCI",
        "source": "monitor",
    })
    assert r.ok and r.queued and r.http_status == 202
    assert q == [("SMCI", "tv")]


def test_dispatch_add_mode_both():
    q: list[tuple[str, str]] = []
    deps = BusDeps(enqueue=lambda sym, mode: q.append((sym, mode)))
    r = dispatch(deps, {"action": "add", "ticker": "HOOD", "mode": "both"})
    assert r.ok and q == [("HOOD", "both")]


def test_dispatch_missing_symbol():
    deps = BusDeps(enqueue=lambda *_: None)
    r = dispatch(deps, {"action": "load_tv"})
    assert not r.ok and r.http_status == 400


def test_dispatch_unknown_action():
    deps = BusDeps(enqueue=lambda *_: None)
    r = dispatch(deps, {"action": "teleport", "symbol": "AAPL"})
    assert not r.ok and "unknown" in (r.error or "")


def test_dispatch_focus_publishes_and_loads(tmp_path):
    q: list[tuple[str, str]] = []
    focus: list[tuple[str, str]] = []
    deps = BusDeps(
        enqueue=lambda sym, mode: q.append((sym, mode)),
        publish_focus=lambda sym, src: focus.append((sym, src)),
    )
    r = dispatch(deps, {
        "action": "focus",
        "symbol": "IREN",
        "source": "ai",
    })
    assert r.ok and r.queued
    assert focus == [("IREN", "ai")]
    assert q == [("IREN", "tv")]


def test_dispatch_journal(tmp_path):
    jpath = tmp_path / "agent_actions.jsonl"
    deps = BusDeps(enqueue=lambda *_: None, journal_path=jpath)
    r = dispatch(deps, {
        "action": "journal",
        "symbol": "SOFI",
        "source": "manual",
        "reason": "liked the setup",
        "meta": {"score": 8.2},
    })
    assert r.ok and r.result == "logged"
    lines = jpath.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["symbol"] == "SOFI"
    assert rec["meta"].get("reason") == "liked the setup"


def test_publish_focus_file(tmp_path):
    p = tmp_path / "active_symbol.json"
    publish_focus_file("NVDA", source="test", path=p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["symbol"] == "NVDA"
    assert data["source"] == "test"


def test_add_wb_retired():
    deps = BusDeps(enqueue=lambda *_: (_ for _ in ()).throw(AssertionError("no enqueue")))
    r = dispatch(deps, {"action": "add_wb", "symbol": "X"})
    assert r.ok and r.result == "retired"


def test_append_journal_threadsafe_path(tmp_path):
    p = tmp_path / "j.jsonl"
    append_journal(action="note", symbol="A", source="t", meta={}, path=p)
    append_journal(action="note", symbol="B", source="t", meta={}, path=p)
    assert len(p.read_text().splitlines()) == 2
