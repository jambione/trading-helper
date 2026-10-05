# S/R test queue (Jonathan approved 2026-10-05)

Highest first. Pre-register each item before any data for that item. No `bot_config` trading
changes, no desk restart over ssh, no Databento, no secrets. Stay on study files if observe-only
wiring is WIP on another branch.

| # | Status | Test | Notes |
|---|---|---|---|
| **1** | **DONE — FAIL** ([write-up](SR_EXIT_RESIST_2026-10-05.md); prereg `4aea5ed`) | **Exit-only at resistance** — keep opens as-is; take profit at the bottom of the next charted resistance block; optional stop under support as info | Prereg [`sr_exit_resist_prereg.json`](sr_exit_resist_prereg.json). Pass: beat no-S/R exit control before costs by more than measured RT spread on both chronological halves; report net. SIP sample; SIP≠IEX. |
| **2** | **NEXT** (queued; do not run until #1 is written up) | **Size by room** — same opens; cut size when room to resistance &lt; ~0.4%, full size when wider | Needs its own prereg before any sizing outcome. Not started. |
| **3** | **AFTER ~10 IEX sessions** (queued; **do not invent a score yet**) | Score the observe-only held-out log (`ob_resist_0.3` / `ob_room_pct`) once the desk has logged ~10 IEX sessions with observe-only order-block fields | Depends on observe-only wiring merging and the operator restarting from the mini Terminal. Document only; no fabricated held-out numbers. |

## Hard rules (every item)
- Pre-register (json+md commit) **before** looking at that item's outcomes.
- Measured rates only. Pass bar = beat the locked control before costs by more than the measured
  round-trip spread on **both chronological halves**; also report net after costs.
- Any PASS = **PENDING SKEPTIC REVIEW**. Config freeze until ~10/15 for live trading knobs.
