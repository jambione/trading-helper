/**
 * sessions.js — Active session tracking + traffic / login UI
 *
 * Polls /api/active-sessions, /api/traffic-log, /api/login-log (admin / jmb).
 * Updates:
 *   - #active-users-badge / #active-users-count in the header
 *   - #sessions-panel in the Settings → Sessions tab
 *   - #traffic-summary-panel / #traffic-visitors-panel
 *   - #login-log-panel
 */

import { api } from './api.js?v=72';

const POLL_INTERVAL = 30_000; // 30 seconds
let _pollTimer = null;

function _ago(seconds) {
  if (seconds < 60)  return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function _locLine(loc, ip, cfCountry) {
  if (loc && (loc.city || loc.region || loc.country)) {
    return [loc.city, loc.region, loc.country].filter(Boolean).join(', ');
  }
  if (cfCountry) return cfCountry;
  return ip || '';
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
    const loc    = _locLine(e.location, e.ip, e.cf_country);
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

function _renderTrafficSummary(summary) {
  const panel = document.getElementById('traffic-summary-panel');
  if (!panel) return;
  if (!summary) {
    panel.innerHTML = '<div class="suggestions-empty">No traffic data yet.</div>';
    return;
  }

  const countries = (summary.by_country || []).slice(0, 8)
    .map(([c, n]) => `${c}: ${n}`)
    .join(' · ') || '—';
  const cities = (summary.by_city || []).slice(0, 6)
    .map(([c, n]) => `${c} (${n})`)
    .join('<br>') || '—';
  const events = (summary.by_event || [])
    .map(([e, n]) => `${e}: ${n}`)
    .join(' · ') || '—';

  panel.innerHTML = `
    <div class="suggestion-item" style="padding:10px;">
      <div style="font-weight:600; color:var(--text-primary,#f1f5f9); margin-bottom:6px;">
        ${summary.unique_visitors ?? 0} unique visitor(s)
        <span style="font-weight:400; color:var(--text-muted,#94a3b8);">
          · ${summary.total_events ?? 0} events · last ${summary.hours ?? 24}h
        </span>
      </div>
      <div style="font-size:12px; color:var(--text-muted,#94a3b8); margin-bottom:4px;">
        <strong style="color:var(--text-primary,#e2e8f0);">Countries:</strong> ${countries}
      </div>
      <div style="font-size:12px; color:var(--text-muted,#94a3b8); margin-bottom:4px;">
        <strong style="color:var(--text-primary,#e2e8f0);">Places:</strong><br>${cities}
      </div>
      <div style="font-size:11px; color:var(--text-muted,#94a3b8);">
        ${events}
      </div>
    </div>
  `;
}

function _renderTrafficVisitors(visitors) {
  const panel = document.getElementById('traffic-visitors-panel');
  if (!panel) return;
  if (!visitors || visitors.length === 0) {
    panel.innerHTML = '<div class="suggestions-empty">No visitors recorded yet. Open the dashboard to start logging.</div>';
    return;
  }

  const rows = visitors.slice(0, 40).map(v => {
    const loc = _locLine(v.location, v.ip, '');
    const users = (v.usernames && v.usernames.length) ? v.usernames.join(', ') : 'anonymous';
    const last = v.last_seen ? v.last_seen.replace('T', ' ').slice(0, 19) : '';
    const tops = (v.top_paths || []).slice(0, 3).map(([p, n]) => `${p}×${n}`).join(' ');
    return `
      <div class="suggestion-item" style="padding:7px 10px; border-bottom:1px solid var(--border-subtle,rgba(255,255,255,.06));">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span style="font-weight:600; color:var(--text-primary,#f1f5f9);">${loc || v.ip}</span>
          <span style="font-size:11px; color:var(--text-muted,#94a3b8);">${last}</span>
        </div>
        <div style="font-size:11px; color:var(--text-muted,#94a3b8); margin-top:2px;">
          ${users} · ${v.hits || 0} hits · ${v.ip}
        </div>
        <div style="font-size:10px; color:var(--text-muted,#64748b); margin-top:2px;">${tops}</div>
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
    const traffic = await api.trafficLog(24, 200);
    if (traffic && traffic.ok) {
      _renderTrafficSummary(traffic.summary);
      _renderTrafficVisitors(traffic.summary?.visitors || []);
    }
  } catch {
    // Silently ignore
  }

  try {
    const logData = await api.loginLog();
    if (logData && (logData.entries || logData.ok)) {
      _renderLoginLog(logData.entries || []);
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
