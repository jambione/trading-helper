/**
 * tickers.js — Ticker table component
 *
 * Single responsibility: render the signal table.
 * Uses targeted DOM mutation (not full re-render) for smooth price updates.
 * Emits ticker-selection by calling store.selectTicker().
 */

import { subscribe, selectTicker, get } from './store.js';
import { api } from './api.js';

let _rowsEl  = null;   // <div data-ticker-rows>
let _countEl = null;   // <span data-ticker-count>
let _prevPrices = {};  // ticker → last price (for flash detection)

export function init(panelEl) {
  _rowsEl  = panelEl.querySelector('[data-ticker-rows]');
  _countEl = panelEl.querySelector('[data-ticker-count]');

  subscribe('tickers',        rows   => _renderTable(rows));
  subscribe('selectedTicker', ticker => _highlightSelected(ticker));
}

// ── Table rendering ────────────────────────────────────────────

function _renderTable(rows) {
  if (!_rowsEl) return;

  // Update count badge
  if (_countEl) {
    _countEl.textContent = rows.length ? `${rows.length} ticker${rows.length !== 1 ? 's' : ''}` : '';
  }

  if (!rows.length) {
    _rowsEl.innerHTML = '<div class="table-empty">No tickers — start the transcriber and mention some stocks.</div>';
    _prevPrices = {};
    return;
  }

  // Collect existing row elements indexed by ticker symbol
  const existing = /** @type {Map<string, HTMLElement>} */ (new Map());
  _rowsEl.querySelectorAll('[data-row]').forEach(el => existing.set(el.dataset.row, el));

  const rendered = new Set();

  for (const row of rows) {
    const sym = row.ticker;
    rendered.add(sym);

    if (existing.has(sym)) {
      _updateRow(existing.get(sym), row);
    } else {
      const el = _createRow(row);
      _rowsEl.appendChild(el);
      existing.set(sym, el);
    }
  }

  // Remove rows for tickers no longer in the list
  existing.forEach((el, sym) => {
    if (!rendered.has(sym)) el.remove();
  });

  // Re-order DOM to match server sort order (BUY → ON_DECK → …)
  const ordered = rows.map(r => r.ticker);
  const children = [..._rowsEl.querySelectorAll('[data-row]')];
  const needsReorder = children.some((el, i) => el.dataset.row !== ordered[i]);
  if (needsReorder) {
    ordered.forEach(sym => {
      const el = _rowsEl.querySelector(`[data-row="${sym}"]`);
      if (el) _rowsEl.appendChild(el);
    });
  }

  // Price-change flash
  for (const row of rows) {
    if (row.price != null && _prevPrices[row.ticker] !== undefined && _prevPrices[row.ticker] !== row.price) {
      const priceEl = _rowsEl.querySelector(`[data-price="${row.ticker}"]`);
      if (priceEl) {
        priceEl.classList.remove('price-flash');
        void priceEl.offsetWidth; // force reflow
        priceEl.classList.add('price-flash');
      }
    }
    if (row.price != null) _prevPrices[row.ticker] = row.price;
  }

  _highlightSelected(get('selectedTicker'));
}

// ── Row creation ───────────────────────────────────────────────

function _createRow(row) {
  const el = document.createElement('div');
  el.className = `ticker-row ${_rowClass(row.status)}${row.mentioned ? ' row-mentioned' : ''}`;
  el.dataset.row = row.ticker;
  el.innerHTML = _rowHTML(row);
  el.addEventListener('click', () => selectTicker(row.ticker));
  el.querySelector('[data-wb-btn]').addEventListener('click', e => {
    e.stopPropagation();
    _addToWebull(e.currentTarget, row.ticker);
  });
  el.querySelector('[data-delete-btn]').addEventListener('click', e => {
    e.stopPropagation();
    _removeTicker(row.ticker);
  });
  return el;
}

async function _removeTicker(ticker) {
  try {
    await api.removeTicker(ticker);
  } catch (err) {
    console.error('Failed to remove ticker', err);
  }
}

async function _addToWebull(btn, ticker) {
  btn.disabled = true;
  btn.textContent = '...';
  try {
    await api.addToWebull(ticker);
    btn.textContent = 'WB';
  } catch {
    btn.textContent = '!';
    setTimeout(() => { btn.textContent = 'WB'; btn.disabled = false; }, 1500);
    return;
  }
  btn.disabled = false;
}

/** Surgical update — only touch the cells that can change between scans. */
function _updateRow(el, row) {
  // Status class on the row itself
  el.className = `ticker-row ${_rowClass(row.status)}${row.mentioned ? ' row-mentioned' : ''}`;

  // Price
  const priceEl = el.querySelector('[data-price]');
  if (priceEl) priceEl.textContent = row.price != null ? `$${row.price.toFixed(2)}` : '—';

  // Pct change from open
  const chgEl = el.querySelector('[data-chg]');
  if (chgEl) {
    chgEl.textContent = _fmtChg(row.pct_change ?? null);
    chgEl.className   = `cell-chg ${_chgClass(row.pct_change ?? null)}`;
  }

  // Volume (today's cumulative, colour-coded by rvol)
  const volEl = el.querySelector('[data-vol]');
  if (volEl) {
    volEl.textContent = _fmtVol(row.day_vol);
    volEl.className   = `cell-vol${(row.rvol ?? 0) >= 1.5 ? ' vol-high' : ''}`;
  }

  // Proximity bar
  const fill = el.querySelector('[data-prox-fill]');
  if (fill) {
    const pct = Math.round((row.proximity ?? 0) * 100);
    fill.style.width = `${pct}%`;
    fill.style.backgroundColor = _proxColor(row.proximity ?? 0);
  }
  const pctEl = el.querySelector('[data-prox-pct]');
  if (pctEl) pctEl.textContent = `${Math.round((row.proximity ?? 0) * 100)}%`;

  // %R fast
  const rteEl = el.querySelector('[data-rte]');
  if (rteEl) rteEl.textContent = row.rte_fast != null ? row.rte_fast.toFixed(0) : '—';

  // RSI
  const rsiEl = el.querySelector('[data-rsi]');
  if (rsiEl) rsiEl.textContent = row.cm_rsi != null ? row.cm_rsi.toFixed(0) : '—';

  // OBV
  const obvEl = el.querySelector('[data-obv]');
  if (obvEl) {
    obvEl.textContent = row.obv_up ? '▲' : '—';
    obvEl.className   = `cell-obv${row.obv_up ? ' obv-up' : ''}`;
  }

  // Streak
  const strkEl = el.querySelector('[data-streak]');
  if (strkEl) {
    strkEl.textContent = row.streak ?? '—';
    strkEl.className   = `cell-streak${(row.streak ?? 0) >= 1 ? ' streak-active' : ''}`;
  }

  // Badge
  const badge = el.querySelector('[data-badge]');
  if (badge) {
    const t = _badgeTheme(row.status);
    badge.className   = `status-badge ${t.cls}`;
    badge.textContent = t.label;
  }
}

// ── Row HTML template ──────────────────────────────────────────

function _rowHTML(row) {
  const price  = row.price != null ? `$${row.price.toFixed(2)}` : '—';
  const rte    = row.rte_fast != null ? row.rte_fast.toFixed(0) : '—';
  const rsi    = row.cm_rsi  != null ? row.cm_rsi.toFixed(0)   : '—';
  const streak = row.streak  != null ? row.streak : '—';
  const pct    = Math.round((row.proximity ?? 0) * 100);
  const color  = _proxColor(row.proximity ?? 0);
  const { cls, label } = _badgeTheme(row.status);
  const chgCls = _chgClass(row.pct_change ?? null);
  const volCls = (row.rvol ?? 0) >= 1.5 ? ' vol-high' : '';

  return `<div class="table-cols">
    <div class="cell-ticker">${row.ticker}</div>
    <div class="cell-price" data-price="${row.ticker}">${price}</div>
    <div class="cell-chg ${chgCls}" data-chg>${_fmtChg(row.pct_change ?? null)}</div>
    <div class="cell-vol${volCls}" data-vol>${_fmtVol(row.day_vol)}</div>
    <div class="cell-proximity">
      <div class="proximity-track">
        <div class="proximity-fill" data-prox-fill style="width:${pct}%;background-color:${color}"></div>
      </div>
      <div class="proximity-pct" data-prox-pct>${pct}%</div>
    </div>
    <div class="cell-rte" data-rte>${rte}</div>
    <div class="cell-rsi" data-rsi>${rsi}</div>
    <div class="cell-obv${row.obv_up ? ' obv-up' : ''}" data-obv>${row.obv_up ? '▲' : '—'}</div>
    <div class="cell-streak${(row.streak ?? 0) >= 1 ? ' streak-active' : ''}" data-streak>${streak}</div>
    <div class="status-badge ${cls}" data-badge>${label}</div>
    <div class="cell-actions">
      <button class="btn-wb" data-wb-btn title="Add to Webull watchlist">WB</button>
      <button class="btn-delete" data-delete-btn title="Remove from watchlist">✕</button>
    </div>
  </div>`;
}

// ── Selection highlight ────────────────────────────────────────

function _highlightSelected(ticker) {
  if (!_rowsEl) return;
  _rowsEl.querySelectorAll('[data-row]').forEach(el => {
    el.classList.toggle('ticker-row--selected', el.dataset.row === ticker);
  });
}

// ── Helpers ────────────────────────────────────────────────────

function _rowClass(status) {
  return { BUY: 'row-buy', ON_DECK: 'row-deck' }[status] ?? '';
}

function _badgeTheme(status) {
  const map = {
    BUY:     { cls: 'badge-buy',    label: 'BUY' },
    ON_DECK: { cls: 'badge-deck',   label: 'ON DECK' },
    WARMING: { cls: 'badge-warm',   label: 'WARMING' },
    COLD:    { cls: 'badge-cold',   label: 'COLD' },
    NO_DATA: { cls: 'badge-nodata', label: 'NO DATA' },
    ERROR:   { cls: 'badge-error',  label: 'ERROR' },
  };
  return map[status] ?? map.NO_DATA;
}

function _proxColor(p) {
  if (p >= 1.0) return 'var(--buy)';
  if (p >= 0.8) return 'var(--deck)';
  if (p >= 0.5) return 'var(--warming)';
  return 'var(--cold)';
}

function _fmtChg(v) {
  if (v == null) return '—';
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
}

function _chgClass(v) {
  if (v == null) return '';
  return v >= 0 ? 'chg-pos' : 'chg-neg';
}

function _fmtVol(v) {
  if (v == null) return '—';
  if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return Math.round(v / 1e3) + 'K';
  return String(v);
}
