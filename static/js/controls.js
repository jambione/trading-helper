/**
 * controls.js — User action handlers
 *
 * Single responsibility: execute user-triggered actions.
 * Buttons are always re-enabled in finally blocks regardless of outcome.
 */

import { api } from './api.js?v=69';
import { get, selectTicker } from './store.js?v=69';
import { clearCopiedTickers } from './tickers.js?v=69';

export async function clearWatchlist() {
  if (!confirm('Clear the watchlist?\n\nThis removes all tickers from wb_watchlist.json.')) return;
  try {
    await api.clearWatchlist();
    clearCopiedTickers();
  } catch (e) {
    console.error('[controls] clearWatchlist', e);
  }
}

function parseTickers(text) {
  const upper = text.toUpperCase();
  const seen = new Set();
  const tickers = [];
  for (const m of upper.matchAll(/\b[A-Z]{2,5}\b/g)) {
    if (!seen.has(m[0])) { seen.add(m[0]); tickers.push(m[0]); }
  }
  return tickers;
}

export async function addTicker(inputEl, btnEl) {
  const raw = (inputEl?.value ?? '').trim();
  if (!raw) return;

  const tickers = parseTickers(raw);
  if (tickers.length === 0) return;

  if (btnEl) btnEl.disabled = true;
  const origLabel = btnEl?.textContent ?? '+ Add';

  try {
    if (tickers.length === 1) {
      await api.addTicker(tickers[0]);
      selectTicker(tickers[0]);
    } else {
      if (btnEl) btnEl.textContent = 'Adding…';
      await api.addBulk(tickers);
      if (btnEl) {
        btnEl.textContent = `Added ${tickers.length}`;
        setTimeout(() => { if (btnEl) btnEl.textContent = origLabel; }, 2000);
      }
    }
    if (inputEl) inputEl.value = '';
  } catch (e) {
    console.error('[controls] addTicker', e);
    if (btnEl) btnEl.textContent = origLabel;
  } finally {
    if (btnEl) btnEl.disabled = false;
    if (inputEl) inputEl.focus();
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
