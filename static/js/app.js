/**
 * app.js — Application bootstrap & module orchestration
 *
 * Single responsibility: wire up modules, route events between api↔store↔UI.
 * No rendering logic lives here — that belongs in the component modules.
 */

import { connect, on }                           from './api.js?v=16';
import { subscribe, set }                        from './store.js?v=16';
import { init as initTranscription }             from './transcription.js?v=16';
import { init as initTickers }                   from './tickers.js?v=16';
import { init as initTradingView }               from './tradingview.js?v=16';
import { init as initConfig, open as openConfig }from './config.js?v=16';
import { init as initResizer }                   from './resizer.js?v=16';
import * as controls                             from './controls.js?v=16';
import * as notifications                        from './notifications.js?v=16';
import { isAuthenticated, logout, getBackendUrl } from './auth.js?v=16';

document.addEventListener('DOMContentLoaded', async () => {

  // ── Auth gate ────────────────────────────────────────────────
  // Ask the backend whether auth is required before enforcing a login redirect.
  // On localhost the backend always runs locally — never require auth if the
  // hostname is localhost/127.0.0.1 regardless of fetch outcome.
  const _isLocal  = ['localhost', '127.0.0.1', ''].includes(window.location.hostname);
  const _isMobile = document.body.classList.contains('mobile');
  let authRequired = false;
  try {
    const res  = await fetch(getBackendUrl() + '/api/meta');
    const meta = await res.json();
    authRequired = _isLocal ? false : (meta.auth_required ?? false);
  } catch {
    // Backend unreachable — localhost is always open; remote requires a token
    authRequired = _isLocal ? false : !isAuthenticated();
  }

  if (authRequired && !isAuthenticated()) {
    window.location.href = '/login';
    return;
  }

  // Hosted mode: transcript + settings are localhost-only features.
  // Use _isLocal directly so this works regardless of auth settings.
  if (!_isLocal) document.body.classList.add('hosted');

  // ── Initialize UI components ─────────────────────────────────
  // Wrapped individually so one failure doesn't block the rest.
  if (_isLocal) {
    try { initTranscription(document.querySelector('[data-panel="transcript"]')); } catch (e) { console.error('[app] initTranscription', e); }
  }
  try { initTickers(document.querySelector('[data-panel="tickers"]')); }          catch (e) { console.error('[app] initTickers', e); }
  if (!_isMobile) {
    try { initTradingView(document.querySelector('[data-panel="tradingview"]')); } catch (e) { console.error('[app] initTradingView', e); }
  }
  try { initConfig(document.querySelector('[data-drawer="config"]')); }           catch (e) { console.error('[app] initConfig', e); }
  try { initResizer(document.querySelector('.main-grid'), document.getElementById('ticker-tv-resizer'), { hosted: !_isLocal }); } catch (e) { console.error('[app] initResizer', e); }
  notifications.init();

  // ── Wire button actions ──────────────────────────────────────
  const txBtn     = document.querySelector('[data-tx-btn]');
  const clrWlBtn  = document.querySelector('[data-clear-watchlist-btn]');
  const clrTxBtn  = document.querySelector('[data-clear-transcript-btn]');
  const settBtn   = document.querySelector('[data-settings-btn]');
  const notifBtn  = document.querySelector('[data-notif-btn]');
  const logoutBtn = document.querySelector('[data-logout-btn]');

  txBtn    ?.addEventListener('click', () => controls.toggleTranscriber(txBtn));
  clrWlBtn ?.addEventListener('click', () => controls.clearWatchlist());
  clrTxBtn ?.addEventListener('click', () => controls.clearTranscript());
  settBtn  ?.addEventListener('click', openConfig);
  logoutBtn?.addEventListener('click', logout);

  const addInput = document.getElementById('add-ticker-input');
  const addBtn   = document.querySelector('[data-add-ticker-btn]');
  addBtn?.addEventListener('click', () => controls.addTicker(addInput, addBtn));
  addInput?.addEventListener('keydown', e => {
    if (e.key === 'Enter') controls.addTicker(addInput, addBtn);
  });
  addInput?.addEventListener('input', e => {
    const pos = e.target.selectionStart;
    e.target.value = e.target.value.toUpperCase();
    e.target.setSelectionRange(pos, pos);
  });

  notifBtn?.addEventListener('click', async () => {
    const granted = await notifications.requestPermission();
    _syncNotifBtn(notifBtn, granted);
  });

  // Reflect already-granted state from a previous session
  _syncNotifBtn(notifBtn, notifications.isEnabled());

  // ── WebSocket → store (snapshot ingest) ──────────────────────
  on('message', snap => {
    const update = {};
    if (snap.tickers      !== undefined) update.tickers      = snap.tickers;
    if (snap.config)                     update.config       = snap.config;
    if (snap.transcriber)                update.transcriber  = snap.transcriber;
    if (Object.keys(update).length)      set(update);
  });

  on('connected', connected => set({ connected }));

  // ── Store → connection indicators (all [data-ws-dot] elements) ──
  subscribe('connected', connected => {
    document.querySelectorAll('[data-ws-dot]').forEach(dot => {
      dot.className = `ws-dot ${connected ? 'ws-dot--on' : 'ws-dot--off'}`;
      dot.title     = connected ? 'Live' : 'Disconnected — reconnecting…';
    });
  });

  // ── Store → transcription controls ──────────────────────────
  subscribe('transcriber', tx => {
    const dot   = document.querySelector('[data-tx-dot]');
    const lbl   = document.querySelector('[data-tx-label]');
    const count = document.querySelector('[data-tx-count]');

    if (dot) dot.className = `tx-dot${tx.running ? ' tx-dot--on' : ''}`;
    if (lbl) {
      lbl.textContent = tx.running ? 'LISTENING' : 'STOPPED';
      lbl.className   = `tx-label${tx.running ? ' tx-label--on' : ''}`;
    }

    if (txBtn && !txBtn.disabled) {
      txBtn.textContent = tx.running ? 'Stop Transcription' : 'Start Transcription';
      txBtn.className   = `tx-btn ${tx.running ? 'tx-btn--stop' : 'tx-btn--start'}`;
    }

    if (count) {
      const n = tx.count ?? 0;
      count.textContent = `${n} ticker${n !== 1 ? 's' : ''} captured today`;
    }

    const audioStatus = document.querySelector('[data-audio-status]');
    if (audioStatus) audioStatus.textContent = tx.running ? 'Listening' : 'Stopped';
  });

  // ── Store → status bar ───────────────────────────────────────
  subscribe('tickers', rows => {
    const el = document.querySelector('[data-statusbar-tickers]');
    if (el) el.textContent = rows.length || '—';
  });

  // ── Persist selected ticker across reloads ───────────────────
  const _savedTicker = localStorage.getItem('ss:selected-ticker');
  if (_savedTicker) set({ selectedTicker: _savedTicker });

  subscribe('selectedTicker', ticker => {
    if (ticker) localStorage.setItem('ss:selected-ticker', ticker);
    else        localStorage.removeItem('ss:selected-ticker');
  });

  // ── Boot ─────────────────────────────────────────────────────
  connect();
});

// ── Helpers ───────────────────────────────────────────────────

function _syncNotifBtn(btn, granted) {
  if (!btn) return;
  if (granted) {
    btn.textContent = '🔔 Alerts On';
    btn.classList.add('btn--alert-on');
    btn.title = 'BUY signal alerts enabled';
  } else {
    btn.textContent = '🔔 Alerts';
    btn.classList.remove('btn--alert-on');
    btn.title = 'Click to enable BUY signal alerts';
  }
}
