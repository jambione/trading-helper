# Halt reopenings: buy the LULD reopen? (2026-10-03)

Pre-registered: `halt_reopen_prereg.json` (5eb0044). Script: `tools/studies/halt_reopen_study.py`. 50,053 candidate
name-days (common stock, open >= $1, day range >= 15%), 2025-10-01..2026-10-02, SIP 1-minute bars; halts inferred
from >= 5 missing minutes after an active stretch. 1,798 events (904 UP, 894 DOWN). Entry at the reopen bar's open
(no entry cost); exit at the SIP bid (half-spread + 10 bp).

| arm | half | +5 m net (gross) | +15 m net (gross) | +30 m net (gross) |
|---|---|---|---|---|
| long UP halts (PRIMARY) | first | -345 (-43) | **-362 (-85), t -7.5** | -353 (-93) |
| long UP halts (PRIMARY) | second | -224 (+29) | **-335 (-95), t -6.1** | -436 (-188) |
| long DOWN halts | first | -259 (+42) | -232 (+80) | -203 (+99) |
| long DOWN halts | second | -287 (+13) | -267 (+19) | -301 (-9) |

By price at +15 m (UP): $1-5 -343, $5-10 -394, $10+ -325; first halt of the day only -355 (t -9.0).

**Verdict: FAIL, decisively.** Up-halts fade after the reopen (-85 to -95 bp gross at 15 min), and the exit spread
after a halt is wide (~250 bp half-spread on average), so even the small gross bounce on down-halts is swamped.
No price bucket helps. The no-spread entry does not matter when the exit pays a halted name's spread.
