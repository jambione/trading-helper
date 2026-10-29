"""alpaca_assets.active_us_equities: raw rows, so an exchange code alpaca-py does not know (TXSE) cannot fail the list."""
import alpaca_assets as A


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def get(self, path, data=None):
        self.calls.append((path, data))
        return self.rows


def test_unknown_exchange_is_just_a_string():
    tc = FakeClient([{"symbol": "GMAB", "name": "Genmab", "exchange": "NASDAQ", "tradable": True},
                     {"symbol": "NEWX", "name": "New venue", "exchange": "TXSE", "tradable": True},
                     "junk"])
    out = A.active_us_equities(tc)
    assert [a.symbol for a in out] == ["GMAB", "NEWX"] and out[1].exchange == "TXSE"
    assert tc.calls == [("/assets", {"status": "active", "asset_class": "us_equity"})]
    assert str(out[0].exchange).split(".")[-1] == "NASDAQ" and str(getattr(out[0].exchange, "value", out[0].exchange)) == "NASDAQ"


def test_overnight_universe_uses_the_raw_list():
    import overnight_book as ob
    tc = FakeClient([{"symbol": "IBM", "name": "IBM", "exchange": "NYSE", "tradable": True},
                     {"symbol": "TXSX", "name": "Texas listing", "exchange": "TXSE", "tradable": True}])
    u = ob.universe(tc)
    assert "IBM" in u and "TXSX" not in u          # unknown venue filtered by OK_EX, not a crash
