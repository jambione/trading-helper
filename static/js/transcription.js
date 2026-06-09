/**
 * transcription.js — Discord Alerts panel component
 *
 * Single responsibility: render the live feed of Discord OCR alerts (the app's
 * ticker source) with watchlist-ticker highlighting. Subscribes to `tickers`
 * (for the known-symbol set) and `discord` (for the captured alert feed).
 */

import { subscribe } from './store.js?v=48';

let _box = null;                  // scrollable feed container
let _known = new Set();           // current watchlist symbols for highlighting
let _lastCount = 0;               // rendered alert count for incremental updates
let _lastKey = '';                // last alert's identity; detects feed shifts

export function init(panelEl) {
  _box = panelEl.querySelector('[data-transcript-box]');

  // Keep known-ticker set in sync so highlighting reflects the live watchlist
  subscribe('tickers', rows => {
    _known = new Set(rows.map(r => r.ticker));
  });

  subscribe('discord', d => _render(d.alerts ?? []));
}

// ── Rendering ─────────────────────────────────────────────────

function _render(alerts) {
  if (!_box) return;

  if (!alerts.length) {
    if (_lastCount !== 0) {
      _box.innerHTML = '<span class="tx-placeholder">Waiting for Discord alerts…</span>';
      _lastCount = 0;
      _lastKey = '';
    }
    return;
  }

  const tail    = alerts[alerts.length - 1];
  const tailKey = `${tail.ts}|${tail.ticker}|${tail.line}`;

  // No change at all — skip DOM work
  if (tailKey === _lastKey && alerts.length === _lastCount) return;

  const atBottom = _box.scrollHeight - _box.scrollTop - _box.clientHeight < 60;

  if (alerts.length > _lastCount && _lastCount > 0) {
    // Pure growth: append only the new alerts
    _box.insertAdjacentHTML('beforeend',
      alerts.slice(_lastCount).map(_renderAlert).join(''));
  } else {
    // First render or rolling-window shift — full re-render
    _box.innerHTML = alerts.map(_renderAlert).join('');
  }

  _lastCount = alerts.length;
  _lastKey = tailKey;
  if (atBottom) _box.scrollTop = _box.scrollHeight;
}

function _renderAlert(a) {
  const ts     = `<span class="tx-ts">${_esc(a.ts || '')}</span>`;
  const ticker = `<strong class="tx-ticker">${_esc(a.ticker || '')}</strong>`;
  const body   = _highlight(_esc(a.line || ''));
  return `<div class="tx-line">${ts} ${ticker} ${body}</div>`;
}

/** Bold any 2-5 uppercase-letter token that is in the live watchlist. */
function _highlight(html) {
  return html.replace(/\b([A-Z]{2,5})\b/g, m =>
    _known.has(m) ? `<strong class="tx-ticker">${m}</strong>` : m
  );
}

function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
