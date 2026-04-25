/**
 * app.js — Application bootstrap & module orchestration
 *
 * Single responsibility: wire up modules, route events between api↔store↔UI.
 * No rendering logic lives here — that belongs in the component modules.
 */

import { connect, on }                  from './api.js';
import { subscribe, set }               from './store.js';
import { init as initTranscription }    from './transcription.js';
import { init as initTickers }          from './tickers.js';
import { init as initTradingView }      from './tradingview.js';
import { init as initConfig, open as openConfig } from './config.js';
import * as controls                    from './controls.js';

document.addEventListener('DOMContentLoaded', () => {

  // ── Initialize UI components ─────────────────────────────────
  initTranscription(document.querySelector('[data-panel="transcript"]'));
  initTickers(document.querySelector('[data-panel="tickers"]'));
  initTradingView(document.querySelector('[data-panel="tradingview"]'));
  initConfig(document.querySelector('[data-drawer="config"]'));

  // ── Wire button actions ──────────────────────────────────────
  const txBtn      = document.querySelector('[data-tx-btn]');
  const scanBtn    = document.querySelector('[data-scan-btn]');
  const clrWlBtn   = document.querySelector('[data-clear-watchlist-btn]');
  const clrTxBtn   = document.querySelector('[data-clear-transcript-btn]');
  const settBtn    = document.querySelector('[data-settings-btn]');

  txBtn    ?.addEventListener('click', () => controls.toggleTranscriber(txBtn));
  scanBtn  ?.addEventListener('click', () => controls.triggerScan(scanBtn));
  clrWlBtn ?.addEventListener('click', () => controls.clearWatchlist());
  clrTxBtn ?.addEventListener('click', () => controls.clearTranscript());
  settBtn  ?.addEventListener('click', openConfig);

  // ── WebSocket → store (snapshot ingest) ──────────────────────
  on('message', snap => {
    const update = {};
    if (snap.tickers      !== undefined) update.tickers      = snap.tickers;
    if (snap.scan_running !== undefined) update.scan_running = snap.scan_running;
    if (snap.scan_ts)                    update.scan_ts      = snap.scan_ts;
    if (snap.config)                     update.config       = snap.config;
    if (snap.transcriber)                update.transcriber  = snap.transcriber;
    if (Object.keys(update).length)      set(update);
  });

  on('connected', connected => set({ connected }));

  // ── Store → header status indicators ────────────────────────
  const wsDot     = document.querySelector('[data-ws-dot]');
  const scanPill  = document.querySelector('[data-scan-pill]');

  subscribe('connected', connected => {
    if (!wsDot) return;
    wsDot.className = `ws-dot ${connected ? 'ws-dot--on' : 'ws-dot--off'}`;
    wsDot.title     = connected ? 'Live' : 'Disconnected — reconnecting…';
  });

  subscribe('scan_running', running => {
    if (!scanPill || !running) return;
    scanPill.textContent = '◉ Scanning';
    scanPill.className   = 'scan-pill scan-pill--scanning';
  });

  subscribe('scan_ts', ts => {
    if (!scanPill || !ts) return;
    scanPill.textContent = `Last scan ${ts}`;
    scanPill.className   = 'scan-pill';
  });

  // ── Store → transcription controls (btn / dot / count) ──────
  subscribe('transcriber', tx => {
    // Dot & label in header of transcript panel
    const dot   = document.querySelector('[data-tx-dot]');
    const lbl   = document.querySelector('[data-tx-label]');
    const count = document.querySelector('[data-tx-count]');

    if (dot) dot.className = `tx-dot${tx.running ? ' tx-dot--on' : ''}`;
    if (lbl) {
      lbl.textContent = tx.running ? 'LISTENING' : 'STOPPED';
      lbl.className   = `tx-label${tx.running ? ' tx-label--on' : ''}`;
    }

    // Re-enable & relabel the tx button (if not mid-click disabled)
    if (txBtn && !txBtn._busy) {
      txBtn.textContent = tx.running ? 'Stop Transcription' : 'Start Transcription';
      txBtn.className   = `tx-btn ${tx.running ? 'tx-btn--stop' : 'tx-btn--start'}`;
      txBtn.disabled    = false;
    }

    if (count) {
      const n = tx.count ?? 0;
      count.textContent = `${n} ticker${n !== 1 ? 's' : ''} captured today`;
    }

    // Status bar
    const audioStatus = document.querySelector('[data-audio-status]');
    if (audioStatus) audioStatus.textContent = tx.running ? 'Listening' : 'Stopped';
  });

  // ── Status bar scan timestamp ────────────────────────────────
  subscribe('scan_ts', ts => {
    const el = document.querySelector('[data-statusbar-scan]');
    if (el && ts) el.textContent = ts;
  });

  subscribe('tickers', rows => {
    const el = document.querySelector('[data-statusbar-tickers]');
    if (el) el.textContent = rows.length || '—';
  });

  // ── Boot ─────────────────────────────────────────────────────
  connect();
});
