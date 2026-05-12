/**
 * notifications.js — BUY signal alerts
 *
 * Single responsibility: watch for status transitions to BUY and alert the user
 * via browser Notification API and a short Web Audio beep.
 * Also auto-selects the ticker in TradingView on first BUY transition.
 */

import { subscribe, selectTicker, get } from './store.js?v=28';

// Start enabled if the browser already granted permission in a prior session
let _enabled = (typeof Notification !== 'undefined' && Notification.permission === 'granted');

const _prevStatuses = {};   // ticker → last known status
const _prevBursts   = {};   // ticker → last known mention_burst bool

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
    const prevStatus = _prevStatuses[row.ticker];
    const prevBurst  = _prevBursts[row.ticker];

    // BUY signal alert — only on genuine transition
    if (row.status === 'BUY' && prevStatus !== undefined && prevStatus !== 'BUY') {
      _beep('buy');
      _notify(row, 'buy');
      if (!currentSelected) selectTicker(row.ticker);
    }

    // Mention burst alert — only on rising edge (false → true)
    if (row.mention_burst && prevBurst === false) {
      _beep('burst');
      _notifyBurst(row);
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
