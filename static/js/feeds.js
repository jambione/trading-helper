/**
 * feeds.js — Trending (Stocktwits) and AI research panels
 *
 * Both render the same market columns — AI rows carry a thesis line and
 * optional source mark (A/X/AX). Click a row (not the symbol) to add it to
 * the watchlist. Click the symbol name to copy the ticker.
 * Column headers sort the list the same way Momentum Stocks does.
 */

import { subscribe, get } from './store.js?v=94';
import { api }       from './api.js?v=94';
import { copyTicker } from './tickers.js?v=94';
import { createSymbolMembershipWatcher } from './panelFlash.js?v=94';

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

  function _aiBook() {
    return get('ai_positions') || {};
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
    if (stampEl && stampEl.textContent !== stampTxt) stampEl.textContent = stampTxt;
    if (kind === 'claude') {
      _paintBookTable(bookSection, bookRowsEl, bookCountEl, bookStampEl, book);
    }

    // New symbol → cyan pulse on Trending / AI Research header (5s; skip first paint).
    noteSymbols(panelEl, rows.map(r => r.symbol));

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
    const bookKey = _bookKey(book);
    const structKey = withLook.map(r =>
      `${r.symbol}:${r.source_mark || ''}:${r.rank ?? ''}:${r.reason || ''}:${r.invalidation || ''}:${r.look ? r.look_reason : ''}`,
    ).join('|') + `|book:${bookKey}|${sortCol}:${sortDir}`;
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

/** Prefer server entry_book; always merge open positions; fall back to entry_watch. */
function _bookRows(book) {
  const pos = (book && book.positions) || {};
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
      reason: w.reason,
      wait_kind: w.wait_kind,
      entry_low: w.entry_low,
      entry_high: w.entry_high,
      last_ask: w.last_ask,
      price: w.price != null ? w.price : w.last_ask,
      qty: w.qty != null ? w.qty : null,
      pl: w.pl != null ? w.pl : null,
      plpc: w.plpc != null ? w.plpc : null,
      avg_entry: w.avg_entry != null ? w.avg_entry : null,
      is_position: !!w.is_position || phase === 'open',
      ready: !!w.ready || phase === 'ready',
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
    };
  }
  return Object.values(by);
}

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

/** Membership / status / zone only — not quotes (those patch in place). */
function _bookStructKey(rows) {
  return rows.map(r =>
    `${r.symbol}\0${r.phase || ''}\0${r.source || ''}\0${r.wait_kind || ''}\0`
    + `${r.entry_low ?? ''}\0${r.entry_high ?? ''}`,
  ).join('|');
}

let _bookStructKeyCached = '';

/**
 * AI Watch table: patch quote/P&L in place; full rebuild only when
 * membership / phase / zone changes (avoids list jitter on ~2s publishes).
 */
function _paintBookTable(sectionEl, rowsEl, countEl, stampEl, book) {
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
  const stampTxt = [
    live ? '● live' : '○ stale',
    ageBucket != null ? ageBucket : null,
    `recheck ~${Math.round(pollSec)}s`,
    nMom ? `${nMom} Mom` : null,
    nSt ? `${nSt} ST` : null,
    owner,
    mode || null,
  ].filter(Boolean).join(' · ');
  if (stampEl) {
    if (stampEl.textContent !== stampTxt) stampEl.textContent = stampTxt;
    stampEl.classList.toggle('feed-stamp--live', live);
    stampEl.classList.toggle('feed-stamp--stale', !live);
    stampEl.title = live
      ? `AI Watch wire updated ${ageSec}s ago · desk heat re-seeded ~2s · trade recheck ~${Math.round(pollSec)}s`
      : `AI Watch wire is stale (${ageSec != null ? ageSec + 's' : '?'} ago) — book thread may be down`;
  }

  if (sectionEl) sectionEl.hidden = false;

  if (!rows.length) {
    if (!rowsEl.querySelector('.tx-placeholder')) {
      rowsEl.innerHTML = '<span class="tx-placeholder">No watches or open AI positions…</span>';
      _bookStructKeyCached = '';
    }
    return;
  }

  const structKey = _bookStructKey(rows);
  const existing = rowsEl.querySelectorAll('[data-book-symbol]');
  const canPatch = structKey === _bookStructKeyCached
    && existing.length === rows.length
    && rows.every((r, i) => {
      const el = existing[i];
      return el && String(el.dataset.bookSymbol || '').toUpperCase() === r.symbol;
    });

  if (canPatch) {
    rows.forEach((r, i) => _updateBookRow(existing[i], r, owner));
    return;
  }

  // Full rebuild only on membership / phase / zone change.
  _bookStructKeyCached = structKey;
  rowsEl.innerHTML = rows.map(r => _bookRowHtml(r, owner)).join('');
  rowsEl.querySelectorAll('[data-book-symbol]').forEach(el => {
    const sym = String(el.dataset.bookSymbol || '').toUpperCase();
    if (!sym) return;
    el.addEventListener('click', () => _add(el, sym));
    const tickerCell = el.querySelector('.cell-ticker');
    if (tickerCell) {
      tickerCell.title = `Copy ${sym}`;
      tickerCell.addEventListener('click', e => {
        e.stopPropagation();
        copyTicker(e.currentTarget, sym);
      });
    }
  });
}

function _updateBookRow(el, r, owner) {
  if (!el || !r) return;
  const phase = String((r && r.phase) || 'watching').toLowerCase();
  const isOpen = phase === 'open' || r.is_position;
  const statusLabel = isOpen ? 'OPEN'
    : phase === 'ready' ? 'READY'
    : phase === 'submitted' ? 'SENT'
    : (r.wait_kind || r.status || 'WATCH');
  const statusEl = el.querySelector('.ai-book-status');
  if (statusEl && statusEl.textContent !== statusLabel) statusEl.textContent = statusLabel;
  if (statusEl) {
    statusEl.className = isOpen ? 'ai-book-status ai-book-status--open'
      : phase === 'ready' ? 'ai-book-status ai-book-status--ready'
      : phase === 'submitted' ? 'ai-book-status ai-book-status--sent'
      : 'ai-book-status';
  }
  const px = r.price != null && Number.isFinite(Number(r.price))
    ? `$${Number(r.price).toFixed(2)}`
    : (r.last_ask != null && Number.isFinite(Number(r.last_ask))
      ? `$${Number(r.last_ask).toFixed(2)}` : '—');
  const zone = _fmtZone(r.entry_low, r.entry_high) || '—';
  const score = r.score != null && Number.isFinite(Number(r.score))
    ? Number(r.score).toFixed(1) : '—';
  const qty = isOpen ? _fmtQty(r.qty) : '—';
  const pl = isOpen ? _fmtPl(r) : '—';
  _setText(el.querySelector('.cell-price'), px);
  _setText(el.querySelector('.cell-zone'), zone);
  _setText(el.querySelector('.cell-score'), score);
  _setText(el.querySelector('.cell-qty'), qty);
  const plEl = el.querySelector('.cell-pl');
  if (plEl) {
    if (plEl.textContent !== pl) plEl.textContent = pl;
    plEl.className = `cell-pl ${isOpen ? _plClass(r) : ''}`.trim();
  }
  el.classList.toggle('feed-row--ai-open', isOpen);
  el.classList.toggle('feed-row--ai-ready', phase === 'ready');
}

function _bookRowHtml(r, owner) {
  const sym = String((r && r.symbol) || '').toUpperCase();
  if (!sym) return '';
  const phase = String((r && r.phase) || 'watching').toLowerCase();
  const isOpen = phase === 'open' || r.is_position;
  const statusLabel = isOpen ? 'OPEN'
    : phase === 'ready' ? 'READY'
    : phase === 'submitted' ? 'SENT'
    : (r.wait_kind || r.status || 'WATCH');
  const statusCls = isOpen ? 'ai-book-status ai-book-status--open'
    : phase === 'ready' ? 'ai-book-status ai-book-status--ready'
    : phase === 'submitted' ? 'ai-book-status ai-book-status--sent'
    : 'ai-book-status';
  const src = _bookSourceLabel(r.source);
  const px = r.price != null && Number.isFinite(Number(r.price))
    ? `$${Number(r.price).toFixed(2)}`
    : (r.last_ask != null && Number.isFinite(Number(r.last_ask))
      ? `$${Number(r.last_ask).toFixed(2)}` : '—');
  const zone = _fmtZone(r.entry_low, r.entry_high) || '—';
  const score = r.score != null && Number.isFinite(Number(r.score))
    ? Number(r.score).toFixed(1) : '—';
  const qty = isOpen ? _fmtQty(r.qty) : '—';
  const pl = isOpen ? _fmtPl(r) : '—';
  const plCls = isOpen ? _plClass(r) : '';
  const rowCls = isOpen
    ? 'ticker-row feed-row feed-row--ai-book feed-row--ai-open'
    : (phase === 'ready'
      ? 'ticker-row feed-row feed-row--ai-book feed-row--ai-ready'
      : 'ticker-row feed-row feed-row--ai-book');
  const title = [
    isOpen ? `${owner} open position` : `Watch · ${statusLabel}`,
    src ? `src ${src}` : null,
    zone !== '—' ? `zone ${zone}` : null,
    r.reason || null,
    isOpen && r.avg_entry != null ? `entry $${Number(r.avg_entry).toFixed(2)}` : null,
  ].filter(Boolean).join(' · ');

  return `<div class="${rowCls}" data-book-symbol="${_esc(sym)}" data-feed-symbol="${_esc(sym)}" title="${_esc(title)}">`
    + `<div class="feed-cols feed-cols--ai-book">`
    + `<div class="cell-ticker">${_esc(sym)}</div>`
    + `<div class="${statusCls}">${_esc(statusLabel)}</div>`
    + `<div class="cell-src">${_esc(src)}</div>`
    + `<div class="cell-price">${_esc(px)}</div>`
    + `<div class="cell-zone">${_esc(zone)}</div>`
    + `<div class="cell-score">${_esc(score)}</div>`
    + `<div class="cell-qty">${_esc(qty)}</div>`
    + `<div class="cell-pl ${plCls}">${_esc(pl)}</div>`
    + `</div></div>`;
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

function _fmtZone(lo, hi) {
  const a = lo != null && Number.isFinite(Number(lo)) ? Number(lo) : null;
  const b = hi != null && Number.isFinite(Number(hi)) ? Number(hi) : null;
  if (a == null && b == null) return '';
  if (a != null && b != null) {
    const fa = a >= 100 ? a.toFixed(1) : a.toFixed(2);
    const fb = b >= 100 ? b.toFixed(1) : b.toFixed(2);
    return `${fa}–${fb}`;
  }
  const v = a != null ? a : b;
  return v >= 100 ? v.toFixed(1) : v.toFixed(2);
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
  const parts = [];
  const t = _stamp(p.last_ok);
  if (t) parts.push(t);
  if (p.next_run_label) parts.push(`next ${p.next_run_label}`);
  // Open-count only — no "Grok paper" / owner·mode in the panel header stamp.
  // Trader owner lives on the top status chip and per-row position chips.
  if (book) {
    const bookRows = _bookRows(book);
    if (bookRows.length) {
      const nOpen = bookRows.filter(r => r && r.phase === 'open').length;
      parts.push(nOpen
        ? `${bookRows.length} book (${nOpen} open)`
        : `${bookRows.length} book`);
    }
  }
  if (p.model) parts.push(String(p.model));
  // Token spend: prefer day rollup, else last single call.
  const day = p.token_day;
  if (day && day.count > 0 && day.total_cost_usd != null) {
    const cost = Number(day.total_cost_usd);
    const label = Number.isFinite(cost)
      ? `$${cost.toFixed(cost >= 1 ? 2 : 3)} today (${day.count})`
      : `${day.count} calls today`;
    parts.push(label);
  } else {
    const u = p.last_usage;
    if (u && u.total_cost_usd != null) {
      const cost = Number(u.total_cost_usd);
      if (Number.isFinite(cost)) {
        const phase = u.phase ? String(u.phase) : 'call';
        parts.push(`last ${phase} $${cost.toFixed(cost >= 1 ? 2 : 3)}`);
      }
    }
  }
  return parts.join(' · ');
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
