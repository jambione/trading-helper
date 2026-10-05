# S/R test queue (Jonathan approved 2026-10-05)

Highest first. Pre-register each item before any data for that item. No `bot_config` trading
changes, no desk restart over ssh, no Databento, no secrets. Stay on study files if observe-only
wiring is WIP on another branch.

| # | Status | Test | Notes |
|---|---|---|---|
| **1** | **DONE — FAIL** ([write-up](SR_EXIT_RESIST_2026-10-05.md); prereg `4aea5ed`) | **Exit-only at resistance** | Resistance TP lost to no-S/R time-stop on both halves. |
| **2** | **DONE — FAIL** ([write-up](SR_SIZE_BY_ROOM_2026-10-05.md); prereg `363728b`) | **Size by room** — half-size when room &lt; 0.40%, full when wider | Sized−equal lift −12.0 / −1.7 bp vs RT ~12 bp; does not clear the bar. |
| **2b** | **NOW** | **Breakout confirmation (not the first poke)** — close above resistance + higher low (or volume) before treating as a run | Prereg [`sr_breakout_confirm_prereg.json`](sr_breakout_confirm_prereg.json). |
| **3** | **AFTER ~10 IEX sessions** (queued; **do not invent a score yet**) | Score observe-only held-out log (`ob_resist_0.3` / `ob_room_pct`) | No fabricated held-out numbers. |

## Hard rules (every item)
- Pre-register (json+md commit) **before** looking at that item's outcomes.
- Measured rates only. Pass bar = beat the locked control before costs by more than the measured
  round-trip spread on **both chronological halves**; also report net after costs.
- Any PASS = **PENDING SKEPTIC REVIEW**. Config freeze until ~10/15 for live trading knobs.
