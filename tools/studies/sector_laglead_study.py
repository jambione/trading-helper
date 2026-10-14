#!/usr/bin/env python3
"""Sector lead-lag. Spec: docs/studies/sector_laglead_prereg.json.

When a sector ETF moves up on its own (vs SPY), buy its large holdings that
have not moved yet, at the next minute's SIP open, hold 10 minutes.

Universe: HOLDINGS below - about 15 large holdings per sector ETF, each name
in exactly one sector, frozen 2026-10-14 from knowledge dated before the test
window (no board membership, no hindsight on the scored days).

  --fetch    daily bars + IEX/SIP minute bars for the universe, the ETFs and SPY
             (mini only, after 16:15 ET)
  (default)  inventory only, no returns
  --score    after the prereg has a result-skeptic READY
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pickle
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402

ROOT = E.ROOT
sys.path.insert(0, ROOT)
OUT_DIR = os.path.join(ROOT, "ai_reports", "sector_laglead")
DAILY = os.path.join(OUT_DIR, "daily.pkl")

# Frozen 2026-10-14. Approximate top holdings (not weights), each name in one sector:
# semiconductors sit in SMH, not XLK; Alphabet/Meta in XLC; Amazon/Tesla in XLY.
HOLDINGS: dict[str, tuple[str, ...]] = {
    "SMH": ("NVDA", "TSM", "AVGO", "AMD", "ASML", "QCOM", "TXN", "AMAT", "MU", "LRCX", "KLAC", "ADI", "INTC", "MRVL", "ARM"),
    "XLK": ("AAPL", "MSFT", "ORCL", "CRM", "ADBE", "CSCO", "ACN", "IBM", "NOW", "INTU", "PLTR", "ANET", "APH", "PANW", "CRWD"),
    "XLC": ("META", "GOOGL", "NFLX", "TMUS", "DIS", "VZ", "T", "CMCSA", "CHTR", "EA", "TTWO", "WBD", "OMC", "LYV"),
    "XLV": ("LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "ISRG", "AMGN", "DHR", "PFE", "BSX", "SYK", "GILD", "VRTX"),
    "XLF": ("JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "SPGI", "BLK", "C", "SCHW", "PGR", "CB"),
    "XLE": ("XOM", "CVX", "COP", "EOG", "WMB", "SLB", "PSX", "MPC", "OKE", "KMI", "VLO", "OXY", "FANG", "BKR"),
    "XLU": ("NEE", "SO", "DUK", "CEG", "VST", "AEP", "SRE", "D", "EXC", "XEL", "PEG", "ETR", "PCG", "ED"),
    "XLRE": ("PLD", "AMT", "WELL", "EQIX", "SPG", "O", "PSA", "CCI", "DLR", "CBRE", "VICI", "EXR", "AVB"),
    "XLY": ("AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "SBUX", "NKE", "ORLY", "CMG", "ABNB", "MAR", "GM", "AZO"),
    "XLP": ("COST", "WMT", "PG", "KO", "PEP", "PM", "MDLZ", "MO", "CL", "TGT", "KMB", "KDP", "KHC", "GIS", "SYY"),
    "XLI": ("GE", "CAT", "RTX", "UBER", "HON", "UNP", "ETN", "BA", "DE", "LMT", "ADP", "GD", "WM", "UPS", "TT"),
    "XLB": ("LIN", "SHW", "ECL", "APD", "FCX", "NEM", "CTVA", "DD", "NUE", "PPG", "DOW", "VMC", "MLM", "IFF"),
}
ETFS = tuple(HOLDINGS)
SPY = "SPY"
DAYS = ["2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16",
        "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
        "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]
SPLIT_END_A = "2026-09-24"
WIN_START, WIN_END = 9 * 60 + 50, 15 * 60 + 20
LOOK = 5
ETF_MIN, ETF_MIN_INFO, REL_MIN = 0.0025, 0.0050, 0.0010
LAG_MAX, LAG_MIN = 0.0005, -0.0050
LEADER_MIN = 0.0050
REFRACT, CTRL_EXCL = 30, 30
HOLD = 10
COST = 0.0010
PX_MIN, ADV_MIN, ADV_N = 10.0, 20e6, 20
MIN_FIRES, MIN_DAYS, MIN_EPISODES = 100, 10, 20
MIN_CTRL_COVER = 0.80
MAX_MISS = 0.05


def universe() -> dict[str, str]:
    out = {}
    for etf, names in HOLDINGS.items():
        for s in names:
            assert s not in out, f"{s} in two sectors"
            out[s] = etf
    return out


def holdings_digest() -> str:
    return hashlib.sha256(repr(sorted((k, v) for k, v in HOLDINGS.items())).encode()).hexdigest()[:12]


# ── pure ─────────────────────────────────────────────────────────────────────

def close_at(series: dict, minute: int, back: int = 15) -> float | None:
    for m in range(minute, minute - back - 1, -1):
        if m in series:
            return series[m][1]
    return None


def ret5(series: dict, m: int) -> float | None:
    a, b = close_at(series, m - LOOK), close_at(series, m)
    if not a or b is None:
        return None
    return b / a - 1


def sector_moves(etf: dict, spy: dict, m: int, etf_min: float = ETF_MIN) -> bool:
    r, s = ret5(etf, m), ret5(spy, m)
    eps = 1e-9            # thresholds are inclusive; float noise must not flip an on-the-bar minute
    return r is not None and s is not None and r + eps >= etf_min and (r - s) + eps >= REL_MIN


def is_laggard(stock: dict, m: int) -> bool:
    r = ret5(stock, m)
    return r is not None and LAG_MIN - 1e-9 <= r <= LAG_MAX + 1e-9


def fires(minutes: list[int], etf: dict, spy: dict, members: dict[str, dict],
          etf_min: float = ETF_MIN) -> list[tuple[str, int, int]]:
    """(symbol, signal minute, episode id). An episode = a run of consecutive qualifying ETF minutes.
    30-minute refractory per symbol (first fire wins)."""
    last: dict[str, int] = {}
    out = []
    episode, prev_q = 0, None
    for m in minutes:
        if not (WIN_START <= m <= WIN_END) or not sector_moves(etf, spy, m, etf_min):
            continue
        if prev_q is None or m != prev_q + 1:
            episode += 1
        prev_q = m
        for sym in sorted(members):
            if sym in last and m - last[sym] < REFRACT:
                continue
            if is_laggard(members[sym], m):
                out.append((sym, m, episode))
                last[sym] = m
    return out


def control_minute(sym: str, day: str, m: int, member: dict, etf: dict, spy: dict, sip: dict,
                   fire_minutes: list[int], hold: int = HOLD) -> int | None:
    """Same name, same day, within 60 min either side of the fire (and inside the window): a minute
    where the name is in the laggard band but its sector is NOT moving, more than 30 min from any of
    this name's fires, with an exit bar. Returns the SIGNAL minute k (entry k+1)."""
    elig = [k for k in range(m - 60, m + 61)
            if WIN_START <= k <= WIN_END and all(abs(k - f) > CTRL_EXCL for f in fire_minutes)
            and is_laggard(member, k) and not sector_moves(etf, spy, k)
            and E.markout(sip, k + 1, hold) is not None]
    if not elig:
        return None
    rng = random.Random(f"{sym}|{day}|{m}|laglead-v2")
    return rng.choice(elig)


def adv_ok(rows: list, day: str) -> bool | None:
    prior = [r for r in rows if r[0] < day]
    if len(prior) < ADV_N:
        return None
    return statistics.mean(c * v for _d, c, v in prior[-ADV_N:]) >= ADV_MIN


def half_verdict(h: dict) -> str:
    """h: net10, t10, net20, lift, lift_t, net_m2, n, days, episodes, net_drop3, net_dropetf."""
    if (h["n"] < MIN_FIRES or h["days"] < MIN_DAYS or h["episodes"] < MIN_EPISODES or h["net10"] is None
            or h.get("ctrl_cover", 1.0) < MIN_CTRL_COVER):
        return "UNDERPOWERED"
    ok = (h["net10"] > 0 and (h["t10"] or 0) >= 2.0 and (h["net20"] or -1) > 0
          and (h["lift"] or -1) > 0 and (h["lift_t"] or 0) >= 2.0
          and (h["net_m2"] or -1) > 0
          and (h["net_drop3"] or -1) > 0 and (h["net_dropetf"] or -1) > 0)
    return "PASS" if ok else "FAIL"


# ── data (mini, after 16:15) ─────────────────────────────────────────────────

def _wait_after_close() -> None:
    while True:
        m = E._et_min(time.time())
        if m >= 16 * 60 + 15 or m < 3 * 60 + 30:
            return
        print("  waiting for 16:15 ET (the desk shares the data budget)", file=sys.stderr, flush=True)
        time.sleep(300)


def do_fetch() -> None:
    import bars
    import board_names_study as BN
    os.makedirs(OUT_DIR, exist_ok=True)
    _wait_after_close()
    want = sorted(set(universe()) | set(ETFS) | {SPY})
    if not os.path.exists(DAILY):
        errs: list = []
        daily = BN.fetch_daily(want, "2026-07-15", (datetime.fromisoformat(DAYS[-1]) + timedelta(days=1)).strftime("%Y-%m-%d"), errs)
        if errs:
            raise SystemExit(f"daily fetch errors {errs}; not cached, rerun")
        pickle.dump(daily, open(DAILY, "wb"))
        print(f"daily: {len(daily)}/{len(want)}", flush=True)
    cl = bars.client()
    for day in DAYS:
        p = os.path.join(OUT_DIR, f"{day}.pkl")
        if os.path.exists(p):
            continue
        _wait_after_close()
        errs = []
        sip = E._fetch_feed(cl, want, day, "sip", errs)
        iex = E._fetch_feed(cl, want, day, "iex", errs)
        if errs:
            print(f"{day}: fetch errors {errs} - not cached", flush=True)
            continue
        pickle.dump({"sip": sip, "iex": iex, "holdings": holdings_digest()}, open(p, "wb"))
        print(f"{day}: sip {len(sip)}/{len(want)} iex {len(iex)}/{len(want)}", flush=True)


# ── scoring ──────────────────────────────────────────────────────────────────

def load() -> tuple[dict, dict]:
    daily = pickle.load(open(DAILY, "rb")) if os.path.exists(DAILY) else {}
    caches = {}
    for day in DAYS:
        p = os.path.join(OUT_DIR, f"{day}.pkl")
        if os.path.exists(p):
            caches[day] = pickle.load(open(p, "rb"))
    return daily, caches


def build(daily: dict, caches: dict, etf_min: float = ETF_MIN, feed: str = "iex") -> tuple[list[dict], Counter, list[str]]:
    uni = universe()
    problems: list[str] = []
    missing_days = [d for d in DAYS if d not in caches]
    if missing_days:
        problems.append(f"days without bars: {missing_days}")
    # A name with no SIP bars on ANY day (renamed / delisted / wrong ticker) leaves the universe, listed.
    never = sorted(s for s in uni if all(s not in caches[d]["sip"] for d in caches))
    drops = Counter(removed_never_traded=len(never))
    rows = []
    for day, cache in sorted(caches.items()):
        sip = {s: E.sip_map(f) for s, f in cache["sip"].items() if f is not None}
        det = {s: E.sip_map(f) for s, f in cache[feed].items() if f is not None}
        if SPY not in det or SPY not in sip:
            problems.append(f"{day}: no SPY bars")
            continue
        members_by_etf = defaultdict(dict)
        for s, etf in uni.items():
            if s in never:
                continue
            drops["name_days"] += 1
            ok = adv_ok(daily.get(s, []), day)
            if ok is None:
                drops["adv_unknown"] += 1
                continue
            if s not in det or s not in sip:
                drops["no_bars"] += 1
                continue
            open930 = sip[s].get(E.RTH_START)
            if not ok or open930 is None or open930[0] < PX_MIN:      # raw 09:30 open, not split-adjusted
                drops["thin_or_cheap"] += 1
                continue
            members_by_etf[etf][s] = det[s]
        for etf, members in members_by_etf.items():
            if etf not in det or etf not in sip:
                problems.append(f"{day}: no {etf} bars")
                continue
            fl = fires(list(range(WIN_START, WIN_END + 1)), det[etf], det[SPY], members, etf_min)
            by_sym = defaultdict(list)
            for sym, m, _ep in fl:
                by_sym[sym].append(m)
            for sym, m, ep in fl:
                g = E.markout(sip[sym], m + 1)
                if g is None:
                    drops["no_exit_bar"] += 1
                    continue
                rows.append({"day": day, "sym": sym, "etf": etf, "m": m, "episode": f"{day}|{etf}|{ep}",
                             "gross": g, "gross_m2": E.markout(sip[sym], m + 2),
                             "sip": sip[sym], "etf_sip": sip[etf], "member": members[sym],
                             "etf_det": det[etf], "spy_det": det[SPY], "members": members, "sips": sip,
                             "fires_sym": by_sym[sym], "fresh": m in members[sym],
                             "px": close_at(sip[sym], m)})
    lost = drops["adv_unknown"] + drops["no_bars"]
    if drops["name_days"] and lost > MAX_MISS * drops["name_days"]:
        problems.append(f"{lost}/{drops['name_days']} name-days lack bars or ADV (> 5%)")
    if never:
        drops["never_traded_names"] = len(never)
    return rows, drops, problems + ([f"removed (no bars on any day): {never}"] if never else [])


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def _t(x):
    return "—" if x is None else f"{x:+.2f}"


def _de(rows, fn):
    by = defaultdict(list)
    for r in rows:
        x = fn(r)
        if x is not None:
            by[r["day"]].append(x)
    return E.day_equal(by)


def ctrl_lift(r) -> float | None:
    k = control_minute(r["sym"], r["day"], r["m"], r["member"], r["etf_det"], r["spy_det"], r["sip"], r["fires_sym"])
    if k is None:
        return None
    return r["gross"] - E.markout(r["sip"], k + 1)


def half_stats(part: list[dict]) -> dict:
    n10 = _de(part, lambda r: r["gross"] - COST)
    n20 = _de(part, lambda r: r["gross"] - 2 * COST)
    lifts = [ctrl_lift(r) for r in part]
    cover = (sum(x is not None for x in lifts) / len(lifts)) if lifts else 0.0
    lift = _de([dict(r, _lift=x) for r, x in zip(part, lifts)], lambda r: r["_lift"])
    m2 = _de(part, lambda r: None if r["gross_m2"] is None else r["gross_m2"] - COST)
    by_day = defaultdict(list)
    for r in part:
        by_day[r["day"]].append(r["gross"] - COST)
    top3 = sorted(by_day, key=lambda d: statistics.mean(by_day[d]), reverse=True)[:3]
    drop3 = _de([r for r in part if r["day"] not in top3], lambda r: r["gross"] - COST)
    big = Counter(r["etf"] for r in part).most_common(1)
    dropetf = _de([r for r in part if not big or r["etf"] != big[0][0]], lambda r: r["gross"] - COST)
    return {"net10": n10[0], "t10": n10[1], "n": n10[2], "days": n10[3], "net20": n20[0],
            "lift": lift[0], "lift_t": lift[1], "net_m2": m2[0], "net_drop3": drop3[0], "net_dropetf": dropetf[0],
            "episodes": len({r["episode"] for r in part}), "big_etf": big[0][0] if big else None,
            "ctrl_cover": cover,
            "gross": _de(part, lambda r: r["gross"])[0]}


def report(rows: list[dict], drops: Counter, info_rows: dict) -> str:
    L = [f"# Sector lead-lag (holdings universe {holdings_digest()})", "",
         f"fires {len(rows)}; drops {dict(drops)}", "",
         "| half | fires | episodes | sessions | gross | net 10 bp | t | net 20 bp | lift vs no-sector-move minute | t "
         "| control coverage | net, entry m+2 | net w/o top-3 days | net w/o largest ETF | verdict |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    verdicts = []
    for h in "AB":
        st = half_stats([r for r in rows if (r["day"] <= SPLIT_END_A) == (h == "A")])
        v = half_verdict(st)
        verdicts.append(v)
        L.append(f"| {h} | {st['n']} | {st['episodes']} | {st['days']} | {_bp(st['gross'])} | {_bp(st['net10'])} "
                 f"| {_t(st['t10'])} | {_bp(st['net20'])} | {_bp(st['lift'])} | {_t(st['lift_t'])} | {st['ctrl_cover']:.0%} | {_bp(st['net_m2'])} "
                 f"| {_bp(st['net_drop3'])} | {_bp(st['net_dropetf'])} ({st['big_etf']}) | {v} |")
    final = "PASS" if all(v == "PASS" for v in verdicts) else ("FAIL" if "FAIL" in verdicts else "UNDERPOWERED")
    L += ["", f"**Sector lead-lag: {final}**", "", "### Information only", ""]

    def line(label, part, fn):
        mu, t, n, nd = _de(part, fn)
        L.append(f"- {label}: {_bp(mu)} bp (t {_t(t)}, n {n}, {nd} sessions)")

    line("lift vs a later same-name minute (entry+1..+60)", rows, lambda r: (
        None if (k := E.control_minute(r["sym"], r["day"], "LAG", r["sip"], set(), r["m"] + 1)) is None
        else r["gross"] - E.markout(r["sip"], k)))

    def vs_leaders(r):
        lead = [s for s, ser in r["members"].items() if s != r["sym"] and (x := ret5(ser, r["m"])) is not None
                and x >= LEADER_MIN and s in r["sips"]]
        rng = random.Random(f"{r['sym']}|{r['day']}|{r['m']}|leader")
        rng.shuffle(lead)
        for s in lead:
            x = E.markout(r["sips"][s], r["m"] + 1)
            if x is not None:
                return r["gross"] - x
        return None
    line("lift vs a same-sector LEADER at the same minute", rows, vs_leaders)
    line("lift vs the sector ETF's next 10 minutes (<= 0 with a PASS = sector momentum: trade the ETF)", rows,
         lambda r: None if (x := E.markout(r["etf_sip"], r["m"] + 1)) is None else r["gross"] - x)
    line("30-minute markout, net 10 bp", rows, lambda r: (
        None if (x := E.markout(r["sip"], r["m"] + 1, 30)) is None else x - COST))
    line("fresh IEX bar at the signal minute, net 10 bp", [r for r in rows if r["fresh"]], lambda r: r["gross"] - COST)
    line("carried IEX close (silent 1+ min), net 10 bp", [r for r in rows if not r["fresh"]], lambda r: r["gross"] - COST)
    line("price >= $30, net 10 bp", [r for r in rows if r["px"] and r["px"] >= 30], lambda r: r["gross"] - COST)
    for etf in ETFS:
        part = [r for r in rows if r["etf"] == etf]
        if part:
            line(f"{etf}, net 10 bp", part, lambda r: r["gross"] - COST)
    for label, rs in info_rows.items():
        line(label, rs, lambda r: r["gross"] - COST)
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        do_fetch()
        return
    daily, caches = load()
    print(f"universe {len(universe())} names in {len(ETFS)} sectors (holdings {holdings_digest()}); "
          f"bar days {len(caches)}/{len(DAYS)}; daily symbols {len(daily)}")
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    rows, drops, problems = build(daily, caches)
    hard = [p for p in problems if not p.startswith("removed")]
    if hard:
        raise SystemExit("ABORT - no verdict:\n" + "\n".join(hard))
    info = {
        f"ETF threshold +{ETF_MIN_INFO * 100:.2f}% instead of +{ETF_MIN * 100:.2f}%": build(daily, caches, ETF_MIN_INFO)[0],
        "SIP-detected version (not computable live)": build(daily, caches, ETF_MIN, "sip")[0],
    }
    md = report(rows, drops, info)
    md += "\n" + "\n".join(f"- {p}" for p in problems)
    print(md)
    with open(os.path.join(OUT_DIR, "result.md"), "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
