#!/usr/bin/env python3
"""Wait, and buy only if it held. Spec: docs/studies/wait_held_prereg.json.

The inverse of the limit-below entry (FAIL 10/14): when the pre-square fires at minute m, do nothing
for W minutes; buy at the SIP open of m+W+1 only if the IEX low never traded k bp below the signal's
IEX close during m+1..m+W (the dip the desk can see did not come). 10-minute hold from the entry,
10 bp round trip on every arm.

Arms (all on the same signals):
  held      the rule: enter at m+W+1 only on held signals
  wait_all  enter at m+W+1 on EVERY signal (separates the filter from the delay)
  market    enter at m+1 on every signal (today's entry)

  (default)        inventory only, no returns
  --score          graded forward read (sessions >= FORWARD_START); after a result-skeptic READY
  --in-sample      the 26 sessions that suggested the idea; information that can only kill it

Cache only (ai_reports/early_entry_honest/<day>.pkl, fetched nightly by scripts/nightly.sh).
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402
import limit_below_study as L  # noqa: E402

PRIMARY = (5, 10)               # (W minutes, k bp)
GRID_W, GRID_K = (2, 5, 10), (5, 10, 20)
HOLD = 10
COST = 0.0010
IN_SAMPLE_END = "2026-10-13"
FORWARD_START = "2026-10-15"
READ_AT, EXTEND_TO = 10, 20     # forward sessions: one read at 10; if UNDERPOWERED, one more at 20
MIN_HELD, MIN_DAYS = 60, 10
LIFT_BAR = 0.0005


# ── pure ─────────────────────────────────────────────────────────────────────

def held(lows: dict, m: int, ref: float, w: int, k_bp: float) -> bool:
    """True when no low in m+1..m+w is at or below ref x (1 - k/10000). Missing minutes are no dip
    (live sees nothing there either). `lows`: {minute: low}."""
    floor = ref * (1 - k_bp / 1e4)
    return all(not (lows.get(j) is not None and lows[j] <= floor) for j in range(m + 1, m + w + 1))


def entry_trade(bars: dict, entry_min: int, hold: int = HOLD) -> float | None:
    """Gross: SIP open of entry_min to the SIP close of entry_min + hold - 1 (last close carried over a
    missing bar, limit_below.exit_close). None past 16:00 or with no entry bar."""
    b = bars.get(entry_min)
    if b is None or not b[0]:
        return None
    x = L.exit_close(bars, entry_min, hold)
    return None if x is None else x / b[0] - 1


def half_verdict(held_net, held_t, lift, lift_t, vs_mkt, n_held, n_days) -> str:
    """A miss on t alone, with every point estimate on the passing side, is UNDERPOWERED, not FAIL
    (skeptic #2: 10 day clusters need roughly +25-30 bp to reach t 2)."""
    if n_held < MIN_HELD or n_days < MIN_DAYS or None in (held_net, held_t, lift, lift_t, vs_mkt):
        return "UNDERPOWERED"
    points = held_net > 0 and lift >= LIFT_BAR and vs_mkt >= 0
    if not points:
        return "FAIL"
    return "PASS" if held_t >= 2.0 and lift_t >= 2.0 else "UNDERPOWERED"


def read_sessions(fwd: list[str], verdict_at) -> list[str] | None:
    """The graded session set: the first READ_AT; the first EXTEND_TO only when the READ_AT read was
    UNDERPOWERED (skeptic #1). None = not due. verdict_at(sessions) -> verdict."""
    if len(fwd) < READ_AT:
        return None
    first = fwd[:READ_AT]
    if len(fwd) >= EXTEND_TO and verdict_at(first) == "UNDERPOWERED":
        return fwd[:EXTEND_TO]
    return first


def kill_check(in_sample: dict) -> bool:
    """A sanity floor, not a test (skeptic #7: almost surely passes): True (kill) when the filter's lift over wait_all is <= 0
    in BOTH halves of the sessions that suggested it."""
    lifts = [x["lift"] for x in in_sample.values()]
    return all(v is not None and v <= 0 for v in lifts)


# ── scoring ──────────────────────────────────────────────────────────────────

SKIPS = Counter()      # signal extraction only; score() counts its own skips


def signals(datas: list[dict]) -> list[dict]:
    out = L.signals(datas)
    lows = {}
    for data in datas:
        for sym, fr in data["iex"].items():
            lows[(data["day"], sym)] = {m: v[2] for m, v in L.ohlc_map(fr).items()}
    for s in out:
        s["iex_low"] = lows.get((s["day"], s["sym"]), {})
        s["sip_low"] = {m: v[2] for m, v in s["bars"].items()}
    SKIPS.update(L.SKIPS)
    return out


def full_coverage(lows: dict, m: int, w: int) -> bool:
    return all(lows.get(j) is not None for j in range(m + 1, m + w + 1))


def score(sigs: list[dict], w: int, k: float, low_src: str = "iex", control: bool = False,
          lag: int = 0, full_only: bool = False) -> dict:
    """One part of signals -> stats. control=True moves the signal to the harness's later-only random
    minute of the same name and day (reference = the IEX close there). lag=1 enters held and wait_all
    one minute later (m+W+2: IEX bar m+W is published seconds after m+W+1 opens; skeptic #3).
    full_only keeps signals with an IEX bar in every wait minute (skeptic #4)."""
    held_by, wait_by, mkt_by, lift_by, vs_by = (defaultdict(list) for _ in range(5))
    n_sig = n_held = 0
    skips = Counter()
    for s in sigs:
        m = s["m"]
        ref = s.get("ref")
        if control:
            sipcl = {kk: (v[0], v[3]) for kk, v in s["bars"].items()}
            kc = E.control_minute(s["sym"], s["day"], "WAITHELD", sipcl, set(), m + 1)
            if kc is None:
                continue
            m = kc - 1
            ref = s["iex_close"].get(m)
        if ref is None:
            skips["no_iex_reference"] += 1
            continue
        if full_only and not full_coverage(s["iex_low"], m, w):
            skips["iex_gap_in_wait"] += 1
            continue
        mk = L.market_trade(s["bars"], m)
        wt = entry_trade(s["bars"], m + w + 1 + lag)
        if mk is None or wt is None:
            skips["no_entry_or_exit_bar"] += 1
            continue
        n_sig += 1
        d = s["day"]
        mkt_by[d].append(mk - COST)
        wait_by[d].append(wt - COST)
        if held(s["iex_low"] if low_src == "iex" else s["sip_low"], m, ref, w, k):
            n_held += 1
            held_by[d].append(wt - COST)
    for d, v in held_by.items():
        lift_by[d].append(sum(v) / len(v) - sum(wait_by[d]) / len(wait_by[d]))
        vs_by[d].append(sum(v) / len(v) - sum(mkt_by[d]) / len(mkt_by[d]))
    hn, ht, _n, nd = E.day_equal(held_by)
    lf, lt, _, _ = E.day_equal(lift_by)
    vm, vt, _, _ = E.day_equal(vs_by)
    return {"held_net": hn, "held_t": ht, "wait_net": E.day_equal(wait_by)[0], "mkt_net": E.day_equal(mkt_by)[0],
            "lift": lf, "lift_t": lt, "vs_mkt": vm, "vs_mkt_t": vt, "signals": n_sig, "held": n_held,
            "keep": (n_held / n_sig) if n_sig else None, "days": nd, "skips": dict(skips),
            "verdict": half_verdict(hn, ht, lf, lt, vm, n_held, nd)}


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def _t(x):
    return "—" if x is None else f"{x:+.2f}"


def _row(name, x):
    keep = "—" if x["keep"] is None else f"{x['keep']:.0%}"
    return (f"| {name} | {x['days']} | {x['signals']} | {x['held']} ({keep}) | {_bp(x['held_net'])} ({_t(x['held_t'])}) "
            f"| {_bp(x['wait_net'])} | {_bp(x['mkt_net'])} | {_bp(x['lift'])} ({_t(x['lift_t'])}) | {_bp(x['vs_mkt'])} | {x['verdict']} |")


HEAD = ["| part | days | signals | held (kept) | held net (t) | wait-all net | market net | lift vs wait-all (t) | held - market | verdict |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]


def info_lines(parts: dict) -> list[str]:
    w, k = PRIMARY
    out = ["", "### Information only", ""]
    for name, sg in parts.items():
        x0 = score(sg, w, k)
        c = score(sg, w, k, control=True)
        flag = ("CONTROL >= SIGNAL: a generic quiet-minutes effect, not the signal (a pass goes to a separate prereg)"
                if None not in (c["lift"], x0["lift"]) and c["lift"] >= x0["lift"] else "control below the signal")
        out.append(f"- {name}: CONTROL (same rule at a random later minute) held net {_bp(c['held_net'])}, "
                   f"lift {_bp(c['lift'])} (t {_t(c['lift_t'])}) vs signal lift {_bp(x0['lift'])} -> {flag}")
        sl = score(sg, w, k, low_src="sip")
        kept = "—" if sl["keep"] is None else f"{sl['keep']:.0%}"
        out.append(f"  SIP-low version held net {_bp(sl['held_net'])}, lift {_bp(sl['lift'])}, kept {kept}")
        lg = score(sg, w, k, lag=1)
        out.append(f"  entry one minute later (m+W+2, publication lag): held net {_bp(lg['held_net'])}, lift {_bp(lg['lift'])} "
                   f"(t {_t(lg['lift_t'])}) - sign kept: {None not in (lg['lift'], x0['lift']) and (lg['lift'] > 0) == (x0['lift'] > 0)}")
        fc = score(sg, w, k, full_only=True)
        fk = "—" if fc["keep"] is None else f"{fc['keep']:.0%}"
        out.append(f"  full IEX coverage of the wait: signals {fc['signals']}, kept {fk}, held net {_bp(fc['held_net'])}, lift {_bp(fc['lift'])}")
        out.append(f"  skipped by score: {x0['skips']}")
        hi = [s for s in sg if (s.get("ref") or 0) >= 30]
        if hi:
            x = score(hi, w, k)
            out.append(f"  price >= $30: held net {_bp(x['held_net'])}, lift {_bp(x['lift'])}, held {x['held']}")
    out += ["", "| W | k bp | part | kept | held net | lift vs wait-all | held - market |", "|---:|---:|---|---:|---:|---:|---:|"]
    for ww in GRID_W:
        for kk in GRID_K:
            for name, sg in parts.items():
                x = score(sg, ww, kk)
                keep = "—" if x["keep"] is None else f"{x['keep']:.0%}"
                out.append(f"| {ww} | {kk} | {name} | {keep} | {_bp(x['held_net'])} | {_bp(x['lift'])} | {_bp(x['vs_mkt'])} |")
    out.append(f"\n- skipped at signal extraction: {dict(SKIPS)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--in-sample", action="store_true")
    a = ap.parse_args()
    datas, ex = E.load_days()
    fwd = sorted(d["day"] for d in datas if d["day"] >= FORWARD_START)
    print(f"cached days {len(datas)}; forward sessions {len(fwd)} {fwd}; excluded {ex}")
    w, k = PRIMARY
    if a.in_sample:
        sigs = signals([d for d in datas if d["day"] <= IN_SAMPLE_END])
        parts = {h: [s for s in sigs if (s["day"] <= L.SPLIT_END_A) == (h == "A")] for h in "AB"}
        res = {h: score(sg, w, k) for h, sg in parts.items()}
        md = [f"# Wait {w} min, buy if it held (k={k} bp) - IN-SAMPLE (information; can only kill)", ""] + HEAD
        md += [_row(f"in-sample {h}", x) for h, x in res.items()]
        md += ["", "**KILL: the filter adds nothing on the days that suggested it; forward run cancelled**" if kill_check(res)
               else "**not killed: the forward read decides**"]
        md += info_lines({f"in-sample {h}": sg for h, sg in parts.items()})
        out = "\n".join(md)
        print(out)
        with open(os.path.join(E.OUT_DIR, "wait_held_in_sample.md"), "w") as f:
            f.write(out + "\n")
        return
    if not a.score:
        print("(inventory only; --score / --in-sample after the prereg has a result-skeptic READY)")
        return
    by_day = {d["day"]: d for d in datas}

    def sigs_of(days):
        return signals([by_day[d] for d in days])

    use = read_sessions(fwd, lambda ds: score(sigs_of(ds), w, k)["verdict"])
    if use is None:
        print(f"NOT DUE: {len(fwd)} forward sessions < {READ_AT}")
        return
    SKIPS.clear()
    sigs = sigs_of(use)
    x = score(sigs, w, k)
    import subprocess
    cfg_sha = subprocess.run(["git", "log", "-1", "--format=%h %cs", "--", "config/bot_config.json"],
                             capture_output=True, text=True, cwd=E.ROOT).stdout.strip()
    md = [f"# Wait {w} min, buy if it held (k={k} bp) - FORWARD read on {len(use)} sessions {use[0]}..{use[-1]}", "",
          f"live config at read: bot_config.json {cfg_sha} (RVOL option A floors from 10/14 evening: the board differs from in-sample)", ""]
    for d in use:
        dd = by_day[d]
        n = len(dd["board"]) or 1
        md.append(f"- {d}: board {len(dd['board'])}, iex miss {sum(1 for s_ in dd['board'] if s_ not in dd['iex']) / n:.1%}, "
                  f"sip miss {sum(1 for s_ in dd['board'] if s_ not in dd['sip']) / n:.1%}")
    md += [""] + HEAD
    md += [_row("forward", x), "", f"**Wait-and-hold forward: {x['verdict']}**"]
    if x["verdict"] == "UNDERPOWERED" and len(use) < EXTEND_TO:
        md.append(f"(one extension: re-read at {EXTEND_TO} forward sessions)")
    md += info_lines({"forward": sigs})
    out = "\n".join(md)
    print(out)
    with open(os.path.join(E.OUT_DIR, f"wait_held_forward_{len(use)}.md"), "w") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
