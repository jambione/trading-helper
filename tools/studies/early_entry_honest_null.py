#!/usr/bin/env python3
"""Null calibration for early_entry_honest.py: score every rule on a driftless
synthetic random walk (IEX = SIP, every minute on the board). Every lift should
read ~0; the artifact demo (the shift method) should not.

  .venv/bin/python tools/studies/early_entry_honest_null.py --study mid_rsi2 --days 30 --names 15
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import zlib
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_entry_honest as E  # noqa: E402


def frame(day: str, seed: int) -> pd.DataFrame:
    r = random.Random(seed)
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=E.ET)
    rows, p = [], 20.0
    for m in range(4 * 60, 16 * 60):
        o = p
        p *= 1 + r.gauss(0, 0.002)
        v = r.randint(50, 500) * (3 if r.random() < 0.05 else 1)
        t = pd.Timestamp(d.replace(hour=m // 60, minute=m % 60)).tz_convert("UTC")
        rows.append((t, o, max(o, p) * 1.001, min(o, p) * 0.999, p, v))
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--study", choices=sorted(E.STUDIES), default="pre_ign")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--names", type=int, default=15)
    a = ap.parse_args()
    E.RULES, E.GRADED = E.STUDIES[a.study]["rules"], E.STUDIES[a.study]["graded"]
    datas, d0 = [], datetime(2026, 6, 1)
    for k in range(a.days):
        day = (d0 + timedelta(days=k)).strftime("%Y-%m-%d")
        syms = [f"S{i}" for i in range(a.names)]
        fr = {s: frame(day, zlib.crc32(f"{s}{day}".encode())) for s in syms}
        datas.append({"day": day, "board": {s: set(range(9 * 60 + 30, 16 * 60)) for s in syms},
                      "iex": fr, "sip": fr})
    print(E.report(E.score(datas)))


if __name__ == "__main__":
    main()
