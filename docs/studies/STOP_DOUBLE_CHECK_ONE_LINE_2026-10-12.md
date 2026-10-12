# Stop plus one %R line (2026-10-12): FAIL

Pre-registration: [`stop_double_check_one_line_prereg.json`](stop_double_check_one_line_prereg.json). Script: `tools/studies/stop_double_check.py --lines 1`.

The both-lines gate is a logged FAIL and is not reopened. This arm sells on a shelf hit when either %R line is off its 10-minute high. It declines the hit only when neither line is down. A missing lookback still sells. 15:50 still flattens. Same 952 desk round trips, 2026-08-11 through 2026-09-23, minute bars from `ai_reports/runway_bars_cache.pkl`. Gross bp. The spread cancels in the difference.

| arm | trades | gross bp/trade |
|---|---|---|
| stop (first shelf hit) | 952 | -4.92 |
| stop and one line | 952 | -5.07 |

Paired daily mean (one-line minus stop): **+0.08 bp/trade**, t **+0.27**, 30 scored days, positive on **27%** of them. The shelf bound on 98% of the stop arm. The gate declined a hit on 57 trades and changed the exit on 48. The $10+ slice (not the verdict) was +0.04 bp on 696 trades.

**Verdict (pre-registered bar: paired mean >= +3 bp, t >= 2, positive on >= 60% of days): FAIL.** The live exit stays the ratchet shelf.

```
stop vs stop+one-line  trades 952  days 34  scored 30
  stop arm exits on the shelf 98%  gate declined a hit on 57 trades  exits differ on 48
  gross bp/trade  stop -4.92  stop+one-line -5.07  paired daily mean +0.08  t +0.27  positive days 27%
  daily paired bp  08-11:+0.0 08-12:-0.4 08-13:+0.0 08-14:-0.4 08-17:+0.5 08-18:+0.0
                   08-19:-0.4 08-20:+0.8 08-21:-3.9 08-24:+0.0 08-25:+0.0 08-26:-3.0
                   08-27:+1.2 08-28:+0.0 08-31:+4.7 09-01:+0.0 09-02:+0.0 09-03:+0.0
                   09-08:+0.0 09-09:+0.0 09-10:-1.0 09-11:+0.0 09-14:+0.9 09-15:+4.2
                   09-16:-0.0 09-17:-1.1 09-18:+1.2 09-21:+0.6 09-22:-0.2 09-23:-1.1
```

Minute shelf, not the live 2-second chase: the 8-second decay steps once a minute. The bar cache is regular hours only, so a slow %R that is not warm yet defaults to sell yes. Sessions after this cache was built are not in the read.
