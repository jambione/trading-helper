"""
test_finviz_universe.py — the optional Finviz universe adapter, offline.

No network: the HTTP session is monkeypatched and the parser runs against an
inline HTML fixture shaped like a Finviz screener page.

The property that matters most here is the last one: this adapter is scraping a
site that does not permit it, behind Cloudflare, and it WILL start failing on
some random morning. Every failure path must return [] so rs_screener falls back
to the Alpaca universe. A broken scrape must never end a run.

Run:
    .venv/bin/python -m pytest tests/test_finviz_universe.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import finviz_universe as fv   # noqa: E402


def page(tickers) -> str:
    """A screener page stripped to the bit the parser anchors on."""
    rows = "".join(
        f'<tr><td><a href="quote.ashx?t={t}&ty=c&p=d&b=1" class="tab-link">{t}</a></td>'
        f'<td><a href="screener.ashx?v=111&f=ind_semiconductors">Semis</a></td></tr>'
        for t in tickers)
    return f"<html><body><table class='screener_table'>{rows}</table></body></html>"


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    """Serves a queue of responses, then empty pages forever — which is what
    Finviz itself does past the end of a result set."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(page([]))


def wire(monkeypatch, session):
    import types
    fake_requests = types.SimpleNamespace(Session=lambda: session)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    return session


# ── URL construction ──────────────────────────────────────────────────────────

def test_the_url_carries_the_filter_tokens_and_the_page_offset():
    url = fv.build_url(["sh_price_o10", "ta_sma50_pa"], offset=21)
    assert "f=sh_price_o10,ta_sma50_pa" in url
    assert url.endswith("&r=21")


def test_the_default_filters_ask_for_stocks_only():
    """The one thing this adapter buys that Alpaca's asset list cannot give —
    Alpaca's Asset model has no ETF flag."""
    assert "ind_stocksonly" in fv.DEFAULT_FILTERS


def test_an_empty_filter_list_still_builds_a_valid_url():
    assert fv.build_url([], offset=1).startswith(fv.BASE_URL)


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_tickers_are_read_off_the_quote_links():
    """Finviz has restructured the screener table repeatedly; the quote href
    has stayed put."""
    assert fv.parse_tickers(page(["NVDA", "AAPL", "AMD"])) == ["NVDA", "AAPL", "AMD"]


def test_screener_links_are_not_mistaken_for_tickers():
    assert "IND_SEMICONDUCTORS" not in fv.parse_tickers(page(["NVDA"]))


def test_a_ticker_listed_twice_appears_once():
    assert fv.parse_tickers(page(["NVDA", "NVDA", "AMD"])) == ["NVDA", "AMD"]


def test_dotted_and_hyphenated_symbols_survive_the_parse():
    assert set(fv.parse_tickers(page(["BRK.B", "BF-B"]))) == {"BRK.B", "BF-B"}


def test_a_symbol_containing_a_digit_is_not_truncated_to_its_letters():
    """A character class that stopped at the first digit would emit 'S' for
    'S1' — a real ticker belonging to a different company."""
    assert fv.parse_tickers(page(["S1", "S2"])) == ["S1", "S2"]


def test_unparseable_html_yields_no_tickers_rather_than_raising():
    assert fv.parse_tickers("<html><body>Just a moment...</body></html>") == []
    assert fv.parse_tickers("") == []


# ── Paging ────────────────────────────────────────────────────────────────────

def test_pages_are_walked_until_no_new_tickers_appear(monkeypatch):
    """Finviz repeats the last page forever past the end of the result set, so
    'nothing new' is the only reliable terminator."""
    session = wire(monkeypatch, FakeSession([
        FakeResponse(page(["AAA", "BBB"])),
        FakeResponse(page(["CCC"])),
        FakeResponse(page(["CCC"])),          # repeat → stop
    ]))
    out = fv.fetch_universe({"rs_finviz_pause_sec": 0})
    assert out == ["AAA", "BBB", "CCC"]
    assert len(session.urls) == 3


def test_the_page_cap_bounds_the_scrape(monkeypatch):
    """Without a cap a filter that matches everything walks thousands of pages."""
    session = wire(monkeypatch, FakeSession(
        [FakeResponse(page([f"S{i}"])) for i in range(50)]))
    fv.fetch_universe({"rs_finviz_pause_sec": 0, "rs_finviz_max_pages": 4})
    assert len(session.urls) == 4


# ── Failure is always soft ────────────────────────────────────────────────────

def test_a_cloudflare_block_returns_nothing_rather_than_raising(monkeypatch):
    wire(monkeypatch, FakeSession([FakeResponse("<html>Attention Required</html>", 403)]))
    assert fv.fetch_universe({"rs_finviz_pause_sec": 0}) == []


def test_a_network_error_returns_nothing_rather_than_raising(monkeypatch):
    class Exploding(FakeSession):
        def get(self, url, timeout=None):
            raise OSError("connection reset")

    wire(monkeypatch, Exploding([]))
    assert fv.fetch_universe({"rs_finviz_pause_sec": 0}) == []


def test_a_partial_scrape_keeps_what_it_managed_to_read(monkeypatch):
    wire(monkeypatch, FakeSession([
        FakeResponse(page(["AAA", "BBB"])),
        FakeResponse("<html>rate limited</html>", 429),
    ]))
    assert fv.fetch_universe({"rs_finviz_pause_sec": 0}) == ["AAA", "BBB"]


def test_the_screener_falls_back_to_alpaca_when_the_scrape_returns_nothing(
        monkeypatch, tmp_path):
    """The integration property: a broken scrape must not end a run."""
    import rs_fetch
    import rs_screener

    monkeypatch.setattr(fv, "fetch_universe", lambda cfg: [])
    monkeypatch.setattr(rs_fetch, "tradable_universe", lambda cfg: ["AAA", "BBB"])
    monkeypatch.setattr(rs_screener, "UNIVERSE_FILE", tmp_path / "absent.json")

    assert rs_screener.screen_universe({"rs_universe_source": "finviz"}) == ["AAA", "BBB"]


def test_a_scrape_that_works_is_used_as_the_universe(monkeypatch, tmp_path):
    import rs_screener

    monkeypatch.setattr(fv, "fetch_universe", lambda cfg: ["NVDA", "AMD"])
    monkeypatch.setattr(rs_screener, "UNIVERSE_FILE", tmp_path / "absent.json")
    assert rs_screener.screen_universe({"rs_universe_source": "finviz"}) == ["NVDA", "AMD"]


def test_the_adapter_is_not_the_default_universe_source():
    """It narrows the ranking population, which changes what an RS rating means."""
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["rs_universe_source"] == "alpaca"
