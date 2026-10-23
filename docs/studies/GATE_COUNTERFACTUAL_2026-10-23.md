# Admission gates, counterfactual screen: no gate shows a cost; the float cap is NO EVIDENCE (2026-10-23)

Prereg: [gate_counterfactual_prereg.json](gate_counterfactual_prereg.json). Amendments amended .. amended_3 were all made
before any refused-vs-board return was computed. Script: `tools/studies/gate_counterfactual.py` (c64d3e7d, two skeptic code
rounds). Window: 30 sessions, 2026-09-10 .. 10-21, halves split at 10-01.
- **Data:** refusals from `rejects.jsonl`, the desk's board from `decision_ledger` arm rows, and SIP 1-minute bars.
- **This is a SCREEN.** The detectable effect is about 30-40 bp per half against a 5 bp bar, so it can only catch large
  costs.

**The question (operator 10/23): is the thin book costing good trades?** For each gate, did names it refused (counted
whether or not they were admitted later) move better than board names over the same 10 minutes?
- **Entry:** the open of the next minute.
- **Control:** board names active in the 2 minutes before the refusal, measured over the same minutes.

| Gate | Half A: refused minus board | Half B | Refused names' own 10-min move | Reading |
|---|---|---|---|---|
| **Float cap > $800M** (primary; 227 clean name-days) | -27 bp (t -0.5) | +5 bp (t 0.6, 95% -13 to +24) | +3 / -6 bp | **NO EVIDENCE** |
| Price floor (information) | -143 bp | -66 bp | -162 / -71 bp | refused names did much worse |
| Gap-down > 1% (information) | -30 bp | -35 bp (t -1.6) | -26 / -48 bp | refused names did worse |
| Spread > cap (information, gross) | +11 bp | +13 bp | +9 / +4 bp | slightly better gross; only +7 bp after the 0.05% cap; these names cost more to trade |
| Thin RVOL (information) | -6 bp | +29 bp (4 days) | 0 / +25 bp | mixed, thin data |
| Not-uptrend / range position | 956 of 1,202 refused name-days were admitted later the same day | | | these gates delay, they rarely refuse |

**What it shows.**
- **The float cap is not visibly costing trades.** Refused large-float names moved about the same as the board (overall
  -14 bp, t -0.45) and roughly flat on their own (+3 / -6 bp). Nothing suggests relaxing it would add winners. A real but
  small cost cannot be ruled out.
- **The gates that refuse the most do not appear to turn away good trades.**
  - **Price floor and gap-down:** the names they kept out did worse than the board, as the earlier evidence said.
  - **Spread gate:** turns away names that moved slightly better gross, but they would cost more to trade, so there is
    no net case.
- **The thin book is mostly the price of avoiding worse trades,** not of missing better ones. That fits the 10/14 replay:
  loosening gates added trades that lost money.

**Caveats (declared).**
- **Moves, not trades:** a 10-minute move is not a desk trade (no exits, no spread).
- **Gate order:** the float gate runs before the trend, RVOL and score gates, so float-refused names were never tested by
  those.
- **Short window:** 30 sessions, with config changes inside it.
- **Feed:** SIP bars, while the live desk runs on IEX.

## Follow-up: the spread cap after costs (information, registered before any quote)

The screen's +11 / +13 bp for spread-refused names was before costs. `tools/studies/gate_spread_net.py` (36627669)
fetched the real SIP bid/ask at each entry minute. Each refused name paid its own full spread, and the board side paid
the mean spread of 3 seeded board names. Sample: 649 refused name-days, 630 with quotes.

| | All days | From 10/03 (the 0.05% cap) | Before 10/03 |
|---|---|---|---|
| Gross: refused minus board | +11.6 bp | +4.1 bp | +27.9 bp |
| **Net, after each side's spread** | **-12.8 bp** (t -1.2, 95% -36 to +10) | **-14.1 bp** (t -0.9) | -10.1 bp (t -0.8) |
| Median quoted spread: refused / board | 14.8 / 8.1 bp | 10.3 / 5.5 bp | 34.4 / 18.6 bp |

**Reading: no net case.** The refused names' small gross advantage is more than eaten by their wider spreads. After
costs they did about 13-14 bp worse than the board. The spread cap protects the desk; keep it.
