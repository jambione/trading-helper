# Task 10 — Deploy verification checklist (Mac mini)

Run after merging/deploying `feat/ai-trader-execution` to the mini.

- [ ] `ai_trader` starts cleanly; no import errors for `ai_entry_watch`
- [ ] Config: `ai_watch_enabled=true` (default); confirm in running process env/config
- [ ] With market open, force or wait for an agreed name with `wait_for_zone` structure and ask in zone → paper bracket or `entry_ok` / status `submitted`
- [ ] `claude_reports/events.jsonl` (or `ai_reports/`) includes `entry_decision` with `entry_low` / `summary` on WAIT
- [ ] `ai_positions_state.json` (or dual-write) includes `entry_watch` array after positions tick
- [ ] After RTH close (process saw open earlier that day), unfilled watches expire (`status=expired`)
- [ ] Open positions still manage stops/targets (`manage_open_positions` path unchanged)
- [ ] Hard-refresh dashboard if UI later surfaces `entry_watch`

Manual only — no code change required beyond this checklist.
