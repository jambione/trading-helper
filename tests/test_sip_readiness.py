"""SIP month plan S1 (wired 2026-11-04): one feed knob, one delay helper, no new hard-coded IEX in live code.
Defaults must keep today's behaviour exactly; flipping alpaca_bar_feed + ai_sip_realtime is the whole switch."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import alpaca_api as aa  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_defaults_are_iex_and_delayed():
    from alpaca.data.enums import DataFeed
    assert aa.live_data_feed({}) == DataFeed.IEX
    assert aa.live_data_feed({"alpaca_bar_feed": "iex"}) == DataFeed.IEX
    assert aa.sip_delay_min(15.0, {}) == 15.0 and aa.sip_delay_min(16.0, {"ai_sip_realtime": False}) == 16.0


def test_the_switch_is_two_knobs():
    from alpaca.data.enums import DataFeed
    cfg = {"alpaca_bar_feed": "sip", "ai_sip_realtime": True}
    assert aa.live_data_feed(cfg) == DataFeed.SIP
    assert aa.sip_delay_min(15.0, cfg) == 0.0 and aa.sip_delay_min(16.0, cfg) == 0.0
    # and back
    assert aa.live_data_feed({"alpaca_bar_feed": "iex"}) == DataFeed.IEX


def test_config_registers_the_knobs():
    import config
    assert config.DEFAULT_CONFIG["alpaca_bar_feed"] == "iex" and config.DEFAULT_CONFIG["ai_sip_realtime"] is False
    assert "ai_sip_realtime" in config._EFFECTIVE_KEYS and "alpaca_bar_feed" in config._EFFECTIVE_KEYS


# Files allowed to name IEX directly, and why. A new live module that hard-codes IEX fails this test.
IEX_ALLOWED = {
    "alpaca_api.py": "the feed helper itself",
    "desk_io.py": "a comment about enum serialisation",
    "momentum-monitor/desk_actions.py": "separate Discord momentum app, not the trading path",
    "overnight_book.py": "retired book (disabled 10/29)",
    "rs_screener.py": "research screener, daily bars",
    "backtest_v2.py": "backtest harness",
}


def test_no_new_hard_coded_iex_in_live_code():
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    skip = re.compile(r"^(tests|tools|scripts|docs)/|study|_sim|replay")
    bad = []
    for f in files:
        if skip.search(f) or f in IEX_ALLOWED:
            continue
        for n, line in enumerate((ROOT / f).read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if re.search(r"DataFeed\.IEX|feed\s*=\s*[\"']iex[\"']|[\"']feed[\"']\s*:\s*[\"']iex[\"']", line):
                bad.append(f"{f}:{n}: {line.strip()[:90]}")
    assert not bad, "hard-coded IEX in live code (use alpaca_api.live_data_feed()):\n" + "\n".join(bad)
