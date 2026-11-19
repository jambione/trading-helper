#!/usr/bin/env python3
"""book_coverage_watch - is the book seeing the day's real leaders? READ-ONLY (operator 11/19 'Yes, build both').

Every 10 minutes in RTH: the top liquid $20-$400 gainers (Alpaca snapshots over most-actives + movers + the prior
day's top-400 by dollar volume) against the names seated on the watch book (ai_reports/entry_watch_state.json),
with each missing leader's refusal reasons from today's admission ledger. Writes
ai_reports/book_coverage/<day>.jsonl (one row per scan) and latest.json. Never places orders, never edits desk state.

    .venv/bin/python tools/book_coverage_watch.py --once     # one scan now
    .venv/bin/python tools/book_coverage_watch.py --loop     # 09:35-16:00 ET every 10 min (run on the mini)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "book_coverage"
WATCH_STATE = ROOT / "ai_reports" / "entry_watch_state.json"
LEDGER_DIR = ROOT / "ai_reports" / "admit_ledger"
DATA = "https://data.alpaca.markets"
TOP_N, MIN_PCT, MIN_PREV_DV, PX_LO, PX_HI = 30, 1.5, 50e6, 20.0, 400.0
INTERVAL, START, END = 600, dtime(9, 35), dtime(16, 0)


def leaders(snaps: dict, top_n=TOP_N, min_pct=MIN_PCT, min_prev_dv=MIN_PREV_DV) -> list[dict]:
    """Alpaca snapshot dict {SYM: snap} -> top gainers in $20-$400 with prior-day dollar volume >= min_prev_dv. Pure."""
    rows = []
    for sym, s in (snaps or {}).items():
        try:
            prev = s.get("prevDailyBar") or {}
            pc, pv = float(prev.get("c") or 0), float(prev.get("v") or 0)
            px = float((s.get("latestTrade") or {}).get("p") or (s.get("minuteBar") or {}).get("c") or 0)
            q = s.get("latestQuote") or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not (pc > 0 and px > 0) or not (PX_LO <= px <= PX_HI) or pc * pv < min_prev_dv:
            continue
        pct = (px / pc - 1) * 100
        if pct < min_pct:
            continue
        spread = (ask - bid) / ((ask + bid) / 2) * 1e4 if bid > 0 and ask >= bid else None
        rows.append({"symbol": sym, "price": round(px, 2), "pct": round(pct, 2),
                     "prev_dollar_vol_m": round(pc * pv / 1e6, 1),
                     "spread_bp_iex": None if spread is None else round(spread, 1)})
    rows.sort(key=lambda r: -r["pct"])
    return rows[:top_n]


def coverage(top: list[dict], seated: set[str], reasons: dict[str, Counter], kept: set[str]) -> dict:
    """Join leaders to the book and the ledger. Pure."""
    seen, missing = [], []
    for r in top:
        s = r["symbol"]
        if s in seated:
            seen.append({**r, "status": "seated"})
            continue
        why = reasons.get(s) or Counter()
        missing.append({**r, "status": "kept_earlier" if s in kept else ("refused" if why else "never_offered"),
                        "top_reasons": dict(why.most_common(3))})
    blockers = Counter()
    for m in missing:
        if m["top_reasons"]:
            blockers[next(iter(m["top_reasons"]))] += 1
        else:
            blockers[m["status"]] += 1
    return {"leaders": len(top), "seated": len(seen), "missing": len(missing),
            "coverage_pct": round(100 * len(seen) / len(top), 1) if top else None,
            "blockers": dict(blockers.most_common()), "seen": seen, "missing_detail": missing}


class LedgerTail:
    """Incremental reader of today's admission ledger: refusal reasons and kept names per symbol."""

    def __init__(self, day: str):
        self.path, self.pos = LEDGER_DIR / f"{day}.jsonl", 0
        self.reasons: dict[str, Counter] = defaultdict(Counter)
        self.kept: set[str] = set()

    def update(self):
        if not self.path.exists():
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.pos)
            chunk = fh.read()
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return
        self.pos += cut + 1
        for line in chunk[:cut].splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            s = r.get("symbol")
            if not s:
                continue
            if r.get("kept") is True:
                self.kept.add(s)
            elif r.get("reason"):
                self.reasons[s][r["reason"]] += 1


def _keys():
    p = os.environ.get("TH_DATA_SECRETS") or str(ROOT / "secrets_data.json")
    k = json.load(open(p))
    return {"APCA-API-KEY-ID": k["alpaca_data_api_key"], "APCA-API-SECRET-KEY": k["alpaca_data_secret_key"]}


def universe(headers) -> list[str]:
    import requests
    syms: set[str] = set()
    for path, key in (("v1beta1/screener/stocks/most-actives?by=volume&top=100", "most_actives"),
                      ("v1beta1/screener/stocks/movers?top=50", "gainers")):
        try:
            r = requests.get(f"{DATA}/{path}", headers=headers, timeout=15)
            if r.status_code == 200:
                syms |= {x.get("symbol") for x in r.json().get(key, []) if x.get("symbol")}
        except Exception:  # noqa: BLE001
            pass
    try:   # prior session's top-400 by dollar volume from the warehouse (what the studies call the universe)
        sys.path.insert(0, str(ROOT / "tools" / "studies"))
        from relative_strength_scan import load_day
        from evening_family import WH_SIP
        import numpy as np
        last = sorted(WH_SIP.glob("20??-??-??.npz"))[-1]
        d = load_day(last)
        dv = np.nansum(d["c"] * d["v"], axis=1)
        syms |= {d["syms"][i] for i in np.argsort(-dv)[:400]}
    except Exception:  # noqa: BLE001
        pass
    return sorted(s for s in syms if s and s.isalpha())


def snapshots(headers, syms) -> dict:
    import requests
    out = {}
    for i in range(0, len(syms), 200):
        try:
            r = requests.get(f"{DATA}/v2/stocks/snapshots", headers=headers, timeout=20,
                             params={"symbols": ",".join(syms[i:i + 200]), "feed": "iex"})
            if r.status_code == 200:
                out.update(r.json() or {})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return out


def seated_now() -> set[str]:
    try:
        return {str(k).upper() for k in json.loads(WATCH_STATE.read_text()).keys()}
    except Exception:  # noqa: BLE001
        return set()


# ── Pillar 1: is every seated name a leader with a catalyst? (operator 11/19) ─────────────────────────────
VOL_PACE_TAG, GAP_TAG = 2.0, 2.0


def enrich(sym: str, s: dict, now: datetime) -> dict | None:
    """One snapshot -> price, day %, volume pace vs the prior day, gap %. Pure."""
    try:
        prev, day = s.get("prevDailyBar") or {}, s.get("dailyBar") or {}
        pc, pv = float(prev.get("c") or 0), float(prev.get("v") or 0)
        px = float((s.get("latestTrade") or {}).get("p") or (s.get("minuteBar") or {}).get("c") or 0)
        q = s.get("latestQuote") or {}
        bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        dv, do = float(day.get("v") or 0), float(day.get("o") or 0)
    except (TypeError, ValueError, AttributeError):
        return None
    if not (pc > 0 and px > 0):
        return None
    elapsed = max((now.hour * 60 + now.minute - 570) / 390, 0.05)
    return {"symbol": sym, "price": round(px, 2), "pct": round((px / pc - 1) * 100, 2),
            "vol_pace": round(dv / (pv * min(elapsed, 1.0)), 2) if pv > 0 else None,
            "gap_pct": round((do / pc - 1) * 100, 2) if do > 0 else None,
            "spread_bp_iex": round((ask - bid) / ((ask + bid) / 2) * 1e4, 1) if bid > 0 and ask >= bid else None}


def catalyst_tags(row: dict, news_first: str | None) -> list[str]:
    """Why a name is moving, from what the feed shows: news today, volume pace >= 2x, gap >= 2%. Pure."""
    tags = []
    if news_first:
        tags.append(f"news {news_first}")
    if (row.get("vol_pace") or 0) >= VOL_PACE_TAG:
        tags.append(f"vol {row['vol_pace']:.1f}x")
    if abs(row.get("gap_pct") or 0) >= GAP_TAG:
        tags.append(f"gap {row['gap_pct']:+.1f}%")
    return tags


def purity(seated: set[str], rows: dict[str, dict], ranked: list[str], news: dict[str, str], top_n=TOP_N) -> dict:
    """Each seated name -> leader / mover / not a mover, its rank among liquid $20-$400 gainers, its catalysts. Pure."""
    rank = {s: i + 1 for i, s in enumerate(ranked)}
    book = []
    for s in sorted(seated):
        r = rows.get(s) or {"symbol": s}
        k = rank.get(s)
        status = "leader" if k and k <= top_n else ("mover" if k else "not_a_mover")
        tags = catalyst_tags(r, news.get(s))
        book.append({**r, "rank": k, "status": status, "catalysts": tags,
                     "good": status == "leader" and bool(tags)})
    n = len(book)
    return {"book": sorted(book, key=lambda b: (b["rank"] or 10 ** 6)),
            "book_n": n, "book_leaders": sum(b["status"] == "leader" for b in book),
            "book_good": sum(b["good"] for b in book),
            "purity_pct": round(100 * sum(b["good"] for b in book) / n, 1) if n else None}


def news_first_times(headers, syms: list[str], day: str) -> dict[str, str]:
    """{SYM: 'HH:MM' of its first Alpaca news story today since 04:00 ET}."""
    import requests
    out: dict[str, str] = {}
    start = datetime.fromisoformat(day).replace(hour=4, tzinfo=ET).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(0, len(syms), 50):
        tok, pages = None, 0
        while pages < 6:
            params = {"symbols": ",".join(syms[i:i + 50]), "start": start, "limit": 50, "sort": "asc"}
            if tok:
                params["page_token"] = tok
            try:
                r = requests.get(f"{DATA}/v1beta1/news", headers=headers, params=params, timeout=20)
                if r.status_code != 200:
                    break
                j = r.json()
            except Exception:  # noqa: BLE001
                break
            for n in j.get("news", []):
                t = datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M")
                for s in n.get("symbols") or []:
                    if s in syms[i:i + 50] and s not in out:
                        out[s] = t
            tok, pages = j.get("next_page_token"), pages + 1
            if not tok:
                break
    return out


# ── Pillar 2: opens per 10 minutes and the timing of each open ────────────────────────────────────────────
def position_intervals(fills: list[dict]) -> list[dict]:
    """Day's fills -> [{symbol, t_open, px_open, qty, t_close|None, px_close|None}] (an open = a buy from flat). Pure."""
    pos, cur, out = defaultdict(float), {}, []
    for f in sorted(fills, key=lambda x: x["t"]):
        s, q = f["symbol"], float(f["qty"])
        if f["side"] == "buy":
            if pos[s] <= 1e-9:
                cur[s] = {"symbol": s, "t_open": f["t"], "px_open": float(f["price"]), "qty": 0.0, "t_close": None, "px_close": None}
                out.append(cur[s])
            pos[s] += q
            cur[s]["qty"] += q
        else:
            pos[s] -= q
            if pos[s] <= 1e-9 and s in cur:
                cur[s]["t_close"], cur[s]["px_close"] = f["t"], float(f["price"])
                pos[s] = 0.0
                del cur[s]
    return out


def opens_summary(iv: list[dict], now: float, day_start: float) -> dict:
    """Opens in the last 10 / 60 minutes and today; open now; share of minutes since 09:30 with >= 1 and >= 2 open. Pure."""
    opens = [x["t_open"] for x in iv]
    mins = max(1, int((now - day_start) // 60))
    one = two = 0
    for k in range(mins):
        t = day_start + 60 * k + 30
        n = sum(1 for x in iv if x["t_open"] <= t and (x["t_close"] is None or x["t_close"] > t))
        one += n >= 1
        two += n >= 2
    return {"last_10m": sum(1 for t in opens if now - t <= 600), "last_60m": sum(1 for t in opens if now - t <= 3600),
            "today": len(opens), "open_now": sum(1 for x in iv if x["t_close"] is None),
            "share_ge1_pct": round(100 * one / mins, 1), "share_ge2_pct": round(100 * two / mins, 1),
            "per_10m_today": round(len(opens) / (mins / 10), 2)}


def timing_card(iv: dict, bars: list[dict]) -> dict:
    """Where an open sat when it was bought: day-range position, minutes since the day's low, fast %R (21 bars,
    today only), and whether it traded above the fill within 3 minutes. bars = today's 1-min bars [{t, h, l, c}]. Pure."""
    before = [b for b in bars if b["t"] + 60 <= iv["t_open"]]
    after = [b for b in bars if iv["t_open"] < b["t"] + 60 <= iv["t_open"] + 180 + 60]
    out = {"symbol": iv["symbol"], "time": datetime.fromtimestamp(iv["t_open"], ET).strftime("%H:%M:%S"),
           "fill": iv["px_open"], "qty": iv["qty"]}
    if len(before) >= 5:
        hi, lo = max(b["h"] for b in before), min(b["l"] for b in before)
        out["range_pos"] = round(100 * (iv["px_open"] - lo) / (hi - lo), 0) if hi > lo else None
        low_t = min(before, key=lambda b: b["l"])["t"]
        out["min_since_low"] = round((iv["t_open"] - low_t) / 60)
        w = before[-21:]
        h21, l21 = max(b["h"] for b in w), min(b["l"] for b in w)
        out["wr_fast_raw"] = round(-100 * (h21 - iv["px_open"]) / (h21 - l21), 0) if h21 > l21 else None
    if after:
        best = max(b["h"] for b in after)
        out["green_3m"] = best > iv["px_open"]
        out["best_3m_bp"] = round((best / iv["px_open"] - 1) * 1e4, 1)
    if iv.get("px_close"):
        out["result_bp"] = round((iv["px_close"] / iv["px_open"] - 1) * 1e4, 1)
        out["held_min"] = round((iv["t_close"] - iv["t_open"]) / 60, 1)
    return out


def _desk_headers():
    k = json.load(open(ROOT / "config" / "secrets.json"))
    return {"APCA-API-KEY-ID": k["api_key"], "APCA-API-SECRET-KEY": k["secret_key"]}


def today_fills(day: str) -> list[dict]:
    """The paper account's FILL activities today (read-only GET, one page of 100 at a time)."""
    import requests
    h, out, tok = _desk_headers(), [], None
    for _ in range(10):
        params = {"date": day, "direction": "asc", "page_size": 100}
        if tok:
            params["page_token"] = tok
        r = requests.get("https://paper-api.alpaca.markets/v2/account/activities/FILL", headers=h, params=params, timeout=20)
        if r.status_code != 200:
            break
        rows = r.json()
        for a in rows:
            out.append({"symbol": a["symbol"], "side": a["side"], "qty": float(a["qty"]), "price": float(a["price"]),
                        "t": datetime.fromisoformat(a["transaction_time"].replace("Z", "+00:00")).timestamp()})
        if len(rows) < 100:
            break
        tok = rows[-1]["id"]
    return out


def day_bars(headers, sym: str, day: str) -> list[dict]:
    import requests
    start = datetime.fromisoformat(day).replace(hour=9, minute=30, tzinfo=ET).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(f"{DATA}/v2/stocks/{sym}/bars", headers=headers, timeout=20,
                         params={"timeframe": "1Min", "start": start, "feed": "iex", "limit": 1000})
        rows = (r.json() or {}).get("bars") or [] if r.status_code == 200 else []
    except Exception:  # noqa: BLE001
        rows = []
    return [{"t": datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp(), "h": b["h"], "l": b["l"], "c": b["c"]}
            for b in rows]


STATE = {"ranked": [], "rows": {}, "news": {}, "coverage": {}, "cards": {}}


def slow_scan(tail: LedgerTail, headers, syms) -> None:
    """Every 10 min: the leaders, their catalysts and what the book is missing."""
    now = datetime.now(ET)
    day = now.date().isoformat()
    tail.update()
    snaps = snapshots(headers, syms)
    top = leaders(snaps)
    full = leaders(snaps, top_n=10 ** 6)
    rows = {s: e for s, e in ((s, enrich(s, v, now)) for s, v in snaps.items()) if e}
    seated = seated_now()
    STATE["news"] = news_first_times(headers, sorted({r["symbol"] for r in top} | seated), day)
    for r in top:
        r.update({k: rows.get(r["symbol"], {}).get(k) for k in ("vol_pace", "gap_pct")})
        r["catalysts"] = catalyst_tags(r, STATE["news"].get(r["symbol"]))
    STATE["ranked"], STATE["rows"] = [r["symbol"] for r in full], rows
    STATE["coverage"] = coverage(top, seated, tail.reasons, tail.kept)
    c = STATE["coverage"]
    miss = ", ".join(f"{m['symbol']} +{m['pct']}% [{next(iter(m['top_reasons']), m['status'])}]" for m in c["missing_detail"][:12])
    print(f"{now:%H:%M} coverage {c['seated']}/{c['leaders']} ({c['coverage_pct']}%) blockers {c['blockers']} | missing: {miss}", flush=True)


def fast_scan(headers) -> dict:
    """Every minute: book purity, opens, timing cards; writes latest.json (+ one row per scan to <day>.jsonl)."""
    now = datetime.now(ET)
    day = now.date().isoformat()
    seated = seated_now()
    missing = sorted(s for s in seated if s not in STATE["rows"])
    if missing:
        snaps = snapshots(headers, missing)
        STATE["rows"].update({s: e for s, e in ((s, enrich(s, v, now)) for s, v in snaps.items()) if e})
    pur = purity(seated, STATE["rows"], STATE["ranked"], STATE["news"])
    try:
        iv = position_intervals(today_fills(day))
    except Exception as e:  # noqa: BLE001
        iv, pur["fills_error"] = [], str(e)[:120]
    day_start = now.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()
    ops = opens_summary(iv, now.timestamp(), day_start)
    for x in iv[-12:]:
        key = f"{x['symbol']}|{int(x['t_open'])}"
        card = STATE["cards"].get(key)
        if card is None or "green_3m" not in card or ("result_bp" not in card and x.get("px_close")):
            if now.timestamp() - x["t_open"] < 240 and card is not None and "green_3m" not in card:
                continue          # wait for the 3 minutes to pass before refetching
            STATE["cards"][key] = timing_card(x, day_bars(headers, x["symbol"], day))
    cards = [STATE["cards"][f"{x['symbol']}|{int(x['t_open'])}"] for x in iv[-12:] if f"{x['symbol']}|{int(x['t_open'])}" in STATE["cards"]]
    res = {"ts": now.isoformat(timespec="seconds"), "opens": ops, "recent_opens": list(reversed(cards)),
           **{k: v for k, v in pur.items()}, "coverage": STATE["coverage"],
           "definition": {"leader": f"top {TOP_N} gainers, $20-$400, prior-day $ volume >= $50M",
                          "catalyst": "news today, volume pace >= 2x the prior day, or a gap >= 2%",
                          "standard": ">= 1 open per 10 minutes"}}
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "latest.json.tmp"
    tmp.write_text(json.dumps(res, indent=1))
    tmp.replace(OUT / "latest.json")
    with open(OUT / f"{day}.jsonl", "a") as fh:
        fh.write(json.dumps({k: res[k] for k in ("ts", "opens", "book_n", "book_leaders", "book_good", "purity_pct")}) + "\n")
    return res


def scan(tail: LedgerTail, headers, syms=None) -> dict:
    slow_scan(tail, headers, syms or universe(headers))
    return fast_scan(headers)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args(argv)
    headers = _keys()
    day = datetime.now(ET).date().isoformat()
    tail = LedgerTail(day)
    if a.once or not a.loop:
        r = scan(tail, headers)
        print(json.dumps({k: r[k] for k in ("opens", "book_n", "book_good", "purity_pct")}))
        return 0
    syms = universe(headers)
    last_slow = 0.0
    while True:
        now = datetime.now(ET)
        if now.date().isoformat() != day or now.time() >= END or now.weekday() >= 5:
            print("session over", flush=True)
            return 0
        if now.time() >= START:
            try:
                if time.time() - last_slow >= INTERVAL:
                    slow_scan(tail, headers, syms)
                    last_slow = time.time()
                fast_scan(headers)
            except Exception as e:  # noqa: BLE001
                print(f"{now:%H:%M} scan error {type(e).__name__}: {str(e)[:120]}", flush=True)
            time.sleep(60)
        else:
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
