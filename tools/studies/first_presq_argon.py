#!/usr/bin/env python3
"""Argon's exact FIRST_PRESQ evaluation script (recovered from conversation c68ecad7 task-586).

Scored across 119 warehouse sessions (2026-04-27 to 2026-10-15) on $20-$150 liquid stocks.
Evaluates:
  - 1_FIRST_PRESQ_morning_vol2x (09:45-11:00, chg 1..4.5%, vol >= 2x)
  - 2_FIRST_PRESQ_allday_vol2x  (09:45-15:00, chg 1..5%, vol >= 2x)
  - 3_FIRST_SQ_OR_PRESQ_morning (09:45-11:30, allow fast>=-20, vol3m>=2x)
  - 4_FIRST_PRESQ_morning_vol1.5x (09:45-11:30, chg 0.5..5%, vol >= 1.5x)
  - 5_ORB15_buystop_pb_s5m_up2  (ORB15 1-bar PB buy-stop + s5m>=+2)
  - 6_FIRST_PRESQ_plus_HOD_brk  (First Sq/PreSq + breaks 15m high on vol>=1.8x)
"""
import sys, os, numpy as np
sys.path.insert(0, "tools/studies")
import indicator_confirm_hist as H
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

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

KEYS = [
    "1_FIRST_PRESQ_morning_vol2x (N=89 exact)",
    "2_FIRST_PRESQ_allday_vol2x  (09:45-15:00, chg 1..5%, vol>=2x)",
    "3_FIRST_SQ_OR_PRESQ_morning (09:45-11:30, allow fast>=-20, vol3m>=2x)",
    "4_FIRST_PRESQ_morning_vol1.5x (09:45-11:30, chg 0.5..5%, vol>=1.5x)",
    "5_ORB15_buystop_pb_s5m_up2  (ORB15 1-bar PB buy-stop + s5m>=+2)",
    "6_FIRST_PRESQ_plus_HOD_brk  (First Sq/PreSq + breaks 15m high on vol>=1.8x)",
]

def eval_day_idx(idx):
    day = days[idx]
    prev_day = days[idx - 1]
    raw_prev = H.load_raw("sip", prev_day)
    raw_cur = H.load_raw("sip", day)
    out = {k: [] for k in KEYS}

    for sym, cur_rows in raw_cur.items():
        if len(cur_rows) < 80:
            continue
        open_930 = cur_rows[0, 1]
        if not (20.0 <= open_930 <= 150.0):
            continue
        prev_rows = raw_prev.get(sym)
        if prev_rows is None or len(prev_rows) < 120:
            continue
        prev_dvol_per_min = np.mean(prev_rows[:, 4] * prev_rows[:, 5])
        if prev_dvol_per_min < 100000.0:
            continue

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

        fast = pctr_series(h, l, c, 21, 7)
        slow = pctr_series(h, l, c, 112, 3)

        orb_mask = (mins[start_i:] >= 570) & (mins[start_i:] < 585)
        if not np.any(orb_mask):
            continue
        orb_h = np.max(h[start_i:][orb_mask])
        orb_l = np.min(l[start_i:][orb_mask])
        orb_rng_pct = (orb_h - orb_l) / orb_l * 100.0 if orb_l > 0 else 99.0

        fired = {k: False for k in KEYS}

        for i in range(start_i + 15, n - 31):
            m_cur = int(mins[i])
            if m_cur < 585 or m_cur > 925:
                continue
            if not (20.0 <= c[i] <= 150.0):
                continue
            dvol15 = np.mean(c[i-15:i+1] * v[i-15:i+1])
            if dvol15 < 150000.0:
                continue

            day_chg_pct = (c[i] / prev_close - 1.0) * 100.0
            avg_v15 = np.mean(v[i-15:i])
            ep = o[i + 1]
            r5 = (c[i + 5] - ep) / ep * 10000.0
            r10 = (c[i + 10] - ep) / ep * 10000.0
            r15 = (c[i + 15] - ep) / ep * 10000.0
            r30 = (c[i + 30] - ep) / ep * 10000.0

            prior_in_today = np.any((fast[start_i:i] >= -35.0) & (slow[start_i:i] >= -35.0) & (np.abs(fast[start_i:i] - slow[start_i:i]) <= 15.0))
            in_pre_now = (fast[i] >= -35.0 and slow[i] >= -35.0 and not (fast[i] >= -20.0 and slow[i] >= -20.0) and abs(fast[i] - slow[i]) <= 15.0)
            in_sq_or_pre_now = (fast[i] >= -35.0 and slow[i] >= -35.0 and abs(fast[i] - slow[i]) <= 15.0)

            # 1. Exact N=89 setup
            if (not fired["1_FIRST_PRESQ_morning_vol2x (N=89 exact)"]) and 585 <= m_cur <= 660:
                if (not prior_in_today and in_pre_now and 1.0 <= day_chg_pct <= 4.5
                    and (fast[i] - fast[i-3]) >= 15.0 and (slow[i] - slow[i-5]) >= 2.0
                    and avg_v15 > 0 and v[i] >= 2.0 * avg_v15):
                    fired["1_FIRST_PRESQ_morning_vol2x (N=89 exact)"] = True
                    out["1_FIRST_PRESQ_morning_vol2x (N=89 exact)"].append((day, sym, r5, r10, r15, r30))

            # 2. All-day (first of the day, 09:45-15:00)
            if (not fired["2_FIRST_PRESQ_allday_vol2x  (09:45-15:00, chg 1..5%, vol>=2x)"]) and 585 <= m_cur <= 900:
                if (not prior_in_today and in_pre_now and 1.0 <= day_chg_pct <= 5.0
                    and (fast[i] - fast[i-3]) >= 15.0 and (slow[i] - slow[i-5]) >= 2.0
                    and avg_v15 > 0 and v[i] >= 2.0 * avg_v15):
                    fired["2_FIRST_PRESQ_allday_vol2x  (09:45-15:00, chg 1..5%, vol>=2x)"] = True
                    out["2_FIRST_PRESQ_allday_vol2x  (09:45-15:00, chg 1..5%, vol>=2x)"].append((day, sym, r5, r10, r15, r30))

            # 3. First Sq OR PreSq in morning (allow fast >= -20 on the surge bar, max vol in last 2 bars >= 2x)
            if (not fired["3_FIRST_SQ_OR_PRESQ_morning (09:45-11:30, allow fast>=-20, vol3m>=2x)"]) and 585 <= m_cur <= 690:
                if (not prior_in_today and in_sq_or_pre_now and 1.0 <= day_chg_pct <= 5.0
                    and (fast[i] - fast[i-3]) >= 15.0 and (slow[i] - slow[i-5]) >= 2.0
                    and avg_v15 > 0 and max(v[i], v[i-1]) >= 2.0 * avg_v15):
                    fired["3_FIRST_SQ_OR_PRESQ_morning (09:45-11:30, allow fast>=-20, vol3m>=2x)"] = True
                    out["3_FIRST_SQ_OR_PRESQ_morning (09:45-11:30, allow fast>=-20, vol3m>=2x)"].append((day, sym, r5, r10, r15, r30))

            # 4. First PreSq morning with vol >= 1.5x
            if (not fired["4_FIRST_PRESQ_morning_vol1.5x (09:45-11:30, chg 0.5..5%, vol>=1.5x)"]) and 585 <= m_cur <= 690:
                if (not prior_in_today and in_pre_now and 0.5 <= day_chg_pct <= 5.0
                    and (fast[i] - fast[i-3]) >= 15.0 and (slow[i] - slow[i-5]) >= 2.0
                    and avg_v15 > 0 and v[i] >= 1.5 * avg_v15):
                    fired["4_FIRST_PRESQ_morning_vol1.5x (09:45-11:30, chg 0.5..5%, vol>=1.5x)"] = True
                    out["4_FIRST_PRESQ_morning_vol1.5x (09:45-11:30, chg 0.5..5%, vol>=1.5x)"].append((day, sym, r5, r10, r15, r30))

            # 5. ORB15 buy-stop pullback + s5m>=+2
            if (not fired["5_ORB15_buystop_pb_s5m_up2  (ORB15 1-bar PB buy-stop + s5m>=+2)"]) and 585 <= m_cur <= 620 and orb_rng_pct <= 2.0:
                if (c[i-1] <= orb_h and c[i] > orb_h and v[i] >= 2.0 * avg_v15 and (slow[i] - slow[i-5]) >= 2.0):
                    # Check next bar inside pullback
                    if i + 1 < n - 31 and h[i+1] <= h[i] and l[i+1] >= l[i] and v[i+1] < v[i]:
                        stop_lvl = h[i+1] + 0.01
                        for k in range(i + 2, min(n - 31, i + 10)):
                            if h[k] >= stop_lvl:
                                ep_bs = max(o[k], stop_lvl)
                                r5_bs = (c[k+5] - ep_bs) / ep_bs * 10000.0
                                r10_bs = (c[k+10] - ep_bs) / ep_bs * 10000.0
                                r15_bs = (c[k+15] - ep_bs) / ep_bs * 10000.0
                                r30_bs = (c[k+30] - ep_bs) / ep_bs * 10000.0
                                fired["5_ORB15_buystop_pb_s5m_up2  (ORB15 1-bar PB buy-stop + s5m>=+2)"] = True
                                out["5_ORB15_buystop_pb_s5m_up2  (ORB15 1-bar PB buy-stop + s5m>=+2)"].append((day, sym, r5_bs, r10_bs, r15_bs, r30_bs))
                                break

            # 6. FIRST_PRESQ + HOD Breakout
            if (not fired["6_FIRST_PRESQ_plus_HOD_brk  (First Sq/PreSq + breaks 15m high on vol>=1.8x)"]) and 585 <= m_cur <= 720:
                high_15m = np.max(h[i-15:i])
                if (not prior_in_today and in_sq_or_pre_now and c[i] > high_15m and 0.5 <= day_chg_pct <= 5.0
                    and (fast[i] - fast[i-3]) >= 15.0 and (slow[i] - slow[i-5]) >= 2.0
                    and avg_v15 > 0 and v[i] >= 1.8 * avg_v15):
                    fired["6_FIRST_PRESQ_plus_HOD_brk  (First Sq/PreSq + breaks 15m high on vol>=1.8x)"] = True
                    out["6_FIRST_PRESQ_plus_HOD_brk  (First Sq/PreSq + breaks 15m high on vol>=1.8x)"].append((day, sym, r5, r10, r15, r30))

    return out

def main():
    pool = ProcessPoolExecutor(max_workers=min(multiprocessing.cpu_count(), 10))
    indices = range(1, len(days))
    results = pool.map(eval_day_idx, indices)

    combined = {k: [] for k in KEYS}
    for res in results:
        for k in KEYS:
            combined[k].extend(res[k])

    print("="*110)
    print(f"{'Setup':<48} | {'N':<4} | {'Win10m':<8} | {'Win15m':<8} | {'Med15m':<8} | {'H1(15m)':<8} | {'H2(15m)':<8} | {'Drop3D(15m)':<11} | {'t(15m)':<6}")
    print("="*110)

    for k in KEYS:
        rows = combined[k]
        if not rows:
            continue
        days_seen = [r[0] for r in rows]
        r10s = np.array([r[3] for r in rows])
        r15s = np.array([r[4] for r in rows])

        w10 = np.clip(r10s, -150.0, 150.0)
        w15 = np.clip(r15s, -200.0, 200.0)

        n = len(rows)
        mean_10 = np.mean(w10)
        mean_15 = np.mean(w15)
        med_15 = np.median(r15s)

        # Halves split
        h1 = [w15[idx] for idx, d in enumerate(days_seen) if d <= "2026-07-29"]
        h2 = [w15[idx] for idx, d in enumerate(days_seen) if d > "2026-07-29"]
        m_h1 = np.mean(h1) if h1 else 0.0
        m_h2 = np.mean(h2) if h2 else 0.0

        # Day-clustered t-stat
        day_dict = {}
        for d, val in zip(days_seen, w15):
            day_dict.setdefault(d, []).append(val)
        day_means = np.array([np.mean(vals) for vals in day_dict.values()])
        t_stat = np.mean(day_means) / (np.std(day_means, ddof=1) / np.sqrt(len(day_means))) if len(day_means) > 1 and np.std(day_means, ddof=1) > 0 else 0.0

        # Drop top 3 days
        sorted_days = sorted(day_dict.items(), key=lambda x: np.mean(x[1]), reverse=True)
        top_3_days = {x[0] for x in sorted_days[:3]}
        drop3_vals = [val for d, val in zip(days_seen, w15) if d not in top_3_days]
        m_drop3 = np.mean(drop3_vals) if drop3_vals else 0.0

        print(f"{k[:48]:<48} | {n:<4} | {mean_10:+6.2f}bp | {mean_15:+6.2f}bp | {med_15:+6.2f}bp | {m_h1:+6.2f}bp | {m_h2:+6.2f}bp | {m_drop3:+9.2f}bp | {t_stat:+5.2f}")
    print("="*110)

if __name__ == "__main__":
    main()
