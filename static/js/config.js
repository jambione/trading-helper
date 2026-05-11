/**
 * config.js — Configuration drawer component
 *
 * Single responsibility: render, load, and save the settings form.
 * Does not touch other parts of the UI.
 */

import { api } from './api.js?v=23';
import { get } from './store.js?v=23';
import { getBackendUrl, setBackendUrl, logout } from './auth.js?v=23';

let _backdrop = null;
let _saveBtn  = null;
let _activeTab = 'api';

export function init(backdropEl) {
  _backdrop = backdropEl;
  _saveBtn  = backdropEl.querySelector('[data-save-btn]');

  // Tab bar
  backdropEl.querySelectorAll('[data-tab-btn]').forEach(btn =>
    btn.addEventListener('click', () => _switchTab(btn.dataset.tabBtn))
  );

  // Close via button or backdrop click
  backdropEl.querySelector('[data-close-btn]').addEventListener('click', close);
  backdropEl.addEventListener('click', e => { if (e.target === backdropEl) close(); });

  // Logout
  backdropEl.querySelector('[data-logout-btn]')?.addEventListener('click', logout);

  // Save
  _saveBtn.addEventListener('click', _save);

  // Audio device refresh
  backdropEl.querySelector('[data-refresh-devices]')
    ?.addEventListener('click', _loadAudioDevices);
}

export async function open() {
  _backdrop.classList.add('open');
  _set('cfg-backend-url', getBackendUrl());
  await Promise.all([_loadConfig(), _loadAudioDevices()]);
}

export function close() {
  _backdrop.classList.remove('open');
}

// ── Tabs ───────────────────────────────────────────────────────

function _switchTab(tab) {
  _activeTab = tab;
  _backdrop.querySelectorAll('[data-tab-btn]').forEach(btn =>
    btn.classList.toggle('tab-btn--active', btn.dataset.tabBtn === tab)
  );
  _backdrop.querySelectorAll('[data-tab-panel]').forEach(panel =>
    panel.classList.toggle('hidden', panel.dataset.tabPanel !== tab)
  );
  if (tab === 'history') _loadLoginHistory();
}

// ── Load config ────────────────────────────────────────────────

async function _loadConfig() {
  try {
    const { config: c } = await api.getConfig();
    _set('cfg-api-key',            c.api_key                ?? '');
    _set('cfg-secret-key',         c.secret_key             ?? '');
    _set('cfg-finnhub-key',        c.finnhub_key            ?? '');
    _set('cfg-timeframe',          c.bar_timeframe          ?? '1Min');
    _set('cfg-bar-count',          c.bar_count              ?? 300);
    _set('cfg-tv-url',             c.tv_chart_url           ?? '');
    _set('cfg-mention-threshold',  c.mention_alert_threshold ?? 5);
    _set('cfg-mention-window',     c.mention_alert_window    ?? 10);
  } catch (e) {
    console.error('[config] load failed', e);
  }
}

// ── Load audio devices ─────────────────────────────────────────

async function _loadAudioDevices() {
  const sel = document.getElementById('cfg-device-index');
  if (!sel) return;

  sel.innerHTML = '<option value="">Loading…</option>';
  try {
    const { ok, devices = [], error } = await api.audioDevices();
    if (!ok && error) {
      sel.innerHTML = `<option value="">${_esc(error)}</option>`;
      return;
    }
    const current = get('config').device_index;
    sel.innerHTML = '<option value="">— system default —</option>';
    for (const d of devices) {
      const opt = document.createElement('option');
      opt.value    = d.index;
      opt.textContent = `[${d.index}] ${d.name}${d.loopback ? ' ⟳ LOOPBACK' : ''}`;
      if (d.index === current) opt.selected = true;
      sel.appendChild(opt);
    }
  } catch {
    sel.innerHTML = '<option value="">Failed to load devices</option>';
  }
}

// ── Save ───────────────────────────────────────────────────────

async function _save() {
  const body = {
    bar_timeframe:            _strVal('cfg-timeframe'),
    bar_count:                _numVal('cfg-bar-count'),
    device_index:             _deviceVal('cfg-device-index'),
    tv_chart_url:             _strVal('cfg-tv-url'),
    mention_alert_threshold:  _numVal('cfg-mention-threshold'),
    mention_alert_window:     _numVal('cfg-mention-window'),
  };

  const ak = _pwdVal('cfg-api-key');
  const sk = _pwdVal('cfg-secret-key');
  const fk = _pwdVal('cfg-finnhub-key');
  if (ak) body.api_key     = ak;
  if (sk) body.secret_key  = sk;
  if (fk) body.finnhub_key = fk;

  // Backend URL is stored locally — not sent to the server
  const backendUrl = _val('cfg-backend-url').trim();
  setBackendUrl(backendUrl);

  // Drop undefined/null values that weren't touched
  Object.keys(body).forEach(k => body[k] === undefined && delete body[k]);

  _saveBtn.disabled    = true;
  _saveBtn.textContent = 'Saving…';

  try {
    await api.saveConfig(body);
    _saveBtn.textContent = 'Saved ✓';
    _saveBtn.classList.add('btn--saved');
    setTimeout(() => {
      _saveBtn.textContent = 'Save Settings';
      _saveBtn.classList.remove('btn--saved');
      _saveBtn.disabled = false;
    }, 1800);
  } catch {
    _saveBtn.textContent = 'Error — retry?';
    _saveBtn.classList.add('btn--failed');
    setTimeout(() => {
      _saveBtn.textContent = 'Save Settings';
      _saveBtn.classList.remove('btn--failed');
      _saveBtn.disabled = false;
    }, 2500);
  }
}

// ── Field helpers ──────────────────────────────────────────────

function _set(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val ?? '';
}

function _val(id) {
  return document.getElementById(id)?.value ?? '';
}

function _numVal(id)    { const v = _val(id); return v !== '' ? +v : undefined; }
function _strVal(id)    { return _val(id) || undefined; }
function _pwdVal(id)    { const v = _val(id).trim(); return v || undefined; }
function _deviceVal(id) { const v = _val(id); return v !== '' ? +v : null; }

function _esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Login history ──────────────────────────────────────────────

async function _loadLoginHistory() {
  const el = _backdrop.querySelector('[data-login-history]');
  if (!el) return;
  el.innerHTML = '<div class="login-history-empty">Loading…</div>';
  try {
    const { entries = [] } = await api.loginLog();
    if (!entries.length) {
      el.innerHTML = '<div class="login-history-empty">No login events recorded yet.</div>';
      return;
    }
    el.innerHTML = entries.map(e => {
      const loc  = e.location;
      const locStr = loc
        ? [loc.city, loc.region, loc.country].filter(Boolean).join(', ') || e.ip
        : e.ip;
      const ts   = e.timestamp ? e.timestamp.replace('T', ' ').slice(0, 19) : '—';
      const ok   = e.success !== false;
      return `<div class="login-entry${ok ? '' : ' login-entry--fail'}">
        <div class="login-entry-row">
          <span class="login-badge${ok ? '' : ' login-badge--fail'}">${ok ? 'OK' : 'FAIL'}</span>
          <span class="login-user">${_esc(e.username || '—')}</span>
          <span class="login-ts">${_esc(ts)}</span>
        </div>
        <div class="login-entry-row login-entry-meta">
          <span class="login-loc">${_esc(locStr)}</span>
          <span class="login-ua" title="${_esc(e.user_agent || '')}">${_esc(_shortUa(e.user_agent || ''))}</span>
        </div>
      </div>`;
    }).join('');
  } catch {
    el.innerHTML = '<div class="login-history-empty">Failed to load history.</div>';
  }
}

function _shortUa(ua) {
  if (!ua) return '';
  const m = ua.match(/\(([^)]+)\)/);
  const os = m ? m[1].split(';')[0].trim() : '';
  const browser = ua.match(/(Chrome|Firefox|Safari|Edge|OPR)\/[\d.]+/)?.[0] ?? '';
  return [browser, os].filter(Boolean).join(' · ') || ua.slice(0, 40);
}
