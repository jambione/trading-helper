/**
 * admin.js — Admin panel for user=jmb
 *
 * Two tabs:
 *   Feedback   — read-only view of user suggestion submissions
 *   Ticker Feed — add / reorder / delete items in the scrolling bottom feed
 */

import { api } from './api.js?v=38';

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

// ── Helpers ────────────────────────────────────────────────────

function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
