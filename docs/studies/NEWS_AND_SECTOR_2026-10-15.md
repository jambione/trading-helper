# News-timed seats and sector lead-lag — both FAIL (2026-10-15)

## News seat (`news_seat_prereg.json`, 75 sessions 2026-07-01..10-13)
NEWS_UP (fresh headline + IEX up-reaction, buy next minute, 10-min hold): half A net −10.6 bp (t −2.6, 468 events), half B −17.7 (t −3.2, 278). FAIL.
Information that answers "would paid real-time data help": the reaction itself is +33 bp (t 19) and happens in the minute of the headline; the move starts **before** publication (+12.5 bp in the prior 10 min, t 3.6). Entering on SIP **1 second** after publication earns +4.1 bp gross (t 0.6) — about the spread. After the first minute, NEWS_UP is −2.6 to −5 bp. So faster data would not rescue headline trading at our speed; the move belongs to whoever is already in.

## Sector lead-lag (`sector_laglead_prereg.json`, 26 sessions, 173 names / 12 ETFs)
Buy a laggard when its sector ETF jumps +0.25%: half A net −12.1 (t −4.8, 492 fires), half B −10.8 (t −3.8, 660). FAIL. Gross −2 / −1 bp; no sector or threshold helps; the SIP-detected version (not computable live) is −11.6. No lag to harvest at minute resolution.
