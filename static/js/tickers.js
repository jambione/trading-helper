/**
 * tickers.js — Ticker table component
 *
 * Single responsibility: render the signal table.
 * Uses targeted DOM mutation (not full re-render) for smooth price updates.
 * Emits ticker-selection by calling store.selectTicker().
 */

import { subscribe, selectTicker, get } from './store.js?v=29';
import { api } from './api.js?v=29';

let _rowsEl     = null;   // <div data-ticker-rows>
let _countEl    = null;   // <span data-ticker-count>
let _prevPrices = {};     // ticker → last price (for flash detection)
let _sortCol    = 'price';
let _sortDir    = 1;      // 1 = ascending, -1 = descending
let _headerEls  = {};     // col key → th element
let _lastRows   = [];     // last received rows (for re-render on sort change)

export function init(panelEl) {
  _rowsEl  = panelEl.querySelector('[data-ticker-rows]');
  _countEl = panelEl.querySelector('[data-ticker-count]');

  // Wire sortable column headers
  panelEl.querySelectorAll('[data-sort-col]').forEach(h => {
    const col = h.dataset.sortCol;
    _headerEls[col] = h;
    h.addEventListener('click', () => {
      if (_sortCol === col) {
        _sortDir *= -1;
      } else {
        _sortCol = col;
        _sortDir = col === 'ticker' ? 1 : 1;
      }
      _updateSortHeaders();
      if (_lastRows.length) _renderTable(_lastRows);
    });
  });
  _updateSortHeaders();

  subscribe('tickers',        rows   => _renderTable(rows));
  subscribe('selectedTicker', ticker => _highlightSelected(ticker));
}

// ── Sort helpers ───────────────────────────────────────────────

function _applySort(rows) {
  const mentioned = rows.filter(r => r.mentioned);
  const rest      = rows.filter(r => !r.mentioned);
  rest.sort((a, b) => {
    switch (_sortCol) {
      case 'ticker': return _sortDir * a.ticker.localeCompare(b.ticker);
      case 'price': {
        const av = a.price  ?? (_sortDir > 0 ? Infinity : -Infinity);
        const bv = b.price  ?? (_sortDir > 0 ? Infinity : -Infinity);
        return _sortDir * (av - bv);
      }
      case 'chg': {
        const av = a.pct_change ?? (_sortDir > 0 ? Infinity : -Infinity);
        const bv = b.pct_change ?? (_sortDir > 0 ? Infinity : -Infinity);
        return _sortDir * (av - bv);
      }
      case 'vol': {
        const av = a.day_vol ?? (_sortDir > 0 ? -Infinity : Infinity);
        const bv = b.day_vol ?? (_sortDir > 0 ? -Infinity : Infinity);
        return _sortDir * (av - bv);
      }
      default: return 0;
    }
  });
  return [...mentioned, ...rest];
}

function _updateSortHeaders() {
  Object.entries(_headerEls).forEach(([col, el]) => {
    // Store original label once
    if (!el.dataset.sortLabel) el.dataset.sortLabel = el.textContent.trim();
    const label = el.dataset.sortLabel;
    if (col === _sortCol) {
      el.textContent = label + (_sortDir === 1 ? ' ↑' : ' ↓');
      el.classList.add('th--sorted');
    } else {
      el.textContent = label;
      el.classList.remove('th--sorted');
    }
  });
}

// ── Table rendering ────────────────────────────────────────────

function _renderTable(rows) {
  if (!_rowsEl) return;
  _lastRows = rows;

  // Update count badge
  if (_countEl) {
    _countEl.textContent = rows.length ? `${rows.length} ticker${rows.length !== 1 ? 's' : ''}` : '';
  }

  if (!rows.length) {
    _rowsEl.innerHTML = '';
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

  // Client-side sort: highlighted rows always first, then un-mentioned sorted by chosen column
  const sorted  = _applySort(rows);
  const ordered = sorted.map(r => r.ticker);
  const children = [..._rowsEl.querySelectorAll('[data-row]')];
  const needsReorder = children.some((el, i) => el.dataset.row !== ordered[i]);
  if (needsReorder) {
    ordered.forEach(sym => {
      const el = _rowsEl.querySelector(`[data-row="${sym}"]`);
      if (el) _rowsEl.appendChild(el);
    });
  }

  // $20 price divider — only shown when sorting by price
  _rowsEl.querySelector('[data-price-divider]')?.remove();
  if (_sortCol === 'price') {
    const firstAbove20 = sorted.find(r => !r.mentioned && r.price != null && r.price > 20);
    const hasBelow20   = sorted.some(r => !r.mentioned && (r.price == null || r.price <= 20));
    if (firstAbove20 && hasBelow20) {
      const aboveEl = _rowsEl.querySelector(`[data-row="${firstAbove20.ticker}"]`);
      if (aboveEl) {
        const divider = document.createElement('div');
        divider.className = 'price-divider';
        divider.setAttribute('data-price-divider', '');
        _rowsEl.insertBefore(divider, aboveEl);
      }
    }
  }

  // Price-change flash — uses CSS transition (no forced reflow)
  for (const row of rows) {
    if (row.price != null && _prevPrices[row.ticker] !== undefined && _prevPrices[row.ticker] !== row.price) {
      const priceEl = _rowsEl.querySelector(`[data-price="${row.ticker}"]`);
      if (priceEl) {
        priceEl.classList.add('price-flash');
        clearTimeout(priceEl._flashTimer);
        priceEl._flashTimer = setTimeout(() => priceEl.classList.remove('price-flash'), 600);
      }
    }
    if (row.price != null) _prevPrices[row.ticker] = row.price;
  }

  _highlightSelected(get('selectedTicker'));
}

// ── Row creation ───────────────────────────────────────────────

function _createRow(row) {
  const el = document.createElement('div');
  el.className = `ticker-row${row.mentioned ? ' row-mentioned' : ''}`;
  el.dataset.row = row.ticker;
  el.innerHTML = _rowHTML(row);
  el.addEventListener('click', () => selectTicker(row.ticker));
  el.querySelector('[data-add-btn]').addEventListener('click', e => {
    e.stopPropagation();
    _addToWBAndTV(e.currentTarget, row.ticker);
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

async function _addToWBAndTV(btn, ticker) {
  btn.disabled = true;
  btn.textContent = '...';
  try {
    // Step 1: save ticker to server watchlist
    await api.addTicker(ticker);

    // Steps 2 & 3: call the local Windows agent for Webull and TradingView.
    // The agent runs on localhost:8889 — if it's not running, skip silently.
    const _agentPost = async (endpoint) => {
      try {
        const resp = await fetch(`http://localhost:8889${endpoint}`, {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ ticker }),
          signal:  AbortSignal.timeout(5000),
        });
        if (!resp.ok) console.warn(`[agent] ${endpoint} returned`, resp.status);
      } catch {
        // Agent not running — skip silently
      }
    };

    await _agentPost('/add-wb');  // Webull Desktop: Ctrl+2, type ticker, Enter
    await _agentPost('/add-tv');  // Brave TradingView: pinned tab, type, Enter, Alt+W

    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = 'Add'; btn.disabled = false; }, 1500);
  } catch {
    btn.textContent = '!';
    setTimeout(() => { btn.textContent = 'Add'; btn.disabled = false; }, 1500);
  }
}

/** Surgical update — only touch the cells that can change between scans. */
function _updateRow(el, row) {
  el.className = `ticker-row${row.mentioned ? ' row-mentioned' : ''}${row.mention_burst ? ' row-burst' : ''}`;

  // Update mention badge
  const tickerCell = el.querySelector('.cell-ticker');
  if (tickerCell) {
    let badge = tickerCell.querySelector('.mention-badge');
    const w = row.mention_window ?? 0;
    if (w > 0) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'mention-badge';
        tickerCell.appendChild(badge);
      }
      badge.textContent = String(w);
      badge.title       = `${row.mention_count ?? 0} today`;
      badge.className   = `mention-badge${row.mention_burst ? ' mentions-burst' : ' mentions-active'}`;
    } else if (badge) {
      badge.remove();
    }
  }

  const priceEl = el.querySelector('[data-price]');
  if (priceEl) priceEl.textContent = row.price != null ? `$${row.price.toFixed(2)}` : '—';

  const chgEl = el.querySelector('[data-chg]');
  if (chgEl) {
    chgEl.textContent = _fmtChg(row.pct_change ?? null);
    chgEl.className   = `cell-chg ${_chgClass(row.pct_change ?? null)}`;
  }

  const volEl = el.querySelector('[data-vol]');
  if (volEl) {
    volEl.textContent = _fmtVol(row.day_vol);
    volEl.className   = `cell-vol${(row.rvol ?? 0) >= 1.5 ? ' vol-high' : ''}`;
  }
}

// ── Row HTML template ──────────────────────────────────────────

function _rowHTML(row) {
  const price  = row.price != null ? `$${row.price.toFixed(2)}` : '—';
  const chgCls = _chgClass(row.pct_change ?? null);
  const volCls = (row.rvol ?? 0) >= 1.5 ? ' vol-high' : '';

  const mentionCls = row.mention_burst  ? ' mentions-burst'
                   : (row.mention_window ?? 0) > 1 ? ' mentions-active' : '';
  const mentionTxt = (row.mention_window ?? 0) > 1 ? `${row.mention_window}` : '';

  return `<div class="watchlist-cols">
    <div class="cell-ticker">
      ${row.ticker}
      ${mentionTxt ? `<span class="mention-badge${mentionCls}" title="${row.mention_count ?? 0} today">${mentionTxt}</span>` : ''}
    </div>
    <div class="cell-price" data-price="${row.ticker}">${price}</div>
    <div class="cell-chg ${chgCls}" data-chg>${_fmtChg(row.pct_change ?? null)}</div>
    <div class="cell-vol${volCls}" data-vol>${_fmtVol(row.day_vol)}</div>
    <div class="cell-actions">
      <button class="btn-add" data-add-btn title="Add to Webull + open in TradingView">Add</button>
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
