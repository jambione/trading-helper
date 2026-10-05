# S/R test queue (Jonathan approved 2026-10-05)

Highest first. Pre-register each item before any data for that item. No `bot_config` trading
changes, no desk restart over ssh, no Databento, no secrets. Stay on study files if observe-only
wiring is WIP on another branch.

| # | Status | Test | Notes |
|---|---|---|---|
| **1** | **DONE — FAIL** ([write-up](SR_EXIT_RESIST_2026-10-05.md); prereg `4aea5ed`) | **Exit-only at resistance** — keep opens as-is; take profit at the bottom of the next charted resistance block; optional stop under support as info | Resistance TP lost to no-S/R time-stop control on both chronological halves. |
| **2** | **NOW** | **Size by room** — same opens; cut size when room to resistance &lt; 0.40%, full size when wider | Prereg [`sr_size_by_room_prereg.json`](sr_size_by_room_prereg.json). Primary: half-size vs full; info: skip vs full. |
| **2b** | **NEXT after #2** (queued; prereg before its own run) | **Breakout confirmation (not the first poke)** — require a close above resistance plus a higher low (or volume) before treating it as a run | Same pass bar (both chronological halves; beat control by more than measured RT spread). Do not start until #2 is written up. |
| **3** | **AFTER ~10 IEX sessions** (queued; **do not invent a score yet**) | Score the observe-only held-out log (`ob_resist_0.3` / `ob_room_pct`) | Depends on observe-only wiring + operator restart from the mini Terminal. No fabricated held-out numbers. |

## Hard rules (every item)
- Pre-register (json+md commit) **before** looking at that item's outcomes.
- Measured rates only. Pass bar = beat the locked control before costs by more than the measured
  round-trip spread on **both chronological halves**; also report net after costs.
- Any PASS = **PENDING SKEPTIC REVIEW**. Config freeze until ~10/15 for live trading knobs.
