# AI suggestions rename checklist

**Status (2026-08-03):** Phase 1 modules done. Phase 2 runtime paths:
`ai_reports/` preferred (legacy `claude_reports/` fallback),
`logs/ai_trader.log`, `init_for_ai` (+ `init_for_claude` alias),
`./trading status` shows `ai_trader`. Per-source wire files
(`claude_suggestions.json`, `grok_suggestions.json`) kept on purpose.
Historical report filenames under either report dir are left as archive.

**When (original):** After Grok is a live research source…

## Measured Grok defaults (2026-08-02 A/B)

Config stubs already exist (`grok_*` in `config.py` / `bot_config.json`). Until the
publisher is wired, leave `grok_research_enabled` / `grok_trading_enabled` **false**.

| Key | Intended production value | Why |
|-----|---------------------------|-----|
| `grok_backend` | `cli` | xAI **subscription** via `grok login`, not `XAI_API_KEY` |
| `grok_model` | `grok-4.5` | CLI default |
| `grok_max_turns` | **4** | t4 ≥ t8 quality, ~20% fewer impact tokens (n=1 + confirm) |
| `grok_live_search` | `true` | research quality |
| `grok_trading_enabled` | `false` | research-only until multi-day review |
| `grok_prompt_file` | `ai_prompt.txt` | shared; JSON-first rule enforced in prompt |

Metrics: `tools/research_ab.py --backend cli` → `benchmarks/research_ab/`.

**Why:** Today “Claude” names the whole AI-suggestions system (`claude_trader.py`, `claude_suggestions.json`, `claude_*` config). With two sources, that name only fits the Anthropic backend.

**Criteria for “done”:** Fresh checkout + docs still make sense; no remaining implication that all AI research *is* Claude; provider-specific symbols still say Claude/Grok clearly.

---

## Naming rules

| Concept | Pattern | Examples |
|---------|---------|----------|
| Shared pipeline / product | `ai_*` or `research_*` | `ai_trader.py`, `ai_positions.py` |
| Anthropic-only | `claude_*` | `claude_model`, `call_claude_cli` |
| xAI / Grok-only | `grok_*` | `grok_model`, `call_grok_cli`, `grok_suggestions.json` |
| One trading book | `ai_positions*`, `ai_trading*` | Single Alpaca risk path — never two traders |

Avoid renaming mid-feature. Prefer **additive Grok names first**, then this pass.

---

## 1. Files (git mv)

| Today | After |
|-------|--------|
| `claude_suggest.py` | `ai_suggest.py` (shared parse / enrich / schedule helpers) |
| `claude_trader.py` | `ai_trader.py` (orchestrates sources; owns trading loop) |
| `claude_positions.py` | `ai_positions.py` |
| `claude_trading.py` | `ai_trading.py` |
| `ai_prompt.txt` | `ai_prompt.txt` (shared), or keep per-source prompt files |
| `claude_suggestions.json` | keep as Claude wire file; add `grok_suggestions.json` |
| `claude_positions_state.json` | `ai_positions_state.json` |
| `claude_reports/` | `ai_reports/` (optional subdirs `claude/`, `grok/`) |
| `logs/claude.log` | `logs/ai_trader.log` |
| `tests/test_ai_suggest.py` | `tests/test_ai_suggest.py` |
| `tests/test_ai_positions.py` | `tests/test_ai_positions.py` |
| `tests/test_ai_trading.py` | `tests/test_ai_trading.py` |
| `_manual_research_run.py` | keep name; add `--source claude\|grok\|all` |

Provider-only helpers stay named for the provider (`call_claude_cli`, `call_grok_cli`).

---

## 2. Config keys

### Shared → `ai_*`

| Today | After |
|-------|--------|
| `claude_trader_enabled` | `ai_trader_enabled` |
| `claude_trading_enabled` | `ai_trading_enabled` |
| `claude_max_price` | `ai_max_price` |
| `claude_quote_poll` / `claude_volume_poll` | `ai_quote_poll` / `ai_volume_poll` |
| `claude_avg_days` / `claude_rvol_time_adjusted` | `ai_*` |
| `claude_trade_amount` / `claude_max_positions` / buy-sell caps | `ai_*` |
| `claude_risk_pct` / `claude_trade_style` / `claude_min_reward_risk` | `ai_*` |
| `claude_positions_poll_sec` | `ai_positions_poll_sec` |

### Split research flags (do not collapse to one)

| Today | After |
|-------|--------|
| `claude_research_enabled` | keep for Claude; add `grok_research_enabled` |
| (n/a) | `grok_backend`, `grok_model`, `grok_cli_bin`, `grok_research_times`, … |

### Claude-only (keep)

`claude_backend`, `claude_cli_bin`, `claude_model`, `claude_effort`, `claude_max_turns`, `claude_search_tools` (`web` | `web_x` | `none`), `claude_live_search`, `claude_use_prior_context`, `claude_save_reports`, `claude_request_timeout`, `claude_prompt_file` (or point at `ai_prompt.txt`).

Grok mirror: `grok_search_tools` (same modes; attaches `x_search` when `grok_backend=api`).

Desk snapshot inject (RS leaders + Stocktwits heat + peer AI board): `claude_use_desk_snapshot` / `grok_use_desk_snapshot` (default true; shared override `ai_use_desk_snapshot`).

### Desk display (`momentum_config.json`)

| Today | After |
|-------|--------|
| `claude_enabled` | `claude_panel_enabled` (+ `grok_panel_enabled`) |
| `claude_panel_limit` / `claude_max_price` (display) | keep Claude-scoped **or** share `ai_panel_*` filters |
| LOOK / RVOL display knobs for Claude panel | mirror for Grok or share `ai_*` display defaults |

### Compatibility (one release)

In `config.py` / `load_config()`:

- Read new keys first; fall back to old `claude_*` names if missing.
- Log a one-line deprecation when an old key is used.
- Remove aliases only after desk + always-on Mac configs are updated.

---

## 3. Code symbols

| Today | After |
|-------|--------|
| `AiSuggestions` | `AiSuggestions` or `SuggestionFeed` |
| `RemoteAiSuggestions` | `RemoteSuggestionFeed` (state key + title params) |
| `claude_panel(...)` | `suggestion_panel(title=..., feed=...)` |
| `load_claude_suggestions` | `load_claude_suggestions` + `load_grok_suggestions`, or `load_suggestions(source)` |
| `CLAUDE_SUGGESTIONS_FILE` | source-specific constants |
| `CLAUDE_POSITIONS_FILE` | `AI_POSITIONS_FILE` (+ legacy path fallback) |

---

## 4. API / wire format

| Today | After |
|-------|--------|
| `GET /api/claude` | Prefer `/api/suggestions/claude`; keep old route as alias one release |
| `GET /api/claude/positions` | `/api/ai/positions` (alias old) |
| `/api/state` → `claude_suggestions` | keep flat keys: `claude_suggestions` + `grok_suggestions` (least monitor churn) |
| `/api/state` → `claude_positions` | `ai_positions` (alias `claude_positions` one release) |

**Recommendation:** flat state keys (`claude_suggestions`, `grok_suggestions`) over nested `suggestions.claude` unless the monitor is rewritten in the same PR.

---

## 5. Launchers and ops

- [ ] `trading` — process name / log file `ai_trader` (or keep starting `ai_trader.py`)
- [ ] `start_all.py` — same
- [ ] `scripts/startup.command` / morning scripts if they hardcode `claude_trader.py`
- [ ] PID / pgrep patterns in `trading status|stop`
- [ ] Memory notes under `.claude/projects/.../memory` that say “Claude desk” meaning the whole system — update wording when convenient

---

## 6. Runtime files on disk

After rename, support **legacy paths** for one session cycle:

```text
ai_positions_state.json     ← preferred
claude_positions_state.json ← read if new missing; optional write-through migrate

claude_suggestions.json     ← Claude source (unchanged name is OK)
grok_suggestions.json       ← Grok source

ai_reports/                 ← preferred
claude_reports/             ← read/write fallback until migrated
```

Do not delete old JSON mid-session while `ai_trader` might still be on an old build.

---

## 7. PR / test checklist

- [ ] Grok source already green: file → dashboard → desk panel display-only
- [ ] `git mv` modules; fix imports in one commit (or stacked: mv → wire → drop aliases)
- [ ] Config aliases for old `claude_*` shared keys
- [ ] Dashboard route aliases
- [ ] Monitor: panels still work; no trading keys reappear in `momentum_config.json`
- [ ] `pytest` — renamed tests + full suite
- [ ] Manual: `./trading restart`, one research run per source, positions panel if trading on
- [ ] Update `README.md` / `docs/ONBOARDING.md` strings that say “Claude trader” for the whole desk
- [ ] Follow-up PR: remove aliases after both machines use new names

---

## 8. Out of scope for the rename PR

- Changing research prompts or schedule quality
- Enabling Grok trading
- Merging Claude+Grok into one ranked list (optional later)
- Renaming historical report filenames already under `claude_reports/` (leave as archive)

---

## Quick inventory (pre-rename snapshot)

Python / config entry points known to carry `claude_` for the **system** (not just the backend):

- `claude_suggest.py`, `claude_trader.py`, `claude_positions.py`, `claude_trading.py`
- `dashboard.py` loaders + `/api/claude*`
- `momentum-monitor/remote_feeds.py`, `momentum_signal.py`, `desk_hotkeys.py`
- `config.py`, `config/bot_config.json`, `momentum-monitor/momentum_config.json`
- `trading`, `start_all.py`, `_manual_research_run.py`
- `tests/test_claude_*.py`

Provider-specific CLI/API paths (`call_claude_cli`, `claude_backend=claude_cli`) **stay** Claude-named after the rename.
