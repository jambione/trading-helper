# Phase B dig — what Alpaca *paper* extended-hours limits fill against

**Date:** 2026-09-17 (pack #6, before trusting PASS bars)  
**Scope:** Paper account only. Live SIP/NBBO behavior may differ.

## Short answer

Alpaca **paper** extended-hours **DAY limit** buys (`extended_hours=True`) are accepted outside RTH, but fills are **not** a reliable simulation of live thin-tape premarket. Treat early PASS/FAIL as **plumbing + ledger honesty**, not as proof the Hybrid C edge survives real overnight books.

## What we know / expect

1. **Order type locked for Phase B:** market / brackets are refused outside RTH. Only DAY limit + `extended_hours=True` (see `docs/PHASE_B_PREMARKET.md`).
2. **Paper matching is synthetic:** Alpaca’s paper engine does not replay a full premarket SIP book. Limits often fill when the paper “last” crosses the limit, or on a simplified match — not when a real resting offer would trade.
3. **IEX vs Finnhub last:** Phase B arms on Finnhub stream-last; the broker may mark fills against a different print. Scoreboard `entry_slip_vs_last` is fill vs **arm last**, which is the honest desk metric even when paper is generous.
4. **Unfilled TTL still matters:** a 45s cancel → `phase_b_unfilled` is real desk behavior. Paper may understate unfilled rate vs live.
5. **Do not retune Hybrid C bars on the first paper PASS.** Pre-registered scoreboard bars stay frozen; paper fills unlock `n_fills>0` so the scoreboard can leave `NO DECISION`, not so we chase overnight knobs.

## Before trusting a PASS

- Confirm ledger has `fill` / `exit` / `flat_on_time` (or explicit miss) for the same lots as `entry_limit`.
- Spot-check a few fills: limit px, arm last, fill px, time vs 04:00–09:20 window.
- If fill_rate ≈ 100% with zero TTL cancels on thin names, assume **paper optimism** and keep friction bars in `phase_b_scoreboard.py` until live/SIP evidence exists.

## Related

- Design freeze: `docs/PHASE_B_PREMARKET.md`
- Scoreboard: `tools/phase_b_scoreboard.py`
- Ledger: `ai_reports/phase_b_ledger/YYYY-MM-DD.jsonl`
