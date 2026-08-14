/**
 * sw.js — Minimal service worker for PWA / iOS Home Screen support.
 *
 * Goals:
 *   1. Satisfy iOS Safari's requirement for a service worker to enable
 *      Web Push Notifications when added to the Home Screen.
 *   2. Cache the app shell so it loads instantly offline (watchlist still
 *      needs the server, but the UI shell renders immediately).
 *
 * Strategy: network-first for all requests; fall back to cache on failure.
 * Cache is versioned — bump CACHE_VERSION when static assets change.
 */

const CACHE_VERSION = 'v33';
const CACHE_NAME    = `trading-${CACHE_VERSION}`;

const APP_SHELL = [
  '/',
  '/static/css/styles.css',
  '/static/js/app.js',
  '/static/js/tickers.js',
  '/static/js/notifications.js',
  '/static/js/tradingview.js',
  '/static/js/store.js',
  '/static/js/api.js',
  '/static/js/auth.js',
  '/static/js/controls.js',
  '/static/js/config.js',
  '/static/js/resizer.js',
  '/static/js/transcription.js',
  '/static/manifest.json',
];

// ── Install: pre-cache app shell ──────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

// ── Activate: delete old caches ───────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(k => k.startsWith('trading-') && k !== CACHE_NAME)
          .map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: network-first, cache fallback ──────────────────────────────────────
self.addEventListener('fetch', event => {
  // Don't intercept WebSocket upgrades or non-GET requests
  if (event.request.method !== 'GET') return;
  if (event.request.url.includes('/ws')) return;
  if (event.request.url.includes('/api/')) return;

  event.respondWith(
    fetch(event.request)
      .then(resp => {
        // Cache successful responses for static assets
        if (resp.ok && event.request.url.includes('/static/')) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
        }
        return resp;
      })
      .catch(() => caches.match(event.request))
  );
});

// ── Push notifications (for future server-side push) ─────────────────────────
self.addEventListener('push', event => {
  if (!event.data) return;
  try {
    const data = event.data.json();
    event.waitUntil(
      self.registration.showNotification(data.title || 'Trading Alert', {
        body:    data.body   || '',
        tag:     data.tag    || 'trading-alert',
        icon:    '/static/icons/icon-192.png',
        badge:   '/static/icons/icon-192.png',
        vibrate: [200, 100, 200],
        data:    { url: data.url || '/dashboard' },
      })
    );
  } catch {
    const text = event.data.text();
    event.waitUntil(
      self.registration.showNotification('Trading Alert', { body: text })
    );
  }
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/dashboard';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(wins => {
      const existing = wins.find(w => w.url.includes('/dashboard'));
      if (existing) return existing.focus();
      return clients.openWindow(url);
    })
  );
});
