"""One source of price truth: the desk. The dashboard renders it.

signal_engine holds its own Finnhub trade socket, aggregates it into
realtime bars, and publishes both the price and the age of the trade behind
it. The dashboard was running a SECOND Finnhub socket plus an Alpaca
fallback and doing its own merge — and the arm gate reads the dashboard's
result, not the engine's.

Measured 2026-08-26 over a full RTH session (12,025 rows):

    engine tape      p50  0.3s   p90  0.9s
    dashboard merge  p50 28.0s   p90 85.4s

which is why `stale quote` was the most common state on the book, and why
an 8s freshness gate admitted only 17.3% of rows.

The desk is added as a normal merge SOURCE rather than an override, so it
still has to win on trade recency. If the desk's print is genuinely older
than an Alpaca one, Alpaca should win — freshest_prices already decides
that correctly and this must not paper over it.
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

d = pytest.importorskip("dashboard")


# ── the merge rule the desk has to play by ───────────────────────────────

def test_the_freshest_trade_wins_regardless_of_source():
    now = 1_787_000_000.0
    desk = {"AAA": (10.00, now - 0.3, now - 0.3)}
    alp = {"AAA": (9.90, now - 30.0, now - 30.0)}
    got = d.freshest_prices(alp, {}, desk)
    assert got["AAA"][0] == 10.00, "0.3s desk print beats a 30s Alpaca print"


def test_a_stale_desk_print_does_not_win():
    """The point of merging on recency rather than on source. A desk price
    that is genuinely older must lose, or this becomes an override."""
    now = 1_787_000_000.0
    desk = {"AAA": (10.00, now - 45.0, now - 45.0)}
    alp = {"AAA": (9.90, now - 2.0, now - 2.0)}
    got = d.freshest_prices(alp, {}, desk)
    assert got["AAA"][0] == 9.90


def test_the_published_age_is_the_trades_own_age():
    """price_age_sec must describe the print, not when we read the file."""
    now = 1_787_000_000.0
    got = d.freshest_prices({}, {}, {"AAA": (10.0, now - 0.4, now - 0.4)})
    _px, _obs, trade_ts = got["AAA"]
    assert trade_ts == pytest.approx(now - 0.4)


# ── what the loop is allowed to take from the desk ───────────────────────

def _desk_rows(**over):
    # `price` and `bars_age_sec` are present and deliberately WRONG-looking:
    # a stale price wearing a sub-second age, which is what the loop used to
    # read. Every test below must ignore them in favour of the rt_ pair.
    base = {"price": 999.0, "bars_age_sec": 0.3,
            "rt_price": 10.0, "rt_price_age_sec": 0.3, "bars_src": "realtime"}
    base.update(over)
    return {"tickers": {"AAA": base}}


def _harvest(sig, universe={"AAA"}, now=1_787_000_000.0):
    """Mirror of the loop's desk-source filter, kept in step by the source
    pin below. Only realtime, priced, dateable rows may contribute."""
    out = {}
    for t, sp in (sig.get("tickers") or {}).items():
        if not isinstance(sp, dict) or t not in universe:
            continue
        if str(sp.get("bars_src") or "") != "realtime":
            continue
        px, age = sp.get("rt_price"), sp.get("rt_price_age_sec")
        if px is None or age is None:
            continue
        px, age = float(px), float(age)
        if px <= 0 or age < 0:
            continue
        tts = now - age
        if tts <= 0:
            continue
        out[t] = (px, tts, tts)
    return out


def test_a_realtime_row_contributes():
    assert "AAA" in _harvest(_desk_rows())


def test_the_price_taken_is_the_tapes_own_print():
    """The whole defect in one assertion.

    `price` is TickerState.last_price, which signal_engine._ingest_state
    adopts FROM THIS DASHBOARD whenever the Finnhub stream is quiet — most of
    premarket. Reading it back here closed a loop, and `bars_age_sec` (the
    tape's trade clock, cached at bar-eval time) then dated our own returned
    number as 0.3s old. Observed 2026-08-27 premarket: eight symbols frozen
    for over two minutes, every one publishing a sub-second age.
    """
    got = _harvest(_desk_rows())
    assert got["AAA"][0] == 10.0, "rt_price, not the fed-back `price`"


def test_the_rest_fallback_does_not_contribute():
    """bars_src flips per ticker mid-session and the fallback has no trade
    clock worth trusting here."""
    assert _harvest(_desk_rows(bars_src="alpaca")) == {}


def test_a_row_with_no_age_does_not_contribute():
    """A price the desk cannot date is exactly what this change exists to
    stop propagating."""
    assert _harvest(_desk_rows(rt_price_age_sec=None)) == {}


def test_a_row_with_no_price_does_not_contribute():
    assert _harvest(_desk_rows(rt_price=None)) == {}
    assert _harvest(_desk_rows(rt_price=0)) == {}


def test_a_row_carrying_only_the_old_pair_does_not_contribute():
    """A ticker that has never traded on the socket publishes rt_price=None
    while `price` and `bars_age_sec` are both still populated. That row must
    read as ABSENT, not as fresh."""
    assert _harvest(_desk_rows(rt_price=None, rt_price_age_sec=None)) == {}


def test_symbols_outside_the_quote_universe_are_ignored():
    assert _harvest(_desk_rows(), universe=set()) == {}


# ── pins ─────────────────────────────────────────────────────────────────

def test_the_loop_actually_passes_the_desk_into_the_merge():
    """Source-pinned: the price loop is threaded and network-bound, so a
    regression here is invisible until the book quietly goes stale again."""
    src = (_ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert "freshest_prices(cached_alpaca, fh_all, desk_prices)" in src
    i = src.index("desk_prices: dict = {}")
    # Bounded to the desk harvest itself. The Finnhub block below it has its
    # own legitimate .get("price"), and a slice that swallowed it would make
    # the loop guard below unfalsifiable.
    body = src[i:src.index("desk_prices = {}", i + 1)]
    assert '_load_signal_state()' in body, "the desk state is the source"
    assert '"realtime"' in body, "only the realtime pipe may contribute"
    assert 'get("rt_price")' in body, "the tape's own print"
    assert 'get("rt_price_age_sec")' in body, "and that print's own clock"
    # The loop guard. Re-reaching for either of these is the regression.
    assert 'get("price")' not in body, "`price` is fed back from this loop"
    assert 'get("bars_age_sec")' not in body, "bar-eval clock, not this price"


def test_the_engine_stamps_the_pair_at_write_time():
    """Source-pinned on the other end. bars_age_sec is cached on the
    TickerState at bar-eval time and republished verbatim, so it under-reports
    age by up to a full eval period; rt_price_age_sec must be computed in the
    writer. And it must come from last_trade(), which hands out the price and
    its timestamp under one lock as one event."""
    src = (_ROOT / "signal_engine.py").read_text(encoding="utf-8")
    i = src.index("def _write_signal_state")
    body = src[i:i + 3000]
    assert "rt_bars.last_trade(" in body, "one lock, one event"
    assert "rt_price_age_sec" in body
    assert "proximity_state()" in body, "still the base row"


def test_freshest_prices_still_accepts_three_sources():
    """Guards the signature this depends on — it is *sources, and a change
    to a fixed arity would silently drop the desk."""
    now = 1_787_000_000.0
    got = d.freshest_prices({"A": (1.0, now - 9, now - 9)},
                            {"B": (2.0, now - 8, now - 8)},
                            {"C": (3.0, now - 7, now - 7)})
    assert set(got) == {"A", "B", "C"}


# ── bars_age_sec must be the age NOW, not the age at bar evaluation ─────────

def test_the_engine_recomputes_bars_age_at_write_time():
    """proximity_state() hands out the value cached on the TickerState when
    the bar was last evaluated, and the writer republishes it unchanged — so
    it reports the age as of that eval and only ever understates. It becomes
    macd_age_sec, which is what the MACD staleness guard reads.

    Observed 2026-08-27 mid-session, one file, one write: VNCE published
    bars_age_sec 0.5s against a trade 639s old; GRRR 1.1s against 41.8s.
    """
    src = (_ROOT / "signal_engine.py").read_text(encoding="utf-8")
    i = src.index("def _write_signal_state")
    body = src[i:src.index("SIGNAL_STATE_FILE.write_text", i)]
    assert 'row["bars_age_sec"] = round(_age, 1)' in body
    # Same clock as the price, or the two disagree about the newest print.
    assert "_age = max(0.0, (_now_ms - float(_ts_ms)) / 1000.0)" in body


def test_only_the_realtime_pipe_gets_its_age_rewritten():
    """A REST-fallback row has no trade clock here; overwriting its age with
    the aggregator's would date an Alpaca bar by a Finnhub print. Aged-out
    realtime rows are demoted before rewrite so the dashboard does not merge
    a multi-minute print as live tape."""
    src = (_ROOT / "signal_engine.py").read_text(encoding="utf-8")
    i = src.index('row["bars_age_sec"] = round(_age, 1)')
    guard = src[max(0, i - 500):i]
    assert 'bars_src' in guard and '"realtime"' in guard
    assert "RT_BARS_MAX_STALE" in guard


def test_undated_alpaca_fetch_does_not_displace_desk_tape():
    """Alpaca fallback often caches (px, now, None). That must not wipe a
    young engine/desk print's trade clock — price_age_sec=None forces REST
    and stream_required on an otherwise live book row."""
    now = 1_787_000_000.0
    desk = {"EOSU": (3.50, now - 3.5, now - 3.5)}
    alp = {"EOSU": (3.49, now, None)}  # fetch-now, no trade clock
    got = d.freshest_prices(alp, {}, desk)
    assert got["EOSU"][0] == 3.50
    assert got["EOSU"][2] == pytest.approx(now - 3.5)


def test_dated_alpaca_still_beats_older_desk():
    now = 1_787_000_000.0
    desk = {"AAA": (10.00, now - 45.0, now - 45.0)}
    alp = {"AAA": (9.90, now - 2.0, now - 2.0)}
    got = d.freshest_prices(alp, {}, desk)
    assert got["AAA"][0] == 9.90
    assert got["AAA"][2] == pytest.approx(now - 2.0)
