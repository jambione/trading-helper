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

// ── Redirect-loop guard ───────────────────────────────────────

const BOUNCE_KEY = 'ss:login-bounce';
const BOUNCE_MAX = 3;

/**
 * Charge this tab for one trip to the login page and report whether it has
 * spent them all.
 *
 * The two halves of the session are read by different parties: the server's
 * gate on "/" reads the HttpOnly cookie, this code reads the token in
 * localStorage. When they disagree — a browser that drops the cookie, a
 * rotated signing secret, a deactivated account — each side hands the browser
 * back to the other and the page flashes instead of loading. Past the ceiling
 * we stay put and let the user see a page with a Sign In button on it.
 * login.html keeps the same count under the same key.
 */
export function redirectBudgetSpent() {
  try {
    const n = (parseInt(sessionStorage.getItem(BOUNCE_KEY) || '0', 10) || 0) + 1;
    sessionStorage.setItem(BOUNCE_KEY, String(n));
    return n > BOUNCE_MAX;
  } catch {
    return false;   // no sessionStorage (private window) — never block
  }
}

/** Called once the dashboard is past the auth gate: the trip worked. */
export function clearRedirectBudget() {
  try { sessionStorage.removeItem(BOUNCE_KEY); } catch { /* no storage */ }
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

export async function logout() {
  try {
    await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
  } catch {
    // Cookie clear is best-effort; local token still goes away below.
  }
  clearToken();
  window.location.href = '/login';
}
