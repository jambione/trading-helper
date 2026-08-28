"""Fill-pinned replay — synthetic bars, no Alpaca."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import sim_fill_replay as sfr  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(hour: int, minute: int, day: str = "2026-08-14") -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=ET).timestamp()


def _bars(pxs: list[float], *, start_h: int = 10, start_m: int = 0,
          day: str = "2026-08-14") -> list:
    out = []
    for i, c in enumerate(pxs):
        m = start_m + i
        ts = _ts(start_h + m // 60, m % 60, day)
        o = pxs[i - 1] if i else c
        h = max(o, c) + 0.01
        low = min(o, c) - 0.01
        out.append((ts, o, h, low, c))
    return out


def _cfg(**over):
    """Trail-only config. Gates are irrelevant — fills are pinned."""
    cfg = {
        "desk_product": "scalp_legacy",
        "ai_watch_synth_stop_pct": 5.0,
        "ai_watch_synth_rr": 0.0,
        "ai_watch_synth_scale_out_pct": 50.0,
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_give_open_r": 0.10,
        "ai_local_trail_min_give_px": 0.0,
        "ai_local_trail_give_max_pct": 0.0,
        "ai_local_trail_arm_r": 0.0,
        "ai_local_trail_arm_pct": 0.0,
        "ai_local_trail_be_at_r": 0.0,
        "ai_local_trail_be_at_pct": 0.0,
        "ai_local_trail_give_spread_k": 0.0,
        "ai_local_trail_print_ring": 2,
        "ai_dead_trade_min": 0,
        "ai_dead_trade_mfe_r": 0.10,
        "ai_exit_left_overbought": False,
        "ai_watch_exhaustion_rules": False,
        "ai_trade_amount": 1000.0,
    }
    cfg.update(over)
    return cfg


def _fill(**over) -> dict:
    row = {
        "symbol": "AAA",
        "entry_price": 10.0,
        "stop_price": 9.5,
        "target_1": 10.3,
        "total_qty": 100,
        "exit_price": 9.95,
        "realized_r_multiple": -0.10,
        "realized_pl_usd": -5.0,
        "close_reason": "local_trail",
        "entry_time": _ts(10, 0),
        "exit_time": _ts(10, 3),
        "features": {},
    }
    row.update(over)
    return row


def test_parse_fill_skips_zero_entry_and_qty():
    ok, why = sfr.parse_fill(_fill())
    assert why is None and ok["symbol"] == "AAA" and ok["qty"] == 100
    assert sfr.parse_fill(_fill(entry_price=0))[1] == "no_entry"
    assert sfr.parse_fill(_fill(total_qty=0))[1] == "no_qty"
    assert sfr.parse_fill(_fill(symbol=""))[1] == "no_symbol"


def test_parse_fill_keeps_row_without_live_pnl():
    fill, why = sfr.parse_fill(_fill(
        realized_r_multiple=None, realized_pl_usd=None, exit_price=None))
    assert why is None
    assert fill["live_usd"] is None
    assert fill["entry"] == 10.0


def test_pinned_fill_trails_through_seeded_shelf():
    # $10, 5% R=$0.50, 0.10R give=$0.05, shelf=$9.95.
    pxs = [10.00, 10.00, 10.00, 10.00]
    bars = _bars(pxs)
    ts, o, h, _l, c = bars[-1]
    bars[-1] = (ts, o, h, 9.90, c)
    fill = sfr.parse_fill(_fill())[0]
    closed = sfr.walk_bars(fill, bars, _cfg())
    assert closed is not None
    assert closed["reason"] == "local_trail"
    assert closed["path"] == "bars"
    assert closed["exit"] < closed["entry"]
    assert closed["r"] < 0


def test_does_not_rearm_or_invent_a_second_trade():
    pxs = [10.00 + 0.04 * i for i in range(12)]
    bars = _bars(pxs)
    ts, o, h, _l, c = bars[-1]
    bars[-1] = (ts, o, h, c - 0.40, c - 0.30)
    fill = sfr.parse_fill(_fill())[0]
    closed = sfr.walk_bars(fill, bars, _cfg())
    assert closed is not None
    assert closed["why_arm"] == "pinned_fill"
    assert closed["reason"] == "local_trail"
    assert closed["r"] > 0


def test_missing_bars_are_not_scored_as_zero():
    fill = sfr.parse_fill(_fill())[0]
    scored = sfr.score_fills(
        [fill], _cfg(),
        bar_cache={},
        shadow_by_sym={},
        allow_shadow=False,
    )
    assert scored["n_walked"] == 0
    assert scored["n_scored"] == 0
    assert scored["skip"].get("no_path") == 1
    assert scored["delta_usd"] is None


def test_live_vs_sim_dollar_delta():
    pxs = [10.00, 10.00, 10.00, 10.00]
    bars = _bars(pxs)
    ts, o, h, _l, _c = bars[-1]
    bars[-1] = (ts, o, h, 9.90, 9.92)
    fill = sfr.parse_fill(_fill(realized_pl_usd=-5.0))[0]
    scored = sfr.score_fills(
        [fill], _cfg(),
        bar_cache={sfr.cache_key("AAA", "2026-08-14"): bars},
        shadow_by_sym={},
        allow_shadow=False,
    )
    assert scored["n_scored"] == 1
    row = scored["rows"][0]
    assert row["live_usd"] == -5.0
    assert row["sim_usd"] == pytest.approx(
        (row["sim_exit"] - 10.0) * 100, abs=0.02)
    assert row["delta_usd"] == pytest.approx(row["sim_usd"] - (-5.0), abs=0.02)


def test_overlay_give_changes_exit():
    pxs = [10.00, 10.00, 10.00, 10.00]
    bars = _bars(pxs)
    ts, o, h, _l, c = bars[-1]
    bars[-1] = (ts, o, h, 9.90, c)
    fill = sfr.parse_fill(_fill())[0]
    tight = sfr.walk_bars(fill, bars, _cfg())
    wide = sfr.walk_bars(fill, bars, _cfg(
        ai_local_trail_give_r=0.50,
        ai_local_trail_give_open_r=0.50,
    ))
    assert tight is not None and wide is not None
    assert tight["reason"] == "local_trail"
    # 0.50R give = $0.25, shelf $9.75 — the 9.90 low does not touch it.
    assert wide["reason"] != "local_trail" or wide["exit"] > tight["exit"]


def test_overlay_parser_maps_short_and_full_keys():
    ov = sfr.overlay_from_items(["give_r=0.20", "ai_dead_trade_min=22",
                                 "trail_enabled=false"])
    assert ov["give_r"] == 0.20
    assert ov["dead_trade_min"] == 22
    assert ov["trail_enabled"] is False
    cfg = sfr.apply_plan_overlay(_cfg(), ov)
    assert cfg["ai_local_trail_give_r"] == 0.20
    assert cfg["ai_dead_trade_min"] == 22
    assert cfg["ai_local_trail_enabled"] is False


def test_eod_flattens_open_long():
    bars = _bars([10.00, 10.02, 10.03], start_h=15, start_m=48)
    fill = sfr.parse_fill(_fill(entry_time=_ts(15, 48)))[0]
    closed = sfr.walk_bars(fill, bars, _cfg(ai_local_trail_enabled=False))
    assert closed is not None
    assert closed["reason"] == "eod_flatten"


def test_shadow_fallback_and_require_bars():
    fill = sfr.parse_fill(_fill())[0]
    ticks = [
        {"symbol": "AAA", "ts": _ts(10, 1), "price": 10.00},
        {"symbol": "AAA", "ts": _ts(10, 2), "price": 9.90},
    ]
    closed = sfr.walk_ticks(fill, ticks, _cfg())
    assert closed is not None
    assert closed["path"] == "shadow"
    assert closed["reason"] == "local_trail"

    scored = sfr.score_fills(
        [fill], _cfg(),
        bar_cache={},
        shadow_by_sym={"AAA": ticks},
        allow_shadow=False,
    )
    assert scored["n_walked"] == 0
    scored2 = sfr.score_fills(
        [fill], _cfg(),
        bar_cache={},
        shadow_by_sym={"AAA": ticks},
        allow_shadow=True,
    )
    assert scored2["n_walked"] == 1
    assert scored2["n_shadow"] == 1


def test_t1_blend_on_pinned_fill():
    pxs = [10.00, 10.20, 10.25, 10.22]
    bars = _bars(pxs)
    ts, o, h, _l, _c = bars[-1]
    bars[-1] = (ts, o, h, 10.00, 10.05)
    fill = sfr.parse_fill(_fill())[0]
    closed = sfr.walk_bars(
        fill, bars, _cfg(ai_watch_synth_rr=0.30, ai_watch_synth_scale_out_pct=50.0),
        risk_mode="current",
    )
    assert closed["t1_hit"] is True
    assert closed["r"] > 0.0


def test_run_does_not_write_bot_config(tmp_path, monkeypatch):
    monkeypatch.setattr(sfr, "resolve_report_dir", lambda: tmp_path)
    bars = _bars([10.0, 10.0, 10.0, 9.5])
    ts, o, h, _l, _c = bars[-1]
    bars[-1] = (ts, o, h, 9.90, 9.92)
    fill = sfr.parse_fill(_fill())[0]
    cfg_path = tmp_path / "bot_config.json"
    cfg_path.write_text("{\"untouched\": true}\n")
    payload = sfr.run(
        days=["2026-08-14"],
        cfg=_cfg(),
        fills=[fill],
        skip_load={},
        bar_cache={sfr.cache_key("AAA", "2026-08-14"): bars},
        shadow_by_sym={},
        overlay={},
        risk_mode="current",
        allow_shadow=False,
        write=True,
    )
    assert payload["ok"] is True
    assert (tmp_path / "fill_replay" / "2026-08-14.md").exists()
    assert json.loads(cfg_path.read_text()) == {"untouched": True}


def test_cli_json_with_bars_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(sfr.opt, "live_cfg", lambda **_k: _cfg())
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text(json.dumps(_fill()) + "\n")
    bars = _bars([10.0, 10.0, 10.0, 10.0])
    ts, o, h, _l, _c = bars[-1]
    bars[-1] = (ts, o, h, 9.90, 9.92)
    bars_path = tmp_path / "bars.json"
    bars_path.write_text(json.dumps({
        "AAA": [
            {"ts": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4]}
            for b in bars
        ],
    }))
    rc = sfr.main([
        "--outcomes", str(outcomes),
        "--bars-file", str(bars_path),
        "--day", "2026-08-14",
        "--require-bars",
        "--no-fetch",
        "--no-write",
        "--json",
    ])
    assert rc == 0


def test_verdict_underpowered_is_hypothesis():
    assert sfr.decide(
        n=10, n_sessions=8, delta_usd=8.0, holds=True, min_n=30,
    ) == sfr.VERDICT_HYPOTHESIS
    assert sfr.decide(
        n=40, n_sessions=2, delta_usd=8.0, holds=True, min_n=30,
    ) == sfr.VERDICT_HYPOTHESIS
    assert sfr.decide(
        n=40, n_sessions=8, delta_usd=8.0, holds=True, min_n=30,
    ) == sfr.VERDICT_CANDIDATE
    assert sfr.decide(
        n=40, n_sessions=8, delta_usd=8.0, holds=False, min_n=30,
    ) == sfr.VERDICT_DO_NOT
    assert sfr.decide(
        n=40, n_sessions=8, delta_usd=-1.0, holds=True, min_n=30,
    ) == sfr.VERDICT_DO_NOT


def test_fetch_401_stops_after_first(monkeypatch):
    fills = [
        sfr.parse_fill(_fill(symbol="AAA"))[0],
        sfr.parse_fill(_fill(symbol="BBB"))[0],
    ]
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("401 Client Error: Unauthorized")

    monkeypatch.setattr(sfr, "alpaca_keys", lambda _cfg: ("k", "s"))
    monkeypatch.setattr(sfr, "fetch_day_ohlc", boom)
    _cache, errors = sfr.fetch_needed(fills, {}, _cfg(), feed="sip")
    assert calls["n"] == 1
    assert set(errors.values()) == {"feed_unauthorized"}


def test_resolve_days_named_range_and_last_n():
    assert sfr.resolve_days(day=["2026-08-27"]) == ["2026-08-27"]
    assert sfr.resolve_days(day=["2026-08-27", "2026-08-20"]) == [
        "2026-08-20", "2026-08-27",
    ]
    rng = sfr.resolve_days(date_from="2026-08-20", date_to="2026-08-24")
    assert rng == ["2026-08-20", "2026-08-21", "2026-08-24"]
    with pytest.raises(ValueError, match="both required"):
        sfr.resolve_days(date_from="2026-08-20")
    with pytest.raises(ValueError, match="not mixed"):
        sfr.resolve_days(day=["2026-08-27"], days_n=10)


def test_load_fills_filters_days():
    rows = [
        _fill(symbol="AAA", entry_time=_ts(10, 0, "2026-08-14")),
        _fill(symbol="BBB", entry_time=_ts(10, 0, "2026-08-13")),
    ]
    fills, skip = sfr.load_fills(rows, days=["2026-08-14"])
    assert [f["symbol"] for f in fills] == ["AAA"]
    assert skip.get("other_day") == 1
