"""Bullish Bob LIVE (Trader Bro) call-out forward-return study.

Question: when the room caller names a symbol on the mic, does that timestamp
have capturable edge after the real SIP spread?

Input: ai_reports/bb_live.jsonl (archived by dashboard._archive_bb_live).
Data: Alpaca historical SIP 1-min bars + SIP NBBO quotes at entry.
Usage (on the mini, where the archive and keys live):

  export PYTHONPATH=$HOME/repo/trading-helper:$HOME/repo/trading-helper/tools
  cd /tmp/bb && mkdir -p /tmp/bb
  nice -n 15 $HOME/repo/trading-helper/.venv/bin/python -u \\
    $HOME/repo/trading-helper/tools/studies/bb_live_callouts_study.py all

Phases: load | fetch | analyze | report | all
Caches under $BB_DIR (default /tmp/bb): calls.json, sip/, spreads.json, grid.pkl, report.txt
"""
from __future__ import annotations

import json, math, os, pickle, random, re, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OUT = os.environ.get("BB_DIR", "/tmp/bb")
REPO = os.environ.get("TH_REPO", str(Path.home() / "repo/trading-helper"))
ARCHIVE = os.environ.get("BB_LIVE_JSONL", str(Path(REPO) / "ai_reports/bb_live.jsonl"))
SLEEP = 0.28
HOLDS = (5, 15, 30)
END_FLAT = 15 * 60 + 50
MORNING_END = 11 * 60
NBASE = 5

# Same stance filters the desk uses for seeding (ai_entry_watch).
_EXIT = re.compile(r"\b(?:sold|sell|selling|out|stopped|closed|trimmed|was\s+a|were)\b", re.I)
_AVOID = re.compile(r"(?:not\s+for\s+me|avoid|\blg\s+float\b|larger\s+float)", re.I)
# Soft "lean in" language (exploratory bucket, not a desk gate).
_LEAN = re.compile(
    r"\b(?:long|bought|buying|adder|adding|entry|break(?:ing|out)?|going|"
    r"test(?:ing)?|retest|hod|high\s+of\s+day|on\s+watch|watching|runner|squeeze)\b",
    re.I,
)


def _cfg():
    sys.path[:0] = [REPO, f"{REPO}/tools"]
    from config import load_config
    c = load_config() or {}
    return c.get("api_key"), c.get("secret_key")


def actionable(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    return not (_EXIT.search(t) or _AVOID.search(t))


def lean_in(text: str) -> bool:
    return bool(_LEAN.search(str(text or "")))


def call_dt(r: dict) -> datetime | None:
    """Best available call timestamp in ET.

    Prefer Discord's own stamp when present (`said` as epoch or HH:MM with archive day).
    Fall back to `at`, then archive `time` (UTC).
    """
    said = r.get("said")
    if said not in (None, ""):
        try:
            return datetime.fromtimestamp(float(said), ET)
        except (TypeError, ValueError):
            pass
    at = r.get("at")
    if at not in (None, ""):
        try:
            return datetime.fromtimestamp(float(at), ET)
        except (TypeError, ValueError):
            pass
    t = r.get("time")
    if t:
        try:
            return datetime.fromisoformat(str(t).replace("Z", "+00:00")).astimezone(ET)
        except ValueError:
            pass
    return None


def load_calls() -> list[dict]:
    rows = []
    seen = set()
    for line in open(ARCHIVE):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        sym = str(r.get("ticker") or "").upper().lstrip("$")
        if not sym or len(sym) > 6:
            continue
        dt = call_dt(r)
        if dt is None:
            continue
        text = str(r.get("text") or "")
        key = (sym, text, int(dt.timestamp()))
        if key in seen:
            continue
        seen.add(key)
        day = dt.date().isoformat()
        minute = dt.hour * 60 + dt.minute
        rows.append({
            "sym": sym,
            "day": day,
            "dt": dt.isoformat(),
            "unix": dt.timestamp(),
            "minute": minute,
            "text": text,
            "actionable": actionable(text),
            "lean": lean_in(text),
            "arch_px": r.get("price"),
            "price_src": r.get("price_src"),
        })
    rows.sort(key=lambda x: (x["day"], x["unix"], x["sym"]))
    Path(OUT).mkdir(parents=True, exist_ok=True)
    json.dump(rows, open(f"{OUT}/calls.json", "w"), indent=0)
    n_act = sum(1 for r in rows if r["actionable"])
    n_lean = sum(1 for r in rows if r["actionable"] and r["lean"])
    days = sorted({r["day"] for r in rows})
    print(f"calls {len(rows)} actionable {n_act} lean {n_lean} sessions {len(days)} "
          f"{days[0]}..{days[-1]} name-days {len({(r['day'], r['sym']) for r in rows})}", flush=True)
    return rows


def _client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(*_cfg())


def _bars(cl, syms, start, end):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    for attempt in range(4):
        try:
            bs = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=syms, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc),
                feed=DataFeed.SIP))
            time.sleep(SLEEP)
            out = {}
            for s, rows in (bs.data or {}).items():
                out[s] = [(int(r.timestamp.timestamp()), float(r.open), float(r.high),
                           float(r.low), float(r.close), float(r.volume)) for r in rows]
            return out
        except Exception as e:  # noqa: BLE001
            print("bars", type(e).__name__, str(e)[:100], flush=True)
            time.sleep(3 * (attempt + 1))
    return {}


def _spread(cl, sym, t0, secs=60):
    """Median (ask-bid)/mid in bp over [t0, t0+secs]. None if no NBBO."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    for attempt in range(3):
        try:
            q = cl.get_stock_quotes(StockQuotesRequest(
                symbol_or_symbols=sym,
                start=t0.astimezone(timezone.utc),
                end=(t0 + timedelta(seconds=secs)).astimezone(timezone.utc),
                feed=DataFeed.SIP))
            time.sleep(SLEEP)
            rows = (q.data or {}).get(sym) or []
            bps = []
            for r in rows:
                bb, a = float(r.bid_price), float(r.ask_price)
                if bb > 0 and a >= bb:
                    bps.append((a - bb) / ((a + bb) / 2) * 1e4)
            if not bps and attempt == 0:
                # standing book: look back 5 min
                q2 = cl.get_stock_quotes(StockQuotesRequest(
                    symbol_or_symbols=sym,
                    start=(t0 - timedelta(minutes=5)).astimezone(timezone.utc),
                    end=t0.astimezone(timezone.utc), feed=DataFeed.SIP))
                time.sleep(SLEEP)
                rows = (q2.data or {}).get(sym) or []
                for r in rows:
                    bb, a = float(r.bid_price), float(r.ask_price)
                    if bb > 0 and a >= bb:
                        bps.append((a - bb) / ((a + bb) / 2) * 1e4)
            return float(sorted(bps)[len(bps) // 2]) if bps else None
        except Exception as e:  # noqa: BLE001
            print("quotes", sym, type(e).__name__, str(e)[:80], flush=True)
            time.sleep(3 * (attempt + 1))
    return None


def fetch_main():
    calls = json.load(open(f"{OUT}/calls.json")) if os.path.exists(f"{OUT}/calls.json") else load_calls()
    cl = _client()
    sip_dir = Path(OUT) / "sip"
    sip_dir.mkdir(exist_ok=True)
    byday = defaultdict(set)
    for r in calls:
        byday[r["day"]].add(r["sym"])
    spreads = json.load(open(f"{OUT}/spreads.json")) if os.path.exists(f"{OUT}/spreads.json") else {}
    for d, syms in sorted(byday.items()):
        pkl = sip_dir / f"{d}.pkl"
        have = pickle.load(open(pkl, "rb")) if pkl.exists() else {}
        need = sorted(s for s in syms if s not in have)
        if need:
            dd = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=ET)
            for i in range(0, len(need), 40):
                b = need[i:i + 40]
                got = _bars(cl, b, dd.replace(hour=4), dd.replace(hour=16, minute=1))
                for s in b:
                    have[s] = got.get(s) or []
                print("bars", d, i, len(b), "got", sum(1 for s in b if have[s]), flush=True)
            pickle.dump(have, open(pkl, "wb"))
        # spreads at each call minute (batch by unique minute)
        day_calls = [r for r in calls if r["day"] == d]
        for r in day_calls:
            key = f"{r['sym']}|{r['day']}|{r['minute']}"
            if key in spreads:
                continue
            t0 = datetime.fromisoformat(r["dt"])
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=ET)
            spreads[key] = _spread(cl, r["sym"], t0)
            if len(spreads) % 25 == 0:
                json.dump(spreads, open(f"{OUT}/spreads.json", "w"))
                print("spreads", len(spreads), key, spreads[key], flush=True)
        json.dump(spreads, open(f"{OUT}/spreads.json", "w"))
        print("day done", d, "syms", len(syms), flush=True)
    print("fetch done spreads", len(spreads), flush=True)


def _bar_index(bars, unix):
    """First bar with timestamp >= unix; bars are (ts,o,h,l,c,v)."""
    for i, b in enumerate(bars):
        if b[0] >= unix - 1:  # 1s slack
            return i
    return None


def _fwd(bars, i0, E, hold_min, slip_bp, spr_bp):
    """Gross/net bp over hold_min bars from entry open E; exit at close of last real bar."""
    if i0 is None or E is None or E <= 0 or not bars:
        return None
    j = min(i0 + hold_min - 1, len(bars) - 1)
    if j < i0:
        return None
    px = bars[j][4]
    gross = (px / E - 1) * 1e4
    cost = (spr_bp or 0) + slip_bp  # full round trip quoted spread + under-$5 tick
    return gross, gross - cost, j


def _mfe_mae(bars, i0, E, n=30):
    if i0 is None or E is None or E <= 0:
        return None, None
    w = bars[i0:i0 + n]
    if not w:
        return None, None
    hi = max(b[2] for b in w)
    lo = min(b[3] for b in w)
    return (hi / E - 1) * 1e4, (lo / E - 1) * 1e4


def _entry_open(bars, unix):
    i = _bar_index(bars, unix)
    if i is None:
        return None, None, None
    # buy next bar's open when call falls inside a bar; if exactly on open, use this open
    if i + 1 < len(bars) and bars[i][0] < unix:
        i = i + 1
    return i, bars[i][1], bars[i][0]


def analyze_main():
    calls = json.load(open(f"{OUT}/calls.json"))
    spreads = json.load(open(f"{OUT}/spreads.json"))
    sip_dir = Path(OUT) / "sip"
    rng = random.Random(7)
    events = []
    miss = Counter()

    # first actionable call per (day, sym)
    first_act = {}
    for r in calls:
        if not r["actionable"]:
            continue
        k = (r["day"], r["sym"])
        if k not in first_act or r["unix"] < first_act[k]["unix"]:
            first_act[k] = r

    days = sorted({r["day"] for r in calls})
    dix = {d: i for i, d in enumerate(days)}
    day_bars = {}

    def bars_for(day, sym):
        if day not in day_bars:
            pkl = sip_dir / f"{day}.pkl"
            if not pkl.exists():
                day_bars[day] = None
            else:
                day_bars[day] = pickle.load(open(pkl, "rb"))
        have = day_bars[day]
        if have is None:
            return None
        return have.get(sym) or []

    def score_call(r, tag):
        bars = bars_for(r["day"], r["sym"])
        if bars is None:
            miss["no_sip_day"] += 1
            return
        if len(bars) < 5:
            miss["thin_bars"] += 1
            return
        i0, E, ets = _entry_open(bars, r["unix"])
        if i0 is None or E is None or E <= 0:
            miss["no_entry"] += 1
            return
        sk = f"{r['sym']}|{r['day']}|{r['minute']}"
        spr = spreads.get(sk)
        if spr is None:
            miss["no_spread"] += 1
            # still score gross-only with spr=0 flagged
            spr = 0.0
            no_spr = 1
        else:
            no_spr = 0
        slip = 1e4 * (0.01 / E) if E < 5 else 0.0
        row = {
            "tag": tag, "sym": r["sym"], "day": r["day"], "di": dix[r["day"]],
            "minute": r["minute"], "E": E, "spr": spr, "no_spr": no_spr,
            "arch_px": r.get("arch_px"), "lean": int(r["lean"]),
            "text": r["text"][:80],
        }
        for h in HOLDS:
            g = _fwd(bars, i0, E, h, slip, spr)
            if g is None:
                row[f"g{h}"] = row[f"n{h}"] = None
            else:
                row[f"g{h}"], row[f"n{h}"], _ = g
        # hold to 11:00 and to 15:50
        for name, target in (("t11", MORNING_END), ("t1550", END_FLAT)):
            # find last bar with clock minute <= target
            j = None
            for k in range(i0, len(bars)):
                tt = datetime.fromtimestamp(bars[k][0], ET)
                if tt.hour * 60 + tt.minute <= target:
                    j = k
                else:
                    break
            if j is None or j < i0:
                row[f"g_{name}"] = row[f"n_{name}"] = None
            else:
                px = bars[j][4]
                gross = (px / E - 1) * 1e4
                row[f"g_{name}"] = gross
                row[f"n_{name}"] = gross - spr - slip
        mfe, mae = _mfe_mae(bars, i0, E, 30)
        row["mfe30"], row["mae30"] = mfe, mae
        # random baseline: mean of NBASE later opens same day
        later = [k for k in range(i0 + 1, len(bars)) if True]
        if len(later) >= 3:
            picks = rng.sample(later, min(NBASE, len(later)))
            gs, ns = [], []
            for k in picks:
                Ek = bars[k][1]
                if Ek <= 0:
                    continue
                g = _fwd(bars, k, Ek, 15, slip, spr)
                if g:
                    gs.append(g[0]); ns.append(g[1])
            row["g15_rnd"] = float(sum(gs) / len(gs)) if gs else None
            row["n15_rnd"] = float(sum(ns) / len(ns)) if ns else None
        else:
            row["g15_rnd"] = row["n15_rnd"] = None
            miss["no_rnd"] += 1
        events.append(row)

    for r in calls:
        score_call(r, "all")
        if r["actionable"]:
            score_call(r, "actionable")
            if r["lean"]:
                score_call(r, "lean")
    for r in first_act.values():
        score_call(r, "first_act")

    pickle.dump({"events": events, "days": days, "miss": dict(miss)}, open(f"{OUT}/grid.pkl", "wb"))
    print("events", len(events), "miss", dict(miss), flush=True)


def _stats(rows, key):
    xs = [r[key] for r in rows if r.get(key) is not None]
    if not xs:
        return None
    # per-day means for t
    by = defaultdict(list)
    for r in rows:
        if r.get(key) is None:
            continue
        by[r["day"]].append(r[key])
    dm = [sum(v) / len(v) for v in by.values()]
    t = float("nan")
    if len(dm) > 2:
        mu = sum(dm) / len(dm)
        sd = (sum((x - mu) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5
        if sd > 0:
            t = mu / (sd / math.sqrt(len(dm)))
    xs_s = sorted(xs)
    med = xs_s[len(xs_s) // 2]
    return {
        "n": len(xs), "days": len(by), "mean": sum(xs) / len(xs), "med": med,
        "win": 100 * sum(1 for x in xs if x > 0) / len(xs), "t": t,
        "spr": float(sorted(r["spr"] for r in rows if r.get(key) is not None)[len(xs) // 2]),
    }


def _fmt(s, lab):
    if not s:
        return f"{lab:42s} n=0"
    return (f"{lab:42s} n={s['n']:4d} d={s['days']:2d} mean={s['mean']:7.0f} med={s['med']:7.0f} "
            f"t={s['t']:5.1f} win={s['win']:4.0f}% spr={s['spr']:5.0f}")


def report_main():
    Z = pickle.load(open(f"{OUT}/grid.pkl", "rb"))
    events, days, miss = Z["events"], Z["days"], Z["miss"]
    half = {d: (0 if i % 2 == 0 else 1) for i, d in enumerate(days)}
    lines = []
    def say(s=""):
        print(s, flush=True); lines.append(s)

    say(f"sessions {len(days)} ({days[0]}..{days[-1]}); events {len(events)}; miss {miss}")
    say("units: basis points after full quoted SIP spread (+1¢ RT when entry <$5), unless marked gross")
    say("")

    tags = ("all", "actionable", "lean", "first_act")
    metrics = [("n15", "net 15m"), ("g15", "gross 15m"), ("n5", "net 5m"), ("n30", "net 30m"),
               ("n_t11", "net to 11:00"), ("n_t1550", "net to 15:50"),
               ("mfe30", "MFE30"), ("mae30", "MAE30"),
               ("n15_rnd", "net 15m random-later"), ("g15_rnd", "gross 15m random-later")]

    for tag in tags:
        rows = [e for e in events if e["tag"] == tag]
        say(f"######## {tag}  (n_rows={len(rows)})")
        for hk, lab in metrics:
            a = _stats([r for r in rows if half[r["day"]] == 0], hk)
            b = _stats([r for r in rows if half[r["day"]] == 1], hk)
            say(_fmt(a, f"{lab} A"))
            say(_fmt(b, f"{lab} B"))
        # lift vs random on same rows
        paired = [r for r in rows if r.get("n15") is not None and r.get("n15_rnd") is not None]
        if paired:
            lift = [r["n15"] - r["n15_rnd"] for r in paired]
            by = defaultdict(list)
            for r, L in zip(paired, lift):
                by[r["day"]].append(L)
            dm = [sum(v) / len(v) for v in by.values()]
            t = float("nan")
            if len(dm) > 2:
                mu = sum(dm) / len(dm)
                sd = (sum((x - mu) ** 2 for x in dm) / (len(dm) - 1)) ** 0.5
                if sd > 0:
                    t = mu / (sd / math.sqrt(len(dm)))
            say(f"  call−random net15: mean={sum(lift)/len(lift):.0f} bp  t={t:.1f}  n={len(lift)}")
        say("")

    # time-of-day on actionable
    rows = [e for e in events if e["tag"] == "actionable"]
    say("######## actionable by call clock (pooled; net 15m)")
    for lab, a0, a1 in (("04-07", 0, 7 * 60), ("07-08", 7 * 60, 8 * 60), ("08-09", 8 * 60, 9 * 60),
                        ("09-09:30", 9 * 60, 9 * 60 + 30), ("09:30-11", 9 * 60 + 30, 11 * 60),
                        ("11+", 11 * 60, 24 * 60)):
        sel = [r for r in rows if a0 <= r["minute"] < a1]
        say(_fmt(_stats(sel, "n15"), f"{lab}"))

    say("")
    say("######## actionable by entry price (pooled; net 15m)")
    for lab, lo, hi in (("<$2", 0, 2), ("$2-5", 2, 5), ("$5-10", 5, 10), ("$10-20", 10, 20), (">=$20", 20, 1e9)):
        sel = [r for r in rows if lo <= r["E"] < hi]
        say(_fmt(_stats(sel, "n15"), lab))

    # tradeable: spread <= 100bp and price >= 2
    sel = [r for r in rows if r["spr"] <= 100 and r["E"] >= 2 and not r["no_spr"]]
    say("")
    say("######## actionable tradeable (spr<=100bp, px>=$2, has quote)")
    for hk, lab in (("n15", "net 15m"), ("n_t11", "net to 11:00"), ("g15", "gross 15m")):
        a = _stats([r for r in sel if half[r["day"]] == 0], hk)
        b = _stats([r for r in sel if half[r["day"]] == 1], hk)
        say(_fmt(a, f"{lab} A"))
        say(_fmt(b, f"{lab} B"))

    out = f"{OUT}/report.txt"
    open(out, "w").write("\n".join(lines) + "\n")
    say(f"wrote {out}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    Path(OUT).mkdir(parents=True, exist_ok=True)
    if mode in ("load", "all"):
        load_calls()
    if mode in ("fetch", "all"):
        fetch_main()
    if mode in ("analyze", "all"):
        analyze_main()
    if mode in ("report", "all"):
        report_main()
