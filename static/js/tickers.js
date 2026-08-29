/**
 * tickers.js — Ticker table component
 *
 * Single responsibility: render the signal table.
 * Uses targeted DOM mutation (not full re-render) for smooth price updates.
 * Emits ticker-selection by calling store.selectTicker().
 */

import { subscribe, selectTicker, get } from './store.js?v=134';
import { api } from './api.js?v=133';
import { createSymbolMembershipWatcher } from './panelFlash.js?v=136';

let _rowsEl     = null;   // <div data-ticker-rows>
let _countEl    = null;   // <span data-ticker-count>
let _suggestEl  = null;   // <div data-funnel-suggest> — funnel banner
let _panelEl    = null;   // panel section (for header flash)
let _prevPrices = {};     // ticker → last price (for flash detection)
let _sortCol    = 'price';
let _sortDir    = 1;      // 1 = ascending, -1 = descending
let _headerEls  = {};     // col key → th element
let _lastRows   = [];     // last received rows (for re-render on sort change)
const _noteSymbols = createSymbolMembershipWatcher();

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

// ── TradingView click-to-open toggle ────────────────────────────
const _TV_CLICK_KEY = 'tb:tv-click-open';
let _tvToggleBtn    = null;

export function isTvClickOpenEnabled() {
  if (typeof document !== 'undefined') {
    if (document.body.classList.contains('mobile') || (window.innerWidth && window.innerWidth <= 768)) {
      return false;
    }
  }
  try {
    return localStorage.getItem(_TV_CLICK_KEY) === 'true';
  } catch {
    return false;
  }
}

export function openTradingViewChart(ticker) {
  const sym = encodeURIComponent(String(ticker || '').trim().toUpperCase());
  if (!sym) return;

  const cfg = get('config') || {};
  let base = (cfg.tv_chart_url || '').trim();
  let url;
  if (!base) {
    url = `https://www.tradingview.com/chart/?symbol=${sym}`;
  } else if (base.includes('{sym}')) {
    url = base.replace('{sym}', sym);
  } else {
    const sep = base.includes('?') ? '&' : (base.endsWith('/') ? '?' : '/?');
    url = `${base}${sep}symbol=${sym}`;
  }
  // A NAMED target, not _blank: the point of the feature is not to open a
  // chart, it is to stop switching tabs and retyping symbols. _blank gives a
  // fresh tab per click, so five tickers leave five TradingView tabs and the
  // operator is back to hunting for the right one. A name reuses the same tab
  // and reloads it with the new symbol, which is what "load it into my chart"
  // actually means.
  //
  // Dropping `noopener` is required for that reuse — it forces a fresh
  // context and defeats the name. The trade is that the opened page can see
  // window.opener. The destination is tradingview.com, or a URL the operator
  // configured themselves in tv_chart_url, so this is a known site rather
  // than untrusted content; the reference is cleared below anyway.
  const win = window.open(url, String(cfg.tv_chart_window || 'tvchart'));
  if (win) {
    try { win.opener = null; } catch { /* cross-origin: already isolated */ }
    try { win.focus(); } catch { /* focus is best-effort */ }
  } else {
    // Say so. The version this replaced swallowed every failure into an empty
    // catch, which is why a dead button was indistinguishable from a working
    // one for two commits.
    console.warn('[tickers] TradingView tab was blocked — allow pop-ups for '
                 + 'this site to load charts on click.');
  }

  // Best effort, and deliberately last: if the desk agent happens to be
  // running it also loads the symbol in the native TradingView app. The
  // browser path above must never depend on it — that dependency is what
  // made this feature do nothing at all.
  try {
    api.addToTV(sym).catch(() => {});
  } catch { /* no agent, no problem */ }
}

export function updateTickerTitles() {
  const on = isTvClickOpenEnabled();
  document.querySelectorAll('.cell-ticker').forEach(el => {
    let sym = el.dataset.symbol || '';
    if (!sym) {
      const row = el.closest('[data-row], [data-feed-symbol], [data-symbol]');
      sym = row?.dataset.row || row?.dataset.feedSymbol || row?.dataset.symbol || '';
    }
    if (!sym && el.title) {
      sym = el.title.replace(/^Open\s+/i, '').replace(/^Load\s+/i, '').replace(/\s+(in|into)\s+TradingView.*$/i, '').replace(/^Copy\s+/i, '').trim();
    }
    if (sym) {
      el.title = on ? `Load ${sym} into TradingView` : `Copy ${sym}`;
    }
  });
}

function _updateTvToggleBtn() {
  if (!_tvToggleBtn) return;
  const on = isTvClickOpenEnabled();
  _tvToggleBtn.classList.toggle('btn--active', on);
  _tvToggleBtn.textContent = on ? '✓ TV Chart: ON' : 'TV Chart: OFF';
  _tvToggleBtn.title = on
    ? 'TradingView chart loading is ON. Clicking a ticker launches/focuses TradingView and loads the ticker. Click to turn OFF.'
    : 'TradingView chart loading is OFF. Clicking a ticker copies it to clipboard. Click to turn ON.';
}

export function initTvToggle(btn) {
  _tvToggleBtn = btn || document.querySelector('[data-tv-toggle-btn]');
  if (!_tvToggleBtn) return;
  _updateTvToggleBtn();
  _tvToggleBtn.addEventListener('click', () => {
    const next = !isTvClickOpenEnabled();
    try {
      localStorage.setItem(_TV_CLICK_KEY, String(next));
    } catch {}
    _updateTvToggleBtn();
    updateTickerTitles();
  });
}

export function init(panelEl) {
  _panelEl   = panelEl;
  _rowsEl    = panelEl.querySelector('[data-ticker-rows]');
  _countEl   = panelEl.querySelector('[data-ticker-count]');
  _suggestEl = panelEl.querySelector('[data-funnel-suggest]');

  const tvBtn = document.querySelector('[data-tv-toggle-btn]');
  if (tvBtn) initTvToggle(tvBtn);

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
  // Funnel banner (EXITS ONLY / Point monitors) removed from the Momentum
  // panel — session labels and one-click send were noise next to the book.
  if (_suggestEl) {
    _suggestEl.classList.add('hidden');
    _suggestEl.innerHTML = '';
  }
  // AI book open/close should refresh chips on momentum rows.
  subscribe('ai_positions',   ()     => { if (_lastRows.length) _renderTable(_lastRows); });

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
      case 'rvol': {
        const av = a.rvol ?? (_sortDir > 0 ? -Infinity : Infinity);
        const bv = b.rvol ?? (_sortDir > 0 ? -Infinity : Infinity);
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

/**
 * Rows the AI Watch book pushed purely so they carry live tape. They are real
 * data subscriptions — the book needs the quote — but they are not momentum
 * candidates, and listing them buried the panel: the watchlist went to 23 rows
 * of which 13 were book coverage. Hidden unless the name earns its own place
 * here (a Discord mention, a burst, or a first-find), in which case it is a
 * momentum candidate that happens to also be on the book.
 */
function _isBookOnly(r) {
  return r && r.src === 'book'
    && !r.mentioned && !r.mention_burst && !r.find_it_first
    && !(r.mention_count > 0);
}

function _renderTable(allRows) {
  if (!_rowsEl) return;
  const rows = (allRows || []).filter(r => !_isBookOnly(r));
  _lastRows = rows;

  // Update count badge
  if (_countEl) {
    const ct = rows.length ? `${rows.length} idea${rows.length !== 1 ? 's' : ''}` : '';
    if (_countEl.textContent !== ct) _countEl.textContent = ct;
  }

  // New symbol → cyan pulse on Momentum Stocks header (5s; skip first paint).
  _noteSymbols(_panelEl, (rows || []).map(r => r.ticker));

  if (!rows.length) {
    const nBook = (allRows || []).filter(r => _isBookOnly(r)).length;
    const msg = nBook
      ? 'No Discord alerts — Book tape names stay off this list'
      : 'No Discord alerts yet';
    _rowsEl.innerHTML = `<span class="tx-placeholder">${msg}</span>`;
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

  // Price-change flash — uses CSS transition (no forced reflow). Coloured by
  // direction: which way a tick went is the part worth catching out of the
  // corner of an eye, and a single accent colour threw that away.
  for (const row of rows) {
    const prev = _prevPrices[row.ticker];
    if (row.price != null && prev !== undefined && prev !== row.price) {
      const priceEl = _rowsEl.querySelector(`[data-price="${row.ticker}"]`);
      if (priceEl) {
        const dir = row.price > prev ? 'up' : 'down';
        // Drop the opposite direction first — successive ticks can flip inside
        // one flash window, and both classes at once resolves by stylesheet
        // order rather than by which tick actually happened.
        priceEl.classList.remove('price-flash--up', 'price-flash--down');
        priceEl.classList.add(`price-flash--${dir}`);
        clearTimeout(priceEl._flashTimer);
        // Hold at full colour, then let the class come off and the base rule's
        // long transition decay it. Paired with the CSS: 140ms rise + this
        // hold + 900ms fade. Shortening this without shortening the rise gives
        // a flash that never reaches its colour.
        priceEl._flashTimer = setTimeout(() => priceEl.classList.remove(
          'price-flash--up', 'price-flash--down'), 600);
      }
    }
    if (row.price != null) _prevPrices[row.ticker] = row.price;
  }
}

// ── Row creation ───────────────────────────────────────────────

function _createRow(row) {
  const el = document.createElement('div');
  el.className = 'ticker-row';
  el.dataset.row = row.ticker;
  el.innerHTML = _rowHTML(row);
  el.addEventListener('click', () => selectTicker(row.ticker));
  // Symbol name: click copies ticker (or opens TradingView if enabled).
  const tickerCell = el.querySelector('.cell-ticker');
  if (tickerCell) {
    tickerCell.title = isTvClickOpenEnabled()
      ? `Load ${row.ticker} into TradingView`
      : `Copy ${row.ticker}`;
    tickerCell.addEventListener('click', e => {
      e.stopPropagation();
      copyTicker(e.currentTarget, row.ticker);
    });
  }
  // Delete stops propagation so it does not select the row.
  const delBtn = el.querySelector('[data-delete-btn]');
  if (delBtn) {
    delBtn.addEventListener('click', e => {
      e.stopPropagation();
      _removeTicker(row.ticker);
    });
  }
  return el;
}

async function _removeTicker(ticker) {
  try {
    await api.removeTicker(ticker);
  } catch (err) {
    console.error('Failed to remove ticker', err);
  }
}

/** Copy symbol to clipboard; track in today's list (shared with Trending / AI).
 *  `el` is the symbol cell (or any element) — flash feedback on success/failure. */
export async function copyTicker(el, ticker) {
  if (isTvClickOpenEnabled()) {
    try {
      openTradingViewChart(ticker);
    } catch (err) {
      console.warn('[tickers] failed to open TradingView', err);
    }
  }
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
    if (el) {
      el.classList.add('cell-ticker--copied');
      clearTimeout(el._copiedTimer);
      el._copiedTimer = setTimeout(() => el.classList.remove('cell-ticker--copied'), 900);
    }
  } catch {
    if (el) {
      el.classList.add('cell-ticker--copy-fail');
      clearTimeout(el._copiedTimer);
      el._copiedTimer = setTimeout(() => el.classList.remove('cell-ticker--copy-fail'), 900);
    }
  }
}

/** Set text only when it changes — avoids layout thrash on 4Hz ticks. */
function _setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

/** Surgical update — only touch the cells that can change between scans. */
function _updateRow(el, row) {
  const confluent = (row.confluence?.count ?? 0) >= 2;
  // classList toggles (not className=) so badge CSS is not restarted every tick.
  // Row-level highlight classes (mentioned / burst / confluent / selected) are
  // intentionally not applied — Momentum Stocks uses badges only.
  el.classList.add('ticker-row');

  // Update mention badge
  const tickerCell = el.querySelector('.cell-ticker');
  if (tickerCell) {
    let badge = tickerCell.querySelector('.mention-badge');
    const w = row.mention_window ?? 0;
    if (w > 1) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'mention-badge';
        tickerCell.appendChild(badge);
      }
      _setText(badge, String(w));
      badge.title = `${row.mention_count ?? 0} today`;
      badge.classList.toggle('mentions-burst', !!row.mention_burst);
      badge.classList.toggle('mentions-active', !row.mention_burst);
    } else if (badge) {
      badge.remove();
    }

    // Confluence badge — keep it in sync (the mention-badge lives here too).
    let cbadge = tickerCell.querySelector('.confluence-badge');
    if (confluent) {
      const names = (row.confluence.sources || []).map(s => _CONF_LABELS[s] || s).join(' + ');
      if (!cbadge) {
        cbadge = document.createElement('span');
        cbadge.className = 'confluence-badge';
        tickerCell.appendChild(cbadge);
      }
      _setText(cbadge, `⚡${row.confluence.count}`);
      cbadge.title = `Confluence: ${names}`;
    } else if (cbadge) {
      cbadge.remove();
    }
  }

  const p = _priceCell(row);
  const priceEl2 = el.querySelector('[data-price]');
  _setText(priceEl2, p.txt);
  if (priceEl2) {
    priceEl2.classList.toggle('cell-price--snapshot', p.snap);
    if (p.snap) priceEl2.title = _snapTitle(p.age);
    else priceEl2.removeAttribute('title');
  }

  const chgEl = el.querySelector('[data-chg]');
  if (chgEl) {
    _setText(chgEl, _fmtChg(row.pct_change ?? null));
    const mod = _chgClass(row.pct_change ?? null);
    chgEl.classList.toggle('chg-pos', mod === 'chg-pos');
    chgEl.classList.toggle('chg-neg', mod === 'chg-neg');
  }

  const volEl = el.querySelector('[data-vol]');
  if (volEl) {
    _setText(volEl, _fmtVol(row.day_vol));
    volEl.classList.toggle('vol-high', (row.rvol ?? 0) >= 1.5);
  }

  const rvolEl = el.querySelector('[data-rvol]');
  if (rvolEl) {
    _setText(rvolEl, _fmtRvol(row.rvol));
    rvolEl.classList.toggle('vol-high', (row.rvol ?? 0) >= 1.5);
  }

  const flagsEl = el.querySelector('[data-flags]');
  if (flagsEl) {
    const fh = _flagsHtml(row);
    if (flagsEl.dataset.flagsKey !== fh) {
      flagsEl.innerHTML = fh;
      flagsEl.dataset.flagsKey = fh;
    }
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
        const w = `${Math.min(pct, 100)}%`;
        if (fillEl.style.width !== w) fillEl.style.width = w;
        const fillCls = `signal-bar-fill ${_signalFillClass(sp)}`;
        if (fillEl.className !== fillCls) fillEl.className = fillCls;
      }
      const labelEl = barEl.querySelector('[data-signal-label]');
      _setText(labelEl, _signalStatusLabel(sp));
      const condsEl = barEl.querySelector('.signal-conds');
      if (condsEl) {
        const pills = _signalPills(sp);
        if (condsEl.dataset.pillsKey !== pills) {
          condsEl.innerHTML = pills;
          condsEl.dataset.pillsKey = pills;
        }
      }
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

  // Render MACD Bullish Gap for all momentum watchlist items
  const gap = sp.macd_gap ?? sp.macd_hist;
  const ratio = sp.macd_sep_ratio;
  const isBull = !!(sp.macd_bull || sp.macd_ok || sp.hist_positive || (gap != null && gap > 0));
  const isWide = !!(sp.macd_ok || (ratio != null && ratio >= 0.8) || (gap != null && gap >= 0.015));
  const macdCls = isWide ? 'cond-ok' : (isBull ? 'cond-warn' : 'cond-no');
  const gapTxt = (gap != null && Number.isFinite(Number(gap)))
    ? `${Number(gap) >= 0 ? '+' : ''}${Number(gap).toFixed(3)}`
    : (sp.hist_growing ? '↑' : (sp.hist_positive ? '+' : '—'));
  const ratioTxt = (ratio != null && Number.isFinite(Number(ratio)))
    ? ` (${Number(ratio).toFixed(1)}×)`
    : '';
  const titleTxt = (gap != null && Number.isFinite(Number(gap)))
    ? `MACD Fast/Slow Line Gap: ${gapTxt}${ratioTxt} — wider gap indicates stronger bullish momentum`
    : `MACD ${sp.hist_growing ? 'growing' : (sp.hist_positive ? 'positive' : 'negative')}`;
  const macdPill = `<span class="sig-cond ${macdCls}" title="${titleTxt}">MACD ${gapTxt}${ratioTxt}</span>`;
  return `${macdPill}${hotPill}${srcBadge}`;
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

// Confluence = same ticker corroborated by 2+ independent producers (Market
// Update scanner, chat, alert, squeeze). Position-independent — it keys on the
// ticker text, not where anything sits on screen.
const _CONF_LABELS = { scanner: 'Market Update', chat: 'Chat', alert: 'Alert', squeeze: 'Squeeze' };

function _confluenceBadge(conf) {
  if (!conf || (conf.count ?? 0) < 2) return '';
  const names = (conf.sources || []).map(s => _CONF_LABELS[s] || s).join(' + ');
  return `<span class="confluence-badge" title="Confluence: ${names}">⚡${conf.count}</span>`;
}

// Morning-funnel chart-readiness → CSS tier. Same states morning_funnel emits.
const _FUNNEL_STATE_CLS = { READY: 'fn-ready', EARLY: 'fn-early', WEAK: 'fn-weak', EXTENDED: 'fn-ext' };

// Inline per-row funnel score: a small pill coloured by readiness state, or a
// muted ✕ when the funnel hard-rejected the ticker (price/spread/liquidity).
function _funnelBadge(f) {
  if (!f) return '';
  if (f.rejects && f.rejects.length) {
    return `<span class="funnel-badge funnel-badge--rej" title="Funnel: rejected — ${f.rejects.join(', ')}">✕</span>`;
  }
  const cls   = _FUNNEL_STATE_CLS[f.state] || '';
  const title = `Funnel ${f.score} · ${f.state}${f.rvol != null ? ` · RVOL ${f.rvol}x` : ''}`;
  return `<span class="funnel-badge ${cls}" title="${title}">${f.score}</span>`;
}

function _aiPosBadge(ticker) {
  const book = get('ai_positions') || {};
  const pos = (book.positions || {})[String(ticker || '').toUpperCase()];
  if (!pos) return '';
  const owner = String(book.book_owner || '').toLowerCase() === 'claude' ? 'Claude' : 'Grok';
  const qtyN = Math.abs(Number(pos.qty) || 0);
  const qty = Number.isInteger(qtyN) ? `${qtyN}sh` : `${qtyN.toFixed(2)}sh`;
  const pl = Number(pos.pl);
  const plpc = Number(pos.plpc);
  let plTxt = '';
  if (Number.isFinite(pl)) plTxt += `${pl >= 0 ? '+' : ''}$${pl.toFixed(0)}`;
  if (Number.isFinite(plpc)) plTxt += `${plTxt ? ' ' : ''}${plpc >= 0 ? '+' : ''}${plpc.toFixed(1)}%`;
  const cls = Number.isFinite(pl) && pl < 0 ? 'ai-pos-chip--neg'
    : Number.isFinite(pl) && pl > 0 ? 'ai-pos-chip--pos' : '';
  const title = `${owner} paper · ${ticker} ${qty}`
    + (pos.avg_entry != null ? ` · entry $${Number(pos.avg_entry).toFixed(2)}` : '')
    + (plTxt ? ` · PnL ${plTxt}` : '');
  return `<span class="ai-pos-chip ${cls}" title="${title}">${owner} ${qty}${plTxt ? ' ' + plTxt : ''}</span>`;
}

function _flagsHtml(row) {
  const parts = [];
  if (row.find_it_first) {
    parts.push('<span class="flag-badge flag-badge--first" title="Find-it-first scanner hit — flagged before it was widely trending">🥇 FIRST FIND</span>');
  }
  // NEW: first minute after a mention window opens (fresh attention).
  if ((row.mention_window ?? 0) === 1 && !row.mention_burst) {
    parts.push('<span class="flag-badge flag-badge--new" title="New mention this window">NEW</span>');
  }
  if (row.mention_burst) {
    parts.push('<span class="flag-badge flag-badge--burst" title="Mention burst">🔥BURST</span>');
  }
  return parts.join('');
}

// Price text for a row. Falls back to the scanner's own snapshot when no quote
// exists — OTC names never get one (Alpaca and Finnhub both return empty), so
// the cell would otherwise read "—" all session on exactly the small-caps the
// scanner surfaces. Marked and titled with its age so a snapshot from alert
// time is never mistaken for a live print.
function _priceCell(row) {
  if (row.price != null) return { txt: `$${row.price.toFixed(2)}`, snap: false, age: null };
  if (row.scanner_price != null) {
    return { txt: `$${row.scanner_price.toFixed(2)}`, snap: true,
             age: row.scanner_price_age_sec };
  }
  return { txt: '\u2014', snap: false, age: null };
}

function _snapTitle(age) {
  if (age == null) return 'Scanner snapshot \u2014 no live quote';
  const mins = Math.floor(age / 60);
  const when = mins >= 1 ? `${mins}m ago` : `${Math.round(age)}s ago`;
  return `Scanner snapshot from ${when} \u2014 no live quote for this symbol`;
}

function _rowHTML(row) {
  const p      = _priceCell(row);
  const chgCls = _chgClass(row.pct_change ?? null);
  const volCls = (row.rvol ?? 0) >= 1.5 ? ' vol-high' : '';

  const mentionCls = row.mention_burst  ? ' mentions-burst'
                   : (row.mention_window ?? 0) > 1 ? ' mentions-active' : '';
  const mentionTxt = (row.mention_window ?? 0) > 1 ? `${row.mention_window}` : '';

  return `<div class="watchlist-cols">
    <div class="cell-ticker">
      ${row.ticker}
      ${mentionTxt ? `<span class="mention-badge${mentionCls}" title="${row.mention_count ?? 0} today">${mentionTxt}</span>` : ''}
      ${_aiPosBadge(row.ticker)}
      ${_confluenceBadge(row.confluence)}
    </div>
    <div class="cell-price${p.snap ? ' cell-price--snapshot' : ''}" data-price="${row.ticker}"${p.snap ? ` title="${_snapTitle(p.age)}"` : ''}>${p.txt}</div>
    <div class="cell-chg ${chgCls}" data-chg>${_fmtChg(row.pct_change ?? null)}</div>
    <div class="cell-rvol${volCls}" data-rvol>${_fmtRvol(row.rvol)}</div>
    <div class="cell-vol${volCls}" data-vol>${_fmtVol(row.day_vol)}</div>
    <div class="cell-flags" data-flags>${_flagsHtml(row)}</div>
    <div class="cell-actions">
      <button class="btn-delete" data-delete-btn title="Remove from watchlist">✕</button>
    </div>
  </div>
  ${_signalBarHTML(row)}`;
}

// ── Funnel suggestion banner ───────────────────────────────────
// Renders the auto-ranked pick above the watchlist. Only rebuilds when the
// meaningful content changes (not on every price tick) so the Send button's
// transient state survives, and so it's cheap under the 4Hz snapshot stream.

let _lastFunnelKey = null;

function _fmtMins(m) {
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m` : `${m}m`;
}

function _renderFunnel(_f) {
  // Banner retired — keep the mount point empty if present.
  if (!_suggestEl) return;
  _suggestEl.classList.add('hidden');
  _suggestEl.innerHTML = '';
  _lastFunnelKey = null;
}

async function _sendToMonitors(btn) {
  const sym = btn.dataset.funnelSend;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Sending…';
  selectTicker(sym);   // also point the dashboard's TradingView panel at it
  try {
    await api.addToWBAndTV(sym);
    btn.textContent = '✓ Sent';
  } catch (err) {
    console.error('Failed to send to monitors', err);
    btn.textContent = '! Retry';
    btn.disabled = false;
    return;
  }
  setTimeout(() => {
    if (btn.isConnected) { btn.textContent = orig; btn.disabled = false; }
  }, 2500);
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

function _fmtRvol(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return '—';
  return `${n.toFixed(2)}×`;
}
