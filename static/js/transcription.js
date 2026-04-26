/**
 * transcription.js — Live transcript panel component
 *
 * Single responsibility: render transcript lines with recognized-ticker highlighting.
 * Subscribes to `tickers` (for the known-symbol set) and `transcriber` (for lines).
 */

import { subscribe } from './store.js';

let _box = null;                  // scrollable transcript container
let _known = new Set();           // current watchlist symbols for highlighting
let _lastLineCount = 0;           // track rendered lines for incremental updates

export function init(panelEl) {
  _box = panelEl.querySelector('[data-transcript-box]');

  // Keep known-ticker set in sync so highlighting reflects the live watchlist
  subscribe('tickers', rows => {
    _known = new Set(rows.map(r => r.ticker));
  });

  subscribe('transcriber', tx => _render(tx.lines ?? []));
}

// ── Rendering ─────────────────────────────────────────────────

function _render(lines) {
  if (!_box) return;

  if (!lines.length) {
    if (_lastLineCount !== 0) {
      _box.innerHTML = '<span class="tx-placeholder">Waiting for transcription…</span>';
      _lastLineCount = 0;
    }
    return;
  }

  const atBottom = _box.scrollHeight - _box.scrollTop - _box.clientHeight < 60;

  if (_lastLineCount === 0 || lines.length < _lastLineCount) {
    // Full re-render on first render or after a clear
    _box.innerHTML = lines.map(_renderLine).join('');
  } else if (lines.length > _lastLineCount) {
    // Append only the new lines — no full DOM replace
    _box.insertAdjacentHTML('beforeend',
      lines.slice(_lastLineCount).map(_renderLine).join(''));
  }

  _lastLineCount = lines.length;
  if (atBottom) _box.scrollTop = _box.scrollHeight;
}

function _renderLine(line) {
  // Error / stack trace
  if (line.includes('Traceback') || line.includes('Error:') || line.startsWith('  File')) {
    return `<div class="tx-error">${_esc(line)}</div>`;
  }

  // [LOG] ticker-extraction events
  if (line.includes('[LOG]')) {
    return `<div class="tx-log">${_highlight(_esc(line))}</div>`;
  }

  // Standard timestamped line: [HH:MM:SS] text…
  const m = line.match(/^\[(\d{2}:\d{2}:\d{2})\] ([\s\S]*)$/);
  if (m) {
    const ts   = `<span class="tx-ts">${_esc(m[1])}</span>`;
    const body = _highlight(_esc(m[2]));
    return `<div class="tx-line">${ts} ${body}</div>`;
  }

  // Status / startup line (no timestamp)
  return `<div class="tx-status">${_esc(line)}</div>`;
}

/**
 * Bold any 2-5 uppercase-letter token that is in the live watchlist.
 * Runs on already-escaped HTML so it only matches text content.
 */
function _highlight(html) {
  return html.replace(/\b([A-Z]{2,5})\b/g, m =>
    _known.has(m)
      ? `<strong class="tx-ticker">${m}</strong>`
      : m
  );
}

function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
