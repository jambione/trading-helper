# Frozen-knob window — 2026-09-21 → 2026-09-25

**Pre-registered 2026-09-20, before any result was seen.** Everything below the
line was written down first. Changing a bar after seeing data voids the window.

## Baseline (the thing being frozen)

| | |
|--|--|
| `config_fp` | **`bd18c6344fb8`** |
| git | **`3ba3896`** |
| Sessions | Mon **2026-09-21** → Fri **2026-09-25** (5 RTH sessions) |
| Score on | Mon **2026-09-28** |
| Lane | RTH Plan A only. Phase B scored separately, never merged in. |

Verify the fingerprint still matches before trusting any score:

```bash
ssh mac-mini-away 'cd ~/repo/trading-helper && .venv/bin/python -c "
import sys; sys.path.insert(0,\".\")
from learn_stamps import config_fingerprint; from config import load_config
print(config_fingerprint(load_config()))"'
```

## Void conditions (any one kills the window)

- `config_fp` differs from `bd18c6344fb8` on any scored session.
- A knob is changed mid-window "just to see" — including via `POST /api/config`.
- The desk is down for a session (like 2026-09-19) — that day is dropped, not
  patched, and the window extends by one session.
- Scoring is run before 2026-09-28.

A config change does not pause the window. It **restarts** it.

## The question

Is admission/selection a lever? Every other component is already falsified:
entry setups score noise-equivalent, exits move win rate but not the mean, the
universe fades at every horizon. The one survivor is admission range position
(shipped, `ai_watch_admit_max_range_pos = 90.0`). This window asks whether the
*source* a name arrives from carries information.

## Decision rules (from `tools/source_scorecard.py`, unchanged)

| Outcome | Action |
|---|---|
| source − control ≤ 0, adequate n, ≥5 sessions | **cut or demote** that source |
| source − control > 0, holds on held-out days | raise its seed cap |
| every source ≈ control | **selection is not the lever** — stop paying for it |

Report only. The tool never auto-applies, and neither do we mid-window.

## Known limitation — read before expecting an answer

The 3-session dry run on 2026-09-16/17/18 (296/431/414 episodes) returned
**THIN or NOT_REPORTABLE in every cell**. The binding problem is power, and it
is structural:

- 123 cells carried a computed `required_n`: **median 95**, max **1,661,890**.
- Actual n: **median 7**, max 111.
- Only **17 of 123** cells had n ≥ required_n, and most of those are degenerate
  (n=1 against required_n=1 — a huge effect measured on one observation).

The one genuinely powered cell group was **trending on 2026-09-18** (n=86–111
against required_n 21–34), and it was **negative at every horizon**:
−0.557 (15m), −0.721 (30m), −0.796 (60m), −0.796 (120m). It is still THIN
because 100% of that n came from a single session, and day effects are not
independent observations.

**So set expectations honestly:** 5 sessions is likely enough to render a
verdict on **trending** (high episode volume, and it will clear the ≥5-session
and session-dominance bars). It is **not** enough for momentum, movers, or the
research sources, which produce 1–8 exclusive episodes a day. Those need
months, or a different question.

If trending comes back negative across 5 independent sessions, that is a real
"cut or demote" result and the window paid for itself. If everything else stays
THIN, that is also information: it says this scoreboard cannot answer the
selection question at this desk's episode volume, and the measurement budget
should move rather than grind.

## Data hygiene

- **Exclude 2026-09-16 from any fill-based view.** `fill_reconcile` found
  ledger=49 against broker=80 (31 `BROKER_ONLY`) — the day was backfilled
  incompletely when the ledger shipped on 9/18. Proposal-ledger views are
  unaffected; `--retro-fills` is not.
- 2026-09-19 is a **Saturday**. No data is correct, not an outage. (An earlier
  draft of this file called it a lost Friday — calendar error, corrected.)

## Log

| Date | Event |
|------|-------|
| 2026-09-20 | Window pre-registered at `bd18c6344fb8` / `3ba3896`. |
