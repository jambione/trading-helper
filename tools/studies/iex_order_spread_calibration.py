#!/usr/bin/env python3
"""iex_order_spread_calibration.py — docs/studies/iex_order_spread_calibration_prereg.json (amended, amended_2, ready).

Moments = the scored rows of move_to_cost_hist (empty-square entries, $20-200, 96 sessions; entry second e:00 with
the SIP quote at e:00 already fetched = TRUTH, wide = SIP spread > 15 bp). Halves A ..07-29 (calibration),
B 07-30.. (held out).

  stage0   fetch ONLY the SIP quote at e:00 - 16 min (move_to_cost_hist rule: last usable quote at or before, age
           <= 15 s). Primary population = moments whose delayed spread is <= the live cap (0.15% = 15 bp, recorded in
           amended_2 G3); no usable delayed quote = excluded (the live gate refuses unknowns). Per half: primary n,
           SIP-wide count in the primary, base rate, mean SIP spread of primary-wide moments, sessions covered.
           UNDERPOWERED (stop, no IEX fetch) if B's primary-wide count < 150 or covers < 30 sessions.
The IEX stages (calibrate on A, score B) are written only if stage 0 is powered.
USAGE (mini, after the close, one heavy Alpaca job at a time):
  .venv/bin/python tools/studies/iex_order_spread_calibration.py stage0
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import move_to_cost_hist as MH  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/iex_order_spread"))
ROWS = MH.OUT / "rows.json"
CAP_BP, WIDE_BP, DELAY_MIN = 15.0, 15.0, 16
MIN_WIDE_B, MIN_SESSIONS_B = 150, 30


def moments(rows: list[dict]) -> list[dict]:
    """The scored move_to_cost_hist rows (status ok, LOW or REST) = the prereg's 5,476 moments."""
    return [r for r in rows if r.get("status") == "ok" and r.get("group") in ("LOW", "REST")]


def delayed_ts(r: dict) -> float:
    return MH.minute_ts(r["day"], r["t"] + 1) - DELAY_MIN * 60


def attach_delayed(ms: list[dict], data) -> Counter:
    st = Counter()
    for r in ms:
        try:
            q = data.quote(r["symbol"], delayed_ts(r))
        except MH.MH.FetchError:
            r["delayed_status"] = "error"
            st["error"] += 1
            continue
        _, sp = MH.mid_spread(q)
        r["delayed_spread"] = sp
        r["delayed_status"] = "ok" if sp is not None else "missing"
        st[r["delayed_status"]] += 1
    return st


def summary(ms: list[dict]) -> dict:
    out = {}
    for h in ("A", "B"):
        rs = [r for r in ms if MH.half(r["day"]) == h]
        prim = [r for r in rs if r.get("delayed_status") == "ok" and r["delayed_spread"] <= CAP_BP]
        wide = [r for r in prim if r["spread"] > WIDE_BP]
        out[h] = {"moments": len(rs), "excluded_no_delayed_quote": sum(r.get("delayed_status") == "missing" for r in rs),
                  "errors": sum(r.get("delayed_status") == "error" for r in rs),
                  "primary_n": len(prim), "primary_wide": len(wide),
                  "base_rate": len(wide) / len(prim) if prim else None,
                  "mean_spread_primary_wide_bp": statistics.fmean(r["spread"] for r in wide) if wide else None,
                  "wide_sessions": len({r["day"] for r in wide}),
                  "all_wide_in_half": sum(r["spread"] > WIDE_BP for r in rs)}
    b = out["B"]
    errs = sum(out[h]["errors"] for h in out)
    n = sum(out[h]["moments"] for h in out)
    out["void"] = n == 0 or errs / n > 0.05
    out["powered"] = (not out["void"]) and b["primary_wide"] >= MIN_WIDE_B and b["wide_sessions"] >= MIN_SESSIONS_B
    out["verdict"] = "VOID" if out["void"] else ("POWERED: continue to IEX" if out["powered"] else
                     "UNDERPOWERED: the delayed gate leaves too few wide orders for an order check to matter")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("stage0",))
    ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    ms = moments(json.loads(ROWS.read_text()))
    data = MH.QuoteData.live()
    data.dir = MH.OUT / "cache"            # shares move_to_cost_hist's quote cache (same rule, same key space)
    st = attach_delayed(ms, data)
    res = summary(ms)
    res.update({"fetch": dict(st), "quote_calls": data.calls})
    (OUT / "stage0.json").write_text(json.dumps(res, indent=1, default=str))
    (OUT / "stage0_rows.json").write_text(json.dumps(ms, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
