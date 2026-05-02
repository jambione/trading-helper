/**
 * scanner.js — Market Radar panel
 *
 * Connects to the trading-scanners backend WebSocket at localhost:8000/ws.
 * Renders one card per scanner type in a responsive grid. Cards support
 * drag-to-reorder; order is persisted to localStorage.
 */

import { api } from './api.js?v=6';

const SCANNER_WS_URL  = 'ws://localhost:8000/ws';
const SCANNER_API_URL = 'http://localhost:8000';
const ORDER_KEY       = 'ss:scanner-order';

const SCANNER_COLORS = {
  gap:             '#38bdf8',
  momentum_hod:    '#60a5fa',
  volume_breakout: '#fb923c',
  price_spike:     '#facc15',
  find_it_first:   '#c084fc',
  vwap_trend:      '#2dd4bf',
};

let _ws             = null;
let _reconnectTimer = null;
let _allScanners    = [];
let _engineRunning  = false;
let _draggedType    = null;

// DOM refs
let _dotEl    = null;
let _pollEl   = null;
let _gridEl   = null;
let _toggleEl = null;

export function init(panelEl) {
  if (!panelEl) return;

  _dotEl    = panelEl.querySelector('[data-scanner-dot]');
  _pollEl   = panelEl.querySelector('[data-scanner-poll]');
  _gridEl   = panelEl.querySelector('[data-scanner-rows]');
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
  _allScanners = _applyOrder(broadcast.scanners);
  if (_pollEl) _pollEl.textContent = `#${broadcast.poll_count}`;
  _renderCards();
}

// ── Order persistence ──────────────────────────────────────────

function _loadOrder() {
  try { return JSON.parse(localStorage.getItem(ORDER_KEY)) ?? []; }
  catch { return []; }
}

function _saveOrder(types) {
  localStorage.setItem(ORDER_KEY, JSON.stringify(types));
}

function _applyOrder(scanners) {
  const order = _loadOrder();
  if (!order.length) return scanners;
  return [...scanners].sort((a, b) => {
    const ai = order.indexOf(a.scanner_type);
    const bi = order.indexOf(b.scanner_type);
    return (ai === -1 ? order.length : ai) - (bi === -1 ? order.length : bi);
  });
}

// ── Rendering ──────────────────────────────────────────────────

function _renderCards() {
  if (!_gridEl) return;

  if (_allScanners.length === 0) {
    _gridEl.innerHTML = '<div class="scanner-grid-empty">Waiting for scanner…</div>';
    return;
  }

  _gridEl.innerHTML = _allScanners.map(s => _cardHTML(s)).join('');

  _gridEl.querySelectorAll('[data-add-wl]').forEach(el => {
    el.addEventListener('click', () => _addToWatchlist(el.dataset.addWl, el));
  });

  _attachDrag();
}

function _cardHTML(scanner) {
  const hasAlerts = scanner.alerts.length > 0;
  const sorted    = [...scanner.alerts].sort((a, b) => b.triggered_at.localeCompare(a.triggered_at));

  const badgeCls  = hasAlerts ? 'scanner-card-badge--active' : '';
  const body      = hasAlerts
    ? `<table class="sct">
        <thead><tr>
          <th>Time</th>
          <th>Ticker</th>
          <th class="r">Price</th>
          <th class="r">Chg%</th>
          <th>Signal</th>
          <th class="r">Float</th>
        </tr></thead>
        <tbody>${sorted.map(_rowHTML).join('')}</tbody>
      </table>`
    : '<div class="scanner-card-empty">no alerts</div>';

  return `<div class="scanner-card" draggable="true" data-scanner-type="${scanner.scanner_type}">
  <div class="scanner-card-header">
    <span class="scanner-card-label">${scanner.label}</span>
    <span class="scanner-card-badge ${badgeCls}">${scanner.alerts.length}</span>
  </div>
  <div class="scanner-card-body">${body}</div>
</div>`;
}

function _rowHTML(a) {
  const color    = SCANNER_COLORS[a.scanner_type] ?? 'var(--text-dim)';
  const time     = _fmtTime(a.triggered_at);
  const chgCls   = a.change_pct >= 0 ? 'chg-pos' : 'chg-neg';
  const float    = a.float_millions != null ? `${a.float_millions.toFixed(1)}M` : '—';
  const note     = a.note || a.scanner_type;

  return `<tr>
  <td class="sct-time">${time}</td>
  <td class="sct-ticker sct-ticker--add" data-add-wl="${a.ticker}" title="Add to watchlist">${a.ticker}</td>
  <td class="sct-num">$${a.price.toFixed(2)}</td>
  <td class="sct-num cell-chg ${chgCls}">${Math.round(a.change_pct)}</td>
  <td class="sct-signal" style="color:${color}">${note}</td>
  <td class="sct-float">${float}</td>
</tr>`;
}

async function _addToWatchlist(ticker, el) {
  const orig = el.textContent;
  el.style.pointerEvents = 'none';
  el.textContent = '…';
  try {
    await api.addTicker(ticker);
    el.textContent = '✓';
    setTimeout(() => { el.textContent = orig; el.style.pointerEvents = ''; }, 1200);
  } catch {
    el.textContent = orig;
    el.style.pointerEvents = '';
  }
}

// ── Drag-to-reorder ────────────────────────────────────────────

function _attachDrag() {
  const cards = [..._gridEl.querySelectorAll('.scanner-card')];

  cards.forEach(card => {
    const type = card.dataset.scannerType;

    card.addEventListener('dragstart', e => {
      _draggedType = type;
      e.dataTransfer.effectAllowed = 'move';
      // Defer class add so the ghost image captures the undimmed card
      requestAnimationFrame(() => card.classList.add('scanner-card--dragging'));
    });

    card.addEventListener('dragend', () => {
      _draggedType = null;
      cards.forEach(c => {
        c.classList.remove('scanner-card--dragging', 'scanner-card--drag-over');
      });
    });

    card.addEventListener('dragover', e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (type === _draggedType) return;
      cards.forEach(c => c.classList.remove('scanner-card--drag-over'));
      card.classList.add('scanner-card--drag-over');
    });

    card.addEventListener('dragleave', e => {
      // Only clear if leaving the card entirely (not moving to a child)
      if (!card.contains(e.relatedTarget)) {
        card.classList.remove('scanner-card--drag-over');
      }
    });

    card.addEventListener('drop', e => {
      e.preventDefault();
      if (!_draggedType || _draggedType === type) return;

      const fromIdx = _allScanners.findIndex(s => s.scanner_type === _draggedType);
      const toIdx   = _allScanners.findIndex(s => s.scanner_type === type);
      if (fromIdx === -1 || toIdx === -1) return;

      const reordered = [..._allScanners];
      const [moved]   = reordered.splice(fromIdx, 1);
      reordered.splice(toIdx, 0, moved);

      _allScanners = reordered;
      _saveOrder(reordered.map(s => s.scanner_type));
      _renderCards();
    });
  });
}

// ── Engine control ─────────────────────────────────────────────

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

function _fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString('en-US', {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
  } catch { return '—'; }
}

function _setDot(state) {
  if (!_dotEl) return;
  _dotEl.className = `ws-dot ${state === 'on' ? 'ws-dot--on' : 'ws-dot--off'}`;
  _dotEl.title     = state === 'on' ? 'Scanner live' : 'Scanner disconnected';
}
