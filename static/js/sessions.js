/**
 * sessions.js — Active session tracking UI
 *
 * Polls /api/active-sessions every 30 seconds (admin only).
 * Updates:
 *   - #active-users-badge / #active-users-count in the header
 *   - #sessions-panel in the Settings → Sessions tab
 *   - #login-log-panel in the Settings → Sessions tab
 */

import { api } from './api.js?v=69';

const POLL_INTERVAL = 30_000; // 30 seconds
let _pollTimer = null;

function _ago(seconds) {
  if (seconds < 60)  return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function _renderSessions(sessions) {
  const panel = document.getElementById('sessions-panel');
  if (!panel) return;

  if (!sessions || sessions.length === 0) {
    panel.innerHTML = '<div class="suggestions-empty">No active sessions in the last 30 minutes.</div>';
    return;
  }

  const rows = sessions.map(s => `
    <div class="suggestion-item" style="display:flex; justify-content:space-between; align-items:center; padding:8px 10px;">
      <span style="font-weight:600; color:var(--text-primary,#f1f5f9);">👤 ${s.username}</span>
      <span style="font-size:11px; color:var(--text-muted,#94a3b8);">${_ago(s.last_seen_seconds)}</span>
    </div>
  `).join('');

  panel.innerHTML = `
    <div class="form-section-label" style="margin-bottom:6px;">Currently Active (last 30 min)</div>
    <div class="suggestions-list" style="margin:0;">${rows}</div>
  `;
}

function _renderLoginLog(entries) {
  const panel = document.getElementById('login-log-panel');
  if (!panel) return;

  if (!entries || entries.length === 0) {
    panel.innerHTML = '<div class="suggestions-empty">No login history yet.</div>';
    return;
  }

  const rows = entries.slice(0, 50).map(e => {
    const color  = e.success ? 'var(--accent,#7dd3fc)' : '#f87171';
    const icon   = e.success ? '✅' : '❌';
    const loc    = e.location
      ? [e.location.city, e.location.region, e.location.country].filter(Boolean).join(', ')
      : e.ip || '';
    const ts     = e.timestamp ? e.timestamp.replace('T', ' ').slice(0, 19) : '';
    return `
      <div class="suggestion-item" style="padding:7px 10px; border-bottom:1px solid var(--border-subtle,rgba(255,255,255,.06));">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span style="font-weight:600; color:${color};">${icon} ${e.username || '—'}</span>
          <span style="font-size:11px; color:var(--text-muted,#94a3b8);">${ts}</span>
        </div>
        <div style="font-size:11px; color:var(--text-muted,#94a3b8); margin-top:2px;">${loc}</div>
      </div>
    `;
  }).join('');

  panel.innerHTML = rows;
}

function _updateBadge(count) {
  const badge   = document.getElementById('active-users-badge');
  const counter = document.getElementById('active-users-count');
  if (!badge || !counter) return;

  counter.textContent = count;
  // Show badge inline-flex so it flows with the other header elements
  badge.style.display = count > 0 ? 'inline-flex' : 'none';
}

async function _poll() {
  try {
    const data = await api.activeSessions();
    if (data && data.ok) {
      _updateBadge(data.count ?? 0);
      _renderSessions(data.sessions ?? []);
    }
  } catch {
    // Not an admin, or server error — silently ignore
  }

  try {
    const logData = await api.loginLog();
    if (logData && logData.entries) {
      _renderLoginLog(logData.entries);
    }
  } catch {
    // Silently ignore
  }
}

export function init() {
  // Only run for admin users (badge element will be hidden via CSS otherwise,
  // but skip the polling to avoid unnecessary 403 errors)
  if (!document.body.classList.contains('user-jmb')) return;

  _poll();
  _pollTimer = setInterval(_poll, POLL_INTERVAL);
}

// Re-poll immediately when the Sessions tab is opened
export function refresh() {
  _poll();
}
