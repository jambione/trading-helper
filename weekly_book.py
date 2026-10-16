"""weekly_book — the weekly undervalued-trending paper book (operator rules, docs/briefs/CLAUDE_WEEKLY_BOOK_2026-10-15.md).

Twenty names held from one week's close to the next. A name must be a liquid common stock, cheap on trailing
earnings (Finnhub P/E > 0 and <= 12), and already in an uptrend. AGY and Grok each rank 30 names from the frozen
screen; the book is the names BOTH list, ordered by the sum of their ranks, at most 20, never padded from one model.
Operator 10/16: if one model gives NO response (0 valid picks after a retry), the other model gets the full vote: its
own top 20 by rank. Both silent = no book, no orders.
The code places the orders; neither model trades.

This module holds the pure rules (screen, agreement, order day, weekly diff, week score) and the I/O steps
overnight_book.run() calls in weekly mode. overnight_book.rank() and nights.jsonl are untouched.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime
from pathlib import Path

MIN_PX, MAX_PX = 5.0, 50.0
MIN_ADV, ADV_DAYS, ADV_MIN_PRESENT = 20e6, 20, 15
PE_MAX = 12.0
SMA_N, BACK_LONG, BACK_SHORT = 50, 60, 20
SPREAD_MAX_PCT = 0.20
BOOK_N, PICK_N = 20, 30
TAGS = {"none", "cycle-peak", "falling", "dilution"}
TURN_COST_BP = 4.0
RETRY_WAIT_SEC = 60.0
SESSIONS_NEEDED = BACK_LONG + 5


# ── pure: screen ─────────────────────────────────────────────────────────────

def trend_row(by: dict, keys: list[str]) -> dict | None:
    """Price / ADV / uptrend facts from {YYYY-MM-DD: (adj close, adj volume)} over *keys* (sessions, oldest
    first, ending the session before the day). None when the history is too short or the name did not trade
    on the last session."""
    if len(keys) < BACK_LONG + 1 or keys[-1] not in by:
        return None
    closes = [by[k][0] if k in by else None for k in keys]
    last = closes[-1]
    if not last or last <= 0:
        return None
    win = [c for c in closes[-SMA_N:] if c]
    if len(win) < SMA_N * 0.8:
        return None
    sma = sum(win) / len(win)
    c60, c20 = closes[-1 - BACK_LONG], closes[-1 - BACK_SHORT]
    dv = [by[k][0] * by[k][1] for k in keys[-ADV_DAYS:] if k in by]
    adv = (sum(dv) / len(dv)) if dv else 0.0
    return {"last_close": last, "sma50": sma, "close_60": c60, "close_20": c20, "adv20": adv,
            "adv_present": len(dv)}


def screen_reason(r: dict, pe) -> str | None:
    """None = on the screen; else the first rule it fails. Pure."""
    if r["last_close"] < MIN_PX:
        return "price_below_5"
    if r["last_close"] >= MAX_PX:
        return "price_50_or_more"
    if r["adv_present"] < ADV_MIN_PRESENT:
        return "adv_history_short"
    if r["adv20"] < MIN_ADV:
        return "adv_below_20m"
    if pe is None:
        return "pe_missing"
    try:
        pe = float(pe)
    except (TypeError, ValueError):
        return "pe_missing"
    if pe <= 0:
        return "pe_not_positive"
    if pe > PE_MAX:
        return "pe_above_12"
    if not r["last_close"] > r["sma50"]:
        return "below_sma50"
    if r["close_60"] is None or not r["last_close"] > r["close_60"]:
        return "down_over_60"
    if r["close_20"] is None or not r["last_close"] > r["close_20"]:
        return "down_over_20"
    return None


def trend_ok(r: dict) -> bool:
    """Price, ADV and uptrend rules only (the P/E lookups run on these)."""
    return screen_reason(r, 1.0) is None


# ── pure: agreement ──────────────────────────────────────────────────────────

def parse_picks(text: str, screen_syms: set[str]) -> list[dict]:
    """A model's JSON {"picks": [{sym, rank, tag, why}]}. Anything not that shape is an empty list; a symbol
    not on the frozen screen, a bad tag or a duplicate is dropped. Ranks are re-read as given."""
    try:
        m = re.search(r"\{.*\}", text or "", re.S)
        obj = json.loads(m.group(0) if m else text)
        picks = obj["picks"]
        if not isinstance(picks, list):
            return []
    except (ValueError, KeyError, TypeError, AttributeError):
        return []
    out, seen = [], set()
    for p in picks:
        if not isinstance(p, dict):
            continue
        s = str(p.get("sym") or "").upper().strip()
        tag = str(p.get("tag") or "none").strip().lower()
        try:
            rk = int(p.get("rank"))
        except (TypeError, ValueError):
            continue
        if not s or s in seen or s not in screen_syms or tag not in TAGS or rk < 1:
            continue
        seen.add(s)
        out.append({"sym": s, "rank": rk, "tag": tag, "why": str(p.get("why") or "")[:200]})
    out.sort(key=lambda x: x["rank"])
    return out[:PICK_N]


def agree(agy: list[dict], grok: list[dict], cap: int = BOOK_N) -> tuple[list[dict], list[str]]:
    """(book, one-model names). Book = names on BOTH lists, sorted by the sum of ranks (then symbol), at most
    *cap*, never padded. Pure."""
    a = {p["sym"]: p for p in agy}
    g = {p["sym"]: p for p in grok}
    both = [{"sym": s, "agy_rank": a[s]["rank"], "grok_rank": g[s]["rank"],
             "rank_sum": a[s]["rank"] + g[s]["rank"],
             "tag": a[s]["tag"] if a[s]["tag"] != "none" else g[s]["tag"]} for s in set(a) & set(g)]
    both.sort(key=lambda r: (r["rank_sum"], r["sym"]))
    one = sorted(set(a) ^ set(g))
    return both[:cap], one


def vote(agy: list[dict], grok: list[dict], cap: int = BOOK_N) -> tuple[list[dict], list[str], str]:
    """(book, one-model names, vote). Operator 10/16: a model that gave NO valid picks forfeits its vote and the
    other model's own ranking makes the book (top *cap* by its rank). Both present = agree() unchanged (never
    padded). Both empty = no book. Pure."""
    if agy and grok:
        book, one = agree(agy, grok, cap)
        return book, one, "both"
    solo, name = (grok, "grok") if grok else (agy, "agy")
    if not solo:
        return [], [], "none"
    rows = sorted(solo, key=lambda p: (p["rank"], p["sym"]))[:cap]
    book = [{"sym": p["sym"], "agy_rank": p["rank"] if name == "agy" else None,
             "grok_rank": p["rank"] if name == "grok" else None, "rank_sum": p["rank"], "tag": p["tag"]}
            for p in rows]
    return book, [], f"{name}_only"


# ── pure: calendar and orders ────────────────────────────────────────────────

def order_day(day: date, sessions: list[date]) -> bool:
    """True when *day* is the last NYSE session of its ISO week (Friday, or Thursday before a holiday Friday)."""
    if day not in sessions:
        return False
    later = [s for s in sessions if s > day]
    return not later or later[0].isocalendar()[:2] != day.isocalendar()[:2]


def week_diff(held: dict[str, float], agreed: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(sell, buy, keep): sell held names off the agreed list, buy agreed names not held, keep the overlap."""
    a = set(agreed)
    sell = sorted(s for s in held if s not in a)
    keep = sorted(s for s in held if s in a)
    buy = [s for s in agreed if s not in held]
    return sell, buy, keep


def week_score(prev_close: dict[str, float], close: dict[str, float], held: list[str],
               entered: list[str], leaving: list[str]) -> dict:
    """Equal-weight return of the held names over the week, less 4 bp per name that entered at the start and
    4 bp per name leaving at the end (score only). Missing prices are counted, not guessed. Pure."""
    rets, missing = [], []
    for s in held:
        a, b = prev_close.get(s), close.get(s)
        if a and b:
            rets.append(b / a - 1)
        else:
            missing.append(s)
    gross = (sum(rets) / len(rets) * 1e4) if rets else None
    n = max(1, len(held))
    cost = TURN_COST_BP * (len([s for s in entered if s in held]) + len([s for s in leaving if s in held])) / n
    return {"n": len(held), "priced": len(rets), "missing": missing, "gross_bp": gross,
            "cost_bp": cost, "net_bp": (gross - cost) if gross is not None else None}


def screen_return(prev_close: dict[str, float], close: dict[str, float], syms: list[str]) -> float | None:
    rets = [close[s] / prev_close[s] - 1 for s in syms if prev_close.get(s) and close.get(s)]
    return (sum(rets) / len(rets) * 1e4) if rets else None


# ── prompt ───────────────────────────────────────────────────────────────────

def prompt_for(screen: dict) -> str:
    rows = "\n".join(f"{r['sym']}\t${r['last_close']:.2f}\tP/E {r['pe']:.1f}\tADV ${r['adv20'] / 1e6:.0f}M\t"
                     f"vs50d {r['last_close'] / r['sma50'] - 1:+.1%}\t60d {r['last_close'] / r['close_60'] - 1:+.1%}"
                     for r in screen["rows"])
    return (
        "You rank stocks for a paper portfolio held about one week (Friday close to Friday close).\n"
        "Every name below already passed a fixed screen: US common stock, $5-50, 20-day average dollar volume >= $20M,\n"
        "trailing P/E between 0 and 12, and an uptrend (above its 50-day average, above where it was 20 and 60 sessions ago).\n"
        "Choose ONLY from this list. Do not invent prices or numbers.\n\n"
        f"{rows}\n\n"
        f"Return your best {PICK_N} for the coming week, rank 1 = most attractive. Flag value traps with tag:\n"
        "'cycle-peak' (earnings at a cyclical peak), 'falling' (fundamentals deteriorating), 'dilution' (share issuance),\n"
        "else 'none'. Reply with JSON ONLY, exactly this shape:\n"
        '{"picks": [{"sym": "ABC", "rank": 1, "tag": "none", "why": "one sentence"}]}\n'
    )


# ── I/O steps (called from overnight_book in weekly mode) ────────────────────

def build_screen(ob, tc, day: date) -> dict:
    """Write ai_reports/overnight/weekly_screen_DAY.json from data the code fetches (bars, P/E). Refuses a
    partial universe like overnight_book.plan()."""
    from datetime import timedelta
    from swing_screener import _Degraded, _finnhub_get
    from config import load_config
    path = ob.OUT / f"weekly_screen_{day.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text())
    sess = [c.date for c in ob.calendar(tc, day - timedelta(days=140), day)]
    prior = [d for d in sess if d < day][-(SESSIONS_NEEDED + 5):]
    if len(prior) < SESSIONS_NEEDED:
        raise RuntimeError(f"calendar too short: {len(prior)} sessions")
    keys = [d.isoformat() for d in prior]
    uni = ob.universe(tc)
    failed: list = []
    bars = ob.daily_closes(sorted(uni), prior[0], prior[-1], failed=failed)
    if failed:
        raise RuntimeError(f"{len(failed)} daily-bars chunks failed; refusing a partial screen")
    cand = {}
    for s, by in bars.items():
        r = trend_row(by, keys)
        if r and trend_ok(r):
            cand[s] = r
    cfg = load_config() or {}
    rows, reasons, pe_fail = [], {}, 0
    for s in sorted(cand):
        try:
            m = (_finnhub_get("stock/metric", cfg, symbol=s, metric="all") or {}).get("metric", {})
            pe = m.get("peTTM") or m.get("peBasicExclExtraTTM")
        except _Degraded:
            pe, pe_fail = None, pe_fail + 1
        why = screen_reason(cand[s], pe)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        rows.append({"sym": s, "pe": float(pe), **{k: v for k, v in cand[s].items()}})
    scr = {"day": day.isoformat(), "made": datetime.now(ob.ET).isoformat(), "data_through": keys[-1],
           "universe": len(uni), "trend_ok": len(cand), "pe_lookup_failed": pe_fail, "rejected": reasons,
           "rows": rows}
    ob.OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scr, indent=1))
    ob.log(f"weekly screen {day}: universe {len(uni)}, uptrend+liquid {len(cand)}, on screen {len(rows)} "
           f"(P/E lookups failed {pe_fail})")
    return scr


WEEKLY_AGY_MODEL = "gemini-3.8-flash-high"


def agy_model_from_config() -> str:
    """The weekly book's AGY model: weekly_agy_model, else gemini-3.8-flash-high. Not the desk's agy_model /
    claude_model: the AGY CLI update of 2026-10-14 dropped gemini-3.1-pro-high (the desk's setting) and
    ai_suggest's default gemini-3.7-flash-high, and only Gemini 3.8 Flash (low/medium/high) remains."""
    try:
        from config import load_config
        cfg = load_config()
    except Exception:  # noqa: BLE001
        cfg = {}
    return str(cfg.get("weekly_agy_model") or WEEKLY_AGY_MODEL)


def run_models(ob, day: date, screen: dict, ask=None) -> dict:
    """Both models rank the frozen screen; write weekly_agreed_DAY.json and point plan_DAY.json at the book.
    *ask(model, prompt) -> text* replaces the CLIs in tests."""
    path = ob.OUT / f"weekly_agreed_{day.isoformat()}.json"
    if path.exists():
        return json.loads(path.read_text())
    syms = {r["sym"] for r in screen["rows"]}
    prompt = prompt_for(screen)
    if ask is None:
        from ai_suggest import call_agy_cli, call_grok_cli

        agy_model = agy_model_from_config()

        def ask(model, p):
            return call_agy_cli(p, model=agy_model, live_search=False, phase="weekly") if model == "agy" else \
                call_grok_cli(p, live_search=False, phase="weekly")
    raw, picks = {}, {}
    for model in ("agy", "grok"):
        t0 = time.time()
        raw[model] = ""
        for attempt in range(2):           # one retry on an empty or failed response (AGY 10/16 06:36 was empty)
            try:
                raw[model] = ask(model, prompt)
            except Exception as e:  # noqa: BLE001
                raw[model] = ""
                ob.log(f"weekly models {day}: {model} failed (try {attempt + 1}): {e!s:.150}")
            if parse_picks(raw[model], syms):
                break
            if attempt == 0:
                time.sleep(RETRY_WAIT_SEC)
        try:   # audit trail: what each model actually said (the agreed file keeps only the parsed picks)
            (ob.OUT / f"weekly_raw_{day.isoformat()}_{model}.txt").write_text(str(raw[model] or ""))
        except OSError:
            pass
        picks[model] = parse_picks(raw[model], syms)
        ob.log(f"weekly models {day}: {model} {len(picks[model])} valid picks in {time.time() - t0:.0f}s")
    book, one, how = vote(picks["agy"], picks["grok"])
    out = {"day": day.isoformat(), "made": datetime.now(ob.ET).isoformat(), "screen_n": len(syms),
           "agy": picks["agy"], "grok": picks["grok"], "book": book, "one_model": one, "vote": how}
    path.write_text(json.dumps(out, indent=1))
    plan = {"day": day.isoformat(), "made": out["made"], "data_through": screen.get("data_through"),
            "n_liquid": len(syms), "weekly": True, "picks": [{"sym": r["sym"], "mom": 0.0} for r in book]}
    (ob.OUT / f"plan_{day.isoformat()}.json").write_text(json.dumps(plan, indent=1))
    ob.log(f"weekly agreed {day} ({how}): {len(book)} names " + " ".join(r["sym"] for r in book)
           + (f"; one-model (not bought) {len(one)}" if one else ""))
    return out


def load_agreed(ob, day: date) -> dict | None:
    try:
        return json.loads((ob.OUT / f"weekly_agreed_{day.isoformat()}.json").read_text())
    except (OSError, ValueError):
        return None


def spread_ok(sym: str) -> tuple[bool, float | None]:
    """The desk's SIP spread statistic (ai_entry_watch.sip_spread_pct). Unknown spread = skip the name."""
    try:
        import ai_entry_watch as ew
        sp = ew.sip_spread_pct(sym, record=False)
    except Exception:  # noqa: BLE001
        sp = None
    return (sp is not None and sp <= SPREAD_MAX_PCT), sp


def rebalance(ob, tc, day: date, dry: bool = False) -> None:
    """Order day at the close: sell held names that left the agreed list, buy agreed names not held (spread
    check), keep the overlap. Sizing = 98% of (cash + the value of what is being sold), split equally over the
    names actually bought, whole shares."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    ag = load_agreed(ob, day)
    if not ag:
        ob.log(f"weekly rebalance {day}: no agreed file; NO ORDERS (never falls back to 12-1)")
        ob.alert(f"weekly book: no agreed list on order day {day}; no orders", key=f"wk-noagree-{day}")
        return
    agreed = [r["sym"] for r in ag.get("book", [])]
    pos = {x.symbol: x for x in tc.get_all_positions()}
    held = {s: float(x.qty) for s, x in pos.items()}
    sell_syms, buy_syms, keep = week_diff(held, agreed)
    proceeds = 0.0
    for s in sell_syms:
        x = pos[s]
        qty = abs(int(float(getattr(x, "qty_available", None) or x.qty)))
        proceeds += float(x.market_value or 0)
        cid = f"wk-{day.isoformat()}-{s}-sell"
        row = {"event": "submit", "week_end": day.isoformat(), "sym": s, "side": "sell", "qty": qty,
               "client_order_id": cid, "dry_run": dry, "weekly": True}
        if not dry and qty > 0:
            try:
                o = tc.submit_order(MarketOrderRequest(symbol=s, qty=qty, side=OrderSide.SELL,
                                                       time_in_force=TimeInForce.DAY, client_order_id=cid))
                row["order_id"] = str(o.id)
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
        ob.append(ob.LEDGER, row)
    ok_buy, skipped = [], {}
    for s in buy_syms:
        good, sp = spread_ok(s)
        if good:
            ok_buy.append(s)
        else:
            skipped[s] = sp
    px = ob.latest_prices(ok_buy) if ok_buy else {}
    a = tc.get_account()
    cash, equity = float(a.cash), float(a.equity)
    budget = ob.EQUITY_FRAC * max(0.0, min(equity, cash + proceeds))
    picks = ob.size_paper(ok_buy, px, set(keep), budget, ob.MAX_PRICE) if ok_buy else []
    for s, qty, price in picks:
        cid = f"on-{day.isoformat()}-{s}-buy"
        row = {"event": "submit", "night": day.isoformat(), "sym": s, "side": "buy", "qty": qty,
               "ref_price": price, "client_order_id": cid, "dry_run": dry, "weekly": True}
        if not dry:
            try:
                o = tc.submit_order(MarketOrderRequest(symbol=s, qty=qty, side=OrderSide.BUY,
                                                       time_in_force=TimeInForce.DAY, client_order_id=cid))
                row["order_id"] = str(o.id)
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)[:200]
        ob.append(ob.LEDGER, row)
    ob.append(ob.OUT / "weekly_orders.jsonl", {"day": day.isoformat(), "sell": sell_syms, "buy": [p[0] for p in picks],
                                                 "keep": keep, "spread_skipped": skipped, "budget": round(budget, 2),
                                                 "dry_run": dry})
    ob.log(f"weekly rebalance {day}: {'DRY RUN ' if dry else ''}sell {len(sell_syms)}, buy {len(picks)}, keep {len(keep)}"
           + (f"; spread-skipped {','.join(skipped)}" if skipped else ""))


def score_week(ob, day: date) -> dict | None:
    """Append the finished week to weeks.jsonl (prior order day close -> this order day close), with the
    mechanical screen's equal-weight return beside it. Needs the prior order day's orders row."""
    rows = ob._read_jsonl(ob.OUT / "weekly_orders.jsonl")
    prior = [r for r in rows if r["day"] < day.isoformat() and not r.get("dry_run")]
    today = [r for r in rows if r["day"] == day.isoformat() and not r.get("dry_run")]
    if not prior:
        return None
    start = prior[-1]
    held = sorted(set(start["buy"]) | set(start["keep"]))
    leaving = today[-1]["sell"] if today else []
    scr_path = ob.OUT / f"weekly_screen_{start['day']}.json"
    scr = json.loads(scr_path.read_text())["rows"] if scr_path.exists() else []
    syms = sorted(set(held) | {r["sym"] for r in scr})
    d0, d1 = date.fromisoformat(start["day"]), day
    bars = ob.daily_closes(syms, d0, d1)
    c0 = {s: by.get(start["day"], (None,))[0] for s, by in bars.items()}
    c1 = {s: by.get(day.isoformat(), (None,))[0] for s, by in bars.items()}
    sc = week_score(c0, c1, held, start["buy"], leaving)
    row = {"week_start": start["day"], "week_end": day.isoformat(), **sc,
           "screen_eq_bp": screen_return(c0, c1, [r["sym"] for r in scr]), "screen_n": len(scr)}
    ob.append(ob.OUT / "weeks.jsonl", row)
    ob.log(f"weekly score {start['day']}..{day}: book {sc['net_bp']} bp net ({sc['n']} names), "
           f"screen {row['screen_eq_bp']} bp")
    return row
