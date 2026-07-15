"""SDK depth feed error handling: a symbol the OpenAPI rejects must not
flood the monitor's Rich display or bill the API at poll rate.

Regression for the OCEA session: OCR handed the BookFeed a ticker Webull
doesn't carry, and every 0.5s poll produced an INVALID_SYMBOL traceback plus
the SDK's own stdout dump of the whole request dict, which shredded the
dashboard into unreadable scroll.
"""
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webull_bridge.providers.webull import (  # noqa: E402
    WebullMarketData, _route_sdk_logs)

INVALID = ("HTTP Status: 417, Code: INVALID_SYMBOL, Msg: The symbol does "
           "not exist in the category., RequestID: d24bd794")


class BoomClient:
    """Stands in for DataClient: counts HTTP attempts, always rejects."""

    def __init__(self, err=INVALID):
        self.calls = 0
        outer = self

        class _MD:
            @staticmethod
            def get_quotes(symbol, category, depth=10):
                outer.calls += 1
                raise RuntimeError(err)

        self.market_data = _MD()


def make_md(err=INVALID, bad_ttl=600.0):
    """WebullMarketData without the SDK import / credential check."""
    md = WebullMarketData.__new__(WebullMarketData)
    md.cfg = {}
    md.client = BoomClient(err)
    md.poll = 0.5
    md.max_rps = 6.0
    md.depth = 10
    md.category = "US_STOCK"
    md._last = {}
    md._active = 0
    md._bad = {}
    md._bad_ttl = bad_ttl
    return md


def test_invalid_symbol_is_tried_once_not_every_poll():
    md = make_md()
    for _ in range(50):
        assert md._fetch("OCEA") is None
    assert md.client.calls == 1


def test_invalid_symbol_cache_is_case_insensitive():
    md = make_md()
    md._fetch("OCEA")
    assert md.known_bad("OCEA")
    assert md.known_bad("ocea")


def test_other_symbols_still_polled():
    md = make_md()
    md._fetch("OCEA")
    md._fetch("AAPL")
    assert md.client.calls == 2, "one bad symbol must not mute the rest"


def test_bad_symbol_reprobed_after_ttl():
    md = make_md()
    md._fetch("OCEA")
    md._bad["OCEA"] = 0.0          # cooldown expired
    md._fetch("OCEA")
    assert md.client.calls == 2
    assert not md.known_bad("XYZ")


def test_other_errors_do_not_poison_the_symbol():
    """A transient failure must stay retryable - only INVALID_SYMBOL is
    permanent enough to stop polling."""
    md = make_md(err="HTTP Status: 500, Code: RATE_LIMIT")
    md._fetch("AAPL")
    assert not md.known_bad("AAPL")
    md._fetch("AAPL")
    assert md.client.calls == 2


def test_fetch_logs_without_traceback(caplog):
    md = make_md()
    with caplog.at_level(logging.WARNING):
        md._fetch("OCEA")
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is None, "no traceback at poll rate"
    assert "OCEA" in caplog.records[0].getMessage()


def test_route_sdk_logs_preempts_the_stdout_handler():
    """DataClient._init_logger attaches a StreamHandler to sys.stdout unless
    a logger is already claimed - that is what wrote into the Rich frame."""
    calls = {}
    client = types.SimpleNamespace(
        _file_logger_set=False,
        set_file_logger=lambda **kw: calls.update(kw))
    _route_sdk_logs(client, {"webull_sdk_log": "sdk.log"})
    assert client._stream_logger_set is True
    assert calls["path"] == "sdk.log"
    assert calls["log_level"] == logging.WARNING


def test_route_sdk_logs_respects_an_existing_file_logger():
    client = types.SimpleNamespace(
        _file_logger_set=True,
        set_file_logger=lambda **kw: pytest.fail("must not re-add"))
    _route_sdk_logs(client, {})
    assert client._stream_logger_set is True
