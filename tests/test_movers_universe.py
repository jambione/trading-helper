"""movers_screener.universe_symbols: raw /v2/assets rows, so an exchange code alpaca-py does not know cannot empty the
scan universe (dead 2026-10-20..10-28)."""
import movers_screener as M


def test_universe_keeps_listed_common_stocks_and_skips_unknown_exchanges():
    rows = [
        {"symbol": "GMAB", "tradable": True, "exchange": "NASDAQ", "name": "Genmab A/S"},
        {"symbol": "IBM", "tradable": True, "exchange": "NYSE", "name": "IBM"},
        {"symbol": "NEWX", "tradable": True, "exchange": "SOMENEWVENUE", "name": "New venue"},
        {"symbol": "HALT", "tradable": False, "exchange": "NASDAQ", "name": "Halted"},
        {"symbol": "TQQQ", "tradable": True, "exchange": "NASDAQ", "name": "ProShares UltraPro QQQ"},
    ]
    assert M.universe_symbols(rows) == ["GMAB", "IBM"]


def test_universe_load_survives_a_model_error(monkeypatch):
    import alpaca.trading.client as tc

    class Fake:
        def __init__(self, *a, raw_data=False, **k):
            self.raw = raw_data

        def get_all_assets(self, req):
            if not self.raw:
                raise ValueError("Input should be 'AMEX', 'ARCA', ...")
            return [{"symbol": "GMAB", "tradable": True, "exchange": "NASDAQ", "name": "Genmab"}]

    monkeypatch.setattr(tc, "TradingClient", Fake)
    monkeypatch.setattr(M, "_scan_universe", [])
    monkeypatch.setattr(M, "_scan_universe_day", None)
    assert M._load_scan_universe("k", "s") == ["GMAB"]
