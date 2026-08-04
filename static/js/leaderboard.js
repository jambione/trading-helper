/**
 * leaderboard.js — Today's Top Mentions leaderboard bar
 *
 * Subscribes to the `tickers` store slice and renders the top 5
 * tickers by today's mention count across the full-width bar
 * between the header and the main panel grid.
 */

import { subscribe } from './store.js?v=95';

let _barEl = null;
let _lastKey = '';

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

  const key = top.length
    ? top.map(r => `${r.ticker}:${r.mention_count}:${r.mention_burst ? 1 : 0}`).join('|')
    : '∅';
  if (key === _lastKey) return;
  _lastKey = key;

  if (!top.length) {
    _barEl.innerHTML =
      '<span class="lb-empty">No mentions yet today — leaderboard clears at market open &amp; close</span>';
    return;
  }

  _barEl.innerHTML = top.map((r, i) => {
    const medals = ['🥇', '🥈', '🥉', '4', '5'];
    const medal  = i < 3 ? medals[i] : `<span class="lb-rank-num">${medals[i]}</span>`;
    const burst  = r.mention_burst ? ' lb-item--burst' : '';
    return `
      <div class="lb-item${burst}">
        <span class="lb-medal">${medal}</span>
        <span class="lb-ticker">${r.ticker}</span>
        <span class="lb-count">${r.mention_count}</span>
      </div>`;
  }).join('');
}
