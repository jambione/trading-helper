/**
 * notifications.js — BUY signal alerts
 *
 * Alerts the user via in-page toasts, browser Notification API, and audio beep.
 * Also auto-selects the ticker in TradingView on first BUY transition.
 * Clicking a toast adds the ticker to TradingView via the local agent.
 *
 * Auto-Add: when the #auto-add-checkbox toggle is enabled (user=jmb only),
 * fires a POST to the local macOS agent (port 8889) to add the ticker to
 * TradingView on every mention_burst or BUY alert.
 */

import { subscribe, selectTicker, get } from './store.js?v=72';
import { api } from './api.js?v=72';

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
 * @param {'buy'|'burst'|'sell'|'info'} type
 * @param {number} duration  - auto-dismiss ms
 * @param {Function|null} onClickFn  - called when the toast is clicked
 */
export function showToast(title, sub = '', type = 'info', duration = 6000, onClickFn = null) {
  const container = _getContainer();
  const icons = { buy: '▲', burst: '🔥', sell: '▼', info: 'ℹ' };
  const el = document.createElement('div');
  el.className = `toast toast--${type}`;
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
const _seenSpikes   = new Set();  // dedupe price-spike toasts within session
let _spikesPrimed   = false;

// ── Auto-Add toggle state ──────────────────────────────────────
const _AUTO_ADD_KEY = 'ss:auto-add';
let _autoAddEl = null;   // checkbox input element

// ── Auto-Alert toggle state ────────────────────────────────────
const _AUTO_ALERT_KEY = 'ss:auto-alert';
let _autoAlertEl = null;

/** Read persisted toggle state and wire up change handler. */
function _initAutoAdd() {
  const label = document.getElementById('auto-add-checkbox')?.closest('.auto-add-toggle');
  _autoAddEl  = document.getElementById('auto-add-checkbox');
  if (!_autoAddEl) return;

  // Restore persisted state
  const saved = localStorage.getItem(_AUTO_ADD_KEY) === 'true';
  _autoAddEl.checked = saved;
  if (label) label.classList.toggle('is-on', saved);

  _autoAddEl.addEventListener('change', () => {
    const on = _autoAddEl.checked;
    localStorage.setItem(_AUTO_ADD_KEY, String(on));
    if (label) label.classList.toggle('is-on', on);
  });
}

/** Returns true when the Auto-Add toggle is switched on. */
function _autoAddEnabled() {
  return _autoAddEl?.checked === true;
}

/** Wire up the Auto-Alert toggle (create TradingView alert on burst). */
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

/** Returns true when the Auto-Alert toggle is switched on. */
function _autoAlertEnabled() {
  return _autoAlertEl?.checked === true;
}

/** Call the dashboard's create-tv-alert endpoint for a ticker. */
async function _agentAlert(ticker) {
  try {
    await api.createTVAlert(ticker);
  } catch {
    // Server-side automation unavailable — skip silently
  }
}

/**
 * Fire-and-forget call to the local Windows agent for both TradingView.
 * Port 8889, same as the existing _addToWBAndTV helper in tickers.js.
 * Silently skips if the agent is not running.
 */
async function _agentAdd(ticker) {
  try {
    const resp = await fetch('http://127.0.0.1:8889/add', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ ticker, mode: 'both' }),
      signal:  AbortSignal.timeout(5000),
    });
    if (!resp.ok) console.warn(`[notifications] agent /add returned`, resp.status);
  } catch {
    // Agent not running — skip silently
  }
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

    _beep('burst');
    showToast(
      `⚡ ${r.ticker}  Price Spike`,
      sub,
      'burst',
      8000,
      () => _agentAdd(r.ticker),
    );
    if (_autoAddEnabled()) _agentAdd(r.ticker);
    selectTicker(r.ticker);
  }
}

export function init() {
  subscribe('tickers', _check);
  subscribe('price_spikes', _checkPriceSpikes);
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

    // BUY signal alert — only on genuine transition to buy_zone
    if (sigStatus === 'buy_zone' && prevStatus !== undefined && prevStatus !== 'buy_zone') {
      _beep('buy');
      _notify(row, 'buy');
      showToast(
        `BUY  ${row.ticker}`,
        [
          row.price != null ? `$${row.price.toFixed(2)}`    : '',
          sp.cm_rsi != null ? `RSI ${sp.cm_rsi.toFixed(0)}` : '',
          sp.pctr   != null ? `%R ${sp.pctr.toFixed(0)}`    : '',
        ].filter(Boolean).join('  ·  '),
        'buy',
        6000,
        () => _agentAdd(row.ticker),
      );
      if (!currentSelected) selectTicker(row.ticker);
      if (_autoAddEnabled()) _agentAdd(row.ticker);
    }

    // Mention burst alert — on first detection and on rising edge
    if (row.mention_burst && prevBurst !== true) {
      _beep('burst');
      if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
      _notifyBurst(row);
      showToast(
        `🔥 ${row.ticker}  ${row.mention_window ?? ''}x mentions`,
        row.price != null ? `$${row.price.toFixed(2)}  ·  tap to add` : 'tap to add',
        'burst',
        8000,
        () => _agentAdd(row.ticker),
      );
      if (_autoAddEnabled())  _agentAdd(row.ticker);
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
    const n = new Notification(`BUY  ${row.ticker}`, {
      body: [
        row.price  != null ? `$${row.price.toFixed(2)}`      : '',
        sp.pctr    != null ? `%R ${sp.pctr.toFixed(0)}`      : '',
        sp.cm_rsi  != null ? `RSI ${sp.cm_rsi.toFixed(0)}`   : '',
      ].filter(Boolean).join('  ·  '),
      tag:               `buy-${row.ticker}`,
      requireInteraction: false,
    });
    n.onclick = () => { window.focus(); selectTicker(row.ticker); };
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
    n.onclick = () => { window.focus(); selectTicker(row.ticker); };
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
      // Three rapid high pulses — urgent but distinct from BUY
      const t = ctx.currentTime;
      osc.frequency.setValueAtTime(880, t);
      gain.gain.setValueAtTime(0.0,  t);
      // Pulse 1
      gain.gain.linearRampToValueAtTime(0.3, t + 0.04);
      gain.gain.linearRampToValueAtTime(0.0, t + 0.10);
      // Pulse 2
      gain.gain.linearRampToValueAtTime(0.3, t + 0.16);
      gain.gain.linearRampToValueAtTime(0.0, t + 0.22);
      // Pulse 3
      gain.gain.linearRampToValueAtTime(0.3, t + 0.28);
      gain.gain.linearRampToValueAtTime(0.0, t + 0.38);
      osc.start(t);
      osc.stop(t + 0.38);
    } else {
      // Two-tone rising: 660 Hz → 990 Hz  (BUY signal)
      osc.frequency.setValueAtTime(660, ctx.currentTime);
      osc.frequency.setValueAtTime(990, ctx.currentTime + 0.12);
      gain.gain.setValueAtTime(0.25,    ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
      osc.start(ctx.currentTime);
      osc.stop(ctx.currentTime + 0.35);
    }
  } catch { /* AudioContext unavailable */ }
}
