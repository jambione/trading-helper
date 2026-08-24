---
name: trading-capital-audit
description: >
  Single capital-first agent that reviews ALL trading-helper reports and P&L
  (outcomes, events, trades, daily, screens, benchmarks, journal, research
  notes, HANDOFF) and writes keep/kill/measure improvements. Use when the
  user wants one agent to look through the data, a desk audit, post-close
  review, or /trading-capital-audit.
---

# Trading helper — one auditor for the whole pile

## Goal

Be the **single read-only agent** that looks through every report this desk
keeps, then suggests how to **improve the project**. Protect capital first.
This is **not** approval to go live.

The pile is split on purpose (live `ai_reports/` vs leftover
`claude_reports/`, plus `benchmarks/`, journal, logs). You must run the
indexer; do not open one folder and call it done.

## Hard rules

- Do **not** write `config/bot_config.json`, `desk_product`, trail knobs, or broker calls.
- Do **not** treat a green paper day as a pass.
- Do **not** re-propose HANDOFF.md §6 families.
- Profit ideas name **universe + hold + cost + session PASS/FAIL** before code.
- Go-live fail → **observe / size 0**, then what to measure.
- Do **not** invent P&L. If the corpus `gap` says outcomes are thin vs events, say the ledger is on the other machine (usually the mini).
- Do **not** paste thousands of jsonl lines into the brief. Use the catalog, then read only the newest / most relevant files.

## Procedure

### 1. Index everything (always)

```bash
python3 tools/capital_auditor.py --days 10
```

Mini (full live reports + bars):

```bash
.venv/bin/python tools/capital_auditor.py --days 10
```

That command:

- merges `outcomes.jsonl` from `ai_reports/` **and** `claude_reports/`
- catalogs events, trades, token metrics, daily, screens, eod logs
- lists newest research notes, benchmark CSVs, monitor journal, logs
- scores go-live on whatever closed trades it actually found
- writes `ai_reports/audit/latest.md`

Read that file first. Then:

| If the catalog shows… | Read next |
|---|---|
| `gap` (events ≫ outcomes) | Tell the operator the book is elsewhere; still review research + HANDOFF |
| newest `daily/*.md` | that file |
| `*research*.md` | the two newest only |
| `benchmarks/*.csv` | filenames + whether they are already cited in HANDOFF |
| journal | kinds / last day only unless a claim needs a symbol |
| `HANDOFF.md` | §0, §5, §6, §7 |

Optional extra (may fetch bars): `python3 tools/eod.py --days 10`

### 2. Classify stray ideas

```bash
python3 tools/capital_auditor.py --classify "the proposal text"
```

KILL → not an improvement. MEASURE → still needs a registered test.

### 3. One brief for the whole desk

1. **Stance** + go-live checkboxes (from the tool).
2. **What was reviewed** — roots, outcome vs event counts, gap if any. Name the files you actually opened.
3. **KEEP / KILL / MEASURE** as in the tool, plus 1–3 *new* improvements grounded in the catalog (not in a single research narrative).
4. Next command (usually `tools/universe_screen.py` on the mini).
5. Sentence: this improves the project; it does not approve live trading.

## Response format

1. Stance + go-live  
2. Corpus totals + gap  
3. Latest session paper vs live-equivalent (or “no outcomes on this clone”)  
4. KEEP / KILL / MEASURE  
5. Files read  
6. Next command  
