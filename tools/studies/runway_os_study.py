#!/usr/bin/env python3
"""Oversold-leave entries and the stop/EOD exit set, on recorded 1-minute bars.

Read-only. Uses ai_reports/runway_bars_cache.pkl and outcomes.jsonl.
Does not write bot_config and does not talk to a broker.

1-minute bars cannot see the live 8-second chase. A minute with no new high
is treated as seven idle steps (60/8). Treat the dollars as directional,
same caveat as docs/RATCHET_STOP_STUDY_2026-09-24.md.

Synthetic entries use a 5% stop (1R = 5% of price), the live synth stop.
Two consecutive 1-minute bars stand in for two arm polls — stricter than
the live ~1s poll, so this under-counts signals that only lasted a second.
"""
from __future__ import annotations

import json
import os
import pickle
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import bars  # noqa: E402
import runway_study as rs  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")
OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
COST_RT = 0.002
OB = -20.0
OS = -80.0
TIGHT = 15.0
ARM_R = 0.06
GIVE_MAX_PCT = 1.0


def smooth_wr(h, l, c, length, span):
    raw = []
    for i in range(len(c)):
        if i + 1 < length:
            raw.append(None)
            continue
        hh = max(h[i - length + 1:i + 1])
        ll = min(l[i - length + 1:i + 1])
        span_px = hh - ll
        raw.append(None if span_px <= 0 else -100.0 * (hh - c[i]) / span_px)
    vals = [v for v in raw if v is not None]
    if not vals:
        return [None] * len(c)
    a = 2.0 / (span + 1.0)
    sm = [vals[0]]
    for v in vals[1:]:
        sm.append(a * v + (1 - a) * sm[-1])
    out = [None] * len(c)
    j = 0
    for i, v in enumerate(raw):
        if v is None:
            continue
        out[i] = sm[j]
        j += 1
    return out


def eod_index(ts):
    for i in range(len(ts) - 1, -1, -1):
        dt = datetime.fromtimestamp(ts[i], ET)
        if dt.hour * 60 + dt.minute <= 15 * 60 + 50:
            return i
    return len(ts) - 1


def rth(ts_i):
    dt = datetime.fromtimestamp(ts_i, ET)
    m = dt.hour * 60 + dt.minute
    return 9 * 60 + 30 <= m <= 15 * 60 + 50


def _tight(fast, slow):
    if fast is None or slow is None:
        return False
    return abs(fast - slow) <= TIGHT + 1e-9


def find_signals(B, fast, slow, sym, day):
    """Square holds and leave-oversold opens. One signal per episode."""
    ts, _o, _h, _l, c, _v = B
    out = []
    sq_streak = 0
    sq_open = False
    os_streak = 0
    qualified = False
    left_i = None
    for i in range(1, len(c)):
        if not rth(ts[i]):
            continue
        f, s = fast[i], slow[i]
        if not _tight(f, s) and (f is None or s is None):
            sq_streak = 0
            sq_open = False
            os_streak = 0
            continue
        both_ob = f is not None and s is not None and f >= OB and s >= OB and _tight(f, s)
        both_deep = f is not None and s is not None and f <= OS and s <= OS and _tight(f, s)
        deep_wide = (
            f is not None and s is not None and f <= OS and s <= OS and not _tight(f, s)
        )
        if both_ob:
            sq_streak += 1
            if sq_streak >= 2 and not sq_open and c[i] and c[i] > 0:
                out.append({"kind": "square", "i": i, "px": float(c[i]),
                            "sym": sym, "day": day})
                sq_open = True
        else:
            sq_streak = 0
            sq_open = False
        if both_deep:
            os_streak += 1
            left_i = None
            if os_streak >= 2:
                qualified = True
            continue
        os_streak = 0
        if deep_wide or not qualified:
            continue
        if left_i is None:
            left_i = i
        if ts[i] - ts[left_i] > 60:
            qualified = False
            left_i = None
            continue
        prev = fast[i - 1]
        if f is not None and prev is not None and f > prev and c[i] and c[i] > 0:
            out.append({"kind": "oversold", "i": i, "px": float(c[i]),
                        "sym": sym, "day": day})
            qualified = False
            left_i = None
    return out


def _give_px(last, risk, give_r):
    raw = give_r * risk
    if GIVE_MAX_PCT > 0 and last > 0:
        raw = min(raw, last * GIVE_MAX_PCT / 100.0)
    return raw


def simulate(B, i0, entry, stop0, risk, fast, spec, binds):
    ts, o, h, l, c, _v = B
    i_end = eod_index(ts)
    stop = float(stop0)
    peak = float(entry)
    mfe_r = 0.0
    mae_r = 0.0
    saw_ob = fast[i0] is not None and fast[i0] >= OB
    dump_streak = 0
    idle_anchor = peak
    for i in range(i0 + 1, i_end + 1):
        if l[i] <= stop:
            xpx = o[i] if o[i] < stop else stop
            mfe_r = max(mfe_r, (h[i] - entry) / risk)
            mae_r = min(mae_r, (l[i] - entry) / risk)
            return _done(i, xpx, "stop", mfe_r, mae_r, ts, i0, peak)
        peak = max(peak, h[i])
        mfe_r = max(mfe_r, (peak - entry) / risk)
        mae_r = min(mae_r, (l[i] - entry) / risk)
        last = c[i]
        hold = ts[i] - ts[i0]
        if spec.get("dead") and hold + 1e-9 >= 30 * 60 and mfe_r < 0.10:
            return _done(i, last, "dead", mfe_r, mae_r, ts, i0, peak)
        f = fast[i]
        if spec.get("lob") and f is not None:
            if f >= OB:
                saw_ob = True
            elif saw_ob and f < OB:
                return _done(i, last, "left_overbought", mfe_r, mae_r, ts, i0, peak)
        if spec.get("dump") and f is not None:
            # 1m bars: two confirming reads span about two minutes, so the
            # prior peak has to stay visible longer than the live 60s tick
            # window or the second bar never sees it. One-bar mode is the
            # screenshot (this bar vs the prior bar) and does not wait.
            if spec.get("dump_one_bar"):
                prev = fast[i - 1] if i > 0 else None
                if prev is not None and prev + 1e-9 >= OB and f <= prev - spec["dump"] + 1e-9:
                    return _done(i, last, "rsi_dump", mfe_r, mae_r, ts, i0, peak)
            else:
                window_peak = f
                for j in range(i, max(i0, i - 4) - 1, -1):
                    if ts[i] - ts[j] > 180:
                        break
                    if fast[j] is not None:
                        window_peak = max(window_peak, fast[j])
                if window_peak + 1e-9 >= OB and f <= window_peak - spec["dump"] + 1e-9:
                    dump_streak += 1
                else:
                    dump_streak = 0
                if dump_streak >= 2:
                    return _done(i, last, "rsi_dump", mfe_r, mae_r, ts, i0, peak)
        new_high = h[i] > idle_anchor + 1e-9
        if new_high:
            idle_anchor = peak
        else:
            green = last > entry + 1e-9
            step_r = 0.0
            if green and (spec.get("chase_from_fill") or mfe_r + 1e-9 >= ARM_R):
                step_r = 0.05
            elif (not green) and spec.get("red_step"):
                step_r = float(spec["red_step"])
            if step_r > 0:
                raised = stop + 7 * step_r * risk
                ceiling = last - 0.01
                if ceiling > stop:
                    stop = min(raised, ceiling)
        if mfe_r + 1e-9 >= ARM_R and spec.get("give_r"):
            give_px = _give_px(last, risk, spec["give_r"])
            if give_r_clipped(last, risk, spec["give_r"]):
                binds["clip"] += 1
            cand = last - give_px
            peak_px = None
            if spec.get("peak_pct"):
                peak_px = peak * (1.0 - spec["peak_pct"] / 100.0)
                if peak_px > cand + 0.001:
                    binds["peak"] += 1
                elif cand > peak_px + 0.001:
                    binds["give"] += 1
                else:
                    binds["tie"] += 1
                cand = max(cand, peak_px)
            else:
                binds["give"] += 1
            if cand >= last:
                cand = last - 0.01
            if cand > stop:
                stop = cand
    i = i_end
    return _done(i, c[i], "eod", mfe_r, mae_r, ts, i0, peak)


def give_r_clipped(last, risk, give_r):
    raw = give_r * risk
    cap = last * GIVE_MAX_PCT / 100.0
    return GIVE_MAX_PCT > 0 and raw > cap + 1e-9


def _done(i, px, why, mfe, mae, ts, i0, peak):
    return {
        "i": i, "px": float(px), "why": why,
        "mfe_r": mfe, "mae_r": mae, "peak": peak,
        "hold_sec": ts[i] - ts[i0],
    }


def pl_of(entry, exit_px, risk, qty):
    net = (exit_px / entry - 1.0) - COST_RT
    dollars = net * entry * qty
    r = (exit_px - entry) / risk - COST_RT * entry / risk
    return dollars, r


SPECS = [
    ("live", dict(give_r=0.35, peak_pct=0.35, lob=True, dead=True,
                  chase_from_fill=False, red_step=0.0, dump=0)),
    ("g12_eod", dict(give_r=0.12, peak_pct=0.35, lob=False, dead=False,
                     chase_from_fill=True, red_step=0.0, dump=0)),
    ("g10_eod", dict(give_r=0.10, peak_pct=0.35, lob=False, dead=False,
                     chase_from_fill=True, red_step=0.0, dump=0)),
    ("g12_red", dict(give_r=0.12, peak_pct=0.35, lob=False, dead=False,
                     chase_from_fill=True, red_step=0.02, dump=0)),
    ("g12_red_d30", dict(give_r=0.12, peak_pct=0.35, lob=False, dead=False,
                         chase_from_fill=True, red_step=0.02, dump=30)),
    ("g12_red_d40", dict(give_r=0.12, peak_pct=0.35, lob=False, dead=False,
                         chase_from_fill=True, red_step=0.02, dump=40)),
    ("g12_red_d30_1bar", dict(give_r=0.12, peak_pct=0.35, lob=False, dead=False,
                              chase_from_fill=True, red_step=0.02, dump=30,
                              dump_one_bar=True)),
    ("g12_peakoff", dict(give_r=0.12, peak_pct=0.0, lob=False, dead=False,
                         chase_from_fill=True, red_step=0.02, dump=0)),
]


def summarize(rows):
    if not rows:
        return {"n": 0, "win": 0.0, "mean_r": 0.0, "med_mfe": 0.0, "dollars": 0.0}
    rs_ = [r["R"] for r in rows]
    mfes = [r["mfe"] for r in rows]
    return {
        "n": len(rows),
        "win": 100.0 * sum(1 for r in rs_ if r > 0) / len(rows),
        "mean_r": statistics.fmean(rs_),
        "med_mfe": statistics.median(mfes),
        "dollars": sum(r["dollars"] for r in rows),
    }


def half_dollars(rows, mid_day):
    a = sum(r["dollars"] for r in rows if r["day"] < mid_day)
    b = sum(r["dollars"] for r in rows if r["day"] >= mid_day)
    na = sum(1 for r in rows if r["day"] < mid_day)
    nb = sum(1 for r in rows if r["day"] >= mid_day)
    return a, na, b, nb


def run_set(trades, fast_of, spec):
    """trades: dicts with B, i, px, stop, risk, qty, day, sym."""
    binds = {"give": 0, "peak": 0, "tie": 0, "clip": 0}
    out = []
    for t in trades:
        sim = simulate(
            t["B"], t["i"], t["px"], t["stop"], t["risk"],
            fast_of(t), spec, binds)
        dollars, r = pl_of(t["px"], sim["px"], t["risk"], t["qty"])
        out.append({
            "R": r, "dollars": dollars, "mfe": sim["mfe_r"],
            "why": sim["why"], "day": t["day"], "sym": t["sym"],
        })
    return out, binds


def main():
    with open(CACHE, "rb") as f:
        cache = pickle.load(f)
    prepared = {}
    signals = []
    for (sym, day), B in cache.items():
        if not (isinstance(B, (list, tuple)) and len(B) >= 6):
            continue
        if len(B[0]) < 120:
            continue
        # B is (ts, open, high, low, close, volume).
        fast = smooth_wr(B[2], B[3], B[4], 21, 7.0)
        slow = smooth_wr(B[2], B[3], B[4], 112, 3.0)
        prepared[(sym, day)] = (B, fast, slow)
        for sig in find_signals(B, fast, slow, sym, day):
            px = sig["px"]
            stop = px * 0.95
            sig.update(B=B, stop=stop, risk=px - stop, qty=1.0, fast=fast)
            signals.append(sig)
    by_kind = {
        "square": [s for s in signals if s["kind"] == "square"],
        "oversold": [s for s in signals if s["kind"] == "oversold"],
        "both": signals,
    }
    fills = []
    with open(OUTCOMES) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            sym = str(r.get("symbol") or "")
            et = r.get("entry_time")
            px = r.get("entry_price")
            if not rs.SYM_RE.match(sym) or not isinstance(et, (int, float)) or not px:
                continue
            day = datetime.fromtimestamp(float(et), ET).date().isoformat()
            got = prepared.get((sym, day))
            if not got:
                continue
            B, fast, _slow = got
            i0 = bars.index_at(B[0], float(et))
            if i0 < 0 or i0 + 2 >= len(B[0]):
                continue
            ep = float(px)
            sp = float(r.get("stop_price") or 0) or ep * 0.95
            if sp >= ep:
                sp = ep * 0.95
            fills.append({
                "B": B, "i": i0, "px": ep, "stop": sp, "risk": ep - sp,
                "qty": float(r.get("total_qty") or 1) or 1.0,
                "day": day, "sym": sym, "fast": fast,
            })

    days = sorted({s["day"] for s in signals} | {t["day"] for t in fills})
    mid = days[len(days) // 2] if days else ""
    lines = []
    lines.append("# Runway / oversold study 2026-09-29")
    lines.append("")
    lines.append(
        f"Bars: {len(prepared)} symbol-days in the runway cache. "
        f"Synthetic signals: {len(by_kind['square'])} square, "
        f"{len(by_kind['oversold'])} leave-oversold, {len(signals)} combined. "
        f"Recorded fills with bars: {len(fills)}. "
        f"Sample split at {mid}. Cost {COST_RT*100:.2f}% round trip. "
        "1R on a synthetic entry is 5% of price."
    )
    lines.append("")
    lines.append(
        "1-minute bars understate the live chase (seven idle steps stand in "
        "for a quiet minute). Two in-zone bars stand in for two arm polls, "
        "which is stricter than the live debounce, so short-lived triangles "
        "are missing."
    )
    lines.append("")
    header = "| set | exit | n | win% | mean R | med MFE | net $ | $ before | n | $ after | n |"
    sep = "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    lines.append(header)
    lines.append(sep)
    results = {}
    for label, trades in (
        ("square", by_kind["square"]),
        ("oversold", by_kind["oversold"]),
        ("both", by_kind["both"]),
        ("fills", fills),
    ):
        for name, spec in SPECS:
            rows, binds = run_set(trades, lambda t: t["fast"], spec)
            s = summarize(rows)
            a, na, b, nb = half_dollars(rows, mid)
            results[(label, name)] = (s, binds, a, na, b, nb, rows)
            lines.append(
                f"| {label} | {name} | {s['n']} | {s['win']:.1f} | {s['mean_r']:.3f} | "
                f"{s['med_mfe']:.3f} | {s['dollars']:.0f} | {a:.0f} | {na} | {b:.0f} | {nb} |"
            )
    lines.append("")
    lines.append("## Which leash binds")
    lines.append("")
    lines.append(
        "Counted on armed bars (MFE ≥ 0.06R). "
        "`peak` means the 0.35% under the high is tighter than give_r. "
        "`clip` means give_r × R was wider than the 1% price cap."
    )
    lines.append("")
    lines.append("| set | exit | give binds | peak binds | tie | 1% cap clips |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for label in ("both", "fills"):
        for name, _spec in SPECS:
            _s, binds, *_rest = results[(label, name)]
            lines.append(
                f"| {label} | {name} | {binds['give']} | {binds['peak']} | "
                f"{binds['tie']} | {binds['clip']} |"
            )
    lines.append("")
    lines.append("## Exit reasons (both-arms signals)")
    lines.append("")
    for name, _spec in SPECS:
        rows = results[("both", name)][5] if False else results[("both", name)][-1]
        counts = {}
        for r in rows:
            counts[r["why"]] = counts.get(r["why"], 0) + 1
        bits = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        lines.append(f"- {name}: {bits}")
    lines.append("")
    text = "\n".join(lines) + "\n"
    out_path = os.path.join(ROOT, "docs", "RUNWAY_OS_STUDY_2026-09-29.md")
    with open(out_path, "w") as f:
        f.write(text)
    print(text)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
