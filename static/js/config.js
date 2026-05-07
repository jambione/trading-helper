/**
 * config.js — Configuration drawer component
 *
 * Single responsibility: render, load, and save the settings form.
 * Does not touch other parts of the UI.
 */

import { api } from './api.js?v=7';
import { get } from './store.js?v=7';

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

  // Save
  _saveBtn.addEventListener('click', _save);

  // Audio device refresh
  backdropEl.querySelector('[data-refresh-devices]')
    ?.addEventListener('click', _loadAudioDevices);
}

export async function open() {
  _backdrop.classList.add('open');
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
}

// ── Load config ────────────────────────────────────────────────

async function _loadConfig() {
  try {
    const { config: c } = await api.getConfig();
    _set('cfg-api-key',       c.api_key          ?? '');
    _set('cfg-secret-key',    c.secret_key        ?? '');
    _set('cfg-finnhub-key',   c.finnhub_key       ?? '');
    _set('cfg-timeframe',     c.bar_timeframe     ?? '5Min');
    _set('cfg-bar-count',     c.bar_count         ?? 300);
    _set('cfg-scan-interval', c.scan_interval_sec ?? 60);
    _set('cfg-rte-threshold', c.rte_threshold     ?? 20);
    _set('cfg-rte-min-boxes', c.rte_min_boxes     ?? 2);
    _set('cfg-obv-length',    c.obv_length        ?? 20);
    _set('cfg-vol-surge',     c.volume_surge_mult ?? 1.5);
    _set('cfg-macd-fast',     c.macd_fast         ?? 12);
    _set('cfg-macd-slow',     c.macd_slow         ?? 26);
    _set('cfg-macd-signal',   c.macd_signal       ?? 9);
    _set('cfg-cm-rsi-len',    c.cm_rsi_length     ?? 14);
    _set('cfg-cm-rsi-os',     c.cm_rsi_oversold   ?? 30);
    _set('cfg-tv-url',        c.tv_chart_url      ?? '');
    _set('cfg-strategy',      c.strategy          ?? 'multiple_os');
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
    bar_timeframe:     _strVal('cfg-timeframe'),
    bar_count:         _numVal('cfg-bar-count'),
    scan_interval_sec: _numVal('cfg-scan-interval'),
    device_index:      _deviceVal('cfg-device-index'),
    rte_threshold:     _numVal('cfg-rte-threshold'),
    rte_min_boxes:     _numVal('cfg-rte-min-boxes'),
    obv_length:        _numVal('cfg-obv-length'),
    volume_surge_mult: _numVal('cfg-vol-surge'),
    macd_fast:         _numVal('cfg-macd-fast'),
    macd_slow:         _numVal('cfg-macd-slow'),
    macd_signal:       _numVal('cfg-macd-signal'),
    cm_rsi_length:     _numVal('cfg-cm-rsi-len'),
    cm_rsi_oversold:   _numVal('cfg-cm-rsi-os'),
    tv_chart_url:      _strVal('cfg-tv-url'),
    strategy:          _strVal('cfg-strategy'),
  };

  const ak = _pwdVal('cfg-api-key');
  const sk = _pwdVal('cfg-secret-key');
  const fk = _pwdVal('cfg-finnhub-key');
  if (ak) body.api_key     = ak;
  if (sk) body.secret_key  = sk;
  if (fk) body.finnhub_key = fk;

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
