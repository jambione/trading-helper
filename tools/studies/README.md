# tools/studies — the 2026-09-23 research, versioned

Each script is self-documenting (module docstring). Run from the repo root on
the mini (they need `config/secrets.json` and the SIP bar/quote caches):

    .venv/bin/python tools/studies/<name>.py

| Script | Question | Headline result (2026-09-23) |
|---|---|---|
| `counterfactual_today.py DAY` | The day replayed under the current setup, with and without the gates | 09-23: actual −$61.78 → about −$16 to −$20 |
| `cost_study.py` | Where the cost per trade goes (1m bars; superseded by `tools/exec_report.py`) | Market −0.05%, costs 0.17% |
| `exit_study.py` | What price did after each exit; replay of exit shapes | Exits can't move the average |
| `exit_sweep.py` | Arm level × trail from the peak × 60s rule | All within 0.05pp; the 60s rule hurts |
| `seed_sweep.py` | Starting-stop size | 1% is best in every set |
| `time_stop_sweep.py` | Sell if not armed within N minutes | Worse at every N, in both halves |
| `spread_gate_study.py` | One-arm entries priced at their true SIP spread | A ≤0.20% gate takes net from −0.062% to −0.011% |
| `name_rank_study.py` | Rank the book by 50-day MA plus news? | No: worse, and the halves flip |
| `daily_filter_screen.py` | Daily trend and news as filters | Gap-down >1% names 46% (z −3.7) |
| `name_quality.py` | Do seed-time features pick better names? | No, beyond what the book already admits |
| `refusal_runway.py` | Do refused names run better than the ones we trade? | `thin_rvol` refusals are flat |
| `signal_timing.py` | Where in the %R cycle to buy | Superseded by `tools/entry_screen.py` |
| `sector_study.py` | Sector strength vs SPY | Sector leading SPY by >0.5%: 62.4% (z +3.9, both halves) |
| `sector_book_study.py` | Does the sector filter survive the book? | No: +0.013% vs +0.008%/trade (±0.061), a third of the trades |
| `runway_target_study.py` | What predicts +2% before -1% within 60m? | Volume pace vs own normal: 4.0% -> 16.0% (z +13.8); volatility and gap up too. Held-out top third runs 2.4x, but net is unchanged with the 0.35% trail |
| `vol_trail_study.py` | Volatility-scaled trail / stop, no book rules | Wider trail (4x vol) helps high-runway names in both halves; wider seeds and a longer window hurt |
| `vol_trail_book_study.py` | Same under gates, true spreads and book rules | S7 (one arm, rvol_pace >= 1.64) +0.046%/trade, both halves positive, t~0.9; S7 + 4x-vol trail +0.158% but $74 of $79 from one day. Not shipped; observe first |
