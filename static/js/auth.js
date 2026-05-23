/**
 * auth.js — Client-side auth utilities
 *
 * Manages JWT token and backend URL in localStorage.
 * Used by api.js to inject auth headers and by app.js to gate access.
 */

const TOKEN_KEY   = 'ss:token';
const BACKEND_KEY = 'ss:backend-url';

// ── Token ─────────────────────────────────────────────────────

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

/**
 * Client-side expiry check — does NOT verify the signature.
 * Signature verification happens on the backend for every request.
 */
export function isAuthenticated() {
  const token = getToken();
  if (!token) return false;
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return false;
    const p   = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const pad = p.length % 4 ? '='.repeat(4 - p.length % 4) : '';
    const { exp } = JSON.parse(atob(p + pad));
    return (exp || 0) > Date.now() / 1000;
  } catch {
    return false;
  }
}

// ── Backend URL ───────────────────────────────────────────────

/**
 * Returns the stored backend URL, or '' (empty string = same origin).
 * api.js prepends this to every API path.
 */
export function getBackendUrl() {
  return localStorage.getItem(BACKEND_KEY) || '';
}

export function setBackendUrl(url) {
  const cleaned = (url || '').trim().replace(/\/$/, '');
  if (cleaned && cleaned !== window.location.origin) {
    localStorage.setItem(BACKEND_KEY, cleaned);
  } else {
    localStorage.removeItem(BACKEND_KEY);
  }
}

// ── Session ───────────────────────────────────────────────────

export function getQueryUser() {
  try {
    return (new URLSearchParams(window.location.search).get('user') || '').trim().toLowerCase();
  } catch {
    return '';
  }
}

export function logout() {
  clearToken();
  window.location.href = '/';
}
