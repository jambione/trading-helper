#!/usr/bin/env python3
"""ai_hold_check.py — SHADOW logger + scorer for docs/studies/ai_hold_check_prereg.json (0c77683). Nothing it logs is
acted on: it never places, modifies or cancels an order, and it does not touch the day-hold test or any desk setting.

Commands (on the mini, from the repo root, .venv/bin/python; agy needs the GUI/Keychain session, so `run` and
`smoke` must be started by the LaunchAgent or a local Terminal, never over ssh):
  run      one session: wait to ~10:46 ET, qualify on delayed SIP bars 09:30-10:29 (day_hold rules), pick up to 30
           names (qualifiers by rank, then seeded eligible), ask the AI once per name from 11:00 (4 at a time), log raw
  smoke    pre-start gate: one search-dependent call + 4 concurrent calls; logs search use and latency
  status   after hours: counts only (sessions, calls, parse failures, latency drops, HOLD/EXIT split, search use, and
           the scored HOLD/EXIT name-days, which need SIP bars but no outcomes). Never prints returns.
  read     the single pre-registered read; refuses until >= 30 scored sessions and >= 150/150 scored name-days
  run --dry  qualify today immediately and print the setup (no AI calls, nothing logged)
Not built yet: the policy line (information only in the prereg); it must be added before the read.
Logs: ai_reports/ai_hold_check/<day>.jsonl (setup + one row per call), bars_<day>.json (SIP 1-min, cached by status/read).
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path[:0] = [HERE, ROOT, os.path.join(ROOT, "tools")]
import bars_structure as BS  # noqa: E402
import day_hold as DH  # noqa: E402

ET = BS.ET if hasattr(BS, "ET") else None
OUT = os.path.join(ROOT, "ai_reports", "ai_hold_check")
MAX_CALLS, CONCURRENCY, LATENCY_CAP = 30, 4, 180.0
T_QUALIFY, T_CHECK, T_GIVE_UP = 10 * 60 + 46, 11 * 60, 11 * 60 + 30
MIN_SESSIONS, MIN_EACH, MAX_SESSIONS = 30, 150, 45
PHASE = "ai_hold_check"
RATE_MARKERS = ("429", "rate limit", "rate-limit", "quota", "resource_exhausted", "too many requests")

PROMPT_V1 = """You are reviewing one stock position for a day-trading study. Decide only whether to keep holding it until 15:54 ET today or to sell it now.

Symbol: {sym} ({name})
Time now (ET): {now}
Bought at the 10:30 ET open: ${entry:.2f}
Current price: ${price:.2f} ({from_entry:+.2f}% from entry)
Today: open ${open:.2f}, high ${high:.2f}, low ${low:.2f}; prior close ${prev:.2f} ({day_chg:+.2f}% on the day)

Check for any news, filings, analyst actions or sentiment about {sym} today that bear on the rest of the session.
Answer in exactly this format:
Line 1: HOLD or EXIT
Line 2: confidence 1-5
Line 3: one sentence giving the reason, citing any news you found (or "no news found")."""
PROMPT_SHA = hashlib.sha256(PROMPT_V1.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ pure pieces (tested)
def parse_answer(text):
    """-> ('HOLD'|'EXIT', confidence or None) or (None, None). Strip markdown, whitespace, case; first token decides."""
    if not text:
        return None, None
    clean = re.sub(r"[*_#`>]", " ", str(text)).strip()
    toks = clean.split()
    if not toks:
        return None, None
    first = toks[0].upper().rstrip(".:,;")
    if first not in ("HOLD", "EXIT"):
        return None, None
    m = re.search(r"confidence\D{0,12}([1-5])", clean, re.I) or re.search(r"^\s*([1-5])\s*$", clean, re.M)
    return first, (int(m.group(1)) if m else None)


def pick_checked(pool, day, cap=MAX_CALLS):
    """pool: dicts with sym, q (qualifies), eligible, families, dv. Qualifiers by day_hold.rank_key first, then a
    seeded random draw of the remaining eligible names, up to cap."""
    quals = sorted([p for p in pool if p["q"]], key=DH.rank_key)
    rest = sorted([p for p in pool if p["eligible"] and not p["q"]], key=lambda p: p["sym"])
    rng = random.Random(int(hashlib.sha256(f"aihold|{day}".encode()).hexdigest(), 16))
    rng.shuffle(rest)
    return (quals + rest)[:cap]


def first_bar_after(bars, t):
    for i, b in enumerate(bars):
        if b[0] > t:
            return i
    return None


def held_before(bars, ml, i0):
    """True if the main day-hold rule still holds the name at the start of bar i0 (no loss-limit or knife exit
    completed before it). Uses only bars stamped 10:30 .. i0-1."""
    after = [(k, b) for k, b in enumerate(bars) if DH.T_ENTRY <= BS.et_hm(b[0]) <= DH.T_EXIT]
    if not after or BS.et_hm(after[0][1][0]) > DH.T_ENTRY + 4:
        return False, None
    entry = after[0][1][1]
    stop, closes = entry * DH.STOP, []
    for j, (k, b) in enumerate(after):
        if k >= i0:
            return True, entry
        if b[3] <= stop:
            return False, entry
        if j + 1 < len(after) and after[j + 1][0] < i0:      # a knife exit fills at the next bar's open, before i0
            if b[4] < ml or (len(closes) >= 5 and b[4] <= DH.KNIFE_DROP * max(closes[-5:])):
                return False, entry
        closes.append(b[4])
    return False, entry


def remaining(bars, ml, entry, i0):
    """Main-rule exit from bar i0 on (loss limit entry x 0.97, red knife, 15:54 close), with the knife's 5-close
    history carried from the entry. -> remaining bp from the open of bar i0, or None."""
    after = [(k, b) for k, b in enumerate(bars) if DH.T_ENTRY <= BS.et_hm(b[0]) <= DH.T_EXIT]
    closes = [b[4] for k, b in after if k < i0]
    run = [b for k, b in after if k >= i0]
    if not run:
        return None
    start, stop = run[0][1], entry * DH.STOP
    for j, b in enumerate(run):
        if b[3] <= stop:
            return 1e4 * (min(stop, b[1]) / start - 1)
        if j + 1 < len(run):
            if b[4] < ml or (len(closes) >= 5 and b[4] <= DH.KNIFE_DROP * max(closes[-5:])):
                return 1e4 * (run[j + 1][1] / start - 1)
        closes.append(b[4])
    return 1e4 * (run[-1][4] / start - 1)


def twin_split(items, n_exit, key):
    """items: dicts with sym and key; label EXIT the n_exit lowest by key (ties by symbol), rest HOLD."""
    order = sorted(items, key=lambda x: (x[key], x["sym"]))
    ex = {x["sym"] for x in order[:n_exit]}
    return [x for x in items if x["sym"] not in ex], [x for x in items if x["sym"] in ex]


def session_diff(hold, exit_, k="rem"):
    if not hold or not exit_:
        return None
    return sum(x[k] for x in hold) / len(hold) - sum(x[k] for x in exit_) / len(exit_)


def tstat(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return (sum(xs) / n if n else float("nan")), float("nan"), n
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m, (m / (sd / math.sqrt(n)) if sd > 0 else float("nan")), n


# ------------------------------------------------------------------ live pieces
def now_et():
    return datetime.now(BS.ET)


def hm(dt):
    return dt.hour * 60 + dt.minute


def wait_until(minute):
    while hm(now_et()) < minute:
        time.sleep(15)


def log(day, rec, lock=threading.Lock()):
    os.makedirs(OUT, exist_ok=True)
    with lock, open(os.path.join(OUT, f"{day}.jsonl"), "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def alpaca():
    import bars as B
    cl = B.client()
    if cl is None:
        raise SystemExit("no Alpaca data client")
    return cl


def minute_bars(cl, syms, start, end, feed):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    out = {}
    for i in range(0, len(syms), 100):
        r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                               start=start, end=end, feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
                                               adjustment=Adjustment.RAW))
        for s, rows in (r.data or {}).items():
            out[s] = [[b.timestamp.timestamp(), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)]
                      for b in rows]
        time.sleep(0.5)
    return out


def daily(cl, syms, start, end, adj):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame.Day, start=start, end=end,
                                           feed=DataFeed.SIP, adjustment=Adjustment.ALL if adj else Adjustment.RAW))
    return {s: {BS.et_day(b.timestamp.timestamp()): float(b.close) for b in rows} for s, rows in (r.data or {}).items()}


def call_ai(prompt, timeout=LATENCY_CAP + 60):
    """agy -p from an empty temp workspace, no --dangerously-skip-permissions (same as the desk's research call).
    -> dict(text, num_turns, status, error, start, end)."""
    import ai_suggest as AS
    binary = AS.resolve_agy_cli(None)
    cfg = json.load(open(os.path.join(ROOT, "config", "bot_config.json")))
    model = AS._agy_model(cfg.get("claude_model") or cfg.get("agy_model"))
    cmd = [binary, "-p", prompt, "--output-format", "json", "--print-timeout", f"{int(timeout)}s",
           "--disable-slash-commands", "--model", model]
    t0 = time.time()
    rec = {"start": t0, "model": model, "text": None, "num_turns": None, "status": None, "error": None}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15, env={**os.environ},
                           cwd=AS._cli_workspace())
        out, err = (p.stdout or "").strip(), (p.stderr or "").strip()
        try:
            env = json.loads(out)
        except json.JSONDecodeError:
            env = None
        if isinstance(env, dict):
            rec.update(text=str(env.get("response") or env.get("result") or ""), num_turns=env.get("num_turns"),
                       status=str(env.get("status") or ""), error=env.get("error"))
            AS._record_usage({"ts": round(t0, 3), "backend": "agy", "phase": PHASE, "model": model,
                              "num_turns": env.get("num_turns"), "duration_ms": int((time.time() - t0) * 1000),
                              "prompt_chars": len(prompt), "result_chars": len(rec["text"] or "")})
        else:
            rec.update(text=out or None, error=None if out else (err or f"exit {p.returncode}")[:300])
    except subprocess.TimeoutExpired:
        rec["error"] = "timeout"
    rec["end"] = time.time()
    return rec


def is_rate_limited(rec):
    s = f"{rec.get('error') or ''} {rec.get('status') or ''}".lower()
    return any(m in s for m in RATE_MARKERS)


def desk_agy_errors(day):
    n = 0
    try:
        for line in open(os.path.join(ROOT, "logs", "ai_trader.log"), errors="ignore"):
            if "Antigravity CLI" in line and ("error" in line.lower() or "timed out" in line.lower() or "exit" in line.lower()):
                n += 1
    except OSError:
        return None
    return n


def run(dry=False):
    """dry=True: qualify today now and print the setup; no waits, no AI calls, nothing logged."""
    t = now_et()
    day = t.strftime("%Y-%m-%d")
    if t.weekday() >= 5:
        return
    if hm(t) > T_GIVE_UP and not dry:
        log(day, {"ev": "outage", "why": f"started {t:%H:%M}, after {T_GIVE_UP // 60}:{T_GIVE_UP % 60:02d}"})
        return
    if not dry:
        log(day, {"ev": "start", "ts": time.time(), "prompt_sha": PROMPT_SHA, "desk_agy_errors_at_start": desk_agy_errors(day)})
        wait_until(T_QUALIFY)
    counts = collections.Counter()
    cand = DH.load_candidates(day, day, counts=counts).get(day, {})
    if not cand:
        log(day, {"ev": "setup", "n_cand": 0, "counts": dict(counts), "note": "no candidates (holiday or no admissions)"})
        return
    cl = alpaca()
    syms = sorted(cand)
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=BS.ET)
    sip = minute_bars(cl, syms, d0.replace(hour=9, minute=30), d0.replace(hour=10, minute=30), "sip")
    raw = daily(cl, syms, d0 - timedelta(days=100), d0 - timedelta(days=1), adj=False)
    adj = daily(cl, syms, d0 - timedelta(days=100), d0 - timedelta(days=1), adj=True)
    pool = []
    for s in syms:
        f = DH.morning(sip.get(s, []))
        r, a = raw.get(s, {}), adj.get(s, {})
        if f is None or not r or not a:
            counts["no_bars_or_daily"] += 1
            continue
        prev = max(r)
        closes = [a[x] for x in sorted(a)[-50:]]
        above = len(closes) == 50 and a.get(prev, 0) > sum(closes) / 50
        pool.append({"sym": s, "families": len(cand[s]["fams"]), "dv": f["dv"], "q": DH.qualifies(f, r[prev], above),
                     "eligible": f["P"] >= DH.MIN_PX and f["dv"] >= DH.MIN_DV, "f": f, "prev": r[prev]})
    checked = pick_checked(pool, day)
    names = DH.asset_names()
    setup = {"ev": "setup", "n_cand": len(cand), "n_pool": len(pool), "n_qual": sum(p["q"] for p in pool),
              "n_elig": sum(p["eligible"] for p in pool), "checked": [p["sym"] for p in checked],
              "qualifiers": [p["sym"] for p in checked if p["q"]], "counts": dict(counts)}
    if dry:
        print(json.dumps(setup, indent=1))
        return
    log(day, setup)
    wait_until(T_CHECK)
    iex = minute_bars(cl, [p["sym"] for p in checked], d0.replace(hour=9, minute=30), now_et(), "iex")
    stop_flag = threading.Event()

    def one(p):
        if stop_flag.is_set():
            return
        s, b = p["sym"], iex.get(p["sym"], [])
        entry_b = [x for x in b if BS.et_hm(x[0]) >= DH.T_ENTRY]
        if not b or not entry_b:
            log(day, {"ev": "skip", "sym": s, "why": "no live bars"})
            return
        price, entry = b[-1][4], entry_b[0][1]
        nm = names.get(s) if isinstance(names.get(s), str) else ""
        inputs = {"sym": s, "name": nm or s, "now": now_et().strftime("%H:%M"), "entry": entry, "price": price,
                  "from_entry": (price / entry - 1) * 100, "open": b[0][1], "high": max(x[2] for x in b),
                  "low": min(x[3] for x in b), "prev": p["prev"], "day_chg": (price / p["prev"] - 1) * 100}
        rec = call_ai(PROMPT_V1.format(**inputs))
        ans, conf = parse_answer(rec["text"])
        lat = rec["end"] - rec["start"]
        log(day, {"ev": "call", "sym": s, "qualifier": p["q"], "prompt_sha": PROMPT_SHA, "inputs": inputs,
                  "start": rec["start"], "answer_ts": rec["end"], "latency": round(lat, 1), "model": rec["model"],
                  "num_turns": rec["num_turns"], "status": rec["status"], "error": rec["error"], "raw": rec["text"],
                  "answer": ans, "confidence": conf, "over_cap": lat > LATENCY_CAP})
        if is_rate_limited(rec):
            stop_flag.set()
            log(day, {"ev": "rate_limit_stop", "sym": s, "ts": time.time()})

    with ThreadPoolExecutor(CONCURRENCY) as ex:
        list(ex.map(one, checked))
    log(day, {"ev": "end", "ts": time.time(), "desk_agy_errors_at_end": desk_agy_errors(day)})


def smoke():
    day = now_et().strftime("%Y-%m-%d")
    q = ("Search the web: what is the most recent news headline about NVIDIA (NVDA) today? Reply with the headline and "
         "its source, or exactly NO_ACCESS if you cannot search or open web pages.")
    one = call_ai(q)
    batch = []
    with ThreadPoolExecutor(4) as ex:
        batch = list(ex.map(lambda s: call_ai(f"Answer with one word, HOLD or EXIT, for {s}; no research needed."),
                            ["AAPL", "MSFT", "AMZN", "GOOGL"]))
    res = {"ev": "smoke", "ts": time.time(), "search_call": {k: one[k] for k in ("num_turns", "status", "error")},
           "search_text": (one["text"] or "")[:400], "search_latency": round(one["end"] - one["start"], 1),
           "searched": bool(one["text"]) and "NO_ACCESS" not in (one["text"] or "") and (one["num_turns"] or 0) > 1,
           "concurrent_latency": [round(r["end"] - r["start"], 1) for r in batch],
           "concurrent_ok": [parse_answer(r["text"])[0] for r in batch]}
    log(f"smoke_{day}", res)
    print(json.dumps(res, indent=1))


# ------------------------------------------------------------------ after hours
def session_days():
    return sorted(f[:-6] for f in os.listdir(OUT) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", f)) if os.path.isdir(OUT) else []


def load_day(day):
    return [json.loads(l) for l in open(os.path.join(OUT, f"{day}.jsonl"))]


def day_bars(day, syms):
    p = os.path.join(OUT, f"bars_{day}.json")
    have = json.load(open(p)) if os.path.exists(p) else {}
    need = [s for s in syms if s not in have]
    if need:
        t = now_et()
        if t.strftime("%Y-%m-%d") == day and hm(t) < 16 * 60 + 20:
            raise SystemExit("after 16:20 ET only (SIP for the day must be complete)")
        d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=BS.ET)
        have.update(minute_bars(alpaca(), need, d0.replace(hour=9, minute=30), d0.replace(hour=16), "sip"))
        for s in need:
            have.setdefault(s, [])
        json.dump(have, open(p, "w"))
    return have


def score_day(day, with_outcomes):
    rows = load_day(day)
    calls = [r for r in rows if r.get("ev") == "call"]
    c = collections.Counter(calls=len(calls))
    outage = any(r.get("ev") == "outage" for r in rows) or not any(r.get("ev") == "setup" for r in rows)
    early_stop = any(r.get("ev") == "rate_limit_stop" for r in rows) and len(calls) < 15
    if outage or early_stop:
        c["excluded_" + ("outage" if outage else "rate_limit_before_15")] += 1
        return c, []
    bars = day_bars(day, sorted({r["sym"] for r in calls}))
    items = []
    for r in calls:
        if r["error"] and not r["answer"]:
            c["failed"] += 1
            continue
        if not r["answer"]:
            c["unparseable"] += 1
            continue
        if r["over_cap"]:
            c["over_latency_cap"] += 1
            continue
        c["searched" if (r.get("num_turns") or 0) > 1 else "no_search"] += 1
        b = bars.get(r["sym"], [])
        f = DH.morning(b)
        i0 = first_bar_after(b, r["answer_ts"])
        if f is None or i0 is None or len(b) < 300:
            c["bars_incomplete"] += 1
            continue
        ok, entry = held_before(b, f["ML"], i0)
        if not ok:
            c["already_exited"] += 1
            continue
        c["scored_" + r["answer"]] += 1
        it = {"sym": r["sym"], "ai": r["answer"], "conf": r["confidence"], "q": r["qualifier"],
              "pct": b[i0][1] / entry - 1}
        lo, hi = min(x[3] for x in b[:i0]), max(x[2] for x in b[:i0])
        it["rng"] = (b[i0][1] - lo) / (hi - lo) if hi > lo else 0.5
        if with_outcomes:
            it["rem"] = remaining(b, f["ML"], entry, i0)
        items.append(it)
    return c, items


def status():
    tot = collections.Counter()
    sessions = 0
    for d in session_days():
        c, items = score_day(d, with_outcomes=False)
        tot.update(c)
        if any(i["ai"] == "HOLD" for i in items) and any(i["ai"] == "EXIT" for i in items):
            sessions += 1
    print(json.dumps({"days_logged": len(session_days()), "scored_sessions": sessions, **dict(tot)}, indent=1))


def read():
    days = session_days()
    per = {}
    tot = collections.Counter()
    for d in days:
        c, items = score_day(d, with_outcomes=False)
        tot.update(c)
        if any(i["ai"] == "HOLD" for i in items) and any(i["ai"] == "EXIT" for i in items):
            per[d] = None
    n_s, n_h, n_e = len(per), tot["scored_HOLD"], tot["scored_EXIT"]
    if not (n_s >= MIN_SESSIONS and n_h >= MIN_EACH and n_e >= MIN_EACH):
        if len(days) >= MAX_SESSIONS:
            print(f"UNDERPOWERED = FAIL at {len(days)} sessions (scored sessions {n_s}, HOLD {n_h}, EXIT {n_e})")
        else:
            print(f"not yet: scored sessions {n_s}/{MIN_SESSIONS}, HOLD {n_h}/{MIN_EACH}, EXIT {n_e}/{MIN_EACH}; no outcomes computed")
        return
    rows = []
    for d in sorted(per):
        _, items = score_day(d, with_outcomes=True)
        items = [i for i in items if i["rem"] is not None]
        hold = [i for i in items if i["ai"] == "HOLD"]
        ex = [i for i in items if i["ai"] == "EXIT"]
        ai = session_diff(hold, ex)
        tp = session_diff(*twin_split(items, len(ex), "pct"))
        tr = session_diff(*twin_split(items, len(ex), "rng"))
        if ai is not None and tp is not None:
            rows.append({"day": d, "ai": ai, "twin_pct": tp, "twin_range": tr, "d_pct": ai - tp,
                         "d_rng": None if tr is None else ai - tr})
    m, t, n = tstat([r["d_pct"] for r in rows])
    halves = {h: tstat([r["d_pct"] for k, r in enumerate(rows) if k % 2 == (0 if h == "A" else 1)]) for h in ("A", "B")}
    best = max(range(len(rows)), key=lambda k: rows[k]["d_pct"])
    m3, t3, _ = tstat([r["d_pct"] for k, r in enumerate(rows) if k != best])
    mr, _, _ = tstat([r["d_rng"] for r in rows])
    ok = m >= 25 and t >= 2 and all(halves[h][0] > 0 for h in "AB") and m3 >= 25 and t3 >= 2 and mr > 0
    out = {"sessions": n, "ai_minus_twin_pct": [round(m, 1), round(t, 2)], "halves": {h: [round(v[0], 1), round(v[1], 2)] for h, v in halves.items()},
           "drop_best_session": [round(m3, 1), round(t3, 2)], "ai_minus_twin_range": round(mr, 1),
           "verdict": "PASS (skeptic review next)" if ok else "FAIL", "counts": dict(tot)}
    json.dump({"summary": out, "sessions": rows}, open(os.path.join(OUT, "read.json"), "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"run": lambda: run(dry="--dry" in sys.argv), "smoke": smoke, "status": status, "read": read}.get(cmd, lambda: print(__doc__))()
