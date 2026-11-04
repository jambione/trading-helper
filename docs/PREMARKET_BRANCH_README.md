# premarket/phase-b-2026-11 (parked)

Created 2026-11-04 (operator: "own branch, dry run"). The live desk runs Phase B in dry run.

- Nine premarket commits from desk/2026-11-03-before-friday-restore, cherry-picked with -x. They cover the SIP feed switch, extended-hours exits, the seat overlay, safety rails, sizing rails, and simulate_premarket_week.
- Not included: 7fed2972 "RTH parity". It re-adds the 5-minute smart zombie cut and profit-capture changes to the DAY desk's exits.
- The config is the live desk's. The picks lowered ai_movers_min_price to $2, which is the all-day movers floor.

**Not merge-ready.** The full suite shows 6 failures beyond master-mac's baseline:
- tests/test_trail_triangle_first.py (4). d5d2835e "align exit logic with RTH ratchet stop" changes apply_local_trail for the day desk too.
- tests/test_setup_audit.py (2). The new premarket knobs are not in the config fingerprint lists.

**Blocker for going live:** IEX shows no premarket bars for the admitted names (Sept SIP-replay check: 0/85). Real-time SIP needs the paid plan.
