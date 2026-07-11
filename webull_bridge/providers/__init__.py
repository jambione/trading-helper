from .base import Account, BrokerProvider, MarketDataProvider, Order, Position


def _make_market_data(name: str, cfg: dict):
    if name == "webull":
        from .webull import WebullMarketData
        return WebullMarketData(cfg)
    from .mock import MockMarketData
    return MockMarketData(cfg)


def _make_broker(name: str, cfg: dict, md):
    if name == "webull":
        from .webull import WebullBroker
        return WebullBroker(cfg)
    from .mock import MockBroker, MockMarketData
    if not isinstance(md, MockMarketData):
        # the mock broker fills against a simulated book, so give it one
        # of its own when the market-data half isn't the mock
        md = MockMarketData(cfg)
    return MockBroker(cfg, md)


def build_providers(cfg: dict):
    """Instantiate the (market_data, broker) pair.

    cfg["provider"] names both halves; cfg["market_data_provider"] /
    cfg["broker_provider"] override either one independently (e.g. real
    Webull broker for positions/orders while depth stays mock/scraped
    because the account has no OpenAPI quotes subscription).
    """
    default = cfg.get("provider", "mock")
    md_name = cfg.get("market_data_provider") or default
    br_name = cfg.get("broker_provider") or default
    md = _make_market_data(md_name, cfg)
    return md, _make_broker(br_name, cfg, md)


__all__ = ["Account", "BrokerProvider", "MarketDataProvider", "Order",
           "Position", "build_providers"]
