# The operator's %R trend trade: FAIL (2026-10-24)

**Rule (operator's words, mechanised and confirmed by the operator).**
- **Buy** when both %R lines are rising out of the bottom zone: an oversold event (both lines <= -80) in the last 20 minutes, fast now above -80, fast up >= 5 points and slow up over 5 minutes.
- **No buys while sideways** (either line not rising).
- **Sell** when the fast line turns down (lower than 3 minutes earlier).
- **Lines:** the desk's exact definitions (fast %R(21) EMA7, slow %R(112) EMA3), on IEX bars as live, read only at real IEX minutes.
- **Prices:** SIP.
- **Records:** prereg `docs/studies/wr_trend_trade_prereg.json` (amended), code `tools/studies/wr_trend_trade.py` (9681c9f7), two skeptic rounds each on prereg and code.

**Sample.** The full liquid warehouse universe, 110 sessions. **247,148 / 238,386 trades**; median hold 10 min; 11% of holds are 3 minutes or less; detectable effect 0.2-0.3 bp.

| | Half A (5/11-7/29) | Half B (7/30-10/15) |
|---|---|---|
| vs random later entry, same stock, same hold | −0.5 bp (t −0.9) | −0.4 bp (t −1.1) |
| vs random other stock, same minutes | **−0.9 bp (t −2.5)** | −0.3 bp (t −1.2) |
| Net after 5 bp | −5.9 bp (t −8.0) | −5.1 bp (t −10.7) |
| Win rate | 31% | 32% |

**Verdict: FAIL.** In half A the cross-name lift is t <= −2. The net upper bound is below 0 in both halves.

**Information (cannot change the verdict):**
- **Other exits:**
  - downturn from the top zone only: −6.0 / −4.3 bp net;
  - both lines down: −5.9 / −5.0;
  - 5-minute minimum hold: −5.9 / −5.0;
  - 15-minute time stop: −5.4 / −4.6.
- **Fast line only rising:** −5.9 / −5.0.
- **The risky full-square buy:** −5.2 / −5.9 net (lift +0.2 / −0.6).
- **No hour of the day is positive.**

**Reading.**
- Catching %R as it rises out of oversold is no better than a random minute, even with the matching %R exit.
- The trades win 31% of the time, roughly break-even before costs.
- The canary-validated harness and 485,000 trades rule out any effect larger than ~0.3 bp.
- What remains open is entering EARLIER than the turn (batch 2: buy-stop, divergence, deceleration, faster line) or with information %R does not carry (volume, SPY, flow).

**No desk change.**
