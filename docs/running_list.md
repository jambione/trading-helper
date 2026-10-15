# Running list

Kept through the session: every concern, request and open item, with status. Newest day first.
Status: **open**, **running** (queued or in progress), **done**, **dropped** (with why).

## 2026-10-15 (Thu)

### Operator concerns (end of day)
| # | Item | Status | Where |
|---|---|---|---|
| 1 | Few opens today | running | opens review tonight (funnel 10/13-10/15, where names were lost, how late each entry was vs its run) |
| 2 | Delays when we should have opened (AAL ~70 s) | running | confirm 2→1, volume gate off, G2 limit tests off (live 12:45); tonight: arm decision on the 3 s tick + profile the 13-40 s watch poll |
| 3 | No-progress exits | running | exit sweep: off / np180 / dc180 / np_dc / np120_dc / np300_dc (scripts/exit_cut_sweep.sh) |
| 4 | Double check holds a sell at or below entry | running | exit sweep arms trail_dc, trail_dc_np_dc |
| 5 | Test momentum names when they appear on the data seed | running | momentum_alert_markouts, momentum_alert_ticks (30-60 s, premarket split), clock_windows (from 16:15) |
| 6 | Review today's exits: trail updates and no-progress | open | per-exit reason, trail path, after-exit move; split at 11:25 (no-progress off) |

### Shipped today
- Weekly book live (paper); AGY on gemini-3.8-flash-high; first order Fri 10/16 15:58
- Discord OCR on ScreenCaptureKit (Screen Recording re-granted 09:56)
- Desk AGY model fixed (all AGY calls had failed since 10/14 11:00)
- ETFs/ETNs/funds never take a book seat
- Book: Gates column (replaces SR), Status uses the arm's tape ceiling, near-arm highlight (needs tight lines), one-line mobile book, mobile positions history
- No-progress cut OFF (11:25)
- Delay cuts (12:45): pre-square confirm 1 poll, recent-volume gate off, G2 entry/exit tests off
- Movers admission print-age 60 → 180 s (13:26)
- Finnhub stream reconnects when its write side dies (fixed twice; live 13:31)

### Incidents
- 05:52-10:22 and 12:58-13:31: our Finnhub socket stopped sending (new names never subscribed, book emptied) - fixed
- 12:55-14:16: Alpaca data vendor outage (IEX and SIP) - external; 10/15 is a degraded day in every study

### Tonight (mini, after 16:15)
- Exit sweep, 10 arms × 8 days → tomorrow's exit config
- Oversold ▲ entry test (os_leave, skeptic READY) → candidate earlier entry
- Momentum-alert studies → SIP-month keep-bar ($4.70/day)
- Simulator fidelity gates (hist_sim_fidelity.sh)

### Build tonight (before the open)
- Arm decision on the 3 s book tick; profile and cut the watch poll
- Pre-subscribe candidates before admission; verify every subscribed name prints, re-subscribe if not
- Opens review + exit review (items 1 and 6)
- Arm-proximity scan design

### Parked / for later
- Volume profile (operator: never mind)
- JEV model (park until a timing edge exists)
- Why the Finnhub write side keeps dying (auto-reconnect makes it harmless for now)
- Re-score PRE/SQ with warmed %R lines (same cold start the ▲ test had)
