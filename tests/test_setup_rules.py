"""The operator's setup, pinned so live and lab cannot drift apart.

This conjunction fires on 25 of 493 logged name-days. At that rate a
threshold quietly loosened by one point in one of the two call sites
would change which trades exist without changing any number anyone
reads — so the rule lives in one module and both sides import it.

The rule that carries the most weight here is that **unknown never
passes**. A missing share count or a missing RVOL is not evidence; the
desk already has a live gate that reads `rv is not None and rv < floor`
and therefore admits any name whose reading failed to arrive.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sr = pytest.importorskip("setup_rules")


def _ok(**over):
    """A qualifying observation; override one field to break one leg."""
    base = dict(pct_change=25.0, rvol=8.0, price=6.0,
                shares_out_m=4.0, news_n_24h=2)
    base.update(over)
    return sr.evaluate(**base)


def test_the_full_conjunction_passes():
    r = _ok()
    assert r["ok"] is True
    assert r["n_legs"] == 5


def test_every_leg_is_load_bearing():
    """Break one at a time; each must sink the whole thing."""
    for field, bad in (("pct_change", 4.0), ("rvol", 2.0), ("price", 45.0),
                       ("shares_out_m", 500.0), ("news_n_24h", 0)):
        r = _ok(**{field: bad})
        assert r["ok"] is False, f"{field}={bad} should fail the setup"
        assert r["n_legs"] == 4


# ---------------------------------------------------------- unknown is not ok

def test_an_unknown_share_count_never_passes():
    """The whole point of the float leg is that we KNOW the supply.

    None means nobody looked it up. Treating that as small float would
    admit exactly the names we know least about.
    """
    r = _ok(shares_out_m=None)
    assert r["float"] is False
    assert r["ok"] is False


def test_a_missing_rvol_never_passes():
    """The live floor reads `rv is not None and rv < floor`, so a missing
    reading passes it. 19 fills had no RVOL at all. Not here."""
    assert _ok(rvol=None)["rvol"] is False


def test_a_garbage_rvol_never_passes():
    """3.94% of logged RVOLs exceed 100; the max is 81,820."""
    assert _ok(rvol=3144.09)["rvol"] is False
    assert _ok(rvol=81820.37)["rvol"] is False


def test_unparseable_inputs_fail_rather_than_raise():
    r = sr.evaluate(pct_change="lots", rvol="high", price="cheap",
                    shares_out_m="small", news_n_24h="some")
    assert r["ok"] is False
    assert r["n_legs"] == 0


# ---------------------------------------------------------- boundaries

def test_the_price_band_is_inclusive_at_both_ends():
    assert _ok(price=2.0)["price"] is True
    assert _ok(price=20.0)["price"] is True
    assert _ok(price=1.99)["price"] is False
    assert _ok(price=20.01)["price"] is False


def test_ten_percent_is_a_floor_not_a_target():
    assert _ok(pct_change=10.0)["up"] is True
    assert _ok(pct_change=9.99)["up"] is False
    assert _ok(pct_change=97.1)["up"] is True     # PCLA 2026-08-20


def test_the_share_cap_is_strict():
    assert _ok(shares_out_m=9.99)["float"] is True
    assert _ok(shares_out_m=10.0)["float"] is False


def test_the_share_cap_is_a_live_parameter_not_a_constant():
    """A flag that silently does nothing is this project's signature bug.

    universe_screen takes --max-shares-m. The first version of evaluate()
    read the module constant instead, so the flag was accepted, ignored,
    and returned the identical seven names at every cap — which is exactly
    how ai_watch_min_pct_change read 50 for weeks while the path that
    admitted most names never consulted it.
    """
    assert sr.evaluate(pct_change=25.0, rvol=8.0, price=6.0,
                       shares_out_m=25.0, news_n_24h=2,
                       max_shares_out_m=30.0)["ok"] is True
    assert sr.evaluate(pct_change=25.0, rvol=8.0, price=6.0,
                       shares_out_m=25.0, news_n_24h=2,
                       max_shares_out_m=10.0)["ok"] is False


def test_a_catalyst_counts_by_headline_or_by_recency():
    assert _ok(news_n_24h=0, news_mins_since=30.0)["news"] is True
    assert _ok(news_n_24h=0, news_mins_since=None)["news"] is False
    # A headline older than the window is not today's catalyst.
    assert _ok(news_n_24h=0, news_mins_since=48 * 60.0)["news"] is False


def test_real_setup_names_from_the_tape():
    """2026-08 name-days that met four legs; float decides the fifth.

    CDTG 3.02M and PCLA 9.61M pass; JUNS 56.52M does not. Recorded so a
    change to the share cap shows up as a named example flipping.
    """
    assert _ok(pct_change=60.8, rvol=51.84, price=5.52,
               shares_out_m=3.02)["ok"] is True         # CDTG
    assert _ok(pct_change=97.1, rvol=7.41, price=12.86,
               shares_out_m=9.61)["ok"] is True         # PCLA
    assert _ok(pct_change=87.1, rvol=35.42, price=8.98,
               shares_out_m=56.52)["ok"] is False       # JUNS


# ---------------------------------------------------------- stage 2

def test_both_lines_rising_is_the_sweet_spot():
    s = sr.stage2(pctr_rising=True, pctr_slow_rising=True)
    assert s["pctr_both_rising"] is True
    assert s["pctr_diverging"] is False


def test_one_line_turning_is_the_exit_tell():
    s = sr.stage2(pctr_rising=True, pctr_slow_rising=False)
    assert s["pctr_both_rising"] is False
    assert s["pctr_diverging"] is True


def test_stage2_reports_unknown_when_a_line_is_missing():
    """31% of shadow rows carry pctr_slow. Absence is not 'not rising'."""
    s = sr.stage2(pctr_rising=True, pctr_slow_rising=None)
    assert s["pctr_both_rising"] is None
    assert s["pctr_diverging"] is None


# ------------------------------------------------- volume provenance on the row

def test_volume_fields_come_from_whichever_producer_supplied_the_row():
    """The first version read only "dollar_volume".

    That key exists on the seed dicts and on NO live row: dashboard rows
    nest rvol under "funnel", research rows carry vol_session and
    avg_vol_consolidated. It would have logged None on every poll of every
    session -- a dead column that looks like a working one.
    """
    import ai_entry_watch as ew
    research = ew._admission_fields(
        {"vol_session": 4_000_000.0, "avg_vol_consolidated": 250_000.0,
         "rvol": 16.0, "rvol_raw": 16.2, "price": 5.0, "pct_change": 40.0},
        {}, 0.0)
    assert research["admit_vol_session"] == 4_000_000.0
    assert research["admit_avg_vol"] == 250_000.0
    assert research["admit_rvol_raw"] == 16.2
    # dollar volume is derived when the producer does not supply it
    assert research["admit_dollar_volume"] == 20_000_000.0


def test_a_row_without_volume_reports_none_not_zero():
    import ai_entry_watch as ew
    f = ew._admission_fields({"price": 5.0, "pct_change": 40.0}, {}, 0.0)
    assert f["admit_vol_session"] is None
    assert f["admit_avg_vol"] is None
    assert f["admit_dollar_volume"] is None


def test_volume_falls_back_to_what_admission_saw():
    """The producer publishes rvol=None until its refresh resolves; a later
    poll must not erase what was true at admission."""
    import ai_entry_watch as ew
    prev = {"admit_vol_session": 9.0, "admit_avg_vol": 3.0}
    f = ew._admission_fields({"price": 5.0}, prev, 0.0)
    assert f["admit_vol_session"] == 9.0
    assert f["admit_avg_vol"] == 3.0
