# Connors RSI-2 green as a 1-minute entry: FAIL (2026-10-14)

Prereg: `docs/studies/connors_green_prereg.json` (skeptic READY after two rounds). Harness: `tools/studies/early_entry_honest.py --study connors --score`, 26 cached sessions 2026-09-08..10-13, live-shaped series (raw IEX bars with the last 220 prior-session bars as warm-up; 4 name-days without prior bars, no day excluded).

Rule: close > SMA(200) and close < SMA(5) and RSI-2 < 10 on a completed IEX bar; onset on board names, 09:50-15:30, 15-min refractory. Buy the next SIP open, hold 10 min, 10 bp round trip; control = a random later minute of the same name.

| half | signals | days | net | lift vs later random | t | verdict |
|---|---:|---:|---:|---:|---:|---|
| A (to 09-24) | 627 | 13 | -6.0 bp | +16.3 bp | +1.34 | FAIL (net < 0, t < 2) |
| B | 1,270 | 13 | -16.3 bp | -0.7 bp | -0.41 | FAIL |

**Verdict: FAIL.** The completed-bar 1-minute Connors green is closed as an entry on the free feed (if_fail).

Information: 5-minute lift +7.8 bp (t 1.85), 15-minute +3.4, 30-minute -2.6 — any bounce is brief and does not pay the spread. Cross control (another board name, same minute) +13.8 bp (t 1.36). Green leads desk fills by a median 9 minutes on 206 of 1,057 fills. The board itself faded -8.3 bp over 10 minutes (t -2.8) in this window.
