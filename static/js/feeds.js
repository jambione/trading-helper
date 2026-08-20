/**
 * feeds.js — Trending (Stocktwits) and AI research panels
 *
 * Both render the same market columns — AI rows carry a thesis line and
 * optional source mark (A/X/AX). Click a row (not the symbol) to add it to
 * the watchlist. Click the symbol name to copy the ticker.
 * Column headers sort the list the same way Momentum Stocks does.
 */

import { subscribe, get } from './store.js?v=133';
import { api }       from './api.js?v=133';
import { copyTicker } from './tickers.js?v=137';
import { createSymbolMembershipWatcher } from './panelFlash.js?v=136';

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
    : 'Waiting for trending data…';
  const noteSymbols = createSymbolMembershipWatcher();

  // Default sort matches server ranking: trending by score desc, AI by
  // server order (rank) until the user picks a column.
  let sortCol   = kind === 'trending' ? 'score' : 'rank';
  let sortDir   = kind === 'trending' ? -1 : 1;  // -1 = desc, 1 = asc
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
  subscribe(kind === 'claude' ? 'claude_suggestions' : 'trending', payload => {
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
      price: w.price != null ? w.price : w.last_ask,
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
      local_stop: p.local_stop != null ? p.local_stop : prev.local_stop,
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
function _sortBookRows(rows) {
  const rank = { open: 0, ready: 1, submitted: 2, filled: 2, watching: 3 };
  return [...rows].sort((a, b) => {
    const pa = rank[String(a.phase || 'watching')] ?? 9;
    const pb = rank[String(b.phase || 'watching')] ?? 9;
    if (pa !== pb) return pa - pb;
    return String(a.symbol || '').localeCompare(String(b.symbol || ''));
  });
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
function _paintBookTable(sectionEl, rowsEl, countEl, stampEl, book, dayPlEl) {
  if (!rowsEl) return;
  const rows = _sortBookRows(_bookRows(book));
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
    tickerCell.title = `Copy ${sym}`;
    tickerCell.addEventListener('click', e => {
      e.stopPropagation();
      copyTicker(e.currentTarget, sym);
    });
  }
  return el;
}

/** Status column shows *why we are not long* (blocker), not READY/WATCH. */
function _bookBlockerLabel(r) {
  if (!r) return '—';
  const phase = String(r.phase || '').toLowerCase();
  if (phase === 'open' || r.is_position) return 'open';
  if (phase === 'submitted') return 'sent';
  const b = String(r.blocker || r.block_reason || '').trim();
  const code = String(r.block_code || '').trim();
  const detail = String(r.block_detail || '').trim();
  // A real refuse (heat low, dead today) wins over leftover "in zone" / buy.
  if (b && !(r.ready) && !['in zone', 'in_zone', 'buy'].includes(b.toLowerCase()) && code !== 'in_zone') {
    return b;
  }
  if (code && !r.ready && !['in_zone', 'placing'].includes(code)) {
    return b || code.replace(/_/g, ' ');
  }
  if (r.ready || phase === 'ready') return 'buy';
  if (b) return b;
  if (detail) return detail;
  return 'watching';
}

function _bookBlockerTitle(r) {
  const label = _bookBlockerLabel(r);
  const detail = String((r && r.block_detail) || '').trim();
  const code = String((r && r.block_code) || '').trim();
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
  if (r && r.ready && (label === 'buy' || label === 'in zone' || label === 'placing' || label === 'placing…')) {
    return 'ai-book-status ai-book-status--ready';
  }
  if (label && label !== 'watching' && label !== '—') {
    return 'ai-book-status ai-book-status--blocked';
  }
  return 'ai-book-status';
}

function _updateBookRow(el, r) {
  if (!el || !r) return;
  const phase = String((r && r.phase) || 'watching').toLowerCase();
  const isOpen = phase === 'open' || r.is_position;
  const statusLabel = _bookBlockerLabel(r);
  const statusEl = el.querySelector('.ai-book-status');
  if (statusEl && statusEl.textContent !== statusLabel) statusEl.textContent = statusLabel;
  if (statusEl) {
    statusEl.className = _bookBlockerClass(r);
    statusEl.title = _bookBlockerTitle(r);
  }
  const trail = _fmtTrail(_bookStopPx(r));
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
  const exhEl = el.querySelector('.cell-exh');
  if (exhEl) {
    const symKey = String(r.symbol || '').toUpperCase();
    const exhN = r.exhaustion != null && Number.isFinite(Number(r.exhaustion))
      ? Number(r.exhaustion) : null;
    const prevExh = _bookPrevExh[symKey];
    if (exhN != null && prevExh !== undefined && prevExh !== exhN) {
      const dir = exhN > prevExh ? 'up' : 'down';
      exhEl.classList.remove('price-flash--up', 'price-flash--down');
      exhEl.classList.add(`price-flash--${dir}`);
      clearTimeout(exhEl._flashTimer);
      exhEl._flashTimer = setTimeout(() => exhEl.classList.remove(
        'price-flash--up', 'price-flash--down'), 600);
    }
    if (exhN != null) _bookPrevExh[symKey] = exhN;
    _setText(exhEl, _bookExhText(r));
    exhEl.classList.add('cell-exh');
    exhEl.classList.toggle('exh--ob', !!r.pctr_ob || r.exhaustion_state === 'overbought');
    exhEl.classList.toggle('exh--tight', !!r.pctr_tight);
    exhEl.classList.toggle('exh--up', r.exhaustion_state === 'heating');
    exhEl.classList.toggle('exh--down', r.exhaustion_state === 'cooling');
    exhEl.classList.toggle('exh--stale', _exhStale(r));
    const tip = _fmtExhTitle(r);
    if (tip) exhEl.title = tip;
  }

  const rsiEl = el.querySelector('.cell-rsi');
  if (rsiEl) {
    _setText(rsiEl, _bookRsiText(r));
    rsiEl.className = `cell-rsi${_rsiPairClass(r)}`;
    rsiEl.title = _fmtRsiTitle(r);
  }
  _setText(el.querySelector('.cell-qty'), qty);
  const plEl = el.querySelector('.cell-pl');
  if (plEl) {
    if (plEl.textContent !== pl) plEl.textContent = pl;
    plEl.className = `cell-pl ${isOpen ? _plClass(r) : ''}`.trim();
  }
  el.classList.toggle('feed-row--ai-open', isOpen);
  el.classList.toggle('feed-row--ai-ready', phase === 'ready' || statusLabel === 'buy');
  if (el.title) el.title = '';
}

function _bookRowHtml(r) {
  const sym = String((r && r.symbol) || '').toUpperCase();
  if (!sym) return '';
  const phase = String((r && r.phase) || 'watching').toLowerCase();
  const isOpen = phase === 'open' || r.is_position;
  const statusLabel = _bookBlockerLabel(r);
  const statusCls = _bookBlockerClass(r);
  const trail = _fmtTrail(_bookStopPx(r));
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
    : ((phase === 'ready' || statusLabel === 'buy')
      ? 'ticker-row feed-row feed-row--ai-book feed-row--ai-ready'
      : 'ticker-row feed-row feed-row--ai-book');
  return `<div class="${rowCls}" data-book-symbol="${_esc(sym)}" data-feed-symbol="${_esc(sym)}">`
    + `<div class="feed-cols feed-cols--ai-book">`
    + `<div class="cell-ticker">${_esc(sym)}</div>`
    + `<div class="${statusCls}" title="${_esc(_bookBlockerTitle(r))}">${_esc(statusLabel)}</div>`
    + `<div class="cell-price${chgMod ? ` ${chgMod}` : ''}" data-price="${_esc(sym)}">${_esc(px)}</div>`
    + `<div class="cell-entry">${_esc(_fmtEntry(r))}</div>`
    + `<div class="cell-trail">${_esc(trail)}</div>`
    + `<div class="cell-exh${_exhPairClass(r)}"${_fmtExhTitle(r) ? ` title="${_esc(_fmtExhTitle(r))}"` : ''}>${_esc(_bookExhText(r))}</div>`
    + `<div class="cell-rsi${_rsiPairClass(r)}" title="${_esc(_fmtRsiTitle(r))}">${_esc(_bookRsiText(r))}</div>`
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

/** "live" means a rolling %R over a clock window, recomputed against the live
 *  print — the number on a chart. Anything else (clock_range, sparse_window)
 *  is position-in-range over whatever bars existed, which is a different
 *  measurement wearing the same column. Marked so it is obvious at a glance
 *  which readings the desk should be trusted to act on. */
function _exhStale(r) {
  if (!r) return false;
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
 *  0-50 band and turning up. Painted green so the column reads as a signal
 *  rather than a number to interpret. */
function _rsiArms(r) {
  if (!r || r.cm_rsi == null || !Number.isFinite(Number(r.cm_rsi))) return false;
  const v = Number(r.cm_rsi);
  return v >= 0 && v <= 50 && !!r.cm_rsi_rising;
}

/** The engine draws its bars from the Finnhub trade stream when the tape is
 *  covering a name and falls back to Alpaca REST when it is not — and it
 *  flips per ticker, mid-session. A reading off the fallback is not wrong,
 *  but it is not the live tape either, so it is marked rather than blended
 *  in with the ones that are. */
function _rsiStale(r) {
  if (!r) return false;
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
  bits.push(_rsiArms(r) ? 'in the 0-50 arm band' : 'outside the arm band');
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
    tickerCell.title = `Copy ${sym}`;
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
