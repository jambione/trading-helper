#!/usr/bin/env python3
"""Reconcile the fill ledger against the broker's own record.

The check that has to pass every session before real money: does
``ai_reports/fills/YYYY-MM-DD.jsonl`` agree with what Alpaca says it filled?

Three findings, in descending order of how much they should worry you:

  BROKER_ONLY   the broker filled it and the ledger never saw it. The audit
                trail has a hole — a position exists that nothing on this box
                recorded. This is the one that makes the ledger untrustworthy.
  MISMATCHED    both have the order, but quantity or average price disagree.
  LEDGER_ONLY   the ledger has an order the broker's lookback no longer
                covers. Usually benign (window age), worth a look if recent.

Exit status is 0 on a clean day and 1 on any finding, so it can be a cron
guard rather than something you remember to run::

    .venv/bin/python tools/fill_reconcile.py                 # today, ET
    .venv/bin/python tools/fill_reconcile.py --day 2026-09-17
    .venv/bin/python tools/fill_reconcile.py --days-back 5 --json
    .venv/bin/python tools/fill_reconcile.py --live           # the live book

Connects for itself when no desk is running, so it works from cron; the
account it reached is printed before the findings. Paper unless --live,
which is a separate flag on purpose, as in flatten.py.

Reads only. It never writes to the ledger and never touches an order.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import fill_ledger


def _ensure_broker(live: bool) -> str:
    """Connect for ourselves when no desk has. Returns credential provenance.

    reconcile_day() asks alpaca_trader for the broker's side, and that module
    is only ``is_active()`` after ``init()``, which the desk performs at
    startup. From cron there is no desk, so every standalone run returned
    ``trader_off`` and exited 1 — a nightly alarm that never actually checks
    anything, which gets muted within a week and is then worth less than no
    alarm at all. So the tool connects on its own.

    Read-only: ``init()`` builds a client and nothing on this path submits,
    cancels or modifies an order. Credential resolution is deliberately
    flatten.py's, so the two safety tools can never disagree about which
    account they mean.
    """
    import alpaca_trader
    if alpaca_trader.is_active():
        return "already active (running inside the desk)"
    import flatten
    key, sec, where = flatten.resolve_credentials()
    if not key or not sec:
        print("\u2717 No Alpaca credentials found (checked environment, "
              "config/secrets.json, signal_engine.env)", file=sys.stderr)
        raise SystemExit(2)
    alpaca_trader.init("live" if live else "paper", key, sec)
    return where


def _days(args) -> list[str]:
    if args.day:
        return [args.day]
    today = datetime.now(tz=fill_ledger.ET).date()
    back = max(1, int(args.days_back))
    return [(today - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(back - 1, -1, -1)]


def _print_human(rep: dict) -> None:
    day = rep.get("day")
    if rep.get("error"):
        print(f"{day}  SKIPPED — {rep['error']}")
        return

    verdict = "OK" if rep.get("ok") else "FINDINGS"
    print(f"{day}  {verdict}   "
          f"ledger={rep.get('ledger_orders', 0)}  "
          f"broker={rep.get('broker_orders', 0)}  "
          f"matched={rep.get('matched', 0)}")

    for row in rep.get("broker_only") or []:
        print(f"    BROKER_ONLY  {row.get('symbol'):<6} "
              f"qty={row.get('filled_qty')} @ {row.get('filled_avg_price')}  "
              f"id={row.get('order_id')}")
    for row in rep.get("mismatched") or []:
        print(f"    MISMATCHED   {row.get('symbol'):<6} "
              f"qty {row.get('ledger_qty')} vs {row.get('broker_qty')}  "
              f"px {row.get('ledger_px')} vs {row.get('broker_px')}  "
              f"id={row.get('order_id')}")
    for row in rep.get("ledger_only") or []:
        print(f"    LEDGER_ONLY  {row.get('symbol'):<6} "
              f"qty={row.get('filled_qty')}  id={row.get('order_id')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="ET date YYYY-MM-DD (default: today)")
    ap.add_argument("--days-back", type=int, default=1,
                    help="reconcile the last N ET days (default 1)")
    ap.add_argument("--limit", type=int, default=500,
                    help="max broker orders to pull per day (default 500)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--live", action="store_true",
                    help="reconcile the LIVE book (default: paper). Separate "
                         "flag on purpose, as in flatten.py.")
    args = ap.parse_args()

    where = _ensure_broker(args.live)
    if not args.json:
        import alpaca_trader
        acct = alpaca_trader.account_number() or "(unknown)"
        book = "LIVE" if args.live else "PAPER"
        print(f"account {acct} ({book})   credential: {where}")

    reports = [fill_ledger.reconcile_day(day, limit=args.limit)
               for day in _days(args)]

    if args.json:
        print(json.dumps(reports, indent=2, default=str))
    else:
        for rep in reports:
            _print_human(rep)

    # A day we could not check is not a day that passed.
    findings = any(
        rep.get("error") or not rep.get("ok") for rep in reports
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
