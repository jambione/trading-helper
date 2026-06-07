/**
 * tickers.js — Ticker table component
 *
 * Single responsibility: render the signal table.
 * Uses targeted DOM mutation (not full re-render) for smooth price updates.
 * Emits ticker-selection by calling store.selectTicker().
 */

import { subscribe, selectTicker, get } from './store.js?v=48';
import { api } from './api.js?v=48';

let _rowsEl     = null;   // <div data-ticker-rows>
let _countEl    = null;   // <span data-ticker-count>
let _prevPrices = {};     // ticker → last price (for flash detection)
let _sortCol    = 'price';
let _sortDir    = 1;      // 1 = ascending, -1 = descending
let _headerEls  = {};     // col key → th element
let _lastRows   = [];     // last received rows (for re-render on sort change)

// ── Copied-ticker feed tracking ────────────────────────────────
const _COPY_DATE_KEY    = 'ss:copied-date';
const _COPY_TICKERS_KEY = 'ss:copied-tickers';
let _copiedTickers = [];

function _loadCopiedTickers() {
  const today = new Date().toISOString().slice(0, 10);
  if (localStorage.getItem(_COPY_DATE_KEY) === today) {
    try { _copiedTickers = JSON.parse(localStorage.getItem(_COPY_TICKERS_KEY) || '[]'); } catch { _copiedTickers = []; }
  } else {
    // New day — reset
    _copiedTickers = [];
    localStorage.setItem(_COPY_DATE_KEY, today);
    localStorage.setItem(_COPY_TICKERS_KEY, '[]');
  }
  _pushToFeed();
}

function _pushToFeed() {
  if (typeof window.__feedUpdateCopied === 'function') {
    window.__feedUpdateCopied([..._copiedTickers]);
  }
}

export function clearCopiedTickers() {
  _copiedTickers = [];
  localStorage.removeItem(_COPY_DATE_KEY);
  localStorage.removeItem(_COPY_TICKERS_KEY);
  _pushToFeed();
}

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

  _loadCopiedTickers();
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
  el.querySelector('[data-copy-btn]').addEventListener('click', e => {
    e.stopPropagation();
    _copyTicker(e.currentTarget, row.ticker);
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

async function _copyTicker(btn, ticker) {
  try {
    await navigator.clipboard.writeText(ticker);
    // Append to today's copied list (deduplicated, order preserved)
    if (!_copiedTickers.includes(ticker)) {
      _copiedTickers.push(ticker);
      const today = new Date().toISOString().slice(0, 10);
      localStorage.setItem(_COPY_DATE_KEY, today);
      localStorage.setItem(_COPY_TICKERS_KEY, JSON.stringify(_copiedTickers));
      _pushToFeed();
    }
    btn.textContent = '✓';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  } catch {
    btn.textContent = '!';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
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

  // Signal proximity bar. Momentum mode shows it on alerted (mention_burst)
  // tickers only; the three_indicator strategy shows it on every engine-tracked
  // ticker so the whole watchlist is watchable while testing.
  const sp          = row.signal_proximity || null;
  const isThreeInd  = !!(sp && sp.strategy === 'three_indicator');
  const isAlert     = !!(sp && sp.strategy === 'alert');
  const hasBar      = !!row.mention_burst || isThreeInd || isAlert;
  let   barEl       = el.querySelector('[data-signal-bar]');

  if (hasBar) {
    if (!barEl) {
      // Bar didn't exist yet — inject it
      const tmp = document.createElement('div');
      tmp.innerHTML = _signalBarHTML(row);
      barEl = tmp.firstElementChild;
      if (barEl) el.appendChild(barEl);
    } else if (sp) {
      // Bar exists — surgically update fill width + class + label + pills
      const pct = sp.proximity_pct ?? 0;
      const fillEl = barEl.querySelector('[data-signal-fill]');
      if (fillEl) {
        fillEl.style.width = `${Math.min(pct, 100)}%`;
        fillEl.className   = `signal-bar-fill ${_signalFillClass(sp)}`;
      }
      const labelEl = barEl.querySelector('[data-signal-label]');
      if (labelEl) labelEl.textContent = _signalStatusLabel(sp);
      const condsEl = barEl.querySelector('.signal-conds');
      if (condsEl) condsEl.innerHTML = _signalPills(sp);
    }
  } else if (barEl) {
    // Ticker no longer has a burst alert — remove bar
    barEl.remove();
  }
}

// ── Row HTML template ──────────────────────────────────────────

// Fill-colour tier from proximity_pct / position state (strategy-agnostic).
function _signalFillClass(sp) {
  const pct = sp.proximity_pct ?? 0;
  return sp.in_position ? 'signal-fill--position'
       : pct >= 100     ? 'signal-fill--max'
       : pct >= 67      ? 'signal-fill--high'
       : pct >= 34      ? 'signal-fill--mid'
       :                  'signal-fill--low';
}

// Human-readable status label — labels differ per strategy.
function _signalStatusLabel(sp) {
  const status = sp.status ?? 'watching';
  const labels = (sp.strategy === 'alert') ? {
    buy_zone:    '🔥 CATALYST — buying',
    aligning:    '📈 Mentions building',
    in_position: '📈 In position',
    watching:    '😴 Watching',
  } : (sp.strategy === 'three_indicator') ? {
    buy_zone:    '🔥 BUY ZONE',
    aligning:    '📈 Aligning',
    in_position: '📈 In position',
    exit_signal: '🔻 EXIT signal',
    watching:    '😴 Watching',
  } : {
    buy_zone:         '🔥 BUY ZONE',
    growing_rsi_high: '📈 Growing — RSI high',
    hist_positive:    '👀 MACD positive',
    retreated:        '↩ Retreated',
    in_position:      '📈 In position',
    watching:         '😴 Watching',
  };
  return labels[status] ?? status;
}

// Condition pills — three_indicator shows CM / %R / MACD; momentum shows RSI / + / ↑.
function _signalPills(sp) {
  const isHot = sp.is_hot;
  const vel   = sp.mention_velocity ?? 0;
  const hotPill  = isHot ? `<span class="sig-cond cond-hot" title="${vel} mentions — RSI bypassed">🔥</span>` : '';
  const srcBadge = (sp.data_source === 'massive')
    ? `<span class="sig-src" title="Bar data from Massive.com">M</span>` : '';

  if (sp.strategy === 'alert') {
    // The catalyst is the signal: show mention velocity; once in a position
    // show live P&L + the peak the trailing stop is protecting.
    const velPill = `<span class="sig-cond ${sp.is_hot ? 'cond-ok' : 'cond-no'}" title="${vel} mentions in window — a burst fires the buy">🔥${vel}</span>`;
    if (sp.in_position) {
      const pnl = sp.pnl_pct, peak = sp.peak_gain_pct;
      const pnlCls = (pnl != null && pnl >= 0) ? 'cond-ok' : 'cond-no';
      const pnlTxt = pnl != null ? `${pnl >= 0 ? '+' : ''}${pnl.toFixed(1)}%` : 'P&L ?';
      const pnlPill = `<span class="sig-cond ${pnlCls}" title="unrealized P&L since entry">${pnlTxt}</span>`;
      const peakPill = `<span class="sig-cond" title="peak gain since entry — trailing stop is ${sp.trail_stop_pct}% below the peak">▲${peak != null ? peak.toFixed(0) : '0'}%</span>`;
      return `${velPill}${pnlPill}${peakPill}${srcBadge}`;
    }
    const stopPill = `<span class="sig-cond" title="exit recipe: trailing ${sp.trail_stop_pct}% / hard ${sp.hard_stop_pct}%">⛒ ${sp.trail_stop_pct}/${sp.hard_stop_pct}</span>`;
    return `${velPill}${stopPill}${srcBadge}`;
  }

  if (sp.strategy === 'three_indicator') {
    const cm = sp.cm_rsi, pr = sp.pctr, sep = sp.macd_sep_ratio;
    const cmPill = `<span class="sig-cond ${sp.cm_ok ? 'cond-ok' : 'cond-no'}" title="CM RSI-2 ${cm != null ? cm.toFixed(1) : '?'}${sp.cm_rsi_rising ? ' rising' : ''} — need <40 rising">CM</span>`;
    const prPill = `<span class="sig-cond ${sp.pctr_ok ? 'cond-ok' : 'cond-no'}" title="%R Exhaustion ${pr != null ? pr.toFixed(1) : '?'}${sp.pctr_rising ? ' rising toward 0' : ''}">%R</span>`;
    const mdPill = `<span class="sig-cond ${sp.macd_ok ? 'cond-ok' : 'cond-no'}" title="MACD ${sp.macd_cross ? 'crossed bullish' : 'no cross'}${sep != null ? `, separation ×${sep}` : ''} — need wide cross">MACD</span>`;
    return `${cmPill}${prPill}${mdPill}${hotPill}${srcBadge}`;
  }

  // momentum (default)
  const rsi = sp.rsi, macdHist = sp.macd_hist;
  const rsiOk = isHot || (rsi != null && rsi < 70);
  const rsiPill  = `<span class="sig-cond ${rsiOk ? 'cond-ok' : 'cond-no'}" title="RSI ${rsi != null ? rsi.toFixed(1) : '?'}${isHot ? ' (bypassed)' : ''}">RSI</span>`;
  const posPill  = `<span class="sig-cond ${sp.hist_positive ? 'cond-ok' : 'cond-no'}" title="MACD histogram ${macdHist != null ? macdHist.toFixed(4) : '?'}">+</span>`;
  const growPill = `<span class="sig-cond ${sp.hist_growing ? 'cond-ok' : 'cond-no'}" title="MACD growing">↑</span>`;
  return `${rsiPill}${posPill}${growPill}${hotPill}${srcBadge}`;
}

function _signalBarHTML(row) {
  const sp = row.signal_proximity || null;
  // Momentum: alerted tickers only. three_indicator / alert: every tracked ticker.
  if (!row.mention_burst &&
      !(sp && (sp.strategy === 'three_indicator' || sp.strategy === 'alert'))) return '';

  // No signal engine data yet — show a dormant placeholder bar
  if (!sp) {
    return `<div class="signal-bar-row" data-signal-bar>
    <div class="signal-bar-track">
      <div class="signal-bar-fill signal-fill--low" style="width:0%" data-signal-fill></div>
    </div>
    <div class="signal-bar-meta">
      <div class="signal-conds">
        <span class="sig-cond cond-no">…</span>
        <span class="sig-cond cond-no">…</span>
        <span class="sig-cond cond-no">…</span>
      </div>
      <span class="signal-status-label" data-signal-label>⏳ Waiting for engine…</span>
    </div>
  </div>`;
  }

  const pct = sp.proximity_pct ?? 0;
  return `<div class="signal-bar-row" data-signal-bar>
    <div class="signal-bar-track">
      <div class="signal-bar-fill ${_signalFillClass(sp)}" style="width:${Math.min(pct, 100)}%" data-signal-fill></div>
    </div>
    <div class="signal-bar-meta">
      <div class="signal-conds">${_signalPills(sp)}</div>
      <span class="signal-status-label" data-signal-label>${_signalStatusLabel(sp)}</span>
    </div>
  </div>`;
}

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
      <button class="btn-copy" data-copy-btn title="Copy ticker to clipboard">Copy</button>
      <button class="btn-delete" data-delete-btn title="Remove from watchlist">✕</button>
    </div>
  </div>
  ${_signalBarHTML(row)}`;
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
