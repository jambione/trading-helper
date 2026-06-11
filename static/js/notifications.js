/**
 * notifications.js — BUY signal alerts
 *
 * Single responsibility: watch for status transitions to BUY and alert the user
 * via browser Notification API and a short Web Audio beep.
 * Also auto-selects the ticker in TradingView on first BUY transition.
 *
 * Auto-Add: when the #auto-add-checkbox toggle is enabled (user=jmb only),
 * fires a POST to the local Windows agent (port 8889) to add the ticker to
 * Webull and TradingView on every mention_burst or BUY alert.
 */

import { subscribe, selectTicker, get } from './store.js?v=51';
import { api } from './api.js?v=51';

// Start enabled if the browser already granted permission in a prior session
let _enabled = (typeof Notification !== 'undefined' && Notification.permission === 'granted');

const _prevStatuses = {};   // ticker → last known status
const _prevBursts   = {};   // ticker → last known mention_burst bool

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
 * Fire-and-forget call to the local Windows agent for both Webull and TradingView.
 * Port 8889, same as the existing _addToWBAndTV helper in tickers.js.
 * Silently skips if the agent is not running.
 */
async function _agentAdd(ticker) {
  const _post = async (path) => {
    try {
      const resp = await fetch(`http://127.0.0.1:8889${path}`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ ticker }),
        signal:  AbortSignal.timeout(5000),
      });
      if (!resp.ok) console.warn(`[notifications] agent ${path} returned`, resp.status);
    } catch {
      // Agent not running — skip silently
    }
  };
  await _post('/add-wb');
  await _post('/add-tv');
}

export function init() {
  subscribe('tickers', _check);
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
    const prevStatus = _prevStatuses[row.ticker];
    const prevBurst  = _prevBursts[row.ticker];

    // BUY signal alert — only on genuine transition
    if (row.status === 'BUY' && prevStatus !== undefined && prevStatus !== 'BUY') {
      _beep('buy');
      _notify(row, 'buy');
      if (!currentSelected) selectTicker(row.ticker);
      if (_autoAddEnabled()) _agentAdd(row.ticker);
    }

    // Mention burst alert — only on rising edge (false → true)
    if (row.mention_burst && prevBurst === false) {
      _beep('burst');
      if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
      _notifyBurst(row);
      if (_autoAddEnabled())  _agentAdd(row.ticker);
      if (_autoAlertEnabled()) _agentAlert(row.ticker);
    }

    _prevStatuses[row.ticker] = row.status;
    _prevBursts[row.ticker]   = row.mention_burst ?? false;
  }
}

function _notify(row, type = 'buy') {
  if (!_enabled) return;
  try {
    const n = new Notification(`BUY  ${row.ticker}`, {
      body: [
        row.price    != null ? `$${row.price.toFixed(2)}`       : '',
        row.rte_fast != null ? `%R ${row.rte_fast.toFixed(0)}`  : '',
        row.cm_rsi   != null ? `RSI ${row.cm_rsi.toFixed(0)}`   : '',
        row.streak   >= 1    ? `streak ${row.streak}`           : '',
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
