#!/usr/bin/env python3
"""fact_picker.py - the fact-sheet picker (operator 11/11: "build the fact-sheet picker with the free data").

Once a week, before the open, every stock named in the week's headlines gets a one-line fact sheet built from free data:
valuation and financials from SEC filings (XBRL company facts, by filing date), performance, risk and technicals from
daily prices. Both AI agents (AGY/Gemini and Grok) score EVERY name 0-100 for the next one to two months from that sheet.
The two scores are averaged; the top 3 go in a paper ledger, the bottom 3 and 3 random names go in shadow ledgers, and
tools/pick_ledger.py scores all of them against SPY and random names. NOTHING HERE PLACES AN ORDER.
Rules, written first: docs/studies/fact_picker_prereg.json. Analyst targets are NOT included (no free source).

    .venv/bin/python tools/fact_picker.py run            # this week's sheet, scores and picks, then the report
    .venv/bin/python tools/fact_picker.py report         # score the three ledgers -> ~/pick_ledgers/FACTS_REPORT.txt
    .venv/bin/python tools/fact_picker.py sheet          # build and print this week's sheet only (no agent is asked)
    .venv/bin/python tools/fact_picker.py replay         # twelve PAST Mondays, a screen only (from the mini's Terminal)
    .venv/bin/python tools/fact_picker.py replay-score
    .venv/bin/python tools/fact_picker.py cikmap FILE    # one time: the SEC's company_tickers.json, saved from a browser

Like auto_picker, `run` and `replay` must run inside the mini's logged-in session (the agents' logins are in the Keychain).
SEC access: data.sec.gov needs no key. The symbol -> SEC company number table (docs/studies/sec_cik_map.json) comes from
www.sec.gov, which only answers a client that names a contact; set "sec_user_agent" in secrets_data.json once.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_picker as A  # noqa: E402
import pick_ledger as P  # noqa: E402

ROOT, ET = A.ROOT, A.ET
CIK_MAP = ROOT / "docs" / "studies" / "sec_cik_map.json"
SEC_CACHE = Path(os.environ.get("FACT_CACHE") or ROOT / "ai_reports" / "fact_picker" / "sec")
LEDGERS = ("facts", "facts_avoid", "facts_random")
MAX_POOL, MIN_BARS, MIN_SHEET, PER_WEEK, HOLD_MONTHS = 100, 252, 20, 3, 2.0
T_MIN = 2.5          # t across entry WEEKS (10 for 30 picks). Their 21-session windows overlap, so the bar is above 2.0
TAGS = {
    "rev": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"],
    "ni": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "eq": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "liab": ["Liabilities"],
    "sh": ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"],
}


# ---------- SEC facts (pure functions first) ----------
def _d(s: str) -> date:
    return date.fromisoformat(s)


def compact(raw: dict, since: str = "2023-01-01") -> dict:
    """Keep only what the sheet needs from an SEC companyfacts document: {key: {tag: [[start, end, value, filed], ...]}}.
    Money is kept in USD only and share counts in shares only (a filer reporting in another currency gets no sheet), and a
    foreign filer (20-F / 40-F: ADR share ratios are not in the data) is dropped entirely. The tag is chosen later, at the
    as-of date (pick_tag), so a filing made after that date cannot change which tag is used."""
    gaap = (raw.get("facts") or {}).get("us-gaap") or {}
    out, foreign = {}, False
    for key, tags in TAGS.items():
        unit = "shares" if key == "sh" else "USD"
        out[key] = {}
        for t in tags:
            src = ((gaap.get(t) or {}).get("units") or {}).get(unit) or []
            foreign = foreign or any(str(r.get("form", "")).startswith(("20-F", "40-F")) for r in src)
            rows = [[r.get("start") or "", r["end"], float(r["val"]), r["filed"]]
                    for r in src if r.get("end", "") >= since and r.get("filed") and r.get("val") is not None]
            if rows:
                out[key][t] = rows
    return {k: {} for k in TAGS} if foreign else out


def pick_tag(tagrows: dict, asof: str) -> list:
    """The visible rows of the tag with the most recent period end as of `asof`; on a tie, the larger latest value
    (a company's total revenue, not one line of it)."""
    best, best_key = [], None
    for rows in (tagrows or {}).values():
        v = visible(rows, asof)
        if not v:
            continue
        end = max(r[1] for r in v)
        key = (end, max(abs(r[2]) for r in v if r[1] == end))
        if best_key is None or key > best_key:
            best, best_key = v, key
    return best


def visible(rows: list, asof: str) -> list:
    """Rows filed BEFORE asof; a period restated later keeps the latest version that was already filed."""
    seen = {}
    for s, e, v, f in sorted((r for r in rows if r[3] < asof), key=lambda r: r[3]):
        seen[(s, e)] = [s, e, v, f]
    return list(seen.values())


def _days(r) -> int:
    return (_d(r[1]) - _d(r[0])).days if r[0] else 0


def _find(rows: list, end: date, length: int, tol: int = 12):
    c = [r for r in rows if r[0] and abs((_d(r[1]) - end).days) <= tol and abs(_days(r) - length) <= tol]
    return max(c, key=lambda r: r[3]) if c else None


def latest_period(rows: list):
    """The longest reported period that ends at the most recent period end (a year, or the year to date)."""
    dur = [r for r in rows if r[0]]
    if not dur:
        return None
    end = max(r[1] for r in dur)
    return max((r for r in dur if r[1] == end), key=_days)


def ttm(rows: list) -> float | None:
    """Trailing twelve months: the latest full year, or full year + this year to date - last year's same span."""
    cur = latest_period(rows)
    if cur is None:
        return None
    n = _days(cur)
    if 350 <= n <= 380:
        return cur[2]
    fy = _find(rows, _d(cur[0]) - timedelta(days=1), 365)
    prior = _find(rows, _d(cur[1]) - timedelta(days=365), n)
    if fy is None or prior is None:
        return None
    return fy[2] + cur[2] - prior[2]


def yoy(rows: list) -> float | None:
    """Growth of the latest reported period against the same span a year earlier."""
    cur = latest_period(rows)
    if cur is None:
        return None
    prior = _find(rows, _d(cur[1]) - timedelta(days=365), _days(cur))
    if prior is None or prior[2] <= 0:
        return None
    return cur[2] / prior[2] - 1


def instant(rows: list) -> float | None:
    return max(rows, key=lambda r: (r[1], r[3]))[2] if rows else None


def fundamentals(facts: dict, asof: str, price: float, split_for=None) -> dict | None:
    """Valuation and financials as known before `asof`. None when revenue, income or the share count is missing or stale.
    split_for(day) -> shares today per share on `day` (raw close / split-adjusted close): the filed share count is from
    before any later split, the adjusted price is from after it, so the count is scaled to match."""
    v = {k: pick_tag(facts.get(k, {}), asof) for k in TAGS}
    rev, ni, sh_row = ttm(v["rev"]), ttm(v["ni"]), latest_period(v["sh"])
    if rev is None or ni is None or sh_row is None or rev <= 0 or sh_row[2] <= 0:
        return None
    last = latest_period(v["rev"])
    if (_d(asof) - _d(last[1])).days > 300:                               # nothing filed for most of a year: not a current sheet
        return None
    mcap = price * sh_row[2] * (split_for(sh_row[1]) if split_for else 1.0)
    eq, liab, ocf = instant(v["eq"]), instant(v["liab"]), ttm(v["ocf"])
    return {"mcap": mcap, "pe": mcap / ni if ni > 0 else None, "ps": mcap / rev, "pb": mcap / eq if eq and eq > 0 else None,
            "rev_growth": yoy(v["rev"]), "margin": ni / rev, "ocf_ni": ocf / ni if ocf is not None and ni > 0 else None,
            "liab_eq": liab / eq if liab is not None and eq and eq > 0 else None, "period_end": last[1], "loss": ni <= 0}


# ---------- price facts ----------
def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    ch = [b - a for a, b in zip(closes, closes[1:])]
    up, dn = sum(max(c, 0) for c in ch[:n]) / n, sum(max(-c, 0) for c in ch[:n]) / n
    for c in ch[n:]:
        up, dn = (up * (n - 1) + max(c, 0)) / n, (dn * (n - 1) + max(-c, 0)) / n
    if dn == 0:
        return 100.0 if up > 0 else 50.0
    return 100 - 100 / (1 + up / dn)


def price_facts(closes: list[float], spy: list[float]) -> dict | None:
    """closes and spy are aligned daily closes, oldest first, ending at the last close BEFORE the run."""
    if len(closes) < MIN_BARS + 1 or len(spy) != len(closes):          # 252 daily returns need 253 closes
        return None
    c = closes
    ret = lambda n: c[-1] / c[-1 - n] - 1 if len(c) > n else None  # noqa: E731
    lr = [math.log(b / a) for a, b in zip(c[-61:], c[-60:])]
    x = [b / a - 1 for a, b in zip(c[-253:], c[-252:])]
    m = [b / a - 1 for a, b in zip(spy[-253:], spy[-252:])]
    mm, xm = statistics.fmean(m), statistics.fmean(x)
    var = sum((v - mm) ** 2 for v in m)
    peak, dd = c[-252], 0.0
    for v in c[-252:]:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"price": c[-1], "r1": ret(21), "r3": ret(63), "r6": ret(126), "r12": ret(251),
            "r3_vs_spy": (1 + ret(63)) / (spy[-1] / spy[-64]) - 1, "vol": statistics.pstdev(lr) * math.sqrt(252), "dd": dd,
            "beta": sum((a - xm) * (b - mm) for a, b in zip(x, m)) / var if var > 0 else None,
            "v50": c[-1] / statistics.fmean(c[-50:]) - 1, "v200": c[-1] / statistics.fmean(c[-200:]) - 1,
            "rsi": rsi(c[-120:]), "off_high": c[-1] / max(c[-252:]) - 1}


# ---------- the sheet ----------
def _p(v, plus: bool = True) -> str:
    return "n/a" if v is None else (f"{100 * v:+.0f}%" if plus else f"{100 * v:.0f}%")


def _x(v, nd: int = 1) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def _money(v: float) -> str:
    return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.0f}M"


def sheet_line(sym: str, f: dict, p: dict, news: list[str]) -> str:
    pe = "loss" if f["loss"] else _x(f["pe"])
    return (f"{sym} | VALUE: size {_money(f['mcap'])}, P/E {pe}, P/S {_x(f['ps'])}, P/B {_x(f['pb'])}"
            f" | FINANCIALS (to {f['period_end']}): revenue {_p(f['rev_growth'])} vs year ago, net margin {_p(f['margin'], False)}, "
            f"cash flow/income {_x(f['ocf_ni'])}, liabilities/equity {_x(f['liab_eq'])}"
            f" | PERFORMANCE: 1m {_p(p['r1'])}, 3m {_p(p['r3'])}, 6m {_p(p['r6'])}, 12m {_p(p['r12'])}, 3m vs S&P {_p(p['r3_vs_spy'])}"
            f" | RISK: volatility {_p(p['vol'], False)}, worst drop in a year {_p(p['dd'])}, beta {_x(p['beta'])}"
            f" | TECHNICALS: vs 50-day avg {_p(p['v50'])}, vs 200-day avg {_p(p['v200'])}, RSI {_x(p['rsi'], 0)}, below 52-week high {_p(p['off_high'])}"
            + (f" | NEWS: {' / '.join(news[:2])}" if news else ""))


def mentions(heads: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for h in heads:
        syms, _, text = h.partition(":")
        for s in syms.split(","):
            out.setdefault(s.strip(), []).append(text.strip()[:110])
    return out


def split_factor(raw: dict, adj: dict, day: str) -> float:
    """Shares today per share on `day`: raw close / split-adjusted close on the last session up to `day` (1.0 if unknown)."""
    ds = [d for d in raw if d <= day and d in adj]
    if not ds:
        return 1.0
    d = max(ds)
    return raw[d][1] / adj[d][1] if adj[d][1] > 0 else 1.0


def build_sheet(heads: list[str], asof: str, bars: dict[str, dict], facts_for, split_for=None) -> dict[str, str]:
    """{symbol: sheet line} for the pool. bars = {sym: {day: (open, close)}}; facts_for(sym) -> compact SEC facts or None.
    Only days before `asof` are used, and only names with full price history and current SEC facts are kept."""
    ment = mentions(heads)
    days = [d for d in sorted(bars.get("SPY", {})) if d < asof]
    rows = {}
    for sym in sorted(ment, key=lambda s: (-len(ment[s]), s)):
        b = bars.get(sym) or {}
        use = [d for d in days[-(MIN_BARS + 8):] if d in b]
        if len(use) < MIN_BARS + 1 or use[-1] != days[-1]:
            continue
        p = price_facts([b[d][1] for d in use], [bars["SPY"][d][1] for d in use])
        fx = facts_for(sym)
        f = fundamentals(fx, asof, p["price"], (lambda d, s=sym: split_for(s, d)) if split_for else None) if (p and fx) else None
        if f is None:
            continue
        rows[sym] = sheet_line(sym, f, p, ment[sym])
        if len(rows) >= MAX_POOL:
            break
    return dict(sorted(rows.items()))


def build_prompt(today: str, sheet: dict[str, str]) -> str:
    """The wording is fixed; only the date and the sheet change."""
    return (
        f"Today is {today}. You are scoring stocks for a paper record that tests whether your judgment beats the S&P 500.\n"
        "No money is traded. Each chosen stock is bought on paper at the next market open and judged against SPY after one to two months.\n\n"
        "Answer directly in your reply. Do NOT run commands, write files, browse, or use any tool: everything you need is in this message.\n\n"
        f"Below is a FACT SHEET with {len(sheet)} US stocks, one per line. The figures are computed from SEC filings and daily prices as of the last close.\n"
        "Give EVERY stock a score from 0 to 100: how likely it is to beat the S&P 500 over the next one to two months\n"
        "(50 = no view, 100 = most likely to beat it, 0 = most likely to trail it). Use the whole range; do not give most names the same score.\n"
        "Weigh valuation, financial health, recent performance, risk, technicals and the news together.\n"
        "Use only the figures on the sheet. Do not add analyst price targets, estimates or other numbers from memory. 'n/a' means unknown.\n\n"
        'Reply with JSON ONLY, exactly this shape, one entry for every symbol on the sheet:\n{"scores": {"ABC": 72, "XYZ": 31}}\n\n'
        "FACT SHEET:\n" + "\n".join(sheet.values()) + "\n"
    )


def parse_scores(text: str, pool: set[str]) -> dict[str, float]:
    m = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not m:
        return {}
    try:
        raw = json.loads(m.group(0)).get("scores", {})
    except (ValueError, AttributeError):
        return {}
    out = {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        sym = str(k).strip().upper()
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if sym in pool and 0 <= x <= 100 and not math.isnan(x):
            out[sym] = x
    return out


def combine(scores: dict[str, dict[str, float]], pool: list[str]) -> list[tuple[str, float]]:
    """Mean of the agents' scores over names every agent scored, best first (ties alphabetical).
    Empty when any agent scored fewer than half the pool: a half-answered sheet is not a ranking."""
    if not scores or any(len(s) < len(pool) / 2 for s in scores.values()):
        return []
    both = [x for x in pool if all(x in s for s in scores.values())]
    return sorted(((x, statistics.fmean(s[x] for s in scores.values())) for x in both), key=lambda t: (-t[1], t[0]))


def choose(ranked: list[tuple[str, float]], pool: list[str], day: str) -> dict[str, list[str]]:
    """Candidate order for each ledger: top scores, bottom scores, and a seeded random order of the same pool."""
    order = [s for s, _ in ranked]
    rnd = sorted(pool)
    random.Random(f"facts-{day}").shuffle(rnd)
    return {"facts": order, "facts_avoid": order[::-1], "facts_random": rnd}


# ---------- I/O (mini only) ----------
def _agent_ua() -> str:
    try:
        with open(ROOT / "secrets_data.json") as fh:
            return str(json.load(fh).get("sec_user_agent") or "")
    except (OSError, ValueError):
        return ""


def _get(url: str, ua: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def cik_map() -> dict[str, int]:
    if CIK_MAP.exists():
        return json.loads(CIK_MAP.read_text())
    ua = _agent_ua()
    if not ua:
        raise RuntimeError('no SEC company-number table yet: set "sec_user_agent" (a name and contact, as the SEC asks) in secrets_data.json and run again')
    return write_cik_map(_get("https://www.sec.gov/files/company_tickers.json", ua))


def write_cik_map(table: dict) -> dict[str, int]:
    """The SEC's company_tickers.json (fetched, or saved from a browser) -> the frozen list's symbol -> company number."""
    uni = set(P.frozen_universe())
    out = {str(r["ticker"]).upper().replace("-", "."): int(r["cik_str"]) for r in table.values()}
    out = {k: v for k, v in sorted(out.items()) if k in uni}
    CIK_MAP.write_text(json.dumps(out, indent=0) + "\n")
    return out


def sec_facts(sym: str, ciks: dict[str, int], max_age_days: float = 6.0) -> dict | None:
    """Compact SEC facts for one name, cached on disk for the week."""
    cik = ciks.get(sym)
    if not cik:
        return None
    f = SEC_CACHE / f"{sym}.json"
    if f.exists() and time.time() - f.stat().st_mtime < max_age_days * 86400:
        return json.loads(f.read_text())
    try:
        raw = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", _agent_ua() or "trading-helper fact-picker")
    except Exception:  # noqa: BLE001
        if f.exists():
            print(f"{sym}: SEC fetch failed, using the copy from {date.fromtimestamp(f.stat().st_mtime)}", flush=True)
        return json.loads(f.read_text()) if f.exists() else None
    time.sleep(0.15)                                                      # the SEC's limit is 10 requests a second
    out = compact(raw)
    SEC_CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out))
    return out


def fetch_adj(symbols: list[str], start: datetime, adjustment: str) -> dict[str, dict]:
    """Daily bars like pick_ledger.fetch, with a chosen adjustment ('raw' or 'split')."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    with open(ROOT / "secrets_data.json") as fh:
        k = json.load(fh)
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    out: dict[str, dict] = {}
    end = datetime.now(ET) - timedelta(minutes=20)
    for i in range(0, len(symbols), 50):
        r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols[i:i + 50], timeframe=TimeFrame.Day, start=start, end=end,
                                               feed=DataFeed.SIP, adjustment=Adjustment(adjustment)))
        for sym, rows in (r.data or {}).items():
            out[sym] = {x.timestamp.astimezone(ET).strftime("%Y-%m-%d"): (float(x.open), float(x.close)) for x in rows}
    return out


def split_lookup(symbols: list[str], start: datetime):
    raw, spl = fetch_adj(symbols, start, "raw"), fetch_adj(symbols, start, "split")
    return lambda sym, day: split_factor(raw.get(sym, {}), spl.get(sym, {}), day)


def make_sheet(when: datetime, heads: list[str] | None = None) -> tuple[dict[str, str], int]:
    uni = set(P.frozen_universe())
    heads = heads if heads is not None else A.headlines(uni, end=when)
    ciks = cik_map()
    named = sorted(mentions(heads))
    bars = P.fetch(sorted(set(named) | {"SPY"}), when - timedelta(days=420))
    splits = split_lookup(named, when - timedelta(days=800))
    return build_sheet(heads, when.strftime("%Y-%m-%d"), bars, lambda x: sec_facts(x, ciks), splits), len(heads)


def ask_all(prompt: str, pool: set[str], raw_for, ask=None) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    ask = ask or A.ask_agent
    scores, models = {}, {}
    for agent in A.AGENTS:
        f: Path = raw_for(agent)
        text = f.read_text() if f.exists() else ""
        if not parse_scores(text, pool):
            for _try in range(2):                                         # one retry with the SAME prompt
                try:
                    text, models[agent] = ask(agent, prompt)
                except Exception as e:  # noqa: BLE001
                    text = ""
                    print(f"{agent}: {type(e).__name__} {e!s:.120}", flush=True)
                if len(parse_scores(text, pool)) >= len(pool) / 2:
                    break
            f.write_text(str(text or ""))
        scores[agent] = parse_scores(text, pool)
    return scores, models


def record(order: dict[str, list[str]], ranked: list[tuple[str, float]], scores: dict, *, now: datetime, extra: dict,
           universe: list[str] | None = None) -> dict[str, list[str]]:
    comb = dict(ranked)
    done: dict[str, list[str]] = {}
    for led in LEDGERS:
        A.ensure_repo(led)
        path = A.ledger_path(led)
        done[led] = []
        if A.this_week_done(led, now):                                    # a rerun after a crash fills only what is missing
            continue
        for sym in order[led]:
            if len(done[led]) >= PER_WEEK:
                break
            why = (f"fact-sheet {led}: combined score {comb.get(sym, float('nan')):.0f} of 100 ("
                   + ", ".join(f"{a} {s[sym]:.0f}" for a, s in scores.items() if sym in s) + f"), pool of {extra.get('pool')}")
            try:
                r = P.add_pick(sym, why, HOLD_MONTHS, "held for the test window; no sell rule", now=now, path=path, universe=universe,
                               extra={"source": led, **extra})
            except ValueError:
                continue
            if not P.git_commit(path, r["symbol"]):
                raise RuntimeError(f"{led}: could not commit {r['symbol']} in {path.parent}")
            done[led].append(sym)
    return done


def run(now: datetime | None = None) -> int:
    now = now or datetime.now(ET)
    day = now.strftime("%Y-%m-%d")
    if all(A.this_week_done(k, now) for k in LEDGERS):
        print("facts: already recorded this week")
        print(report())
        return 0
    try:
        sheet, n_heads = make_sheet(now)
    except Exception as e:  # noqa: BLE001
        print(f"could not build the sheet: {type(e).__name__} {e!s:.300}. Nothing recorded.")
        return 1
    if len(sheet) < MIN_SHEET:
        print(f"only {len(sheet)} names have a full fact sheet ({n_heads} headlines); too few to rank. Nothing recorded.")
        return 1
    raw = A.HOME / "facts" / "raw"
    A.ensure_repo("facts")
    raw.mkdir(exist_ok=True)
    (raw / f"{day}_sheet.txt").write_text("\n".join(sheet.values()) + "\n")
    prompt, pool = build_prompt(day, sheet), sorted(sheet)
    scores, models = ask_all(prompt, set(pool), lambda a: raw / f"{day}_{a}.txt")
    ranked = combine(scores, pool)
    (raw / f"{day}_scores.json").write_text(json.dumps({"scores": scores, "combined": ranked, "models": models}, indent=1))
    if not ranked:
        print(f"an agent scored fewer than half of the {len(pool)} names ({ {a: len(s) for a, s in scores.items()} }). Nothing recorded.")
        return 1
    sha = hashlib.sha1(build_prompt("DATE", {}).encode()).hexdigest()[:10]
    done = record(choose(ranked, pool, day), ranked, scores, now=now,
                  extra={"models": models, "prompt_sha": sha, "pool": len(pool), "headlines": n_heads})
    d = str(A.HOME / "facts")                                             # the sheet, replies and scores go in the same git record
    subprocess.run(["git", "-C", d, "add", "raw"], check=False)
    subprocess.run(["git", "-C", d, "commit", "-q", "-m", f"sheet, replies and scores {day}"], check=False)
    print("; ".join(f"{k}: {', '.join(v) or 'none'}" for k, v in done.items()))
    print(report())
    return 0


# ---------- scoring ----------
def _line(name: str, v: dict) -> str:
    if "mean_vs_spy" not in v:
        return f"{name:<13} {v['verdict']} {'; '.join(v.get('why', []))}"
    return (f"{name:<13} {v['counted']:3d} picks  vs SPY mean {P.pct(v['mean_vs_spy'])} median {P.pct(v['median_vs_spy'])}  t {v['t']:+.2f}  "
            f"ahead {100 * v['ahead']:.0f}%  vs random names {P.pct(v['mean_vs_random'])}  rank {100 * v['mean_rank']:.0f}th pct  beta-adj {P.pct(v['mean_beta_excess'])}")


def week_verdict(scored: list[dict], need: int = P.NEED, today: str | None = None) -> dict:
    """pick_ledger's verdict at 21 sessions, with the t clustered by entry WEEK instead of entry month: 30 picks at 3 a week
    span about three calendar months, and pick_ledger's month clustering needs four (it would return no t at all)."""
    v = P.verdict(scored, h=P.EARLY, need=need, today=today, t_min=T_MIN)
    if "mean_vs_spy" not in v:
        return v
    first = sorted((x for x in scored if x.get("counts", True)), key=lambda x: datetime.fromisoformat(x["recorded_at"]))[:need]
    wk = ["%d-W%02d" % datetime.fromisoformat(x["recorded_at"]).isocalendar()[:2] for x in first]
    v["t"], v["weeks"] = P.cluster_t([x["h"][P.EARLY]["vs_spy"] for x in first], wk), len(set(wk))
    ok = (v["mean_vs_spy"] > 0 and v["t"] >= T_MIN and v["ahead"] > 0.5 and v["median_vs_spy"] > 0 and v["mean_vs_random"] > 0
          and v["mean_rank"] > 0.5 and v["mean_beta_excess"] > 0)
    v["verdict"] = "PASS" if ok else "FAIL"
    return v


def pool_control(pools: dict[str, list[str]], stamps: dict[str, str], days: list[str], bars: dict[str, dict]) -> float | None:
    """The no-skill line: every name on each week's sheet, bought at the same open and held 21 sessions, as a mean ratio
    lift vs SPY (names averaged inside a week, then weeks averaged). None if any week cannot be scored."""
    out = []
    for day, names in pools.items():
        a = P.entry_index(days, stamps[day])
        if a is None or a + P.EARLY > len(days) - 1:
            return None
        m = P.span(bars["SPY"], days, a, a + P.EARLY)
        x = [(1 + r) / (1 + m) - 1 for r in (P.span(bars.get(n, {}), days, a, a + P.EARLY) for n in names) if r is not None]
        if m is None or len(x) < 0.8 * len(names):
            return None
        out.append(statistics.fmean(x))
    return statistics.fmean(out) if out else None


def read_pools(raw: Path, days: list[str]) -> dict[str, list[str]]:
    out = {}
    for d in days:
        f = raw / f"{d}_scores.json"
        if f.exists():
            out[d] = [x[0] for x in json.loads(f.read_text()).get("combined", [])]
    return out


def judge(v: dict, control: float | None) -> str:
    """The prereg read: PASS at 21 sessions (t across entry weeks) AND a mean vs SPY above the whole sheet's."""
    if v["verdict"] not in ("PASS", "FAIL"):
        return v["verdict"]
    if control is None:
        return "BLOCKED (the whole-sheet control could not be scored)"
    return "PASS" if v["verdict"] == "PASS" and v["mean_vs_spy"] > control else "FAIL"


def report() -> str:
    parts = [f"Fact-sheet picker, written {datetime.now(ET):%Y-%m-%d %H:%M} ET. Paper record only; no orders are placed.",
             "facts = top 3 combined scores each week; facts_avoid = bottom 3; facts_random = 3 random names from the same sheet.\n"]
    for led in LEDGERS:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                P.cmd_score(A.ledger_path(led))
            except Exception as e:  # noqa: BLE001
                print(f"could not score: {type(e).__name__} {e!s:.200}")
        table = buf.getvalue().split("\nEARLY LOOK")[0]                     # the ledger's own verdict lines belong to another test
        parts.append(f"===== {led.upper()} =====\n{table}")
    try:
        parts.append(live_verdict())
    except Exception as e:  # noqa: BLE001
        parts.append(f"verdict could not be computed: {type(e).__name__} {e!s:.200}")
    text = "\n".join(parts)
    A.HOME.mkdir(parents=True, exist_ok=True)
    (A.HOME / "FACTS_REPORT.txt").write_text(text + "\n")
    return text


def live_verdict() -> str:
    led = {k: P.read_ledger(A.ledger_path(k)) for k in LEDGERS}
    picks = [p for rows in led.values() for p in rows]
    if not picks:
        return "no picks recorded yet"
    first = sorted(led["facts"], key=lambda p: datetime.fromisoformat(p["recorded_at"]))[:P.NEED]
    stamps = {p["recorded_at"][:10]: p["recorded_at"] for p in first}
    pools = read_pools(A.HOME / "facts" / "raw", sorted(stamps))
    need = {p["symbol"] for p in picks} | {"SPY"} | {c for p in picks for c in p["controls"]} | {n for v in pools.values() for n in v}
    bars = P.fetch(sorted(need), min(datetime.fromisoformat(p["recorded_at"]) for p in picks) - timedelta(days=420))
    days = sorted(bars["SPY"])
    sc = {k: [P.score_pick(p, days, bars, commit_ts=P.first_commit_ts(p["id"], A.ledger_path(k))) for p in rows] for k, rows in led.items()}
    v = week_verdict(sc["facts"], today=datetime.now(ET).strftime("%Y-%m-%d"))
    ctl = pool_control(pools, stamps, days, bars) if len(pools) == len(stamps) else None
    rows = [f"VERDICT at {P.EARLY} sessions on the first {P.NEED} picks (docs/studies/fact_picker_prereg.json): {judge(v, ctl)}",
            _line("facts", v), f"whole sheet   {'not scoreable yet' if ctl is None else 'vs SPY mean ' + P.pct(ctl)}  (the control the picks must beat)"]
    models = sorted({json.dumps(p.get("models", {}), sort_keys=True) for p in first})
    if len(models) > 1:
        rows.append(f"NOTE: the agents' models changed during the record: {'; '.join(models)}")
    for k in LEDGERS[1:]:                                                  # reported, not judged
        ok = [x for x in sc[k] if x["recorded_at"][:10] in stamps and x["status"] == "scored" and x["h"].get(P.EARLY, {}).get("closed")]
        if ok:
            rows.append(_line(k, P.verdict(ok, h=P.EARLY, need=len(ok))))
    return "\n".join(rows)


# ---------- replay on past weeks (a screen only) ----------
def replay_collect(ask=None, heads_for=None, bars=None, facts_for=None) -> dict[str, list[dict]]:
    uni = P.frozen_universe()
    out_dir = A.HOME / "facts_replay"
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    heads_for = heads_for or (lambda mon: A.headlines(set(uni), end=mon))
    mondays = A.replay_mondays()
    weeks = {mon: heads_for(mon) for mon in mondays}
    if facts_for is None:
        ciks = cik_map()
        facts_for = lambda s: sec_facts(s, ciks, max_age_days=30)  # noqa: E731
    splits = None
    if bars is None:
        named = sorted({s for h in weeks.values() for s in mentions(h)})
        bars = P.fetch(sorted(set(named) | {"SPY"}), mondays[0] - timedelta(days=420))
        splits = split_lookup(named, mondays[0] - timedelta(days=800))
    lines: dict[str, list[dict]] = {k: [] for k in LEDGERS}
    taken: dict[str, dict] = {k: {} for k in LEDGERS}
    for mon in mondays:
        day = mon.strftime("%Y-%m-%d")
        sheet = build_sheet(weeks[mon], day, bars, facts_for, splits)
        if len(sheet) < MIN_SHEET:
            print(f"{day}: only {len(sheet)} names with a full sheet, week skipped", flush=True)
            continue
        pool = sorted(sheet)
        (out_dir / "raw" / f"{day}_sheet.txt").write_text("\n".join(sheet.values()) + "\n")
        scores, _ = ask_all(build_prompt(day, sheet), set(pool), lambda a: out_dir / "raw" / f"{day}_{a}.txt", ask=ask)
        ranked = combine(scores, pool)
        if not ranked:
            print(f"{day}: an agent scored fewer than half the names, week skipped", flush=True)
            continue
        comb = dict(ranked)
        (out_dir / "raw" / f"{day}_scores.json").write_text(json.dumps({"scores": scores, "combined": ranked}))
        for k, order in choose(ranked, pool, day).items():
            ideas = [{"sym": s, "thesis": f"fact-sheet {k}: combined score {comb.get(s, float('nan')):.0f} of 100", "months": HOLD_MONTHS,
                      "sell_if": "held for the test window"} for s in order]
            for p in A.take_three(ideas, taken[k], mon):
                lines[k].append(A.replay_line(lines[k][-1]["id"] if lines[k] else "", k, p, mon, uni))
        print(f"{day}: {len(pool)} names on the sheet; top {', '.join(s for s, _ in ranked[:3])}", flush=True)
    for k, rows in lines.items():
        (out_dir / f"{k}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return lines


def replay_score(lines: dict[str, list[dict]] | None = None) -> str:
    out_dir = A.HOME / "facts_replay"
    lines = lines or {k: P.read_ledger(out_dir / f"{k}.jsonl") for k in LEDGERS}
    picks = [p for rows in lines.values() for p in rows]
    if not picks:
        return "no replay picks"
    stamps = {p["recorded_at"][:10]: p["recorded_at"] for p in lines["facts"]}
    pools = read_pools(out_dir / "raw", sorted(stamps))
    need = {p["symbol"] for p in picks} | {"SPY"} | {c for p in picks for c in p["controls"]} | {n for v in pools.values() for n in v}
    bars = P.fetch(sorted(need), min(datetime.fromisoformat(p["recorded_at"]) for p in picks) - timedelta(days=420))
    days = sorted(bars["SPY"])
    rows = [f"FACT-SHEET REPLAY, {A.replay_mondays()[0]:%Y-%m-%d} to {A.REPLAY_LAST}, scored {P.EARLY} sessions after entry. A SCREEN, not proof:",
            "the agents may have been trained on this period, and the stock list was chosen AFTER it (names that did badly are missing),",
            "which flatters every line against SPY. Compare the picks with the whole sheet, not with zero.", ""]
    res = {}
    for k, lst in lines.items():
        ok = [x for x in (P.score_pick(p, days, bars, commit_ts=0.0) for p in lst) if x["status"] == "scored" and x["h"].get(P.EARLY, {}).get("closed")]
        if len(ok) < 12:
            rows.append(f"{k:<13} only {len(ok)} scoreable picks")
            continue
        res[k] = week_verdict(ok, need=len(ok))
        rows.append(_line(k, res[k]))
    ctl = pool_control(pools, stamps, days, bars) if pools and len(pools) == len(stamps) else None
    rows.append(f"whole sheet   {'not scoreable' if ctl is None else 'vs SPY mean ' + P.pct(ctl)}  (the control the picks must beat)")
    if "facts" in res:
        rows += ["", f"FACTS: {'SCREEN POSITIVE (not proof; the live record decides)' if judge(res['facts'], ctl) == 'PASS' else 'SCREEN NEGATIVE'}"]
    text = "\n".join(rows)
    (out_dir / "REPORT.txt").write_text(text + "\n")
    return text


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["run"])[0]
    if cmd == "cikmap":                                                   # fact_picker.py cikmap ~/Downloads/company_tickers.json
        m = write_cik_map(json.loads(Path(sys.argv[2]).expanduser().read_text()))
        print(f"{len(m)} of {len(P.frozen_universe())} listed symbols matched -> {CIK_MAP}")
        sys.exit(0)
    if cmd == "sheet":
        s, n = make_sheet(datetime.now(ET))
        print("\n".join(s.values()) + f"\n\n{len(s)} names from {n} headlines")
        sys.exit(0)
    sys.exit(run() if cmd == "run" else (print(replay_score(replay_collect())) or 0) if cmd == "replay"
             else (print(replay_score()) or 0) if cmd == "replay-score" else (print(report()) or 0))
