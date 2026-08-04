/**
 * priceSpikes.js — Scanner Price Spike strip (below leaderboard)
 *
 * Renders recent OCR-captured price-spike cards with price, float, and tier.
 * Click a chip to select that ticker in the watchlist / chart.
 */

import { subscribe, selectTicker } from './store.js?v=91';

let _barEl = null;
let _lastKey = '';

export function init(barEl) {
  _barEl = barEl;
  subscribe('price_spikes', rows => _render(rows ?? []));
}

function _render(rows) {
  if (!_barEl) return;

  if (!rows.length) {
    if (_lastKey !== '∅') {
      _barEl.innerHTML =
        '<span class="ps-empty">No scanner spikes yet</span>';
      _lastKey = '∅';
    }
    return;
  }

  const key = rows.map(r =>
    `${r.ts}|${r.ticker}|${r.price ?? ''}|${r.float_size ?? ''}`,
  ).join('||');
  if (key === _lastKey) return;
  _lastKey = key;

  _barEl.innerHTML = rows.slice().reverse().slice(0, 12).map(_renderChip).join('');
  _barEl.querySelectorAll('[data-ps-ticker]').forEach(el => {
    el.addEventListener('click', () => selectTicker(el.dataset.psTicker));
  });
}

function _renderChip(r) {
  const tier = r.scanner_tier
    ? `<span class="ps-tier">${_esc(r.scanner_tier)}</span>` : '';
  const price = r.price != null
    ? `<span class="ps-price">$${Number(r.price).toFixed(2)}</span>` : '';
  const flt = _fmtFloat(r.float_size);
  const floatChip = flt ? `<span class="ps-float">${flt}</span>` : '';
  const type = r.alert_type
    ? `<span class="ps-type">${_esc(r.alert_type)}</span>` : '';
  return `
    <button type="button" class="ps-item" data-ps-ticker="${_esc(r.ticker)}"
            title="${_esc(r.line || r.ticker)}">
      <span class="ps-ts">${_esc(r.ts || '')}</span>
      <span class="ps-ticker">${_esc(r.ticker)}</span>
      ${tier}${type}${price}${floatChip}
    </button>`;
}

function _fmtFloat(v) {
  if (v == null || Number.isNaN(Number(v))) return '';
  const n = Number(v);
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M float`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K float`;
  return `${n} float`;
}

function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}