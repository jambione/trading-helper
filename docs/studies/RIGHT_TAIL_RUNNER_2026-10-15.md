# Right-tail runner — FAIL, but the tail is real (2026-10-15)

Prereg `docs/studies/right_tail_runner_prereg.json` (skeptic READY after two rounds). First fill per name-day 9/08-10/13 (431 of 1,057 fills); runner = 2% stop, 1% trail after +1%, else 15:49; graded worse bar ordering; 5 bp exit cost (+4.0 bp measured stop slippage as a second level).

| half | trades | runner net | actual net | runner − real fill (t) | runner − actual on SIP (t) | median hold | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| A | 168 | −11.0 | −28.6 | +17.6 (2.41) | +15.9 (1.24) | 46 min | FAIL (runner net < 0, SIP arm t < 2) |
| B | 262 | −17.2 | −9.5 | −7.7 (−0.92) | −5.8 (−0.71) | 144 min | FAIL |

**The tail exists, the leash removes it, and it does not pay for the losers at this entry quality:**

| | trades ≥ +2% | ≥ +5% | best trade | top 5% of trades, sum | all trades, sum |
|---|---:|---:|---:|---:|---:|
| A runner | 6.5% | 1.8% | +718 bp | +3,366 bp | −3,545 bp |
| A actual | 0.6% | 0% | +295 bp | +1,091 bp | −6,301 bp |
| B runner | 2.7% | 0.8% | +588 bp | +3,252 bp | −3,986 bp |
| B actual | 0% | 0% | +116 bp | +651 bp | −2,568 bp |

The top 5% of runner trades earn +3,300 bp in each half; the other 95% lose more. A wider stop (3%) finds more +2% trades (13.7% / 4.2%) but loses more overall; holding to the close with only a stop is worst. Implication: the right tail is an ENTRY-SELECTION problem — a filter that removes even a third of the runner's losers while keeping its winners would flip it. That is the next test, not a looser exit.
