/**
 * admin.js — Admin panel for user=jmb
 *
 * Tabs:
 *   Feedback    — read-only view of user suggestion submissions
 *   Ticker Feed — add / reorder / delete items in the scrolling bottom feed
 *   News        — create / delete news items shown in the news feed
 */

import { api } from './api.js?v=87';

let _backdrop = null;
let _activeTab = 'feedback';

// ── Public ─────────────────────────────────────────────────────

export function init(backdropEl) {
  if (!backdropEl) return;
  _backdrop = backdropEl;

  // Tab switching
  backdropEl.querySelectorAll('[data-admin-tab]').forEach(btn =>
    btn.addEventListener('click', () => _switchTab(btn.dataset.adminTab))
  );

  // Close
  backdropEl.querySelector('[data-admin-close]')
    ?.addEventListener('click', close);
  backdropEl.addEventListener('click', e => { if (e.target === backdropEl) close(); });
}

export function open() {
  if (!_backdrop) return;
  _backdrop.classList.add('open');
  _switchTab(_activeTab);
}

export function close() {
  _backdrop?.classList.remove('open');
}

// ── Tabs ───────────────────────────────────────────────────────

function _switchTab(tab) {
  _activeTab = tab;
  _backdrop.querySelectorAll('[data-admin-tab]').forEach(btn =>
    btn.classList.toggle('tab-btn--active', btn.dataset.adminTab === tab)
  );
  _backdrop.querySelectorAll('[data-admin-panel]').forEach(panel =>
    panel.classList.toggle('hidden', panel.dataset.adminPanel !== tab)
  );
  if (tab === 'feedback')    _loadFeedback();
  if (tab === 'ticker-feed') _loadFeed();
  if (tab === 'news')        _loadNews();
}

// ── Feedback tab ───────────────────────────────────────────────

async function _loadFeedback() {
  const el = _backdrop.querySelector('[data-admin-feedback]');
  if (!el) return;
  el.innerHTML = '<div class="suggestions-empty">Loading…</div>';
  try {
    const { suggestions = [] } = await api.getSuggestions();
    if (!suggestions.length) {
      el.innerHTML = '<div class="suggestions-empty">No feedback yet.</div>';
      return;
    }
    const sorted = [...suggestions].reverse();
    el.innerHTML = sorted.map(s => {
      const ts  = s.timestamp ? s.timestamp.replace('T', ' ').slice(0, 19) : '—';
      const ip  = s.ip || '—';
      const key = _esc(s.timestamp || '');
      return `<div class="suggestion-card" data-ts="${key}">
        <div class="suggestion-msg">${_esc(s.message)}</div>
        <div class="suggestion-meta">
          <span class="suggestion-ts">${_esc(ts)}</span>
          <span>${_esc(ip)}</span>
          <button class="btn-feed-del suggestion-del-btn" data-del-ts="${key}" title="Delete">✕</button>
        </div>
      </div>`;
    }).join('');

    // Wire delete buttons
    el.querySelectorAll('[data-del-ts]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const ts = btn.dataset.delTs;
        btn.disabled = true;
        btn.textContent = '…';
        try {
          await api.deleteSuggestion(ts);
          btn.closest('.suggestion-card')?.remove();
          if (!el.querySelector('.suggestion-card')) {
            el.innerHTML = '<div class="suggestions-empty">No feedback yet.</div>';
          }
        } catch {
          btn.disabled = false;
          btn.textContent = '✕';
        }
      });
    });
  } catch {
    el.innerHTML = '<div class="suggestions-empty">Failed to load.</div>';
  }
}

// ── Ticker Feed tab ────────────────────────────────────────────

let _feedItems = [];   // working copy

async function _loadFeed() {
  const listEl = _backdrop.querySelector('[data-feed-list]');
  if (!listEl) return;
  listEl.innerHTML = '<div class="suggestions-empty">Loading…</div>';
  try {
    const { items = [] } = await api.getTickerFeed();
    _feedItems = items;
    _renderFeed();
  } catch {
    listEl.innerHTML = '<div class="suggestions-empty">Failed to load.</div>';
  }
}

function _renderFeed() {
  const listEl = _backdrop.querySelector('[data-feed-list]');
  if (!listEl) return;
  if (!_feedItems.length) {
    listEl.innerHTML = '<div class="suggestions-empty">No items yet.</div>';
    return;
  }
  listEl.innerHTML = _feedItems.map((item, i) => `
    <div class="feed-item" data-feed-idx="${i}">
      <span class="feed-item-type feed-type--${item.type}">${item.type.toUpperCase()}</span>
      <span class="feed-item-text">${_esc(item.text)}</span>
      <div class="feed-item-actions">
        <button class="btn-feed-up"   data-up="${i}"   title="Move up"   ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="btn-feed-down" data-down="${i}" title="Move down" ${i === _feedItems.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="btn-feed-del"  data-del="${i}"  title="Delete">✕</button>
      </div>
    </div>`).join('');

  // Wire buttons
  listEl.querySelectorAll('[data-up]').forEach(btn =>
    btn.addEventListener('click', () => { _move(+btn.dataset.up, -1); })
  );
  listEl.querySelectorAll('[data-down]').forEach(btn =>
    btn.addEventListener('click', () => { _move(+btn.dataset.down, 1); })
  );
  listEl.querySelectorAll('[data-del]').forEach(btn =>
    btn.addEventListener('click', () => { _delete(+btn.dataset.del); })
  );
}

function _move(idx, dir) {
  const other = idx + dir;
  if (other < 0 || other >= _feedItems.length) return;
  [_feedItems[idx], _feedItems[other]] = [_feedItems[other], _feedItems[idx]];
  _renderFeed();
  _saveFeed();
}

function _delete(idx) {
  _feedItems.splice(idx, 1);
  _renderFeed();
  _saveFeed();
}

export function addFeedItem(type, text) {
  const t = text.trim();
  if (!t) return;
  _feedItems.push({ type, text: t });
  _renderFeed();
  _saveFeed();
}

async function _saveFeed() {
  const saveBtn = _backdrop.querySelector('[data-feed-save-status]');
  if (saveBtn) { saveBtn.textContent = 'Saving…'; saveBtn.hidden = false; }
  try {
    await api.saveTickerFeed(_feedItems);
    if (saveBtn) {
      saveBtn.textContent = 'Saved ✓';
      setTimeout(() => { saveBtn.hidden = true; }, 2000);
    }
  } catch {
    if (saveBtn) {
      saveBtn.textContent = 'Error saving';
      setTimeout(() => { saveBtn.hidden = true; }, 3000);
    }
  }
}

// ── News tab ───────────────────────────────────────────────────

let _newsItems = [];

async function _loadNews() {
  const listEl = _backdrop.querySelector('[data-news-list]');
  if (!listEl) return;
  listEl.innerHTML = '<div class="suggestions-empty">Loading…</div>';
  try {
    const { items = [] } = await api.getNews();
    _newsItems = items;
    _renderNews();
    _wireNewsAddBtn();
  } catch {
    listEl.innerHTML = '<div class="suggestions-empty">Failed to load.</div>';
  }
}

function _renderNews() {
  const listEl = _backdrop.querySelector('[data-news-list]');
  if (!listEl) return;
  if (!_newsItems.length) {
    listEl.innerHTML = '<div class="suggestions-empty">No news items yet.</div>';
    return;
  }
  listEl.innerHTML = _newsItems.map((item, i) => `
    <div class="feed-item" data-news-idx="${i}" style="flex-direction:column;align-items:flex-start;gap:4px">
      <div style="display:flex;width:100%;justify-content:space-between;align-items:center">
        <span>
          ${item.ticker ? `<strong>${_esc(item.ticker)}</strong> · ` : ''}
          <span>${_esc(item.headline)}</span>
          ${item.date ? `<span style="opacity:.6;margin-left:6px">${_esc(item.date)}</span>` : ''}
        </span>
        <button class="btn-feed-del" data-news-del="${i}" title="Delete">✕</button>
      </div>
      ${item.body ? `<div style="opacity:.7;font-size:.85em">${_esc(item.body)}</div>` : ''}
    </div>`).join('');

  listEl.querySelectorAll('[data-news-del]').forEach(btn =>
    btn.addEventListener('click', () => {
      _newsItems.splice(+btn.dataset.newsDel, 1);
      _renderNews();
      _saveNews();
    })
  );
}

function _wireNewsAddBtn() {
  const btn = _backdrop.querySelector('#news-add-btn');
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  btn.addEventListener('click', () => {
    const headline = _backdrop.querySelector('#news-headline')?.value.trim();
    if (!headline) return;
    _newsItems.unshift({
      ticker:   (_backdrop.querySelector('#news-ticker')?.value.trim() || '').toUpperCase(),
      headline,
      date:     _backdrop.querySelector('#news-date')?.value || '',
      body:     _backdrop.querySelector('#news-body')?.value.trim() || '',
    });
    _backdrop.querySelector('#news-ticker').value   = '';
    _backdrop.querySelector('#news-headline').value = '';
    _backdrop.querySelector('#news-date').value     = '';
    _backdrop.querySelector('#news-body').value     = '';
    _renderNews();
    _saveNews();
  });
}

async function _saveNews() {
  const statusEl = _backdrop.querySelector('[data-news-save-status]');
  if (statusEl) { statusEl.textContent = 'Saving…'; statusEl.hidden = false; }
  try {
    await api.saveNews(_newsItems);
    if (statusEl) {
      statusEl.textContent = 'Saved ✓';
      setTimeout(() => { statusEl.hidden = true; }, 2000);
    }
  } catch {
    if (statusEl) {
      statusEl.textContent = 'Error saving';
      setTimeout(() => { statusEl.hidden = true; }, 3000);
    }
  }
}

// ── Helpers ────────────────────────────────────────────────────

function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
