#!/usr/bin/env python3
"""Pre-screen turn test. Spec: docs/studies/prescreen_turn_prereg.json.

Does a %R turn out of a dip pay on ANY liquid name, before a screen admits it? Operator 10/16 RPRX: both lines
turned up at 11:45, the run was 11:47-11:52, the movers screen admitted RPRX at 11:52 (because of that run) and
the desk bought at 11:57. Every earlier entry test used only board (screened) minutes.

Data: the warehouse (tools/studies/warehouse_fetch.py): IEX bars for the %R lines (what live sees), SIP bars for
the markout, the previous session's IEX bars to warm the lines. A seeded sample of SAMPLE names per session from
the names present on both feeds; every RTH minute counts as 'on the board'. Scoring is early_entry_honest.score
unchanged (entry at the next SIP open, 10-minute hold, later-only same-name control, 10 bp, day-clustered t).

  .venv/bin/python tools/studies/prescreen_turn.py            # inventory only
  .venv/bin/python tools/studies/prescreen_turn.py --score    # after a result-skeptic READY on the prereg
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_entry_honest as E  # noqa: E402

ROOT = os.environ.get("REPO") or os.getcwd()
WH = os.path.join(ROOT, "ai_reports", "warehouse")
SAMPLE = 400
MAX_EXCLUDED_DAYS = 6       # prereg amended_1: more excluded sessions -> graded rules UNDERPOWERED
RULES = ("BOTHUP", "MID", "OSL", "PRE", "SQ")
GRADED = ("BOTHUP", "MID")


def _frames(path: str) -> dict:
    """npz day-file -> {sym: DataFrame(time, open, high, low, close, volume)} (time = UTC, tz-aware)."""
    import pandas as pd
    z = np.load(path, allow_pickle=False)
    syms, idx, mins = z["syms"], z["sym_idx"], z["minute"]
    day = os.path.basename(path)[:-4]
    base = pd.Timestamp(day, tz=E.ET)
    out = {}
    order = np.argsort(idx, kind="stable")
    bounds = np.searchsorted(idx[order], np.arange(len(syms) + 1))
    for k, s in enumerate(syms):
        sel = order[bounds[k]:bounds[k + 1]]
        if len(sel) == 0:
            continue
        t = [(base + pd.Timedelta(minutes=int(m))).tz_convert("UTC") for m in mins[sel]]
        out[str(s)] = pd.DataFrame({"time": t, "open": z["o"][sel].astype(float), "high": z["h"][sel].astype(float),
                                    "low": z["l"][sel].astype(float), "close": z["c"][sel].astype(float),
                                    "volume": z["v"][sel].astype(float)})
    return out


def sample_names(day: str, names: list[str], n: int = SAMPLE) -> list[str]:
    rng = random.Random(int.from_bytes(hashlib.sha256(f"{day}|prescreen-sample".encode()).digest()[:8], "big"))
    names = sorted(names)
    return names if len(names) <= n else sorted(rng.sample(names, n))


class LazyDay(dict):
    """A day whose bars load on first access; only one day is held in memory at a time (score() walks days in
    order)."""
    _slot: "LazyDay | None" = None

    def __init__(self, day: str, prev: str | None):
        super().__init__(day=day)
        self._prev = prev

    def _load(self) -> None:
        if LazyDay._slot is not None and LazyDay._slot is not self:
            for k in ("board", "iex", "sip", "iex_prior"):
                dict.pop(LazyDay._slot, k, None)
        LazyDay._slot = self
        iex = _frames(os.path.join(WH, "iex", f"{self['day']}.npz"))
        sip = _frames(os.path.join(WH, "sip", f"{self['day']}.npz"))
        p_iex = _frames(os.path.join(WH, "iex", f"{self._prev}.npz")) if self._prev else {}
        # Skeptic F1: sample only names with today's IEX + SIP AND the previous session's IEX bars (known before the
        # open), so no sampled name is scored cold and no day is dropped for missing warm-up.
        names = sample_names(self["day"], [s for s in iex if s in sip and s in p_iex])
        prior = {s: p_iex[s].tail(E.CON_PRIOR_BARS).reset_index(drop=True) for s in names}
        minutes = set(range(E.RTH_START, E.RTH_END))
        dict.update(self, board={s: minutes for s in names}, iex={s: iex[s] for s in names},
                    sip={s: sip[s] for s in names}, iex_prior=prior)

    def __getitem__(self, k):
        if k != "day" and not dict.__contains__(self, k):
            self._load()
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        try:
            return self[k]
        except KeyError:
            return default


def usable_days() -> list[LazyDay]:
    have = {f: {x[:-4] for x in os.listdir(os.path.join(WH, f)) if x.endswith(".npz")}
            for f in ("iex", "sip") if os.path.isdir(os.path.join(WH, f))}
    if len(have) < 2:
        return []
    both = sorted(have["iex"] & have["sip"])
    out = []
    for k, d in enumerate(both):
        if k == 0:
            continue                       # the first session has no prior bars to warm the lines: excluded
        out.append(LazyDay(d, both[k - 1]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    E.RULES, E.GRADED, E.WARM_LINES = RULES, GRADED, True
    days = usable_days()
    print(f"usable sessions {len(days)}: {days[0]['day'] if days else '-'} .. {days[-1]['day'] if days else '-'}; "
          f"half A ends {E.split([d['day'] for d in days])}; sample {SAMPLE} names/session")
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    res = E.score(days)
    excluded = len(res.get("con_excluded") or [])
    if excluded > MAX_EXCLUDED_DAYS:
        for r in GRADED:
            res["rules"][r]["verdict"] = "UNDERPOWERED"
            res["rules"][r]["pooled"]["promising"] = False
    # Skeptic F3: the same-minute cross-name control must ALSO be beaten in each half (strips market-wide bounces
    # and time-of-day drift that a later same-name control can carry).
    cross_note = []
    for r in GRADED:
        mu_a, mu_b = res["rules"][r]["info_x"]["crossA"][0], res["rules"][r]["info_x"]["crossB"][0]
        ok = mu_a is not None and mu_b is not None and mu_a > 0 and mu_b > 0
        cross_note.append(f"- {r}: cross-name lift A {E._bp(mu_a)}, B {E._bp(mu_b)} -> "
                          f"{'beats the same-minute cross control in both halves' if ok else 'FAILS the cross control'}")
        if not ok:
            if res["rules"][r]["verdict"] == "PASS":
                res["rules"][r]["verdict"] = "FAIL"
            res["rules"][r]["pooled"]["promising"] = False
    md = E.report(res)
    md += ("\n\n### Pre-screen gates (prereg amended_1)\n\n" + "\n".join(cross_note) +
           f"\n- days excluded for missing warm-up: {excluded} (cap {MAX_EXCLUDED_DAYS})\n")
    print(md)
    out = os.path.join(ROOT, "ai_reports", "warehouse", "prescreen_turn_result.md")
    with open(out, "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
