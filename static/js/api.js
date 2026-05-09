/**
 * api.js — WebSocket + REST communication layer
 *
 * Single responsibility: talk to the server.
 * Consumers subscribe via `on(event, fn)`.
 * REST calls return promises resolving to parsed JSON.
 *
 * Backend URL is read from auth.js (localStorage).
 * Empty string → same origin (local dev).  Set string → remote backend.
 */

import { getToken, getBackendUrl, clearToken } from './auth.js?v=12';

const _handlers = /** @type {Map<string, Function[]>} */ (new Map());

export function on(event, fn) {
  if (!_handlers.has(event)) _handlers.set(event, []);
  _handlers.get(event).push(fn);
}

function emit(event, data) {
  (_handlers.get(event) ?? []).forEach(fn => {
    try { fn(data); } catch (e) { console.error('[api] handler error', event, e); }
  });
}

// ── URL helpers ───────────────────────────────────────────────

function _apiUrl(path) {
  return getBackendUrl() + path;
}

function _wsUrl() {
  const base  = getBackendUrl() || window.location.origin;
  const proto = base.startsWith('https') ? 'wss' : 'ws';
  const host  = base.replace(/^https?:\/\//, '');
  const token = encodeURIComponent(getToken());
  return `${proto}://${host}/ws?token=${token}`;
}

// ── WebSocket ─────────────────────────────────────────────────

let _ws = null;
let _reconnectTimer = null;
let _reconnectDelay = 1500;

export function connect() {
  if (_ws && _ws.readyState < WebSocket.CLOSING) return;

  _ws = new WebSocket(_wsUrl());

  _ws.onopen = () => {
    _reconnectDelay = 1500;
    clearTimeout(_reconnectTimer);
    emit('connected', true);
  };

  _ws.onmessage = ({ data }) => {
    try {
      const msg = JSON.parse(data);
      emit('message', msg);
    } catch (e) {
      console.warn('[api] unparseable WS message', e);
    }
  };

  _ws.onclose = e => {
    emit('connected', false);
    if (e.code === 4001) {
      // Server rejected the token
      clearToken();
      window.location.href = '/login';
      return;
    }
    _reconnectTimer = setTimeout(() => {
      _reconnectDelay = Math.min(_reconnectDelay * 1.5, 15000);
      connect();
    }, _reconnectDelay);
  };

  _ws.onerror = () => _ws.close();
}

// ── REST helpers ──────────────────────────────────────────────

async function request(method, path, body) {
  const token = getToken();
  const opts  = {
    method,
    headers: { ...(token ? { 'Authorization': `Bearer ${token}` } : {}) },
  };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(_apiUrl(path), opts);
  if (res.status === 401) {
    clearToken();
    window.location.href = '/login';
    return;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Public API surface ────────────────────────────────────────

export const api = {
  getState:        ()       => request('GET',  '/api/state'),
  startTx:         ()       => request('POST', '/api/transcriber/start'),
  stopTx:          ()       => request('POST', '/api/transcriber/stop'),
  clearWatchlist:  ()       => request('POST', '/api/ticker-log/clear'),
  clearTranscript: ()       => request('POST', '/api/transcript/clear'),
  triggerScan:     ()       => request('POST', '/api/scan'),
  getConfig:       ()       => request('GET',  '/api/config'),
  saveConfig:      cfg      => request('POST', '/api/config', cfg),
  audioDevices:    ()       => request('GET',  '/api/audio-devices'),
  addTicker:       ticker   => request('POST', '/api/tickers/add',      { ticker }),
  removeTicker:    ticker   => request('POST', '/api/tickers/remove',   { ticker }),
  addBulk:         tickers  => request('POST', '/api/tickers/add-bulk', { tickers }),
  addToWebull:     ticker   => request('POST', '/api/tickers/add-wb',    { ticker }),
  addToTV:         ticker   => request('POST', '/api/tickers/add-tv',    { ticker }),
  addToWBAndTV:    ticker   => request('POST', '/api/tickers/add-wb-tv', { ticker }),
  loginLog:        ()       => request('GET',  '/api/login-log'),
};
