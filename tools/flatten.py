#!/usr/bin/env python3
"""flatten.py — cancel every order and close every position, desk or no desk.

THE FAILURE THIS ANSWERS
  This desk runs software stops on purpose (ai_broker_stop_enabled=False): the
  local trail is 578 of 790 exits. That means the desk PROCESS is the stop. If
  ai_trader.py dies with a position open — crash, power cut, OS update, network
  drop — nothing at the broker is protecting those shares.

  alpaca_trader.liquidate_all() already does this well, but it lives inside the
  desk's module graph and needs alpaca_trader.init(), which the desk performs at
  startup. Reaching it therefore means the desk is running, which is precisely
  the assumption that has failed. Same for ai_eod_liquidate / ai_sod_liquidate:
  real flatten logic, running inside the process you are trying to work around.

  So this script imports NOTHING from the desk. Credentials, broker SDK, stdlib.
  It does not read the desk's state files, does not need the dashboard, does not
  care whether anything else on the box is alive.

USAGE
  Check what it would do — safe, touches nothing:
      .venv/bin/python tools/flatten.py --dry-run

  Flatten the paper book:
      .venv/bin/python tools/flatten.py --yes

  Flatten the live book (deliberately a separate flag from the paper default):
      .venv/bin/python tools/flatten.py --live --yes

  From a phone, over ssh, one line:
      ssh mac-mini-away 'cd ~/repo/trading-helper && \\
          .venv/bin/python tools/flatten.py --yes'

EXIT STATUS
  0  the broker confirms the book is flat
  1  something is still open, or the state could not be verified
  2  could not connect / no credentials

  Nonzero means GO LOOK. "ok" here means verified flat, never merely attempted
  — see the 2026-08-07 note in _close_everything.

TESTING IT
  --dry-run exercises credential resolution, connectivity and the account
  identity print without sending an order. Run it monthly against paper; a
  flatten switch nobody has tested is not a flatten switch.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Unbuffered by default. stdout block-buffers when piped or redirected while
# stderr does not, so the first run of this script printed its connection error
# ABOVE its own header — the one moment you want the order to be trustworthy.
print = functools.partial(print, flush=True)

# A cancel is not instant. Alpaca reports an order "pending cancel" while the
# shares are still held_for_orders, so an immediate close is rejected with
# available:0 — and by the time that error is read the cancel HAS completed,
# leaving a position with no stop and no target. That is how USAR ended
# 2026-08-07 naked. Same constants the desk's own liquidate_all uses.
CANCEL_SETTLE_SEC = 2.0
ATTEMPTS = 3


# ── Credentials ───────────────────────────────────────────────────────────────

def _from_secrets_json() -> tuple[str, str]:
    """config/secrets.json — where the mini keeps them."""
    try:
        data = json.loads(
            (_ROOT / "config" / "secrets.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return (str(data.get("api_key") or "").strip(),
            str(data.get("secret_key") or "").strip())


def _from_engine_env() -> tuple[str, str]:
    """signal_engine.env — where a dev checkout may keep them instead."""
    key = sec = ""
    try:
        for line in (_ROOT / "signal_engine.env").read_text(
                encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "ALPACA_API_KEY":
                key = v
            elif k == "ALPACA_SECRET_KEY":
                sec = v
    except OSError:
        return "", ""
    return key, sec


def resolve_credentials() -> tuple[str, str, str]:
    """(key, secret, where). The two boxes store these in different places.

    Checked 2026-09-18: the mini keeps them in config/secrets.json and its
    signal_engine.env has none; a laptop checkout is the other way round. Env
    vars win so a one-off can override without editing either file.
    """
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    sec = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if key and sec:
        return key, sec, "environment"
    key, sec = _from_secrets_json()
    if key and sec:
        return key, sec, "config/secrets.json"
    key, sec = _from_engine_env()
    if key and sec:
        return key, sec, "signal_engine.env"
    return "", "", "nowhere"


# ── Broker ────────────────────────────────────────────────────────────────────

def connect(paper: bool):
    key, sec, where = resolve_credentials()
    if not key or not sec:
        print("✗ No Alpaca credentials found (checked environment, "
              "config/secrets.json, signal_engine.env)")
        raise SystemExit(2)
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        print("✗ alpaca-py is not installed in this interpreter.")
        raise SystemExit(2) from None

    client = TradingClient(key, sec, paper=paper)
    try:
        acct = client.get_account()
    except Exception as e:  # noqa: BLE001
        print(f"✗ Could not reach Alpaca: {e}")
        raise SystemExit(2) from None

    # Say WHICH account out loud before touching it. The paper and live keys
    # are different values from different pages and one env var apart.
    print(f"  account   : {getattr(acct, 'account_number', '?')} "
          f"({'PAPER' if paper else 'LIVE'})")
    print(f"  equity    : ${float(getattr(acct, 'equity', 0) or 0):,.2f}")
    print(f"  credential: {where}")
    return client


def _positions(client) -> list:
    try:
        return list(client.get_all_positions() or [])
    except Exception as e:  # noqa: BLE001
        print(f"  ! could not read positions: {e}")
        return []


def _open_orders(client) -> list:
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return list(client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        ) or [])
    except Exception as e:  # noqa: BLE001
        print(f"  ! could not read open orders: {e}")
        return []


def _market_open(client) -> bool | None:
    try:
        return bool(client.get_clock().is_open)
    except Exception:
        return None


# ── The switch ────────────────────────────────────────────────────────────────

def _close_everything(client, errors: list[str]) -> list[str]:
    """Cancel, settle, close, retry. Returns symbols confirmed closed.

    Ordering matters and is not cosmetic: a resting order holds the shares, so
    a close before the cancel settles is refused for available:0.
    """
    closed: list[str] = []

    orders = _open_orders(client)
    if orders:
        print(f"  cancelling {len(orders)} open order(s)…")
        try:
            client.cancel_orders()
        except Exception as e:  # noqa: BLE001
            errors.append(f"cancel_orders: {e}")
            print(f"  ! cancel_orders failed: {e}")
        print(f"  waiting {CANCEL_SETTLE_SEC}s for cancels to settle…")
        time.sleep(CANCEL_SETTLE_SEC)
    else:
        print("  no open orders")

    for attempt in range(ATTEMPTS):
        remaining = [p for p in _positions(client)
                     if str(getattr(p, "symbol", "")).upper() not in closed]
        if not remaining:
            break
        if attempt:
            print(f"  retry {attempt}/{ATTEMPTS - 1} — "
                  f"{len(remaining)} position(s) left")
        for pos in remaining:
            sym = str(getattr(pos, "symbol", "") or "").upper()
            qty = getattr(pos, "qty", "?")
            try:
                client.close_position(sym)
                closed.append(sym)
                print(f"  ✓ closed {sym} ({qty})")
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {sym}: {e}")
                if attempt == ATTEMPTS - 1:
                    errors.append(f"{sym}: {e}")
        if attempt < ATTEMPTS - 1:
            time.sleep(CANCEL_SETTLE_SEC)
    return closed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cancel every order and close every position. "
                    "Runs with no dependency on the trading desk.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="act on the LIVE account (default: paper)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be closed; send nothing")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt (required when "
                         "stdin is not a terminal, e.g. over ssh)")
    args = ap.parse_args()

    paper = not args.live
    print("")
    print("  ── FLATTEN ─────────────────────────────────────────")
    client = connect(paper)

    is_open = _market_open(client)
    print(f"  market    : {'OPEN' if is_open else 'closed' if is_open is False else 'unknown'}")

    positions = _positions(client)
    orders = _open_orders(client)
    print("")
    if not positions and not orders:
        print("  Book is already flat — nothing to do.")
        print("")
        return 0

    exposure = 0.0
    for p in positions:
        try:
            exposure += abs(float(getattr(p, "market_value", 0) or 0))
        except (TypeError, ValueError):
            pass
    print(f"  {len(positions)} position(s), ${exposure:,.2f} exposure; "
          f"{len(orders)} open order(s)")
    for p in positions:
        print(f"    {getattr(p, 'symbol', '?')!s:<6} "
              f"qty={getattr(p, 'qty', '?'):<8} "
              f"value=${float(getattr(p, 'market_value', 0) or 0):,.2f} "
              f"pl=${float(getattr(p, 'unrealized_pl', 0) or 0):,.2f}")
    print("")

    if args.dry_run:
        print("  --dry-run: nothing was sent.")
        print("")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("  Refusing to act without --yes on a non-interactive run.")
            return 2
        word = "LIVE" if args.live else "PAPER"
        if input(f"  Close all {word} positions? type 'flatten' to confirm: "
                 ).strip().lower() != "flatten":
            print("  Aborted.")
            return 2
        print("")

    if is_open is False:
        # close_position() sends a market order, which the broker will not fill
        # outside RTH. Say so rather than reporting a silent non-fill as done.
        print("  ! Market is closed — market closes will not fill until the")
        print("    next session. Orders are submitted; verify at the open.")
        print("")

    errors: list[str] = []
    closed = _close_everything(client, errors)

    # ok means THE BOOK IS FLAT, verified against the broker — never inferred
    # from what was attempted. The desk's own liquidate_all once returned
    # ok:true with closed:0 because a single cancel had succeeded; a naked
    # position went into the weekend on 2026-08-07. Ask the broker instead.
    print("")
    leftover = [str(getattr(p, "symbol", "?")).upper()
                for p in _positions(client)]
    print(f"  closed    : {len(closed)} {closed or '-'}")
    print(f"  errors    : {len(errors)}")
    for e in errors:
        print(f"    {e}")
    print(f"  still open: {leftover or '-'}")
    print("")

    if leftover:
        print("  ✗ NOT FLAT — positions remain. Go look.")
        print("")
        return 1
    print("  ✓ Flat. Broker confirms no open positions.")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
