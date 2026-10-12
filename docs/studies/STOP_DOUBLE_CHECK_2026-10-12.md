# Stop plus %R double-check (2026-10-12): FAIL

Pre-registration: [`stop_double_check_prereg.json`](stop_double_check_prereg.json). Script: `tools/studies/stop_double_check.py`.

The stop arm sells at the first ratchet-shelf hit. The other arm sells only when that shelf is hit and `tools/sell_double_check.py` says sell yes (both %R lines off the 10-minute high, touch price as the live reading). A missing lookback still sells. 15:50 still flattens. 952 desk round trips, 2026-08-11 through 2026-09-23, minute bars from `ai_reports/runway_bars_cache.pkl`. Gross bp. Both arms pay one round trip, so the spread cancels in the difference.

| arm | trades | gross bp/trade |
|---|---|---|
| stop (first shelf hit) | 952 | -4.92 |
| stop and double-check | 952 | -7.38 |

Paired daily mean (check minus stop): **-1.11 bp/trade**, t **-0.98**, 30 scored days, positive on **30%** of them. The shelf bound on 98% of the stop arm, so the replay did see stops. The gate declined a hit on 152 trades and changed the exit on 131. The $10+ slice (not the verdict) was -2.65 bp on 696 trades.

**Verdict (pre-registered bar: paired mean >= +3 bp, t >= 2, positive on >= 60% of days): FAIL.** The live exit stays the ratchet shelf.

```
stop vs stop+double-check  trades 952  days 34  scored 30
  stop arm exits on the shelf 98%  gate declined a hit on 152 trades  exits differ on 131
  gross bp/trade  stop -4.92  stop+check -7.38  paired daily mean -1.11  t -0.98  positive days 30%
  daily paired bp  08-11:+0.0 08-12:-0.2 08-13:+0.0 08-14:+7.6 08-17:-0.5 08-18:+0.0
                   08-19:-2.7 08-20:-11.0 08-21:-2.8 08-24:+0.0 08-25:+0.2 08-26:-3.3
                   08-27:+1.6 08-28:+0.0 08-31:+7.5 09-01:-0.0 09-02:-1.4 09-03:+0.0
                   09-08:+4.0 09-09:+4.5 09-10:-1.0 09-11:-1.4 09-14:+0.9 09-15:+5.5
                   09-16:-25.4 09-17:-2.6 09-18:-1.7 09-21:-10.2 09-22:+0.7 09-23:-1.3
```

Minute shelf, not the live 2-second chase: the 8-second decay steps once a minute. The bar cache is regular hours only, so a slow %R that is not warm yet defaults to sell yes. Sessions after this cache was built are not in the read.
