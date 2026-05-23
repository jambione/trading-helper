/**
 * app.js — Application bootstrap & module orchestration
 *
 * Single responsibility: wire up modules, route events between api↔store↔UI.
 * No rendering logic lives here — that belongs in the component modules.
 */

import { connect, on }                           from './api.js?v=6';
import { subscribe, set }                        from './store.js?v=6';
import { init as initTranscription }             from './transcription.js?v=6';
import { init as initTickers }                   from './tickers.js?v=6';
import { init as initScanner }                   from './scanner.js?v=6';
import { init as initConfig, open as openConfig }from './config.js?v=6';
import { init as initResizer }                   from './resizer.js?v=6';
import * as controls                             from './controls.js?v=6';
import * as notifications                        from './notifications.js?v=6';

document.addEventListener('DOMContentLoaded', () => {

  // ── Initialize UI components ─────────────────────────────────
  // Wrapped individually so one failure doesn't block the rest.
  try { initTranscription(document.querySelector('[data-panel="transcript"]')); } catch (e) { console.error('[app] initTranscription', e); }
  try { initTickers(document.querySelector('[data-panel="tickers"]')); }          catch (e) { console.error('[app] initTickers', e); }
  try { initScanner(document.querySelector('[data-panel="scanner"]')); }          catch (e) { console.error('[app] initScanner', e); }
  try { initConfig(document.querySelector('[data-drawer="config"]')); }           catch (e) { console.error('[app] initConfig', e); }
  try { initResizer(document.querySelector('.main-grid'), document.getElementById('ticker-tv-resizer')); } catch (e) { console.error('[app] initResizer', e); }
  notifications.init();

  // ── Wire button actions ──────────────────────────────────────
  const txBtn     = document.querySelector('[data-tx-btn]');
  const scanBtn   = document.querySelector('[data-scan-btn]');
  const clrWlBtn  = document.querySelector('[data-clear-watchlist-btn]');
  const clrTxBtn  = document.querySelector('[data-clear-transcript-btn]');
  const settBtn   = document.querySelector('[data-settings-btn]');
  const notifBtn  = document.querySelector('[data-notif-btn]');

  txBtn    ?.addEventListener('click', () => controls.toggleTranscriber(txBtn));
  scanBtn  ?.addEventListener('click', () => controls.triggerScan(scanBtn));
  clrWlBtn ?.addEventListener('click', () => controls.clearWatchlist());
  clrTxBtn ?.addEventListener('click', () => controls.clearTranscript());
  settBtn  ?.addEventListener('click', openConfig);

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
    if (snap.scan_running !== undefined) update.scan_running = snap.scan_running;
    if (snap.scan_ts)                    update.scan_ts      = snap.scan_ts;
    if (snap.config)                     update.config       = snap.config;
    if (snap.transcriber)                update.transcriber  = snap.transcriber;
    if (snap.scanner      !== undefined) update.scanners     = snap.scanner;
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

  // ── Store → scan pill + scan button ─────────────────────────
  const scanPill = document.querySelector('[data-scan-pill]');

  subscribe('scan_running', running => {
    // Pill and button are independent — guard them separately
    if (scanPill) {
      if (running) {
        scanPill.textContent = '◉ Scanning';
        scanPill.className   = 'scan-pill scan-pill--scanning';
      }
      // Pill text on completion is set by the scan_ts subscriber below
    }

    if (running) {
      if (scanBtn) scanBtn.disabled = true;
    } else {
      if (scanBtn) {
        scanBtn.textContent = '↺ Scan Now';
        scanBtn.disabled    = false;
      }
    }
  });

  subscribe('scan_ts', ts => {
    if (!ts) return;
    if (scanPill) {
      scanPill.textContent = `Last scan ${ts}`;
      scanPill.className   = 'scan-pill';
    }
    const el = document.querySelector('[data-statusbar-scan]');
    if (el) el.textContent = ts;
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

    // Only sync label when button is not mid-click (disabled = in-flight action)
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
