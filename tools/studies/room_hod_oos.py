#!/usr/bin/env python3
"""room_hod_oos.py — pre-registered in docs/studies/room_hod_oos_prereg.json (e19f14f). Nothing here may change it.

ROOM: room_hod = (1 - close[i] / HOD_i) * 100 >= 2.744, HOD_i = max high of the day's RTH bars through bar i.
Same universe, grid, band, cost and averaging as needle_cells_oos.py (shared helpers imported from there); the hold is
60 min (exit = last bar starting <= min(entry + 60 min, 15:55); a target inside a gap needs a bar within 75 min).
Name-days with no prior close are kept (ROOM does not need it).

USAGE (on the mini, after needle_cells_oos.py fetch has cached the premarket/SPY files; read-only otherwise)
  .venv/bin/python tools/studies/room_hod_oos.py      # report -> ai_reports/room_hod_oos/report.md
"""
from __future__ import annotations

import bisect
import json
import os
import pickle
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import needle_cells_oos as N  # noqa: E402

OUT = os.path.join(N.ROOT, "ai_reports", "room_hod_oos")
CUT = 2.744
HOLD, HOLE = 60 * 60, 75 * 60


def room_of(rows, i):
    hod = max(r[2] for r in rows[:i + 1])
    return (1 - rows[i][3] / hod) * 100 if hod > 0 else None


def outcome(rows, i, day, hold=HOLD, hole=HOLE):
    """needle_cells_oos.outcome with the hold as a parameter (60 min here, 30 for the information line)."""
    if i + 1 >= len(rows) or rows[i + 1][0] - rows[i][0] > 5 * 60:
        return None
    te, pe = rows[i + 1][0], rows[i + 1][1]
    if pe <= 0:
        return None
    cap = N.day_ts(day, N.CAP_MIN)
    tgt = min(te + hold, cap)
    k = i + 1
    while k + 1 < len(rows) and rows[k + 1][0] <= tgt:
        k += 1
    if rows[k][0] < tgt and tgt < cap:
        if not (k + 1 < len(rows) and rows[k + 1][0] <= te + hole):
            return None
    return te, rows[k][0], (rows[k][3] / pe - 1) * 1e4


def vol5(rows, i, n=30):
    """stdev of 1-min close returns over the last n bars, scaled to 5 min (bp); information only."""
    if i < n:
        return None
    rs = [(rows[j][3] / rows[j - 1][3] - 1) * 1e4 for j in range(i - n + 1, i + 1) if rows[j - 1][3] > 0]
    return statistics.stdev(rs) * 5 ** 0.5 if len(rs) > 2 else None


def build(hold=HOLD, hole=HOLE):
    daily = pickle.load(open(N.DAILY, "rb"))
    pc = N.prior_closes(daily)
    days = N.cached_days()
    moments, drops, spy_by_day = [], defaultdict(int), {}
    for day in days:
        mb = pickle.load(open(os.path.join(N.ALLSYM, f"sipbrk_min_{day}.pkl"), "rb"))
        pm = json.load(open(N.pm_path(day))) if os.path.exists(N.pm_path(day)) else {"status": "missing"}
        pmd = pm["pm"] if pm.get("status") == "ok" else {}
        if pm.get("status") == "ok":
            spy_by_day[day] = {round(t): (o, c) for t, o, c in pm["spy"]}
        for sym, rows in mb.items():
            ds, cs = pc.get(sym, ([], []))
            j = bisect.bisect_left(ds, day) - 1
            prev = cs[j] if j >= 0 else None
            if prev is None:
                drops["no_prior_close (kept)"] += 1
            pmb = pmd.get(sym)
            pmr = N.pm_range_of(pmb) if pmb else None
            for i, r in enumerate(rows):
                m = N.et_min(r[0])
                if not (N.GRID_LO <= m <= N.GRID_HI) or (m - N.GRID_LO) % 5:
                    continue
                room = room_of(rows, i)
                if room is None:
                    continue
                px = r[3]
                dc = (px / prev - 1) * 100 if prev else None
                o = outcome(rows, i, day, hold, hole)
                is_room = room >= CUT
                if o is None:
                    drops["no_exit_bar " + ("room" if is_room else "control")] += 1
                    continue
                drops["scored " + ("room" if is_room else "control")] += 1
                te, tx, g = o
                v5 = vol5(rows, i)
                moments.append({
                    "day": day, "nd": (sym, day), "sym": sym, "hour": m // 60, "m": m, "gross": g, "te": te, "tx": tx,
                    "band": N.BAND[0] <= px <= N.BAND[1], "room_v": room,
                    "room": is_room, "room15": room >= 1.5, "room4": room >= 4.0,
                    "room_green": is_room and dc is not None and dc > 0,
                    "room_vs": (room * 100 / v5) if v5 else None,
                    "c2": dc is not None and N.in_c2(dc, pmr)})
    return days, moments, dict(drops), spy_by_day


def tline(xs):
    m, t = N.tstat(xs)
    return m, t


def verdict(moments, halves, cost=N.COST_BP, cell="room"):
    lines, ok, rel_ok, abs_ok = [], True, True, True
    for h in ("A", "B"):
        r = N.score_cell(moments, cell, cost, days_subset=halves[h])
        mn, tn = tline(list(r["day_net"].values()))
        md, td = tline(list(r["day_diff"].values()))
        enough = r["n_nd"] >= 30 and r["n_days"] >= 20
        rel_ok &= enough and md >= 5
        abs_ok &= mn > 0
        lines.append(f"half {h}: name-days {r['n_nd']}, days {r['n_days']}, moments {r['n_mom']} | net {mn:+.1f} bp "
                     f"(t {tn:+.2f}) | minus control {md:+.1f} bp (t {td:+.2f})" + ("" if enough else " | UNDERPOWERED"))
    r = N.score_cell(moments, cell, cost)
    md, td = tline(list(r["day_diff"].values()))
    rel_ok &= td >= 2.0
    top_days = sorted(r["day_diff"], key=r["day_diff"].get, reverse=True)[:3]
    rd = N.score_cell(moments, cell, cost, days_subset=set(r["day_diff"]) - set(top_days))
    md_d, td_d = tline(list(rd["day_diff"].values()))
    by_sym = defaultdict(list)
    for nd, v in r["nd_mean_diff"].items():
        by_sym[nd[0]].append(v)
    top_syms = sorted(by_sym, key=lambda s: statistics.fmean(by_sym[s]), reverse=True)[:5]
    rs = N.score_cell(moments, cell, cost, drop_nd={nd for nd in r["nd_mean_diff"] if nd[0] in top_syms})
    md_s, td_s = tline(list(rs["day_diff"].values()))
    rel_ok &= md_d >= 5 and td_d >= 2.0 and md_s >= 5 and td_s >= 2.0
    lines.append(f"pooled minus control {md:+.1f} bp (t {td:+.2f}, {len(r['day_diff'])} days)")
    lines.append(f"without top 3 days {', '.join(top_days)}: {md_d:+.1f} bp (t {td_d:+.2f})")
    lines.append(f"without top 5 symbols {', '.join(top_syms)}: {md_s:+.1f} bp (t {td_s:+.2f})")
    ok = rel_ok and abs_ok
    return ok, rel_ok, lines


def within_name(moments, cost=N.COST_BP):
    """ROOM vs non-ROOM moments of the SAME name-day and hour -> per name-day mean -> per day mean."""
    g = defaultdict(lambda: {True: [], False: []})
    for x in moments:
        g[(x["nd"], x["hour"])][x["room"]].append(x["gross"] - cost)
    nd = defaultdict(list)
    for (k, _), v in g.items():
        if v[True] and v[False]:
            nd[k].append(statistics.fmean(v[True]) - statistics.fmean(v[False]))
    by_day = defaultdict(list)
    for k, v in nd.items():
        by_day[k[1]].append(statistics.fmean(v))
    return N.tstat([statistics.fmean(v) for v in by_day.values()]), len(nd)


def main():
    os.makedirs(OUT, exist_ok=True)
    days, moments, drops, spy_by_day = build()
    halves = {"A": set(days[0::2]), "B": set(days[1::2])}
    banded = [x for x in moments if x["band"]]
    sr, sc = drops.get("scored room", 0), drops.get("scored control", 0)
    dr, dc_ = drops.get("no_exit_bar room", 0), drops.get("no_exit_bar control", 0)
    rate_r = dr / max(1, dr + sr) * 100
    rate_c = dc_ / max(1, dc_ + sc) * 100
    flag = rate_r > rate_c + 2
    ok, rel_ok, lines = verdict(banded, halves)
    v = "PASS" if ok else ("RELATIVE-ONLY (FAIL as a filter; may earn a forward ranking prereg)" if rel_ok else "FAIL")
    cover = sum(x["room"] for x in banded) / max(1, len(banded))
    L = ["# Room below HOD out of period (prereg docs/studies/room_hod_oos_prereg.json)", "",
         f"days {len(days)} ({days[0]}..{days[-1]}); in-band moments {len(banded)}, ROOM share {cover:.0%}; drops {drops}",
         f"no-exit drop rate ROOM {rate_r:.1f}% vs control {rate_c:.1f}%" + (" — **FLAG: ROOM > control + 2 pp**" if flag else ""),
         "", "## ROOM (verdict), $20-100, 60-min hold, 10 bp", *[f"- {s}" for s in lines], "",
         f"**PRIMARY: {v}**", "", "## Information only (no verdict)"]
    for lab, mm, cell, cost in (("cut 1.5%", banded, "room15", N.COST_BP), ("cut 4%", banded, "room4", N.COST_BP),
                                ("green names only", banded, "room_green", N.COST_BP), ("4 bp", banded, "room", N.COST_INFO_BP),
                                ("$10+ (no band)", moments, "room", N.COST_BP)):
        _, _, ls = verdict(mm, halves, cost, cell)
        L.append(f"- **{lab}**")
        L += [f"    - {s}" for s in ls]
    (wm, wt), wn = within_name(banded)
    L.append(f"- within name-day and hour (ROOM minus non-ROOM, {wn} name-days): {wm:+.1f} bp (t {wt:+.2f})")
    vs = sorted(x["room_vs"] for x in banded if x["room"] and x["room_vs"] is not None)
    if vs:
        cuts = (vs[len(vs) // 3], vs[2 * len(vs) // 3])
        for lab, f in (("low", lambda z: z < cuts[0]), ("mid", lambda z: cuts[0] <= z < cuts[1]), ("high", lambda z: z >= cuts[1])):
            mm = [dict(x, sub=x["room"] and x["room_vs"] is not None and f(x["room_vs"])) for x in banded]
            r = N.score_cell(mm, "sub", N.COST_BP)
            m_, t_ = N.tstat(list(r["day_diff"].values()))
            L.append(f"- ROOM tercile of room / own 5-min vol ({lab}): minus control {m_:+.1f} bp (t {t_:+.2f}, name-days {r['n_nd']})")
    early = sum(1 for x in banded if x["room"] and x["m"] < 10 * 60 + 30) / max(1, sum(x["room"] for x in banded))
    L.append(f"- share of ROOM moments before 10:30: {early:.0%}")
    hr = defaultdict(lambda: [0, 0])
    for x in banded:
        hr[x["hour"]][0] += x["room"]
        hr[x["hour"]][1] += 1
    L.append("- ROOM share by hour: " + ", ".join(f"{h}h {a / b:.0%}" for h, (a, b) in sorted(hr.items())))
    for cell in ("room",):
        b = N.spy_beta(banded, spy_by_day, cell)
        bc = N.spy_beta([dict(x, ctl=not x["room"]) for x in banded], spy_by_day, "ctl")
        L.append("- SPY beta ROOM " + ("n/a" if b is None else f"{b[0]:.2f} (n {b[1]})") + ", control "
                 + ("n/a" if bc is None else f"{bc[0]:.2f} (n {bc[1]})"))
    both = sum(1 for x in banded if x["room"] and x["c2"])
    L.append(f"- overlap: ROOM moments that are also needle C2: {both} of {sum(x['c2'] for x in banded)} C2 moments")
    _, mo30, _, _ = build(hold=30 * 60, hole=45 * 60)
    _, _, ls = verdict([x for x in mo30 if x["band"]], halves)
    L.append("- **30-min hold**")
    L += [f"    - {s}" for s in ls]
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
