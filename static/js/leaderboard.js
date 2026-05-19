/**
 * leaderboard.js — Today's Top Mentions leaderboard bar
 *
 * Subscribes to the `tickers` store slice and renders the top 5
 * tickers by today's mention count across the full-width bar
 * between the header and the main panel grid.
 *
 * Each entry shows the wall-clock time the ticker entered its
 * current position. The time updates whenever the ticker moves
 * to a new rank (including freshly entering the top 5).
 */

import { subscribe } from './store.js?v=38';

let _barEl = null;

// ticker → { rank, since }   (millisecond timestamp the ticker entered `rank`)
const _rankSince = new Map();

export function init(barEl) {
  _barEl = barEl;
  subscribe('tickers', rows => _render(rows));
}

// ── Rendering ──────────────────────────────────────────────────────────────

function _render(rows) {
  if (!_barEl) return;

  const top = [...rows]
    .filter(r => (r.mention_count ?? 0) > 0)
    .sort((a, b) => (b.mention_count ?? 0) - (a.mention_count ?? 0))
    .slice(0, 5);

  if (!top.length) {
    _rankSince.clear();
    _barEl.innerHTML =
      '<span class="lb-empty">No mentions yet today — leaderboard clears at market open &amp; close</span>';
    return;
  }

  const now = Date.now();
  const seen = new Set();

  top.forEach((row, i) => {
    const rank = i + 1;
    const sym  = row.ticker;
    seen.add(sym);
    const prev = _rankSince.get(sym);
    if (!prev || prev.rank !== rank) {
      _rankSince.set(sym, { rank, since: now });
    }
  });

  // Drop tickers that have fallen out of the top 5 so they reset on re-entry.
  for (const sym of _rankSince.keys()) {
    if (!seen.has(sym)) _rankSince.delete(sym);
  }

  _barEl.innerHTML = top.map((r, i) => {
    const medals = ['🥇', '🥈', '🥉', '4', '5'];
    const medal  = i < 3 ? medals[i] : `<span class="lb-rank-num">${medals[i]}</span>`;
    const burst  = r.mention_burst ? ' lb-item--burst' : '';
    const since  = _rankSince.get(r.ticker)?.since ?? now;
    const clock  = _fmtClock(since);
    return `
      <div class="lb-item${burst}">
        <span class="lb-medal">${medal}</span>
        <span class="lb-ticker">${r.ticker}</span>
        <span class="lb-count">${r.mention_count}</span>
        <span class="lb-since" title="Moved into this rank at ${new Date(since).toLocaleTimeString()}">${clock}</span>
      </div>`;
  }).join('');
}

function _fmtClock(ms) {
  return new Date(ms).toLocaleTimeString([], {
    hour:   'numeric',
    minute: '2-digit',
  });
}
