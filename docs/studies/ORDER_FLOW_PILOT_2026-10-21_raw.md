# Order-flow pilot: result

Prereg: docs/studies/order_flow_pilot_prereg.json (amended .. amended_4, budget_restated). PRE entries, SIP ticks; gated (top tercile) minus rest on gross 180 s mid-to-mid; gated net = ask in, bid out.

Rows 15563, drops {'missing_h60': 417, 'missing_h120': 394, 'missing_h180': 398, 'missing_h300': 440}.

| group | half | IN | OUT | days | gated - rest (bp) | t | t_crit | 97.5% UB | gated net | name-day | drop 3 names / days | half |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| G1 | A | 2494 | 5006 | 55 | +1.9 | 3.54 | 2.47 | +2.9 | -16.0 | +2.0 | +1.7 / +1.5 | NOT_PASSED |
| G1 | B | 2608 | 4891 | 55 | +1.5 | 3.41 | 2.47 | +2.4 | -12.5 | +1.3 | +1.2 / +1.1 | NOT_PASSED |
| G2 | A | 2524 | 5044 | 55 | +0.5 | 0.88 | 2.47 | +1.7 | -14.9 | +0.7 | +0.3 / -0.1 | NOT_PASSED |
| G2 | B | 2505 | 5068 | 55 | +0.9 | 1.86 | 2.47 | +1.9 | -11.7 | +0.7 | +0.7 / +0.5 | NOT_PASSED |
| G3 | A | 963 | 6524 | 55 | +1.9 | 2.27 | 2.47 | +3.5 | -15.2 | +2.0 | +1.5 / +1.2 | NOT_PASSED |
| G3 | B | 987 | 6504 | 55 | +1.8 | 3.15 | 2.47 | +3.0 | -10.9 | +1.7 | +1.5 / +1.2 | NOT_PASSED |

| group | verdict |
|---|---|
| G1 | **FAIL** |
| G2 | **FAIL** |
| G3 | **FAIL** |

## Information (cannot pass)

```
{
 "H=60s gross": {
  "G1": {
   "A": "+1.2 bp (t 4.08, n_in 2487)",
   "B": "+0.9 bp (t 3.0, n_in 2627)"
  },
  "G2": {
   "A": "+1.0 bp (t 3.19, n_in 2520)",
   "B": "+1.0 bp (t 4.16, n_in 2526)"
  },
  "G3": {
   "A": "+1.5 bp (t 3.31, n_in 961)",
   "B": "+1.2 bp (t 3.66, n_in 997)"
  }
 },
 "H=120s gross": {
  "G1": {
   "A": "+1.7 bp (t 4.16, n_in 2490)",
   "B": "+1.4 bp (t 3.88, n_in 2619)"
  },
  "G2": {
   "A": "+1.1 bp (t 2.4, n_in 2518)",
   "B": "+1.3 bp (t 3.07, n_in 2508)"
  },
  "G3": {
   "A": "+1.8 bp (t 2.74, n_in 960)",
   "B": "+1.9 bp (t 3.72, n_in 987)"
  }
 },
 "H=300s gross": {
  "G1": {
   "A": "+1.9 bp (t 2.44, n_in 2487)",
   "B": "+1.3 bp (t 2.28, n_in 2603)"
  },
  "G2": {
   "A": "+0.1 bp (t 0.17, n_in 2502)",
   "B": "+1.2 bp (t 2.08, n_in 2512)"
  },
  "G3": {
   "A": "+1.9 bp (t 1.77, n_in 953)",
   "B": "+1.7 bp (t 2.04, n_in 984)"
  }
 },
 "MICRO top tercile": {
  "A": "+0.3 bp (t 0.62, n_in 2521)",
  "B": "+1.2 bp (t 2.49, n_in 2381)"
 },
 "by hour (global cuts)": {
  "10:00": {
   "G1": {
    "A": "+3.9 bp (t 2.23, n_in 451)",
    "B": "+5.4 bp (t 3.34, n_in 387)"
   },
   "G2": {
    "A": "-1.2 bp (t -0.55, n_in 451)",
    "B": "-0.1 bp (t -0.04, n_in 377)"
   },
   "G3": {
    "A": "+1.5 bp (t 0.4, n_in 155)",
    "B": "+2.9 bp (t 0.82, n_in 128)"
   }
  },
  "11:00": {
   "G1": {
    "A": "+1.2 bp (t 0.92, n_in 362)",
    "B": "+0.6 bp (t 0.47, n_in 406)"
   },
   "G2": {
    "A": "-1.2 bp (t -0.97, n_in 397)",
    "B": "-0.1 bp (t -0.13, n_in 387)"
   },
   "G3": {
    "A": "+1.2 bp (t 0.57, n_in 144)",
    "B": "-1.8 bp (t -0.99, n_in 132)"
   }
  },
  "12:00": {
   "G1": {
    "A": "+2.6 bp (t 2.18, n_in 440)",
    "B": "+2.0 bp (t 2.18, n_in 457)"
   },
   "G2": {
    "A": "+2.8 bp (t 2.47, n_in 439)",
    "B": "+0.9 bp (t 0.83, n_in 418)"
   },
   "G3": {
    "A": "+5.9 bp (t 2.99, n_in 175)",
    "B": "+0.8 bp (t 0.62, n_in 168)"
   }
  },
  "13:00": {
   "G1": {
    "A": "+1.4 bp (t 1.48, n_in 455)",
    "B": "+0.1 bp (t 0.21, n_in 524)"
   },
   "G2": {
    "A": "-0.0 bp (t -0.05, n_in 455)",
    "B": "+1.0 bp (t 1.17, n_in 490)"
   },
   "G3": {
    "A": "+2.4 bp (t 1.48, n_in 168)",
    "B": "+0.0 bp (t 0.02, n_in 209)"
   }
  },
  "14:00": {
   "G1": {
    "A": "+1.1 bp (t 1.15, n_in 520)",
    "B": "+0.8 bp (t 0.99, n_in 543)"
   },
   "G2": {
    "A": "-1.0 bp (t -0.87, n_in 514)",
    "B": "+1.5 bp (t 2.13, n_in 536)"
   },
   "G3": {
    "A": "+0.1 bp (t 0.07, n_in 209)",
    "B": "+1.4 bp (t 1.22, n_in 231)"
   }
  },
  "15:00": {
   "G1": {
    "A": "-0.3 bp (t -0.22, n_in 266)",
    "B": "+1.3 bp (t 1.0, n_in 291)"
   },
   "G2": {
    "A": "+2.2 bp (t 1.59, n_in 268)",
    "B": "+1.5 bp (t 1.24, n_in 297)"
   },
   "G3": {
    "A": "+0.9 bp (t 0.57, n_in 112)",
    "B": "+2.5 bp (t 1.16, n_in 119)"
   }
  }
 },
 "terciles cut within each hour": {
  "G1": {
   "A": "+2.0 bp (t 3.45, n_in 2495)",
   "B": "+1.5 bp (t 3.06, n_in 2583)"
  },
  "G2": {
   "A": "+0.6 bp (t 0.92, n_in 2526)",
   "B": "+0.9 bp (t 1.93, n_in 2495)"
  }
 },
 "G1 within onset-bar-return terciles": {
  "cuts (half A)": [
   0.0,
   0.0004682509789768474
  ],
  "onset return low": {
   "A": "+3.3 bp (t 2.49, n_in 312)",
   "B": "+2.3 bp (t 1.69, n_in 321)"
  },
  "onset return mid": {
   "A": "+1.7 bp (t 2.33, n_in 1004)",
   "B": "+0.2 bp (t 0.47, n_in 1161)"
  },
  "onset return high": {
   "A": "+1.6 bp (t 1.64, n_in 1050)",
   "B": "+2.7 bp (t 3.21, n_in 973)"
  }
 },
 "share whose 3-min max BID clears the entry ask by > 2x the spread (net excursion)": {
  "G1 IN": 0.06534578014859974,
  "G1 OUT": 0.10290628706998814,
  "G2 IN": 0.09157935584012418,
  "G2 OUT": 0.08802545314307751,
  "G3 IN": 0.06646676661669165,
  "G3 OUT": 0.09377811094452773
 },
 "not computed": {
  "IEX-only trade signing": "needs the if_pass feed=iex fetch (amended_4 (6))"
 },
 "note": "gated net averages every day with an IN entry, not only the days graded for the difference"
}
```
