from .base import Account, BrokerProvider, MarketDataProvider, Order, Position


def build_providers(cfg: dict):
    """Instantiate the (market_data, broker) pair named by cfg["provider"]."""
    name = cfg.get("provider", "mock")
    if name == "webull":
        from .webull import WebullBroker, WebullMarketData
        md = WebullMarketData(cfg)
        return md, WebullBroker(cfg)
    from .mock import MockBroker, MockMarketData
    md = MockMarketData(cfg)
    return md, MockBroker(cfg, md)


__all__ = ["Account", "BrokerProvider", "MarketDataProvider", "Order",
           "Position", "build_providers"]
