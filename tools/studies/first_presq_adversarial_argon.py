import sys, os, json, datetime, random, numpy as np
sys.path.insert(0, "tools/studies")
import indicator_confirm_hist as H
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from zoneinfo import ZoneInfo

days = H.warehouse_days()

def ema_arr(x, span):
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(x, dtype=np.float64)
    if len(x) == 0:
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out

def pctr_series(h, l, c, length, span):
    n = len(c)
    raw = np.full(n, np.nan)
    for i in range(length - 1, n):
        hh = np.max(h[i - length + 1 : i + 1])
        ll = np.min(l[i - length + 1 : i + 1])
        if hh > ll:
            raw[i] = (hh - c[i]) / (hh - ll) * -100.0
        else:
            raw[i] = -50.0
    valid = ~np.isnan(raw)
    if not np.any(valid):
        return raw
    first = int(np.argmax(valid))
    sm = np.full(n, np.nan)
    sm[first:] = ema_arr(raw[first:], span)
    return np.round(sm, 1)

def sim_exit_pessimistic(o, h, l, c, fast, entry_idx, entry_px):
    # Pessimistic live exit simulation:
    # - Hard stop -50 bp checked BEFORE bar high
    # - Trail arms at +15 bp MFE (checked at end of bar j for j+1 onward), trails by 8 bp
    # - Time exit at 15m close
    peak = entry_px
    armed = False
    trail_px = 0.0
    stop_px = entry_px * (1.0 - 0.0050)
    n = len(c)
    for j in range(entry_idx, min(n, entry_idx + 15)):
        if armed and o[j] <= trail_px:
            return (o[j] - entry_px) / entry_px * 10000.0
        if o[j] <= stop_px:
            return (o[j] - entry_px) / entry_px * 10000.0
        if armed and l[j] <= trail_px:
            return (trail_px - entry_px) / entry_px * 10000.0
        if l[j] <= stop_px:
            return -50.0
        if h[j] > peak:
            peak = h[j]
            if (peak - entry_px) / entry_px * 10000.0 >= 15.0:
                armed = True
                trail_px = max(entry_px * 1.0005, peak * (1.0 - 0.0008))
    end_i = min(n - 1, entry_idx + 14)
    return (c[end_i] - entry_px) / entry_px * 10000.0

def eval_day_idx(idx):
    day = days[idx]
    prev_day = days[idx - 1]
    sip_prev = H.load_raw("sip", prev_day)
    sip_cur = H.load_raw("sip", day)
    iex_prev = H.load_raw("iex", prev_day)
    iex_cur = H.load_raw("iex", day)

    # Build cross-sectional control map: minute -> list of (sym, r10, r15, day_chg) for liquid $20-$150 stocks
    ctrl_by_min = {}
    valid_syms = []
    for sym, cur_rows in sip_cur.items():
        if len(cur_rows) < 80:
            continue
        open_930 = cur_rows[0, 1]
        if not (20.0 <= open_930 <= 150.0):
            continue
        prev_rows = sip_prev.get(sym)
        if prev_rows is None or len(prev_rows) < 120:
            continue
        if np.mean(prev_rows[:, 4] * prev_rows[:, 5]) < 100000.0:
            continue
        valid_syms.append(sym)
        prev_close = prev_rows[-1, 4]
        mins = cur_rows[:, 0].astype(np.int32)
        o = cur_rows[:, 1]
        c = cur_rows[:, 4]
        n = len(c)
        for i in range(15, n - 31):
            m = int(mins[i])
            if 585 <= m <= 900 and 20.0 <= c[i] <= 150.0:
                dchg = (c[i] / prev_close - 1.0) * 100.0
                if 0.5 <= dchg <= 5.0:
                    ep = o[i + 1]
                    r10 = (c[i + 10] - ep) / ep * 10000.0
                    r15 = (c[i + 15] - ep) / ep * 10000.0
                    ctrl_by_min.setdefault(m, []).append((sym, r10, r15))

    rng = random.Random(f"adv-{day}")
    results = []

    for sym in valid_syms:
        cur_rows = sip_cur[sym]
        prev_rows = sip_prev[sym]
        prev_tail = prev_rows[-120:]
        comb = np.vstack([prev_tail, cur_rows])
        start_i = len(prev_tail)
        prev_close = prev_rows[-1, 4]

        mins = comb[:, 0]
        o = comb[:, 1]
        h = comb[:, 2]
        l = comb[:, 3]
        c = comb[:, 4]
        v = comb[:, 5]
        n = len(c)

        fast_sip = pctr_series(h, l, c, 21, 7)
        slow_sip = pctr_series(h, l, c, 112, 3)

        # Also compute IEX-based indicators aligned to minute `m`
        iex_map = {}
        ip = iex_prev.get(sym)
        ic = iex_cur.get(sym)
        if ip is not None and ic is not None and len(ip) >= 60 and len(ic) >= 30:
            icomb = np.vstack([ip[-120:], ic])
            ifast = pctr_series(icomb[:, 2], icomb[:, 3], icomb[:, 4], 21, 7)
            islow = pctr_series(icomb[:, 2], icomb[:, 3], icomb[:, 4], 112, 3)
            istart = len(ip[-120:])
            for ii in range(istart + 5, len(icomb)):
                im = int(icomb[ii, 0])
                iprior = np.any((ifast[istart:ii] >= -35.0) & (islow[istart:ii] >= -35.0) & (np.abs(ifast[istart:ii] - islow[istart:ii]) <= 15.0))
                iavg_v = np.mean(icomb[max(istart, ii-15):ii, 5])
                ivr = icomb[ii, 5] / iavg_v if iavg_v > 0 else 0.0
                iex_map[im] = (ifast[ii], islow[ii], ifast[ii] - ifast[ii-1], ifast[ii] - ifast[ii-3], islow[ii] - islow[ii-5], iprior, ivr)

        for i in range(start_i + 15, n - 31):
            m_cur = int(mins[i])
            if m_cur < 585 or m_cur > 900:
                continue
            if not (20.0 <= c[i] <= 150.0):
                continue
            dvol15 = np.mean(c[i-15:i+1] * v[i-15:i+1])
            if dvol15 < 150000.0:
                continue

            in_pre = (
                fast_sip[i] >= -35.0 and slow_sip[i] >= -35.0
                and not (fast_sip[i] >= -20.0 and slow_sip[i] >= -20.0)
                and abs(fast_sip[i] - slow_sip[i]) <= 15.0
                and (fast_sip[i] - fast_sip[i-1]) > 0.05
                and (fast_sip[i] - fast_sip[i-3]) >= 15.0
                and (slow_sip[i] - slow_sip[i-5]) >= 2.0
            )
            if not in_pre:
                continue

            day_chg_pct = (c[i] / prev_close - 1.0) * 100.0
            avg_v15 = np.mean(v[i-15:i])
            vol_ratio = v[i] / avg_v15 if avg_v15 > 0 else 0.0
            prior_in_today = bool(np.any((fast_sip[start_i:i] >= -35.0) & (slow_sip[start_i:i] >= -35.0) & (np.abs(fast_sip[start_i:i] - slow_sip[start_i:i]) <= 15.0)))
            prior_in_45m = bool(np.any((fast_sip[max(start_i, i-45):i] >= -35.0) & (slow_sip[max(start_i, i-45):i] >= -35.0) & (np.abs(fast_sip[max(start_i, i-45):i] - slow_sip[max(start_i, i-45):i]) <= 15.0)))

            ep = o[i + 1]
            # Adverse fill price: max(o[i+1], c[i]) (never benefits from a negative gap between bar i close and bar i+1 open)
            ep_adv = max(o[i + 1], c[i])
            r5 = (c[i + 5] - ep) / ep * 10000.0
            r10 = (c[i + 10] - ep) / ep * 10000.0
            r15 = (c[i + 15] - ep) / ep * 10000.0
            r15_adv = (c[i + 15] - ep_adv) / ep_adv * 10000.0
            r_exit = sim_exit_pessimistic(o, h, l, c, fast_sip, i + 1, ep)

            # Same-minute cross-name control (another stock at exact same minute also up +0.5%..+5.0%)
            pool_x = [x for x in ctrl_by_min.get(m_cur, []) if x[0] != sym]
            ctrl_x_15 = rng.choice(pool_x)[2] if pool_x else np.nan

            # Same-name later-minute control (random minute i+15 .. min(n-16, i+75))
            later_candidates = list(range(i + 15, min(n - 16, i + 75)))
            if later_candidates:
                li = rng.choice(later_candidates)
                ctrl_d_15 = (c[li + 15] - o[li + 1]) / o[li + 1] * 10000.0
            else:
                ctrl_d_15 = np.nan

            # Check if IEX bars also trigger First PreSq at this minute
            iex_ok = False
            if m_cur in iex_map:
                if_v, is_v, if_d1, if_d3, is_d5, ipr, ivr = iex_map[m_cur]
                if (if_v >= -35.0 and is_v >= -35.0 and not (if_v >= -20.0 and is_v >= -20.0)
                    and abs(if_v - is_v) <= 15.0 and if_d1 > 0.05 and if_d3 >= 15.0 and is_d5 >= 2.0
                    and not ipr and ivr >= 1.5):
                    iex_ok = True

            results.append({
                "day": day, "sym": sym, "m": m_cur,
                "day_chg": day_chg_pct, "vol_ratio": vol_ratio,
                "prior_today": prior_in_today, "prior_45m": prior_in_45m,
                "r5": r5, "r10": r10, "r15": r15, "r15_adv": r15_adv, "r_exit": r_exit,
                "ctrl_x_15": ctrl_x_15, "ctrl_d_15": ctrl_d_15,
                "iex_ok": iex_ok,
            })
    return results

with ProcessPoolExecutor(max_workers=6, mp_context=multiprocessing.get_context("fork")) as ex:
    all_rows = [r for sub in ex.map(eval_day_idx, range(1, len(days))) for r in sub]

def report_subset(label, pred):
    sub = [r for r in all_rows if pred(r)]
    # Apply 30m per-symbol cooldown
    kept = []
    last_m = {}
    for r in sub:
        key = (r["day"], r["sym"])
        if r["m"] - last_m.get(key, -999) >= 30:
            last_m[key] = r["m"]
            kept.append(r)
    if not kept:
        return
    w10 = np.clip([r["r10"] for r in kept], -150, 150)
    w15 = np.clip([r["r15"] for r in kept], -150, 150)
    w15_adv = np.clip([r["r15_adv"] for r in kept], -150, 150)
    w_ex = np.clip([r["r_exit"] for r in kept], -150, 150)
    cx = [np.clip(r["r15"], -150, 150) - np.clip(r["ctrl_x_15"], -150, 150) for r in kept if not np.isnan(r["ctrl_x_15"])]
    cd = [np.clip(r["r15"], -150, 150) - np.clip(r["ctrl_d_15"], -150, 150) for r in kept if not np.isnan(r["ctrl_d_15"])]
    by_d = {}
    for r in kept:
        by_d.setdefault(r["day"], []).append(np.clip(r["r15"], -150, 150))
    d_means = np.array([np.mean(v) for v in by_d.values()])
    t_day = np.mean(d_means) / (np.std(d_means, ddof=1) / np.sqrt(len(d_means))) if len(d_means) > 1 else 0.0
    print(f"{label:52s} | N={len(kept):4d} | Win15m={np.mean(w15):+6.2f} (Med {np.median(w15):+5.2f}) | AdvFill={np.mean(w15_adv):+6.2f} | Lift_X={np.mean(cx):+6.2f} | Lift_D={np.mean(cd):+6.2f} | DayEq={np.mean(d_means):+6.2f} (t={t_day:+5.2f})")

print("=== ADVERSARIAL AUDIT ACROSS 119 WAREHOUSE SESSIONS ===")
# Threat 1 & 2: Compare 1st Square vs 2nd+ Square INSIDE THE EXACT SAME MORNING WINDOW (09:45-11:00 ET, m <= 660)!
report_subset("1. Morning (09:45-11:00) 1st Sq, chg 0.5..5%, vol>=2x", lambda r: r["m"] <= 660 and not r["prior_today"] and 0.5 <= r["day_chg"] <= 5.0 and r["vol_ratio"] >= 2.0)
report_subset("2. Morning (09:45-11:00) 1st Sq, chg 0.5..5%, vol>=1.5x", lambda r: r["m"] <= 660 and not r["prior_today"] and 0.5 <= r["day_chg"] <= 5.0 and r["vol_ratio"] >= 1.5)
report_subset("3. Morning (09:45-11:00) 2nd+ Sq, chg 0.5..5%, vol>=1.5x", lambda r: r["m"] <= 660 and r["prior_today"] and 0.5 <= r["day_chg"] <= 5.0 and r["vol_ratio"] >= 1.5)
report_subset("4. All-Day 45m-Reset Sq, chg 0.5..5%, vol>=2.0x", lambda r: not r["prior_45m"] and 0.5 <= r["day_chg"] <= 5.0 and r["vol_ratio"] >= 2.0)
report_subset("5. IEX-Signal + SIP-Fill 1st Sq, chg 0.5..5%", lambda r: r["iex_ok"] and 0.5 <= r["day_chg"] <= 5.0)
report_subset("6. Morning Extended (chg >= 8%) any Sq, vol>=1.5x", lambda r: r["m"] <= 660 and r["day_chg"] >= 8.0 and r["vol_ratio"] >= 1.5)