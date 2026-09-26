"""Unit tests for tools/ai_catalyst_score.py — no network.

Synthetic 1-minute and daily closes for AAA/BBB/CCC + SPY; calendar and
fetchers are injected. Asserts config/bot_config.json mtime is unchanged.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import ai_catalyst_score as S
import bars

import ai_paths

FIX = ROOT / "tests" / "fixtures" / "ai_catalyst"
BOT_CONFIG = ROOT / "config" / "bot_config.json"
ET = bars.ET

SYMS = ["AAA", "BBB", "CCC", "SPY"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(day: str, hour: int, minute: int, second: int = 0) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, second=second, microsecond=0, tzinfo=ET,
    ).timestamp()


def _rth_stamps(day: str) -> list[float]:
    """1-minute bar starts 09:30..15:59 inclusive."""
    out = []
    t = _ts(day, 9, 30)
    end = _ts(day, 16, 0)
    while t < end:
        out.append(t)
        t += 60.0
    return out


def _closes_flat(n: int, px: float) -> list[float]:
    return [float(px)] * n


def make_minute_store(days: list[str], prices: dict[str, dict[str, float]]) -> dict:
    """prices[sym][day] = entry-ish level; intraday path is flat at that px,
    except 15:50 which can be overridden via prices[sym][day + '#1550'].
    """
    store: dict[tuple[str, str], tuple] = {}
    for day in days:
        stamps = _rth_stamps(day)
        for sym in SYMS:
            base = float(prices.get(sym, {}).get(day, 100.0))
            px1550 = float(prices.get(sym, {}).get(f"{day}#1550", base))
            closes = []
            t1550 = _ts(day, 15, 50)
            for t in stamps:
                closes.append(px1550 if abs(t - t1550) < 0.5 else base)
            store[(sym, day)] = (stamps, closes)
    return store


def make_daily_store(prices: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """prices[sym][day] = daily close."""
    out: dict[str, dict[str, float]] = {s: {} for s in SYMS}
    for sym, by_day in prices.items():
        for day, px in by_day.items():
            if "#" in day:
                continue
            out.setdefault(sym, {})[day] = float(px)
    return out


def trading_days(n: int, start: str = "2026-01-05") -> list[str]:
    """Generate n weekdays starting at start (skips Sat/Sun only)."""
    d = datetime.strptime(start, "%Y-%m-%d")
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def calendar_fn_for(days: list[str]):
    dayset = sorted(days)

    def _cal(start: str, end: str) -> list[str]:
        return [d for d in dayset if start <= d <= end]

    return _cal


def score_row(
    *,
    day: str,
    slot: str,
    symbol: str,
    model: str,
    direction: str,
    materiality: int,
    confidence: float = 0.8,
    scored_hm: tuple[int, int] = (8, 45),
    status: str = "ok",
    sources: list[str] | None = None,
    row_id: str | None = None,
) -> dict:
    scored_ts = _ts(day, scored_hm[0], scored_hm[1])
    rid = row_id or f"{day}|{slot}|{symbol}|{model}"
    return {
        "row_id": rid,
        "kind": "score",
        "day": day,
        "slot": slot,
        "run_ts": scored_ts,
        "scored_ts": scored_ts,
        "symbol": symbol,
        "company": symbol,
        "sources": sources or ["universe"],
        "model_family": model,
        "model_id": model,
        "status": status,
        "direction": direction,
        "materiality": materiality,
        "expected_horizon": "days",
        "confidence": confidence,
        "catalyst_type": "earnings",
        "rationale": "test",
    }


def write_day_log(cdir: Path, day: str, rows: list[dict]) -> None:
    cdir.mkdir(parents=True, exist_ok=True)
    path = cdir / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def bot_mtime():
    before = BOT_CONFIG.stat().st_mtime_ns if BOT_CONFIG.exists() else None
    yield before
    after = BOT_CONFIG.stat().st_mtime_ns if BOT_CONFIG.exists() else None
    assert after == before, "config/bot_config.json mtime changed"


@pytest.fixture
def catalyst_env(tmp_path, monkeypatch, bot_mtime):
    """Redirect AI_REPORT_DIR and return (cdir, report_dir)."""
    report = tmp_path / "ai_reports"
    report.mkdir()
    monkeypatch.setenv("AI_REPORT_DIR", str(report))
    # Resolve through ai_paths so the scorer sees it.
    assert ai_paths.resolve_report_dir() == report
    cdir = report / "ai_catalyst"
    cdir.mkdir()
    return cdir, report


def _install_fetchers(monkeypatch, minute_store, daily_store):
    def fake_fetch_many(syms, day, feed="sip", chunk=100):
        return None

    def fake_fetch(sym, day, feed="sip"):
        return minute_store.get((str(sym).upper(), day), (None, None))

    def fake_daily(syms, start, end):
        out = {}
        for s in syms:
            s = str(s).upper()
            out[s] = {
                d: px for d, px in (daily_store.get(s) or {}).items()
                if start <= d <= end
            }
        return out

    return fake_fetch_many, fake_fetch, fake_daily


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------

def test_side_and_buckets():
    assert S.side_of("up") == 1
    assert S.side_of("down") == -1
    assert S.side_of("neutral") == 0
    assert S.materiality_bucket(1) == "1-2"
    assert S.materiality_bucket(3) == "3"
    assert S.materiality_bucket(5) == "4-5"
    assert S.confidence_bucket(0.4) == "<0.5"
    assert S.confidence_bucket(0.6) == "0.5-0.7"
    assert S.confidence_bucket(0.9) == ">=0.7"


def test_agreement_buckets():
    up4 = {"status": "ok", "direction": "up", "materiality": 4}
    up5 = {"status": "ok", "direction": "up", "materiality": 5}
    up2 = {"status": "ok", "direction": "up", "materiality": 2}
    dn4 = {"status": "ok", "direction": "down", "materiality": 4}
    assert S.agreement_bucket(up4, up5) == "agree_strong"
    assert S.agreement_bucket(up4, up2) == "agree_weak"
    assert S.agreement_bucket(up4, dn4) == "disagree"
    assert S.agreement_bucket(up4, None) == "single"
    assert S.agreement_bucket(up4, {"status": "parse_fail"}) == "single"


def test_entry_bar_choice_0845_and_1215():
    day = "2026-01-05"
    stamps = _rth_stamps(day)
    closes = _closes_flat(len(stamps), 100.0)

    # 08:45 → 09:30 bar
    gate = S.entry_gate_ts(_ts(day, 8, 45), day)
    assert gate == S.rth_open_ts(day)
    i = S.first_bar_at_or_after(stamps, gate)
    assert i == 0
    assert stamps[i] == _ts(day, 9, 30)

    # 12:15 exactly → 12:15 bar
    gate2 = S.entry_gate_ts(_ts(day, 12, 15), day)
    i2 = S.first_bar_at_or_after(stamps, gate2)
    assert stamps[i2] == _ts(day, 12, 15)

    # 12:15:30 → 12:16 bar
    gate3 = S.entry_gate_ts(_ts(day, 12, 15, 30), day)
    i3 = S.first_bar_at_or_after(stamps, gate3)
    assert stamps[i3] == _ts(day, 12, 16)

    et, ep = S._price_at_entry(stamps, closes, _ts(day, 8, 45), day)
    assert et == _ts(day, 9, 30)
    assert ep == 100.0


def test_signed_excess_and_5bp_net():
    # up: name +2%, base +1% → excess +1% → net 0.01 - 0.0005 = 0.0095 = 95 bp
    ex = S.signed_excess(1, 0.02, 0.01)
    assert ex == pytest.approx(0.01)
    assert S.to_bp(S.net_of(ex)) == pytest.approx(95.0)

    # down: name -2%, base -1% → r_name - r_base = -0.01; side=-1 → excess +0.01
    ex_d = S.signed_excess(-1, -0.02, -0.01)
    assert ex_d == pytest.approx(0.01)
    assert S.to_bp(S.net_of(ex_d)) == pytest.approx(95.0)

    # down that underperforms on the short: name flat, base -1%
    # excess = -1 * (0 - (-0.01)) = -0.01 → net -105 bp
    ex_bad = S.signed_excess(-1, 0.0, -0.01)
    assert S.to_bp(S.net_of(ex_bad)) == pytest.approx(-105.0)


def test_date_clustered_t_hand_computed():
    daily = {"2026-01-05": 0.01, "2026-01-06": 0.02, "2026-01-07": 0.015}
    got = S.date_clustered_stats(daily)
    vals = list(daily.values())
    mu = statistics.mean(vals)
    sd = statistics.stdev(vals)
    t = mu / (sd / math.sqrt(3))
    assert got["mean"] == pytest.approx(mu)
    assert got["t"] == pytest.approx(t)
    assert got["n_days"] == 3
    assert got["mean_bp"] == pytest.approx(mu * 10000)


def test_half_split():
    days = [f"2026-01-{d:02d}" for d in range(1, 11)]
    a, b = S.half_split_days(days)
    assert a == days[:5]
    assert b == days[5:]
    assert S.half_split_days([]) == ([], [])
    assert S.half_split_days(["only"]) == ([], ["only"])


def test_dedupe_symbol_day():
    rows = [
        {"symbol": "AAA", "day": "2026-01-05", "scored_ts": 200.0, "row_id": "b"},
        {"symbol": "AAA", "day": "2026-01-05", "scored_ts": 100.0, "row_id": "a"},
        {"symbol": "AAA", "day": "2026-01-06", "scored_ts": 100.0, "row_id": "c"},
        {"symbol": "BBB", "day": "2026-01-05", "scored_ts": 100.0, "row_id": "d"},
    ]
    out = S.dedupe_symbol_day(rows)
    assert [r["row_id"] for r in out] == ["a", "d", "c"]


# ---------------------------------------------------------------------------
# Integration with injected fetchers
# ---------------------------------------------------------------------------

def test_horizons_and_unmatured(catalyst_env, monkeypatch):
    cdir, report = catalyst_env
    days = trading_days(12, "2026-01-05")
    d0 = days[0]
    # Minute prices on d0: entry 100; 15:50 → 101 for AAA (+1%)
    # Universe BBB/CCC flat 100→100 at 15:50; SPY 100→100.5
    minute_px = {
        "AAA": {d0: 100.0, f"{d0}#1550": 101.0},
        "BBB": {d0: 100.0, f"{d0}#1550": 100.0},
        "CCC": {d0: 100.0, f"{d0}#1550": 100.0},
        "SPY": {d0: 100.0, f"{d0}#1550": 100.5},
    }
    # Daily closes: D+1, D+5, D+10
    daily_px = {
        "AAA": {
            d0: 100.0,
            days[1]: 102.0,   # +2% from entry 100
            days[5]: 105.0,   # +5%
            days[10]: 110.0,  # +10%
        },
        "BBB": {d0: 100.0, days[1]: 100.0, days[5]: 100.0, days[10]: 100.0},
        "CCC": {d0: 100.0, days[1]: 100.0, days[5]: 100.0, days[10]: 100.0},
        "SPY": {d0: 100.0, days[1]: 100.5, days[5]: 101.0, days[10]: 102.0},
    }
    # Need minute bars on every scoring day we might touch — only d0 scored.
    minute_store = make_minute_store([d0], minute_px)
    daily_store = make_daily_store(daily_px)
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)
    cal = calendar_fn_for(days)

    slot = f"{d0}T08:45"
    write_day_log(cdir, d0, [
        score_row(day=d0, slot=slot, symbol="AAA", model="grok",
                  direction="up", materiality=4, scored_hm=(8, 45)),
        score_row(day=d0, slot=slot, symbol="AAA", model="agy",
                  direction="up", materiality=5, scored_hm=(8, 45)),
    ])

    # asof = d0 → only h_1550 matured
    payload = S.run_score(
        asof=d0,
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=cal,
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
        write_outputs=True,
    )
    rid = f"{d0}|{slot}|AAA|grok"
    rets = payload["row_returns"][rid]
    assert rets["h_1550"]["matured"] is True
    assert rets["h_1550"]["r_name"] == pytest.approx(0.01)
    # base_ew: BBB+CCC = 0,0 → mean 0
    assert rets["h_1550"]["r_base_ew"] == pytest.approx(0.0)
    assert rets["h_1550"]["r_spy"] == pytest.approx(0.005)
    assert rets["h_d1"]["r_name"] is None
    assert rets["h_d5"]["r_name"] is None
    assert rets["h_d10"]["r_name"] is None
    assert payload["primary"]["verdict"]["verdict"] == "NOT YET"

    # asof = days[10] → all horizons matured
    payload2 = S.run_score(
        asof=days[10],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=cal,
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
        write_outputs=True,
    )
    rets2 = payload2["row_returns"][rid]
    assert rets2["h_d1"]["r_name"] == pytest.approx(0.02)
    assert rets2["h_d5"]["r_name"] == pytest.approx(0.05)
    assert rets2["h_d10"]["r_name"] == pytest.approx(0.10)
    # excess_base at h_d5: side*(0.05 - 0) = 0.05; net = 0.05 - 0.0005
    assert S.to_bp(S.net_of(S.signed_excess(1, 0.05, 0.0))) == pytest.approx(495.0)


def test_cache_freezing(catalyst_env, monkeypatch):
    cdir, report = catalyst_env
    days = trading_days(8, "2026-01-05")
    d0 = days[0]
    minute_px = {
        "AAA": {d0: 100.0, f"{d0}#1550": 101.0},
        "BBB": {d0: 100.0},
        "CCC": {d0: 100.0},
        "SPY": {d0: 100.0},
    }
    daily_px = {
        "AAA": {days[5]: 105.0},
        "BBB": {days[5]: 100.0},
        "CCC": {days[5]: 100.0},
        "SPY": {days[5]: 101.0},
    }
    minute_store = make_minute_store([d0], minute_px)
    daily_store = make_daily_store(daily_px)
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)
    cal = calendar_fn_for(days)
    slot = f"{d0}T08:45"
    write_day_log(cdir, d0, [
        score_row(day=d0, slot=slot, symbol="AAA", model="grok",
                  direction="up", materiality=4),
    ])

    S.run_score(
        asof=days[5],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=cal,
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
    )
    cache_path = cdir / "returns_cache.jsonl"
    assert cache_path.exists()
    cache1 = S.load_returns_cache(cache_path)
    rid = f"{d0}|{slot}|AAA|grok"
    key = (rid, "h_d5")
    assert cache1[key]["matured"] is True
    frozen = cache1[key]["r_name"]
    assert frozen == pytest.approx(0.05)

    # Corrupt the underlying daily store; matured cache must not recompute.
    daily_store["AAA"][days[5]] = 999.0
    calls = {"n": 0}

    def counting_daily(syms, start, end):
        calls["n"] += 1
        return fdaily(syms, start, end)

    payload2 = S.run_score(
        asof=days[5],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=cal,
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=counting_daily,
    )
    assert payload2["row_returns"][rid]["h_d5"]["r_name"] == pytest.approx(frozen)


def test_symbol_day_dedupe_in_slice(catalyst_env, monkeypatch):
    cdir, report = catalyst_env
    days = trading_days(8, "2026-01-05")
    d0 = days[0]
    minute_px = {s: {d0: 100.0, f"{d0}#1550": 101.0} for s in SYMS}
    daily_px = {s: {days[5]: 100.0} for s in SYMS}
    daily_px["AAA"][days[5]] = 110.0  # +10%
    minute_store = make_minute_store([d0], minute_px)
    daily_store = make_daily_store(daily_px)
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)
    cal = calendar_fn_for(days)

    # Two slots same day — morning first.
    write_day_log(cdir, d0, [
        score_row(day=d0, slot=f"{d0}T08:45", symbol="AAA", model="grok",
                  direction="up", materiality=4, scored_hm=(8, 45)),
        score_row(day=d0, slot=f"{d0}T12:15", symbol="AAA", model="grok",
                  direction="up", materiality=4, scored_hm=(12, 15)),
    ])
    payload = S.run_score(
        asof=days[5],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=cal,
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
    )
    s = payload["by_model"]["grok"]["h_d5"]
    assert s["n_names"] == 1
    assert s["n_days"] == 1


def test_agreement_and_primary_not_yet(catalyst_env, monkeypatch):
    cdir, report = catalyst_env
    days = trading_days(12, "2026-01-05")
    d0, d1 = days[0], days[1]
    minute_px = {}
    daily_px = {s: {} for s in SYMS}
    for d in (d0, d1):
        for s in SYMS:
            minute_px.setdefault(s, {})[d] = 100.0
            minute_px[s][f"{d}#1550"] = 100.0
        daily_px["AAA"][days[days.index(d) + 5]] = 106.0  # +6%
        daily_px["BBB"][days[days.index(d) + 5]] = 100.0
        daily_px["CCC"][days[days.index(d) + 5]] = 100.0
        daily_px["SPY"][days[days.index(d) + 5]] = 100.0

    minute_store = make_minute_store([d0, d1], minute_px)
    daily_store = make_daily_store(daily_px)
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)
    cal = calendar_fn_for(days)

    for d in (d0, d1):
        slot = f"{d}T08:45"
        write_day_log(cdir, d, [
            score_row(day=d, slot=slot, symbol="AAA", model="grok",
                      direction="up", materiality=4),
            score_row(day=d, slot=slot, symbol="AAA", model="agy",
                      direction="up", materiality=5),
            # weak agree on BBB
            score_row(day=d, slot=slot, symbol="BBB", model="grok",
                      direction="up", materiality=4),
            score_row(day=d, slot=slot, symbol="BBB", model="agy",
                      direction="up", materiality=2),
            # disagree on CCC
            score_row(day=d, slot=slot, symbol="CCC", model="grok",
                      direction="up", materiality=4),
            score_row(day=d, slot=slot, symbol="CCC", model="agy",
                      direction="down", materiality=4),
        ])

    payload = S.run_score(
        asof=days[11],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=cal,
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
    )
    assert payload["by_agreement"]["agree_strong"]["h_d5"]["n_names"] == 2
    assert payload["by_agreement"]["agree_weak"]["h_d5"]["n_names"] == 2
    # disagree excluded from directional (side 0)
    assert payload["by_agreement"]["disagree"]["h_d5"]["n_names"] == 0
    assert payload["primary"]["verdict"]["verdict"] == "NOT YET"
    assert payload["header"]["n_days_logged"] == 2

    text = (cdir / "scorecard.md").read_text(encoding="utf-8")
    assert "Primary slice:" in text
    assert "agree_strong" in text
    assert "5-trading-day horizon" in text
    assert "NOT YET" in text


def test_verdict_go_and_no_go(catalyst_env, monkeypatch):
    """>=40 logged days; construct primary slice that PASSes then FAILs."""
    cdir, report = catalyst_env
    days = trading_days(55, "2026-01-05")
    score_days = days[:42]
    # For each score day, need D+5 available.
    cal = calendar_fn_for(days)

    minute_px: dict[str, dict[str, float]] = {s: {} for s in SYMS}
    daily_px: dict[str, dict[str, float]] = {s: {} for s in SYMS}
    for d in score_days:
        for s in SYMS:
            minute_px[s][d] = 100.0
            minute_px[s][f"{d}#1550"] = 100.0
        # AAA beats base by ~1% each day → net ~95 bp after 5bp cost
        d5 = days[days.index(d) + 5]
        daily_px["AAA"][d5] = 101.0
        daily_px["BBB"][d5] = 100.0
        daily_px["CCC"][d5] = 100.0
        daily_px["SPY"][d5] = 100.0

    minute_store = make_minute_store(score_days, minute_px)
    daily_store = make_daily_store(daily_px)
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)

    for d in score_days:
        slot = f"{d}T08:45"
        write_day_log(cdir, d, [
            score_row(day=d, slot=slot, symbol="AAA", model="grok",
                      direction="up", materiality=4),
            score_row(day=d, slot=slot, symbol="AAA", model="agy",
                      direction="up", materiality=4),
        ])

    asof = days[47]
    payload = S.run_score(
        asof=asof,
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=cal,
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
    )
    v = payload["primary"]["verdict"]
    assert payload["header"]["n_days_logged"] >= 40
    assert v["mean_net_bp"] == pytest.approx(95.0, abs=0.1)
    # Constant daily means → sd=0 → t = inf → passes t>=2
    assert v["t"] is not None and v["t"] >= 2
    assert v["ok_halves"] is True
    assert v["verdict"] == "GO"

    # NO-GO: rewrite cache/logs with tiny edge (<50 bp net)
    # Clear and rebuild with +0.3% excess → net 25 bp
    for p in cdir.glob("*.jsonl"):
        if p.name != "returns_cache.jsonl":
            # keep day logs; instead overwrite daily prices via new store
            pass
    (cdir / "returns_cache.jsonl").write_text("", encoding="utf-8")
    for d in score_days:
        d5 = days[days.index(d) + 5]
        daily_store["AAA"][d5] = 100.3  # +30 bp gross → 25 bp net

    payload2 = S.run_score(
        asof=asof,
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=cal,
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=lambda syms, start, end: {
            s: {dd: px for dd, px in (daily_store.get(s) or {}).items() if start <= dd <= end}
            for s in syms
        },
    )
    v2 = payload2["primary"]["verdict"]
    assert v2["verdict"] == "NO-GO"
    assert v2["mean_net_bp"] == pytest.approx(25.0, abs=0.1)


def test_ignores_runs_and_dryrun(catalyst_env, monkeypatch):
    cdir, report = catalyst_env
    days = trading_days(6, "2026-01-05")
    d0 = days[0]
    (cdir / "runs.jsonl").write_text(
        json.dumps({"kind": "run", "day": d0}) + "\n", encoding="utf-8",
    )
    dry = cdir / "dryrun"
    dry.mkdir()
    (dry / "x.json").write_text("[]", encoding="utf-8")
    # Non-day file
    (cdir / "notes.jsonl").write_text(
        json.dumps({"kind": "score", "status": "ok", "day": d0, "symbol": "ZZZ",
                    "model_family": "grok", "direction": "up", "materiality": 5,
                    "scored_ts": _ts(d0, 8, 45), "row_id": "nope"}) + "\n",
        encoding="utf-8",
    )
    minute_store = make_minute_store([d0], {s: {d0: 100.0} for s in SYMS})
    daily_store = make_daily_store({s: {days[5]: 100.0} for s in SYMS})
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)
    write_day_log(cdir, d0, [
        score_row(day=d0, slot=f"{d0}T08:45", symbol="AAA", model="grok",
                  direction="up", materiality=4),
    ])
    payload = S.run_score(
        asof=days[5],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=calendar_fn_for(days),
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
    )
    assert payload["header"]["ok_by_model"].get("grok") == 1
    assert "ZZZ" not in {
        o["symbol"] for o in (payload["by_model"]["grok"]["h_d5"].get("obs") or [])
    }


def test_down_call_signed_excess(catalyst_env, monkeypatch):
    cdir, report = catalyst_env
    days = trading_days(8, "2026-01-05")
    d0 = days[0]
    minute_px = {s: {d0: 100.0} for s in SYMS}
    # Name drops 2%, universe flat → for down call excess = -1*( -0.02 - 0 ) = +0.02
    daily_px = {
        "AAA": {days[5]: 98.0},
        "BBB": {days[5]: 100.0},
        "CCC": {days[5]: 100.0},
        "SPY": {days[5]: 100.0},
    }
    minute_store = make_minute_store([d0], minute_px)
    daily_store = make_daily_store(daily_px)
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)
    write_day_log(cdir, d0, [
        score_row(day=d0, slot=f"{d0}T08:45", symbol="AAA", model="grok",
                  direction="down", materiality=4),
    ])
    payload = S.run_score(
        asof=days[5],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=calendar_fn_for(days),
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
    )
    s = payload["by_model"]["grok"]["h_d5"]
    assert s["mean_net_bp"] == pytest.approx(195.0, abs=0.1)


def test_scorecard_contains_verbatim_primary_text(catalyst_env, monkeypatch):
    cdir, report = catalyst_env
    days = trading_days(6, "2026-01-05")
    d0 = days[0]
    minute_store = make_minute_store([d0], {s: {d0: 100.0} for s in SYMS})
    daily_store = make_daily_store({s: {days[5]: 100.0} for s in SYMS})
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)
    write_day_log(cdir, d0, [
        score_row(day=d0, slot=f"{d0}T08:45", symbol="AAA", model="grok",
                  direction="up", materiality=4),
    ])
    S.run_score(
        asof=days[5],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=calendar_fn_for(days),
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
    )
    text = (cdir / "scorecard.md").read_text(encoding="utf-8")
    # Spot-check distinctive verbatim lines
    assert "`agree_strong` & up" in text
    assert "more than 50 bp net" in text
    assert "date-clustered t >= 2" in text
    assert "must not be changed after data starts arriving" in text
    assert "overlapping 5- and 10-day" in text


def test_no_load_config_against_real_bot_config(catalyst_env, monkeypatch):
    """Scorer must not call load_config/save_config."""
    import config as cfg_mod

    def boom(*a, **k):
        raise AssertionError("must not touch load_config/save_config")

    monkeypatch.setattr(cfg_mod, "load_config", boom)
    if hasattr(cfg_mod, "save_config"):
        monkeypatch.setattr(cfg_mod, "save_config", boom)

    cdir, report = catalyst_env
    days = trading_days(6, "2026-01-05")
    d0 = days[0]
    minute_store = make_minute_store([d0], {s: {d0: 100.0} for s in SYMS})
    daily_store = make_daily_store({s: {days[5]: 100.0} for s in SYMS})
    fmany, f1, fdaily = _install_fetchers(monkeypatch, minute_store, daily_store)
    write_day_log(cdir, d0, [
        score_row(day=d0, slot=f"{d0}T08:45", symbol="AAA", model="grok",
                  direction="up", materiality=4),
    ])
    S.run_score(
        asof=days[5],
        report_dir=report,
        universe_path=FIX / "liquid_universe_tiny.json",
        calendar_fn=calendar_fn_for(days),
        fetch_many_fn=fmany,
        fetch_fn=f1,
        daily_fetch_fn=fdaily,
    )
