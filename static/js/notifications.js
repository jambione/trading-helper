/**
 * notifications.js — BUY signal alerts
 *
 * Single responsibility: watch for status transitions to BUY and alert the user
 * via browser Notification API and a short Web Audio beep.
 * Also auto-selects the ticker in TradingView on first BUY transition.
 */

import { subscribe, selectTicker, get } from './store.js?v=8';

// Start enabled if the browser already granted permission in a prior session
let _enabled = (typeof Notification !== 'undefined' && Notification.permission === 'granted');

const _prevStatuses = {};  // ticker → last known status

export function init() {
  subscribe('tickers', _check);
}

/** Request browser notification permission. Returns true if granted. */
export async function requestPermission() {
  if (typeof Notification === 'undefined') return false;
  if (Notification.permission === 'granted') { _enabled = true; return true; }
  const result = await Notification.requestPermission();
  _enabled = result === 'granted';
  return _enabled;
}

export function isEnabled() { return _enabled; }

// ── Internal ──────────────────────────────────────────────────

function _check(rows) {
  const currentSelected = get('selectedTicker');

  for (const row of rows) {
    const prev = _prevStatuses[row.ticker];

    // Only alert on genuine BUY transition (not on initial page load)
    if (row.status === 'BUY' && prev !== undefined && prev !== 'BUY') {
      _beep();
      _notify(row);
      // Auto-load chart only when nothing is selected yet
      if (!currentSelected) selectTicker(row.ticker);
    }

    _prevStatuses[row.ticker] = row.status;
  }
}

function _notify(row) {
  if (!_enabled) return;
  try {
    const n = new Notification(`BUY  ${row.ticker}`, {
      body: [
        row.price   != null ? `$${row.price.toFixed(2)}`  : '',
        row.rte_fast != null ? `%R ${row.rte_fast.toFixed(0)}` : '',
        row.cm_rsi  != null ? `RSI ${row.cm_rsi.toFixed(0)}`  : '',
        row.streak  >= 1    ? `streak ${row.streak}`          : '',
      ].filter(Boolean).join('  ·  '),
      tag:               `buy-${row.ticker}`,   // replaces duplicate alerts
      requireInteraction: false,
    });
    n.onclick = () => { window.focus(); selectTicker(row.ticker); };
  } catch (e) {
    console.warn('[notifications] notify failed', e);
  }
}

function _beep() {
  try {
    const ctx  = new (window.AudioContext || window.webkitAudioContext)();
    const osc  = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = 'sine';
    // Two-tone up: 660 Hz → 990 Hz
    osc.frequency.setValueAtTime(660,  ctx.currentTime);
    osc.frequency.setValueAtTime(990,  ctx.currentTime + 0.12);
    gain.gain.setValueAtTime(0.25,     ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.35);
  } catch { /* AudioContext unavailable */ }
}
