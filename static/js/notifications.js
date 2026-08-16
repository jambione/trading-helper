/**
 * notifications.js — desk event → local agent bus
 *
 * Events:
 *   burst     — mention_burst rising edge
 *   buy_zone  — signal_proximity status → buy_zone
 *   ax        — AI suggestion newly marked AX (A+X agreement)
 *
 * Each event can be: off | toast | auto (localStorage + Auto-Add toggle).
 * Toast click and auto both hit POST http://127.0.0.1:8889/v1/action
 * (action=load_tv|focus depending on event).
 */

import { subscribe, selectTicker, get } from './store.js?v=130';
import { api } from './api.js?v=130';

// Start enabled if the browser already granted permission in a prior session
let _enabled = (typeof Notification !== 'undefined' && Notification.permission === 'granted');

// ── In-page toast system ───────────────────────────────────────

let _toastContainer = null;

function _getContainer() {
  if (!_toastContainer) {
    _toastContainer = document.getElementById('toast-container');
    if (!_toastContainer) {
      _toastContainer = document.createElement('div');
      _toastContainer.id = 'toast-container';
      document.body.appendChild(_toastContainer);
    }
  }
  return _toastContainer;
}

/**
 * Show an in-page toast.
 * @param {string} title
 * @param {string} sub
 * @param {'buy'|'burst'|'sell'|'info'|'ax'} type
 * @param {number} duration  - auto-dismiss ms
 * @param {Function|null} onClickFn  - called when the toast is clicked
 */
export function showToast(title, sub = '', type = 'info', duration = 6000, onClickFn = null) {
  const container = _getContainer();
  const icons = { buy: '▲', burst: '🔥', sell: '▼', info: 'ℹ', ax: '✦' };
  const el = document.createElement('div');
  el.className = `toast toast--${type === 'ax' ? 'info' : type}`;
  el.style.position = 'relative';
  el.innerHTML = `
    <span class="toast-icon">${icons[type] ?? 'ℹ'}</span>
    <span class="toast-body">
      <span class="toast-title">${title}</span>
      ${sub ? `<span class="toast-sub">${sub}</span>` : ''}
    </span>
    <div class="toast-progress" style="width:100%"></div>
  `;

  container.appendChild(el);

  const bar = el.querySelector('.toast-progress');
  requestAnimationFrame(() => {
    bar.style.transitionDuration = `${duration}ms`;
    bar.style.width = '0%';
  });

  const dismiss = () => {
    el.classList.add('toast--dismissing');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  };

  const timer = setTimeout(dismiss, duration);
  el.addEventListener('click', () => {
    clearTimeout(timer);
    dismiss();
    if (onClickFn) onClickFn();
  });
}


const _prevStatuses = {};   // ticker → last known status
const _prevBursts   = {};   // ticker → last known mention_burst bool
const _prevAx       = new Set();  // symbols currently AX
let _axPrimed       = false;
const _seenSpikes   = new Set();  // dedupe price-spike toasts within session
let _spikesPrimed   = false;

// ── Event mode config ──────────────────────────────────────────
// Per-event: off | toast | auto
// Auto-Add checkbox forces auto for burst + buy_zone (legacy).
const _AUTO_ADD_KEY = 'ss:auto-add';
const _EVENT_KEYS = {
  burst:    'ss:event-burst',
  buy_zone: 'ss:event-buy-zone',
  ax:       'ss:event-ax',
};
const _EVENT_ACTIONS = {
  burst:    'load_tv',
  buy_zone: 'focus',
  ax:       'focus',
};

let _autoAddEl = null;

const _AUTO_ALERT_KEY = 'ss:auto-alert';
let _autoAlertEl = null;

function _readEventMode(event) {
  const key = _EVENT_KEYS[event];
  const raw = key ? localStorage.getItem(key) : null;
  if (raw === 'off' || raw === 'toast' || raw === 'auto') return raw;
  // Defaults
  if (event === 'ax') return 'toast';
  return 'toast';
}

/** Effective mode after applying Auto-Add override. */
function _eventMode(event) {
  const base = _readEventMode(event);
  if (_autoAddEnabled() && (event === 'burst' || event === 'buy_zone')) {
    return 'auto';
  }
  return base;
}

function _shouldToast(mode) {
  return mode === 'toast' || mode === 'auto';
}

function _shouldAuto(mode) {
  return mode === 'auto';
}

function _initAutoAdd() {
  const label = document.getElementById('auto-add-checkbox')?.closest('.auto-add-toggle');
  _autoAddEl  = document.getElementById('auto-add-checkbox');
  if (!_autoAddEl) return;

  const saved = localStorage.getItem(_AUTO_ADD_KEY) === 'true';
  _autoAddEl.checked = saved;
  if (label) label.classList.toggle('is-on', saved);

  _autoAddEl.addEventListener('change', () => {
    const on = _autoAddEl.checked;
    localStorage.setItem(_AUTO_ADD_KEY, String(on));
    if (label) label.classList.toggle('is-on', on);
  });
}

function _autoAddEnabled() {
  return _autoAddEl?.checked === true;
}

function _initAutoAlert() {
  const label = document.getElementById('auto-alert-checkbox')?.closest('.auto-alert-toggle');
  _autoAlertEl = document.getElementById('auto-alert-checkbox');
  if (!_autoAlertEl) return;
  const saved = localStorage.getItem(_AUTO_ALERT_KEY) === 'true';
  _autoAlertEl.checked = saved;
  if (label) label.classList.toggle('is-on', saved);
  _autoAlertEl.addEventListener('change', () => {
    const on = _autoAlertEl.checked;
    localStorage.setItem(_AUTO_ALERT_KEY, String(on));
    if (label) label.classList.toggle('is-on', on);
  });
}

function _autoAlertEnabled() {
  return _autoAlertEl?.checked === true;
}

async function _agentAlert(ticker) {
  try {
    await api.createTVAlert(ticker);
  } catch {
    // Server-side automation unavailable — skip silently
  }
}

/**
 * POST /v1/action on the local desk agent. Falls back to legacy /add.
 */
async function _agentAction(action, symbol, source, meta = {}) {
  const body = {
    action: action || 'load_tv',
    symbol,
    source: source || 'dashboard',
    meta,
  };
  try {
    let resp = await fetch('http://127.0.0.1:8889/v1/action', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
      signal:  AbortSignal.timeout(5000),
    });
    if (resp.status === 404) {
      resp = await fetch('http://127.0.0.1:8889/add', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          ticker: symbol,
          mode: 'tv',
          source,
        }),
        signal:  AbortSignal.timeout(5000),
      });
    }
    if (!resp.ok) console.warn(`[notifications] agent action returned`, resp.status);
  } catch {
    // Agent not running — skip silently
  }
}

/** Convenience: fire the configured bus action for an event. */
function _agentForEvent(event, symbol, meta = {}) {
  const action = _EVENT_ACTIONS[event] || 'load_tv';
  return _agentAction(action, symbol, event, { event, ...meta });
}

/**
 * Apply toast + optional auto for one desk event.
 */
function _handleEvent(event, {
  symbol,
  title,
  sub = '',
  toastType = 'info',
  duration = 6000,
  meta = {},
  select = false,
  beepType = null,
}) {
  const mode = _eventMode(event);
  if (mode === 'off') return;

  const fire = () => _agentForEvent(event, symbol, meta);

  if (_shouldToast(mode)) {
    if (beepType) _beep(beepType);
    showToast(title, sub, toastType, duration, fire);
  }
  if (_shouldAuto(mode)) {
    fire();
  }
  if (select) selectTicker(symbol);
}

function _spikeKey(r) {
  const type = String(r.alert_type || 'spike').replace(/[^a-z0-9]/gi, '').toLowerCase();
  const tier = String(r.scanner_tier || '').toUpperCase();
  return `sc|${r.ticker}|${type}|${tier}`.toLowerCase();
}

function _checkPriceSpikes(rows) {
  if (!_spikesPrimed) {
    rows.forEach(r => _seenSpikes.add(_spikeKey(r)));
    _spikesPrimed = true;
    return;
  }
  for (const r of rows) {
    const key = _spikeKey(r);
    if (_seenSpikes.has(key)) continue;
    _seenSpikes.add(key);

    const parts = [];
    if (r.price != null) parts.push(`$${Number(r.price).toFixed(2)}`);
    if (r.float_size != null) {
      const n = Number(r.float_size);
      parts.push(n >= 1e6 ? `${(n / 1e6).toFixed(2)}M float` : `${n} float`);
    }
    if (r.scanner_tier) parts.push(r.scanner_tier);
    const sub = parts.length ? parts.join('  ·  ') : (r.alert_type || 'tap to view');

    // Spikes use burst routing (same urgency band)
    _handleEvent('burst', {
      symbol: r.ticker,
      title: `⚡ ${r.ticker}  Price Spike`,
      sub,
      toastType: 'burst',
      duration: 8000,
      meta: { kind: 'price_spike', tier: r.scanner_tier },
      select: true,
      beepType: 'burst',
    });
  }
}

function _isAxRow(r) {
  if (!r) return false;
  if (r.agreement === true) return true;
  const mark = String(r.source_mark || '').toUpperCase();
  if (mark === 'AX') return true;
  const src = String(r.source || '').toLowerCase();
  return src === 'both' || src === 'ax';
}

function _checkAiSuggestions(payload) {
  const rows = (payload && payload.rows) || [];
  const cur = new Set();
  const bySym = {};
  for (const r of rows) {
    if (!_isAxRow(r)) continue;
    const sym = String(r.symbol || r.ticker || '').toUpperCase();
    if (!sym) continue;
    cur.add(sym);
    bySym[sym] = r;
  }

  if (!_axPrimed) {
    cur.forEach(s => _prevAx.add(s));
    _axPrimed = true;
    return;
  }

  for (const sym of cur) {
    if (_prevAx.has(sym)) continue;
    const r = bySym[sym] || {};
    const score = r.trending_score ?? r.score;
    const reason = String(r.reason || '').slice(0, 40);
    const bits = ['A+X agree'];
    if (score != null) bits.push(`score ${score}`);
    if (reason) bits.push(reason);

    _handleEvent('ax', {
      symbol: sym,
      title: `✦ AX  ${sym}`,
      sub: bits.join('  ·  ') + '  ·  tap to focus',
      toastType: 'ax',
      duration: 8000,
      meta: { score, reason },
      select: true,
      beepType: 'ax',
    });
  }

  _prevAx.clear();
  cur.forEach(s => _prevAx.add(s));
}

export function init() {
  subscribe('tickers', _check);
  subscribe('price_spikes', _checkPriceSpikes);
  subscribe('ai_suggestions', _checkAiSuggestions);
  const _onReady = () => { _initAutoAdd(); _initAutoAlert(); };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _onReady);
  } else {
    _onReady();
  }
}

/** Request browser notification permission. Returns true if granted. */
export async function requestPermission() {
  if (typeof Notification === 'undefined') return false;
  if (Notification.permission === 'granted') { _enabled = true; _setupPushSubscription(); return true; }
  const result = await Notification.requestPermission();
  _enabled = result === 'granted';
  if (_enabled) _setupPushSubscription();
  return _enabled;
}

export function isEnabled() { return _enabled; }

// Expose for config UI / console debugging
export function getEventModes() {
  return {
    burst: _eventMode('burst'),
    buy_zone: _eventMode('buy_zone'),
    ax: _eventMode('ax'),
    autoAdd: _autoAddEnabled(),
  };
}

export function setEventMode(event, mode) {
  const key = _EVENT_KEYS[event];
  if (!key) return;
  if (!['off', 'toast', 'auto'].includes(mode)) return;
  localStorage.setItem(key, mode);
}

// ── Web Push subscription ─────────────────────────────────────

function _urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const b64     = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw     = atob(b64);
  return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
}

async function _setupPushSubscription() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;
  try {
    const reg  = await navigator.serviceWorker.ready;
    const resp = await fetch('/api/push/vapid-public-key');
    if (!resp.ok) return;
    const { key } = await resp.json();
    if (!key) return;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly:      true,
      applicationServerKey: _urlBase64ToUint8Array(key),
    });
    await fetch('/api/push/subscribe', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(sub),
    });
  } catch (e) {
    console.warn('[notifications] push subscription failed', e);
  }
}

// ── Internal ──────────────────────────────────────────────────

function _check(rows) {
  const currentSelected = get('selectedTicker');

  for (const row of rows) {
    const sp        = row.signal_proximity || {};
    const sigStatus = sp.status ?? null;
    const prevStatus = _prevStatuses[row.ticker];
    const prevBurst  = _prevBursts[row.ticker];

    // BUY zone — genuine transition only
    if (sigStatus === 'buy_zone' && prevStatus !== undefined && prevStatus !== 'buy_zone') {
      _notify(row, 'buy');
      _handleEvent('buy_zone', {
        symbol: row.ticker,
        title: `Momentum BUY  ${row.ticker}`,
        sub: [
          row.price != null ? `$${row.price.toFixed(2)}`    : '',
          sp.cm_rsi != null ? `RSI ${sp.cm_rsi.toFixed(0)}` : '',
          sp.pctr   != null ? `%R ${sp.pctr.toFixed(0)}`    : '',
          'tap to focus',
        ].filter(Boolean).join('  ·  '),
        toastType: 'buy',
        duration: 6000,
        meta: {
          price: row.price,
          proximity_pct: sp.proximity_pct,
          cm_rsi: sp.cm_rsi,
          pctr: sp.pctr,
        },
        select: !currentSelected,
        beepType: 'buy',
      });
    }

    // Mention burst — rising edge (prevBurst !== true covers first + false→true)
    if (row.mention_burst && prevBurst !== true) {
      if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
      _notifyBurst(row);
      _handleEvent('burst', {
        symbol: row.ticker,
        title: `🔥 ${row.ticker}  ${row.mention_window ?? ''}x mentions`,
        sub: row.price != null ? `$${row.price.toFixed(2)}  ·  tap to load` : 'tap to load',
        toastType: 'burst',
        duration: 8000,
        meta: { mention_window: row.mention_window, price: row.price },
        beepType: 'burst',
      });
      if (_autoAlertEnabled()) _agentAlert(row.ticker);
    }

    _prevStatuses[row.ticker] = sigStatus;
    _prevBursts[row.ticker]   = row.mention_burst ?? false;
  }
}

function _notify(row, type = 'buy') {
  if (!_enabled) return;
  try {
    const sp = row.signal_proximity || {};
    const n = new Notification(`Momentum BUY  ${row.ticker}`, {
      body: [
        row.price  != null ? `$${row.price.toFixed(2)}`      : '',
        sp.pctr    != null ? `%R ${sp.pctr.toFixed(0)}`      : '',
        sp.cm_rsi  != null ? `RSI ${sp.cm_rsi.toFixed(0)}`   : '',
      ].filter(Boolean).join('  ·  '),
      tag:               `buy-${row.ticker}`,
      requireInteraction: false,
    });
    n.onclick = () => {
      window.focus();
      selectTicker(row.ticker);
      _agentForEvent('buy_zone', row.ticker, {});
    };
  } catch (e) {
    console.warn('[notifications] notify failed', e);
  }
}

function _notifyBurst(row) {
  if (!_enabled) return;
  try {
    const count = row.mention_window ?? 0;
    const n = new Notification(`🔥 ${row.ticker}  ${count}x mentions`, {
      body: row.price != null
        ? `$${row.price.toFixed(2)}  ·  ${count} mentions in the last few seconds`
        : `${count} rapid mentions detected`,
      tag:               `burst-${row.ticker}`,
      requireInteraction: false,
    });
    n.onclick = () => {
      window.focus();
      selectTicker(row.ticker);
      _agentForEvent('burst', row.ticker, { mention_window: count });
    };
  } catch (e) {
    console.warn('[notifications] burst notify failed', e);
  }
}

function _beep(type = 'buy') {
  try {
    const ctx  = new (window.AudioContext || window.webkitAudioContext)();
    const osc  = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = 'sine';
    osc.addEventListener('ended', () => ctx.close());

    if (type === 'burst') {
      const t = ctx.currentTime;
      osc.frequency.setValueAtTime(880, t);
      gain.gain.setValueAtTime(0.0,  t);
      gain.gain.linearRampToValueAtTime(0.3, t + 0.04);
      gain.gain.linearRampToValueAtTime(0.0, t + 0.10);
      gain.gain.linearRampToValueAtTime(0.3, t + 0.16);
      gain.gain.linearRampToValueAtTime(0.0, t + 0.22);
      gain.gain.linearRampToValueAtTime(0.3, t + 0.28);
      gain.gain.linearRampToValueAtTime(0.0, t + 0.38);
      osc.start(t);
      osc.stop(t + 0.38);
    } else if (type === 'ax') {
      // Soft two-tone chord-ish: 520 → 780 (distinct from buy)
      const t = ctx.currentTime;
      osc.frequency.setValueAtTime(520, t);
      osc.frequency.setValueAtTime(780, t + 0.15);
      gain.gain.setValueAtTime(0.22, t);
      gain.gain.exponentialRampToValueAtTime(0.001, t + 0.4);
      osc.start(t);
      osc.stop(t + 0.4);
    } else {
      osc.frequency.setValueAtTime(660, ctx.currentTime);
      osc.frequency.setValueAtTime(990, ctx.currentTime + 0.12);
      gain.gain.setValueAtTime(0.25,    ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + 0.35);
    }
  } catch { /* AudioContext unavailable */ }
}
