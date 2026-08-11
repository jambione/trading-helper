"""Guards for the three defects the 2026-08-11 edge-mode analysis surfaced.

1. Adoption fabricated ``entry_exhaustion_state: "overbought"`` for a position
   whose %R was never read, which corrupts any slice by entry state.
2. Outcome rows did not say which desk opened them, so gated (ai_entry_watch)
   and ungated (ai_suggest) entries were indistinguishable in one ledger.
3. Adoption sets ``exh_was_overbought`` so left_overbought can flatten an
   orphan immediately. Turning that exit off globally — which the continuation
   and hybrid edge modes do — left orphans riding the broker stop alone.
"""
import ai_entry_watch as ew
import ai_positions as cp


def test_adoption_does_not_claim_an_unread_exhaustion_state(monkeypatch):
    """The label must not assert a %R reading that was never taken."""
    monkeypatch.setattr(cp, "_latest_entry_ok_event", lambda s: None)
    monkeypatch.setattr(cp, "_resting_stop_price", lambda s: 4.50)
    # Adoption logs; keep the fixture out of the live events.jsonl.
    monkeypatch.setattr(cp, "log_event", lambda *a, **k: {})

    state: dict = {}
    cp._adopt_unmanaged(
        ["MLTX"],
        {"MLTX": {"qty": 10, "avg_entry_price": 5.00}},
        state,
        1_000.0,
    )

    pos = state["MLTX"]
    assert pos["entry_exhaustion_state"] == "adopted"
    assert pos.get("entry_exhaustion") is None
    assert pos["entry_path"] == "adopted"
    # The latch still arms the exit — that part was doing real work.
    assert pos["exh_was_overbought"] is True


def test_adopted_orphans_keep_left_overbought_when_edge_mode_disables_it():
    """Hybrid/continuation turn the exit off globally; orphans must keep it."""
    hybrid = {
        "ai_edge_mode": "exhaustion_scalp",
        "ai_exit_left_overbought": False,
        "ai_watch_exhaustion_rules": True,
        "rte_threshold": 20,
    }
    # Globally the exit is off.
    assert ew.left_overbought_exit_enabled(hybrid) is False

    # evaluate_positions re-enables it for adopted rows by overriding the flag.
    adopted_cfg = {**hybrid, "ai_exit_left_overbought": True}
    assert ew.left_overbought_exit_enabled(adopted_cfg) is True

    # A name that was overbought and has faded out of the band exits under the
    # adopted override, and does not under the plain hybrid config.
    faded = {
        "symbol": "ORPH",
        "indicator": {"pctr": -40.0},
        "exh_was_overbought": True,
    }
    hit_adopted, why_adopted = ew.exhaustion_exit_now(dict(faded), adopted_cfg)
    hit_plain, why_plain = ew.exhaustion_exit_now(dict(faded), hybrid)
    assert hit_adopted is True, why_adopted
    assert hit_plain is False
    assert why_plain == "left_overbought_off"


def test_entry_path_is_stamped_by_every_entry_path():
    """watch / suggest / adopted must each name themselves on the row."""
    import inspect

    watch = inspect.getsource(ew)
    assert 'place_decision["entry_path"] = "watch"' in watch

    import ai_suggest
    suggest = inspect.getsource(ai_suggest)
    assert 'decision["entry_path"] = "suggest"' in suggest

    positions = inspect.getsource(cp)
    assert '"entry_path": "adopted"' in positions
    # Carried from the decision onto the position, and from the position onto
    # the outcome row — both hops are needed for the ledger to see it.
    assert '"entry_path": decision.get("entry_path") or "unknown"' in positions
    assert '"entry_path": pos.get("entry_path") or "unknown"' in positions
