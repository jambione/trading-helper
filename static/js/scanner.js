/**
 * scanner.js — Market Radar panel
 *
 * Connects to the trading-scanners backend WebSocket at localhost:8000/ws,
 * renders scanner alerts in a tabbed table, and lets the user add any alert
 * ticker to the helper watchlist via the +WL button.
 */

import { api } from './api.js';

const SCANNER_WS_URL  = 'ws://localhost:8000/ws';
const SCANNER_API_URL = 'http://localhost:8000';

const SCANNER_COLORS = {
  gap:             '#38bdf8',
  momentum_hod:    '#3d8bff',
  volume_breakout: '#fb923c',
  price_spike:     '#facc15',
  find_it_first:   '#a78bfa',
  vwap_trend:      '#2dd4bf',
};

const SIGNAL_LABELS = {
  gap:             'GAP',
  momentum_hod:    'HOD',
  volume_breakout: 'VOL BRK',
  price_spike:     'SPIKE',
  find_it_first:   'FIRST',
  vwap_trend:      'VWAP',
};

let _ws             = null;
let _reconnectTimer = null;
let _allAlerts      = [];   // merged, sorted list across all scanners
let _engineRunning  = false;

// DOM refs
let _dotEl    = null;
let _pollEl   = null;
let _rowsEl   = null;
let _toggleEl = null;

export function init(panelEl) {
  if (!panelEl) return;

  _dotEl    = panelEl.querySelector('[data-scanner-dot]');
  _pollEl   = panelEl.querySelector('[data-scanner-poll]');
  _rowsEl   = panelEl.querySelector('[data-scanner-rows]');
  _toggleEl = panelEl.querySelector('[data-scanner-toggle]');

  _toggleEl?.addEventListener('click', _onToggle);

  _connect();
}

// ── WebSocket ──────────────────────────────────────────────────

function _connect() {
  if (_ws) { try { _ws.close(); } catch (_) {} }

  _ws = new WebSocket(SCANNER_WS_URL);

  _ws.onopen = () => {
    _setDot('on');
    clearTimeout(_reconnectTimer);
    _fetchRunningState();
  };

  _ws.onmessage = ({ data }) => {
    let msg;
    try { msg = JSON.parse(data); } catch { return; }
    _ingest(msg);
  };

  _ws.onclose = _ws.onerror = () => {
    _setDot('off');
    clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(_connect, 3000);
  };
}

function _ingest(broadcast) {
  if (!Array.isArray(broadcast.scanners)) return;

  _allAlerts = broadcast.scanners
    .flatMap(s => s.alerts)
    .sort((a, b) => b.triggered_at.localeCompare(a.triggered_at));

  if (_pollEl) _pollEl.textContent = `Poll #${broadcast.poll_count}`;

  _renderRows();
}

// ── Rendering ──────────────────────────────────────────────────

function _renderRows() {
  if (!_rowsEl) return;

  if (_allAlerts.length === 0) {
    _rowsEl.innerHTML = '<div class="table-empty">No alerts</div>';
    return;
  }

  _rowsEl.innerHTML = _allAlerts.map(a => {
    const color = SCANNER_COLORS[a.scanner_type] ?? 'var(--text-dim)';
    const t       = new Date(a.triggered_at);
    const time    = t.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    const chgCls  = a.change_pct >= 0 ? 'chg-pos' : 'chg-neg';
    const chgSign = a.change_pct >= 0 ? '+' : '';
    const vol     = _fmtVol(a.volume);
    const float   = a.float_millions != null ? `${a.float_millions.toFixed(1)}M` : '—';
    const rvol    = a.rvol != null ? `${a.rvol.toFixed(1)}x` : '—';

    return `<div class="scanner-row">
  <div class="scanner-cols">
    <div class="cell-scanner-time">${time}</div>
    <div class="cell-ticker">${a.ticker}</div>
    <div class="cell-price">$${a.price.toFixed(2)}</div>
    <div class="cell-chg ${chgCls}">${chgSign}${a.change_pct.toFixed(1)}%</div>
    <div class="cell-vol">${vol}</div>
    <div class="cell-scanner-float">${float}</div>
    <div class="cell-scanner-rvol">${rvol}</div>
    <div class="cell-scanner-signal" style="color:${color}">${SIGNAL_LABELS[a.scanner_type] ?? a.scanner_type}</div>
    <div><button class="btn-add" data-wl="${a.ticker}">+WL</button></div>
  </div>
</div>`;
  }).join('');

  _rowsEl.querySelectorAll('[data-wl]').forEach(btn => {
    btn.addEventListener('click', () => _addToWatchlist(btn.dataset.wl, btn));
  });
}

async function _addToWatchlist(ticker, btn) {
  btn.disabled    = true;
  btn.textContent = '…';
  try {
    await api.addTicker(ticker);
    btn.textContent = 'added';
  } catch {
    btn.disabled    = false;
    btn.textContent = '+WL';
  }
}

// ── Engine control ────────────────────────────────────────────

async function _fetchRunningState() {
  try {
    const res  = await fetch(`${SCANNER_API_URL}/health`);
    const data = await res.json();
    _setEngineRunning(data.running ?? false);
  } catch { /* scanner backend not reachable */ }
}

async function _onToggle() {
  if (!_toggleEl) return;
  _toggleEl.disabled = true;

  const action = _engineRunning ? 'stop' : 'start';
  try {
    const res  = await fetch(`${SCANNER_API_URL}/${action}`, { method: 'POST' });
    const data = await res.json();
    _setEngineRunning(data.running ?? !_engineRunning);
  } catch {
    // fall back to optimistic toggle
    _setEngineRunning(!_engineRunning);
  } finally {
    _toggleEl.disabled = false;
  }
}

function _setEngineRunning(running) {
  _engineRunning = running;
  if (!_toggleEl) return;
  _toggleEl.textContent = running ? 'Stop Scanner' : 'Start Scanner';
  _toggleEl.classList.toggle('btn--danger', running);
}

// ── Helpers ────────────────────────────────────────────────────

function _fmtVol(v) {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000)     return `${Math.round(v / 1_000)}K`;
  return String(v);
}

function _setDot(state) {
  if (!_dotEl) return;
  _dotEl.className = `ws-dot ${state === 'on' ? 'ws-dot--on' : 'ws-dot--off'}`;
  _dotEl.title     = state === 'on' ? 'Scanner live' : 'Scanner disconnected';
}
