/**
 * feeds.js — Trending (Stocktwits) and AI research panels
 *
 * Both render the same market columns — AI rows carry a thesis line and
 * optional source mark (A/X/AX). Click a row (not the symbol) to add it to
 * the watchlist. Copy buttons mirror Momentum Stocks.
 * Column headers sort the list the same way Momentum Stocks does.
 */

import { subscribe } from './store.js?v=74';
import { api }       from './api.js?v=74';
import { copyTicker } from './tickers.js?v=74';

export function init(panelEl, kind) {
  if (!panelEl) return;

  const rowsEl  = panelEl.querySelector(`[data-${kind}-rows]`);
  const countEl = panelEl.querySelector(`[data-${kind}-count]`);
  const stampEl = panelEl.querySelector(`[data-${kind}-stamp]`);
  const errEl   = panelEl.querySelector(`[data-${kind}-error]`);
  const empty   = kind === 'claude'
    ? 'Waiting for AI research…'
    : 'Waiting for trending data…';

  // Default sort matches server ranking: trending by score desc, AI by
  // server order (rank) until the user picks a column.
  let sortCol   = kind === 'trending' ? 'score' : 'rank';
  let sortDir   = kind === 'trending' ? -1 : 1;  // -1 = desc, 1 = asc
  let lastRows  = [];
  let lastKey   = '';
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
      if (lastRows.length) _paint(rowsEl, lastRows, kind, sortCol, sortDir, empty);
    });
  });
  _updateSortHeaders(headerEls, sortCol, sortDir);

  // AI panel prefers merged ai_suggestions (A/X/AX); store mirrors it onto
  // claude_suggestions for older snapshots.
  subscribe(kind === 'claude' ? 'claude_suggestions' : 'trending', payload => {
    const p    = payload ?? {};
    const rows = Array.isArray(p.rows) ? p.rows : [];

    // The list poll and the quote poll fail independently — surface whichever
    // is broken, since a stale price is not obvious from the number alone.
    // Schedule notices ("next research run…") are status, not hard failures:
    // show them only when the list is empty; otherwise use next_run_label.
    let err = p.error || p.quotes_error || '';
    if (_isScheduleNotice(err) && rows.length) {
      err = p.quotes_error || '';
    }
    if (errEl) {
      errEl.hidden = !err;
      errEl.textContent = err;
      errEl.classList.toggle('feed-error--info', _isScheduleNotice(err));
    }

    if (countEl) countEl.textContent = rows.length ? `${rows.length} ideas` : '';
    if (stampEl) stampEl.textContent = _stampLine(p);

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

    const key = rows.map(r =>
      `${r.symbol}:${r.source_mark || ''}:${r.price ?? ''}:${r.pct_change ?? ''}:${r.trending_score ?? ''}:${r.vol_session ?? ''}:${r.rvol ?? ''}:${r.position_pct ?? ''}:${r.reason || ''}`,
    ).join('|');
    lastRows = rows;
    if (key === lastKey) return;
    lastKey = key;

    _paint(rowsEl, rows, kind, sortCol, sortDir, empty);
  });
}

function _paint(rowsEl, rows, kind, sortCol, sortDir, empty) {
  if (!rows.length) {
    rowsEl.innerHTML = `<span class="tx-placeholder">${empty}</span>`;
    return;
  }
  const sorted = _applySort(rows, sortCol, sortDir);
  rowsEl.innerHTML = sorted.map(r => _row(r, kind)).join('');
  rowsEl.querySelectorAll('[data-feed-symbol]').forEach(el => {
    const sym = el.dataset.feedSymbol;
    // Row body: click → add to watchlist (symbol name and Copy are excluded).
    el.addEventListener('click', () => _add(el, sym));
    const tickerCell = el.querySelector('.cell-ticker');
    if (tickerCell) {
      tickerCell.addEventListener('click', e => {
        e.stopPropagation();
      });
      tickerCell.title = '';
    }
    const copyBtn = el.querySelector('[data-copy-btn]');
    if (copyBtn) {
      copyBtn.addEventListener('click', e => {
        e.stopPropagation();
        copyTicker(e.currentTarget, sym);
      });
    }
  });
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

function _row(r, kind) {
  const sym = _esc(r.symbol || '');
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

    return `<div class="ticker-row feed-row" data-feed-symbol="${sym}" title="Click row to add ${sym} to the watchlist">`
         + `<div class="${colsClass}">`
         +   `<div class="cell-ticker">${rank}${sym}</div>`
         +   `<div class="cell-src" title="A=Anthropic · X=xAI · AX=both">${mark}</div>`
         +   `<div class="cell-price">${price}</div>`
         +   `<div class="${chgCls}">${chg}</div>`
         +   `<div class="${volCls.trim()}">${_esc(vol)}</div>`
         +   `<div class="cell-vol">${_esc(rvol)}</div>`
         +   `<div class="cell-vol cell-score">${_esc(score)}</div>`
         +   `<div class="cell-vol cell-score">${_esc(size)}</div>`
         +   `<div class="cell-actions">`
         +     `<button type="button" class="btn-copy" data-copy-btn title="Copy ticker to clipboard">Copy</button>`
         +   `</div>`
         + `</div>`
         + thesis
         + `</div>`;
  }

  let last = '—';
  if (r.trending_score != null) {
    last = Number(r.trending_score).toFixed(1);
  }

  return `<div class="ticker-row feed-row" data-feed-symbol="${sym}" title="Click row to add ${sym} to the watchlist">`
       + `<div class="${colsClass}">`
       +   `<div class="cell-ticker">${rank}${sym}</div>`
       +   `<div class="cell-price">${price}</div>`
       +   `<div class="${chgCls}">${chg}</div>`
       +   `<div class="${volCls.trim()}">${_esc(vol)}</div>`
       +   `<div class="cell-vol">${_esc(rvol)}</div>`
       +   `<div class="cell-vol cell-score">${_esc(last)}</div>`
       +   `<div class="cell-actions">`
       +     `<button type="button" class="btn-copy" data-copy-btn title="Copy ticker to clipboard">Copy</button>`
       +   `</div>`
       + `</div>`
       + `</div>`;
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

function _stampLine(p) {
  const parts = [];
  const t = _stamp(p.last_ok);
  if (t) parts.push(t);
  if (p.next_run_label) parts.push(`next ${p.next_run_label}`);
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
