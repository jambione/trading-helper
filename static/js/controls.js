/**
 * controls.js — User action handlers
 *
 * Single responsibility: execute user-triggered actions.
 * Buttons are always re-enabled in finally blocks regardless of outcome.
 */

import { api } from './api.js';
import { get } from './store.js';

export async function toggleTranscriber(btnEl) {
  const running = get('transcriber').running;

  if (btnEl) {
    btnEl.disabled    = true;
    btnEl.textContent = running ? 'Stopping…' : 'Starting…';
  }

  let result = null;
  try {
    result = await (running ? api.stopTx() : api.startTx());
  } catch (e) {
    console.error('[controls] toggleTranscriber', e);
  } finally {
    if (btnEl) {
      // Use the API response state when available; fall back to current store state.
      // The WS subscriber will correct any mismatch within 1 second.
      const actual = result?.running ?? get('transcriber').running;
      btnEl.disabled    = false;
      btnEl.textContent = actual ? 'Stop Transcription' : 'Start Transcription';
      btnEl.className   = `tx-btn ${actual ? 'tx-btn--stop' : 'tx-btn--start'}`;
    }
  }
}

export async function clearWatchlist() {
  if (!confirm('Clear the watchlist?\n\nThis removes all tickers from wb_watchlist.json.')) return;
  try {
    await api.clearWatchlist();
  } catch (e) {
    console.error('[controls] clearWatchlist', e);
  }
}

export async function clearTranscript() {
  try {
    await api.clearTranscript();
  } catch (e) {
    console.error('[controls] clearTranscript', e);
  }
}

export async function triggerScan(btnEl) {
  if (btnEl) {
    btnEl.disabled    = true;
    btnEl.textContent = '◉ Scanning…';
  }

  try {
    await api.triggerScan();
  } catch (e) {
    console.error('[controls] triggerScan', e);
  } finally {
    // Re-enable immediately — the scan_running store state drives the pill indicator.
    // The app.js scan_running subscriber will also re-enable if still disabled when scan ends.
    if (btnEl) btnEl.disabled = false;
  }
}
