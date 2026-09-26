"""Tests for ai_catalyst shadow logger (log-only; no network)."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ai_catalyst as ac

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ai_catalyst"
BOT_CONFIG = ROOT / "config" / "bot_config.json"


@pytest.fixture
def bot_mtime():
    before = BOT_CONFIG.stat().st_mtime if BOT_CONFIG.exists() else None
    yield before
    after = BOT_CONFIG.stat().st_mtime if BOT_CONFIG.exists() else None
    assert before == after, "config/bot_config.json mtime changed during tests"


@pytest.fixture
def cat_dir(tmp_path, monkeypatch):
    """Point report dir at tmp so catalyst writes stay isolated."""
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path / "ai_reports"))
    # Reset module memo of last slot between tests.
    ac._LAST_SLOT_MEM = None
    ac._CHILD = None
    d = ac.catalyst_dir()
    return d


def _weekday_ts(hour: int, minute: int, day: str = "2026-09-28") -> float:
    """2026-09-28 is a Monday."""
    return datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=5, tzinfo=ET,
    ).timestamp()


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def test_prompt_stable_and_ordered(bot_mtime):
    names = json.loads((FIXTURES / "prompt_names.json").read_text())
    # Cap headlines at 8 — fixture has 9 for AAPL.
    p1 = ac.build_prompt(names, max_headlines=8, summary_max=400)
    p2 = ac.build_prompt(names, max_headlines=8, summary_max=400)
    assert ac.prompt_sha256(p1) == ac.prompt_sha256(p2)
    assert "SHOULD NOT APPEAR" not in p1
    assert "Apple announces new product" in p1
    # Newest first: product headline before analyst.
    assert p1.index("Apple announces new product") < p1.index("Analyst upgrades AAPL")
    # Summary truncated to 400.
    assert ("x" * 500) not in p1
    assert ("x" * 400) in p1 or "expanded features" in p1
    # No leakage from outside fixture.
    assert "SENTINEL" not in p1
    assert "sk_test" not in p1


# ---------------------------------------------------------------------------
# Parse / validate
# ---------------------------------------------------------------------------

def _valid_json(tickers=("AAPL", "NVDA")):
    results = []
    for t in tickers:
        results.append({
            "ticker": t,
            "catalyst_type": "product",
            "direction": "up",
            "materiality": 4,
            "expected_horizon": "days",
            "confidence": 0.8,
            "rationale": f"{t} looks constructive on the news.",
        })
    return json.dumps({"results": results})


def test_parse_valid(bot_mtime):
    got, missing = ac.parse_model_response(_valid_json(), ["AAPL", "NVDA"])
    assert set(got) == {"AAPL", "NVDA"}
    assert missing == []
    assert got["AAPL"]["materiality"] == 4


def test_parse_fenced(bot_mtime):
    text = "```json\n" + _valid_json() + "\n```"
    got, missing = ac.parse_model_response(text, ["AAPL", "NVDA"])
    assert not missing and "AAPL" in got


def test_parse_prose_around(bot_mtime):
    text = "Here is my read.\n" + _valid_json() + "\nThanks."
    got, missing = ac.parse_model_response(text, ["AAPL", "NVDA"])
    assert not missing and "NVDA" in got


def test_parse_bad_enum(bot_mtime):
    bad = json.dumps({"results": [{
        "ticker": "AAPL", "catalyst_type": "moonshot", "direction": "up",
        "materiality": 3, "expected_horizon": "days", "confidence": 0.5,
        "rationale": "nope",
    }]})
    got, missing = ac.parse_model_response(bad, ["AAPL"])
    assert got == {} and missing == ["AAPL"]


def test_parse_materiality_out_of_range(bot_mtime):
    bad = json.dumps({"results": [{
        "ticker": "AAPL", "catalyst_type": "other", "direction": "up",
        "materiality": 9, "expected_horizon": "days", "confidence": 0.5,
        "rationale": "nope",
    }]})
    got, missing = ac.parse_model_response(bad, ["AAPL"])
    assert got == {} and "AAPL" in missing


def test_parse_confidence_string(bot_mtime):
    # float("0.7") works — but we also accept numeric strings via float().
    ok = json.dumps({"results": [{
        "ticker": "AAPL", "catalyst_type": "other", "direction": "neutral",
        "materiality": 2, "expected_horizon": "intraday", "confidence": "0.7",
        "rationale": "mixed",
    }]})
    got, missing = ac.parse_model_response(ok, ["AAPL"])
    assert "AAPL" in got and abs(got["AAPL"]["confidence"] - 0.7) < 1e-9


def test_parse_extra_and_missing_ticker(bot_mtime):
    text = json.dumps({"results": [
        {"ticker": "AAPL", "catalyst_type": "other", "direction": "up",
         "materiality": 3, "expected_horizon": "days", "confidence": 0.5,
         "rationale": "ok"},
        {"ticker": "TSLA", "catalyst_type": "other", "direction": "up",
         "materiality": 3, "expected_horizon": "days", "confidence": 0.5,
         "rationale": "extra"},
    ]})
    got, missing = ac.parse_model_response(text, ["AAPL", "NVDA"])
    assert set(got) == {"AAPL"}
    assert "TSLA" not in got
    assert missing == ["NVDA"]


def test_score_batch_retry_then_ok(bot_mtime, cat_dir):
    calls = {"n": 0}

    def fake(fam, prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not json at all"
        return _valid_json(["AAPL"])

    batch = [{
        "symbol": "AAPL", "company": "Apple", "prior_close": 1, "last": 1,
        "last_ts": time.time(), "age_sec": 1, "news": [{
            "id": "alpaca:1", "ts": time.time(), "headline": "h",
            "summary": "s", "url": "u", "source": "x",
        }],
        "sources": ["universe"],
    }]
    rows = ac.score_batch(
        "grok", batch, {"grok_model": "g", "agy_model": "a"},
        day="2026-09-28", slot="2026-09-28T08:45", run_ts=time.time(),
        spy=None, company_names={"AAPL": "Apple"}, sources_map={"AAPL": ["universe"]},
        cli_ver="test", call_fn=fake,
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["retry_used"] is True
    assert calls["n"] == 2


def test_score_batch_two_failures_parse_fail(bot_mtime, cat_dir):
    def fake(fam, prompt, **kw):
        return "still junk {{{"

    batch = [{
        "symbol": "AAPL", "company": "Apple", "prior_close": 1, "last": 1,
        "last_ts": time.time(), "age_sec": 1, "news": [{
            "id": "alpaca:1", "ts": time.time(), "headline": "h",
            "summary": "s", "url": "u", "source": "x",
        }],
        "sources": ["universe"],
    }]
    rows = ac.score_batch(
        "agy", batch, {"grok_model": "g", "agy_model": "a"},
        day="2026-09-28", slot="2026-09-28T08:45", run_ts=time.time(),
        spy=None, company_names={}, sources_map={"AAPL": ["universe"]},
        cli_ver="test", call_fn=fake,
    )
    assert rows[0]["status"] == "parse_fail"
    assert rows[0]["retry_used"] is True


# ---------------------------------------------------------------------------
# Dedupe / selection
# ---------------------------------------------------------------------------

def test_dedupe_seen_and_other_model(bot_mtime, cat_dir):
    day = "2026-09-25"
    prior = {
        "kind": "score", "day": day, "symbol": "AAPL", "model_family": "grok",
        "status": "ok", "news_ids": ["alpaca:1", "alpaca:2"],
    }
    ac.append_jsonl(cat_dir / f"{day}.jsonl", prior)
    # Failed status also counts as seen.
    ac.append_jsonl(cat_dir / f"{day}.jsonl", {
        "kind": "score", "day": day, "symbol": "NVDA", "model_family": "agy",
        "status": "timeout", "news_ids": ["alpaca:9"],
    })
    seen = ac.load_seen_news_ids(cat_dir, lookback_days=5, asof_day="2026-09-28")
    assert ("AAPL", "grok", "alpaca:1") in seen
    assert ("NVDA", "agy", "alpaca:9") in seen

    by_sym = {
        "AAPL": [
            {"id": "alpaca:1", "ts": 1.0},
            {"id": "alpaca:3", "ts": 2.0},
        ],
        "NVDA": [{"id": "alpaca:9", "ts": 3.0}],
        "JPM": [{"id": "alpaca:10", "ts": 4.0}],
    }
    grok_fresh = ac.filter_new_news(by_sym, seen, "grok")
    agy_fresh = ac.filter_new_news(by_sym, seen, "agy")
    # Grok still scores AAPL on alpaca:3; NVDA fully new for grok.
    assert [x["id"] for x in grok_fresh["AAPL"]] == ["alpaca:3"]
    assert "NVDA" in grok_fresh
    # AGY still scores AAPL fully (other model); NVDA blocked.
    assert "AAPL" in agy_fresh
    assert "NVDA" not in agy_fresh
    assert "JPM" in agy_fresh


def test_ineligible_without_new_news(bot_mtime):
    sources = {"AAPL": ["universe"], "NVDA": ["universe"]}
    news_by_model = {
        "grok": {"AAPL": [{"id": "a", "ts": 10.0}]},
        "agy": {},
    }
    kept, dropped = ac.pick_eligible(sources, news_by_model, max_names=60)
    assert kept == ["AAPL"]
    assert dropped == 0


def test_cap_60_newest_first(bot_mtime):
    sources = {f"S{i:03d}": ["universe"] for i in range(80)}
    news = {
        "grok": {
            f"S{i:03d}": [{"id": f"n{i}", "ts": float(i)}] for i in range(80)
        },
        "agy": {},
    }
    kept, dropped = ac.pick_eligible(sources, news, max_names=60)
    assert len(kept) == 60
    assert dropped == 20
    assert kept[0] == "S079"
    assert kept[-1] == "S020"


def test_gapper_filter(bot_mtime):
    now = time.time()
    snaps = {
        "GOOD": {
            "last": 50.0, "last_ts": now - 10, "age_sec": 10,
            "prior_close": 40.0, "prior_volume": 1_000_000,
        },  # gap 25%, dollars 40M
        "CHEAP": {
            "last": 10.0, "last_ts": now - 10, "age_sec": 10,
            "prior_close": 8.0, "prior_volume": 5_000_000,
        },
        "SMALLGAP": {
            "last": 50.0, "last_ts": now - 10, "age_sec": 10,
            "prior_close": 49.0, "prior_volume": 1_000_000,
        },
        "STALE": {
            "last": 50.0, "last_ts": now - 2000, "age_sec": 2000,
            "prior_close": 40.0, "prior_volume": 1_000_000,
        },
        "THIN": {
            "last": 50.0, "last_ts": now - 10, "age_sec": 10,
            "prior_close": 40.0, "prior_volume": 100,
        },
        "TOP2": {
            "last": 100.0, "last_ts": now - 5, "age_sec": 5,
            "prior_close": 50.0, "prior_volume": 1_000_000,
        },
    }
    rows = ac.select_gappers(snaps, now=now, top=1)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "TOP2"


def test_stale_boards_ignored(bot_mtime, tmp_path, monkeypatch):
    day = "2026-09-28"
    board = tmp_path / "movers_stocks.json"
    board.write_text(json.dumps({
        "ts": datetime(2026, 9, 20, 10, 0, tzinfo=ET).timestamp(),
        "rows": [{"symbol": "OLD1"}],
    }))
    monkeypatch.setattr(ac, "ROOT", tmp_path)
    # Also need empty other boards
    for name in ("trending_stocks.json", "grok_suggestions.json",
                 "seed_rank_gx.json", "claude_suggestions.json"):
        (tmp_path / name).write_text(json.dumps({
            "updated": datetime(2026, 9, 20, 10, 0, tzinfo=ET).timestamp(),
            "rows": [{"symbol": "OLDX"}],
        }))
    # Fake ai_paths files via monkeypatch of load_seed_boards internals
    boards = ac.load_seed_boards(day)
    assert boards["seed_movers"] == []
    assert boards["seed_trending"] == []


# ---------------------------------------------------------------------------
# Schedule / tick
# ---------------------------------------------------------------------------

def test_due_weekday_catchup(bot_mtime, cat_dir):
    ac._save_last_slot("")
    # Monday 08:50 within 40m catch-up of 08:45
    slot = ac.due(_weekday_ts(8, 50))
    assert slot == "2026-09-28T08:45"
    # Weekend
    sat = datetime(2026, 9, 26, 9, 0, tzinfo=ET).timestamp()
    assert ac.due(sat, last_slot="") is None
    # After catch-up window
    assert ac.due(_weekday_ts(10, 0), last_slot="") is None
    # 12:15 catch-up
    assert ac.due(_weekday_ts(12, 30), last_slot="2026-09-28T08:45") == "2026-09-28T12:15"


def test_due_no_double(bot_mtime, cat_dir):
    ac._save_last_slot("2026-09-28T08:45")
    assert ac.due(_weekday_ts(8, 55)) is None


def test_tick_kill_switch(bot_mtime, cat_dir, monkeypatch):
    spawned = []

    def fake_spawn(args):
        spawned.append(args)
        return True

    monkeypatch.setattr(ac, "_spawn", fake_spawn)
    assert ac.tick({"ai_catalyst_log_enabled": False}, _weekday_ts(8, 50)) is None
    assert spawned == []


def test_tick_spawns_and_writes_last_slot(bot_mtime, cat_dir, monkeypatch):
    spawned = []
    monkeypatch.setattr(ac, "_spawn", lambda args: spawned.append(args) or True)
    ac._save_last_slot("")
    msg = ac.tick({"ai_catalyst_log_enabled": True}, _weekday_ts(8, 50))
    assert msg and "run started" in msg
    assert spawned and spawned[0][0] == "--run"
    assert ac._load_last_slot() == "2026-09-28T08:45"


def test_tick_dryrun_request(bot_mtime, cat_dir, monkeypatch):
    spawned = []
    monkeypatch.setattr(ac, "_spawn", lambda args: spawned.append(args) or True)
    req = ac.dryrun_request_path()
    req.write_text("AAPL,NVDA,JPM\n", encoding="utf-8")
    msg = ac.tick({"ai_catalyst_log_enabled": True}, time.time())
    assert msg == "dryrun started"
    assert not req.exists()
    assert ac.dryrun_taken_path().exists()
    assert spawned[0][:2] == ["--dry-run", "--symbols"]
    assert "AAPL" in spawned[0][2]


def test_dry_run_never_appends_day_log(bot_mtime, cat_dir, monkeypatch):
    monkeypatch.setattr(ac, "probe_auth", lambda cfg: {"grok": True, "agy": False})
    monkeypatch.setattr(ac, "cli_version", lambda b: "test")
    monkeypatch.setattr(ac, "fetch_alpaca_news", lambda *a, **k: {
        "AAPL": [{"id": "alpaca:1", "ts": time.time(), "headline": "h",
                  "summary": "s", "url": "u", "source": "x", "symbols": ["AAPL"]}],
    })
    monkeypatch.setattr(ac, "fetch_snapshots", lambda syms, **k: {
        "AAPL": {"last": 1, "last_ts": time.time(), "age_sec": 1,
                 "prior_close": 1, "prior_volume": 1, "source": "alpaca_iex_snapshot"},
        "SPY": {"last": 1, "last_ts": time.time(), "age_sec": 1,
                "prior_close": 1, "prior_volume": 1, "source": "alpaca_iex_snapshot"},
    })
    monkeypatch.setattr(ac, "load_scan_universe_assets", lambda: [("AAPL", "Apple")])

    def fake(fam, prompt, **kw):
        return _valid_json(["AAPL"])

    day = _et_day()
    rows = ac.run_dry(
        ["AAPL"], cfg={"grok_model": "g", "agy_model": "a", "grok_cli_bin": "grok"},
        models=["grok"], call_fn=fake, now=_weekday_ts(10, 0),
    )
    assert rows and rows[0]["status"] == "ok"
    assert not (cat_dir / f"{day}.jsonl").exists()
    assert not ac.runs_log_path().exists() or ac.runs_log_path().read_text() == ""


def _et_day() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def test_no_secrets_in_rows(bot_mtime, cat_dir, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "sk_test_SENTINEL")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret_SENTINEL")
    monkeypatch.setattr(ac, "probe_auth", lambda cfg: {"grok": True, "agy": False})
    monkeypatch.setattr(ac, "cli_version", lambda b: "test")
    monkeypatch.setattr(ac, "fetch_alpaca_news", lambda *a, **k: {
        "AAPL": [{"id": "alpaca:1", "ts": time.time(), "headline": "h",
                  "summary": "s", "url": "u", "source": "x", "symbols": ["AAPL"]}],
    })
    monkeypatch.setattr(ac, "fetch_snapshots", lambda syms, **k: {
        "AAPL": {"last": 1, "last_ts": time.time(), "age_sec": 1,
                 "prior_close": 1, "prior_volume": 1, "source": "alpaca_iex_snapshot"},
        "SPY": {"last": 1, "last_ts": time.time(), "age_sec": 1,
                "prior_close": 1, "prior_volume": 1, "source": "alpaca_iex_snapshot"},
    })
    monkeypatch.setattr(ac, "load_scan_universe_assets", lambda: [("AAPL", "Apple")])

    def fake(fam, prompt, **kw):
        return _valid_json(["AAPL"])

    rows = ac.run_dry(
        ["AAPL"], cfg={"grok_model": "g", "agy_model": "a"},
        models=["grok"], call_fn=fake, now=_weekday_ts(10, 0),
    )
    blob = json.dumps(rows)
    assert "SENTINEL" not in blob
    # Also check dryrun out file
    for p in (cat_dir / "dryrun").glob("*.json"):
        assert "SENTINEL" not in p.read_text()


def test_news_window_1215_uses_morning_end(bot_mtime, cat_dir):
    day = "2026-09-28"
    ac.append_jsonl(ac.runs_log_path(), {
        "kind": "run", "day": day, "slot": f"{day}T08:45",
        "news_start": 1.0, "news_end": 1759059900.0, "gappers": ["AAA"],
    })
    start, end = ac.news_window(day, "12:15", 1759072500.0)
    assert start == 1759059900.0
    assert end == 1759072500.0


def test_universe_file_has_100(bot_mtime):
    uni = ac.load_universe()
    assert len(uni["symbols"]) == 100
    assert len(uni["etfs"]) == 10
