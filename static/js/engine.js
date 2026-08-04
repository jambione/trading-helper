/**
 * engine.js — Trading Engine panel (owner-only, gated by body.user-jmb CSS)
 *
 * Three jobs:
 *   1. Live status chips from the store's `engine` slice (trader mode, kill
 *      switch, today's P&L, PDT counter, open positions) — fed by the WS
 *      snapshot, which carries signal_state.json's trader/risk blocks.
 *   2. Settings editor for whitelisted signal_engine.env keys
 *      (POST /api/engine/config) + engine restart (POST /api/engine/restart).
 *      Mutations need localhost or the engine control secret; on 403 we
 *      prompt once and cache the secret in localStorage.
 *   3. Daily performance report (GET /api/engine/report) — the
 *      "is it consistent enough to raise TRADE_AMOUNT?" view.
 */

import { subscribe } from './store.js?v=87';
import { api }       from './api.js?v=87';

const SECRET_LS = 'ss:engine-secret'; // pragma: allowlist secret (localStorage key name)

let $status, $body;
let _view = null;          // null | 'settings' | 'report'
let _eng  = null;          // last engine slice

// ── Settings form definition (whitelist mirrors engine_env.py) ───────────────

const FIELD_GROUPS = [
  { group: 'Mode', items: [
    { key: 'TRADER_MODE',     label: 'Trader mode',   type: 'select', options: ['off', 'paper'],
      hint: 'live is file-edit only, on purpose' },
    { key: 'STRATEGY_MODE',   label: 'Strategy',      type: 'select',
      options: ['three_indicator', 'momentum', 'alert'] },
    { key: 'REALTIME_BARS',   label: 'Realtime bars', type: 'select', options: ['1', '0'] },
    { key: 'COMPARE_TICKERS', label: 'Pinned compare tickers', type: 'text', placeholder: 'AAPL,SPY' },
  ]},
  { group: 'Scalp exits & sizing', items: [
    { key: 'TRADE_AMOUNT',       label: '$ per trade',      type: 'number' },
    { key: 'TAKE_PROFIT',        label: 'Take-profit %',    type: 'number' },
    { key: 'STOP_LOSS',          label: 'Stop-loss %',      type: 'number' },
    { key: 'MAX_HOLD_MINUTES',   label: 'Time stop (min)',  type: 'number' },
    { key: 'MAX_PRICE',          label: 'Max share price $', type: 'number' },
    { key: 'MAX_TOTAL_EXPOSURE', label: 'Max exposure $',   type: 'number' },
  ]},
  { group: 'Risk guard', items: [
    { key: 'DAILY_LOSS_LIMIT',   label: 'Daily loss limit $', type: 'number' },
    { key: 'MAX_TRADES_PER_DAY', label: 'Max trades/day',     type: 'number' },
    { key: 'PDT_PROTECT',        label: 'PDT protection',     type: 'select',
      options: ['warn', 'block', 'off'] },
  ]},
  { group: '3-indicator parameters', items: [
    { key: 'THREE_IND_CM_RSI_BUY_MAX',  label: 'CM RSI buy max',         type: 'number' },
    { key: 'THREE_IND_CM_RSI_SELL_MIN', label: 'CM RSI sell min',        type: 'number' },
    { key: 'THREE_IND_MACD_SEP_MULT',   label: 'MACD separation mult',   type: 'number' },
    { key: 'THREE_IND_CONFIRM_WINDOW',  label: 'Confirm window (bars)',  type: 'number' },
    { key: 'THREE_IND_TREND_LOOKBACK',  label: 'Trend lookback (bars)',  type: 'number' },
    { key: 'THREE_IND_EXIT_MODE',       label: 'Exit mode',              type: 'select',
      options: ['all', 'any'], hint: 'benchmarks: all beat any everywhere' },
    { key: 'THREE_IND_REQUIRE_HOT',     label: 'Require mention burst',  type: 'select',
      options: ['1', '0'], hint: 'catalyst gate — indicator-only entries lost on microcaps' },
    { key: 'THREE_IND_MIN_HOLD',        label: 'Min hold (sec)',         type: 'number' },
  ]},
];

// ── Secret handling for mutating calls ────────────────────────────────────────

function _secret() { return localStorage.getItem(SECRET_LS) || ''; }

async function _mutate(fn) {
  // Try with any cached secret; on 403 prompt once and retry.
  try {
    return await fn(_secret());
  } catch (e) {
    if (e && e.status === 403) {
      const s = prompt('Engine control secret (set engine_control_secret in bot_config.json):');
      if (!s) throw e;
      localStorage.setItem(SECRET_LS, s);
      return fn(s);
    }
    throw e;
  }
}

// ── Status chips ──────────────────────────────────────────────────────────────

function _chip(text, cls = '', title = '') {
  return `<span class="eng-chip ${cls}" title="${title}">${text}</span>`;
}

function _fmtMoney(v) {
  if (v == null) return '—';
  const sign = v < 0 ? '-' : '';
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}

function renderStatus(eng) {
  _eng = eng || _eng;
  if (!$status) return;
  const e = _eng || {};
  const trader = e.trader || {};
  const risk   = e.risk   || {};

  let fresh = false;
  if (e.updated) {
    const age = (Date.now() - Date.parse(e.updated)) / 1000;
    fresh = age >= 0 && age < 30;
  }

  const mode = (trader.mode || 'off').toUpperCase();
  const modeCls = mode === 'LIVE' ? 'eng-chip--live'
                : mode === 'PAPER' ? 'eng-chip--paper' : 'eng-chip--off';

  const chips = [];
  chips.push(_chip(fresh ? '● engine' : '○ engine off', fresh ? 'eng-chip--ok' : 'eng-chip--warn',
                   e.updated ? `last write ${e.updated}` : 'engine not writing state'));
  chips.push(_chip(mode, modeCls, `$${trader.trade_amount ?? '?'} per trade`));
  if (e.strategy) chips.push(_chip(e.strategy, '', 'STRATEGY_MODE'));

  if (risk.kill_switch) {
    chips.push(_chip('🛑 KILL SWITCH', 'eng-chip--live',
                     'Daily loss limit hit — no new buys until the next ET day'));
  }
  if (risk.realized_pnl_today != null) {
    const pnl = risk.realized_pnl_today;
    chips.push(_chip(`P&L ${_fmtMoney(pnl)}`,
                     pnl > 0 ? 'eng-chip--ok' : pnl < 0 ? 'eng-chip--warn' : '',
                     `today's realized P&L (limit ${_fmtMoney(risk.daily_loss_limit)})`));
  }
  if (risk.trades_today != null) {
    chips.push(_chip(`${risk.trades_today} trades`, '', 'round trips closed today'));
  }
  if (risk.day_trades_5d != null) {
    chips.push(_chip(`PDT ${risk.day_trades_5d}/3`,
                     risk.day_trades_5d >= 3 ? 'eng-chip--warn' : '',
                     `same-day round trips in 5 business days (${risk.pdt_protect})`));
  }
  if (risk.open_positions != null) {
    chips.push(_chip(`${risk.open_positions} open · $${risk.exposure ?? 0}`, '',
                     `exposure cap $${risk.max_total_exposure ?? '∞'}`));
  }

  $status.innerHTML = chips.join('');
}

// ── Settings view ─────────────────────────────────────────────────────────────

async function openSettings() {
  if (_view === 'settings') { _close(); return; }
  _view = 'settings';
  $body.classList.remove('hidden');
  $body.innerHTML = '<div class="eng-note">Loading settings…</div>';

  let values = {};
  try {
    const res = await api.getEngineConfig();
    values = res.values || {};
  } catch (e) {
    $body.innerHTML = `<div class="eng-note eng-note--err">Could not load settings: ${e.message}</div>`;
    return;
  }

  const groups = FIELD_GROUPS.map(g => {
    const rows = g.items.map(f => {
      const cur = values[f.key];
      let input;
      if (f.type === 'select') {
        const opts = f.options.map(o =>
          `<option value="${o}" ${String(cur) === o ? 'selected' : ''}>${o}</option>`).join('');
        const unset = cur == null || !f.options.includes(String(cur));
        input = `<select data-eng-key="${f.key}">
                   ${unset ? `<option value="" selected>(default)</option>` : ''}${opts}
                 </select>`;
      } else {
        input = `<input type="${f.type}" step="any" data-eng-key="${f.key}"
                        value="${cur ?? ''}" placeholder="${f.placeholder ?? '(default)'}" />`;
      }
      const hint = f.hint ? `<span class="eng-hint">${f.hint}</span>` : '';
      return `<label class="eng-field"><span class="eng-label">${f.label}</span>${input}${hint}</label>`;
    }).join('');
    return `<div class="eng-group"><div class="eng-group-title">${g.group}</div>${rows}</div>`;
  }).join('');

  $body.innerHTML = `
    <div class="eng-form">${groups}</div>
    <div class="eng-form-actions">
      <button data-eng-save class="btn btn--sm">💾 Save</button>
      <button data-eng-save-restart class="btn btn--sm">💾 Save + Restart Engine</button>
      <span data-eng-msg class="eng-note"></span>
    </div>`;

  const $msg = $body.querySelector('[data-eng-msg]');
  const collect = () => {
    const out = {};
    $body.querySelectorAll('[data-eng-key]').forEach(el => {
      const key = el.dataset.engKey;
      const val = el.value;
      const orig = values[key];
      if (val === '' && (orig == null || orig === '')) return;   // untouched default
      if (String(val) !== String(orig ?? '')) out[key] = val;
    });
    return out;
  };

  const save = async (restart) => {
    const updates = collect();
    if (!Object.keys(updates).length && !restart) {
      $msg.textContent = 'Nothing changed.';
      return;
    }
    $msg.textContent = 'Saving…';
    try {
      if (Object.keys(updates).length) {
        await _mutate(s => api.setEngineConfig(updates, s));
        Object.assign(values, updates);
      }
      if (restart) {
        await _mutate(s => api.restartEngine(s));
        $msg.textContent = '✓ Saved — engine restarting (watch the build badge)…';
      } else {
        $msg.textContent = '✓ Saved — restart the engine to apply.';
      }
    } catch (e) {
      $msg.textContent = `✗ ${e.body?.error || e.message}`;
    }
  };

  $body.querySelector('[data-eng-save]')        .addEventListener('click', () => save(false));
  $body.querySelector('[data-eng-save-restart]').addEventListener('click', () => save(true));
}

// ── Report view ───────────────────────────────────────────────────────────────

function _pct(v) {
  if (v == null) return '—';
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

async function openReport() {
  if (_view === 'report') { _close(); return; }
  _view = 'report';
  $body.classList.remove('hidden');
  $body.innerHTML = '<div class="eng-note">Building report…</div>';

  let res;
  try {
    res = await api.getEngineReport(14);
  } catch (e) {
    $body.innerHTML = `<div class="eng-note eng-note--err">Report failed: ${e.message}</div>`;
    return;
  }
  const r = res.report || {};
  const o = r.overall || {};
  if (!o.trades) {
    $body.innerHTML = `<div class="eng-note">No closed trades in the last ${res.days} days.
      Run the engine in paper mode to collect data.</div>`;
    return;
  }

  const days = Object.entries(r.by_day || {}).sort(([a], [b]) => a.localeCompare(b));
  const green = days.filter(([, s]) => (s.total_pnl_pct ?? 0) > 0).length;

  const dayRows = days.map(([day, s]) => `
    <tr class="${(s.total_pnl_pct ?? 0) > 0 ? 'eng-row--green' : 'eng-row--red'}">
      <td>${day}</td><td>${s.trades}</td><td>${s.win_rate}%</td>
      <td>${_pct(s.avg_pnl_pct)}</td><td>${_pct(s.total_pnl_pct)}</td>
      <td>${_fmtMoney(s.total_dollar)}</td>
    </tr>`).join('');

  const reasonRows = Object.entries(r.by_reason || {})
    .sort(([, a], [, b]) => (b.total_pnl_pct ?? 0) - (a.total_pnl_pct ?? 0))
    .map(([reason, s]) => `
      <tr><td>${reason}</td><td>${s.trades}</td><td>${s.win_rate}%</td>
      <td>${_pct(s.total_pnl_pct)}</td><td>${_fmtMoney(s.total_dollar)}</td></tr>`).join('');

  $body.innerHTML = `
    <div class="eng-report-summary">
      ${_chip(`${o.trades} trades`, '')}
      ${_chip(`win ${o.win_rate}%`, o.win_rate >= 50 ? 'eng-chip--ok' : 'eng-chip--warn')}
      ${_chip(`total ${_pct(o.total_pnl_pct)} (${_fmtMoney(o.total_dollar)})`,
              o.total_pnl_pct > 0 ? 'eng-chip--ok' : 'eng-chip--warn')}
      ${_chip(`PF ${o.profit_factor ?? '∞'}`, '')}
      ${_chip(`green days ${green}/${days.length}`,
              green === days.length ? 'eng-chip--ok' : '')}
      <span class="eng-note">last ${res.days} days @ $${res.size}/trade</span>
    </div>
    <div class="eng-tables">
      <table class="eng-table">
        <thead><tr><th>Day (ET)</th><th>Trades</th><th>Win</th><th>Avg</th><th>Total</th><th>$</th></tr></thead>
        <tbody>${dayRows}</tbody>
      </table>
      <table class="eng-table">
        <thead><tr><th>Exit reason</th><th>Trades</th><th>Win</th><th>Total</th><th>$</th></tr></thead>
        <tbody>${reasonRows}</tbody>
      </table>
    </div>`;
}

function _close() {
  _view = null;
  $body.classList.add('hidden');
  $body.innerHTML = '';
}

// ── Init ──────────────────────────────────────────────────────────────────────

export function init(root) {
  if (!root) return;
  $status = root.querySelector('[data-engine-status]');
  $body   = root.querySelector('[data-engine-body]');

  root.querySelector('[data-engine-settings-btn]')?.addEventListener('click', openSettings);
  root.querySelector('[data-engine-report-btn]')  ?.addEventListener('click', openReport);
  root.querySelector('[data-engine-restart-btn]') ?.addEventListener('click', async () => {
    if (!confirm('Restart the signal engine? Open positions are re-adopted on boot.')) return;
    try {
      await _mutate(s => api.restartEngine(s));
    } catch (e) {
      alert(`Restart failed: ${e.body?.error || e.message}`);
    }
  });

  subscribe('engine', renderStatus);
  renderStatus(null);
}
