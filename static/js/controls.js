/**
 * controls.js — User action handlers
 *
 * Single responsibility: execute user-triggered actions.
 * No DOM queries beyond the specific button passed in.
 * Returns promises so callers can chain or ignore.
 */

import { api } from './api.js';
import { get } from './store.js';

export async function toggleTranscriber(btnEl) {
  const running = get('transcriber').running;

  if (btnEl) {
    btnEl.disabled    = true;
    btnEl.textContent = running ? 'Stopping…' : 'Starting…';
  }

  try {
    await (running ? api.stopTx() : api.startTx());
  } catch (e) {
    console.error('[controls] toggleTranscriber', e);
  }
  // State update arrives via WebSocket — btn re-enables via store subscriber
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
  }

  if (btnEl) {
    setTimeout(() => {
      btnEl.textContent = '↺ Scan Now';
      btnEl.disabled    = false;
    }, 5000);
  }
}
