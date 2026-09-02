/**
 * feeds.js — Trending (Stocktwits) and AI research panels
 *
 * Both render the same market columns — AI rows carry a thesis line and
 * optional source mark (A/X/AX). Click a row (not the symbol) to add it to
 * the watchlist. Click the symbol name to copy the ticker.
 * Column headers sort the list the same way Momentum Stocks does.
 */

import { subscribe, get } from './store.js?v=134';
import { api }       from './api.js?v=133';
import { copyTicker, isTvClickOpenEnabled } from './tickers.js?v=147';
import { createSymbolMembershipWatcher } from './panelFlash.js?v=136';
import * as notifications from './notifications.js?v=133';

export function init(panelEl, kind) {
  if (!panelEl) return;

  const rowsEl  = panelEl.querySelector(`[data-${kind}-rows]`);
  const countEl = panelEl.querySelector(`[data-${kind}-count]`);
  const stampEl = panelEl.querySelector(`[data-${kind}-stamp]`);
  const errEl   = panelEl.querySelector(`[data-${kind}-error]`);
  // AI Watch is its own main-grid column (not nested under Research).
  const bookSection = kind === 'claude'
    ? document.querySelector('[data-ai-book-section]')
    : null;
  const bookRowsEl = kind === 'claude'
    ? document.querySelector('[data-ai-book-rows]')
    : null;
  const bookCountEl = kind === 'claude'
    ? document.querySelector('[data-ai-book-count]')
    : null;
  const bookStampEl = kind === 'claude'
    ? document.querySelector('[data-ai-book-stamp]')
    : null;
  const bookDayPlEl = kind === 'claude'
    ? document.querySelector('[data-ai-book-day-pl]')
    : null;
  const empty   = kind === 'claude'
    ? 'Waiting for AI research…'
    : kind === 'movers' ? 'Waiting for movers data…'
    : 'Waiting for trending data…';
  const noteSymbols = createSymbolMembershipWatcher();

  // Default sort matches server ranking: trending by score desc, AI by
  // server order (rank) until the user picks a column.
  // Anything that is not the research panel ranks like Trend: score desc.
  // Movers scores off day change, so that puts the biggest movers on top.
  let sortCol   = kind === 'claude' ? 'rank' : 'score';
  let sortDir   = kind === 'claude' ? 1 : -1;   // -1 = desc, 1 = asc
  let lastRows  = [];
  let lastKey   = '';
  let lastPayload = {};
  const headerEls = {};

  panelEl.querySelectorAll('[data-sort-col]').forEach(h => {
    const col = h.dataset.sortCol;
    headerEls[col] = h;
    h.addEventListener('click', () => {
      if (sortCol === col) {
        sortDir *= -1;
      } else {
        sortCol = col;
        // Strings asc; numeric metrics default high-first (desc)
        sortDir = (col === 'ticker' || col === 'src') ? 1 : -1;
      }
      _updateSortHeaders(headerEls, sortCol, sortDir);
      if (lastRows.length) {
        _paint(rowsEl, lastRows, kind, sortCol, sortDir, empty, _aiBook());
      }
    });
  });
  _updateSortHeaders(headerEls, sortCol, sortDir);

  if (bookRowsEl) {
    setInterval(() => {
      bookRowsEl.querySelectorAll('.cell-trail[data-entry-time]').forEach(_tickHoldClock);
    }, 250);
    const _repaintBook = () => _paintBookTable(
      bookSection, bookRowsEl, bookCountEl, bookStampEl, _aiBook(), bookDayPlEl,
    );
    _bindBookSort(_repaintBook);

    // Click a row to have the legend explain THAT name. Open positions get
    // EXIT only; watch rows get ENTRY only (pass/fail marks). Click again to
    // clear. Delegated, because rows are re-rendered on every paint.
    bookRowsEl.addEventListener('click', (ev) => {
      const row = ev.target && ev.target.closest
        ? ev.target.closest('[data-book-symbol]') : null;
      if (!row) return;
      const sym = String(row.dataset.bookSymbol || '');
      _legendFor = (_legendFor === sym) ? '' : sym;
      bookRowsEl.querySelectorAll('[data-book-symbol]').forEach((el) => {
        el.classList.toggle('is-explained',
          !!_legendFor && el.dataset.bookSymbol === _legendFor);
      });
      // Row-click explain needs the legend visible even when the operator
      // keeps it collapsed by default — temporary reveal, preference stays.
      _repaintBook();
    });
    _bindBookLegendToggle(_repaintBook);
  }

  /** Prefer last good wire when store briefly has a clobber/empty book. */
  function _aiBook() {
    return _stableAiBook(get('ai_positions') || {});
  }

  function _refresh(payload) {
    const p    = payload ?? lastPayload ?? {};
    lastPayload = p;
    const rows = Array.isArray(p.rows) ? p.rows : [];
    const book = _aiBook();

    // Hard research errors (e.g. "empty suggestions from model") only matter
    // when the table has nothing to show. If we already have A/X/AX rows —
    // including stale prior research — hide the red banner so a failed reparse
    // on one source does not look like an empty desk.
    // Quote errors stay suppressed here too when rows exist; prices show "-" when
    // missing. Schedule text lives in next_run_label / the stamp line.
    let err = '';
    if (!rows.length) {
      err = p.error || p.quotes_error || '';
    }
    if (errEl) {
      errEl.hidden = !err;
      if (errEl.textContent !== err) errEl.textContent = err;
      errEl.classList.toggle('feed-error--info', _isScheduleNotice(err));
    }

    const countTxt = rows.length ? `${rows.length} ideas` : '';
    if (countEl && countEl.textContent !== countTxt) countEl.textContent = countTxt;
    const stampTxt = _stampLine(p, book);
    if (stampEl) {
      if (stampEl.textContent !== stampTxt) stampEl.textContent = stampTxt;
      if (p._stamp_title) stampEl.title = p._stamp_title;
    }
    if (kind === 'claude') {
      _paintBookTable(
        bookSection, bookRowsEl, bookCountEl, bookStampEl, book, bookDayPlEl,
      );
    }

    // New names or a new research/trend payload → cyan pulse on the Scan tab.
    const rev = rows.map(r =>
      `${r.symbol}:${r.source_mark || ''}:${r.rank ?? ''}:${r.reason || ''}`,
    ).join('|');
    noteSymbols(panelEl, rows.map(r => r.symbol), rev);

    if (!rows.length) {
      lastRows = [];
      if (lastKey !== '∅') {
        const hint = _isScheduleNotice(p.error)
          ? (p.error || empty)
          : empty;
        rowsEl.innerHTML = `<span class="tx-placeholder">${_esc(hint)}</span>`;
        lastKey = '∅';
      }
      return;
    }

    // LOOK badges (EXT near 52w high / WASH near 52w low) — same rules as the
    // terminal desk. Mutates a shallow copy so store payloads stay clean.
    const withLook = applyLookHighlights(rows.map(r => ({ ...r })));

    // Membership + structure only — quote ticks patch cells without this short-circuit.
    // Do not key off live book quotes (that would thrash the research tables).
    const structKey = withLook.map(r =>
      `${r.symbol}:${r.source_mark || ''}:${r.rank ?? ''}:${r.reason || ''}:${r.invalidation || ''}:${r.look ? r.look_reason : ''}`,
    ).join('|') + `|${sortCol}:${sortDir}`;
    lastRows = withLook;
    lastKey = structKey;

    _paint(rowsEl, withLook, kind, sortCol, sortDir, empty, book);
  }

  // AI panel prefers merged ai_suggestions (A/X/AX); store mirrors it onto
  // claude_suggestions for older snapshots.
  const _channel = kind === 'claude' ? 'claude_suggestions'
    : kind === 'movers' ? 'movers'
    : 'trending';
  subscribe(_channel, payload => {
    _refresh(payload ?? {});
  });
  if (kind === 'claude') {
    subscribe('ai_positions', () => _refresh(lastPayload));
    // Momentum ticker ticks → re-paint book PRICE without waiting for the next
    // ai_positions file write (server also overlays stream prices on the wire).
    subscribe('tickers', () => {
      if (bookRowsEl) {
        _paintBookTable(
          bookSection, bookRowsEl, bookCountEl, bookStampEl, _aiBook(), bookDayPlEl,
        );
      }
    });
  }
}

function _posKey(book) {
  const pos = (book && book.positions) || {};
  return Object.keys(pos).sort().map(s => {
    const p = pos[s] || {};
    return `${s}:${p.qty ?? ''}:${p.pl ?? ''}:${p.plpc ?? ''}`;
  }).join(',');
}

function _ageSec(ts) {
  if (ts == null || ts === '') return null;
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return null;
  // Support unix seconds or ms.
  const sec = n > 1e12 ? n / 1000 : n;
  return Math.max(0, Math.round(Date.now() / 1000 - sec));
}

/** Last successful open positions — hold through transient empty wires. */
let _stickyOpenPos = /** @type {Record<string, Object>} */ ({});
/** Last full ai_trader wire (has ``updated``) — ignore clobber / empty frames. */
let _stickyAiBook = /** @type {Object|null} */ (null);

/**
 * Dashboard wire must include ``updated`` (or nested positions). Bare managed
 * maps / {} would flip the stamp live↔stale and clear day P&L + "1 open".
 */
function _stableAiBook(book) {
  const b = book && typeof book === 'object' ? book : {};
  const hasWire = b.updated != null
    || Array.isArray(b.entry_book)
    || (b.positions && typeof b.positions === 'object'
      && !Array.isArray(b.positions)
      && Object.keys(b).some(k =>
        k === 'positions' || k === 'mode' || k === 'book_owner' || k === 'day_pl'));
  // Reject symbol-keyed managed maps: only ticker keys, no wire envelope.
  const keys = Object.keys(b);
  const looksManagedOnly = keys.length > 0
    && b.updated == null
    && !b.positions
    && !b.entry_book
    && keys.every(k => /^[A-Z][A-Z0-9.]*$/i.test(k));
  if (hasWire && !looksManagedOnly) {
    _stickyAiBook = b;
    return b;
  }
  if (_stickyAiBook && _stickyAiBook.updated != null) {
    return _stickyAiBook;
  }
  return b;
}

/**
 * Prefer server entry_book; always merge open positions; fall back to entry_watch.
 * Sticky last-good positions when the wire drops them under a query error so the
 * OPEN row does not blink out for one poll cycle.
 *
 * Live prices: server overlays Finnhub/Alpaca onto entry_book at 4Hz. Client
 * also merges Momentum ``tickers`` prices for dual-listed names so a ticker
 * tick alone re-paints the book without waiting for the next ai_positions write.
 */
function _bookRows(book) {
  let pos = (book && book.positions && typeof book.positions === 'object')
    ? book.positions
    : {};
  const posKeys = Object.keys(pos);
  if (posKeys.length) {
    _stickyOpenPos = { ...pos };
  } else if (Object.keys(_stickyOpenPos).length) {
    const err = String((book && book.error) || '');
    const wireOk = book && book.updated != null;
    // Keep last open book on broker failures OR non-wire clobber frames.
    if (!wireOk || /alpaca|position|query failed|inactive/i.test(err)) {
      pos = _stickyOpenPos;
    } else {
      // Authoritative flat book from a real wire publish.
      _stickyOpenPos = {};
    }
  }
  const by = {};

  // Prefer unified book when the server sent rows (Mom/ST/research + phases).
  const primary = (book && Array.isArray(book.entry_book) && book.entry_book.length)
    ? book.entry_book
    : ((book && Array.isArray(book.entry_watch)) ? book.entry_watch : []);

  for (const w of primary) {
    if (!w || !w.symbol) continue;
    const sym = String(w.symbol).toUpperCase();
    const phase = w.phase
      || (w.ready ? 'ready' : (w.status || 'watching'));
    by[sym] = {
      symbol: sym,
      phase,
      source: w.source || 'research',
      score: w.score,
      // Wire field is rvol (admit-time); keep it so the AI Watch RVOL column
      // is not permanently "—" while PRICE/ZONE paint fine.
      rvol: w.rvol != null ? w.rvol : null,
      reason: w.reason,
      wait_kind: w.wait_kind,
      entry_low: w.entry_low,
      entry_high: w.entry_high,
      zone_kind: w.zone_kind || null,
      in_zone: !!w.in_zone,
      stop_price: w.stop_price != null ? w.stop_price : null,
      local_stop: w.local_stop != null ? w.local_stop : null,
      risk_per_share: w.risk_per_share != null ? w.risk_per_share : null,
      entry_stop_price: w.entry_stop_price != null ? w.entry_stop_price : null,
      trail_give_r: w.trail_give_r != null ? w.trail_give_r : null,
      trail_give_px: w.trail_give_px != null ? w.trail_give_px : null,
      last_ask: w.last_ask,
      last_ask_src: w.last_ask_src || w.price_src || null,
      last_ask_age_sec: w.last_ask_age_sec != null ? w.last_ask_age_sec
        : (w.price_age_sec != null ? w.price_age_sec : null),
      price: w.price != null ? w.price : w.last_ask,
      price_age_sec: w.price_age_sec != null ? w.price_age_sec : null,
      qty: w.qty != null ? w.qty : null,
      pl: w.pl != null ? w.pl : null,
      plpc: w.plpc != null ? w.plpc : null,
      avg_entry: w.avg_entry != null ? w.avg_entry : null,
      is_position: !!w.is_position || phase === 'open',
      ready: !!w.ready || phase === 'ready',
      block_code: w.block_code || null,
      blocker: w.blocker || w.block_reason || null,
      block_reason: w.block_reason || w.blocker || null,
      block_detail: w.block_detail || null,
      exhaustion: w.exhaustion != null ? w.exhaustion : null,
      exhaustion_state: w.exhaustion_state || null,
      pctr: w.pctr != null ? w.pctr : null,
      pctr_slow: w.pctr_slow != null ? w.pctr_slow : null,
      pctr_ob: !!w.pctr_ob,
      pctr_tight: !!w.pctr_tight,
      pctr_gap: w.pctr_gap != null ? w.pctr_gap : null,
      cm_rsi: w.cm_rsi != null ? w.cm_rsi : null,
      cm_rsi_green: !!w.cm_rsi_green,
      cm_rsi_low: !!w.cm_rsi_low,
      cm_rsi_rising: !!w.cm_rsi_rising,
      cm_rsi_src: w.cm_rsi_src || null,
      cm_rsi_age_sec: w.cm_rsi_age_sec != null ? w.cm_rsi_age_sec : null,
      // MACD — the entry lever. This builder rebuilds every row field by
      // field, so anything not listed is dropped before the renderer ever
      // sees it: the wire carried macd_gap for all seven rows and the MACD
      // Gap column still showed "—" on every one. Third whitelist in the
      // same chain (engine -> record -> snapshot -> here); this is the last.
      // Direction rides with size, because a wide gap that is CLOSING is
      // momentum already over and no size field can say so.
      macd_gap: w.macd_gap != null ? w.macd_gap : null,
      macd_hist: w.macd_hist != null ? w.macd_hist : null,
      macd_fast: w.macd_fast != null ? w.macd_fast : null,
      macd_slow: w.macd_slow != null ? w.macd_slow : null,
      macd_sep_ratio: w.macd_sep_ratio != null ? w.macd_sep_ratio : null,
      // Provenance, age and %R direction: the legend cannot say whether a
      // row satisfies FRESH or 'and rising' without them. Fourth field to
      // be added to this chain for exactly that reason.
      macd_src: w.macd_src != null ? w.macd_src : null,
      macd_age_sec: w.macd_age_sec != null ? w.macd_age_sec : null,
      pctr_rising: w.pctr_rising != null ? !!w.pctr_rising : null,
      macd_bull: !!w.macd_bull,
      macd_cross: !!w.macd_cross,
      macd_ok: !!w.macd_ok,
      // null-preserving: false is "not widening", null is "cannot say".
      macd_gap_rising: w.macd_gap_rising != null ? !!w.macd_gap_rising : null,
      macd_gap_falling: w.macd_gap_falling != null ? !!w.macd_gap_falling : null,
      macd_gap_prev: w.macd_gap_prev != null ? w.macd_gap_prev : null,
      pctr_raw: w.pctr_raw != null ? w.pctr_raw : null,
      pctr_src: w.pctr_src || null,
      exh_bars: w.exh_bars != null ? w.exh_bars : null,
      exh_window_min: w.exh_window_min != null ? w.exh_window_min : null,
      exh_hh: w.exh_hh != null ? w.exh_hh : null,
      exh_ll: w.exh_ll != null ? w.exh_ll : null,
    };
  }
  // Live positions always win (P&L / qty).
  for (const [symRaw, p] of Object.entries(pos)) {
    const sym = String(symRaw || '').toUpperCase();
    if (!sym || !p) continue;
    const prev = by[sym] || { symbol: sym, source: 'position' };
    by[sym] = {
      ...prev,
      phase: 'open',
      is_position: true,
      price: p.current ?? p.current_price ?? prev.price,
      last_ask: p.current ?? prev.last_ask,
      qty: p.qty,
      avg_entry: p.avg_entry,
      pl: p.pl,
      plpc: p.plpc,
      local_stop: p.local_stop != null ? p.local_stop
        : (p.local_stop_price != null ? p.local_stop_price : prev.local_stop),
      peak_price: p.peak_price != null ? p.peak_price : prev.peak_price,
      min_hold_left_sec: p.min_hold_left_sec != null ? p.min_hold_left_sec
        : prev.min_hold_left_sec,
      min_hold_active: p.min_hold_active != null ? p.min_hold_active
        : prev.min_hold_active,
      min_hold_sec: p.min_hold_sec != null ? p.min_hold_sec : prev.min_hold_sec,
      entry_time: p.entry_time != null ? p.entry_time : prev.entry_time,
      stop_price: p.stop_price != null ? p.stop_price : prev.stop_price,
      risk_per_share: p.risk_per_share != null ? p.risk_per_share : prev.risk_per_share,
      entry_stop_price: p.entry_stop_price != null ? p.entry_stop_price : prev.entry_stop_price,
      trail_give_r: p.trail_give_r != null ? p.trail_give_r : prev.trail_give_r,
      trail_give_px: p.trail_give_px != null ? p.trail_give_px : prev.trail_give_px,
    };
  }

  // Overlay Momentum desk live prints + day % onto book PRICE (open rows keep
  // broker mark for the number; day % still colours the cell when known).
  const live = _tickerQuoteMap();
  for (const r of Object.values(by)) {
    if (!r) continue;
    const q = live[r.symbol];
    if (q) {
      if (!(r.is_position || r.phase === 'open')) {
        if (q.price != null && Number.isFinite(q.price) && q.price > 0) {
          r.price = q.price;
          r.last_ask = q.price;
        }
      }
      if (q.pct_change != null && Number.isFinite(q.pct_change)) {
        r.pct_change = q.pct_change;
      }
    }
    // RStop is the live shelf on an OPEN long only. Watches must not show
    // last − give (that sits above the zone and looks like an exit).
    const isOpen = !!(r.is_position || r.phase === 'open' || r.phase === 'submitted'
      || r.status === 'filled' || r.status === 'submitted');
    if (!isOpen) {
      r.local_stop = null;
      const sk = String(r.symbol || '').toUpperCase();
      if (sk) delete _rstopHigh[sk];
    }
    // Open longs: keep the engine shelf. Do not recompute last − give.
  }
  return Object.values(by);
}

/** last − give (give_r × R, else computed trail_give_px). Never below floor. */
function _liveLocalStop(r, last) {
  if (!r || last == null || !Number.isFinite(last) || last <= 0) {
    const prev = r && r.local_stop != null ? Number(r.local_stop) : NaN;
    return Number.isFinite(prev) && prev > 0 ? prev : null;
  }
  let give = r.trail_give_px != null ? Number(r.trail_give_px) : NaN;
  if (!Number.isFinite(give) || give <= 0) {
    const giveR = r.trail_give_r != null ? Number(r.trail_give_r) : 0.10;
    let risk = r.risk_per_share != null ? Number(r.risk_per_share) : NaN;
    if (!Number.isFinite(risk) || risk <= 0) {
      const lo = r.entry_low != null ? Number(r.entry_low) : NaN;
      const st = r.entry_stop_price != null ? Number(r.entry_stop_price)
        : (r.stop_price != null ? Number(r.stop_price) : NaN);
      if (Number.isFinite(lo) && Number.isFinite(st) && lo > st) risk = lo - st;
    }
    if (Number.isFinite(risk) && risk > 0 && Number.isFinite(giveR)) {
      give = Math.max(0.01, giveR * risk);
    } else if (Number.isFinite(giveR) && giveR > 0) {
      give = Math.max(0.01, giveR * last / 100);
    } else {
      give = 0.01;
    }
  }
  // Never use the watch-plan stop_price as floor — it sits under the
  // live shelf and makes RSTOP look like it dropped (LFS 3.19 → 2.95).
  const floorRaw = r.entry_stop_price != null ? r.entry_stop_price : null;
  const floor = floorRaw != null ? Number(floorRaw) : NaN;
  let want = last - give;
  if (want >= last) want = last - 0.01;
  if (Number.isFinite(floor) && floor > 0) want = Math.max(floor, want);
  const prev = r.local_stop != null ? Number(r.local_stop) : NaN;
  if (Number.isFinite(prev) && prev > 0) want = Math.max(want, prev);
  return want;
}

/** symbol → { price, pct_change } from the Momentum tickers store slice. */
function _tickerQuoteMap() {
  const rows = get('tickers') || [];
  const out = Object.create(null);
  if (!Array.isArray(rows)) return out;
  for (const t of rows) {
    if (!t) continue;
    const s = String(t.ticker || t.symbol || '').toUpperCase();
    if (!s) continue;
    const px = Number(t.price);
    const pct = t.pct_change != null ? Number(t.pct_change) : NaN;
    out[s] = {
      price: Number.isFinite(px) && px > 0 ? px : null,
      pct_change: Number.isFinite(pct) ? pct : null,
    };
  }
  return out;
}

/** Day-change colour class — same rules as Momentum Stocks. */
function _bookChgClass(pct) {
  if (pct == null || !Number.isFinite(Number(pct))) return '';
  const n = Number(pct);
  if (n > 0) return 'chg-pos';
  if (n < 0) return 'chg-neg';
  return '';
}

/** Last painted price per book symbol — drives up/down flash like Momentum. */
const _bookPrevPrices = Object.create(null);
const _bookPrevExh = Object.create(null);

/** Stable display order — phase groups, then symbol (no score shuffle). */
const _PHASE_RANK = { open: 0, ready: 1, submitted: 2, filled: 2, watching: 3 };

/** Numeric value behind each sortable book column, or null when unknown.
 *
 *  Deliberately reads the same raw fields the CELLS read, not the formatted
 *  text: "$16.95", "99.2% OB" and "+0.007 (1.1×)" do not sort as numbers, and
 *  a column that sorted by its own label would put $9 after $10. */
function _bookSortVal(r, col) {
  if (col === 'rsi') {
    const v = r && r.cm_rsi != null ? Number(r.cm_rsi) : null;
    return Number.isFinite(v) ? v : null;
  }
  const num = (v) => (v != null && Number.isFinite(Number(v)) ? Number(v) : null);
  switch (col) {
    case 'last':
      return num(r.price) ?? num(r.last_trade) ?? num(r.last_ask);
    case 'entry': {
      const open = !!(r.is_position || r.phase === 'open' || r.phase === 'submitted'
        || r.status === 'filled' || r.status === 'submitted');
      return open ? (num(r.avg_entry) ?? num(r.entry_price)) : null;
    }
    case 'stop':
      return num(_bookStopPx(r));
    case 'exh': {
      const ex = num(r.exhaustion);
      if (ex != null) return ex;
      const p = num(r.pctr);
      return p == null ? null : Math.max(0, Math.min(100, 100 + p));
    }
    case 'macd':
      return num(r.macd_gap) ?? num(r.macd_hist);
    case 'pl':
      return num(r.pl);
    default:
      return null;
  }
}

/** Book ordering. `col` null = the default: phase first (open, then ready,
 *  then the rest), ticker within phase — the ordering that answers "what am I
 *  in and what is about to fire" without being asked.
 *
 *  When the operator picks a column, that column wins outright and phase is
 *  NOT used as a pre-sort: the point of clicking MACD GAP is to see the whole
 *  book in gap order, and grouping by phase first would silently defeat it.
 *  Unknowns always sink to the bottom in either direction — a name with no
 *  reading is not the best or the worst, and floating "—" to the top of a
 *  descending sort is how a blank column looks like a leader. */
function _sortBookRows(rows, sort) {
  const s = sort || _bookSort;
  const list = [...rows];
  const byTicker = (a, b) =>
    String(a.symbol || '').localeCompare(String(b.symbol || ''));

  if (!s || !s.col) {
    return list.sort((a, b) => {
      const pa = _PHASE_RANK[String(a.phase || 'watching')] ?? 9;
      const pb = _PHASE_RANK[String(b.phase || 'watching')] ?? 9;
      return pa !== pb ? pa - pb : byTicker(a, b);
    });
  }

  const dir = s.dir < 0 ? -1 : 1;
  if (s.col === 'ticker') {
    return list.sort((a, b) => dir * byTicker(a, b));
  }
  if (s.col === 'state') {
    return list.sort((a, b) => {
      const pa = _PHASE_RANK[String(a.phase || 'watching')] ?? 9;
      const pb = _PHASE_RANK[String(b.phase || 'watching')] ?? 9;
      return pa !== pb ? dir * (pa - pb) : byTicker(a, b);
    });
  }
  return list.sort((a, b) => {
    const av = _bookSortVal(a, s.col);
    const bv = _bookSortVal(b, s.col);
    if (av == null && bv == null) return byTicker(a, b);
    if (av == null) return 1;      // unknowns sink, both directions
    if (bv == null) return -1;
    return av === bv ? byTicker(a, b) : dir * (av - bv);
  });
}

/** Active book sort. col null = default phase ordering. */
const _bookSort = { col: null, dir: -1 };

const _BOOK_SORT_LS = 'aiBookSort';

(function _restoreBookSort() {
  try {
    const raw = localStorage.getItem(_BOOK_SORT_LS);
    if (!raw) return;
    const v = JSON.parse(raw);
    if (v && typeof v.col === 'string') {
      _bookSort.col = v.col;
      _bookSort.dir = v.dir < 0 ? -1 : 1;
    }
  } catch (e) { /* private mode / blocked storage: keep the default */ }
})();

function _bookSortHeaders() {
  return document.querySelectorAll('.ai-book-table-header [data-book-sort-col]');
}

/** Same indicator the Scan tables use — _updateSortHeaders appends " ↑"/" ↓"
 *  to the label and flags .th--sorted for the accent colour. Reused rather
 *  than reimplemented so the two tables cannot drift apart: one arrow glyph,
 *  one highlight rule, one place to change either. */
function _paintBookSortHeaders() {
  const map = {};
  _bookSortHeaders().forEach(h => { map[h.dataset.bookSortCol] = h; });
  _updateSortHeaders(map, _bookSort.col, _bookSort.dir);
  _bookSortHeaders().forEach(h => {
    h.setAttribute('aria-sort', _bookSort.col === h.dataset.bookSortCol
      ? (_bookSort.dir > 0 ? 'ascending' : 'descending')
      : 'none');
  });
}

/** Click cycles desc → asc → default. The third state matters: the default
 *  (open positions first) is the one an operator actually watches, and a sort
 *  you cannot leave is a sort that hides your open risk behind a reload. */
function _bindBookSort(repaint) {
  _bookSortHeaders().forEach(h => {
    const col = h.dataset.bookSortCol;
    h.setAttribute('role', 'columnheader');
    h.setAttribute('tabindex', '0');
    const hit = () => {
      if (_bookSort.col !== col) {
        _bookSort.col = col;
        // Text asc (A→Z reads naturally); numbers high-first.
        _bookSort.dir = (col === 'ticker' || col === 'state') ? 1 : -1;
      } else if ((_bookSort.dir < 0 && col !== 'ticker' && col !== 'state')
        || (_bookSort.dir > 0 && (col === 'ticker' || col === 'state'))) {
        _bookSort.dir *= -1;
      } else {
        _bookSort.col = null;
        _bookSort.dir = -1;
      }
      try {
        if (_bookSort.col) {
          localStorage.setItem(_BOOK_SORT_LS, JSON.stringify(_bookSort));
        } else {
          localStorage.removeItem(_BOOK_SORT_LS);
        }
      } catch (e) { /* storage blocked: the sort still works this session */ }
      _paintBookSortHeaders();
      repaint();
    };
    h.addEventListener('click', hit);
    h.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); hit(); }
    });
  });
  _paintBookSortHeaders();
}

/** Optional duel line in day-P&L strip title / equity area. */
function _duelSummary(book) {
  const d = book && book.duel;
  if (!d || !d.enabled) return '';
  const phase = d.phase || 'trial';
  const w = d.winner === 'anthropic' ? 'A' : d.winner === 'xai' ? 'X' : null;
  const sc = d.score || {};
  const rA = sc.anthropic && sc.anthropic.realized_r;
  const rX = sc.xai && sc.xai.realized_r;
  const lead = d.close_before_research_min != null ? d.close_before_research_min : 10;
  const tot = d.totals || {};
  const tA = tot.anthropic;
  const tX = tot.xai;
  if (phase === 'trial') {
    const cuts = Array.isArray(d.window_cuts) ? d.window_cuts.join(',') : (d.trial_end || '');
    return `duel · flat ${lead}m pre-research · cuts ${cuts}`;
  }
  if (phase === 'scored' || phase === 'chance3') {
    const rs = [
      Number.isFinite(Number(tA)) ? `A Σ${Number(tA) >= 0 ? '+' : ''}${Number(tA).toFixed(2)}R` :
        (Number.isFinite(Number(rA)) ? `A ${Number(rA) >= 0 ? '+' : ''}${Number(rA).toFixed(2)}R` : 'A —'),
      Number.isFinite(Number(tX)) ? `X Σ${Number(tX) >= 0 ? '+' : ''}${Number(tX).toFixed(2)}R` :
        (Number.isFinite(Number(rX)) ? `X ${Number(rX) >= 0 ? '+' : ''}${Number(rX).toFixed(2)}R` : 'X —'),
    ].join(' · ');
    return w ? `duel ${rs} · ${w} wins C3` : `duel ${rs} · tie`;
  }
  if (phase === 'done') return 'duel done';
  return `duel ${phase}`;
}

/** Account day P&L strip at top of AI Watch (equity − last_equity). */
function _paintBookDayPl(dayPlEl, book) {
  if (!dayPlEl) return;
  const acct = (book && book.account && typeof book.account === 'object')
    ? book.account
    : {};
  const meta = (book && book.watch_meta) || {};
  let dayPl = book && book.day_pl != null ? book.day_pl : acct.day_pl;
  if (dayPl == null) dayPl = meta.day_pl;
  let dayPct = book && book.day_pl_pct != null ? book.day_pl_pct : acct.day_pl_pct;
  if (dayPct == null) dayPct = meta.day_pl_pct;
  const equity = acct.equity != null ? acct.equity : meta.equity;

  const valEl = dayPlEl.querySelector('[data-ai-book-day-pl-value]');
  const pctEl = dayPlEl.querySelector('[data-ai-book-day-pl-pct]');
  const eqEl = dayPlEl.querySelector('[data-ai-book-day-pl-eq]');

  const n = Number(dayPl);
  const hasPl = Number.isFinite(n);
  const sign = hasPl && n > 0 ? '+' : '';
  const valTxt = hasPl
    ? `${sign}$${Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(2)}`
    : '—';
  if (valEl && valEl.textContent !== valTxt) valEl.textContent = valTxt;
  if (valEl) {
    valEl.classList.toggle('chg-pos', hasPl && n > 0);
    valEl.classList.toggle('chg-neg', hasPl && n < 0);
  }

  const pctN = Number(dayPct);
  const hasPct = Number.isFinite(pctN);
  const pctTxt = hasPct
    ? `${pctN > 0 ? '+' : ''}${pctN.toFixed(2)}%`
    : '';
  if (pctEl && pctEl.textContent !== pctTxt) pctEl.textContent = pctTxt;
  if (pctEl) {
    pctEl.classList.toggle('chg-pos', hasPct && pctN > 0);
    pctEl.classList.toggle('chg-neg', hasPct && pctN < 0);
  }

  const eqN = Number(equity);
  const eqTxt = Number.isFinite(eqN) && eqN > 0
    ? `$${eqN >= 1000 ? Math.round(eqN).toLocaleString() : eqN.toFixed(2)}`
    : '—';
  if (eqEl && eqEl.textContent !== eqTxt) eqEl.textContent = eqTxt;

  const duelLine = _duelSummary(book);

  const title = [
    'Alpaca account day P&L (equity − last close equity)',
    hasPl ? `today ${valTxt}` : null,
    hasPct ? pctTxt : null,
    eqTxt || null,
    duelLine || null,
    book && book.mode ? String(book.mode) : null,
  ].filter(Boolean).join(' · ');
  if (dayPlEl.title !== title) dayPlEl.title = title;
}

/**
 * AI Watch table — surgical DOM like Momentum/Research.
 * Never wipe the whole list on membership churn (that flashed open rows away).
 * Quotes / P&L patch in place; add/remove/reorder only the rows that changed.
 */

/**
 * Fire the position sounds on real open/close transitions.
 *
 * Diffs against the SETTLED book, never the raw wire: _bookRows already holds
 * the last-good positions through a transient empty frame, and a naive diff
 * would announce a close every time a poll dropped the map — which it does.
 * A symbol has to actually leave a populated book to count as closed.
 *
 * The close sound follows the last P&L the row carried, so a losing exit does
 * not get a winning chime. Primed on first paint so a page refresh with
 * positions already open is silent.
 */
let _prevOpenSyms = null;                 // null = not primed yet
const _lastPl = /** @type {Record<string, number>} */ ({});

function _announcePositions(rows) {
  const open = rows.filter(r => r && r.phase === 'open' && r.ticker);
  const now = new Set(open.map(r => String(r.ticker).toUpperCase()));
  for (const r of open) {
    const v = r.pl != null ? Number(r.pl) : (r.plpc != null ? Number(r.plpc) : null);
    if (v != null && Number.isFinite(v)) _lastPl[String(r.ticker).toUpperCase()] = v;
  }
  if (_prevOpenSyms === null) { _prevOpenSyms = now; return; }   // prime, silent
  try {
    for (const sym of now) {
      if (!_prevOpenSyms.has(sym)) notifications.positionOpened();
    }
    for (const sym of _prevOpenSyms) {
      if (!now.has(sym)) {
        const pl = _lastPl[sym];
        notifications.positionClosed(Number.isFinite(pl) ? pl > 0 : false);
        delete _lastPl[sym];
      }
    }
  } catch (e) {
    console.warn('[feeds] position sound failed', e);
  }
  _prevOpenSyms = now;
}

/** Which book row the legend is explaining, or '' for the plain rules. */
let _legendFor = '';

/** Legend panel open/closed. Default collapsed — the ENTRY/EXIT block eats
 *  vertical space the book needs; criteria still paint as green cells. */
const _BOOK_LEGEND_LS = 'aiBookLegendOpen';

function _legendExpanded() {
  try {
    return localStorage.getItem(_BOOK_LEGEND_LS) === '1';
  } catch (e) {
    return false;
  }
}

function _setLegendExpanded(open) {
  try {
    if (open) localStorage.setItem(_BOOK_LEGEND_LS, '1');
    else localStorage.removeItem(_BOOK_LEGEND_LS);
  } catch (e) { /* private mode: session-only */ }
}

function _syncLegendToggleBtn() {
  const btn = document.querySelector('[data-ai-book-legend-toggle]');
  if (!btn) return;
  const open = _legendExpanded() || !!_legendFor;
  btn.setAttribute('aria-pressed', open ? 'true' : 'false');
  btn.classList.toggle('btn--active', _legendExpanded());
  btn.title = _legendExpanded()
    ? 'Hide entry/exit criteria legend'
    : 'Show entry/exit criteria legend';
  btn.textContent = 'Criteria';
}

function _bindBookLegendToggle(repaint) {
  const btn = document.querySelector('[data-ai-book-legend-toggle]');
  if (!btn || btn.dataset.bound === '1') return;
  btn.dataset.bound = '1';
  btn.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    _setLegendExpanded(!_legendExpanded());
    _syncLegendToggleBtn();
    if (typeof repaint === 'function') repaint();
  });
  _syncLegendToggleBtn();
}

/** Shared ENTRY pass evaluation — legend marks and per-cell crit--pass.
 *  Provenance-first, same knobs as the arm gate. null = unjudgeable. */
function _bookEntryCriteria(cfg, row) {
  const c = cfg && typeof cfg === 'object' ? cfg : {};
  const n = (k, d) => {
    const v = Number(c[k]);
    return Number.isFinite(v) ? v : d;
  };
  const s = (k, d) => {
    const v = c[k];
    return (v === undefined || v === null || v === '') ? d : String(v);
  };
  const b = (k, d) => {
    const v = c[k];
    return (v === undefined || v === null) ? !!d : !!v;
  };
  const r = row && typeof row === 'object' ? row : null;
  const num = (v) => (v == null || !Number.isFinite(Number(v)) ? null : Number(v));

  let macd = null, exh = null, both = null, fresh = null, live = null, rsi = null;
  if (r) {
    // PROVENANCE FIRST. The gate refuses a MACD not drawn on the live tape
    // before it looks at any of the rules below, so a legend that scored the
    // rules without it would tick a row the gate never reached — PATH showed
    // EITHER as satisfied while its State said "MACD not live". A reading
    // that cannot be used is not a rule that passes; it is a rule that could
    // not be judged.
    const src = String(r.macd_src || '').toLowerCase();
    live = src ? src === 'realtime' : null;

    const gap = num(r.macd_gap), sep = num(r.macd_sep_ratio);
    const falling = r.macd_gap_falling;
    macd = live !== true ? null
      : (gap == null || sep == null) ? null
      : (gap > n('macd_min_gap', 0.005) && sep >= n('macd_sep_mult', 1.5)
         && falling !== true);

    const ex = num(r.exhaustion), rising = r.pctr_rising;
    // Same switch exhaustion_allows_buy uses: rules off → not a gate in force.
    if (!n('ai_watch_exhaustion_rules', 1)) {
      exh = null;
    } else {
      exh = ex == null ? null
        : ((ex >= n('ai_watch_exhaustion_heat_min_pct', 40) && rising === true)
           || ex >= n('ai_watch_ob_flat_min_pct', 99));
    }

    // OR, matching the gate: either leg alone earns the bypass — but the
    // override sits BEHIND the provenance check, so an unusable MACD makes
    // the whole branch unreachable regardless of which leg is strong.
    const _exhLeg = ex == null ? null
      : (ex >= n('ai_watch_macd_exh_override_min_pct', 70) && rising === true);
    const _macdLeg = r.macd_gap_rising == null ? null : r.macd_gap_rising === true;
    both = live !== true ? null
      : (_exhLeg === true || _macdLeg === true) ? true
      : (_exhLeg == null || _macdLeg == null) ? null : false;

    // RSI leg. Reads the SAME three knobs cm_rsi_allows_buy does, so the
    // legend cannot drift from the gate: band, the turn, and the deep-OS
    // waiver. Note the gate fails CLOSED on a missing reading — so a null
    // here is "no reading", which the gate treats as a refusal, and the row
    // is marked unjudgeable rather than passing.
    const rv = num(r.cm_rsi);
    if (!n('ai_watch_arm_require_cm_rsi', 0)) {
      rsi = null;                       // switched off: not a rule in force
    } else if (rv == null) {
      rsi = false;                      // no_rsi_data — the gate refuses this
    } else if (rv > n('ai_watch_arm_cm_rsi_max', 50)
               || rv < n('ai_watch_arm_cm_rsi_min', 0)) {
      rsi = false;
    } else if (r.cm_rsi_rising === true) {
      rsi = true;
    } else if (r.cm_rsi_rising == null) {
      rsi = null;                       // level fine, direction unknown
    } else {
      const fb = n('ai_watch_arm_cm_rsi_allow_falling_below', 0);
      rsi = (fb > 0 && rv < fb && r.pctr_rising === true);
    }

    const pAge = num(r.price_age_sec), mAge = num(r.macd_age_sec);
    fresh = live === false ? false
      : (pAge == null || mAge == null) ? null
      : (pAge <= n('ai_watch_decision_max_age_sec', 8)
         && mAge <= n('ai_watch_macd_max_age_sec', 30));
  }

  const ready = !!(r && (r.ready
    || String(r.phase || '').toLowerCase() === 'ready'));

  return { macd, exh, both, fresh, live, rsi, ready, n, s, b };
}

/** Same open-row test _bookRows / RStop use: is_position or open/submitted. */
function _legendRowIsOpen(r) {
  if (!r || typeof r !== 'object') return false;
  const phase = String(r.phase || '').toLowerCase();
  const status = String(r.status || '').toLowerCase();
  return !!(r.is_position || phase === 'open' || phase === 'submitted'
    || status === 'filled' || status === 'submitted');
}

/** Entry/exit rules, rendered from the LIVE config rather than written down.
 *
 *  Hardcoding the numbers here would produce a legend that drifts from the
 *  thresholds it claims to describe — the same failure as a config knob
 *  nothing reads, and this desk has hit that three times in one session.
 *  Every value below comes off the config the server actually loaded.
 *
 *  With a row selected each rule is also EVALUATED against it, so the panel
 *  answers "what is this name missing" rather than only "what is required".
 *  A rule whose inputs are absent reads UNKNOWN, never PASS — the desk's own
 *  rule that absence is not a pass.
 *
 *  Section choice follows the row: an OPEN position (is_position / phase open)
 *  shows EXIT only; a watch-only row shows ENTRY only. Criteria with no row
 *  selected defaults to ENTRY only — not the full both-block.
 */
function _paintBookLegend(cfg, row) {
  const el = document.querySelector('[data-ai-book-legend]');
  if (!el) return;
  _syncLegendToggleBtn();
  // Collapsed by default. A selected row still expands so explain works.
  const show = _legendExpanded() || !!_legendFor;
  el.classList.toggle('ai-book-legend--collapsed', !show);
  if (!show) return;

  const c = cfg && typeof cfg === 'object' ? cfg : {};
  if (!Object.keys(c).length) return;
  const { macd, exh, both, fresh, rsi, n, s, b } = _bookEntryCriteria(cfg, row);
  const r = row && typeof row === 'object' ? row : null;
  const isOpen = _legendRowIsOpen(r);
  // Open → EXIT only. Watch / no selection → ENTRY only.
  const showEntry = !isOpen;
  const showExit = isOpen;

  // ENTRY. Evaluated against the selected row where the inputs exist.
  const entry = [
    ['MACD',  `gap &gt; ${n('macd_min_gap', 0.005)} &nbsp;·&nbsp; sep ≥ ${n('macd_sep_mult', 1.0)}× &nbsp;·&nbsp; opening`, macd],
    ['EXH',   n('ai_watch_exhaustion_rules', 1)
                ? `≥ ${n('ai_watch_exhaustion_heat_min_pct', 40)}% and rising &nbsp;·&nbsp; or ≥ ${n('ai_watch_ob_flat_min_pct', 99)}% pinned`
                : 'not required', exh],
    ['EITHER', `EXH ≥ ${n('ai_watch_macd_exh_override_min_pct', 70)}% rising OR MACD rising = override`, both],
    ['RSI',   n('ai_watch_arm_require_cm_rsi', 0)
                ? `CM RSI-2 rising${n('ai_watch_arm_cm_rsi_max', 100) < 100
                    ? ` &nbsp;·&nbsp; ${n('ai_watch_arm_cm_rsi_min', 0)}–${n('ai_watch_arm_cm_rsi_max', 100)} band` : ''}`
                  + `${n('ai_watch_arm_cm_rsi_allow_falling_below', 0) > 0
                    ? ` &nbsp;·&nbsp; or falling under ${n('ai_watch_arm_cm_rsi_allow_falling_below', 0)} with EXH rising` : ''}`
                : 'not required', rsi],
    // Provenance is a gate, not a footnote: %R and RSI are refused outright
    // when they did not come off the live tape, so the legend has to say so
    // or it describes a looser desk than the one running.
    ['FRESH', `price ≤ ${n('ai_watch_decision_max_age_sec', 15)}s &nbsp;·&nbsp; MACD ≤ ${n('ai_watch_macd_max_age_sec', 60)}s`
                + `${b('ai_watch_require_realtime_macd', 0) ? ' on the live tape' : ' (REST ok)'}`
                + `${b('ai_watch_require_live_pctr', 0) ? ' &nbsp;·&nbsp; %R live' : ''}`
                + `${b('ai_watch_require_realtime_rsi', 0) ? ' &nbsp;·&nbsp; RSI realtime' : ''}`, fresh],
    ['SETUP', `R:R ≥ ${n('ai_min_reward_risk', 0.5)} &nbsp;·&nbsp; stop ≥ ${n('ai_watch_min_stop_pct', 1.5)}% `
                + `&nbsp;·&nbsp; 1R = ${n('ai_watch_synth_stop_pct', 5)}% of price`, null],
    ['ARM',   `${n('ai_watch_arm_confirm_ticks', 1)} agreeing polls &nbsp;·&nbsp; ${n('ai_exit_min_hold_sec', 0)}s min hold after fill`, null],
    // The brakes. These refuse before price is looked at, so a row can clear
    // every rule above and still not open.
    ['BRAKE', `≤ ${n('ai_watch_max_entries_per_symbol_day', 0) || '∞'}/name/day &nbsp;·&nbsp; ${n('ai_reentry_cooldown_sec', 0)}s cooldown`
                + `${b('ai_dead_reentry_block', 0)
                    ? ` &nbsp;·&nbsp; no re-entry after a red exit under ${n('ai_reentry_min_mfe_r', 0.5)}R` : ''}`
                + ` &nbsp;·&nbsp; stop at −${n('ai_daily_loss_limit_r', 0)}R`, null],
    ['SIZE',  `${s('ai_entry_order_style', 'limit')} &nbsp;·&nbsp; ${n('ai_risk_pct', 1)}% risk &nbsp;·&nbsp; ${n('ai_max_positions', 0)} open`
                + ` &nbsp;·&nbsp; ${n('ai_max_buys_per_poll', 0)}/poll &nbsp;·&nbsp; abort ${n('ai_fill_abort_r', 0)}R through`, null],
  ];

  // EXIT. Informational — these describe an open position.
  //
  // The shelf is the one line that must be COMPUTED rather than quoted. The
  // give is min(give_r × R, give_max_pct% of price) and R is synth_stop_pct%
  // of price, so the cap converts to give_max_pct/synth_stop_pct in R and the
  // smaller of the two is what actually trails. Printing give_max_pct alone
  // said "0.5%" while the shelf was really a flat 0.10R, and printing give_r
  // alone said 0.2R — both true about a knob, neither true about the desk.
  const _capR = n('ai_watch_synth_stop_pct', 5) > 0
    ? n('ai_local_trail_give_max_pct', 0) / n('ai_watch_synth_stop_pct', 5)
    : 0;
  const _giveR = n('ai_local_trail_give_max_pct', 0) > 0
    ? Math.min(n('ai_local_trail_give_r', 0), _capR)
    : n('ai_local_trail_give_r', 0);
  const exit = [
    ['SHELF', b('ai_local_trail_enabled', 1)
                ? `peak − ${(Math.round(_giveR * 1000) / 1000)}R`
                  + ` &nbsp;(min of give ${n('ai_local_trail_give_r', 0)}R, cap ${n('ai_local_trail_give_max_pct', 0)}% of price)`
                  + ` &nbsp;·&nbsp; arms at ${n('ai_local_trail_arm_r', 0)}R`
                : 'off', null],
    ['STOP',  `${b('ai_broker_stop_enabled', 0) ? 'broker stop' : 'software'} 1R`
                + ` &nbsp;=&nbsp; ${n('ai_watch_synth_stop_pct', 5)}% under entry`, null],
    ['BE',    `floor at fill +$${n('ai_breakeven_offset_px', 0)} once ${n('ai_local_trail_be_at_r', 0)}R or ${n('ai_local_trail_be_at_pct', 0)}%`, null],
    ['DEAD',  `${n('ai_dead_trade_min', 0)}m held with MFE under ${n('ai_dead_trade_mfe_r', 0)}R`, null],
    ['STALE', `no live quote for ${n('ai_stale_data_max_age_sec', 15)}s → close`, null],
    ['EOD',   b('ai_eod_liquidate_enabled', 1) ? `flatten at ${s('ai_eod_liquidate_time', '15:50')}` : 'no EOD flatten', null],
  ];

  const head = r
    ? `<div class="lg-head">${_esc(String(r.symbol || ''))} — ${isOpen ? 'EXIT' : 'ENTRY'} · tap row again to clear</div>`
    : '';
  const paint = (list) => list.map(([k, v, ok]) => {
    const cls = ok === true ? ' lg-pass' : ok === false ? ' lg-fail' : '';
    const mark = !r || ok == null ? '' :
      `<span class="lg-mark">${ok ? '✓' : '✗'}</span>`;
    return `<div class="lg-row${cls}"><span class="lg-k">${k}</span>`
      + `<span class="lg-v">${v}</span>${mark}</div>`;
  }).join('');
  let html = head;
  if (showEntry) html += '<div class="lg-sec">ENTRY — all must pass</div>' + paint(entry);
  if (showExit) html += '<div class="lg-sec">EXIT — any one fires</div>' + paint(exit);
  if (el.innerHTML !== html) el.innerHTML = html;
}

function _paintBookTable(sectionEl, rowsEl, countEl, stampEl, book, dayPlEl) {
  if (!rowsEl) return;
  const rows = _sortBookRows(_bookRows(book));
  try {
    _paintBookLegend(get('config'),
      _legendFor ? rows.find(x => String(x.symbol || '') === _legendFor) : null);
  } catch (e) { /* legend is never load-bearing */ }
  _announcePositions(rows);
  const nOpen = rows.filter(r => r && r.phase === 'open').length;
  const nReady = rows.filter(r => r && r.phase === 'ready').length;
  const countTxt = rows.length
    ? (nOpen
      ? `${rows.length} · ${nOpen} open`
      : (nReady ? `${rows.length} · ${nReady} ready` : `${rows.length}`))
    : '';
  if (countEl && countEl.textContent !== countTxt) countEl.textContent = countTxt;
  _paintBookDayPl(dayPlEl, book);
  const owner = _ownerLabel(book);
  const mode = String((book && book.mode) || '').toLowerCase();
  const meta = (book && book.watch_meta) || {};
  const ageSec = _ageSec(book && book.updated);
  const pollSec = meta.poll_sec != null ? Number(meta.poll_sec) : 20;
  const nMom = meta.n_momentum != null
    ? Number(meta.n_momentum)
    : rows.filter(r => _bookSourceLabel(r.source) === 'Mom').length;
  const nSt = meta.n_trending != null
    ? Number(meta.n_trending)
    : rows.filter(r => _bookSourceLabel(r.source) === 'ST').length;
  // Live if wire updated within ~3 poll cycles; stale means recheck stalled.
  const live = ageSec != null && ageSec < Math.max(45, pollSec * 3);
  // Bucket age so the stamp does not rewrite every second (less chrome flicker).
  const ageBucket = ageSec == null ? null
    : (ageSec < 5 ? '<5s' : ageSec < 15 ? '<15s' : `${Math.round(ageSec / 10) * 10}s`);
  // Keep stamp short so the panel header stays one line; detail lives in title.
  const stampTxt = live ? '● live' : '○ stale';
  if (stampEl) {
    if (stampEl.textContent !== stampTxt) stampEl.textContent = stampTxt;
    stampEl.classList.toggle('feed-stamp--live', live);
    stampEl.classList.toggle('feed-stamp--stale', !live);
    const detail = [
      ageBucket != null ? `updated ${ageBucket}` : null,
      `recheck ~${Math.round(pollSec)}s`,
      nMom ? `${nMom} Mom` : null,
      nSt ? `${nSt} ST` : null,
      owner,
      mode || null,
    ].filter(Boolean).join(' · ');
    stampEl.title = live
      ? detail
      : `Stale (${ageSec != null ? ageSec + 's' : '?'}) — book thread may be down · ${detail}`;
  }

  if (sectionEl) {
    sectionEl.hidden = false;
    sectionEl.classList.toggle('ai-book--has-open', nOpen > 0);
  }

  if (!rows.length) {
    // Hold open rows through a one-tick empty wire (broker blip / race).
    if (rowsEl.querySelector('.feed-row--ai-open')) return;
    if (!rowsEl.querySelector('.tx-placeholder')) {
      rowsEl.innerHTML = '<span class="tx-placeholder">No watches or open AI positions…</span>';
    }
    return;
  }

  const ph = rowsEl.querySelector('.tx-placeholder');
  if (ph) ph.remove();

  const existing = /** @type {Map<string, HTMLElement>} */ (new Map());
  rowsEl.querySelectorAll('[data-book-symbol]').forEach(el => {
    existing.set(String(el.dataset.bookSymbol || '').toUpperCase(), el);
  });

  const ordered = [];
  for (const r of rows) {
    const sym = String(r.symbol || '').toUpperCase();
    if (!sym) continue;
    let el = existing.get(sym);
    if (el) {
      _updateBookRow(el, r, owner);
    } else {
      el = _createBookRow(r, owner);
      existing.set(sym, el);
    }
    ordered.push(el);
  }

  const keep = new Set(ordered.map(el =>
    String(el.dataset.bookSymbol || '').toUpperCase()));
  existing.forEach((el, sym) => {
    if (!keep.has(sym)) el.remove();
  });

  const children = [...rowsEl.querySelectorAll('[data-book-symbol]')];
  const needsReorder = ordered.length !== children.length
    || ordered.some((el, i) => children[i] !== el);
  if (needsReorder) {
    ordered.forEach(el => rowsEl.appendChild(el));
  }
}

function _createBookRow(r, owner) {
  const wrap = document.createElement('div');
  wrap.innerHTML = _bookRowHtml(r, owner).trim();
  const el = /** @type {HTMLElement} */ (wrap.firstElementChild);
  const sym = String((r && r.symbol) || '').toUpperCase();
  el.addEventListener('click', () => _add(el, sym));
  const statusEl0 = el.querySelector('.ai-book-status');
  if (statusEl0) {
    statusEl0.title = _bookBlockerTitle(r);
  }
  const tickerCell = el.querySelector('.cell-ticker');
  if (tickerCell) {
    tickerCell.title = isTvClickOpenEnabled()
      ? `Load ${sym} into TradingView`
      : `Copy ${sym}`;
    tickerCell.addEventListener('click', e => {
      e.stopPropagation();
      copyTicker(e.currentTarget, sym);
    });
  }
  return el;
}

/** True when Last is too old to arm — STATE must say so, never "buy".
 *  Src/age only — a leftover block_code=stale_quote must not stick after
 *  the tape recovers (2026-08-25 locked the whole book that way). */
function _bookTapeStale(r) {
  if (!r) return false;
  const src = String(r.last_ask_src || r.price_src || '').toLowerCase().trim();
  if (src === 'stale_tape' || src === 'none') return true;
  const age = r.last_ask_age_sec != null ? Number(r.last_ask_age_sec)
    : (r.price_age_sec != null ? Number(r.price_age_sec) : NaN);
  const raw = Number(r.decision_max_age_sec);
  const maxAge = Number.isFinite(raw) && raw > 0 ? raw : 8;
  return Number.isFinite(age) && age > maxAge;
}

const _MACD_BLOCKER_LABELS = {
  'macd_bearish': 'MACD bear',
  'macd_gap_too_close': 'MACD narrow',
  'macd_gap_insufficient': 'MACD gap low',
  'no_macd_data': 'no MACD',
  'macd_no_recent_cross': 'wait cross',
  'macd_bullish_gap': 'ready',
};

const _MACD_BLOCKER_DESCRIPTIONS = {
  'macd_bearish': 'MACD Bearish: Fast line is below slow signal line (bullish crossover required)',
  'macd_gap_too_close': 'MACD Narrow: Fast/slow separation gap is too close (< 0.005 minimum)',
  'macd_gap_insufficient': 'MACD Gap Low: Line separation is under 0.8× rolling standard deviation',
  'no_macd_data': 'No MACD: Real-time 1-minute bar MACD calculation not available yet',
  'macd_no_recent_cross': 'Wait Cross: Bullish crossover has not occurred within the confirm window',
};

/** Status column shows *why we are not long* (blocker), not READY/WATCH. */
function _bookBlockerLabel(r) {
  if (!r) return '—';
  const phase = String(r.phase || '').toLowerCase();
  if (phase === 'open' || r.is_position) {
    if (_shelfHit(r) && _holdLeft(r) == null) return 'open · SELL';
    return 'open';
  }
  if (phase === 'submitted') return 'sent';
  if (_bookTapeStale(r) && phase !== 'open') return 'stale quote';
  const b = String(r.blocker || r.block_reason || '').trim();
  const code = String(r.block_code || '').trim().toLowerCase();
  const detail = String(r.block_detail || '').trim();

  if (_MACD_BLOCKER_LABELS[code]) {
    return _MACD_BLOCKER_LABELS[code];
  }

  // A real refuse (heat low, dead today) wins over leftover "in zone" / ready.
  if (b && !(r.ready) && !['in zone', 'in_zone', 'buy', 'ready'].includes(b.toLowerCase()) && code !== 'in_zone') {
    return _MACD_BLOCKER_LABELS[b.toLowerCase()] || b;
  }
  if (code && !r.ready && !['in_zone', 'placing'].includes(code)) {
    return _MACD_BLOCKER_LABELS[code] || b || code.replace(/_/g, ' ');
  }
  // Armable, not filled — never say "buy" here (that read as an open).
  if (r.ready || phase === 'ready') return 'ready';
  if (b) return _MACD_BLOCKER_LABELS[b.toLowerCase()] || b;
  if (detail) return detail;
  return 'watching';
}

function _bookBlockerTitle(r) {
  const code = String((r && r.block_code) || '').trim().toLowerCase();
  if (_MACD_BLOCKER_DESCRIPTIONS[code]) {
    return _MACD_BLOCKER_DESCRIPTIONS[code];
  }
  const label = _bookBlockerLabel(r);
  const detail = String((r && r.block_detail) || '').trim();
  const parts = [];
  if (label && label !== '—') parts.push(label);
  if (detail && detail.toLowerCase() !== label.toLowerCase()) parts.push(detail);
  if (code && !parts.some(p => p.toLowerCase().includes(code.replace(/_/g, ' ')))) {
    parts.push(code.replace(/_/g, ' '));
  }
  return parts.join(' — ');
}

function _bookBlockerClass(r) {
  const phase = String((r && r.phase) || '').toLowerCase();
  if (phase === 'open' || (r && r.is_position)) return 'ai-book-status ai-book-status--open';
  if (phase === 'submitted') return 'ai-book-status ai-book-status--sent';
  const label = _bookBlockerLabel(r).toLowerCase();
  if (r && r.ready && (label === 'ready' || label === 'buy' || label === 'in zone' || label === 'placing' || label === 'placing…')) {
    return 'ai-book-status ai-book-status--ready';
  }
  if (label && label !== 'watching' && label !== '—') {
    return 'ai-book-status ai-book-status--blocked';
  }
  return 'ai-book-status';
}

function _updateBookRow(el, r) {
  if (!el || !r) return;
  // A call-out can land after the row is already on the book — Bro names a
  // symbol momentum seeded minutes ago — and this path patches cells rather
  // than re-rendering, so the badge has to be added (and removed, once the
  // call ages out of the freshness window) here too.
  const tickerCell = el.querySelector('.cell-ticker');
  if (tickerCell) {
    const badge = tickerCell.querySelector('.bro-badge');
    if (r.bro_call && !badge) {
      const span = document.createElement('span');
      span.className = 'bro-badge';
      span.title = 'Trader Bro called this one out';
      span.textContent = 'BRO';
      tickerCell.appendChild(span);
    } else if (!r.bro_call && badge) {
      badge.remove();
    }
  }
  const phase = String((r && r.phase) || 'watching').toLowerCase();
  const isOpen = phase === 'open' || r.is_position;
  const statusLabel = _bookBlockerLabel(r);
  const crit = _bookEntryCriteria(get('config'), r);
  const statusEl = el.querySelector('.ai-book-status');
  if (statusEl && statusEl.textContent !== statusLabel) statusEl.textContent = statusLabel;
  if (statusEl) {
    // STATE: ready, or FRESH when the tape ages are clean (no dedicated column).
    statusEl.className = _bookBlockerClass(r)
      + ((crit.ready || crit.fresh === true) ? ' crit--pass' : '');
    statusEl.title = _bookBlockerTitle(r);
  }
  const trail = _fmtStopCell(r);
  const rawPx = r.price != null && Number.isFinite(Number(r.price))
    ? Number(r.price)
    : (r.last_ask != null && Number.isFinite(Number(r.last_ask))
      ? Number(r.last_ask) : null);
  const px = rawPx != null ? `$${rawPx.toFixed(2)}` : '—';
  const qty = isOpen ? _fmtQty(r.qty) : '—';
  const entryTxt = _fmtEntry(r);
  const pl = isOpen ? _fmtPl(r) : '—';
  const entryEl = el.querySelector('.cell-entry');
  if (entryEl) _setText(entryEl, entryTxt);
  const trailEl = el.querySelector('.cell-trail') || el.querySelector('.cell-src');
  if (trailEl) {
    trailEl.classList.remove('cell-src');
    trailEl.classList.add('cell-trail');
    trailEl.classList.toggle('is-held', _holdLeft(r) != null);
    trailEl.classList.toggle('is-hit', _shelfHit(r));
    trailEl.title = _stopCellTitle(r);
    const shelfNow = _bookStopPx(r);
    const symStop = String(r.symbol || '').toUpperCase();
    const prevShelf = trailEl._prevShelf;
    if (shelfNow != null && prevShelf != null && shelfNow > prevShelf + 1e-9) {
      trailEl.classList.remove('price-flash--down');
      trailEl.classList.add('price-flash--up');
      clearTimeout(trailEl._flashTimer);
      trailEl._flashTimer = setTimeout(() => trailEl.classList.remove(
        'price-flash--up'), 600);
    }
    if (shelfNow != null) trailEl._prevShelf = shelfNow;
    // The countdown runs in THIS cell now, so it carries the clock attrs —
    // and the stop price it should reveal when the clock hits zero, since
    // the 250ms ticker fires between book ticks and would otherwise blank
    // the cell until the next update.
    const et = r.entry_time != null ? Number(r.entry_time) : NaN;
    const cap = r.min_hold_sec != null ? Number(r.min_hold_sec) : NaN;
    if (Number.isFinite(et) && et > 1e9 && Number.isFinite(cap) && cap > 0) {
      trailEl.dataset.entryTime = String(et);
      trailEl.dataset.minHoldSec = String(cap);
      trailEl.dataset.stopText = _fmtTrail(_bookStopPx(r));
    } else {
      delete trailEl.dataset.entryTime;
      delete trailEl.dataset.minHoldSec;
      delete trailEl.dataset.stopText;
    }
    _setText(trailEl, trail);
  }
  const priceEl = el.querySelector('.cell-price');
  if (priceEl) {
    // Same key the row HTML seeds under (upper), or the first tick after a row
    // is created reads an undefined prev and swallows its flash.
    const symKey = String(r.symbol || '').toUpperCase();
    const prev = _bookPrevPrices[symKey];
    if (rawPx != null && prev !== undefined && prev !== rawPx) {
      // Drop the opposite direction first — successive ticks can flip inside one
      // flash window (mirrors Momentum in tickers.js).
      const dir = rawPx > prev ? 'up' : 'down';
      priceEl.classList.remove('price-flash--up', 'price-flash--down');
      priceEl.classList.add(`price-flash--${dir}`);
      clearTimeout(priceEl._flashTimer);
      // 140ms rise + 600ms hold + 900ms decay from the base rule.
      priceEl._flashTimer = setTimeout(() => priceEl.classList.remove(
        'price-flash--up', 'price-flash--down'), 600);
    }
    if (rawPx != null) _bookPrevPrices[symKey] = rawPx;
    if (priceEl.textContent !== px) priceEl.textContent = px;
    // Steady day-change colour (same chg-pos / chg-neg as Momentum CHG%).
    const chgMod = _bookChgClass(r.pct_change);
    priceEl.classList.toggle('chg-pos', chgMod === 'chg-pos');
    priceEl.classList.toggle('chg-neg', chgMod === 'chg-neg');
  }
  const rsiEl = el.querySelector('.cell-rsi');
  if (rsiEl) {
    _setText(rsiEl, _bookRsiText(r));
    rsiEl.className = `cell-rsi${_rsiPairClass(r)}${crit.rsi === true ? ' crit--pass' : ''}`;
    const rsiTip = _fmtRsiTitle(r);
    if (rsiTip) rsiEl.title = rsiTip;
  }
  const exhEl = el.querySelector('.cell-exh');
  if (exhEl) {
    _setText(exhEl, _bookExhText(r));
    exhEl.className = `cell-exh${_bookExhClass(r)}${crit.exh === true ? ' crit--pass' : ''}`;
    const exhTip = _fmtExhTitle(r);
    if (exhTip) exhEl.title = exhTip;
  }
  const macdEl = el.querySelector('.cell-macd');
  if (macdEl) {
    _setText(macdEl, _bookMacdText(r));
    macdEl.className = `cell-macd${_bookMacdClass(r)}${crit.macd === true ? ' crit--pass' : ''}`;
    const tip = _fmtMacdTitle(r);
    if (tip) macdEl.title = tip;
  }
  _setText(el.querySelector('.cell-qty'), qty);
  const plEl = el.querySelector('.cell-pl');
  if (plEl) {
    if (plEl.textContent !== pl) plEl.textContent = pl;
    plEl.className = `cell-pl ${isOpen ? _plClass(r) : ''}`.trim();
  }
  el.classList.toggle('feed-row--ai-open', isOpen);
  el.classList.toggle('feed-row--ai-ready', phase === 'ready' || statusLabel === 'ready' || statusLabel === 'buy');
  if (el.title) el.title = '';
}

function _bookRowHtml(r) {
  const sym = String((r && r.symbol) || '').toUpperCase();
  if (!sym) return '';
  const phase = String((r && r.phase) || 'watching').toLowerCase();
  const isOpen = phase === 'open' || r.is_position;
  const statusLabel = _bookBlockerLabel(r);
  const crit = _bookEntryCriteria(get('config'), r);
  const statusCls = _bookBlockerClass(r)
    + ((crit.ready || crit.fresh === true) ? ' crit--pass' : '');
  const trail = _fmtStopCell(r);
  const rawPx = r.price != null && Number.isFinite(Number(r.price))
    ? Number(r.price)
    : (r.last_ask != null && Number.isFinite(Number(r.last_ask))
      ? Number(r.last_ask) : null);
  const px = rawPx != null ? `$${rawPx.toFixed(2)}` : '—';
  if (rawPx != null && _bookPrevPrices[sym] === undefined) {
    _bookPrevPrices[sym] = rawPx;
  }
  const chgMod = _bookChgClass(r.pct_change);
  const qty = isOpen ? _fmtQty(r.qty) : '—';
  const pl = isOpen ? _fmtPl(r) : '—';
  const plCls = isOpen ? _plClass(r) : '';
  const rowCls = isOpen
    ? 'ticker-row feed-row feed-row--ai-book feed-row--ai-open'
    : ((phase === 'ready' || statusLabel === 'ready' || statusLabel === 'buy')
      ? 'ticker-row feed-row feed-row--ai-book feed-row--ai-ready'
      : 'ticker-row feed-row feed-row--ai-book');
  return `<div class="${rowCls}" data-book-symbol="${_esc(sym)}" data-feed-symbol="${_esc(sym)}">`
    + `<div class="feed-cols feed-cols--ai-book">`
    + `<div class="cell-ticker">${_esc(sym)}${r.bro_call
      ? `<span class="bro-badge" title="Trader Bro called this one out">BRO</span>`
      : ''}</div>`
    + `<div class="${statusCls}" title="${_esc(_bookBlockerTitle(r))}">${_esc(statusLabel)}</div>`
    + `<div class="cell-price${chgMod ? ` ${chgMod}` : ''}" data-price="${_esc(sym)}">${_esc(px)}</div>`
    + `<div class="cell-entry">${_esc(_fmtEntry(r))}</div>`
    + `<div class="cell-trail${_holdLeft(r) != null ? ' is-held' : ''}${_shelfHit(r) ? ' is-hit' : ''}" title="${_esc(_stopCellTitle(r))}"${_holdDataAttrs(r)}>${_esc(trail)}</div>`
    + `<div class="cell-rsi${_rsiPairClass(r)}${crit.rsi === true ? ' crit--pass' : ''}"${_fmtRsiTitle(r) ? ` title="${_esc(_fmtRsiTitle(r))}"` : ''}>${_esc(_bookRsiText(r))}</div>`
    + `<div class="cell-exh${_bookExhClass(r)}${crit.exh === true ? ' crit--pass' : ''}"${_fmtExhTitle(r) ? ` title="${_esc(_fmtExhTitle(r))}"` : ''}>${_esc(_bookExhText(r))}</div>`
    + `<div class="cell-macd${_bookMacdClass(r)}${crit.macd === true ? ' crit--pass' : ''}"${_fmtMacdTitle(r) ? ` title="${_esc(_fmtMacdTitle(r))}"` : ''}>${_esc(_bookMacdText(r))}</div>`
    + `<div class="cell-qty">${_esc(qty)}</div>`
    + `<div class="cell-pl ${plCls}">${_esc(pl)}</div>`
    + `</div></div>`;
}

const _rstopHigh = Object.create(null);

function _bookStopPx(r) {
  if (!r) return null;
  const sym = String(r.symbol || '').toUpperCase();
  const open = !!(r.is_position || r.phase === 'open' || r.phase === 'submitted'
    || r.status === 'filled' || r.status === 'submitted');
  if (!open) {
    if (sym) delete _rstopHigh[sym];
    return null;
  }
  const a = r.local_stop != null ? Number(r.local_stop) : NaN;
  const live = Number.isFinite(a) && a > 0 ? a : null;
  const prev = _rstopHigh[sym];
  if (live != null) {
    const v = (prev != null && live < prev) ? prev : live;
    _rstopHigh[sym] = v;
    return v;
  }
  return prev != null ? prev : null;
}

/** Average fill price — open positions only. A watch row has no entry yet, and
 *  showing the zone's planned entry there would read as a fill that never
 *  happened. */
function _fmtEntry(r) {
  if (!r) return '—';
  const open = !!(r.is_position || r.phase === 'open' || r.phase === 'submitted'
    || r.status === 'filled' || r.status === 'submitted');
  if (!open) return '—';
  const v = r.avg_entry != null ? Number(r.avg_entry)
    : (r.entry_price != null ? Number(r.entry_price) : NaN);
  return Number.isFinite(v) && v > 0 ? `$${v.toFixed(2)}` : '—';
}

function _fmtTrail(v) {
  const n = Number(v);
  return v != null && Number.isFinite(n) && n > 0 ? `$${n.toFixed(2)}` : '—';
}

/** The shelf is SOFTWARE and, while ai_exit_min_hold_sec is armed, the sale
 *  is deliberately muzzled. Shown because the bare number reads as a broken
 *  stop: the shelf keeps ratcheting up while held, so it drifts further above
 *  the print the longer it works correctly. On 2026-08-24 the desk suppressed
 *  237 exits on one position exactly as designed and it looked like a
 *  failure every time the panel was checked. */
function _holdLeft(r) {
  if (!r) return null;
  const open = !!(r.is_position || r.phase === 'open' || r.phase === 'submitted'
    || r.status === 'filled' || r.status === 'submitted');
  if (!open) return null;
  const et = r.entry_time != null ? Number(r.entry_time) : NaN;
  const cap = r.min_hold_sec != null ? Number(r.min_hold_sec) : NaN;
  if (Number.isFinite(et) && et > 1e9 && Number.isFinite(cap) && cap > 0) {
    const left = cap - (Date.now() / 1000 - et);
    return left > 0 ? left : null;
  }
  const v = r.min_hold_left_sec != null ? Number(r.min_hold_left_sec) : NaN;
  return Number.isFinite(v) && v > 0 ? v : null;
}

function _fmtHoldClock(sec) {
  const n = Math.max(0, Math.ceil(sec));
  const m = Math.floor(n / 60);
  const s = String(n % 60).padStart(2, '0');
  return `${m}:${s}`;
}


function _holdDataAttrs(r) {
  // Includes the stop price the cell falls back to at zero — see
  // _tickHoldClock. Kept in one place so the create and update paths cannot
  // disagree about what the cell shows when the clock runs out.
  const et = r && r.entry_time != null ? Number(r.entry_time) : NaN;
  const cap = r && r.min_hold_sec != null ? Number(r.min_hold_sec) : NaN;
  if (!Number.isFinite(et) || et < 1e9 || !Number.isFinite(cap) || cap <= 0) {
    return '';
  }
  return ` data-entry-time="${et}" data-min-hold-sec="${cap}"`
    + ` data-stop-text="${_esc(_fmtTrail(_bookStopPx(r)))}"`;
}

function _tickHoldClock(el) {
  if (!el) return;
  const et = Number(el.dataset.entryTime);
  const cap = Number(el.dataset.minHoldSec);
  if (!Number.isFinite(et) || !Number.isFinite(cap) || cap <= 0) return;
  const left = cap - (Date.now() / 1000 - et);
  const hit = el.classList.contains('is-hit');
  // Zero hands the cell back to the stop price. `armed` was right when this
  // drove a separate Hold column; in the Stop cell it would blank the one
  // number the operator needs the instant the sale becomes possible.
  let text = el.dataset.stopText || '—';
  if (left > 0) {
    text = hit ? `HIT ${_fmtHoldClock(left)}` : _fmtHoldClock(left);
    el.classList.add('is-held');
  } else {
    el.classList.remove('is-held');
    if (hit && text !== '—') text = `${text} · SELL`;
  }
  if (el.textContent !== text) el.textContent = text;
}


function _lastPx(r) {
  const v = r && (r.price != null ? Number(r.price)
    : (r.last_ask != null ? Number(r.last_ask) : NaN));
  return Number.isFinite(v) && v > 0 ? v : null;
}

function _shelfHit(r) {
  const last = _lastPx(r);
  const shelf = _bookStopPx(r);
  return last != null && shelf != null && last <= shelf + 1e-9;
}

/** The Stop cell does double duty.
 *
 *  While min-hold is running the ratchet cannot SELL — the shelf still
 *  raises, but a sale is muzzled — so the number that matters is how long
 *  is left, not where the shelf currently sits. Once the clock reaches zero
 *  the stop price takes the cell back. That is one column instead of two
 *  saying different halves of the same fact, and it puts the countdown
 *  where the operator is already looking when a position is open. */
function _fmtStopCell(r) {
  const left = _holdLeft(r);
  if (left != null) {
    return _shelfHit(r) ? `HIT ${_fmtHoldClock(left)}` : _fmtHoldClock(left);
  }
  const shelf = _bookStopPx(r);
  const px = _fmtTrail(shelf);
  if (px === '—') return px;
  if (_shelfHit(r)) return `${px} · SELL`;
  return px;
}

function _stopCellTitle(r) {
  const last = _lastPx(r);
  const shelf = _bookStopPx(r);
  const peak = r && r.peak_price != null ? Number(r.peak_price) : NaN;
  const bits = [
    'Ratchet shelf (software). Raises with the print, never lowers.',
    'Sells a market order when LAST ≤ shelf — after min-hold if that is on.',
  ];
  if (last != null) bits.push(`last $${last.toFixed(2)}`);
  if (shelf != null) bits.push(`shelf $${shelf.toFixed(2)}`);
  if (Number.isFinite(peak) && peak > 0) bits.push(`peak $${peak.toFixed(2)}`);
  const left = _holdLeft(r);
  if (left != null) {
    bits.push(`sale HELD ${Math.ceil(left)}s (5m min-hold). Shelf still raises.`);
  } else if (_shelfHit(r)) {
    bits.push('LAST is through the shelf — selling.');
  } else {
    bits.push('sale armed: next tag of the shelf exits.');
  }
  return bits.join(' · ');
}

function _zonePx(x) {
  const n = Number(x);
  if (!Number.isFinite(n) || n <= 0) return null;
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(2);
  return n.toFixed(3);
}

function _fmtZone(r) {
  const lo = r && r.entry_low != null ? Number(r.entry_low) : NaN;
  const hi = r && r.entry_high != null ? Number(r.entry_high) : NaN;
  const a = _zonePx(Math.min(lo, hi));
  const b = _zonePx(Math.max(lo, hi));
  if (!a || !b) return '—';
  return `${a}–${b}`;
}

function _zoneWhere(r) {
  const lo = r && r.entry_low != null ? Number(r.entry_low) : NaN;
  const hi = r && r.entry_high != null ? Number(r.entry_high) : NaN;
  const last = r && (r.price != null ? Number(r.price)
    : (r.last_ask != null ? Number(r.last_ask) : NaN));
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo <= 0 || hi <= 0
      || !Number.isFinite(last) || last <= 0) {
    return '';
  }
  const top = Math.max(lo, hi);
  const bot = Math.min(lo, hi);
  if (last > top) return 'above';
  if (last < bot) return 'below';
  return 'in';
}

function _zoneClass(r) {
  const w = _zoneWhere(r);
  return w ? `zone--${w}` : '';
}

function _fmtZoneTitle(r) {
  const band = _fmtZone(r);
  if (band === '—') return '';
  const kind = String((r && r.zone_kind) || '').replace(/_/g, ' ');
  const where = _zoneWhere(r);
  const last = r && (r.price != null ? Number(r.price)
    : (r.last_ask != null ? Number(r.last_ask) : NaN));
  const lastS = Number.isFinite(last) && last > 0 ? `last $${last.toFixed(2)}` : '';
  return [kind || 'zone', band, lastS, where].filter(Boolean).join(' · ');
}

function _bookSourceLabel(source) {
  const s = String(source || '').toLowerCase();
  if (s === 'momentum' || s === 'mom') return 'Mom';
  if (s === 'trending' || s === 'stocktwits' || s === 'st') return 'ST';
  if (s === 'xai' || s === 'grok') return 'X';
  if (s === 'anthropic' || s === 'claude') return 'A';
  if (s === 'research') return 'AI';
  if (s === 'position') return 'Pos';
  if (!s) return '—';
  return s.slice(0, 4);
}

/** Exhaustion as "72%↑" — level and direction together, because the level
 *  alone cannot tell "pinned at the highs and rolling over" from "climbing
 *  into them". OB marks the overbought band. */
function _fmtExh(v, state, src) {
  if (src === 'sparse_window' && (v == null || !Number.isFinite(Number(v)))) {
    return 'thin';
  }
  const n = Number(v);
  if (v == null || !Number.isFinite(n)) return '—';
  const mark = state === 'overbought' ? ' OB'
    : state === 'heating' ? '\u2191'
    : state === 'cooling' ? '\u2193' : '';
  return `${n.toFixed(1)}%${mark}`;
}

function _fmtPr(v) {
  const n = Number(v);
  if (v == null || !Number.isFinite(n)) return '—';
  return n.toFixed(1);
}

/** TradingView-style pair: fast / slow on the %R scale (0 at the top). */
/** The book's EXH cell: the 0-100 exhaustion the gate compares, with its
 *  direction — _fmtExh already renders exactly that, so this only picks the
 *  arguments off a book row and fills in the scale when the snapshot carried
 *  the raw line but not the derived percentage.
 *
 *  It used to print "fast / slow". The slow line is a 112-bar window and the
 *  book's names carry a few dozen bars — 0 of 14 live records had one at
 *  09:42 — so half the column was a permanent em dash, and the half that
 *  mattered was drawn on a scale (-6.5) that does not match the threshold the
 *  operator sets (40). Both raw lines stay in the hover.
 */
function _bookExhText(r) {
  if (!r) return '—';
  let ex = r.exhaustion;
  if ((ex == null || !Number.isFinite(Number(ex)))
      && r.pctr != null && Number.isFinite(Number(r.pctr))) {
    ex = Math.max(0, Math.min(100, 100 + Number(r.pctr)));
  }
  return _fmtExh(ex, r.exhaustion_state, r.pctr_src);
}

/** Colour for the EXH cell, off the same state the arrow comes from.
 *
 *  The styles (exh--ob / exh--up / exh--down) survived the column's removal
 *  in styles.css; only the function that selects them did not, so this
 *  restores the pairing rather than inventing a new one. `thin` gets no
 *  class: a reading the desk could not take should not be coloured as
 *  though it had an opinion. */
function _bookExhClass(r) {
  if (!r) return '';
  if (r.pctr_src === 'sparse_window' && r.exhaustion == null) return '';
  const state = String(r.exhaustion_state || '').toLowerCase();
  if (state === 'overbought') return ' exh--ob';
  if (state === 'heating') return ' exh--up';
  if (state === 'cooling') return ' exh--down';
  return '';
}

/** "live" means a rolling %R over a clock window, recomputed against the live
 *  print — the number on a chart. Anything else (clock_range, sparse_window)
 *  is position-in-range over whatever bars existed, which is a different
 *  measurement wearing the same column. Marked so it is obvious at a glance
 *  which readings the desk should be trusted to act on. */
function _exhStale(r) {
  if (!r) return false;
  if (_bookTapeStale(r)) return true;
  const src = String(r.pctr_src || '').toLowerCase().trim();
  return src !== '' && src !== 'live';
}

function _exhPairClass(r) {
  let cls = _exhClass(r && r.exhaustion_state);
  if (r && r.pctr_ob) cls += ' exh--ob';
  if (r && r.pctr_tight) cls += ' exh--tight';
  if (_exhStale(r)) cls += ' exh--stale';
  return cls;
}

function _bookMacdText(r) {
  if (!r) return '—';
  const gap = r.macd_gap ?? r.macd_hist;
  if (gap == null || !Number.isFinite(Number(gap))) return '—';
  const n = Number(gap);
  const sign = n >= 0 ? '+' : '';
  // ▲/▼ is the SIGN of the gap — bullish or bearish. It says nothing about
  // whether the lines are still separating, so opening/closing gets its own
  // glyph: ↗ widening, ↘ closing. A wide gap that is closing is momentum
  // already over, and reading the sign arrow as direction is the exact
  // confusion this pair removes.
  const arrow = n > 0 ? '▲ ' : (n < 0 ? '▼ ' : '');
  const dir = r.macd_gap_rising ? ' ↗' : (r.macd_gap_falling ? ' ↘' : '');
  const ratio = r.macd_sep_ratio != null && Number.isFinite(Number(r.macd_sep_ratio))
    ? ` (${Number(r.macd_sep_ratio).toFixed(1)}×)`
    : '';
  return `${arrow}${sign}${n.toFixed(3)}${ratio}${dir}`;
}

function _bookMacdClass(r) {
  if (!r) return '';
  const gap = r.macd_gap ?? r.macd_hist;
  if (gap == null || !Number.isFinite(Number(gap))) return '';
  const n = Number(gap);
  const ratio = Number(r.macd_sep_ratio || 0);
  if (n > 0) {
    if (ratio >= 0.8 || n >= 0.015) return ' macd--wide';
    return ' macd--bull';
  }
  if (Math.abs(n) <= 0.002) return ' macd--narrow';
  return ' macd--bear';
}

function _fmtMacdTitle(r) {
  if (!r) return 'MACD — no reading';
  const fast = r.macd_fast ?? r.macd_line;
  const slow = r.macd_slow ?? r.macd_signal;
  const gap = r.macd_gap ?? r.macd_hist;
  const bits = ['MACD Momentum'];
  if (r.macd_src) bits.push(`src: ${r.macd_src}`);
  if (r.macd_age_sec != null && Number.isFinite(Number(r.macd_age_sec))) {
    bits.push(`age: ${Number(r.macd_age_sec).toFixed(1)}s`);
  }
  if (fast != null && Number.isFinite(Number(fast))) bits.push(`Fast: ${Number(fast).toFixed(4)}`);
  if (slow != null && Number.isFinite(Number(slow))) bits.push(`Slow: ${Number(slow).toFixed(4)}`);
  if (gap != null && Number.isFinite(Number(gap))) bits.push(`Gap: ${Number(gap) >= 0 ? '+' : ''}${Number(gap).toFixed(4)}`);
  if (r.macd_sep_ratio != null && Number.isFinite(Number(r.macd_sep_ratio))) bits.push(`Sep: ${Number(r.macd_sep_ratio).toFixed(2)}x std`);
  // Say the direction in words, with the previous value behind it — the
  // glyph in the cell is small and "closing" is the reading that stops an
  // entry, so it should not depend on spotting an arrow.
  if (r.macd_gap_rising || r.macd_gap_falling) {
    const prev = (r.macd_gap_prev != null && Number.isFinite(Number(r.macd_gap_prev)))
      ? ` (was ${Number(r.macd_gap_prev) >= 0 ? '+' : ''}${Number(r.macd_gap_prev).toFixed(4)})`
      : '';
    bits.push(`Gap ${r.macd_gap_rising ? 'OPENING' : 'CLOSING'}${prev}`);
  } else if (r.macd_gap_rising === null && r.macd_gap_falling === null) {
    bits.push('Gap direction unknown');
  }
  if (r.macd_bull) bits.push('Bullish');
  if (r.macd_cross) bits.push('Recent Bull Cross');
  if (r.macd_ok) bits.push('Wide Separation Confirmed');
  return bits.join(' · ');
}

function _fmtRsi(r) {
  if (!r || r.cm_rsi == null || !Number.isFinite(Number(r.cm_rsi))) return '—';
  return Number(r.cm_rsi).toFixed(1);
}

/** The book's RSI cell: CM RSI-2 with its direction, because the entry rule
 *  is a band AND a turn — "trending up from 0 to 50" — and a bare level
 *  answers only half of it. Arrow is the engine's cm_rsi_rising (RSI-2 now
 *  against RSI-2 trend_lookback bars back). */
function _bookRsiText(r) {
  const v = _fmtRsi(r);
  if (v === '—') return v;
  return `${v}${r && r.cm_rsi_rising ? '↑' : '↓'}`;
}

/** True when this reading satisfies the arm condition on its own: inside the
 *  0-50 band and turning up. Also paints when RSI is still falling but deeply
 *  washed out (<20) while EXH is heating toward overbought — matches
 *  ai_watch_arm_cm_rsi_allow_falling_below. */
function _rsiArms(r) {
  if (!r || r.cm_rsi == null || !Number.isFinite(Number(r.cm_rsi))) return false;
  const v = Number(r.cm_rsi);
  if (v < 0 || v > 50) return false;
  if (r.cm_rsi_rising) return true;
  return v < 20 && String(r.exhaustion_state || '').toLowerCase() === 'heating';
}

/** The engine draws its bars from the Finnhub trade stream when the tape is
 *  covering a name and falls back to Alpaca REST when it is not — and it
 *  flips per ticker, mid-session. A reading off the fallback is not wrong,
 *  but it is not the live tape either, so it is marked rather than blended
 *  in with the ones that are. */
function _rsiStale(r) {
  if (!r) return false;
  if (_bookTapeStale(r)) return true;
  const src = String(r.cm_rsi_src || '').toLowerCase().trim();
  return src !== '' && src !== 'realtime';
}

function _rsiClass(r) {
  if (!r) return '';
  if (_rsiArms(r)) return ' rsi--arm';
  if (r.cm_rsi_green) return ' rsi--green';
  if (r.cm_rsi_low) return ' rsi--low';
  return '';
}

function _rsiPairClass(r) {
  let cls = _rsiClass(r);
  if (_rsiStale(r)) cls += ' rsi--stale';
  return cls;
}

function _fmtRsiTitle(r) {
  if (!r) return 'CM RSI-2';
  const bits = ['CM RSI-2'];
  if (r.cm_rsi != null && Number.isFinite(Number(r.cm_rsi))) {
    bits.push(Number(r.cm_rsi).toFixed(1));
  } else {
    return 'CM RSI-2 — no reading';
  }
  bits.push(r.cm_rsi_rising ? 'rising' : 'not rising');
  if (_rsiArms(r)) {
    if (!r.cm_rsi_rising && Number(r.cm_rsi) < 20) {
      bits.push('deep OS + EXH heating (falling RSI allowed)');
    } else {
      bits.push('in the 0-50 arm band');
    }
  } else {
    bits.push('outside the arm band');
  }
  const src = String(r.cm_rsi_src || '').toLowerCase().trim();
  if (src === 'realtime') {
    bits.push('live Finnhub tape');
  } else if (src) {
    bits.push(`NOT the live tape (${src}) — REST fallback bars`);
  } else {
    bits.push('source unknown');
  }
  if (r.cm_rsi_age_sec != null && Number.isFinite(Number(r.cm_rsi_age_sec))) {
    bits.push(`newest bar ${Number(r.cm_rsi_age_sec).toFixed(0)}s old`);
  }
  if (r.cm_rsi_green) bits.push('green');
  else if (r.cm_rsi_low) bits.push('low');
  return bits.join(' · ');
}

/** Hover: the raw %R lines behind the cell's 0-100 reading, plus the window.
 *  The slow line needs a 112-bar window, so on a name admitted today it is
 *  usually absent — the cell shows the fast-line exhaustion either way. */
function _fmtExhTitle(r) {
  if (!r) return '';
  const bits = ['EXH = 100 + fast %R'];
  if (r.pctr != null && Number.isFinite(Number(r.pctr))) {
    bits.push(`fast ${Number(r.pctr).toFixed(1)}`);
  }
  if (r.pctr_slow != null && Number.isFinite(Number(r.pctr_slow))) {
    bits.push(`slow ${Number(r.pctr_slow).toFixed(1)}`);
  } else {
    bits.push('slow n/a (needs 112 bars)');
  }
  const src = String(r.pctr_src || '').toLowerCase().trim();
  if (src && src !== 'live') {
    bits.push(`NOT LIVE (${src}) - range over the bars that existed`);
  }
  if (r.pctr_ob) bits.push('red boxes');
  if (r.pctr_tight) bits.push('tight');
  if (r.pctr_gap != null && Number.isFinite(Number(r.pctr_gap))) {
    bits.push(`gap ${Number(r.pctr_gap).toFixed(1)}`);
  }
  if (r.exh_window_min != null && Number.isFinite(Number(r.exh_window_min))) {
    bits.push(`window ${Number(r.exh_window_min).toFixed(1)}m`);
  }
  if (r.exh_bars != null) bits.push(`${r.exh_bars} bars`);
  if (r.pctr_src === 'sparse_window') {
    return 'No 1m %R — not enough prints in the clock window to trust a reading';
  }
  if (r.pctr_src === 'clock_range') {
    bits[0] = 'Range %R on recent 1m prints (not a full 21/112 window)';
  }
  return bits.length > 1 ? bits.join(' · ') : '';
}

function _exhClass(state) {
  if (state === 'overbought') return ' exh--ob';
  if (state === 'heating')    return ' exh--up';
  if (state === 'cooling')    return ' exh--down';
  return '';
}

/** RVOL as "1.92×" — same shape the Research/Trending columns use. */
function _fmtRvol(v) {
  const n = Number(v);
  return v != null && Number.isFinite(n) ? `${n.toFixed(2)}×` : '—';
}

/**
 * Surgical paint — reuse row nodes by symbol (like Momentum Stocks).
 * Quote ticks patch cells; structural changes rebuild that row only.
 */
function _paint(rowsEl, rows, kind, sortCol, sortDir, empty, book) {
  if (!rows.length) {
    rowsEl.innerHTML = `<span class="tx-placeholder">${empty}</span>`;
    return;
  }

  // Drop placeholder if present
  const ph = rowsEl.querySelector('.tx-placeholder');
  if (ph) ph.remove();

  const sorted = _applySort(rows, sortCol, sortDir);
  const existing = /** @type {Map<string, HTMLElement>} */ (new Map());
  rowsEl.querySelectorAll('[data-feed-symbol]').forEach(el => {
    existing.set(String(el.dataset.feedSymbol || '').toUpperCase(), el);
  });

  const ordered = [];
  for (const r of sorted) {
    const sym = String(r.symbol || '').toUpperCase();
    if (!sym) continue;
    let el = existing.get(sym);
    const struct = _rowStructKey(r, kind);
    if (el && el.dataset.structKey === struct) {
      _updateFeedRow(el, r, kind, book);
    } else if (el) {
      const next = _createFeedRow(r, kind, book);
      el.replaceWith(next);
      el = next;
      existing.set(sym, el);
    } else {
      el = _createFeedRow(r, kind, book);
      rowsEl.appendChild(el);
      existing.set(sym, el);
    }
    el.dataset.structKey = struct;
    ordered.push(el);
  }

  const keep = new Set(ordered.map(el => String(el.dataset.feedSymbol || '').toUpperCase()));
  existing.forEach((el, sym) => {
    if (!keep.has(sym)) el.remove();
  });

  // Reorder only when needed
  const children = [...rowsEl.querySelectorAll('[data-feed-symbol]')];
  const needsReorder = ordered.length !== children.length
    || ordered.some((el, i) => children[i] !== el);
  if (needsReorder) {
    ordered.forEach(el => rowsEl.appendChild(el));
  }
}

function _rowStructKey(r, kind) {
  return [
    r.symbol,
    kind,
    r.source_mark || r.source || '',
    r.rank ?? '',
    r.reason || r.summary || '',
    r.invalidation || '',
    r.look ? r.look_reason : '',
  ].join('\0');
}

function _createFeedRow(r, kind, book) {
  const wrap = document.createElement('div');
  wrap.innerHTML = _row(r, kind, book).trim();
  const el = /** @type {HTMLElement} */ (wrap.firstElementChild);
  const sym = String(r.symbol || '').toUpperCase();
  el.addEventListener('click', () => _add(el, sym));
  const tickerCell = el.querySelector('.cell-ticker');
  if (tickerCell) {
    tickerCell.title = isTvClickOpenEnabled()
      ? `Load ${sym} into TradingView`
      : `Copy ${sym}`;
    tickerCell.addEventListener('click', e => {
      e.stopPropagation();
      copyTicker(e.currentTarget, sym);
    });
  }
  return el;
}

function _setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

function _updateFeedRow(el, r, kind, book) {
  const price = r.price != null ? `$${Number(r.price).toFixed(2)}` : '—';
  let chg = '—';
  let chgMod = '';
  if (r.pct_change != null) {
    const n = Number(r.pct_change);
    chg = `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
    chgMod = n > 0 ? 'chg-pos' : n < 0 ? 'chg-neg' : '';
  }
  const vol  = r.vol_session != null ? _fmtVol(r.vol_session) : '—';
  const rvol = r.rvol != null ? `${Number(r.rvol).toFixed(2)}×` : '—';
  const score = r.trending_score != null ? Number(r.trending_score).toFixed(1) : '—';

  _setText(el.querySelector('.cell-price'), price);

  const chgEl = el.querySelector('.cell-chg');
  if (chgEl) {
    _setText(chgEl, chg);
    chgEl.classList.toggle('chg-pos', chgMod === 'chg-pos');
    chgEl.classList.toggle('chg-neg', chgMod === 'chg-neg');
  }

  const volEl = el.querySelector('.cell-vol');
  if (volEl) {
    _setText(volEl, vol);
    volEl.classList.toggle('vol-high', (r.rvol ?? 0) >= 1.5);
  }

  _setText(el.querySelector('.cell-rvol'), rvol);
  _setText(el.querySelector('.cell-score'), score);

  if (kind === 'claude') {
    const size = r.position_pct != null ? `${Number(r.position_pct).toFixed(0)}%` : '—';
    _setText(el.querySelector('.cell-size'), size);
    const mark = r.source_mark || _markFromSource(r.source) || 'A';
    _setText(el.querySelector('.cell-src'), mark);
    const notesEl = el.querySelector('.cell-notes');
    if (notesEl) {
      const why = r.reason || r.summary || '';
      _setText(notesEl, why || '—');
      if (why) notesEl.title = why;
      else notesEl.removeAttribute('title');
    }

    // Position chip — replace only when markup changes
    const tickerCell = el.querySelector('.cell-ticker');
    if (tickerCell) {
      const want = _posChipHtml(String(r.symbol || '').toUpperCase(), book);
      const cur = tickerCell.querySelector('.ai-pos-chip');
      if (want) {
        const tmp = document.createElement('div');
        tmp.innerHTML = want;
        const nb = tmp.firstElementChild;
        if (cur) {
          if (cur.outerHTML !== nb.outerHTML) cur.replaceWith(nb);
        } else {
          tickerCell.appendChild(nb);
        }
        el.classList.add('feed-row--ai-pos');
      } else {
        if (cur) cur.remove();
        el.classList.remove('feed-row--ai-pos');
      }
    }
  }

  el.classList.toggle('feed-row--look', !!r.look);
  const lookEl = el.querySelector('.cell-look');
  if (lookEl) {
    const html = _lookBadgeHtml(r);
    if (lookEl.innerHTML !== html) lookEl.innerHTML = html;
  }
}

function _applySort(rows, sortCol, sortDir) {
  const list = [...rows];
  const nullHi = sortDir > 0 ? Infinity : -Infinity;
  const nullLo = sortDir > 0 ? -Infinity : Infinity;

  list.sort((a, b) => {
    switch (sortCol) {
      case 'ticker':
        return sortDir * (a.symbol || '').localeCompare(b.symbol || '');
      case 'src':
        return sortDir * String(a.source_mark || '').localeCompare(String(b.source_mark || ''));
      case 'price': {
        const av = a.price ?? nullHi;
        const bv = b.price ?? nullHi;
        return sortDir * (av - bv);
      }
      case 'chg': {
        const av = a.pct_change ?? nullHi;
        const bv = b.pct_change ?? nullHi;
        return sortDir * (av - bv);
      }
      case 'vol': {
        // Missing volume sorts last when ranking high-first
        const av = a.vol_session ?? nullLo;
        const bv = b.vol_session ?? nullLo;
        return sortDir * (av - bv);
      }
      case 'rvol': {
        const av = a.rvol ?? nullLo;
        const bv = b.rvol ?? nullLo;
        return sortDir * (av - bv);
      }
      case 'score': {
        const av = a.trending_score ?? nullLo;
        const bv = b.trending_score ?? nullLo;
        return sortDir * (av - bv);
      }
      case 'size': {
        const av = a.position_pct ?? nullLo;
        const bv = b.position_pct ?? nullLo;
        return sortDir * (av - bv);
      }
      case 'rank': {
        const av = a.rank ?? nullHi;
        const bv = b.rank ?? nullHi;
        return sortDir * (av - bv);
      }
      case 'notes':
        return sortDir * String(a.reason || a.summary || '')
          .localeCompare(String(b.reason || b.summary || ''));
      default:
        return 0;
    }
  });
  return list;
}

function _updateSortHeaders(headerEls, sortCol, sortDir) {
  Object.entries(headerEls).forEach(([col, el]) => {
    if (!el.dataset.sortLabel) el.dataset.sortLabel = el.textContent.trim();
    const label = el.dataset.sortLabel;
    if (col === sortCol) {
      el.textContent = label + (sortDir === 1 ? ' ↑' : ' ↓');
      el.classList.add('th--sorted');
    } else {
      el.textContent = label;
      el.classList.remove('th--sorted');
    }
  });
}

function _row(r, kind, book) {
  const rawSym = String(r.symbol || '').toUpperCase();
  const sym = _esc(rawSym);
  const colsClass = kind === 'claude' ? 'feed-cols feed-cols--claude' : 'feed-cols feed-cols--trending';

  // Same cell classes as Momentum Stocks (tickers.js) so chrome matches.
  const price = r.price != null ? `$${Number(r.price).toFixed(2)}` : '—';

  let chg = '—';
  let chgCls = 'cell-chg';
  if (r.pct_change != null) {
    const n = Number(r.pct_change);
    chg = `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
    chgCls += n > 0 ? ' chg-pos' : n < 0 ? ' chg-neg' : '';
  }

  const vol  = r.vol_session != null ? _fmtVol(r.vol_session) : '—';
  const rvol = r.rvol != null ? `${Number(r.rvol).toFixed(2)}×` : '—';
  const volCls = (r.rvol ?? 0) >= 1.5 ? ' cell-vol vol-high' : ' cell-vol';

  const rank = r.rank != null
    ? `<span class="feed-rank">${_esc(String(r.rank))}</span>`
    : '';

  if (kind === 'claude') {
    const mark = _esc(r.source_mark || _markFromSource(r.source) || 'A');
    const score = r.trending_score != null
      ? Number(r.trending_score).toFixed(1)
      : '—';
    const size = r.position_pct != null
      ? `${Number(r.position_pct).toFixed(0)}%`
      : '—';
    let thesis = '';
    const why = r.reason || r.summary || '';
    if (why) thesis += `<div class="feed-why">${_esc(why)}</div>`;
    if (r.invalidation) thesis += `<div class="feed-invalid">✕ ${_esc(r.invalidation)}</div>`;
    const posChip = _posChipHtml(rawSym, book);
    const look = _lookBadgeHtml(r);

    return `<div class="ticker-row feed-row${posChip ? ' feed-row--ai-pos' : ''}${r.look ? ' feed-row--look' : ''}" data-feed-symbol="${sym}" title="Click row to add ${sym} to the watchlist">`
         + `<div class="${colsClass}">`
         +   `<div class="cell-ticker">${rank}${sym}${posChip}</div>`
         +   `<div class="cell-src" title="A=Anthropic · X=xAI · AX=both">${mark}</div>`
         +   `<div class="cell-price" data-price>${price}</div>`
         +   `<div class="${chgCls}" data-chg>${chg}</div>`
         +   `<div class="${volCls.trim()}" data-vol>${_esc(vol)}</div>`
         +   `<div class="cell-rvol" data-rvol>${_esc(rvol)}</div>`
         +   `<div class="cell-score" data-score>${_esc(score)}</div>`
         +   `<div class="cell-size" data-size>${_esc(size)}</div>`
         +   `<div class="cell-look">${look}</div>`
         +   `<div class="cell-notes" data-notes title="${_esc(why)}">${why ? _esc(why) : '—'}</div>`
         + `</div>`
         + thesis
         + `</div>`;
  }

  let last = '—';
  if (r.trending_score != null) {
    last = Number(r.trending_score).toFixed(1);
  }
  const look = _lookBadgeHtml(r);

  return `<div class="ticker-row feed-row${r.look ? ' feed-row--look' : ''}" data-feed-symbol="${sym}" title="Click row to add ${sym} to the watchlist">`
       + `<div class="${colsClass}">`
       +   `<div class="cell-ticker">${rank}${sym}</div>`
       +   `<div class="cell-price" data-price>${price}</div>`
       +   `<div class="${chgCls}" data-chg>${chg}</div>`
       +   `<div class="${volCls.trim()}" data-vol>${_esc(vol)}</div>`
       +   `<div class="cell-rvol" data-rvol>${_esc(rvol)}</div>`
       +   `<div class="cell-score" data-score>${_esc(last)}</div>`
       +   `<div class="cell-look">${look}</div>`
       + `</div>`
       + `</div>`;
}

/** Desk LOOK badge — green pill matching the terminal monitor (LOOK EXT / LOOK WASH). */
function _lookBadgeHtml(r) {
  if (!r || !r.look) return '';
  const reason = String(r.look_reason || '').toUpperCase();
  const label = reason ? `LOOK ${reason}` : 'LOOK';
  const title = reason === 'EXT'
    ? 'LOOK EXT — heat + volume + green near 52w high'
    : reason === 'WASH'
      ? 'LOOK WASH — heat + volume + red near 52w low'
      : 'LOOK — focus candidate (desk eyes, not auto-trade)';
  return `<span class="look-badge" title="${_esc(title)}">${_esc(label)}</span>`;
}

/**
 * Mirror stocktwits_trending.apply_look_highlights for the web desk.
 * Marks look / look_reason / range_pct on each row (mutates in place).
 */
export function applyLookHighlights(rows, opts = {}) {
  const minAbsChg = opts.minAbsChg ?? 3.0;
  const maxLooks = opts.maxLooks ?? 2;
  const nearHigh = opts.nearHigh ?? 0.70;
  const nearLow = opts.nearLow ?? 0.30;
  const heatTopN = opts.heatTopN ?? 3;
  const minRvol = opts.minRvol ?? 1.5;

  for (const r of rows) {
    r.look = false;
    r.look_reason = '';
    r.look_priority = 0;
    r.range_pct = _rangePct(r.price, r.high_52w, r.low_52w);
  }
  if (!rows.length) return rows;

  const scores = rows
    .map(r => (r.trending_score != null ? Number(r.trending_score) : null))
    .filter(v => v != null && Number.isFinite(v));
  const vols = rows
    .map(r => _rowVol(r))
    .filter(v => v != null);
  const medScore = scores.length ? _median(scores) : null;
  const medVol = vols.length ? _median(vols) : null;

  const byScore = rows
    .filter(r => r.trending_score != null)
    .slice()
    .sort((a, b) => Number(b.trending_score) - Number(a.trending_score));
  const topHeat = new Set(
    byScore.slice(0, Math.max(1, heatTopN)).map(r => r.symbol),
  );

  const candidates = [];
  for (const r of rows) {
    const sc = r.trending_score != null ? Number(r.trending_score) : null;
    const chg = r.pct_change != null ? Number(r.pct_change) : null;
    const vol = _rowVol(r);
    const rp = r.range_pct;

    const heat = topHeat.has(r.symbol)
      || (sc != null && medScore != null && sc >= medScore);
    const move = chg != null && Math.abs(chg) >= minAbsChg;
    let volOk = vol != null && (
      (medVol != null && vol >= medVol) || (medVol == null && vol > 0)
    );
    const rv = r.rvol != null ? Number(r.rvol) : null;
    if (volOk && minRvol != null && rv != null && Number.isFinite(rv)) {
      volOk = rv >= minRvol;
    }
    if (!(heat && move && volOk) || rp == null || chg == null) continue;

    let reason = '';
    if (chg > 0 && rp >= nearHigh) reason = 'EXT';
    else if (chg < 0 && rp <= nearLow) reason = 'WASH';
    else continue;

    const pri = (sc || 0) * Math.abs(chg)
      * Math.log10(1 + Math.max(0, vol || 0));
    candidates.push({ pri, r, reason });
  }

  candidates.sort((a, b) => b.pri - a.pri);
  for (const { pri, r, reason } of candidates.slice(0, Math.max(0, maxLooks))) {
    r.look = true;
    r.look_reason = reason;
    r.look_priority = pri;
  }
  return rows;
}

function _rangePct(price, hi, lo) {
  const p = price != null ? Number(price) : null;
  const h = hi != null ? Number(hi) : null;
  const l = lo != null ? Number(lo) : null;
  if (p == null || h == null || l == null || !(h > l)) return null;
  return (p - l) / (h - l);
}

function _rowVol(r) {
  const v = r && r.vol_session;
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function _median(nums) {
  if (!nums.length) return null;
  const s = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

async function _add(el, symbol) {
  const ticker = el.querySelector('.cell-ticker');
  if (ticker) ticker.classList.add('cell-ticker--firing');
  el.classList.add('ticker-row--selected');
  try {
    await api.addTicker(symbol);
  } catch (err) {
    console.error('[feeds] add to watchlist failed', err);
  } finally {
    setTimeout(() => {
      if (ticker) ticker.classList.remove('cell-ticker--firing');
      el.classList.remove('ticker-row--selected');
    }, 800);
  }
}

function _isScheduleNotice(err) {
  if (!err) return false;
  const s = String(err).toLowerCase();
  return s.startsWith('next research') || s === 'no research times configured'
    || s.startsWith('a:next research') || s.startsWith('x:next research');
}

function _markFromSource(source) {
  const s = String(source || '').toLowerCase();
  if (s === 'both' || s === 'merged' || s === 'ax') return 'AX';
  if (s === 'xai' || s === 'grok') return 'X';
  if (s === 'anthropic' || s === 'claude') return 'A';
  return '';
}

function _stampLine(p, book) {
  // Compact header stamp (one line). Full detail goes on title tooltip.
  const parts = [];
  if (p.next_run_label) parts.push(`next ${p.next_run_label}`);
  const t = _stamp(p.last_ok);
  if (t && parts.length < 2) parts.push(t);
  // Build rich tooltip for hover.
  const tip = [];
  if (t) tip.push(`updated ${t}`);
  if (p.next_run_label) tip.push(`next ${p.next_run_label}`);
  if (p.model) tip.push(String(p.model));
  const day = p.token_day;
  if (day && day.count > 0 && day.total_cost_usd != null) {
    const cost = Number(day.total_cost_usd);
    if (Number.isFinite(cost)) tip.push(`$${cost.toFixed(cost >= 1 ? 2 : 3)} today (${day.count})`);
  } else {
    const u = p.last_usage;
    if (u && u.total_cost_usd != null) {
      const cost = Number(u.total_cost_usd);
      if (Number.isFinite(cost)) tip.push(`last $${cost.toFixed(cost >= 1 ? 2 : 3)}`);
    }
  }
  // Stash tip on the payload so callers can set element.title if needed.
  p._stamp_title = tip.join(' · ');
  return parts.join(' · ') || (p.model ? String(p.model) : '');
}

function _ownerLabel(book) {
  const o = String((book && book.book_owner) || '').toLowerCase();
  if (o === 'grok') return 'Grok';
  if (o === 'claude') return 'Claude';
  return 'AI';
}

function _posChipHtml(sym, book) {
  const pos = book && book.positions && book.positions[sym];
  if (!pos) return '';
  const owner = _ownerLabel(book);
  const qty = _fmtQty(pos.qty);
  const pl = _fmtPl(pos);
  const plCls = _plClass(pos);
  return `<span class="ai-pos-chip ${plCls}" title="${_esc(_posTitle(owner, sym, pos))}">`
    + `${_esc(owner)} ${_esc(qty)} ${_esc(pl)}`
    + `</span>`;
}

function _posTitle(owner, sym, p) {
  const qty = _fmtQty(p.qty);
  const pl = _fmtPl(p);
  const entry = p.avg_entry != null ? ` · entry $${Number(p.avg_entry).toFixed(2)}` : '';
  return `${owner} paper · ${sym} ${qty}${entry} · P&L ${pl}`;
}

function _fmtQty(q) {
  const n = Math.abs(Number(q) || 0);
  if (!n) return '0sh';
  return Number.isInteger(n) ? `${n}sh` : `${n.toFixed(2)}sh`;
}

function _fmtPl(p) {
  const pl = Number(p && p.pl);
  const plpc = Number(p && p.plpc);
  const parts = [];
  if (Number.isFinite(pl)) {
    const sign = pl > 0 ? '+' : '';
    parts.push(`${sign}$${pl.toFixed(0)}`);
  }
  if (Number.isFinite(plpc)) {
    const sign = plpc > 0 ? '+' : '';
    parts.push(`${sign}${plpc.toFixed(1)}%`);
  }
  return parts.join(' ') || '—';
}

function _plClass(p) {
  const pl = Number(p && p.pl);
  if (!Number.isFinite(pl) || pl === 0) return '';
  return pl > 0 ? 'ai-pos-chip--pos' : 'ai-pos-chip--neg';
}

function _stamp(ts) {
  if (!ts) return '';
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function _fmtVol(v) {
  v = Number(v);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M';
  if (v >= 1_000)     return Math.round(v / 1_000) + 'K';
  return String(v);
}

function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
