#!/usr/bin/env python3
"""SIP go/no-go checklist (operator 11/05). Read-only except one stamp file. Run it on the MINI (the Alpaca keys live there).

    .venv/bin/python tools/sip_sunday_check.py            # Sunday night, before the first SIP Monday
    .venv/bin/python tools/sip_sunday_check.py --entitlement   # weekday with the market OPEN, right after buying: records the stamp

Why two runs: a delayed-only plan answers "subscription does not permit querying recent SIP data" ONLY for trades newer than
~15 minutes. On a weekend the newest SPY trade is days old, so the same call passes on a plan that does NOT have live SIP.
So the entitlement test counts only when the market is open and the newest SIP trade is seconds old; it writes
ai_reports/sip_entitlement.json, and the Sunday run requires that stamp (a pass within 5 days). No stamp = NO-GO.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
STAMP = ROOT / "ai_reports" / "sip_entitlement.json"
MAX_TRADE_AGE_SEC = 30.0          # a real-time SIP print on SPY is seconds old; delayed data is 900+
STAMP_MAX_AGE_DAYS = 5
VOL_CACHE_MAX_AGE_DAYS = 3


def market_open(now: datetime) -> bool:
    """Weekday 09:30-16:00 ET (holidays are not modelled: a holiday just looks like a stale trade and fails safe)."""
    n = now.astimezone(ET)
    return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)


def entitlement_passes(trade_age_sec: float | None, is_open: bool, error: str | None) -> tuple[bool, str]:
    """Pure decision: does one SIP latest-trade probe prove live entitlement?"""
    if error:
        return False, f"SIP latest-trade call failed: {error[:120]}"
    if not is_open:
        return False, "market closed: a delayed-only plan also passes this call on old trades, so it proves nothing"
    if trade_age_sec is None:
        return False, "no trade timestamp returned"
    if trade_age_sec > MAX_TRADE_AGE_SEC:
        return False, f"newest SIP SPY trade is {trade_age_sec:.0f}s old (delayed data is ~900s); need <= {MAX_TRADE_AGE_SEC:.0f}s"
    return True, f"newest SIP SPY trade is {trade_age_sec:.1f}s old with the market open"


def stamp_ok(stamp: dict | None, now_ts: float) -> tuple[bool, str]:
    if not stamp or not stamp.get("passed"):
        return False, "no passing entitlement stamp: run --entitlement on a weekday with the market open after buying SIP"
    age_days = (now_ts - float(stamp.get("ts", 0))) / 86400.0
    if age_days > STAMP_MAX_AGE_DAYS:
        return False, f"entitlement stamp is {age_days:.1f} days old (limit {STAMP_MAX_AGE_DAYS}); re-run --entitlement"
    return True, f"entitlement stamp passed {age_days:.1f} days ago ({stamp.get('detail', '')})"


def probe_sip() -> tuple[float | None, str | None]:
    """Newest SPY SIP trade age in seconds, or (None, error text)."""
    try:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest
        import alpaca_api as aa
        from config import load_config_ro
        client = aa.connect_data_client(dict(load_config_ro() or {}))
        t = client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols="SPY", feed=DataFeed.SIP))["SPY"]
        return max(0.0, time.time() - t.timestamp.timestamp()), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def read_env_feed() -> str:
    p = ROOT / "signal_engine.env"
    try:
        for line in p.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?ALPACA_BAR_FEED\s*=\s*['\"]?([A-Za-z]+)", line)
            if m:
                return m.group(1).lower()
    except OSError:
        pass
    return ""


def run_entitlement() -> int:
    now = datetime.now(ET)
    age, err = probe_sip()
    ok, detail = entitlement_passes(age, market_open(now), err)
    if market_open(now) or ok:
        STAMP.parent.mkdir(exist_ok=True)
        STAMP.write_text(json.dumps({"ts": time.time(), "passed": ok, "detail": detail, "et": now.isoformat(timespec="seconds")}))
    print(("PASS  " if ok else "FAIL  ") + detail)
    if not ok:
        print("Do NOT flip alpaca_bar_feed to sip. Live SIP is not entitled (or the market is closed).")
    return 0 if ok else 1


def run_checklist() -> int:
    import alpaca_api as aa
    from config import load_config_ro
    cfg = dict(load_config_ro() or {})
    rows: list[tuple[bool, str, str]] = []

    def add(ok: bool, name: str, detail: str) -> None:
        rows.append((ok, name, detail))

    try:
        stamp = json.loads(STAMP.read_text())
    except (OSError, ValueError):
        stamp = None
    ok, d = stamp_ok(stamp, time.time())
    add(ok, "1 live SIP entitlement proven on a weekday with prints", d)

    add(str(cfg.get("alpaca_bar_feed", "")).lower() == "sip", "2 config alpaca_bar_feed = sip", f"is {cfg.get('alpaca_bar_feed')!r}")
    add(cfg.get("ai_sip_realtime") is True, "3 config ai_sip_realtime = true", f"is {cfg.get('ai_sip_realtime')!r}")
    env = read_env_feed()
    add(env == "sip", "4 signal_engine.env ALPACA_BAR_FEED = sip (the env var wins over config)", f"is {env or '(unset)'!r}")
    feed = aa.live_data_feed(cfg)
    delay = aa.sip_delay_min(float(cfg.get("ai_movers_sip_delay_min", 15.0)), cfg)
    add(getattr(feed, "value", str(feed)) == "sip" and delay == 0, "5 live_data_feed() reads sip and SIP delay terms are 0", f"feed {getattr(feed, 'value', feed)}, pacing delay {delay:g} min")

    vc = ROOT / "cache" / "avg_session_volume.json"
    if vc.exists():
        age_d = (time.time() - vc.stat().st_mtime) / 86400
        add(age_d <= VOL_CACHE_MAX_AGE_DAYS, "6 cache/avg_session_volume.json rebuilt on SIP daily bars (RVOL needs one feed)", f"file is {age_d:.1f} days old; rebuild at the flip if older than {VOL_CACHE_MAX_AGE_DAYS}")
    else:
        add(False, "6 cache/avg_session_volume.json rebuilt on SIP daily bars", "file missing")

    add(cfg.get("ai_phase_b_dry_run") is True, "7 Phase B (premarket) stays DRY RUN for the first SIP morning", f"ai_phase_b_dry_run is {cfg.get('ai_phase_b_dry_run')!r}")

    try:
        pos = json.loads((ROOT / "ai_reports" / "positions_state.json").read_text())
        add(not pos, "8 no open positions", f"{len(pos)} open")
    except (OSError, ValueError):
        add(False, "8 no open positions", "positions_state.json unreadable")

    lg = ROOT / "logs" / "ai_trader.log"
    try:
        tail = "\n".join(lg.read_text(errors="replace").splitlines()[-300:])
        add("Traceback" not in tail, "9 no traceback in the last 300 ai_trader log lines", "clean" if "Traceback" not in tail else "TRACEBACK present")
    except OSError:
        add(False, "9 ai_trader log readable", "missing")

    try:  # informational, not a GO line: what premarket saw on the last logged morning (operator 11/06)
        from tools import premarket_report as pr
        days = pr.days_available(1)
        pm_info = pr.ping_text(days[-1]) if days else "no premarket ledger yet"
    except Exception as e:  # noqa: BLE001
        pm_info = f"premarket report failed: {type(e).__name__}: {e}"
    print(f"SIP go/no-go  {datetime.now(ET):%a %Y-%m-%d %H:%M ET}")
    print(f"  (info) last premarket morning: {pm_info}")
    for ok, name, detail in rows:
        print(f"  [{'OK' if ok else 'NO'}] {name}\n        {detail}")
    go = all(r[0] for r in rows)
    print("\nGO: every line is OK. Monday morning: watch for 429s and re-read the stale-tape / spread / RVOL numbers (tools/sip_readiness.py)." if go
          else "\nNO-GO: fix every [NO] line first. Until then the desk stays on IEX.")
    print("Rollback: alpaca_bar_feed iex, ai_sip_realtime false, ALPACA_BAR_FEED=iex, restart via the LaunchAgent.")
    return 0 if go else 1


def main(argv: list[str]) -> int:
    return run_entitlement() if "--entitlement" in argv else run_checklist()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
