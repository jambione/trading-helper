# tests/test_ai_entry_watch.py
import json
import os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import DEFAULT_CONFIG, load_config

def test_watch_config_defaults_present():
    for key in (
        "ai_watch_enabled",
        "ai_watch_require_agreement",
        "ai_watch_single_source",
        "ai_watch_poll_sec",
        "ai_structure_ttl_sec",
        "ai_watch_expire_at_close",
        "ai_entry_zone_pad_pct",
        "ai_max_structure_calls_per_hour",
        "ai_persist_entry_decisions",
    ):
        assert key in DEFAULT_CONFIG
    cfg = load_config()
    assert cfg["ai_watch_enabled"] is True
    # Live bot_config may flip agreement; defaults document the knobs.
    assert "ai_watch_require_agreement" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_seed_momentum"] is True
    assert DEFAULT_CONFIG["ai_watch_seed_trending"] is True
    assert cfg["ai_watch_poll_sec"] == 20.0
    assert float(DEFAULT_CONFIG["ai_entry_zone_pad_pct"]) == 0.0


def test_upsert_requires_agreement_by_default(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": True, "ai_watch_single_source": False}
    rows = [
        {"symbol": "SMCI", "agreement": True, "trending_score": 8.2, "reason": "ai"},
        {"symbol": "HOOD", "agreement": False, "trending_score": 7.5, "reason": "x"},
    ]
    state = ew.upsert_from_rows(rows, cfg=cfg, now=1_000.0)
    assert "SMCI" in state
    assert "HOOD" not in state


def test_upsert_preserves_submitted_status(tmp_path, monkeypatch):
    """Rebuild/upsert must not clobber submitted (or filled) back to watching."""
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": True, "ai_watch_single_source": False}
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI",
            "status": "submitted",
            "structure": {"entry_low": 27.0, "entry_high": 28.0},
            "structure_ts": 900.0,
            "reason": "old",
        },
    })
    rows = [
        {"symbol": "SMCI", "agreement": True, "trending_score": 9.0, "reason": "refresh"},
    ]
    state = ew.upsert_from_rows(rows, cfg=cfg, now=1_000.0)
    assert state["SMCI"]["status"] == "submitted"
    assert state["SMCI"]["score"] == 9.0
    assert state["SMCI"]["reason"] == "refresh"
    # rebuild path too
    state2 = ew.rebuild_watch_from_book(rows, cfg=cfg, now=1_100.0)
    assert state2["SMCI"]["status"] == "submitted"


def test_upsert_preserves_filled_status(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    cfg = {"ai_watch_require_agreement": False}
    ew.save_watch({"AAA": {"symbol": "AAA", "status": "filled", "score": 1.0}})
    state = ew.upsert_from_rows(
        [{"symbol": "AAA", "agreement": True, "trending_score": 2.0}],
        cfg=cfg,
        now=50.0,
    )
    assert state["AAA"]["status"] == "filled"


def test_drop_missing_invalidates(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    state = {
        "SMCI": {"symbol": "SMCI", "status": "watching", "updated_ts": 1.0},
        "OLD": {"symbol": "OLD", "status": "watching", "updated_ts": 1.0},
    }
    out = ew.drop_missing(state, {"SMCI"}, now=2.0)
    assert out["SMCI"]["status"] == "watching"
    assert out["OLD"]["status"] == "invalidated"


def test_rebuild_watch_from_book(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(
        ew, "desk_candidate_rows",
        lambda cfg=None: [{"symbol": "SOFI", "source": "trending", "score": 12.0,
                           "reason": "heat", "agreement": True}],
    )
    cfg = {
        "ai_watch_require_agreement": True,
        "ai_watch_single_source": False,
        "ai_watch_seed_momentum": True,
        "ai_watch_seed_trending": True,
        # This test covers mirroring/preservation, not admission.
        "ai_watch_require_uptrend": False,
        "ai_watch_require_indicators": False,
        "ai_watch_admit_ticks": 1,
        "ai_watch_min_price": 0.0,
        "ai_watch_min_rvol": 0.0,
        "ai_watch_require_look_ext": False,
    }
    state = ew.rebuild_watch_from_book([], cfg=cfg, now=100.0)
    assert "SOFI" in state and state["SOFI"]["status"] == "watching"
    assert ew.load_watch()["SOFI"]["symbol"] == "SOFI"


def test_sync_watch_mirrors_source_panels_only(tmp_path, monkeypatch):
    """AI Watch is rebuilt from live panels — orphans are deleted, not kept."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    ew.save_watch({
        "KEEP": {
            "symbol": "KEEP", "status": "watching", "source": "momentum", "score": 7,
            "structure": {"entry_low": 1.0, "entry_high": 2.0},
            "structure_ts": 50.0,
        },
        "GONE": {
            "symbol": "GONE", "status": "watching", "source": "momentum", "score": 7,
        },
        "OLD_AI": {
            "symbol": "OLD_AI", "status": "watching", "source": "anthropic", "score": 6,
        },
        "FILLED": {
            "symbol": "FILLED", "status": "filled", "source": "momentum", "score": 7,
        },
    })
    monkeypatch.setattr(
        ew, "desk_candidate_rows",
        lambda cfg=None: [
            {"symbol": "KEEP", "source": "momentum", "agreement": True, "score": 7.5,
             "reason": "mom"},
            {"symbol": "STAY_TR", "source": "trending", "agreement": True, "score": 8,
             "reason": "heat"},
        ],
    )
    state = ew.sync_watch_from_source_panels(
        {"ai_watch_seed_momentum": True, "ai_watch_seed_trending": True,
         # Mirroring/preservation test — admission gates covered separately.
         "ai_watch_require_uptrend": False, "ai_watch_require_indicators": False,
         "ai_watch_admit_ticks": 1, "ai_watch_min_price": 0.0,
         "ai_watch_min_rvol": 0.0, "ai_watch_require_look_ext": False},
        now=100.0,
    )
    # Orphans go regardless of source: OLD_AI is research-sourced and is dropped
    # because no panel still lists it, not because research is excluded.
    assert "GONE" not in state and "OLD_AI" not in state
    assert state["KEEP"]["status"] == "watching"
    assert state["KEEP"]["structure"]["entry_low"] == 1.0  # structure preserved
    assert state["STAY_TR"]["source"] == "trending"
    assert state["FILLED"]["status"] == "filled"  # in-flight kept
    book = ew.book_table_rows(state=state)
    syms = {r["symbol"] for r in book}
    assert syms == {"KEEP", "STAY_TR", "FILLED"}


def test_desk_candidates_restrictive_filters(tmp_path, monkeypatch):
    """Trending: score>min + chg up + LOOK=EXT (never WASH); momentum flags."""
    import ai_entry_watch as ew

    (tmp_path / "trending_stocks.json").write_text(json.dumps({
        "rows": [
            {"symbol": "HOT", "trending_score": 12.5, "pct_change": 4.0,
             "look_reason": "EXT", "rvol": 3.5, "price": 18.0, "is_equity": True},
            {"symbol": "LOW", "trending_score": 8.0, "pct_change": 2.0,
             "look_reason": "EXT", "rvol": 2.5, "price": 10.0, "is_equity": True},
            {"symbol": "WASHY", "trending_score": 20.0, "pct_change": -5.0,
             "look_reason": "WASH", "rvol": 4.0, "price": 9.0, "is_equity": True},
            {"symbol": "NOLOOK", "trending_score": 15.0, "pct_change": 6.0,
             "look_reason": "", "rvol": 3.0, "price": 11.0, "is_equity": True},
            {"symbol": "DOWN", "trending_score": 15.0, "pct_change": -2.0,
             "look_reason": "EXT", "rvol": 3.0, "price": 12.0, "is_equity": True},
            {"symbol": "THIN", "trending_score": 12.0, "pct_change": 3.0,
             "look_reason": "EXT", "rvol": 1.2, "price": 8.0, "is_equity": True},
            {"symbol": "BTC", "trending_score": 99.0, "pct_change": 10.0,
             "look_reason": "EXT", "price": 1.0, "is_crypto": True},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    monkeypatch.setattr(
        ew, "_momentum_flagged_from_dashboard",
        lambda max_price=None: [
            (10.0, {
                "symbol": "FLAG",
                "score": 10.0,
                "trending_score": 10.0,
                "reason": "momentum FIRST",
                "agreement": True,
                "source": "momentum",
            }),
        ],
    )
    monkeypatch.setattr(
        ew, "_big_mover_from_dashboard",
        lambda max_price=None, min_pct=50.0: [
            (161.0, {
                "symbol": "AMIX",
                "score": 161.0,
                "trending_score": 161.0,
                "reason": "momentum chg +161%",
                "agreement": True,
                "source": "momentum",
            }),
        ],
    )
    rows = ew.desk_candidate_rows({
        "ai_watch_seed_momentum": True,
        "ai_watch_seed_trending": True,
        "ai_watch_seed_research": False,
        "ai_watch_seed_bb_live": False,
        "ai_watch_seed_momentum_n": 12,
        "ai_watch_seed_trending_n": 20,
        "ai_watch_trending_min_score": 5.0,
        "ai_watch_min_pct_change": 50.0,
        "ai_watch_min_rvol": 2.0,
        "ai_max_price": 100.0,
    })
    by = {r["symbol"]: r for r in rows}
    assert by["HOT"]["source"] == "trending"
    assert by["HOT"]["look_reason"] == "EXT"
    assert by["LOW"]["source"] == "trending"  # score 8 > 5, up, EXT
    assert by["FLAG"]["source"] == "momentum"
    assert by["AMIX"]["source"] == "momentum"
    assert "WASHY" not in by   # LOOK=WASH never seeds
    assert "NOLOOK" not in by  # no EXT flag
    assert "DOWN" not in by    # day change not up
    assert "THIN" not in by    # known rvol < 2x
    assert "BTC" not in by


def _seed_cfg(**over):
    """desk_candidate_rows cfg with every seed off — switch on what you test."""
    cfg = {
        "ai_watch_seed_momentum": False,
        "ai_watch_seed_momentum_open": False,
        "ai_watch_seed_trending": False,
        "ai_watch_seed_research": False,
        "ai_watch_seed_bb_live": False,
        "ai_max_price": 100.0,
    }
    cfg.update(over)
    return cfg


def test_research_seed_puts_board_names_on_the_shortlist(tmp_path, monkeypatch):
    """AI Research is a source panel, enriched with the desk row's numbers."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "ROOT", tmp_path)
    (tmp_path / "grok_suggestions.json").write_text(json.dumps({
        "source": "xai",
        "rows": [{"symbol": "THESIS", "score": 8.0, "reason": "catalyst next week"},
                 {"symbol": "NOROW", "score": 7.0, "reason": "no desk row yet"}],
    }), encoding="utf-8")
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "THESIS", "price": 12.0, "pct_change": 6.5,
         "rvol": 2.2, "day_vol": 1_000_000},
    ])

    rows = ew.desk_candidate_rows(_seed_cfg(ai_watch_seed_research=True))
    by = {r["symbol"]: r for r in rows}

    assert by["THESIS"]["source"] == "xai"
    assert by["THESIS"]["reason"] == "catalyst next week"
    # Numbers come off the desk row, not off the board.
    assert by["THESIS"]["price"] == 12.0
    assert by["THESIS"]["pct_change"] == 6.5
    assert by["THESIS"]["dollar_volume"] == 12_000_000.0
    # A board name the desk has no row for still ships (so the engine starts
    # quoting it); passes_inclusion rejects it as no_price until it does.
    assert by["NOROW"]["price"] is None


def test_research_seed_respects_cap_and_flag(tmp_path, monkeypatch):
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "ROOT", tmp_path)
    (tmp_path / "grok_suggestions.json").write_text(json.dumps({
        "source": "xai",
        "rows": [{"symbol": f"S{i}", "score": 9.0} for i in range(5)],
    }), encoding="utf-8")
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [])

    assert ew.desk_candidate_rows(_seed_cfg()) == []          # flag off
    rows = ew.desk_candidate_rows(
        _seed_cfg(ai_watch_seed_research=True, ai_watch_seed_research_n=2))
    assert len(rows) == 2


def test_bb_live_seed_admits_fresh_calls_only(monkeypatch):
    """A bro call is a candidate while it is fresh, ranked newest first."""
    import ai_entry_watch as ew

    now = 10_000.0
    monkeypatch.setattr(ew, "dashboard_state", lambda force=False: {
        "bb_live": {"history": [
            {"ticker": "FRESH", "at": now - 60,   "said": "9:31 AM", "text": "on it"},
            {"ticker": "AGING", "at": now - 800,  "said": "9:20 AM", "text": "watch"},
            {"ticker": "STALE", "at": now - 5000, "said": "8:10 AM", "text": "old"},
        ]},
    })
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "FRESH", "price": 4.0, "pct_change": 9.0, "day_vol": 500_000},
        {"ticker": "AGING", "price": 6.0, "pct_change": 2.0},
        {"ticker": "STALE", "price": 3.0, "pct_change": 1.0},
    ])

    scored = ew._bb_live_from_dashboard(100.0, 900.0, now=now)
    syms = [r["symbol"] for _, r in scored]
    assert syms == ["FRESH", "AGING"]          # STALE aged out, newest ranks first
    row = dict(scored[0][1])
    assert row["source"] == "bb_live"
    assert row["criteria"] == ["bro_call"]
    assert row["price"] == 4.0 and row["pct_change"] == 9.0
    assert row["dollar_volume"] == 2_000_000.0


def test_bb_call_actionable_classification():
    """The call stream is commentary, not a buy list — exits and passes are out.

    Every string here is a real line from ai_reports/bb_live.jsonl (2026-08-10).
    """
    import ai_entry_watch as ew

    for bullish in [
        "AUUD retest hod with vol", "JWEL low vol curl", "HUDI pop",
        "AUUD currently has the vol", "JWEL + AUUD on watch", "AUUD test res",
        "PPBT vol", "STKH test res 5-5.50", "AUUD more",
    ]:
        assert ew._bb_call_is_actionable(bullish), bullish

    for exit_or_pass in [
        "AUUD sold lotto - loss",                  # exit
        "JWEL sold lotto flat",                    # exit
        "HUDI all out lotto - flat (red side)",    # exit
        "AUUD was a fun one to get us started",    # post-mortem, reads as praise
        "JWEL + AUUD were good ones to get us",    # post-mortem
        "ABCL lg float - not for me",              # explicit pass
        "SLN larger float",                        # explicit pass
        "KWM recent res - will adjust OR avoid",   # standard disclaimer
    ]:
        assert not ew._bb_call_is_actionable(exit_or_pass), exit_or_pass

    # Unrecognised lines stay actionable: the shortlist is not an admission.
    assert ew._bb_call_is_actionable("SOME brand new phrasing")
    assert ew._bb_call_is_actionable("")


def test_bb_live_seed_drops_exit_calls(monkeypatch):
    """Recency ranks the newest call, which is usually how the trade ENDED.

    Unfiltered this selects for exits — the live shortlist on 2026-08-10 was
    two "sold" calls. A settled symbol must also stay settled: an older bullish
    line for the same name must not resurrect it.
    """
    import ai_entry_watch as ew

    now = 10_000.0
    monkeypatch.setattr(ew, "dashboard_state", lambda force=False: {
        "bb_live": {"history": [
            {"ticker": "SOLD",  "at": now - 30,  "text": "SOLD sold lotto flat"},
            {"ticker": "SOLD",  "at": now - 300, "text": "SOLD retest hod"},
            {"ticker": "ONIT",  "at": now - 60,  "text": "ONIT test res"},
            {"ticker": "PASS",  "at": now - 90,  "text": "PASS lg float - not for me"},
        ]},
    })
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": s, "price": 4.0, "pct_change": 5.0} for s in ("SOLD", "ONIT", "PASS")
    ])

    syms = [r["symbol"] for _, r in ew._bb_live_from_dashboard(100.0, 900.0, now=now)]
    assert syms == ["ONIT"]


def test_bb_live_seed_measures_from_said_not_capture(monkeypatch):
    """Freshness uses `at` (when it was said), never `unix` (when OCR read it).

    The OCR source re-posts a whole screen on restart, so capture time would
    make an hour of stale call-outs all look current.
    """
    import ai_entry_watch as ew

    now = 10_000.0
    monkeypatch.setattr(ew, "dashboard_state", lambda force=False: {
        "bb_live": {"history": [
            {"ticker": "OLDCALL", "at": now - 4000, "unix": now - 5},
        ]},
    })
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [
        {"ticker": "OLDCALL", "price": 5.0, "pct_change": 3.0},
    ])
    assert ew._bb_live_from_dashboard(100.0, 900.0, now=now) == []


def test_bb_live_seed_yields_to_stronger_panels(tmp_path, monkeypatch):
    """A call-out only contributes symbols no other panel already named."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "ROOT", tmp_path)
    monkeypatch.setattr(ew, "_momentum_flagged_from_dashboard", lambda max_price=None: [
        (10.0, {"symbol": "BOTH", "score": 10.0, "trending_score": 10.0,
                "reason": "momentum FIRST", "agreement": True, "source": "momentum",
                "price": 7.0, "pct_change": 40.0, "rvol": 4.0}),
    ])
    monkeypatch.setattr(ew, "_big_mover_from_dashboard",
                        lambda max_price=None, min_pct=50.0: [])
    monkeypatch.setattr(ew, "_bb_live_from_dashboard", lambda mp, fresh, now=None: [
        (900.0, {"symbol": "BOTH", "source": "bb_live", "score": 900.0,
                 "reason": "bro call", "agreement": True, "criteria": ["bro_call"]}),
        (800.0, {"symbol": "ONLYBRO", "source": "bb_live", "score": 800.0,
                 "reason": "bro call", "agreement": True, "criteria": ["bro_call"]}),
    ])

    rows = ew.desk_candidate_rows(
        _seed_cfg(ai_watch_seed_momentum=True, ai_watch_seed_bb_live=True))
    by = {r["symbol"]: r for r in rows}
    # Momentum measured BOTH; the call must not overwrite a row with evidence.
    assert by["BOTH"]["source"] == "momentum"
    assert by["BOTH"]["rvol"] == 4.0
    assert by["ONLYBRO"]["source"] == "bb_live"


def test_sync_keeps_research_and_bb_live_sources(tmp_path, monkeypatch):
    """The sync source filter no longer drops research / bro-call rows."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(ew, "ROOT", tmp_path)
    ew.save_watch({})
    monkeypatch.setattr(ew, "desk_candidate_rows", lambda cfg=None: [
        {"symbol": "MOM", "source": "momentum", "agreement": True, "score": 7,
         "reason": "mom"},
        {"symbol": "RSCH", "source": "xai", "agreement": True, "score": 8,
         "reason": "thesis"},
        {"symbol": "BRO", "source": "bb_live", "agreement": True, "score": 900,
         "reason": "bro call"},
        {"symbol": "JUNK", "source": "somewhere_else", "agreement": True, "score": 9,
         "reason": "no panel"},
    ])
    monkeypatch.setattr(ew, "push_candidates_to_engine", lambda syms: {"pushed": 0})

    state = ew.sync_watch_from_source_panels(
        {"ai_watch_require_uptrend": False, "ai_watch_require_indicators": False,
         "ai_watch_admit_ticks": 1, "ai_watch_min_price": 0.0,
         "ai_watch_min_rvol": 0.0, "ai_watch_require_look_ext": False},
        now=100.0,
    )
    assert set(state) == {"MOM", "RSCH", "BRO"}
    assert state["RSCH"]["source"] == "xai"
    assert state["BRO"]["source"] == "bb_live"
    assert "JUNK" not in state   # unknown source has no panel behind it


def test_upsert_desk_does_not_steal_research_source(tmp_path, monkeypatch):
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI",
            "status": "watching",
            "source": "xai",
            "score": 8.5,
            "reason": "research thesis",
        },
    })
    state = ew.upsert_from_rows(
        [{"symbol": "SMCI", "source": "momentum", "score": 7.0, "reason": "mom",
          "agreement": True}],
        cfg={"ai_watch_require_agreement": False},
        now=1.0,
    )
    assert state["SMCI"]["source"] == "xai"
    assert state["SMCI"]["reason"] == "research thesis"


def test_book_table_rows_merges_position_and_sources(tmp_path, monkeypatch):
    """Open positions show P&L; momentum/trending sources preserved on watches."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(
        ew, "live_panel_universe",
        lambda cfg=None: {"SMCI", "ACHR", "SOFI"},
    )
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI",
            "status": "watching",
            "source": "research",
            "score": 8.2,
            "last_ask": 29.0,
            "structure": {"entry_low": 27.0, "entry_high": 28.0, "wait_kind": "wait_for_zone"},
        },
        "ACHR": {
            "symbol": "ACHR",
            "status": "watching",
            "source": "momentum",
            "score": 7.1,
            "reason": "momentum HOT",
            "last_ask": 8.5,
            "structure": None,
        },
        "SOFI": {
            "symbol": "SOFI",
            "status": "watching",
            "source": "trending",
            "score": 7.8,
            "last_ask": 18.0,
            "structure": None,
        },
        "DEAD": {
            "symbol": "DEAD",
            "status": "expired",
            "source": "momentum",
        },
    })
    positions = {
        "SMCI": {
            "qty": 35.0,
            "avg_entry": 28.0,
            "current": 29.5,
            "pl": 52.5,
            "plpc": 5.36,
            "mkt_val": 1032.5,
        },
    }
    rows = ew.book_table_rows(positions=positions)
    by = {r["symbol"]: r for r in rows}
    assert "DEAD" not in by
    assert by["SMCI"]["phase"] == "open"
    assert by["SMCI"]["is_position"] is True
    assert by["SMCI"]["pl"] == 52.5
    assert by["SMCI"]["qty"] == 35.0
    assert by["ACHR"]["source"] == "momentum"
    assert by["ACHR"]["phase"] == "watching"
    assert by["SOFI"]["source"] == "trending"
    # Open first
    assert rows[0]["symbol"] == "SMCI"


def test_book_table_rows_stamps_live_local_stop(tmp_path, monkeypatch):
    """TRAIL uses the software shelf (last − give×R), not the frozen plan stop."""
    import ai_entry_watch as ew
    import ai_positions as cp

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(ew, "live_panel_universe", lambda cfg=None: {"SMCI"})
    monkeypatch.setattr(ew, "live_print", lambda _sym: None)
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI",
            "status": "filled",
            "source": "momentum",
            "last_ask": 9.0,
            "structure": {
                "entry_low": 8.5, "entry_high": 8.7, "stop_price": 8.38,
            },
        },
    })
    monkeypatch.setattr(cp, "_load_state", lambda: {
        "SMCI": {
            "entry_price": 8.64,
            "entry_stop_price": 8.38,
            "stop_price": 8.38,
            "risk_per_share": 0.26,
            "last_seen_price": 8.80,
            "local_stop_price": 8.38,
        },
    })
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.20,
    })
    rows = ew.book_table_rows(positions={
        "SMCI": {"qty": 10, "avg_entry": 8.64, "current": 8.80, "pl": 1.6},
    })
    by = {r["symbol"]: r for r in rows}
    assert by["SMCI"]["local_stop"] == pytest.approx(8.748)
    assert by["SMCI"]["risk_per_share"] == pytest.approx(0.26)
    assert by["SMCI"]["entry_stop_price"] == pytest.approx(8.38)
    assert by["SMCI"]["trail_give_r"] == pytest.approx(0.20)


def test_book_rstop_not_previewed_on_watches(tmp_path, monkeypatch):
    """RStop must not trail last on a watch — that shelf sits above the zone."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(ew, "live_panel_universe", lambda cfg=None: {"FGI"})
    monkeypatch.setattr(ew, "live_print", lambda _sym: None)
    monkeypatch.setattr(ew, "stream_quote", lambda _sym: (15.32, 0.1))
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_local_trail_give_r": 0.20,
        "ai_local_trail_give_px": 0.0,
    })
    ew.save_watch({
        "FGI": {
            "symbol": "FGI",
            "status": "watching",
            "source": "momentum",
            "last_ask": 15.32,
            "structure": {
                "entry_low": 14.42, "entry_high": 15.40, "stop_price": 14.15,
            },
        },
    })
    rows = ew.book_table_rows()
    by = {r["symbol"]: r for r in rows}
    assert by["FGI"]["local_stop"] is None


def test_book_table_rows_uses_tape_when_last_seen_missing(tmp_path, monkeypatch):
    """RStop must not sit on the entry floor just because last_seen was blank."""
    import ai_entry_watch as ew
    import ai_positions as cp

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(ew, "live_panel_universe", lambda cfg=None: {"SMCI"})
    monkeypatch.setattr(ew, "live_print", lambda _sym: (8.80, 0.1))
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI", "status": "filled", "source": "momentum",
            "last_ask": 8.80,
            "structure": {"entry_low": 8.5, "entry_high": 8.7, "stop_price": 8.38},
        },
    })
    monkeypatch.setattr(cp, "_load_state", lambda: {
        "SMCI": {
            "entry_price": 8.64, "entry_stop_price": 8.38, "stop_price": 8.38,
            "risk_per_share": 0.26, "local_stop_price": 8.38,
        },
    })
    monkeypatch.setattr(ew, "_push_cfg", lambda: {
        "ai_local_trail_enabled": True, "ai_local_trail_give_r": 0.20,
    })
    rows = ew.book_table_rows(positions={
        "SMCI": {"qty": 10, "avg_entry": 8.64, "current": 8.80, "pl": 1.6},
    })
    by = {r["symbol"]: r for r in rows}
    assert by["SMCI"]["local_stop"] == pytest.approx(8.748)


def test_rebuild_seeds_momentum_into_active(tmp_path, monkeypatch):
    """Flagged momentum lands on the book; research does not."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(
        ew, "desk_candidate_rows",
        lambda cfg=None: [{
            "symbol": "ACHR",
            "score": 9.5,
            "trending_score": 9.5,
            "reason": "momentum FIRST",
            "agreement": True,
            "source": "momentum",
        }],
    )
    cfg = {
        "ai_watch_require_agreement": False,
        "ai_watch_seed_momentum": True,
        "ai_watch_seed_trending": False,
        # Seeding/exclusion test — admission gates covered separately.
        "ai_watch_require_uptrend": False,
        "ai_watch_require_indicators": False,
        "ai_watch_admit_ticks": 1,
        "ai_watch_min_price": 0.0,
        "ai_watch_min_rvol": 0.0,
    }
    state = ew.rebuild_watch_from_book([], cfg=cfg, now=200.0)
    assert "SOFI" not in state  # research excluded
    assert "ACHR" in state
    assert state["ACHR"]["source"] == "momentum"
    assert state["ACHR"]["status"] == "watching"


def test_expire_open_watches(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew.save_watch({"SMCI": {"symbol": "SMCI", "status": "watching"}})
    out = ew.expire_open_watches(now=1.0)
    assert out["SMCI"]["status"] == "expired"


def test_expire_stale_watches_for_new_day(tmp_path, monkeypatch):
    """Open watches stamped on a prior ET day expire; same-day stay open."""
    import ai_entry_watch as ew
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    et = ZoneInfo("America/New_York")
    # 2026-08-04 10:00 ET
    now = datetime(2026, 8, 4, 10, 0, tzinfo=et).timestamp()
    # prior day ~ 2026-08-03 15:00 ET
    prev = datetime(2026, 8, 3, 15, 0, tzinfo=et).timestamp()
    same = datetime(2026, 8, 4, 9, 30, tzinfo=et).timestamp()
    ew.save_watch({
        "OLD": {
            "symbol": "OLD",
            "status": "watching",
            "updated_ts": prev,
            "structure_ts": prev,
        },
        "NEW": {
            "symbol": "NEW",
            "status": "armed",
            "updated_ts": same,
            "structure_ts": same,
        },
        "DONE": {
            "symbol": "DONE",
            "status": "submitted",
            "updated_ts": prev,
        },
    })
    out = ew.expire_stale_watches_for_new_day(now)
    assert out["OLD"]["status"] == "expired"
    assert out["NEW"]["status"] == "armed"
    assert out["DONE"]["status"] == "submitted"


def test_public_snapshot_shape(tmp_path, monkeypatch):
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    state = {
        "ZZZ": {
            "symbol": "ZZZ",
            "status": "watching",
            "agreement": True,
            "score": 7.5,
            "last_ask": 12.3,
            "structure": {
                "wait_kind": "wait_for_zone",
                "entry_low": 11.0,
                "entry_high": 13.0,
            },
        },
        "AAA": {
            "symbol": "AAA",
            "status": "armed",
            "agreement": False,
            "score": 9.1,
            "last_ask": None,
            "structure": None,
        },
    }
    ew.save_watch(state)
    snap = ew.public_snapshot()
    assert isinstance(snap, list)
    # Ready first (armed / in-zone), then higher score — AAA (armed, 9.1) then ZZZ.
    assert [r["symbol"] for r in snap] == ["AAA", "ZZZ"]
    keys = {
        "symbol", "status", "wait_kind", "entry_low", "entry_high",
        "stop_price",
        "last_ask", "score", "rvol", "exhaustion", "exhaustion_state",
        "pctr", "pctr_raw", "pctr_src", "exh_bars", "exh_window_min",
        "exh_hh", "exh_ll",
        "agreement", "reason", "source", "ready", "in_zone",
        # Which geometry drew the band — double_bottom vs the offset fallback.
        "zone_kind",
        "block_code", "blocker", "block_reason",
    }
    for row in snap:
        assert set(row.keys()) == keys
        assert "blocker" in row
    zzz = snap[1]
    assert zzz["status"] == "watching"
    assert zzz["wait_kind"] == "wait_for_zone"
    assert zzz["entry_low"] == 11.0
    assert zzz["entry_high"] == 13.0
    assert zzz["last_ask"] == 12.3
    assert zzz["score"] == 7.5
    assert zzz["agreement"] is True
    assert zzz["ready"] is True  # ask inside zone
    assert zzz["in_zone"] is True
    aaa = snap[0]
    assert aaa["status"] == "armed"
    assert aaa["ready"] is True
    assert aaa["wait_kind"] is None
    assert aaa["entry_low"] is None
    assert aaa["entry_high"] is None
    assert aaa["last_ask"] is None
    # Also accepts in-memory state without load
    assert ew.public_snapshot(state)[0]["symbol"] == "AAA"


def test_should_expire_watches_on_close_edge():
    """Pre-market closed must not expire or latch; only open→closed does."""
    import ai_entry_watch as ew

    day = "2026-08-03"
    # Pre-market: closed, never saw open → no expire, no latch.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day, seen_open=False, expired_day="")
    assert do is False and seen is False and exp == ""

    # Still pre-market closed — still no latch.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day, seen_open=seen, expired_day=exp)
    assert do is False and seen is False and exp == ""

    # RTH open → mark seen_open.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=True, day_key=day, seen_open=seen, expired_day=exp)
    assert do is False and seen is True and exp == ""

    # Stay open.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=True, day_key=day, seen_open=seen, expired_day=exp)
    assert do is False and seen is True

    # Close after open → expire once, latch day.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day, seen_open=seen, expired_day=exp)
    assert do is True and seen is False and exp == day

    # Still closed same day → do not re-expire.
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day, seen_open=seen, expired_day=exp)
    assert do is False and exp == day

    # Next day pre-market: no expire until open→closed again.
    day2 = "2026-08-04"
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day2, seen_open=False, expired_day=exp)
    assert do is False
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=True, day_key=day2, seen_open=seen, expired_day=exp)
    assert do is False and seen is True
    do, seen, exp = ew.should_expire_watches_on_close(
        market_open=False, day_key=day2, seen_open=seen, expired_day=exp)
    assert do is True and exp == day2


def test_ask_in_zone_with_pad():
    import ai_entry_watch as ew
    assert ew.ask_in_zone(28.0, 27.0, 28.5, 0.15) is True
    assert ew.ask_in_zone(30.0, 27.0, 28.5, 0.15) is False


def test_spread_ok_mid_pct():
    import ai_entry_watch as ew
    # (28.0 - 27.95) / mid * 100 ≈ 0.18%
    assert ew.spread_ok(27.95, 28.0, 1.0) is True
    assert ew.spread_ok(27.0, 28.0, 0.5) is False
    # Missing bid must not block (IEX often one-sided).
    assert ew.spread_ok(None, 28.0, 1.0) is True
    assert ew.spread_ok(None, 28.0, 0.0) is True  # enforcement off


def test_should_arm_wait_for_zone(monkeypatch):
    import ai_entry_watch as ew
    rec = {
        "status": "watching",
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": 27.0, "entry_high": 28.5,
            "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        },
    }
    cfg = {"ai_max_spread_pct": 1.0, "ai_entry_zone_pad_pct": 0.15,
           "ai_min_reward_risk": 3.0,
           # zone-membership test; arm-time indicator check has its own tests
           "ai_watch_arm_require_indicators": False,
           "ai_watch_exhaustion_rules": False}
    ok, why = ew.should_arm_buy(rec, ask=28.0, bid=27.95, cfg=cfg)
    assert ok and why.startswith("zone")
    ok2, why2 = ew.should_arm_buy(rec, ask=32.0, bid=31.9, cfg=cfg)
    assert not ok2
    assert why2 == "above_zone"


def test_should_arm_rejects_wait_setup_and_hard_no():
    import ai_entry_watch as ew
    cfg = {"ai_max_spread_pct": 1.0, "ai_entry_zone_pad_pct": 0.15,
           "ai_min_reward_risk": 3.0,
           # zone-membership test; arm-time indicator check has its own tests
           "ai_watch_arm_require_indicators": False,
           "ai_watch_exhaustion_rules": False}
    base = {
        "entry_low": 27.0, "entry_high": 28.5,
        "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
    }
    rec_setup = {
        "status": "watching",
        "structure": {"decision": "WAIT", "wait_kind": "wait_setup", **base},
    }
    rec_hard = {
        "status": "watching",
        "structure": {"decision": "WAIT", "wait_kind": "hard_no", **base},
    }
    ok, why = ew.should_arm_buy(rec_setup, ask=28.0, bid=27.95, cfg=cfg)
    assert not ok and why == "wait_setup"
    ok2, why2 = ew.should_arm_buy(rec_hard, ask=28.0, bid=27.95, cfg=cfg)
    assert not ok2 and why2 == "hard_no"


def test_should_arm_buy_decision_in_zone():
    import ai_entry_watch as ew
    rec = {
        "status": "armed",
        "structure": {
            "decision": "BUY",
            "entry_low": 27.0, "entry_high": 28.5,
            "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        },
    }
    cfg = {"ai_max_spread_pct": 1.0, "ai_entry_zone_pad_pct": 0.15,
           "ai_min_reward_risk": 3.0,
           # zone-membership test; arm-time indicator check has its own tests
           "ai_watch_arm_require_indicators": False,
           "ai_watch_exhaustion_rules": False}
    ok, why = ew.should_arm_buy(rec, ask=28.0, bid=27.95, cfg=cfg)
    assert ok and why.startswith("zone")


def test_should_arm_in_zone_despite_wide_spread():
    """READY = in zone; a wide IEX book must not block the fill."""
    import ai_entry_watch as ew
    rec = {
        "status": "watching",
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": 27.0, "entry_high": 28.5,
            "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        },
    }
    cfg = {"ai_max_spread_pct": 0.1, "ai_entry_zone_pad_pct": 0.15,
           "ai_min_reward_risk": 3.0,
           # spread-vs-zone test; arm-time indicator check has its own tests
           "ai_watch_arm_require_indicators": False,
           "ai_watch_exhaustion_rules": False}
    # ~1.8% spread but ask is inside the entry zone → arm.
    ok, why = ew.should_arm_buy(rec, ask=28.0, bid=27.5, cfg=cfg)
    assert ok and why.startswith("zone")
    # Outside the zone is reported as above/below (not spread).
    ok2, why2 = ew.should_arm_buy(rec, ask=32.0, bid=27.5, cfg=cfg)
    assert not ok2 and why2 == "above_zone"


def _poll_cfg(**overrides):
    cfg = {
        "ai_watch_enabled": True,
        "ai_max_spread_pct": 1.0,
        "ai_entry_zone_pad_pct": 0.15,
        "ai_min_reward_risk": 3.0,
        "ai_structure_ttl_sec": 999999,
        "ai_max_structure_calls_per_hour": 12,
        "ai_max_price": 100.0,
        "ai_risk_pct": 1.0,
        # Isolate poll unit tests from live desk heat files.
        "ai_watch_seed_momentum": False,
        "ai_watch_seed_trending": False,
        # These tests cover zone / gate / ordering mechanics. The arm-time
        # indicator check is orthogonal and has its own tests below, so leave
        # it off here rather than threading fake signal state through each.
        "ai_watch_arm_require_indicators": False,
        # Exhaustion needs bar/OHLC data; place/gate tests isolate zone logic.
        "ai_watch_exhaustion_rules": False,
        # Live dashboard tape (SMCI ~$31 today) must not prefilter unit tests
        # whose fixture zones sit around $28.
        "ai_watch_stream_enabled": False,
    }
    cfg.update(overrides)
    return cfg


def _patch_trading_ready(monkeypatch, gt, *, ask=28.0, bid=27.95):
    import ai_entry_watch as ew
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # poll_once uses ET clock / SOD gates — pin a mid-session RTH weekday.
    fixed_et = datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(ew, "_et_now", lambda now=None: fixed_et)
    monkeypatch.setattr(ew, "sod_liquidate_done", lambda cfg, now=None: True)
    monkeypatch.setattr(ew, "expire_stale_watches_for_new_day", lambda now: None)
    # Do not hit the live dashboard / Finnhub tape / engine map during unit tests.
    monkeypatch.setattr(ew, "stream_quote", lambda s: None)
    monkeypatch.setattr(ew, "stream_says_far_from_zone", lambda rec, cfg: (False, None))
    monkeypatch.setattr(ew, "ensure_live_exhaustion", lambda *a, **k: False)
    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: {})
    monkeypatch.setattr(ew, "_dashboard_tickers", lambda: [])
    monkeypatch.setattr(gt, "market_is_open", lambda: True)
    monkeypatch.setattr(gt, "is_ready", lambda: True)
    monkeypatch.setattr(gt, "_latest_ask", lambda s: ask)
    monkeypatch.setattr(gt, "_latest_bid", lambda s: bid)
    monkeypatch.setattr(gt, "has_open_position", lambda s: False)
    monkeypatch.setattr(gt, "can_open_new_position", lambda s: True)
    monkeypatch.setattr(gt, "get_account", lambda: {"ok": True, "equity": 100_000})
    monkeypatch.setattr(gt, "buys_left_this_poll", lambda: 3)
    monkeypatch.setattr(gt, "record_external_buy", lambda *a, **k: None)
    try:
        monkeypatch.setattr(gt, "prime_quotes", lambda symbols: None)
    except Exception:
        pass


def test_poll_once_buys_when_in_zone(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    state = {
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    }
    ew.save_watch(state)
    _patch_trading_ready(monkeypatch, gt)
    placed = []

    def fake_place(sym, decision, equity, **kw):
        placed.append(sym)
        return {"ok": True, "stop_price": 25.0, "target_1": 36.0}

    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    events = ew.poll_once(cfg=_poll_cfg(), now=1e12 + 10)
    assert placed == ["SMCI"]
    assert any(e.get("kind") == "entry_ok" or e.get("symbol") == "SMCI" for e in events) or placed
    saved = ew.load_watch()
    assert saved["SMCI"]["status"] in ("submitted", "filled")


def test_poll_once_in_zone_places_despite_wide_spread(tmp_path, monkeypatch):
    """UI READY = in zone; poll must place even when IEX spread is wide."""
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    state = {
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    }
    ew.save_watch(state)
    # ~1.8% spread with max 0.1% — still in zone → place.
    _patch_trading_ready(monkeypatch, gt, ask=28.0, bid=27.5)
    placed = []
    monkeypatch.setattr(
        cp, "place_scaled_entry",
        lambda *a, **k: placed.append(a[0]) or {"ok": True},
    )
    events = ew.poll_once(
        cfg=_poll_cfg(ai_max_spread_pct=0.1),
        now=1e12 + 10,
    )
    assert placed == ["SMCI"]
    assert ew.load_watch()["SMCI"]["status"] in ("submitted", "filled", "armed")


def test_poll_once_wait_setup_does_not_place(tmp_path, monkeypatch):
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    state = {
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_setup",
                "entry_low": 0, "entry_high": 0,
                "stop_price": 0, "target_1": 0, "reward_risk": 0,
            },
        }
    }
    ew.save_watch(state)
    _patch_trading_ready(monkeypatch, gt)
    placed = []
    evals = []

    def fake_place(sym, decision, equity, **kw):
        placed.append(sym)
        return {"ok": True}

    def fake_eval(*a, **k):
        evals.append(1)
        return {
            "decision": "WAIT", "wait_kind": "wait_setup",
            "entry_low": 0, "entry_high": 0,
            "stop_price": 0, "target_1": 0, "reward_risk": 0,
        }

    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    monkeypatch.setattr(cp, "evaluate_entry", fake_eval)
    events = ew.poll_once(cfg=_poll_cfg(), now=1e12 + 10)
    assert placed == []
    # Fresh structure within TTL → no blind restructure / no buy
    assert evals == []
    assert any(e.get("reason") == "wait_setup" for e in events)


def test_poll_once_gate_error_fail_closed_no_place(tmp_path, monkeypatch):
    """has_open_position exceptions must not fall through into place_scaled_entry."""
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    state = {
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    }
    ew.save_watch(state)
    _patch_trading_ready(monkeypatch, gt)

    def boom(_sym):
        raise RuntimeError("broker_down")

    monkeypatch.setattr(gt, "has_open_position", boom)
    placed = []

    def fake_place(sym, decision, equity, **kw):
        placed.append(sym)
        return {"ok": True, "stop_price": 25.0, "target_1": 36.0}

    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    events = ew.poll_once(cfg=_poll_cfg(), now=1e12 + 10)
    assert placed == []
    assert any(
        "gate_error:has_open_position" in str(e.get("reason") or "")
        for e in events
    )
    assert ew.load_watch()["SMCI"]["status"] == "watching"


def test_format_blocker_and_derive():
    import ai_entry_watch as ew
    assert ew.format_blocker("above_zone") == "above zone"
    assert ew.format_blocker("recheck_below_zone") == "left zone"
    assert "wash" in (ew.format_blocker("x", detail="potential wash trade detected") or "").lower()
    rec = {
        "status": "watching",
        "last_ask": 30.0,
        "structure": {
            "wait_kind": "wait_for_zone",
            "entry_low": 20.0, "entry_high": 21.0,
            "stop_price": 19.0, "target_1": 25.0, "reward_risk": 3.0,
        },
    }
    code, label = ew.derive_blocker(rec)
    assert code == "above_zone"
    assert label == "above zone"
    ew.set_block_reason(rec, "wash trade", detail="potential wash trade")
    code2, label2 = ew.derive_blocker(rec)
    assert "wash" in (label2 or "").lower()
    rec_in = {
        "status": "watching",
        "last_ask": 20.5,
        "structure": {
            "wait_kind": "wait_for_zone",
            "entry_low": 20.0, "entry_high": 21.0,
            "stop_price": 19.0, "target_1": 25.0, "reward_risk": 3.0,
        },
    }
    c3, l3 = ew.derive_blocker(rec_in)
    assert c3 == "in_zone"
    assert l3 == "in zone"


def test_find_double_bottom_support_matches_two_swing_lows():
    import ai_entry_watch as ew
    # Synthetic path of lows: trend down to 20, bounce, retest 20, bounce.
    lows = (
        [21.0, 20.8, 20.5, 20.2, 20.0, 20.1, 20.3, 20.5, 20.4, 20.2]
        + [20.0, 20.15, 20.4, 20.6, 20.8, 21.0, 21.2]
    )
    found = ew.find_double_bottom_support(
        lows, swing=2, match_pct=0.5, min_sep_bars=3)
    assert found is not None
    assert abs(found["support"] - 20.0) < 0.05
    assert found["low_a"] <= 20.05 and found["low_b"] <= 20.05


def test_build_double_bottom_zone_geometry():
    import ai_entry_watch as ew
    cfg = {
        "ai_watch_db_above_pct": 1.25,
        "ai_watch_db_below_pct": 0.25,
        "ai_watch_db_stop_below_pct": 0.5,
        "ai_watch_synth_rr": 0.6,
        "ai_watch_synth_scale_out_pct": 50,
        "ai_watch_synth_trail_pct": 2.5,
        "ai_watch_db_require_price_above": True,
    }
    z = ew.build_double_bottom_zone_structure(
        20.0, cfg, reason="test", low_a=20.0, low_b=19.98, last_price=20.50)
    assert z is not None
    assert z["zone_kind"] == "double_bottom"
    assert abs(z["entry_low"] - 20.0 * 0.9975) < 0.01
    assert abs(z["entry_high"] - 20.0 * 1.0125) < 0.01
    assert z["stop_price"] < z["entry_low"]
    assert z["target_1"] > z["entry_low"]  # T1 above zone floor; may sit near high at 0.6R
    assert z["support"] == 20.0
    # Price already through support → refuse long zone
    assert ew.build_double_bottom_zone_structure(
        20.0, cfg, last_price=19.5) is None


def test_double_bottom_zone_for_symbol_uses_injected_lows():
    import ai_entry_watch as ew
    lows = (
        [22, 21.5, 21, 20.5, 20.0, 20.2, 20.5, 20.8, 20.5, 20.2]
        + [20.0, 20.3, 20.6, 21.0, 21.2]
    )
    cfg = {
        "ai_watch_db_above_pct": 1.0,
        "ai_watch_db_below_pct": 0.25,
        "ai_watch_db_stop_below_pct": 0.5,
        "ai_watch_db_match_pct": 0.5,
        "ai_watch_db_swing_bars": 2,
        "ai_watch_db_min_sep_bars": 3,
        "ai_watch_synth_rr": 0.6,
    }
    z = ew.build_double_bottom_zone_for_symbol(
        "TEST", 21.0, cfg, lows=lows, reason="unit")
    assert z is not None
    assert z["zone_kind"] == "double_bottom"
    assert abs(z["support"] - 20.0) < 0.05
    assert z["entry_high"] > z["support"] > z["stop_price"]


def test_decision_for_place_keeps_db_stop_under_support():
    import ai_entry_watch as ew
    structure = {
        "synthetic": True,
        "zone_kind": "double_bottom",
        "entry_low": 19.95,
        "entry_high": 20.25,
        "stop_price": 19.90,
        "target_1": 20.50,
        "support": 20.0,
        "reward_risk": 0.6,
        "scale_out_pct": 50,
        "trail_pct": 2.5,
    }
    d = ew._decision_for_place(
        structure, ask=20.20,
        cfg={"ai_watch_synth_rr": 0.6, "ai_watch_db_stop_below_pct": 0.5})
    assert d["decision"] == "BUY"
    assert d["stop_price"] < 20.20
    assert d["stop_price"] <= 20.0  # still under support shelf
    assert d["target_1"] > d["stop_price"]


def test_synth_offset_zone_from_price():
    import ai_entry_watch as ew
    s = ew.build_offset_zone_structure(100.0, {
        "ai_watch_zone_offset_pct": 5.0,
        "ai_watch_zone_width_pct": 2.0,
        "ai_watch_synth_stop_pct": 2.0,
        "ai_watch_synth_rr": 3.0,
    })
    assert s["wait_kind"] == "wait_for_zone"
    assert abs(s["entry_high"] - 95.0) < 0.02  # 5% under 100
    assert s["entry_low"] < s["entry_high"]
    assert s["stop_price"] < s["entry_low"]
    assert s["target_1"] > s["entry_high"]
    assert s["synthetic"] is True
    rec = {"symbol": "X", "source": "trending", "status": "watching",
           "structure": {"wait_kind": "hard_no", "entry_low": 0, "entry_high": 0}}
    cfg = {
        "ai_watch_synth_zone_enabled": True,
        "ai_watch_zone_mode": "offset",  # legacy path under test
        "ai_watch_zone_offset_pct": 5.0,
        "ai_watch_zone_width_pct": 2.0, "ai_watch_synth_stop_pct": 2.0,
        "ai_watch_synth_rr": 3.0, "ai_watch_synth_reanchor_pct": 0.5,
    }
    ev = ew.ensure_offset_zone_if_needed(rec, 50.0, cfg, now=1.0)
    assert ev is not None
    assert rec["structure"]["wait_kind"] == "wait_for_zone"
    # Still near zone top → keep frozen zone
    hi = rec["structure"]["entry_high"]
    mid = (float(rec["structure"]["entry_low"]) + float(hi)) / 2.0
    ev2 = ew.ensure_offset_zone_if_needed(rec, mid, cfg, now=2.0)
    assert ev2 is None
    assert rec["structure"]["entry_high"] == hi
    # Price runs well above zone top → re-anchor from new last
    ev3 = ew.ensure_offset_zone_if_needed(rec, 60.0, cfg, now=3.0)
    assert ev3 is not None
    assert ev3.get("reason") == "reanchor_from_last"
    assert abs(float(rec["structure"]["entry_high"]) - 57.0) < 0.05  # 5% under 60


def test_poll_once_resets_the_buy_cap_each_poll(tmp_path, monkeypatch):
    """ai_max_buys_per_poll is per *poll*, so each poll must start a new budget.

    reset_poll_counters() was only called from the research path, so on the
    watch path the counter accumulated: after three lifetime buys every later
    READY name was skipped with "buy_cap" until a research run cleared it.
    """
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    })
    _patch_trading_ready(monkeypatch, gt)
    resets = []
    monkeypatch.setattr(gt, "reset_poll_counters", lambda: resets.append(1))
    monkeypatch.setattr(
        cp, "place_scaled_entry",
        lambda *a, **k: {"ok": True, "stop_price": 25.0, "target_1": 36.0})

    ew.poll_once(cfg=_poll_cfg(), now=1e12 + 10)
    assert resets, "poll_once must reset the per-poll buy budget"


def test_poll_once_does_not_clobber_a_concurrent_sync(tmp_path, monkeypatch):
    """poll_once writes back only what it touched.

    The 2s book sync and the 20s poll both read-modify-write the book. Blind
    whole-file writes meant the last writer won, silently reverting the other's
    work — including status="submitted" back to "watching", which re-armed a
    symbol that already had a live order.
    """
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    })
    _patch_trading_ready(monkeypatch, gt)

    # A sync lands mid-poll: adds NVDA, drops nothing.
    def fake_place(sym, decision, equity, **kw):
        book = ew.load_watch()
        book["NVDA"] = {"symbol": "NVDA", "status": "watching", "score": 9.0}
        ew.save_watch(book)
        return {"ok": True, "stop_price": 25.0, "target_1": 36.0}

    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    ew.poll_once(cfg=_poll_cfg(), now=1e12 + 10)

    saved = ew.load_watch()
    assert saved["SMCI"]["status"] in ("submitted", "filled")
    assert "NVDA" in saved, "poll_once clobbered a symbol added during the poll"


def _zone_cfg(**over):
    cfg = {
        "ai_watch_synth_zone_enabled": True,
        "ai_watch_zone_offset_pct": 2.0,
        "ai_watch_zone_width_pct": 2.0,
        "ai_watch_synth_stop_pct": 5.0,
        "ai_watch_synth_rr": 1.5,
        "ai_watch_synth_reanchor_pct": 0.0,
    }
    cfg.update(over)
    return cfg


def test_synth_zone_sits_2pct_under_the_print(tmp_path, monkeypatch):
    """At the old 5% offset the ask was a permanent +5.26% above the zone top,
    so only a 5% break from the high-water mark ever filled — above_zone was
    51% of every skip logged on 2026-08-04."""
    import ai_entry_watch as ew

    s = ew.build_offset_zone_structure(100.0, _zone_cfg())
    assert s["entry_high"] == 98.0                  # 100 * (1 - 2%)
    assert round(s["entry_low"], 3) == 96.04        # 98 * (1 - 2%)
    # The gap the ask must close is now 2%, not 5.26%.
    assert round(100.0 / s["entry_high"] - 1.0, 4) == 0.0204


def test_synth_zone_does_not_chase_a_falling_price(tmp_path, monkeypatch):
    """A pullback zone must hold still while price pulls back into it.

    This previously compared the live ask against entry_high. The zone sits
    ai_watch_zone_offset_pct BELOW its anchor, so that comparison was true on
    almost every poll — including while price fell — and the band was redrawn
    under each new lower print. Price could only ever enter it by dropping
    more than the offset within one poll interval: a crash, not a pullback.

    Measured on 2026-08-06: 22 zones drawn, 4 touched, 0 armed, 0 trades. The
    three that touched fell 12%, 24% and 30% in minutes; SOUN fell 2.4%
    against a 2.0% zone and never touched it, because the zone kept retreating.

    The original change was made for a cosmetic report ("the watchlist does not
    update current prices"). Showing a live price is a display concern; moving
    the entry level is not.
    """
    import ai_entry_watch as ew

    cfg = _zone_cfg()
    rec = {"symbol": "AAA", "source": "trending", "status": "watching"}
    assert ew.ensure_offset_zone_if_needed(rec, 100.0, cfg, 1000.0) is not None
    assert rec["structure"]["entry_high"] == 98.0

    # Price drifts DOWN toward the zone — the band must not move away.
    assert ew.ensure_offset_zone_if_needed(rec, 98.5, cfg, 1001.0) is None
    assert rec["structure"]["entry_high"] == 98.0, "zone chased price down"

    assert ew.ensure_offset_zone_if_needed(rec, 98.1, cfg, 1002.0) is None
    assert rec["structure"]["entry_high"] == 98.0

    # And a pullback that reaches the band finds it where it was left.
    assert 98.0 >= rec["structure"]["entry_low"]


def test_synth_zone_reanchors_when_price_runs_away_upward(tmp_path, monkeypatch):
    """The behaviour the re-anchor exists for: a name that got away (ZETA stuck
    at $24 while printing $28) must have its band lifted, or it waits forever
    for a level the stock has left behind."""
    import ai_entry_watch as ew

    cfg = _zone_cfg()
    rec = {"symbol": "AAA", "source": "trending", "status": "watching"}
    ew.ensure_offset_zone_if_needed(rec, 100.0, cfg, 1000.0)
    assert rec["structure"]["entry_high"] == 98.0

    ev = ew.ensure_offset_zone_if_needed(rec, 110.0, cfg, 1001.0)
    assert ev is not None and ev["reason"] == "reanchor_from_last"
    assert rec["structure"]["entry_high"] == round(110.0 * 0.98, 3)


def test_synth_zone_applies_to_research_records_too(tmp_path, monkeypatch):
    """Non-desk records used to be excluded, and the LLM refresh only fired on
    an *unusable* structure — so a stale research zone was refreshed by neither
    path. Zero synth_zone events were logged across all of 2026-08-04."""
    import ai_entry_watch as ew

    # Pin the offset band: this test is about *whether* a stale research zone
    # gets re-anchored, and asserts the offset geometry to prove it did. Left
    # unset it inherits the ai_watch_zone_mode default, which became
    # double_bottom in 697c854 — and because HPE is a real ticker,
    # build_double_bottom_zone_for_symbol found real bars and re-anchored to an
    # actual support level (46.97), so the assertion read as a regression when
    # the behaviour was correct. Pinning also drops the bar-cache dependency
    # that made this test's result depend on the machine it ran on.
    cfg = _zone_cfg(ai_structure_ttl_sec=100.0, ai_watch_zone_mode="offset")
    rec = {
        "symbol": "HPE", "source": "anthropic", "status": "watching",
        "structure_ts": 0.0,
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": 47.75, "entry_high": 48.85,
            "stop_price": 45.85, "target_1": 55.8, "reward_risk": 3.1,
        },
    }
    # Stale (ts 0 vs now 5000, ttl 100) and the print has run to 52.85.
    ev = ew.ensure_offset_zone_if_needed(rec, 52.85, cfg, 5000.0)
    assert ev is not None, "a stale research zone must be re-anchored"
    assert rec["structure"]["entry_high"] == round(52.85 * 0.98, 3)


def test_place_decision_puts_the_stop_5pct_under_the_actual_fill():
    """Derived from entry_low, real risk was 2-4% depending on where in the band
    the fill landed, swinging position size ~1.9x. Keyed to the price paid it is
    constant, and a 1.5R target is actually 1.5R."""
    import ai_entry_watch as ew

    structure = {
        "decision": "WAIT", "wait_kind": "wait_for_zone", "synthetic": True,
        "entry_low": 8.7514, "entry_high": 8.93,
        "stop_price": 8.42, "target_1": 9.1, "reward_risk": 1.5,
    }
    d = ew._decision_for_place(structure, ask=8.90, cfg=_zone_cfg())
    assert d["decision"] == "BUY" and d["wait_kind"] is None
    assert d["stop_price"] == 8.455                     # 8.90 * 0.95
    assert d["target_1"] == 9.568                       # 8.90 + 1.5 * 0.445
    # Risk per share is exactly 5% of the fill, so sizing is deterministic.
    assert round((8.90 - d["stop_price"]) / 8.90, 4) == 0.05


def test_place_decision_leaves_model_levels_alone():
    """Model zones come from real structure, not a percentage — never override."""
    import ai_entry_watch as ew

    structure = {
        "decision": "WAIT", "wait_kind": "wait_for_zone",
        "entry_low": 47.75, "entry_high": 48.85,
        "stop_price": 45.85, "target_1": 55.8, "reward_risk": 3.1,
    }
    d = ew._decision_for_place(structure, ask=48.0, cfg=_zone_cfg())
    assert d["stop_price"] == 45.85 and d["target_1"] == 55.8


def _incl_cfg(**over):
    cfg = {
        "ai_watch_require_uptrend": True,
        "ai_watch_require_indicators": True,
        "ai_watch_min_proximity": 67,
        "ai_watch_min_adx": 0.0,
        "ai_watch_min_price": 1.0,
        "ai_watch_admit_ticks": 1,
        "ai_min_dollar_volume": 0.0,
    }
    cfg.update(over)
    return cfg


def _bullish(prox=67, sell=False, adx=None):
    d = {"proximity_pct": prox, "sell_signal": sell, "status": "aligning"}
    if adx is not None:
        d["adx"] = adx
    return d


def test_inclusion_rejects_a_name_that_is_down_on_the_day():
    """Long-only desk. The old rule ranked on abs(pct_change), so on 2026-08-05
    four of six admitted names were down: DKNG -7.0, UBER -6.9, SNAP -9.1,
    CIFR -3.4 — all admitted on Stocktwits popularity alone."""
    import ai_entry_watch as ew

    row = {"symbol": "DKNG", "price": 21.98, "pct_change": -7.04, "score": 14.9}
    ok, _met, why = ew.passes_inclusion(
        row, _incl_cfg(), indicators={"DKNG": _bullish()})
    assert ok is False and why == "not_uptrend"


def test_inclusion_rejects_popularity_only():
    """trending_score is a ranking tiebreak now, never an admission gate."""
    import ai_entry_watch as ew

    row = {"symbol": "AAA", "price": 20.0, "pct_change": 3.0,
           "score": 14.9, "criteria": ["score"]}
    ok, _met, why = ew.passes_inclusion(row, _incl_cfg(), indicators={})
    assert ok is False and why == "no_indicators", (
        "a name with no indicator data must be rejected, not admitted")


def test_inclusion_requires_bullish_indicator_state():
    import ai_entry_watch as ew

    row = {"symbol": "AAA", "price": 20.0, "pct_change": 3.0}
    cfg = _incl_cfg()
    ok, _m, why = ew.passes_inclusion(
        row, cfg, indicators={"AAA": _bullish(prox=33)})
    assert ok is False and why == "proximity_33"

    ok, _m, why = ew.passes_inclusion(
        row, cfg, indicators={"AAA": _bullish(prox=100, sell=True)})
    assert ok is False and why == "sell_signal"

    ok, met, why = ew.passes_inclusion(
        row, cfg, indicators={"AAA": _bullish(prox=100)})
    assert ok is True and "bullish" in met and "uptrend" in met


def test_inclusion_price_and_liquidity_floors():
    import ai_entry_watch as ew

    cfg = _incl_cfg(ai_min_dollar_volume=1_000_000.0)
    ind = {"AAA": _bullish()}
    sub_dollar = {"symbol": "AAA", "price": 0.75, "pct_change": 5.0}
    ok, _m, why = ew.passes_inclusion(sub_dollar, cfg, indicators=ind)
    assert ok is False and why == "below_min_price"

    thin = {"symbol": "AAA", "price": 20.0, "pct_change": 5.0,
            "dollar_volume": 5_000.0}
    ok, _m, why = ew.passes_inclusion(thin, cfg, indicators=ind)
    assert ok is False and why == "thin_dollar_volume"


def test_absent_price_rejects_as_no_price_not_below_min():
    """A missing price is a feed outage, not a penny stock.

    Both still reject — nothing downstream can size or zone a name with no
    price — but the gate scorecard reads the reason as a verdict about the
    symbol. Reporting a quote gap as "below_min_price" put PLTR, ABNB and NET
    in the penny-stock bucket, in bursts of the whole shortlist at once.
    """
    import ai_entry_watch as ew

    cfg = _incl_cfg()
    ind = {"AAA": _bullish()}

    no_px = {"symbol": "AAA", "price": None, "pct_change": 5.0}
    ok, _m, why = ew.passes_inclusion(no_px, cfg, indicators=ind)
    assert ok is False and why == "no_price"

    missing_key = {"symbol": "AAA", "pct_change": 5.0}
    ok, _m, why = ew.passes_inclusion(missing_key, cfg, indicators=ind)
    assert ok is False and why == "no_price"

    # A real sub-$1 price is still a real price verdict.
    cheap = {"symbol": "AAA", "price": 0.75, "pct_change": 5.0}
    ok, _m, why = ew.passes_inclusion(cheap, cfg, indicators=ind)
    assert ok is False and why == "below_min_price"

    # Relabelling must not become a new policy: a zero floor disables the
    # check, and a priceless row still passes it to be judged further down.
    no_floor = _incl_cfg(ai_watch_min_price=0.0)
    ok, _m, why = ew.passes_inclusion(no_px, no_floor, indicators=ind)
    assert why not in ("no_price", "below_min_price")


def test_indicator_record_carries_every_key_the_arm_gate_reads():
    """The arm gate must not require a field the record never copies.

    ai_watch_arm_require defaults to naming cm_rsi_rising. It was absent from
    the dict built during the watch sync, so should_arm_buy saw None for it on
    every record and no candidate could ever arm — 326 zones and 0 arms on
    2026-08-07 while the engine published cm_rsi_rising=True on the wire.
    """
    import ai_entry_watch as ew
    from config import DEFAULT_CONFIG

    wire = {
        "proximity_pct": 67, "status": "watching", "buy_signal": False,
        "sell_signal": False, "cm_ok": True, "pctr_ok": True,
        "cm_rsi_rising": True, "macd_ok": False, "cm_rsi": 30.7,
        "pctr": -74.7,
    }
    rec = {
        "symbol": "AAA", "status": "watching",
        "structure": {"decision": "WAIT", "wait_kind": "wait_for_zone",
                      "entry_low": 10.0, "entry_high": 11.0,
                      "stop_price": 9.5, "target_1": 12.0, "reward_risk": 1.5},
        "indicator": {k: wire[k] for k in wire},
    }
    cfg = {
        "ai_watch_arm_require_indicators": True,
        "ai_min_reward_risk": 0.0,
        # This test is about named engine flags, not live %R bars.
        "ai_watch_exhaustion_rules": False,
    }
    ok, why = ew.should_arm_buy(rec, ask=10.5, bid=10.4, cfg=cfg)
    assert ok is True, why

    # Every flag the shipped default gates on must be present on the wire
    # shape above, or the gate is unsatisfiable by construction.
    for key in DEFAULT_CONFIG["ai_watch_arm_require"]:
        assert key in wire, f"{key} is gated on but never published"


def test_entry_features_snapshot_selection_and_timing_separately():
    """The feature vector must carry WHY a name was admitted (selection) and
    the indicator state at arm (timing) as distinct fields — they are the two
    things being A/B'd and conflating them makes either unsliceable."""
    import time as _time

    import ai_entry_watch as ew

    rec = {
        "symbol": "AAA", "source": "trending", "score": 12.5,
        "admit_rvol": 2.4, "admit_pct_change": 6.1,
        "admit_look_reason": "EXT",
        "admit_criteria": ["score", "rvol", "uptrend", "ext"],
        "admit_ts": _time.time() - 300,
        "indicator": {"cm_ok": True, "pctr_ok": True, "cm_rsi_rising": False,
                      "cm_rsi": 18.3, "proximity_pct": 100},
    }
    f = ew._entry_features(rec, ask=14.22)

    # Selection side.
    assert f["source"] == "trending" and f["rvol"] == 2.4
    assert f["look_reason"] == "EXT" and "ext" in f["criteria"]
    # Timing side — recorded even when false, so "armed without it" is
    # distinguishable from "not recorded".
    assert f["cm_ok"] is True and f["cm_rsi_rising"] is False
    assert f["cm_rsi"] == 18.3
    # Time-of-day and dwell: a 09:35 entry and a 15:45 entry facing the
    # flatten are different trades with the same signal.
    assert f["entry_hour_et"] is not None
    assert 299 <= f["dwell_sec"] <= 301


def test_entry_features_keep_missing_values_missing():
    """A feature the desk never observed must stay None, not become 0.0 —
    averaging a substituted zero into a slice silently biases the result."""
    import ai_entry_watch as ew

    f = ew._entry_features({"symbol": "AAA", "source": "momentum"}, ask=None)
    assert f["rvol"] is None and f["pct_change"] is None
    assert f["cm_rsi"] is None and f["ask"] is None
    assert f["look_reason"] is None and f["criteria"] == []
    # Booleans are a real observation (the gate was checked and was false).
    assert f["cm_ok"] is False


def test_admission_provenance_survives_a_refresh_without_numbers():
    """The book is rebuilt every 2s. A refresh poll that arrives without rvol
    must not erase the admission numbers, or the feature vector is empty by
    the time price finally reaches the zone."""
    import ai_entry_watch as ew

    rows = [{"symbol": "AAA", "source": "trending", "score": 12.0,
             "reason": "heat", "agreement": True, "rvol": 2.4,
             "pct_change": 6.1, "look_reason": "EXT",
             "criteria": ["score", "rvol"]}]
    cfg = {"ai_watch_require_agreement": False}
    state = ew.upsert_from_rows(rows, cfg=cfg, now=100.0)
    assert state["AAA"]["admit_rvol"] == 2.4
    assert state["AAA"]["admit_look_reason"] == "EXT"

    bare = [{"symbol": "AAA", "source": "trending", "score": 12.0,
             "reason": "heat", "agreement": True}]
    state = ew.upsert_from_rows(bare, cfg=cfg, now=102.0)
    assert state["AAA"]["admit_rvol"] == 2.4, "refresh erased admission rvol"
    assert state["AAA"]["admit_look_reason"] == "EXT"
    assert state["AAA"]["admit_ts"] == 100.0, "admit_ts must mark first admit"


def test_live_sync_path_records_admission_provenance(tmp_path, monkeypatch):
    """The LIVE book is rebuilt by _sync_locked every 2s, not upsert_from_rows.

    The admission fields were first added only to upsert_from_rows, so every
    record the running desk actually created carried admit_rvol=None and
    admit_criteria=None — the selection half of the entry feature vector was
    silently empty on 2026-08-06 while appearing correct in tests.
    """
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(
        ew, "desk_candidate_rows",
        lambda cfg=None: [{
            "symbol": "SOUN", "source": "trending", "score": 19.3,
            "reason": "trending score 19.3", "agreement": True,
            "rvol": 2.1, "pct_change": 18.0, "look_reason": "EXT",
            "criteria": ["score", "rvol"],
        }],
    )
    cfg = {"ai_watch_seed_momentum": True, "ai_watch_seed_trending": True,
           "ai_watch_require_uptrend": False, "ai_watch_require_indicators": False,
           "ai_watch_admit_ticks": 1, "ai_watch_min_price": 0.0,
           "ai_watch_min_rvol": 0.0, "ai_watch_require_look_ext": False}

    state = ew.sync_watch_from_source_panels(cfg, now=500.0)
    rec = state["SOUN"]
    assert rec["admit_rvol"] == 2.1, "live path lost admission rvol"
    assert rec["admit_look_reason"] == "EXT"
    assert rec["admit_criteria"] == ["score", "rvol"]
    assert rec["admit_ts"] == 500.0

    # And it survives the 2s rebuild that arrives without the numbers.
    monkeypatch.setattr(
        ew, "desk_candidate_rows",
        lambda cfg=None: [{
            "symbol": "SOUN", "source": "trending", "score": 19.3,
            "reason": "trending score 19.3", "agreement": True,
        }],
    )
    state = ew.sync_watch_from_source_panels(cfg, now=502.0)
    rec = state["SOUN"]
    assert rec["admit_rvol"] == 2.1, "rebuild erased admission rvol"
    assert rec["admit_ts"] == 500.0, "admit_ts must mark first admit"


def test_inclusion_requires_rvol_for_momentum_and_trending():
    """Popularity/flag alone isn't evidence of a real dislocation — relative
    volume has to back it up, for both sources. Research rows are exempt."""
    import ai_entry_watch as ew

    cfg = _incl_cfg(ai_watch_require_indicators=False, ai_watch_min_rvol=2.0)

    thin = {"symbol": "AAA", "price": 20.0, "pct_change": 3.0, "rvol": 1.2,
            "source": "trending", "criteria": ["score"], "look_reason": "EXT"}
    ok, _m, why = ew.passes_inclusion(thin, cfg, indicators={})
    assert ok is False and why == "thin_rvol"

    strong = {"symbol": "AAA", "price": 20.0, "pct_change": 3.0, "rvol": 2.0,
              "source": "momentum"}
    ok, met, why = ew.passes_inclusion(strong, cfg, indicators={})
    assert ok is True and "rvol" in met

    # Soft mom_open seed used to skip RVOL entirely — thin Mom names on the
    # book (ACHR 1.72x, CMG 0.66x). Known-thin still refuses.
    mom_soft = {
        "symbol": "BBB", "price": 10.0, "pct_change": 5.0, "rvol": 1.76,
        "source": "momentum", "criteria": ["mom_open"], "mom_open_soft": True,
    }
    ok, _m, why = ew.passes_inclusion(mom_soft, cfg, indicators={})
    assert ok is False and why == "thin_rvol"

    mom_ok = dict(mom_soft, rvol=2.5)
    ok, met, why = ew.passes_inclusion(mom_ok, cfg, indicators={})
    assert ok is True and "rvol" in met and "mom_open" in met

    # Research-sourced rows carry no rvol at all — must not be gated on it.
    research = {"symbol": "AAA", "price": 20.0, "pct_change": 3.0,
                "source": "anthropic"}
    ok, _m, why = ew.passes_inclusion(research, cfg, indicators={})
    assert ok is True


def test_unknown_rvol_and_missing_ext_abstain_rather_than_reject():
    """Unknown RVOL abstains; missing EXT is a reject when require_look_ext is on.

    RVOL still: absence is not thin. LOOK: operator wants only EXT longs, so
    a trending name with no look_reason (or WASH) does not admit.
    """
    import ai_entry_watch as ew

    cfg = _incl_cfg(ai_watch_require_indicators=False, ai_watch_min_rvol=1.5)

    # Momentum row as the live desk emits it: flag + price + pct, no rvol.
    mom = {"symbol": "AAA", "price": 20.0, "pct_change": 7.6,
           "source": "momentum", "criteria": ["flag"]}
    ok, met, why = ew.passes_inclusion(mom, cfg, indicators={})
    assert ok is True, f"momentum row with unknown rvol was rejected: {why}"
    assert "rvol" not in met, "must not claim an rvol it never saw"

    # Trending without LOOK tag — not_ext when require_look_ext is on.
    cfg_ext = dict(cfg, ai_watch_require_look_ext=True)
    tr = {"symbol": "BBB", "price": 20.0, "pct_change": 18.0, "rvol": None,
          "source": "trending", "criteria": ["score"]}
    ok, met, why = ew.passes_inclusion(tr, cfg_ext, indicators={})
    assert ok is False and why == "not_ext"

    # Unknown rvol + EXT still admits (rvol abstains).
    tr_ext = dict(tr, look_reason="EXT")
    ok, met, why = ew.passes_inclusion(tr_ext, cfg_ext, indicators={})
    assert ok is True and "ext" in met

    # Default/post-2026-08-11: non-EXT trending heat may admit (WASH still no).
    ok, met, why = ew.passes_inclusion(tr, cfg, indicators={})
    assert ok is True


def test_inclusion_criteria_are_not_duplicated():
    """criteria lands in the entry feature vector, where slicing reads it as a
    set — the shortlist tags what it matched and the gates re-append on
    independent confirmation, so 'rvol' appeared twice on live trending rows."""
    import ai_entry_watch as ew

    cfg = _incl_cfg(ai_watch_require_indicators=False, ai_watch_min_rvol=1.5)
    row = {"symbol": "AAA", "price": 20.0, "pct_change": 5.0, "rvol": 2.2,
           "source": "trending", "criteria": ["score", "rvol"],
           "look_reason": "EXT"}
    ok, met, _why = ew.passes_inclusion(row, cfg, indicators={})
    assert ok is True
    assert len(met) == len(set(met)), f"duplicate criteria: {met}"


def test_inclusion_trending_requires_score_and_ext_flag():
    """Legacy EXT path: require_look_ext refuses non-EXT; WASH always refused."""
    import ai_entry_watch as ew

    cfg = _incl_cfg(
        ai_watch_require_indicators=False,
        ai_watch_min_rvol=1.5,
        ai_watch_require_look_ext=True,
    )
    base = {"symbol": "AAA", "price": 20.0, "pct_change": 3.0, "rvol": 2.0,
            "source": "trending"}

    wash = dict(base, criteria=["score"], look_reason="WASH")
    ok, _m, why = ew.passes_inclusion(wash, cfg, indicators={})
    assert ok is False and why == "look_wash"

    no_ext = dict(base, criteria=["score"], look_reason="")
    ok, _m, why = ew.passes_inclusion(no_ext, cfg, indicators={})
    assert ok is False and why == "not_ext"

    both = dict(base, criteria=["score"], look_reason="EXT")
    ok, met, why = ew.passes_inclusion(both, cfg, indicators={})
    assert ok is True and "ext" in met

    # Conversion path: require_look_ext off → score/rvol heat without EXT.
    loose = dict(cfg, ai_watch_require_look_ext=False)
    ok, met, why = ew.passes_inclusion(no_ext, loose, indicators={})
    assert ok is True


def test_admission_dwell_requires_consecutive_qualifying_polls():
    """The book is rebuilt every 2s; without dwell a one-tick blip deleted a
    name and threw away its frozen zone, then re-admitted it at a worse price."""
    import ai_entry_watch as ew

    ew._admit_ticks.clear()
    cfg = _incl_cfg(ai_watch_admit_ticks=2)
    rows = [{"symbol": "AAA", "price": 20.0, "pct_change": 3.0}]
    ind = {"AAA": _bullish()}

    kept, rejected = ew.apply_inclusion_gate(rows, cfg, indicators=ind)
    assert kept == [] and rejected[0]["reason"] == "dwell_1/2"

    kept, _ = ew.apply_inclusion_gate(rows, cfg, indicators=ind)
    assert [r["symbol"] for r in kept] == ["AAA"]

    # A failing poll resets the counter — no partial credit.
    ew.apply_inclusion_gate(
        [{"symbol": "AAA", "price": 20.0, "pct_change": -1.0}], cfg,
        indicators=ind)
    kept, rejected = ew.apply_inclusion_gate(rows, cfg, indicators=ind)
    assert kept == [] and rejected[0]["reason"] == "dwell_1/2"


def test_inclusion_on_the_real_2026_08_05_trending_payload():
    """The six names the old OR-rule admitted, with their live numbers. Only the
    two that were actually up on the day survive the direction gate."""
    import ai_entry_watch as ew

    ew._admit_ticks.clear()
    live = [
        ("DKNG", 21.98, 14.9, -7.04), ("UBER", 67.06, 13.2, -6.85),
        ("APPS", 12.74, 11.4, 34.10), ("ZETA", 27.06, 11.1, 11.70),
        ("SNAP", 5.26, 12.0, -9.13), ("CIFR", 19.69, 10.9, -3.40),
    ]
    rows = [{"symbol": s, "price": p, "score": sc, "pct_change": ch}
            for s, p, sc, ch in live]
    ind = {s: _bullish() for s, _, _, _ in live}

    kept, _ = ew.apply_inclusion_gate(rows, _incl_cfg(), indicators=ind)
    assert sorted(r["symbol"] for r in kept) == ["APPS", "ZETA"]


def test_poll_once_enforces_the_daily_loss_limit(tmp_path, monkeypatch):
    """pre_entry_gate lived only in the research path, so the daily loss limit,
    aggregate open-risk cap and already-managed check did not bind on the path
    that places essentially every live trade."""
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    })
    _patch_trading_ready(monkeypatch, gt)
    placed = []
    monkeypatch.setattr(
        cp, "place_scaled_entry",
        lambda *a, **k: placed.append(a[0]) or {"ok": True})
    # Already down 3.5R today, past the 3.0R limit.
    monkeypatch.setattr(cp, "realized_r_today", lambda now=None: -3.5)

    cfg = dict(_poll_cfg())
    cfg["ai_daily_loss_limit_r"] = 3.0
    ew.poll_once(cfg=cfg, now=1e12 + 10)

    assert placed == [], "must not open a new position past the daily loss cap"
    assert ew.load_watch()["SMCI"]["block_code"] == "daily_loss_limit"


def test_poll_once_blocks_re_entry_during_cooldown(tmp_path, monkeypatch):
    """A stopped-out name must not immediately re-arm inside the same move."""
    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    })
    _patch_trading_ready(monkeypatch, gt)
    placed = []
    monkeypatch.setattr(
        cp, "place_scaled_entry",
        lambda *a, **k: placed.append(a[0]) or {"ok": True})
    monkeypatch.setattr(ew, "_recent_exit_ts", lambda s: 1e12 - 60)

    cfg = dict(_poll_cfg())
    cfg["ai_reentry_cooldown_sec"] = 900.0
    ew.poll_once(cfg=cfg, now=1e12 + 10)

    assert placed == []
    assert ew.load_watch()["SMCI"]["block_code"] == "reentry_cooldown"


def test_dead_reentry_blocks_same_et_day():
    import ai_entry_watch as ew

    cfg = {"ai_dead_reentry_block": True}
    # 2026-08-13 11:00 ET
    now = 1_786_618_800.0
    monkey_ts = now - 3600.0
    assert ew._dead_reentry_blocked("OMER", now, cfg) is False

    orig = ew._recent_dead_exit_ts
    ew._recent_dead_exit_ts = lambda s: monkey_ts if s == "OMER" else None
    try:
        assert ew._dead_reentry_blocked("OMER", now, cfg) is True
        # Next ET session is clear.
        assert ew._dead_reentry_blocked("OMER", now + 86400.0, cfg) is False
        off = dict(cfg, ai_dead_reentry_block=False)
        assert ew._dead_reentry_blocked("OMER", now, off) is False
    finally:
        ew._recent_dead_exit_ts = orig


def _stream_cfg(**over):
    cfg = {
        "ai_watch_stream_enabled": True,
        "ai_watch_stream_max_age_sec": 10.0,
        "ai_watch_stream_skip_margin_pct": 1.0,
    }
    cfg.update(over)
    return cfg


def _zoned_rec(sym="AAA", lo=27.0, hi=29.0):
    return {
        "symbol": sym, "status": "watching",
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": lo, "entry_high": hi,
            "stop_price": 25.0, "target_1": 36.0, "reward_risk": 1.5,
        },
    }


def _tape(monkeypatch, price, age):
    import ai_entry_watch as ew
    monkeypatch.setattr(
        ew, "_dashboard_tickers",
        lambda: [{"ticker": "AAA", "price": price, "price_age_sec": age}])


def test_stream_skips_the_rest_quote_when_price_is_far_from_the_zone(monkeypatch):
    """_latest_ask/_latest_bid are a REST round trip each, per symbol, per poll —
    ~120 calls/min on a full book against a 200/min limit shared with the
    engine, RS screener and dashboard."""
    import ai_entry_watch as ew

    _tape(monkeypatch, 40.0, 1.0)                     # way above a 27-29 zone
    far, px = ew.stream_says_far_from_zone(_zoned_rec(), _stream_cfg())
    assert far is True and px == 40.0

    _tape(monkeypatch, 10.0, 1.0)                     # below — still armable
    far, _ = ew.stream_says_far_from_zone(_zoned_rec(), _stream_cfg())
    assert far is False


def test_stream_never_skips_near_the_zone(monkeypatch):
    """The socket carries trades, not quotes: a print can land at the bid while
    the ask is still outside the zone. Anything near the band must take the
    real quote, or we arm on a price the order cannot get."""
    import ai_entry_watch as ew

    _tape(monkeypatch, 28.0, 1.0)                     # inside the zone
    far, _ = ew.stream_says_far_from_zone(_zoned_rec(), _stream_cfg())
    assert far is False

    # Just outside, but inside the 1% skip margin → still fetch the real ask.
    _tape(monkeypatch, 29.2, 1.0)
    far, _ = ew.stream_says_far_from_zone(_zoned_rec(), _stream_cfg())
    assert far is False


def test_stale_or_unknown_tape_falls_back_to_the_real_quote(monkeypatch):
    """price_age_sec is the real observation age — price_ts is a write time and
    always reads fresh, so it must never be used for staleness."""
    import ai_entry_watch as ew

    _tape(monkeypatch, 40.0, 45.0)                    # far, but 45s old
    far, _ = ew.stream_says_far_from_zone(_zoned_rec(), _stream_cfg())
    assert far is False

    monkeypatch.setattr(
        ew, "_dashboard_tickers",
        lambda: [{"ticker": "AAA", "price": 40.0}])   # no age at all
    far, _ = ew.stream_says_far_from_zone(_zoned_rec(), _stream_cfg())
    assert far is False


def test_no_zone_yet_always_takes_the_real_quote(monkeypatch):
    """Without a zone there is nothing to compare against — and the ask is what
    builds the zone in the first place."""
    import ai_entry_watch as ew

    _tape(monkeypatch, 40.0, 1.0)
    rec = {"symbol": "AAA", "status": "watching", "structure": None}
    far, _ = ew.stream_says_far_from_zone(rec, _stream_cfg())
    assert far is False


def test_stream_prefilter_can_be_disabled(monkeypatch):
    import ai_entry_watch as ew

    _tape(monkeypatch, 40.0, 1.0)
    far, _ = ew.stream_says_far_from_zone(
        _zoned_rec(), _stream_cfg(ai_watch_stream_enabled=False))
    assert far is False


def test_poll_once_skips_quotes_but_still_reports_a_blocker(tmp_path, monkeypatch):
    """A skipped poll must still tell the operator where price is.

    last_ask follows the live tape so the book / EXH / arm share one print
    (FGI leftover 11.69 vs tape 10.28). REST is still skipped when far.
    """
    import ai_entry_watch as ew
    import ai_trading as gt
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    ew.save_watch({
        "AAA": dict(_zoned_rec(), agreement=True, score=8.0, structure_ts=1e12,
                    last_ask=28.0),
    })
    # Do not use _patch_trading_ready — it stubs stream_says_far_from_zone off.
    fixed_et = datetime(2026, 8, 5, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(ew, "_et_now", lambda now=None: fixed_et)
    monkeypatch.setattr(ew, "sod_liquidate_done", lambda cfg, now=None: True)
    monkeypatch.setattr(ew, "expire_stale_watches_for_new_day", lambda now: None)
    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: {})
    monkeypatch.setattr(ew, "ensure_live_exhaustion", lambda *a, **k: False)
    monkeypatch.setattr(gt, "market_is_open", lambda: True)
    monkeypatch.setattr(gt, "is_ready", lambda: True)
    monkeypatch.setattr(gt, "has_open_position", lambda s: False)
    monkeypatch.setattr(gt, "can_open_new_position", lambda s: True)
    monkeypatch.setattr(gt, "get_account", lambda: {"ok": True, "equity": 100_000})
    monkeypatch.setattr(gt, "buys_left_this_poll", lambda: 3)
    monkeypatch.setattr(gt, "record_external_buy", lambda *a, **k: None)
    try:
        monkeypatch.setattr(gt, "prime_quotes", lambda symbols: None)
    except Exception:
        pass
    called = []
    monkeypatch.setattr(gt, "_latest_ask", lambda s: called.append(s) or 28.0)
    monkeypatch.setattr(gt, "_latest_bid", lambda s: 27.9)
    _tape(monkeypatch, 40.0, 1.0)

    ew.poll_once(cfg={**_poll_cfg(), **_stream_cfg()}, now=1e12 + 10)

    assert called == [], "no REST quote should be issued when the tape is far off"
    rec = ew.load_watch()["AAA"]
    assert rec["block_code"] == "above_zone"
    assert rec["last_trade"] == 40.0
    assert rec["last_ask"] == 40.0
    assert rec.get("last_ask_src") == "stream"


def test_engine_push_respects_the_websocket_subscription_cap(monkeypatch):
    """Finnhub's free tier allows ~50 concurrent WS subscriptions desk-wide and
    finnhub_stream.request_subscribe does not enforce it. Overflow symbols get
    no trades -> no forming bars -> no indicator state -> silently rejected."""
    import ai_entry_watch as ew

    known = {f"SYM{i}": {"proximity_pct": 67} for i in range(20)}
    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: known)
    monkeypatch.setattr(ew, "_push_cfg", lambda: {"ai_watch_engine_push_max": 24})

    pushed = {}

    def fake_urlopen(req, timeout=None):
        import json as _j
        pushed["tickers"] = _j.loads(req.data.decode())["tickers"]

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    cands = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN",
             "META", "NFLX", "INTC", "AMD", "SOFI"]
    out = ew.push_candidates_to_engine(cands)
    # 20 already known, cap 24 -> only 4 slots left.
    assert out["pushed"] == 4
    assert len(pushed["tickers"]) == 4


def test_engine_push_stops_entirely_when_the_cap_is_full(monkeypatch):
    import ai_entry_watch as ew

    known = {f"SYM{i}": {} for i in range(24)}
    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: known)
    monkeypatch.setattr(ew, "_push_cfg", lambda: {"ai_watch_engine_push_max": 24})

    def boom(*a, **k):
        raise AssertionError("must not push past the subscription cap")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", boom)

    out = ew.push_candidates_to_engine(["AAPL", "MSFT"])
    assert out["pushed"] == 0 and out.get("capped") is True


def _armable_rec(cm=True, pctr=True, macd=False, sell=False, with_indicator=True):
    rec = {
        "symbol": "AAA", "status": "watching",
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": 27.0, "entry_high": 28.5,
            "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        },
    }
    if with_indicator:
        rec["indicator"] = {
            "cm_ok": cm, "pctr_ok": pctr, "macd_ok": macd,
            "sell_signal": sell,
            "proximity_pct": 33 * sum((cm, pctr, macd)),
            # Raw %R so exhaustion_allows_buy can pass when rules are on.
            "pctr": -5.0 if pctr else -80.0,
            "pctr_rising": bool(pctr),
            "pctr_falling": not pctr,
        }
    return rec


def _arm_cfg(**over):
    cfg = {
        "ai_entry_zone_pad_pct": 0.15,
        "ai_min_reward_risk": 3.0,
        "ai_watch_arm_require_indicators": True,
        "ai_watch_arm_require": ["cm_ok", "pctr_ok"],
        "ai_watch_arm_min_proximity": 0,
        # These tests target the named cm/pctr flags, not live %R bars.
        "ai_watch_exhaustion_rules": False,
    }
    cfg.update(over)
    return cfg


def test_arm_requires_price_in_zone_AND_the_two_named_indicators():
    """CM RSI-2 and %R exhaustion are the buy signals. The zone answers "is this
    a good price"; those two answer "is this a good moment". Both must hold at
    the same instant — admission only checks indicators once, at the 2s sync."""
    import ai_entry_watch as ew

    ok, why = ew.should_arm_buy(_armable_rec(), ask=28.0, bid=27.9, cfg=_arm_cfg())
    assert ok and why.startswith("zone")

    for cm, pctr in ((True, False), (False, True), (False, False)):
        ok, why = ew.should_arm_buy(
            _armable_rec(cm=cm, pctr=pctr), ask=28.0, bid=27.9, cfg=_arm_cfg())
        assert not ok and why == "indicators_faded", (cm, pctr)

    ok, why = ew.should_arm_buy(
        _armable_rec(sell=True), ask=28.0, bid=27.9, cfg=_arm_cfg())
    assert not ok and why == "sell_signal"

    # Perfect indicators at the wrong price is still not an entry.
    ok, why = ew.should_arm_buy(_armable_rec(), ask=32.0, bid=31.9, cfg=_arm_cfg())
    assert not ok and why == "above_zone"


def test_macd_does_not_block_the_entry():
    """MACD is the laggard by design — buy_signal's docstring notes that by the
    time it crosses, CM RSI-2 has usually left the <40 zone. Requiring it (which
    a proximity==100 count silently did) waits out the move."""
    import ai_entry_watch as ew

    rec = _armable_rec(cm=True, pctr=True, macd=False)
    assert rec["indicator"]["proximity_pct"] == 66      # would fail a 100 count
    ok, why = ew.should_arm_buy(rec, ask=28.0, bid=27.9, cfg=_arm_cfg())
    assert ok and why.startswith("zone")


def test_arm_rejects_when_indicator_state_is_missing():
    """Absence is not a pass — a name the engine has no reading for must not arm
    on price alone."""
    import ai_entry_watch as ew

    ok, why = ew.should_arm_buy(
        _armable_rec(with_indicator=False), ask=28.0, bid=27.9, cfg=_arm_cfg())
    assert not ok and why == "no_indicators"


def test_arm_require_list_is_configurable():
    """Putting macd_ok back in the list restores the old all-three behaviour."""
    import ai_entry_watch as ew

    cfg = _arm_cfg(ai_watch_arm_require=["cm_ok", "pctr_ok", "macd_ok"])
    ok, why = ew.should_arm_buy(_armable_rec(macd=False), ask=28.0, bid=27.9, cfg=cfg)
    assert not ok and why == "indicators_faded"
    ok, _ = ew.should_arm_buy(_armable_rec(macd=True), ask=28.0, bid=27.9, cfg=cfg)
    assert ok


def test_ready_is_false_when_the_poller_recorded_a_blocker():
    """READY must reflect the poller's verdict, not just price-vs-zone.

    Two ways they diverge: the stream pre-filter skips the REST quote and leaves
    last_ask stale (a stale in-zone ask would read READY while the tape is far
    away), and portfolio gates block names whose price genuinely is in the zone.
    Either would show READY for something that will never fill.
    """
    import ai_entry_watch as ew

    base = {
        "symbol": "AAA", "status": "watching", "score": 8.0, "last_ask": 28.0,
        "structure": {
            "decision": "WAIT", "wait_kind": "wait_for_zone",
            "entry_low": 27.0, "entry_high": 29.0,
            "stop_price": 25.0, "target_1": 33.0, "reward_risk": 1.5,
        },
    }
    clean = ew.public_snapshot(state={"AAA": dict(base)})[0]
    assert clean["in_zone"] is True and clean["ready"] is True

    # Same in-zone last_ask, but the poll refused for a portfolio reason.
    blocked = dict(base, block_code="daily_loss_limit",
                   block_reason="day loss cap")
    row = ew.public_snapshot(state={"AAA": blocked})[0]
    assert row["in_zone"] is True, "price really is in the zone"
    assert row["ready"] is False, "but the poller will not buy it"

    # Stream pre-filter case: tape is far off, last_ask left stale in-zone.
    stale = dict(base, block_code="above_zone", block_reason="above zone")
    assert ew.public_snapshot(state={"AAA": stale})[0]["ready"] is False


def test_engine_push_is_debounced_between_engine_scans(monkeypatch):
    """A pushed symbol does not appear in the indicator map until the engine's
    next scan (60s), so without a debounce the 2s book sync re-POSTs it ~30x."""
    import ai_entry_watch as ew

    ew._pushed_at.clear()
    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: {})
    monkeypatch.setattr(
        ew, "_push_cfg",
        lambda: {"ai_watch_engine_push_max": 24, "scan_interval_sec": 60})

    posts = []

    def fake_urlopen(req, timeout=None):
        posts.append(1)

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    first = ew.push_candidates_to_engine(["AAPL"])
    assert first["pushed"] == 1 and len(posts) == 1

    # Engine still has not computed it — must not re-POST.
    for _ in range(10):
        again = ew.push_candidates_to_engine(["AAPL"])
        assert again["pushed"] == 0 and again.get("debounced") is True
    assert len(posts) == 1, "re-POSTed a symbol still inside the debounce hold"


def test_no_dead_ai_knobs_are_dashboard_editable():
    """A key in SAFE_CONFIG_KEYS that nothing reads is worse than no key: the
    operator edits it, the UI accepts it, and nothing changes. Several shipped
    that way (ai_watch_include_research and ai_watch_momentum_require_flag were
    both hardcoded in sync_watch_from_source_panels; ai_trade_amount and
    ai_quote_poll existed only in config.py).
    """
    from pathlib import Path
    from config import SAFE_CONFIG_KEYS

    root = Path(__file__).resolve().parent.parent
    # One pass over the tree — grepping per key made the suite 9x slower.
    blob = []
    for f in root.rglob("*.py"):
        parts = set(f.parts)
        if parts & {"tests", ".worktrees", ".venv", "node_modules"}:
            continue
        if f.name == "config.py":
            continue
        try:
            blob.append(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    haystack = "\n".join(blob)

    dead = [k for k in SAFE_CONFIG_KEYS
            if k.startswith(("ai_", "claude_", "grok_")) and k not in haystack]
    assert not dead, f"dashboard-editable keys nothing reads: {dead}"


def test_dashboard_url_follows_env_not_hardcoded_localhost(monkeypatch):
    """The engine and this module must resolve to ONE dashboard.

    Regression guard. DASHBOARD_URL was hardcoded to 127.0.0.1:8888 while
    signal_engine.py read DASHBOARD_URL from the environment. On a two-box
    setup that split the desk: the engine polled the remote for its symbol
    list, this module pushed candidates to a local dashboard the engine never
    read, and _engine_indicator_map() came back empty forever — so
    should_arm_buy blocked every symbol on `no_indicators` and the desk could
    not place a single trade. Nothing failed loudly; it just never traded.
    """
    import importlib
    import ai_entry_watch

    monkeypatch.setenv("DASHBOARD_URL", "https://example.invalid/")
    mod = importlib.reload(ai_entry_watch)
    try:
        assert mod.DASHBOARD_URL == "https://example.invalid", (
            "DASHBOARD_URL must come from the environment so the engine and the "
            "watchlist share one universe"
        )
    finally:
        monkeypatch.delenv("DASHBOARD_URL", raising=False)
        importlib.reload(ai_entry_watch)


def test_dashboard_requests_send_a_non_default_user_agent():
    """The edge fronting the remote dashboard 403s urllib's default agent.

    signal_engine.py never hit this because `requests` sends its own UA, so the
    failure was invisible until this module started talking to the remote.
    """
    import ai_entry_watch as ew

    assert ew._DASH_UA and "urllib" not in ew._DASH_UA.lower()

    seen = {}

    class _Resp:
        def read(self):
            return b'{"tickers": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return _Resp()

    import urllib.request

    orig = urllib.request.urlopen
    urllib.request.urlopen = _fake_urlopen
    try:
        ew.dashboard_state(force=True)
    finally:
        urllib.request.urlopen = orig

    assert seen["ua"] == ew._DASH_UA
    assert seen["timeout"] == ew._DASH_TIMEOUT


def test_capped_engine_push_sends_the_best_candidates_not_the_alphabet(monkeypatch):
    """desk_candidate_rows ranks by score and the push cap truncates, so order
    decides which names get indicator data at all.

    Alphabetising first meant a capped push sent the A-names. The strongest
    setups could then sit on the book with no indicators and be rejected as
    "indicators_faded" — indistinguishable from a genuine fade, and invisible
    in every metric.
    """
    import ai_entry_watch as ew

    posted = {}

    def _fake_urlopen(req, timeout=None):
        posted["tickers"] = json.loads(req.data.decode())["tickers"]

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: {})
    monkeypatch.setattr(ew, "_push_cfg",
                        lambda: {"ai_watch_engine_push_max": 3,
                                 "scan_interval_sec": 60})
    ew._pushed_at.clear()
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    # Ranked best-first; the alphabetically-first names are the WORST here.
    ew.push_candidates_to_engine(["ZTOP", "YSEC", "XTHD", "AAAA", "BBBB"])

    assert posted["tickers"] == ["ZTOP", "YSEC", "XTHD"], (
        "capped push must keep caller ranking, not sort alphabetically")


def test_engine_push_dedupes_without_losing_order(monkeypatch):
    import ai_entry_watch as ew

    posted = {}

    def _fake_urlopen(req, timeout=None):
        posted["tickers"] = json.loads(req.data.decode())["tickers"]

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    monkeypatch.setattr(ew, "_engine_indicator_map", lambda: {})
    monkeypatch.setattr(ew, "_push_cfg",
                        lambda: {"ai_watch_engine_push_max": 10,
                                 "scan_interval_sec": 60})
    ew._pushed_at.clear()
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    ew.push_candidates_to_engine(["ZTOP", "AAAA", "ZTOP", "", "toolongsym", "BBBB"])
    assert posted["tickers"] == ["ZTOP", "AAAA", "BBBB"]


# ── config values of 0 must survive the read ────────────────────────────────

def test_opt_float_keeps_a_deliberate_zero():
    import ai_entry_watch as ew
    """`float(cfg.get(k, d) or d)` silently replaced 0 with the default, so no
    value in bot_config.json could turn the runner trail off."""
    assert ew._opt_float(0, 2.5) == 0.0
    assert ew._opt_float(0.0, 2.5) == 0.0
    assert ew._opt_float(None, 2.5) == 2.5
    assert ew._opt_float("", 2.5) == 2.5
    assert ew._opt_float("junk", 2.5) == 2.5


def test_zero_trail_is_honored_by_both_zone_builders():
    import ai_entry_watch as ew

    cfg = {"ai_watch_synth_trail_pct": 0}

    offset = ew.build_offset_zone_structure(10.0, cfg)
    assert offset["trail_pct"] == 0.0

    db = ew.build_double_bottom_zone_structure(10.0, cfg, last_price=10.2)
    assert db["trail_pct"] == 0.0


def test_zero_zone_percentages_are_honored():
    import ai_entry_watch as ew
    """A 0 band below support means 'entry_low sits exactly on the shelf', not
    'use the 0.25% default'."""
    db = ew.build_double_bottom_zone_structure(
        10.0, {"ai_watch_db_below_pct": 0}, last_price=10.2)
    assert db["entry_low"] == 10.0

    offset = ew.build_offset_zone_structure(
        10.0, {"ai_watch_zone_offset_pct": 0})
    assert offset["entry_high"] == 10.0     # zone top at the print, no offset


# ── the offset-zone fallback is a different strategy ────────────────────────

def _armable_record(structure):
    return {
        "symbol": "AAA", "status": "watching", "structure": structure,
        "indicator": {
            "cm_ok": True, "pctr_ok": True, "cm_rsi_rising": True,
            "sell_signal": False, "proximity_pct": 100.0,
            "pctr": -5.0, "pctr_rising": True, "pctr_falling": False,
        },
    }


def _db_cfg(**over):
    cfg = {
        "ai_watch_zone_mode": "double_bottom",
        "ai_min_reward_risk": 0.5,
        "ai_watch_exhaustion_rules": False,
    }
    cfg.update(over)
    return cfg


def test_offset_fallback_zone_is_refused_in_double_bottom_mode():
    """No shelf found -> a percentage band with a 5% stop. That is not the
    trade the zone mode asks for, and it was indistinguishable downstream."""
    import ai_entry_watch as ew

    offset = ew.build_offset_zone_structure(10.0, {})
    offset.setdefault("zone_kind", "offset")
    ask = (offset["entry_low"] + offset["entry_high"]) / 2      # inside the zone

    ok, why = ew.should_arm_buy(
        _armable_record(offset), ask=ask, bid=ask - 0.01, cfg=_db_cfg())
    assert (ok, why) == (False, "offset_zone")
    assert ew.format_blocker("offset_zone") == "no shelf"


def test_double_bottom_zone_still_arms():
    import ai_entry_watch as ew

    db = ew.build_double_bottom_zone_structure(10.0, {}, last_price=10.2)
    ask = (db["entry_low"] + db["entry_high"]) / 2

    ok, why = ew.should_arm_buy(
        _armable_record(db), ask=ask, bid=ask - 0.01, cfg=_db_cfg())
    assert ok and why.startswith("zone")


def test_offset_fallback_can_be_re_enabled_by_config():
    import ai_entry_watch as ew

    offset = ew.build_offset_zone_structure(10.0, {})
    offset.setdefault("zone_kind", "offset")
    ask = (offset["entry_low"] + offset["entry_high"]) / 2

    ok, why = ew.should_arm_buy(
        _armable_record(offset), ask=ask, bid=ask - 0.01,
        cfg=_db_cfg(ai_watch_require_db_zone=False))
    assert ok and why.startswith("zone")


# ── risk per share must be a real risk unit ─────────────────────────────────

def test_a_fill_at_the_bottom_of_the_band_is_too_tight_to_size():
    """The double-bottom band spans 6.9x of risk-per-share. At the tight end a
    1% risk implies ~400% of equity, so the notional cap becomes the sizer and
    the trade's real risk is a fraction of what it is booked as."""
    import ai_entry_watch as ew

    zone = ew.build_double_bottom_zone_structure(
        10.0, {}, low_a=10.0, low_b=10.0, last_price=10.5)
    rec = _armable_record(zone)
    cfg = _db_cfg(ai_watch_min_stop_pct=0.5)

    # bottom of the band: 9.975 vs a 9.95 stop = 0.25% of price
    assert ew.should_arm_buy(rec, ask=zone["entry_low"], bid=None, cfg=cfg) == (
        False, "stop_too_tight")
    # top of the band: 10.125 vs 9.95 = 1.73% — a real risk unit
    ok_hi, why_hi = ew.should_arm_buy(
        rec, ask=zone["entry_high"], bid=None, cfg=cfg)
    assert ok_hi and why_hi.startswith("zone")
    assert ew.format_blocker("stop_too_tight") == "stop too tight"


def test_cheap_pullback_band_overbought_is_refused():
    import ai_entry_watch as ew

    rec = _armable_record({
        "decision": "WAIT", "wait_kind": "wait_for_zone",
        "entry_low": 1.90, "entry_high": 2.10, "stop_price": 1.80,
        "target_1": 2.30, "reward_risk": 1.5,
        "zone_kind": "pullback_band", "synthetic": True,
    })
    rec["indicator"] = {
        # 85 exhaustion: overbought, under the 90 heat_max so cheap_ob_band
        # (not already_extended) is the refusal we are testing.
        "pctr": -15.0, "pctr_rising": True, "pctr_falling": False,
    }
    cfg = _db_cfg(
        ai_watch_exhaustion_rules=True,
        ai_edge_mode="exhaustion_scalp",
        ai_watch_cheap_price=5.0,
        ai_watch_min_stop_pct=0,
        ai_min_reward_risk=0.5,
    )
    ok, why = ew.should_arm_buy(rec, ask=2.00, bid=1.99, cfg=cfg)
    assert (ok, why) == (False, "cheap_ob_band")
    rec["structure"]["zone_kind"] = "double_bottom"
    ok2, why2 = ew.should_arm_buy(rec, ask=2.00, bid=1.99, cfg=cfg)
    assert ok2 and why2.startswith("zone")
    rec["structure"]["zone_kind"] = "pullback_band"
    rec["source"] = "bb_live"
    ok3, why3 = ew.should_arm_buy(rec, ask=2.00, bid=1.99, cfg=cfg)
    assert ok3 and why3 == "zone_overbought_hot"


def test_hot_overbought_arms_in_and_below_zone_not_above():
    """Trending/momentum already in OB: fill the dip, do not chase the high."""
    import ai_entry_watch as ew

    rec = _armable_record({
        "decision": "WAIT", "wait_kind": "wait_for_zone",
        "entry_low": 11.184, "entry_high": 11.229, "stop_price": 10.646,
        "target_1": 11.55, "reward_risk": 0.6,
        "zone_kind": "pullback_band", "synthetic": True,
    })
    rec["source"] = "momentum"
    rec["indicator"] = {
        "pctr": -3.72, "pctr_rising": True, "pctr_falling": False,
    }
    cfg = _db_cfg(
        ai_watch_exhaustion_rules=True,
        ai_watch_ob_allow_hot=True,
        ai_watch_arm_below_zone=True,
        ai_watch_arm_below_zone_max_r=1.0,
        ai_watch_min_stop_pct=0,
        ai_min_reward_risk=0.5,
        ai_watch_cheap_price=5.0,
    )
    # In the band.
    ok, why = ew.should_arm_buy(rec, ask=11.20, bid=11.19, cfg=cfg)
    assert ok and why == "zone_overbought_hot"
    # Below the band, still above the stop (armable dip).
    ok, why = ew.should_arm_buy(rec, ask=10.90, bid=10.89, cfg=cfg)
    assert ok and why == "zone_overbought_hot"
    # Above the band — NMAX 11.32 vs 11.229 — still refuse.
    ok, why = ew.should_arm_buy(rec, ask=11.32, bid=11.31, cfg=cfg)
    assert (ok, why) == (False, "above_zone")


def test_in_zone_ignore_fade_is_temporary_and_not_above_zone():
    import ai_entry_watch as ew

    rec = _armable_record({
        "decision": "WAIT", "wait_kind": "wait_for_zone",
        "entry_low": 6.44, "entry_high": 6.54, "stop_price": 6.20,
        "target_1": 6.80, "reward_risk": 0.6,
        "zone_kind": "pullback_band", "synthetic": True,
    })
    rec["source"] = "trending"
    rec["indicator"] = {
        "pctr": -50.0, "pctr_rising": False, "pctr_falling": True,
    }
    cfg = _db_cfg(
        ai_watch_exhaustion_rules=True,
        ai_watch_in_zone_ignore_fade=True,
        ai_watch_min_stop_pct=0,
        ai_min_reward_risk=0.5,
        ai_watch_cheap_price=0,
    )
    ok, why = ew.should_arm_buy(rec, ask=6.50, bid=6.49, cfg=cfg)
    assert ok and why == "zone_in_zone_fade_ok"
    ok, why = ew.should_arm_buy(rec, ask=6.70, bid=6.69, cfg=cfg)
    assert ok is False
    assert why in ("above_zone", "not_rising_cooling")


def test_min_stop_pct_of_zero_disables_the_check():
    import ai_entry_watch as ew

    zone = ew.build_double_bottom_zone_structure(
        10.0, {}, low_a=10.0, low_b=10.0, last_price=10.5)
    ok, why = ew.should_arm_buy(
        _armable_record(zone), ask=zone["entry_low"], bid=None,
        cfg=_db_cfg(ai_watch_min_stop_pct=0))
    assert ok and why.startswith("zone")


def test_stop_of_reads_the_structure_stop():
    import ai_entry_watch as ew

    assert ew._stop_of({"structure": {"stop_price": 9.95}}) == 9.95
    assert ew._stop_of({"structure": {"stop_price": 0}}) is None
    assert ew._stop_of({"structure": {}}) is None
    assert ew._stop_of({}) is None
    assert ew._stop_of(None) is None


def _synthetic_ohlc(n: int = 40, *, base: float = 30.0) -> list[tuple[float, float, float]]:
    """Rising closes so live %R is near the highs (heating / overbought)."""
    rows = []
    for i in range(n):
        c = base + i * 0.05
        rows.append((c + 0.02, c - 0.02, c))
    return rows


def test_price_in_or_below_zone():
    import ai_entry_watch as ew

    rec = {
        "structure": {
            "entry_low": 10.0, "entry_high": 11.0,
            "stop_price": 9.0, "target_1": 12.0, "reward_risk": 1.5,
        },
    }
    assert ew._price_in_or_below_zone(rec, 10.5) is True
    assert ew._price_in_or_below_zone(rec, 9.5) is True   # through the floor
    assert ew._price_in_or_below_zone(rec, 12.0) is False  # above
    assert ew._price_in_or_below_zone({"structure": None}, 10.0) is False


def test_ask_triggers_zone_includes_armable_below_dip():
    """In or below the band is a buy. The planned stop is not a veto."""
    import ai_entry_watch as ew

    lo, hi, stop = 10.0, 11.0, 9.0
    assert ew.ask_triggers_zone(10.5, lo, hi, stop=stop, max_below_r=0.5) is True
    assert ew.ask_triggers_zone(9.6, lo, hi, stop=stop, max_below_r=0.5) is True
    assert ew.ask_triggers_zone(9.5, lo, hi, stop=stop, max_below_r=0.5) is True
    # Through the planned stop — still a buy (IPWR $5.10 vs stop $5.22).
    assert ew.ask_triggers_zone(9.4, lo, hi, stop=stop, max_below_r=0.5) is True
    assert ew.ask_triggers_zone(8.0, lo, hi, stop=None, max_below_r=0.5) is True
    assert ew.ask_triggers_zone(12.0, lo, hi, stop=stop, max_below_r=0.5) is False


def test_derive_blocker_armable_below_is_in_zone():
    import ai_entry_watch as ew

    rec = {
        "status": "watching",
        "last_ask": 9.6,
        "block_code": "below_zone",
        "block_reason": "below zone",
        "structure": {
            "entry_low": 10.0, "entry_high": 11.0,
            "stop_price": 9.0, "target_1": 12.0, "reward_risk": 1.5,
            "wait_kind": "wait_for_zone", "decision": "WAIT",
        },
    }
    code, label = ew.derive_blocker(rec, max_below_r=0.5, arm_below=True)
    assert code == "in_zone"
    assert label == "in zone"


def test_stream_prefilter_not_far_inside_armable_dip(monkeypatch):
    import ai_entry_watch as ew

    rec = {
        "symbol": "DIP",
        "structure": {
            "entry_low": 10.0, "entry_high": 11.0,
            "stop_price": 9.0, "target_1": 12.0, "reward_risk": 1.5,
        },
    }
    # 9.6 is 4% under the floor — outside the 1% stream margin, but inside
    # the 0.5R armable window. Must still fetch a real ask.
    monkeypatch.setattr(ew, "stream_quote", lambda _s: (9.6, 0.5))
    cfg = {
        "ai_watch_stream_enabled": True,
        "ai_watch_stream_max_age_sec": 10.0,
        "ai_watch_stream_skip_margin_pct": 1.0,
        "ai_watch_arm_below_zone": True,
        "ai_watch_arm_below_zone_max_r": 0.5,
    }
    far, px = ew.stream_says_far_from_zone(rec, cfg)
    assert far is False
    assert abs(px - 9.6) < 1e-9
    # Through the planned stop — still not far; stop is not live yet.
    monkeypatch.setattr(ew, "stream_quote", lambda _s: (9.3, 0.5))
    far2, _ = ew.stream_says_far_from_zone(rec, cfg)
    assert far2 is False


def test_release_orphaned_submits_returns_to_watching(tmp_path, monkeypatch):
    import alpaca_trader as at
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    # HOLD still has a live position; BCRX is flat — only BCRX should free.
    monkeypatch.setattr(at, "get_positions_detail", lambda: {
        "HOLD": {"qty": 10, "avg_entry_price": 1.5},
    })
    monkeypatch.setattr(at, "get_open_orders", lambda: [])

    ew.save_watch({
        "BCRX": {
            "symbol": "BCRX", "status": "submitted",
            "block_code": "submitted", "block_reason": "sent",
            "structure": {"entry_low": 10.0, "entry_high": 11.0},
        },
        "HOLD": {
            "symbol": "HOLD", "status": "submitted",
            "structure": {"entry_low": 1.0, "entry_high": 2.0},
        },
    })

    freed = ew.release_orphaned_submits()
    assert freed == ["BCRX"]
    state = ew.load_watch()
    assert state["BCRX"]["status"] == "watching"
    assert "block_code" not in state["BCRX"]
    assert state["HOLD"]["status"] == "submitted"

    # Operator force releases even held names from the watch queue label.
    freed2 = ew.release_orphaned_submits(["HOLD"], force=True)
    assert freed2 == ["HOLD"]
    assert ew.load_watch()["HOLD"]["status"] == "watching"


def test_sync_preserves_poller_indicator_and_block(tmp_path, monkeypatch):
    """2s desk rebuild must not wipe live %R the poller just stamped."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(ew, "push_candidates_to_engine", lambda *a, **k: {})
    monkeypatch.setattr(
        ew, "desk_candidate_rows",
        lambda cfg: [{
            "symbol": "SMCI", "source": "trending", "score": 12.0,
            "reason": "test", "price": 31.5, "rvol": 1.6,
        }],
    )
    # No live bar fetch in this unit test.
    monkeypatch.setattr(ew, "ensure_live_exhaustion", lambda *a, **k: False)
    monkeypatch.setattr(ew, "ensure_offset_zone_if_needed", lambda *a, **k: None)

    prev_ind = {
        "pctr": -12.0, "pctr_rising": True, "pctr_falling": False,
        "pctr_src": "live", "pctr_ts": 1e12,
    }
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "source": "trending",
            "score": 11.0, "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 31.0, "entry_high": 32.0,
                "stop_price": 30.0, "target_1": 35.0, "reward_risk": 1.5,
                "zone_kind": "double_bottom",
            },
            "structure_ts": 1e12,
            "last_ask": 31.5,
            "last_poll_ts": 1e12,
            "indicator": prev_ind,
            "block_code": "in_zone",
            "block_reason": "in zone",
            "block_ts": 1e12,
        },
    })
    out = ew.sync_watch_from_source_panels(cfg={}, now=1e12 + 5)
    rec = out["SMCI"]
    assert rec["indicator"]["pctr"] == -12.0
    assert rec["indicator"]["pctr_src"] == "live"
    assert rec["block_code"] == "in_zone"
    assert rec["structure"]["entry_low"] == 31.0


def test_ensure_symbol_ohlc_fetches_when_cache_cold(monkeypatch):
    """Cold cache after restart must fetch — not wait for a zone rebuild."""
    import ai_entry_watch as ew

    now = 1_700_000_000.0
    filled = _synthetic_ohlc()
    calls = {"n": 0}

    def fake_fetch(symbol, cfg, t):
        calls["n"] += 1
        with ew._ohlc_cache_lock:
            ew._ohlc_cache[str(symbol).upper()] = (t, list(filled))
            # Tight 1-min stamps so the span gate does not refuse the window.
            ew._ohlc_ts_cache[str(symbol).upper()] = (
                t, [t - 60.0 * (len(filled) - 1 - i) for i in range(len(filled))])
        return [r[1] for r in filled]

    monkeypatch.setattr(ew, "_fetch_symbol_lows", fake_fetch)
    with ew._ohlc_cache_lock:
        ew._ohlc_cache.pop("SMCI", None)
        ew._ohlc_ts_cache.pop("SMCI", None)

    rows = ew.ensure_symbol_ohlc("SMCI", {}, now)
    assert calls["n"] == 1
    assert len(rows) >= 23
    # Second call hits cache — no extra fetch while fresh.
    rows2 = ew.ensure_symbol_ohlc("SMCI", {}, now + 1.0)
    assert calls["n"] == 1
    assert rows2 == rows


def test_ensure_live_exhaustion_stamps_pctr_for_the_buy_gate(monkeypatch):
    """Buy path used to leave every name at exhaustion_state=unknown."""
    import ai_entry_watch as ew

    now = 1_700_000_000.0
    filled = _synthetic_ohlc()

    def fake_fetch(symbol, cfg, t):
        with ew._ohlc_cache_lock:
            ew._ohlc_cache[str(symbol).upper()] = (t, list(filled))
            ew._ohlc_ts_cache[str(symbol).upper()] = (
                t, [t - 60.0 * (len(filled) - 1 - i) for i in range(len(filled))])
        return [r[1] for r in filled]

    monkeypatch.setattr(ew, "_fetch_symbol_lows", fake_fetch)
    with ew._ohlc_cache_lock:
        ew._ohlc_cache.pop("SMCI", None)
        ew._ohlc_ts_cache.pop("SMCI", None)

    rec = {"symbol": "SMCI"}
    cfg = {
        "ai_watch_exhaustion_rules": True,
        "ai_watch_exhaustion_live": True,
        "ai_watch_require_exhaustion_data": True,
        "rte_fast_length": 21,
        "ai_watch_exhaustion_max_window_mult": 10.0,
    }
    px = filled[-1][2]
    assert ew.ensure_live_exhaustion(rec, px, cfg, now) is True
    assert rec["indicator"]["pctr"] is not None
    assert rec["indicator"]["pctr_src"] == "live"
    state = ew.exhaustion_state(rec, cfg)
    assert state in ("overbought", "heating", "cooling", "flat")
    ok, why = ew.exhaustion_allows_buy(rec, cfg)
    # Rising ≥ 50 or refuse heating_too_low / not_rising_*; never no %R
    # once live %R is stamped.
    assert why != "no_exhaustion_data"
    assert ok is True and why in ("overbought", "heating") or (
        not ok and why in (
            "heating_too_low", "already_extended",
            "not_rising_cooling", "not_rising_flat",
            "not_rising_overbought", "not_rising_heating",
        )
    )


def test_exhaustion_allows_buy_rising_past_heat_min():
    """Arm on rising EXH ≥ 50; refuse fading OB and sub-50 heat."""
    import ai_entry_watch as ew

    cfg = {
        "ai_edge_mode": "exhaustion_scalp",
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": True,
        "rte_threshold": 20,
        "ai_watch_exhaustion_heat_min_pct": 50.0,
    }
    # Rising through 70% — the new admission (was refused as not_overbought).
    heat = {
        "symbol": "BBB",
        "indicator": {
            "pctr": -30.0, "pctr_rising": True, "pctr_falling": False,
        },
    }
    ok, why = ew.exhaustion_allows_buy(heat, cfg)
    assert ok is True and why == "heating"

    # Rising overbought still arms if not yet in the 90–100 fade bucket.
    # pctr -15 → exhaustion 85 (overbought, below heat_max 90).
    ob_up = {
        "symbol": "AAA",
        "indicator": {
            "pctr": -15.0, "pctr_rising": True, "pctr_falling": False,
        },
    }
    ok, why = ew.exhaustion_allows_buy(ob_up, cfg)
    assert ok is True and why == "overbought"

    # Already 90+ — 08-13 forward print was −1.1% / 30m. Do not chase.
    too_hot = {
        "symbol": "HOT",
        "indicator": {
            "pctr": -5.0, "pctr_rising": True, "pctr_falling": False,
        },
    }
    ok, why = ew.exhaustion_allows_buy(too_hot, cfg)
    assert ok is False and why == "already_extended"

    # Desk-hot (trending / momentum) already in OB may still arm.
    hot_tr = dict(too_hot, source="trending")
    ok, why = ew.exhaustion_allows_buy(hot_tr, cfg)
    assert ok is True and why == "overbought_hot"
    # Falling OB — FGI 08-14. Hot source does not waive a rollover.
    hot_fade = {
        "symbol": "FGI",
        "source": "momentum",
        "indicator": {
            "pctr": -0.0, "pctr_rising": False, "pctr_falling": True,
        },
    }
    ok, why = ew.exhaustion_allows_buy(hot_fade, cfg)
    assert ok is False and why == "not_rising_overbought"
    hot_bro = dict(too_hot, source="bb_live")
    ok, why = ew.exhaustion_allows_buy(hot_bro, cfg)
    assert ok is True and why == "overbought_hot"

    # Overbought but fading — do not buy the roll-over.
    cool = {
        "symbol": "CCC",
        "indicator": {
            "pctr": -15.0, "pctr_rising": False, "pctr_falling": True,
        },
    }
    ok, why = ew.exhaustion_allows_buy(cool, cfg)
    assert ok is False and why == "not_rising_overbought"

    heat_low = {
        "symbol": "LOW",
        "indicator": {
            "pctr": -60.0, "pctr_rising": True, "pctr_falling": False,
        },
    }
    ok, why = ew.exhaustion_allows_buy(heat_low, cfg)
    assert ok is False and why == "heating_too_low"


def test_continuation_arms_heating_and_disables_left_overbought_exit():
    """Option A: heating ≥ heat_min arms; left_overbought exit is off."""
    import ai_entry_watch as ew

    cfg = {
        "ai_edge_mode": "continuation",
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": True,
        "rte_threshold": 20,
        "ai_watch_exhaustion_heat_min_pct": 50.0,
    }
    heat = {
        "symbol": "BBB",
        "indicator": {
            "pctr": -30.0, "pctr_rising": True, "pctr_falling": False,
        },
        "exh_was_overbought": True,
    }
    ok, why = ew.exhaustion_allows_buy(heat, cfg)
    assert ok is True and why == "heating"

    heat_low = {
        "symbol": "LOW",
        "indicator": {
            "pctr": -60.0, "pctr_rising": True, "pctr_falling": False,
        },
    }
    ok, why = ew.exhaustion_allows_buy(heat_low, cfg)
    assert ok is False and why == "heating_too_low"

    # Was overbought, now below band — continuation must NOT sell on that alone.
    left = {
        "symbol": "OUT",
        "indicator": {
            "pctr": -40.0, "pctr_rising": False, "pctr_falling": True,
        },
        "exh_was_overbought": True,
    }
    hit, reason = ew.exhaustion_exit_now(left, cfg)
    assert hit is False and reason == "left_overbought_off"

    scalp_cfg = dict(cfg, ai_edge_mode="exhaustion_scalp")
    hit2, reason2 = ew.exhaustion_exit_now(left, scalp_cfg)
    assert hit2 is True and reason2 == "left_overbought"


def test_arm_refuses_no_exhaustion_when_require_data():
    import ai_entry_watch as ew

    rec = {
        "symbol": "SMCI",
        "status": "watching",
        "structure": {
            "decision": "WAIT",
            "wait_kind": "wait_for_zone",
            "entry_low": 31.0,
            "entry_high": 32.0,
            "stop_price": 30.0,
            "target_1": 35.0,
            "reward_risk": 1.5,
            "zone_kind": "double_bottom",
            "synthetic": False,
        },
    }
    cfg = {
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": True,
        "ai_watch_arm_require_indicators": False,
        "ai_min_reward_risk": 0,
        "ai_watch_min_stop_pct": 0,
        "ai_watch_require_db_zone": False,
    }
    ok, why = ew.should_arm_buy(rec, ask=31.5, bid=31.4, cfg=cfg)
    assert not ok and why == "no_exhaustion_data"
    assert ew.format_blocker("no_exhaustion_data") == "no %R"


def _prime_ohlc(ew, symbol, rows, now, *, step_sec=60.0):
    stamps = [now - step_sec * (len(rows) - 1 - i) for i in range(len(rows))]
    with ew._ohlc_cache_lock:
        ew._ohlc_cache[symbol] = (now, list(rows))
        ew._ohlc_ts_cache[symbol] = (now, stamps)


def test_live_exhaustion_refuses_sparse_clock_window():
    """21 prints stretched over an hour is not a 1-minute %R(21)."""
    import ai_entry_watch as ew

    now = 1_700_000_000.0
    rows = _synthetic_ohlc(40)
    _prime_ohlc(ew, "OMER", rows, now, step_sec=600.0)  # 10 min between prints
    cfg = {"rte_fast_length": 21, "ai_watch_db_bar_seconds": 60.0}
    px = rows[-1][2]
    assert ew.live_exhaustion("OMER", px, cfg, now) is None
    rec = {
        "symbol": "OMER",
        "indicator": {"pctr": -2.0, "pctr_src": "live", "pctr_ok": True},
    }
    assert ew.apply_live_exhaustion(rec, px, cfg, now) is False
    assert rec["indicator"]["pctr"] is None
    assert rec["indicator"]["pctr_src"] == "sparse_window"


def test_live_exhaustion_uses_dense_1m_clock_window():
    import ai_entry_watch as ew

    now = 1_700_000_000.0
    rows = _synthetic_ohlc(40)
    _prime_ohlc(ew, "SMCI", rows, now, step_sec=60.0)
    cfg = {"rte_fast_length": 21, "ai_watch_db_bar_seconds": 60.0}
    px = rows[-1][2] + 0.01
    got = ew.live_exhaustion("SMCI", px, cfg, now)
    assert got is not None
    pctr, ex, _up, _dn = got
    assert -100.5 <= pctr <= 0.5
    rec = {"symbol": "SMCI"}
    assert ew.apply_live_exhaustion(rec, px, cfg, now) is True
    assert rec["indicator"]["pctr_src"] == "live"
    assert rec["indicator"]["pctr_bars"] >= 21
    assert rec["indicator"]["pctr_window_sec"] <= 26 * 60 + 1
    wire = ew._exhaustion_wire_fields(rec)
    assert wire["pctr"] is not None
    assert wire["exh_window_min"] is not None
    assert wire["exh_window_min"] <= 26.1


def test_live_exhaustion_accepts_exact_21_one_minute_bars():
    """A complete 21×1m window is enough — do not demand 23 bars in 22 min."""
    import ai_entry_watch as ew

    now = 1_700_000_000.0
    rows = _synthetic_ohlc(21)
    _prime_ohlc(ew, "CRMD", rows, now, step_sec=60.0)
    cfg = {
        "rte_fast_length": 21,
        "ai_watch_db_bar_seconds": 60.0,
        "ai_watch_exhaustion_clock_slack": 1.25,
    }
    px = rows[-1][2]
    got = ew.live_exhaustion("CRMD", px, cfg, now)
    assert got is not None


def test_decision_price_prefers_fresh_stream(monkeypatch):
    import ai_entry_watch as ew
    import ai_trading as gt

    monkeypatch.setattr(ew, "live_print", lambda s: (10.28, 1.5))
    monkeypatch.setattr(gt, "_latest_ask", lambda s: 11.69)
    px, src, age = ew.decision_price("FGI", {"ai_watch_decision_max_age_sec": 8.0})
    assert px == 10.28 and src == "stream" and age == 1.5


def test_decision_price_falls_back_to_rest_when_tape_unaged(monkeypatch):
    import ai_entry_watch as ew
    import ai_trading as gt

    monkeypatch.setattr(ew, "live_print", lambda s: (10.28, None))
    monkeypatch.setattr(gt, "_latest_ask", lambda s: 11.69)
    px, src, age = ew.decision_price("FGI", {"ai_watch_decision_max_age_sec": 8.0})
    assert px == 11.69 and src == "rest"


def test_decision_price_stale_tape_when_no_rest(monkeypatch):
    import ai_entry_watch as ew
    import ai_trading as gt

    monkeypatch.setattr(ew, "live_print", lambda s: (10.28, 40.0))
    monkeypatch.setattr(gt, "_latest_ask", lambda s: None)
    px, src, age = ew.decision_price("FGI", {"ai_watch_decision_max_age_sec": 8.0})
    assert px == 10.28 and src == "stale_tape" and age == 40.0


def test_live_exhaustion_range_fallback_for_thin_tape():
    """~10 prints in 25 minutes still get a range %R, not a blank EXH."""
    import ai_entry_watch as ew

    now = 1_700_000_000.0
    rows = _synthetic_ohlc(10)
    _prime_ohlc(ew, "MOBX", rows, now, step_sec=150.0)
    cfg = {
        "rte_fast_length": 21,
        "ai_watch_db_bar_seconds": 60.0,
        "ai_watch_exhaustion_clock_slack": 1.25,
        "ai_watch_exhaustion_min_range_bars": 6,
    }
    px = rows[-1][2]
    got = ew.live_exhaustion("MOBX", px, cfg, now)
    assert got is not None
    rec = {"symbol": "MOBX"}
    assert ew.apply_live_exhaustion(rec, px, cfg, now) is True
    assert rec["indicator"]["pctr_src"] == "clock_range"
    assert rec["indicator"]["pctr"] is not None
