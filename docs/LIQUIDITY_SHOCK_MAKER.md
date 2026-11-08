# liquidity_shock_maker (docs/studies/liquidity_shock_maker_prereg.json)

SIP bars; 1 scored test (M: Passive Liquidity Air-Pocket Maker), one-sided alpha 0.05. Canaries passed (planted +8 bp recovered, controls unchanged, null lift within 2 SE).
Scored days: 96. Threshold used: 80 bp drop over 2 minutes, volume ratio >= 3.0.

## M Passive Liquidity Air-Pocket Maker: FAIL
- n 606 (halves (320, 286)), scored days 93 (halves (46, 47)); primary control mean -5.28, secondary -12.99, bar-of-day baseline -2.97 bp
- lift vs primary -3.24 bp, day-t -0.87 vs crit 1.66, halves -8.39/+1.81; winsorized halves -8.52/+1.76; lift vs secondary +14.01
- net -8.61 bp (halves -15.37/-1.99); at stress -12.61

### Exit Reason Breakdown:
- TARGET: 325 of 606 (53.6%), mean net +40.00 bp
- STOP: 158 of 606 (26.1%), mean net -103.23 bp
- TIME: 123 of 606 (20.3%), mean net -24.74 bp
- Hold duration: mean 4.4 bars, median 3 bars (0 = bar t+1, 10 = bar t+11)

(skipped trades: 24; unfilled orders: 215; filled orders: 606; control candidates removed: primary 79248, secondary 11566)

