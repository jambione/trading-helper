/**
 * news.js — Stock news feed component
 *
 * Single responsibility: render the news section from news.json.
 * Updates automatically when the server sends new news via WebSocket.
 * Edit news.json on the server — changes appear within seconds.
 */

import { subscribe } from './store.js?v=42';

let _containerEl = null;

export function init(containerEl) {
  _containerEl = containerEl;
  subscribe('news', items => _render(items ?? []));
}

// ── Rendering ──────────────────────────────────────────────────

function _render(items) {
  if (!_containerEl) return;

  if (!items.length) {
    _containerEl.innerHTML = '';
    _containerEl.classList.add('hidden');
    return;
  }

  _containerEl.classList.remove('hidden');
  _containerEl.innerHTML = items.map(_itemHTML).join('');
}

function _itemHTML(item) {
  const ticker  = item.ticker  ? `<span class="news-ticker">${_esc(item.ticker)}</span>` : '';
  const date    = item.date    ? `<span class="news-date">${_esc(item.date)}</span>` : '';
  const headline = item.headline
    ? `<div class="news-headline">${ticker}${_esc(item.headline)}${date}</div>`
    : (ticker ? `<div class="news-headline">${ticker}${date}</div>` : '');
  const body    = item.body
    ? `<div class="news-body">${_esc(item.body)}</div>`
    : '';

  return `<div class="news-item">${headline}${body}</div>`;
}

function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
