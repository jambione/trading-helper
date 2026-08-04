# Agent command bus

Local desk automation lives on **http://127.0.0.1:8889** (`mac_agent` / `windows_agent`).

The product insight: **webhooks are the bridge** from site/monitor intelligence to hands-free desk action. This bus is that bridge, made first-class.

## Contract

### `POST /v1/action`

```json
{
  "action": "load_tv",
  "symbol": "SOFI",
  "source": "toast",
  "meta": { "reason": "mention burst" }
}
```

| Field | Required | Notes |
|-------|----------|--------|
| `action` | yes | Verb name (see below) |
| `symbol` / `ticker` | most verbs | US equity ticker, uppercased |
| `source` | no | Who fired it: `toast`, `dashboard`, `monitor`, `burst`, `ai`, `manual`, … |
| `meta` | no | Free-form object (reason, score, levels, …) |

Aliases: `ticker` ≡ `symbol`. Unknown fields ignored.

### `GET /v1/actions`

Lists verbs the running agent understands (for clients and debugging).

### `GET /health`

Existing health JSON plus `actions` (verb names) and `bus: "v1"`.

### Legacy (still supported)

| Path | Maps to |
|------|---------|
| `GET/POST /add?ticker=&mode=` | `add` / `add_tv` / `add_wb` |
| `POST /add-tv` | `add_tv` |
| `POST /add-wb` | `add_wb` (no-op / retired Webull) |

New clients should use **`/v1/action` only**.

---

## Verbs (v1)

| Action | Symbol? | Behavior | Async? |
|--------|---------|----------|--------|
| **`load_tv`** | yes | Load chart in pinned TradingView tab | queued |
| **`add_tv`** | yes | Same as `load_tv` (name kept for toast/dashboard) | queued |
| **`add`** | yes | TV load (+ retired WB path if mode both) | queued |
| **`add_wb`** | yes | Retired — ok:false or no-op success w/ note | sync |
| **`focus`** | yes | Write `active_symbol.json` **and** queue `load_tv` | mixed |
| **`journal`** | yes | Append one JSONL line (why / source / meta) | sync |
| **`ping`** | no | Liveness | sync |

### Queued vs sync

UI automation (pyautogui) is **not thread-safe**. Verbs that touch the keyboard go through the existing agent work queue and return **202** with `"queued": true`.

`journal` and `ping` run inline and return **200**.

---

## Response shape

```json
{
  "ok": true,
  "action": "load_tv",
  "symbol": "SOFI",
  "source": "toast",
  "queued": true,
  "result": "queued",
  "message": "",
  "bus": "v1",
  "version": "1.3.0"
}
```

Errors: `ok: false`, HTTP 4xx, `error` string.

---

## Client migration

| Client | Today | Target |
|--------|--------|--------|
| Dashboard toast | `POST /add` | `POST /v1/action` `{action:"add_tv",…}` (or keep legacy) |
| `static/js` helpers | `/add` | same or bus |
| Monitor hotkeys | in-process `load_tv` | optional HTTP for remote desk |
| Future iPhone Shortcut / TV webhook | — | bus only |

Legacy paths remain until all call sites migrate.

---

## Event → bus map (v1)

The agent poller and the dashboard toast layer share three events:

| Event | Trigger | Default bus action | Env / localStorage |
|-------|---------|-------------------|--------------------|
| **burst** | `mention_burst` rising edge | `load_tv` | `EVENT_BURST` / `ss:event-burst` |
| **buy_zone** | `signal_proximity.status` → `buy_zone` | `focus` | `EVENT_BUY_ZONE` / `ss:event-buy-zone` |
| **ax** | AI row newly `agreement` / `source_mark=AX` | `focus` | `EVENT_AX` / `ss:event-ax` |

Modes: **`off`** | **`toast`** (notify, click fires bus) | **`auto`** (notify + queue bus immediately).

Legacy `AUTO_ADD=1` (agent) or dashboard Auto-Add checkbox upgrades **burst** + **buy_zone** to `auto`. **AX** stays toast unless set explicitly.

Agent example `.env`:

```
EVENT_BURST=toast
EVENT_BUY_ZONE=toast
EVENT_AX=toast
```

Dashboard console:

```js
// import path exposes helpers when modules are live
// setEventMode('ax', 'auto')
```

---

## Next verbs (not yet)

| Action | Purpose |
|--------|---------|
| `arm_entry` | Paper risk bracket from meta levels |
| `compare` | Load relative chart / second symbol |
| `layout` | Named window layout |
| `undo_last` | Best-effort reverse last queue |

Add only when a real daily friction needs them.

---

## Security

Bind remains local / LAN. No auth on 8889 by design (desk machine). Do **not** expose 8889 through Cloudflare tunnel.
