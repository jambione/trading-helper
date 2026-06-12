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

import { getToken, getBackendUrl, clearToken, getQueryUser } from './auth.js?v=56';

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
  const user = getQueryUser();
  const base = getBackendUrl(); // empty string means same origin
  const sep = path.includes('?') ? '&' : '?';
  const query = user ? `${sep}user=${encodeURIComponent(user)}` : '';
  return base + path + query;
}

function _wsUrl() {
  const base = getBackendUrl() || window.location.origin;
  const proto = base.startsWith('https') ? 'wss' : 'ws';
  const host = base.replace(/^https?:\/\//, '');
  const token = encodeURIComponent(getToken());
  const user = getQueryUser();
  return `${proto}://${host}/ws?token=${token}${
    user ? `&user=${encodeURIComponent(user)}` : ''
  }`;
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
      window.location.href = '/';
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

async function request(method, path, body, extraHeaders) {
  const token = getToken();
  const opts  = {
    method,
    headers: {
      ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
      ...(extraHeaders || {}),
    },
  };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(_apiUrl(path), opts);
  if (res.status === 401) {
    clearToken();
    window.location.href = '/';
    return;
  }
  if (!res.ok) {
    // Attach status + parsed body so callers can branch (e.g. 403 → prompt
    // for the engine control secret) and surface server error messages.
    const err = new Error(`HTTP ${res.status}`);
    err.status = res.status;
    try { err.body = await res.json(); } catch { err.body = null; }
    throw err;
  }
  return res.json();
}

// ── Public API surface ────────────────────────────────────────

export const api = {
  getState:        ()       => request('GET',  '/api/state'),
  clearWatchlist:  ()       => request('POST', '/api/ticker-log/clear'),
  triggerScan:     ()       => request('POST', '/api/scan'),
  getConfig:       ()       => request('GET',  '/api/config'),
  saveConfig:      cfg      => request('POST', '/api/config', cfg),
  addTicker:       ticker   => request('POST', '/api/tickers/add',      { ticker }),
  removeTicker:    ticker   => request('POST', '/api/tickers/remove',   { ticker }),
  addBulk:         tickers  => request('POST', '/api/tickers/add-bulk', { tickers }),
  addToWebull:     ticker   => request('POST', '/api/tickers/add-wb',    { ticker }),
  addToTV:         ticker   => request('POST', '/api/tickers/add-tv',    { ticker }),
  addToWBAndTV:    ticker   => request('POST', '/api/tickers/add-wb-tv', { ticker }),
  burstAlert:      ticker   => request('POST', '/api/tickers/burst',          { ticker }),
  createTVAlert:   ticker   => request('POST', '/api/tickers/create-tv-alert', { ticker }),
  loginLog:        ()       => request('GET',  '/api/login-log'),
  activeSessions:  ()       => request('GET',  '/api/active-sessions'),
  addSuggestion:    msg       => request('POST',   '/api/suggestions', { message: msg }),
  getSuggestions:   ()        => request('GET',    '/api/suggestions'),
  deleteSuggestion: timestamp => request('DELETE', '/api/suggestions', { timestamp }),
  getTickerFeed:   ()       => request('GET',  '/api/ticker-feed'),
  saveTickerFeed:  items    => request('POST', '/api/ticker-feed', { items }),
  getNews:         ()       => request('GET',  '/api/news'),
  saveNews:        items    => request('POST', '/api/news',        { items }),
  // Trading engine (mutations need localhost or X-Engine-Secret)
  getEngineConfig: ()       => request('GET',  '/api/engine/config'),
  setEngineConfig: (values, secret) =>
    request('POST', '/api/engine/config', values,
            secret ? { 'X-Engine-Secret': secret } : undefined),
  restartEngine:   secret   =>
    request('POST', '/api/engine/restart', {},
            secret ? { 'X-Engine-Secret': secret } : undefined),
  getEngineReport: (days = 14) => request('GET', `/api/engine/report?days=${days}`),
};
