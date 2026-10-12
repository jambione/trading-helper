#!/usr/bin/env python3
"""%R latch / RSI fire simulation. Spec: docs/studies/latch_gate_prereg.json.

Do not pass --score until that prereg has a result-skeptic READY. The default
does not read the cache and does not print a return.

The state machine (`walk`) is pure. Market bars are turned into bars only
under --score, and only from ai_reports/indicator_levels/ext.pkl.
"""
from __future__ import annotations

import hashlib
import math
import os
import pickle
import random
import statistics
import sys
from dataclasses import dataclass

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import signals  # noqa: E402
from strategy_three_indicator import _falling, _rising  # noqa: E402

LOOKBACK = 2
RSI_ENTRY_MIN = 0.0
RSI_ENTRY_MAX = 50.0
RSI_EXIT_MIN = 70.0
COST = 0.0020
MIN_PRICE = 10.0
ENTRY_START = 9 * 60 + 40
ENTRY_END = 15 * 60 + 30
FLATTEN_MIN = 15 * 60 + 50
MIN_TRADES = 100
MIN_DAYS = 8
LIFT_BAR = 0.0005  # +5 bp
CACHE = os.path.join(ROOT, "ai_reports", "indicator_levels", "ext.pkl")
CFG = {
    "cm_rsi_length": 2,
    "rte_fast_length": 21,
    "rte_fast_ewm_span": 7,
    "rte_slow_native_length": 112,
    "rte_slow_ewm_span": 3,
    "rte_minute_grid": True,
    "rte_slow_timeframe": None,
}


@dataclass
class Bar:
    minute: int
    open: float
    close: float
    fast_up: bool
    slow_up: bool
    fast_dn: bool
    slow_dn: bool
    rsi: float
    rsi_up: bool
    price_ok: bool


def walk(bars: list[Bar]) -> list[dict]:
    """One name-day. Decisions use the completed bar. Fills are the next open.

    The gate starts shut. Both lines rising opens it. Both lines falling shuts
    it. Anything else leaves it. A fire is the first bar of a streak where the
    gate is open, RSI is in [0, 50], and RSI is rising. RSI >= 70 sells the
    swing at the next open and does not shut the gate. Still open on the 15:50
    bar sells at that bar's close.
    """
    gate = False
    prev_conj = False
    pending: tuple | None = None
    pos: dict | None = None
    out: list[dict] = []

    def _close(exit_i: int, px: float, reason: str) -> None:
        nonlocal pos
        assert pos is not None
        out.append({
            "signal_i": pos["signal_i"],
            "fill_i": pos["fill_i"],
            "entry": pos["entry"],
            "exit_i": exit_i,
            "exit": px,
            "reason": reason,
            "gross": px / pos["entry"] - 1.0,
            "latch_only": pos["latch_only"],
            "gate_shut_while_in": pos["gate_shut_while_in"],
        })
        pos = None

    for i, b in enumerate(bars):
        if pending is not None and pending[0] == "sell" and pos is not None:
            _close(i, b.open, "rsi70")
            pending = None
        elif pending is not None and pending[0] == "buy" and pos is None:
            pos = {
                "signal_i": pending[1],
                "fill_i": i,
                "entry": b.open,
                "latch_only": pending[2],
                "gate_shut_while_in": False,
            }
            pending = None

        if pos is not None and b.minute >= FLATTEN_MIN and pending is None:
            _close(i, b.close, "session_flatten")

        if b.fast_up and b.slow_up:
            gate = True
        elif b.fast_dn and b.slow_dn:
            gate = False
        if pos is not None and not gate:
            pos["gate_shut_while_in"] = True

        rsi_ok = math.isfinite(b.rsi) and RSI_ENTRY_MIN <= b.rsi <= RSI_ENTRY_MAX
        conj = bool(gate and rsi_ok and b.rsi_up)
        nxt = i + 1 < len(bars) and bars[i + 1].minute <= FLATTEN_MIN
        if (
            pos is not None
            and pending is None
            and math.isfinite(b.rsi)
            and b.rsi >= RSI_EXIT_MIN
            and b.minute < FLATTEN_MIN
            and nxt
        ):
            pending = ("sell", i)
        elif (
            pos is None
            and pending is None
            and conj
            and not prev_conj
            and b.price_ok
            and ENTRY_START <= b.minute <= ENTRY_END
            and nxt
        ):
            pending = ("buy", i, not (b.fast_up and b.slow_up))
        prev_conj = conj

    if pos is not None:
        last = bars[-1]
        _close(len(bars) - 1, last.close, "session_flatten")
    return out


def _minute(ts) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t = t.tz_convert("America/New_York")
    return int(t.hour) * 60 + int(t.minute)


def bars_from_frame(frame: pd.DataFrame) -> list[Bar]:
    """Completed-bar flags from the desk's own %R and RSI functions."""
    df = frame.copy()
    if "time" not in df.columns:
        df = df.reset_index()
        df = df.rename(columns={df.columns[0]: "time"})
    df = signals.compute_cm_rsi_lower(df, CFG)
    df = signals.compute_percent_r_exhaustion(df, CFG)
    fast = df["s_percentR"].to_numpy(dtype=float)
    slow = df["l_percentR"].to_numpy(dtype=float)
    rsi = df["cm_rsi"].to_numpy(dtype=float)
    times = df["time"].to_numpy()
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    bars: list[Bar] = []
    for i in range(len(df)):
        bars.append(Bar(
            minute=_minute(times[i]),
            open=float(opens[i]),
            close=float(closes[i]),
            fast_up=_rising(fast, i, LOOKBACK),
            slow_up=_rising(slow, i, LOOKBACK),
            fast_dn=_falling(fast, i, LOOKBACK),
            slow_dn=_falling(slow, i, LOOKBACK),
            rsi=float(rsi[i]) if rsi[i] == rsi[i] else float("nan"),
            rsi_up=_rising(rsi, i, LOOKBACK),
            price_ok=bool(closes[i] == closes[i] and closes[i] >= MIN_PRICE),
        ))
    return bars


def _rng(sym: str, day: str, entry_minute: int) -> random.Random:
    digest = hashlib.sha256(f"{sym}|{day}|{entry_minute}|latch-v1".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _pair(times: list, trade: dict, hour_indexes: dict[int, list[int]]) -> tuple[float, float] | None:
    """(lift, net) or None when the hold is gapped or the hour has no control."""
    fill_i, exit_i = trade["fill_i"], trade["exit_i"]
    hold = exit_i - fill_i
    if hold <= 0:
        return None
    span = times[exit_i] - times[fill_i]
    if int(round(span)) != hold * 60:
        return None
    entry_minute = _minute(times[fill_i])
    hour = entry_minute // 60
    eligible = [
        k for k in hour_indexes.get(hour, [])
        if k != fill_i and k + hold < len(times)
        and int(round(times[k + hold] - times[k])) == hold * 60
    ]
    if not eligible:
        return None
    k = _rng(trade["sym"], trade["day"], entry_minute).choice(eligible)
    gross_c = trade["ctrl_close"][k + hold] / trade["ctrl_open"][k] - 1.0
    net = trade["gross"] - COST
    net_c = gross_c - COST
    return net - net_c, net


def day_equal(values_by_day: dict[str, list[float]]) -> tuple[float | None, float | None, int, int]:
    """Day-equal mean and its t. Days with no trades are absent."""
    means = [statistics.mean(v) for v in values_by_day.values() if v]
    n = sum(len(v) for v in values_by_day.values())
    if len(means) < 3 or n == 0:
        return None, None, n, len(means)
    mu = statistics.mean(means)
    sd = statistics.stdev(means)
    t = mu / (sd / math.sqrt(len(means))) if sd > 0 else None
    return mu, t, n, len(means)


def half_verdict(mu_lift, t_lift, mu_net, n, n_days) -> str:
    if n < MIN_TRADES or n_days < MIN_DAYS or mu_lift is None or t_lift is None or mu_net is None:
        return "UNDERPOWERED"
    if mu_lift >= LIFT_BAR and t_lift >= 2.0 and mu_net > 0:
        return "PASS"
    return "FAIL"


def score(cache: dict) -> dict:
    """Score every non-null name-day. The calendar cut comes from the keys only."""
    days = sorted({d for (_s, d), frame in cache.items() if frame is not None})
    if not days:
        raise SystemExit("ext.pkl has no frames")
    cut = days[(len(days) - 1) // 2]
    misses = sum(1 for frame in cache.values() if frame is None)
    lifts: dict[str, dict[str, list[float]]] = {"A": {}, "B": {}}
    nets: dict[str, dict[str, list[float]]] = {"A": {}, "B": {}}
    m15: dict[str, dict[str, list[float]]] = {"A": {}, "B": {}}
    drops = {"gapped_or_no_control": 0}
    reasons = {"rsi70": 0, "session_flatten": 0}
    latch_only = 0
    gate_shut = 0
    n_trades = 0
    holds: list[int] = []

    for (sym, day), frame in cache.items():
        if frame is None or len(frame) < 120:
            continue
        half = "A" if day <= cut else "B"
        built = bars_from_frame(frame)
        times = _times(frame)
        opens = frame["open"].to_numpy(dtype=float) if "open" in frame.columns else None
        closes = frame["close"].to_numpy(dtype=float) if "close" in frame.columns else None
        if opens is None:
            raw = frame.reset_index()
            opens = raw["open"].to_numpy(dtype=float)
            closes = raw["close"].to_numpy(dtype=float)
        by_hour: dict[int, list[int]] = {}
        for i, ts in enumerate(times):
            by_hour.setdefault(_minute(ts) // 60, []).append(i)
        for tr in walk(built):
            n_trades += 1
            reasons[tr["reason"]] = reasons.get(tr["reason"], 0) + 1
            latch_only += int(tr["latch_only"])
            gate_shut += int(tr["gate_shut_while_in"])
            holds.append(int(round(times[tr["exit_i"]] - times[tr["fill_i"]])) // 60)
            tr["sym"] = sym
            tr["day"] = day
            tr["ctrl_open"] = opens
            tr["ctrl_close"] = closes
            paired = _pair(times, tr, by_hour)
            if paired is None:
                drops["gapped_or_no_control"] += 1
                continue
            lift, net = paired
            lifts[half].setdefault(day, []).append(lift)
            nets[half].setdefault(day, []).append(net)
            mark = _mark15(times, opens, closes, tr)
            if mark is not None:
                m15[half].setdefault(day, []).append(mark)

    halves = {}
    for h in ("A", "B"):
        mu_l, t_l, n, nd = day_equal(lifts[h])
        mu_n, _t_n, _n2, _d2 = day_equal(nets[h])
        mu_m, t_m, n_m, _dm = day_equal(m15[h])
        halves[h] = {
            "lift": mu_l, "t": t_l, "net": mu_n, "n": n, "days": nd,
            "verdict": half_verdict(mu_l, t_l, mu_n, n, nd),
            "mark15": mu_m, "mark15_t": t_m, "mark15_n": n_m,
        }
    overall = "PASS" if halves["A"]["verdict"] == "PASS" and halves["B"]["verdict"] == "PASS" else "FAIL"
    return {
        "cut": cut, "n_days_cache": len(days), "fetch_misses": misses,
        "fires": n_trades, "latch_only": latch_only, "gate_shut_while_in": gate_shut,
        "reasons": reasons, "median_hold_min": statistics.median(holds) if holds else None,
        "drops": drops, "halves": halves, "verdict": overall,
    }


def _times(frame: pd.DataFrame) -> list[float]:
    if "time" in frame.columns:
        col = frame["time"]
    else:
        col = frame.reset_index().iloc[:, 0]
    out = []
    for ts in col:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        out.append(t.timestamp())
    return out


def _mark15(times, opens, closes, tr) -> float | None:
    """Information only. Entry-open to the close 15 minutes later, minus 20 bp, minus the same on the control."""
    fill_i = tr["fill_i"]
    target = times[fill_i] + 15 * 60
    exit_i = next((i for i, ts in enumerate(times) if abs(ts - target) < 1), None)
    if exit_i is None or exit_i <= fill_i:
        return None
    hold = exit_i - fill_i
    if int(round(times[exit_i] - times[fill_i])) != hold * 60:
        return None
    entry_minute = _minute(times[fill_i])
    hour = entry_minute // 60
    eligible = [
        k for k in range(len(times))
        if _minute(times[k]) // 60 == hour and k != fill_i and k + hold < len(times)
        and int(round(times[k + hold] - times[k])) == hold * 60
    ]
    if not eligible:
        return None
    k = _rng(tr["sym"], tr["day"], entry_minute).choice(eligible)
    net = closes[exit_i] / opens[fill_i] - 1.0 - COST
    net_c = closes[k + hold] / opens[k] - 1.0 - COST
    return net - net_c


def _fmt(x, bp=True) -> str:
    if x is None:
        return "—"
    return f"{x * 1e4:+.1f} bp" if bp else f"{x:+.2f}"


def report(result: dict) -> str:
    lines = [
        f"calendar days in cache: {result['n_days_cache']}",
        f"cut (last date of the first half): {result['cut']}",
        f"fetch misses: {result['fetch_misses']}",
        f"fires: {result['fires']}",
        f"scored-trade drops (gapped hold or no control): {result['drops']['gapped_or_no_control']}",
        f"exit reasons: {result['reasons']}",
        f"median hold minutes: {result['median_hold_min']}",
        f"fires with the gate held open from an earlier bar: {result['latch_only']}",
        f"trades whose gate shut before the exit: {result['gate_shut_while_in']}",
        "",
    ]
    for h in ("A", "B"):
        s = result["halves"][h]
        lines.append(
            f"half {h}: {s['verdict']}  n={s['n']} days={s['days']}  "
            f"lift {_fmt(s['lift'])}  t {_fmt(s['t'], bp=False)}  net {_fmt(s['net'])}"
        )
        lines.append(
            f"  15-min markout (information, not a verdict): "
            f"lift {_fmt(s['mark15'])}  t {_fmt(s['mark15_t'], bp=False)}  n={s['mark15_n']}"
        )
    lines.append("")
    lines.append(f"VERDICT: {result['verdict']}")
    if result["verdict"] == "PASS":
        lines.append("PENDING SKEPTIC REVIEW. This does not change the live arm.")
    else:
        lines.append("The latch stays a way to read the chart. The live arm stays the square.")
    return "\n".join(lines)


def main() -> None:
    if "--score" not in sys.argv:
        print("latch_gate_sim: not scored. --score reads ext.pkl and is waiting on a skeptic READY.")
        return
    if not os.path.isfile(CACHE):
        raise SystemExit(f"missing {CACHE}; not falling back to the RTH-only runway cache")
    with open(CACHE, "rb") as f:
        cache = pickle.load(f)
    print(report(score(cache)))


if __name__ == "__main__":
    main()
