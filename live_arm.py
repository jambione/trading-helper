"""Is live trading armed, and for which account?

Single responsibility: decide whether the AI desk may place REAL orders. It
answers, it never acts.

WHAT THIS REPLACES
  ai_trading.init_for_ai() used to hard-code ``mode="paper"`` under a comment
  reading "HARD RULE: AI desk path never places live orders through this
  module", and ``is_ready()`` returned ``_ready and _mode == "paper"``. That
  rail was correct and load-bearing, so it is not being deleted — it is being
  replaced by something a single mistake still cannot get past.

THE TWO-FACTOR RULE
  Live needs BOTH, and neither alone does anything:

    1. ``ai_live_trading_enabled: true`` in config/bot_config.json.
       Tracked in git, reviewable in a diff, and listed in
       PROTECTED_CONFIG_KEYS so the dashboard's HTTP write path refuses it.

    2. config/live_armed.json on disk, gitignored, naming the account:
           {"account_number": "PA...", "armed_by": "jmb",
            "armed_at": "2026-09-18", "note": "stage 1 machinery test"}

  One is in the repo and travels with a deploy; the other is per-machine and
  cannot travel at all. A config push cannot arm a box that was never armed by
  hand, and a stray file cannot arm a desk whose config says paper.

  The second factor carries the account number on purpose. You cannot arm live
  without writing down which account you mean, which is what makes the startup
  assertion possible at all — see verify_account(). Paper and live keys are
  different values from different pages and one env var apart, and the desk has
  historically had no way to notice it was pointed at the wrong one.

SEPARATE KEYS
  Live uses ALPACA_LIVE_API_KEY / ALPACA_LIVE_SECRET_KEY and NEVER falls back
  to the paper pair. A missing live key disarms rather than quietly trading the
  paper account under a live label.

HOST
  When ai_trading_host is set, only that machine may arm. Risk caps are
  enforced per-instance and liquidate_all closes EVERY position in the account,
  so two boxes on one key is a real failure — it has happened on paper.

FAIL DIRECTION
  Every ambiguity disarms. "off" or "paper" when live was intended is a missed
  trade; "live" when paper was intended is real money on a desk nobody checked.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ARM_FILE = ROOT / "config" / "live_armed.json"

LIVE_KEY_ENV = "ALPACA_LIVE_API_KEY"
LIVE_SECRET_ENV = "ALPACA_LIVE_SECRET_KEY"


def _cfg(cfg: dict | None) -> dict:
    if isinstance(cfg, dict):
        return cfg
    try:
        from config import load_config
        return load_config()
    except Exception:
        return {}


def read_arm_file(path: Path | None = None) -> dict[str, Any]:
    """The on-disk arming record, or {} when absent/unreadable/malformed."""
    p = path or ARM_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def live_keys() -> tuple[str, str]:
    """The LIVE pair. Never falls back to the paper keys — that is the point."""
    return (os.environ.get(LIVE_KEY_ENV, "").strip(),
            os.environ.get(LIVE_SECRET_ENV, "").strip())


def host_allows(cfg: dict | None = None) -> tuple[bool, str]:
    """(allowed, why_not). Empty ai_trading_host means no restriction."""
    want = str(_cfg(cfg).get("ai_trading_host") or "").strip()
    if not want:
        return True, ""
    here = socket.gethostname().strip()
    # Hostnames arrive with and without the .local suffix depending on how the
    # process was started; compare on the short name so an ssh session and a
    # GUI session agree.
    if here.split(".")[0].lower() == want.split(".")[0].lower():
        return True, ""
    return False, f"host {here!r} is not ai_trading_host {want!r}"


def arm_state(cfg: dict | None = None, *, arm_path: Path | None = None) -> dict[str, Any]:
    """Full arming decision, with every factor named.

    Returns ``{armed, account_number, factors, reasons}``. ``reasons`` is empty
    exactly when ``armed`` is True, so a caller can log why live did not engage
    without re-deriving it.
    """
    c = _cfg(cfg)
    reasons: list[str] = []

    cfg_on = bool(c.get("ai_live_trading_enabled", False))
    if not cfg_on:
        reasons.append("ai_live_trading_enabled is not true in bot_config.json")

    record = read_arm_file(arm_path)
    account = str(record.get("account_number") or "").strip().upper()
    file_on = bool(record) and bool(account)
    if not record:
        reasons.append(f"no arming file at {(arm_path or ARM_FILE)}")
    elif not account:
        reasons.append("arming file has no account_number")

    key, secret = live_keys()
    keys_on = bool(key and secret)
    if not keys_on:
        reasons.append(f"{LIVE_KEY_ENV} / {LIVE_SECRET_ENV} not set")

    host_ok, host_why = host_allows(c)
    if not host_ok:
        reasons.append(host_why)

    return {
        "armed": bool(cfg_on and file_on and keys_on and host_ok),
        "account_number": account,
        "factors": {
            "config": cfg_on,
            "arm_file": file_on,
            "live_keys": keys_on,
            "host": host_ok,
        },
        "reasons": reasons,
    }


def is_armed(cfg: dict | None = None) -> bool:
    return bool(arm_state(cfg).get("armed"))


def verify_account(actual: str, expected: str) -> tuple[bool, str]:
    """Does the connected account match the one the operator armed?

    The assertion the desk never had. ``alpaca_trader.init`` builds its client
    with ``paper = (_mode == "paper")`` and never checks what came back, so a
    wrong key pair produced a perfectly healthy-looking desk pointed at an
    account nobody intended.

    A blank ``actual`` fails: an account we cannot identify is not the account
    we armed.
    """
    a = str(actual or "").strip().upper()
    e = str(expected or "").strip().upper()
    if not e:
        return False, "no armed account_number to check against"
    if not a:
        return False, "broker did not report an account number"
    if a != e:
        return False, f"connected account {a} is not the armed account {e}"
    return True, ""


def describe(cfg: dict | None = None) -> str:
    """One block of text for a startup log. Safe to print — no secrets."""
    st = arm_state(cfg)
    lines = [f"live arming: {'ARMED' if st['armed'] else 'not armed'}"]
    for name, ok in st["factors"].items():
        lines.append(f"  {'✓' if ok else '✗'} {name}")
    if st["armed"]:
        lines.append(f"  account: {st['account_number']}")
    for why in st["reasons"]:
        lines.append(f"  - {why}")
    return "\n".join(lines)


__all__ = [
    "ARM_FILE",
    "arm_state",
    "describe",
    "host_allows",
    "is_armed",
    "live_keys",
    "read_arm_file",
    "verify_account",
]
