# Handoff: state of the desk, 2026-08-20 EOD

Audience: Grok, as book owner and research agent on this desk. You have no
access to the session that produced this, so it is self-contained. It states
what was measured today, what instruments now exist, what the options are, and
where the current plan points — and it ends with specific questions, because
the point of this document is your independent read, not your agreement.
Challenge anything here; every claim carries its evidence and its weakness.

## 1. What happened today, in one paragraph

The desk's strategy — momentum continuation on the daily watchlist, ~86-second
holds, mechanical trail exits — was measured against controls for the first
time and showed **no positive edge at any layer**: entry timing is *negatively*
predictive (4.2σ), name selection is indistinguishable from random liquid names
(0.6σ), and no exit configuration clears friction. Separately, the tuning
simulator turned out to have placed **zero trades ever** — every historical
"no overlay beat live, do not change config" verdict was empty. The simulator
is fixed, the measurement stack is rebuilt on 1m bars, and the open question
has moved from "what settings" to "what entry thesis, if any, has edge".

## 2. The evidence

### 2a. The day's book (2026-08-20, paper)

59 closed trades, **28.8% win rate**, sum −0.33R. 55 of 59 exits were
`local_trail`, median hold 86 seconds, median MFE +0.04R / MAE −0.02R.
Median run-up before a trail exit +0.23%; median give-back from peak +0.23% —
the trail returns the whole typical move and pays the spread. Post-exit drift:
+0.13% @5m, −0.09% @15m, −0.23% @30m, so the trail is *not* cutting winners;
there is nothing to cut.

### 2b. Admission timing is negative (`tools/admission_null.py`)

593 RTH admissions, 2026-08-14..20, SIP 1m bars, paired per admission:

| horizon | admitted − WITHIN (random instant, same name/day) | beat | σ |
|---|---|---|---|
| 5m | −0.038% | 46% | 2.0 |
| 15m | −0.208% | 41% | 4.3 |
| 30m | −0.355% | 41% | 4.2 |
| 60m | −0.421% | 43% | 3.4 |

The admission moment is reliably **worse than a random moment in the same
name**. Meanwhile admitted − ACROSS (other watched names, same instant) ≈ 0,
and admitted − OUTSIDE (price-matched liquid names never watched) is −0.019%,
0.6σ: **selection adds nothing measurable either**. Caveat: OUTSIDE is
price-matched but not volatility-matched, so it is a calmer pool; the paired
direction stands, the magnitude comparison is soft.

**A second caveat surfaced by the rule screen (§6): the WITHIN control is
loaded with hindsight.** Its random instants span the whole session, but a
name joins the watchlist *because* it moved that morning — so the dart gets to
buy a run-up the desk could never have traded, because the name was not on the
list yet. The magnitude of "worse than random" is therefore inflated by an
unknown amount. The same-instant controls (ACROSS, OUTSIDE) carry no
hindsight, and against them the desk is flat, not negative. The defensible
claim is: **no positive timing edge against any same-instant alternative**,
not "reliably destructive".

### 2c. But the watched names themselves drift

WITHIN — buy a watched name at a *random* RTH moment:

| horizon | median | up% | mean |
|---|---|---|---|
| 5m | +0.000% | 49% | ~0 |
| 30m | +0.099% | 59% | +0.309% |
| 60m | +0.149% | 57% | **+0.681%** |

Drift turns on past ~15m, and the 60m mean is 4–5× the median — fat right
tail. The desk harvests at 86 seconds, where the median move is exactly zero.

### 2d. The exhaustion gate points backwards (bar-scored, `tools/desk_report.py`)

Forward 30m by %R-exhaustion at the decision moment:

| bucket | n | fwd |
|---|---|---|
| 0–25% | 963 | **+0.309%** |
| 25–50% | 815 | **+0.334%** |
| 50–75% | 949 | −0.626% |
| 75–90% | 607 | +0.511% ← non-monotonic; treat with suspicion |
| 90–100% | 234 | −0.591% |

Live config requires **heat ≥ 40** (`ai_watch_exhaustion_heat_min_pct=40`,
no ceiling): the floor excludes the only consistently positive range, and
`heating_too_low` refused 527 arms today. Corroboration: `replay_ab`'s
overbought core, n=851, mean −0.67%. A 5-day sweep of the heat gate
(`tools/rstop_search_entry_heat.json`) produced **no candidate**: best cell
(heat_min=0, max=75) made +$23.90 on n=149 but won 1/5 held-out folds, with an
incoherent response curve (min=20 → −$70) and friction larger than the gain.
Important limit: every cell kept the live 86-second exits, so cool entries
were never tested at a horizon where they could work. Entry and horizon are
coupled; that grid varied only one.

### 2e. The risk frame does not fit the signal

1R = 5% of price (`ai_watch_synth_stop_pct=5`). Median trade MFE is 0.046R —
stop and T1 sit ~20× beyond anything the trade does. T1 attached 34 times
today and filled zero. Every trade resolves on the trail, the only mechanism
scaled anywhere near the tape.

## 3. What was broken and is now fixed

1. **The simulator never traded.** Two causes: the desk runs
   `ai_watch_cm_rsi_local=False` (the engine publishes CM RSI-2; a replay has
   no engine, so every bar refused `no_rsi_data`), and once forced local, the
   `cm_rsi_rising` direction was never written, so every bar refused
   `rsi_not_rising`. Fixed in `sim_rstop_path.try_arm` + `live_cm_rsi`. Same
   command, same tape: n=0 → n=42 (1 day) / 225 (5 days). **Every config
   decision ever justified by a sweep was justified by nothing.**
2. **Empty runs now refuse to render a verdict** (`EMPTY RUN` guard in
   `optimize_rstop` and `replay_ab`) and print arm-refusal counts. Before
   quoting any sweep, check n > 0 including baseline.
3. **`desk_report` forward returns were biased**: scored off the shadow
   series, which only exists while the desk watches a name — conditioning on
   a post-decision variable (median episode 383s vs 30m horizon). All forward
   returns now come from 1m bars via `tools/bars.py` (one shared fetcher; four
   tools use it).
4. **Six knobs were unsweepable**, including the size of 1R
   (`synth_stop_pct`), the heat ceiling, and the $0.06 min-give floor that
   actually binds the trail. Now in `OVERLAY_KEYS`.

## 4. Live config that matters (bot_config.json on the mini)

| knob | value | note |
|---|---|---|
| CM RSI band | 0–50 | 75 was tried intraday, reverted on the evidence |
| heat_min / heat_max | 40 / 0 (off) | **contradicts §2d; deliberately unchanged pending a real test** |
| 1R (synth_stop_pct) | 5% | ~20× the median move |
| min_give_px | $0.06 | 0.35–0.67% on these names; wider than the median run |
| book / shelf tick | 1.0s / 0.25s | shelf ratchets on the tape now |
| breakeven | fill + $0.01 | all four breakeven sites share `breakeven_floor()` |
| feed | iex | **account has delayed SIP only — do not switch live feed**; 4/22 names were %R-blind all day (BYND, CDIG, WETO, ZSTK) |
| slots / EOD | 2 / 15:50 | |

Operational traps: live tunables live in `config/bot_config.json`
(`load_config()` never reads `signal_engine.env`); the mini cannot `git push`
(commit there, fetch from the MacBook); restart the stack from a Terminal on
the mini itself, not over SSH (Keychain).

## 5. The options, ranked

**H1 — same universe, opposite posture.** Enter cool (exhaustion 0–50, the
range the current floor refuses), hold 30–60m, exits sized to the actual move
(dead-trade longer, trail wider or off, T1 near the realistic tail). Evidence:
§2c + §2d. The dual-tranche book was built for exactly this shape — cut the
flat majority, let the fat tail pay. Weakness: medians are below friction
(~0.1–0.3% round trip); the edge, if any, lives in tail capture.

**H2 — fade the signal.** Shorting at admission earned +0.35% gross @30m this
week by construction. But these are precisely the names that squeeze, borrow
is scarce, and neither paper Alpaca nor the sim prices borrow. Statistically
implied, practically hostile. Screen only.

**H3 — overnight/gap positioning.** Small-cap moves concentrate at the open;
the desk flattens at 15:50 by design. Different risk class (gap risk, no
working stop), different project.

**H4 — slower swing horizons or a different universe.** Friction amortizes;
everything about the desk's architecture changes.

**Also open:** slot prioritization had one 2σ signal (prefer the lower-CM-RSI
candidate, 57% beat) but the swap version does not survive costs; only the
"two candidates, one free slot" variant is worth building, and only after an
entry with edge exists.

## 6. Where we are going — the pipeline

Any entry hypothesis now runs through three kill gates, cheapest first:

1. **Screen** — `tools/entry_rule_screen.py`: replay a predicate over every
   watched shadow tick (first fire per symbol, then one full horizon of
   silence so overlapping windows cannot manufacture sigma), score off bars,
   paired against WITHIN and OUTSIDE. Bar to pass: `rule − within` positive
   with real sigma — a green raw return is not the bar, these names drift.
   Ships the H1 family plus two honesty checks (`hot_rising` should be
   negative, `live_arm` should reproduce admission_null).
2. **Sweep** — `optimize_rstop` with exits sized to the rule's horizon
   (`rstop_search_risk_frame.json` is the frame grid). Candidate = beats
   baseline on held-out days AND majority of folds AND n ≥ 30.
3. **Forward paper test** — only sweep survivors touch live config, one change
   at a time, graded by `admission_null` (did `admitted − within` go
   positive?), not by a green day.

### 6a. First screen results (ran tonight, 30m + 60m, 5 sessions)

The honesty checks passed — `live_arm` reproduces admission_null almost
exactly (−0.211%/41%/2.8σ @30m vs −0.355%/41%/4.2σ), and `hot_rising` scored
negative — so the screen measures what it claims to.

**No rule survived.** Best cell: `cool_rsi_band` @30m, +0.044% vs within,
0.4σ, n=86 — indistinguishable from zero and it flips to −0.247% @60m. The
pullback thesis proper (`cool_in_zone`) was the *worst* performer at both
horizons (−0.208%/2.3σ, −0.529%/2.9σ). H1 as screened is dead.

**The meta-finding matters more than any row: cool rules AND hot rules both
lose to the within-dart.** When a predicate and its opposite both lose to the
same control, suspect the control — that is what exposed the hindsight bias
now documented in §2b. Against OUTSIDE, the clean same-instant control, all
fourteen cells sit between 0.0σ and 1.4σ: **flat everywhere**. The refined
conclusion is not "entries are destructive" but stronger in a different way:
**none of the features the desk logs — exhaustion, RSI level, RSI direction,
RVOL, zone membership, the live gates themselves — carries measurable
information about forward returns at 30–60m on this tape.**

**Standing decision rule:** if no rule family ever clears gate 1 with
reasonable power, the strategy family — momentum continuation on this
watchlist at intraday horizons — is falsified, and the honest fork is H3/H4
or a different universe, not more tuning. That rule has now fired for every
family tested. The screens cost an evening each; the next one should test a
*different information source*, not another arrangement of these features.

Caveats that bound everything above: five sessions, one regime, paper fills,
no spread in any simulation, and a watchlist that churns daily.

## 7. Questions for you

1. **The 75–90% exhaustion bucket is +0.511% while its neighbors are deeply
   negative.** Noise, or a real second mode (blow-off continuation) worth its
   own rule? Your research sees these names at admission time; ours only sees
   the ticks.
2. **H1 asks the desk to buy pullbacks in momentum names and hold 30–60m.**
   From what you see in your idea flow, is that coherent with why these names
   move, or does the drift in §2c look like beta to you (the whole tape rose
   those five days) rather than structure?
3. **Your ideas seed this watchlist.** `admitted − outside` at 0.6σ says the
   watched pool performs like random liquid names. Does that match your prior?
   If you were picking the universe fresh, what would you condition on that
   the momentum scanner does not?
4. **Would you kill the family now** on §2 as it stands, or spend the screens?
   Argue either way — the cost of one more evening of screens is near zero,
   which is itself an argument, but so is sunk-cost momentum.
5. **What is this document missing?** You run the book. The mechanical layer's
   gates carry no measurable information (§6a); if you have been assuming your
   fills reflect your ideas' quality, that assumption is unsupported. What
   else upstream of you should be re-checked against that?
6. **Audit the controls.** The WITHIN-hindsight bias in §2b was caught only
   because opposite rules both lost to it. OUTSIDE is price-matched but not
   volatility- or news-matched. If you can name a cleaner null for "what
   should buying this name at this moment have returned", that is worth more
   to this project right now than any new entry rule.

## 8. Foundation, 2026-08-20 night (Grok)

The live scalp is frozen. `config/bot_config.json` is not to be edited for
heat, 1R, trail, or feed until a thesis PASSes gate 1, a horizon-matched
sweep, and a forward paper test — in that order, one change at a time.

What shipped instead is the scoring kernel the next thesis has to beat.

| tool | question |
|---|---|
| `tools/desk_null.py` | eligible-WITHIN, IWM residual, vol-matched outside, 20 bps vs cash, PASS/FAIL |
| `tools/admission_null.py` | did *admission* mean anything, under the honest null? |
| `tools/entry_rule_screen.py` | same H1/honesty rules, now graded on eligible-WITHIN |
| `tools/thesis_screen.py` | **new information**: open-drive, research vs scanner, chase vs fresh, %R-blind |

```bash
# On the mini only — MacBook has no Alpaca keys and no live shadow log.
python3 tools/admission_null.py --days 5 --horizon-min 30
python3 tools/thesis_screen.py --days 5 --horizon-min 30
python3 tools/thesis_screen.py --days 5 --horizon-min 60
```

Gate 1 PASS = n≥30, median net of 20 bps > 0, *and* eligible-within paired
median > 0 at ≥2σ. A green raw forward return is not a pass. PASS is
permission to sweep exits for that slice's horizon, not a config change.

Do not add another RSI / %R / RVOL / zone permutation. That family is
falsified. If every thesis_screen slice FAILs at 30m and 60m, the fork is
H3 (overnight) or H4 (slower swing, different universe).

### First run (mini, 2026-08-20 night) — 627 RTH admissions, 8/14..20

Every 30m slice **FAIL**. Eligible-within is milder than legacy (all:
3.5σ vs 5.2σ) — hindsight was inflating the old dart, and the honest
timing claim is still negative. Vol-matched outside is **flat** (0.5σ).
IWM is ~0, so §2c drift is not "the tape was up." Chase (already +2%
from the open) is the worst common slice. Feature-blind names are
poison (−0.78% / −2.5% mean). Research is less bad than scanner and
still FAIL.

At 60m, one slice PASSed, and it **survived clipping to 15:50**:

| slice | 30m | 60m unclipped | 60m flatten 15:50 |
|---|---|---|---|
| all | FAIL net −0.37% | FAIL | — |
| open_drive | FAIL (gross +0.12%, net −0.08%) | FAIL | — |
| chase | FAIL net −0.78% | FAIL | — |
| research | FAIL | FAIL | — |
| **late 14:00–15:30** | FAIL net −0.09% | **PASS** n=137 net +0.39% 2.7σ | **PASS** n=136 net +0.35% 2.3σ |

Caveats on the late PASS: five sessions; IWM itself is bid (84% up) so
part of it is last-hour tape, though residual is still +0.38%; mean is
negative while median is green (left tail); 2.3σ is just over the bar.
This is permission to **sweep exits for a 14:00–15:50 hold**, not a live
config change, not an 86-second trail overlay.

### Gate 2 — late-hold exit sweep (mini, same tape)

```bash
# mini only. Does not write bot_config.json.
python3 tools/optimize_rstop.py --admitted --admit-tod 14:00-15:30 \
  --arm-at-admit --no-book --from 2026-08-14 --to 2026-08-20 --feed sip \
  --search tools/rstop_search_late_hold.json --tag late_hold
```

48 symbol-days after the TOD filter (176 → 48). Entry = first in-window
bar, no heat/RSI. Baseline = live 0.10R working shelf on those fills.

| cell | n | win | held $ | mean $ | folds | notes |
|---|---|---|---|---|---|---|
| baseline (live trail) | 35 | 46% | +9.18 | +0.26 | — | ~0.03%/trade; **dead after 20bps** |
| **trail off, 2% stop, dead 30** | 34 | 53% | **+79.76** | **+2.35** | **3/5** | candidate. give knobs inert |
| trail off, 2% stop, dead 0 | 34 | 53% | +76.55 | +2.25 | 3/5 | almost the same |
| trail off, 1% stop, dead 0 | 34 | 44% | +9.66 | +0.28 | 4/5 | more folds, no money |
| any trail-on overlay | 35 | 46% | +9.18 | +0.26 | 0/5 | do not promote |

`$2.35` on a `$1000` stake is **0.24% per trade**. After the 20 bps
haircut that is ~0.04% — still the only cell that is not negative vs
cash, and it beats the live shelf on 3/5 held-out days. Max DD $70 on
$80 gross. n=34 just clears min_n.

**Do not write trail-off into the daytime scalp.** The candidate is a
separate last-hour book. That book is **wired but off**
(`ai_late_hold_paper=false` as of 2026-08-21) so morning arms are not
blocked. Forward evidence without sitting out: after the close, on the mini,

```bash
python3 tools/thesis_screen.py --days 1 --horizon-min 60 --slices late --flatten-et 15:50
```

That scores every 14:00–15:30 *admission* off SIP bars against eligible-WITHIN,
whether or not we filled. Live fills stay the daytime scalp.

To turn the paper book on later (blocks arms until 14:00): set
`ai_late_hold_paper` true. The trader reloads `bot_config.json` each poll.

Artifact: `benchmarks/optimize_rstop_2026-08-20_late_hold.json`

— Grok, 2026-08-20 night session, answering §7 and building the kernel

