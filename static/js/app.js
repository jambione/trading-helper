/**
 * app.js — Application bootstrap & module orchestration
 *
 * Single responsibility: wire up modules, route events between api↔store↔UI.
 * No rendering logic lives here — that belongs in the component modules.
 */

import { connect, on, api }                      from './api.js?v=46';
import { subscribe, set }                        from './store.js?v=46';
import { init as initTranscription }             from './transcription.js?v=46';
import { init as initTickers }                   from './tickers.js?v=46';
import { init as initTradingView }               from './tradingview.js?v=46';
import { init as initConfig, open as openConfig, updateFeedbackBadge } from './config.js?v=46';
import { init as initResizer }                   from './resizer.js?v=46';
import * as controls                             from './controls.js?v=46';
import * as notifications                        from './notifications.js?v=46';
import { isAuthenticated, logout, getQueryUser } from './auth.js?v=46';
import { init as initNews }                      from './news.js?v=46';
import { init as initLeaderboard }               from './leaderboard.js?v=46';
import { init as initAdmin, open as openAdmin }  from './admin.js?v=46';
import { init as initHotkeys, registerHotkey }   from './hotkeys.js?v=46';
import { init as initSessions, refresh as refreshSessions } from './sessions.js';

// Build badge — shows which code the dashboard and the signal engine are each
// running, so a stale or mismatched process is obvious at a glance. Amber when
// they differ, the engine is stale (>30s since its last write), or it's off.
function _renderBuildBadge(v) {
  const el = document.querySelector('[data-build-badge]');
  if (!el) return;
  const dash = v.dashboard || '?';
  const eng  = v.engine || null;
  let stale = true;
  if (v.engine_updated) {
    const age = (Date.now() - Date.parse(v.engine_updated)) / 1000;
    stale = !(age >= 0 && age < 30);
  }
  const mismatch = eng && eng !== dash;
  const engTxt   = eng ? `engine ${eng}${v.engine_strategy ? ' · ' + v.engine_strategy : ''}` : 'engine off';
  const ok       = eng && !mismatch && !stale;
  el.textContent = `${dash} · ${(eng && stale) ? '⚠ ' : ''}${engTxt}`;
  el.className   = `build-badge ${ok ? 'build-badge--ok' : 'build-badge--warn'}`;
  el.title = [
    `dashboard build: ${dash}`,
    `engine build: ${eng || '(not running)'}`,
    v.engine_strategy ? `engine strategy: ${v.engine_strategy}` : '',
    v.engine_started  ? `engine started: ${v.engine_started}` : '',
    v.engine_updated  ? `engine last write: ${v.engine_updated}${(eng && stale) ? '  (STALE — engine not writing)' : ''}` : '',
    mismatch ? '⚠ dashboard and engine are on DIFFERENT builds — restart the engine' : '',
  ].filter(Boolean).join('\n');
}

document.addEventListener('DOMContentLoaded', async () => {

  // ── Auth gate ────────────────────────────────────────────────
  // Ask the backend whether auth is required before enforcing a login redirect.
  // On localhost the backend always runs locally — never require auth if the
  // hostname is localhost/127.0.0.1 regardless of fetch outcome.
  const _isLocal  = ['localhost', '127.0.0.1', ''].includes(window.location.hostname);
  const _isMobile = document.body.classList.contains('mobile');

  // Clear any stale backend URL left over from the old login page field.
  // Everything now runs on the same origin as the page.
  localStorage.removeItem('ss:backend-url');
  const _queryUser = getQueryUser();
  let authRequired = false;
  let _isAdmin     = _queryUser === 'jmb';
  let _tokenSent   = false;
  try {
    const token = localStorage.getItem('ss:token') || '';
    _tokenSent  = !!token;
    const url = '/api/meta' + (_queryUser ? ('?user=' + encodeURIComponent(_queryUser)) : '');
    const res   = await fetch(url, {
      headers: token ? { 'Authorization': 'Bearer ' + token } : {},
    });
    const meta  = await res.json();
    authRequired = _isLocal ? false : (meta.auth_required ?? false);
    _isAdmin     = meta.is_admin || _isAdmin;
    if (meta.auth_required) document.body.classList.add('auth-on');
  } catch {
    // Backend unreachable — localhost is always open; remote requires a token
    authRequired = _isLocal ? false : !isAuthenticated();
  }

  if (authRequired && !isAuthenticated()) {
    window.location.href = '/';
    return;
  }

  // Confirm admin status from the server (the inline script already set user-jmb
  // before paint via the JWT payload, so this is a no-op in the normal case).
  if (_isAdmin) {
    document.body.classList.add('user-jmb');
  } else if (_tokenSent) {
    // Server had a valid token and explicitly said not-admin — strip the class.
    // This handles revoked admin without affecting anonymous or failed fetches.
    document.body.classList.remove('user-jmb');
  }

  // Hosted mode: transcript + settings are localhost-only features.
  // Use _isLocal directly so this works regardless of auth settings.
  if (!_isLocal) document.body.classList.add('hosted');

  // ── Initialize UI components ─────────────────────────────────
  // Wrapped individually so one failure doesn't block the rest.
  // Transcription is localhost-only. Admin on remote does not get it.
  if (_isLocal) {
    try { initTranscription(document.querySelector('[data-panel="transcript"]')); } catch (e) { console.error('[app] initTranscription', e); }
  }
  try { initTickers(document.querySelector('[data-panel="tickers"]')); }          catch (e) { console.error('[app] initTickers', e); }
  try { initNews(document.querySelector('[data-news]')); }                        catch (e) { console.error('[app] initNews', e); }
  try { initLeaderboard(document.querySelector('[data-leaderboard]')); }         catch (e) { console.error('[app] initLeaderboard', e); }
  if (!_isMobile) {
    try { initTradingView(document.querySelector('[data-panel="tradingview"]')); } catch (e) { console.error('[app] initTradingView', e); }
  }
  try { initConfig(document.querySelector('[data-drawer="config"]')); }           catch (e) { console.error('[app] initConfig', e); }
  try { initAdmin(document.querySelector('[data-drawer="admin"]')); }             catch (e) { console.error('[app] initAdmin', e); }
  try { initResizer(document.querySelector('.main-grid'), document.getElementById('ticker-tv-resizer'), { hosted: !_isLocal }); } catch (e) { console.error('[app] initResizer', e); }
  try { initSessions(); }                                                          catch (e) { console.error('[app] initSessions', e); }
  notifications.init();

  // ── Refresh sessions when Settings → Sessions tab is opened ──
  document.querySelector('[data-tab-btn="sessions"]')?.addEventListener('click', () => {
    try { refreshSessions(); } catch {}
  });

  // ── Hotkeys ──────────────────────────────────────────────────
  try {
    initHotkeys(
      document.getElementById('hotkey-panel'),
      document.getElementById('hotkey-btn'),
    );
    // ALT+A — toggle Auto-Add (jmb only; safe to register for all, checkbox won't exist otherwise)
    registerHotkey('a', 'Toggle Auto-Add', () => {
      const cb = document.getElementById('auto-add-checkbox');
      if (cb) { cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); }
    });
    // ALT+N — toggle browser alerts
    registerHotkey('n', 'Toggle Alerts', () => {
      document.querySelector('[data-notif-btn]')?.click();
    });
    // ALT+S — open Settings
    registerHotkey('s', 'Open Settings', () => {
      document.querySelector('[data-settings-btn]')?.click();
    });
  } catch (e) { console.error('[app] initHotkeys', e); }

  // ── Wire button actions ──────────────────────────────────────
  const txBtn     = document.querySelector('[data-tx-btn]');
  const clrWlBtn  = document.querySelector('[data-clear-watchlist-btn]');
  const clrTxBtn  = document.querySelector('[data-clear-transcript-btn]');
  const settBtn   = document.querySelector('[data-settings-btn]');
  const adminBtn  = document.querySelector('[data-admin-btn]');
  const notifBtn  = document.querySelector('[data-notif-btn]');
  const logoutBtn = document.querySelector('[data-logout-btn]');

  txBtn    ?.addEventListener('click', () => controls.toggleTranscriber(txBtn));
  clrWlBtn ?.addEventListener('click', () => controls.clearWatchlist());
  clrTxBtn ?.addEventListener('click', () => controls.clearTranscript());
  settBtn  ?.addEventListener('click', openConfig);
  adminBtn ?.addEventListener('click', openAdmin);
  logoutBtn?.addEventListener('click', logout);

  const addInput = document.getElementById('add-ticker-input');
  const addBtn   = document.querySelector('[data-add-ticker-btn]');
  addBtn?.addEventListener('click', () => controls.addTicker(addInput, addBtn));
  addInput?.addEventListener('keydown', e => {
    if (e.key === 'Enter') controls.addTicker(addInput, addBtn);
  });
  addInput?.addEventListener('input', e => {
    const pos = e.target.selectionStart;
    e.target.value = e.target.value.toUpperCase();
    e.target.setSelectionRange(pos, pos);
  });

  // ── Admin: ticker feed add-item form ────────────────────────
  const feedInput  = document.getElementById('feed-item-input');
  const feedType   = document.getElementById('feed-item-type');
  const feedAddBtn = document.getElementById('feed-item-add-btn');
  const _addFeedItem = async () => {
    const text = feedInput?.value.trim();
    const type = feedType?.value || 'info';
    if (!text) return;
    const m = await import('./admin.js?v=46');
    m.addFeedItem(type, text);
    if (feedInput) feedInput.value = '';
  };
  feedAddBtn?.addEventListener('click', _addFeedItem);
  feedInput  ?.addEventListener('keydown', e => { if (e.key === 'Enter') _addFeedItem(); });

  // ── Feedback badge — check on startup ───────────────────────
  try {
    const { suggestions = [] } = await api.getSuggestions();
    updateFeedbackBadge(suggestions.length);
  } catch { /* non-fatal */ }

  // ── Suggestion bar ───────────────────────────────────────────
  const suggInput = document.getElementById('suggestion-input');
  const suggBtn   = document.getElementById('suggestion-btn');
  const _submitSuggestion = async () => {
    const msg = suggInput?.value.trim();
    if (!msg) return;
    suggBtn.disabled = true;
    suggBtn.textContent = '…';
    try {
      await api.addSuggestion(msg);
      suggInput.value    = '';
      suggBtn.textContent = 'Sent ✓';
      // Re-check count so the badge reflects the new submission
      try {
        const { suggestions = [] } = await api.getSuggestions();
        updateFeedbackBadge(suggestions.length);
      } catch { /* non-fatal */ }
      setTimeout(() => { suggBtn.textContent = 'Send'; suggBtn.disabled = false; }, 2000);
    } catch {
      suggBtn.textContent = 'Error';
      setTimeout(() => { suggBtn.textContent = 'Send'; suggBtn.disabled = false; }, 2000);
    }
  };
  suggBtn  ?.addEventListener('click', _submitSuggestion);
  suggInput?.addEventListener('keydown', e => { if (e.key === 'Enter') _submitSuggestion(); });

  notifBtn?.addEventListener('click', async () => {
    const granted = await notifications.requestPermission();
    _syncNotifBtn(notifBtn, granted);
  });

  // Reflect already-granted state from a previous session
  _syncNotifBtn(notifBtn, notifications.isEnabled());

  // ── WebSocket → store (snapshot ingest) ──────────────────────
  on('message', snap => {
    const update = {};
    if (snap.tickers      !== undefined) update.tickers      = snap.tickers;
    if (snap.config)                     update.config       = snap.config;
    if (snap.transcriber)                update.transcriber  = snap.transcriber;
    if (snap.news         !== undefined) update.news         = snap.news;
    if (Object.keys(update).length)      set(update);
    if (snap.version)                    _renderBuildBadge(snap.version);
  });

  on('connected', connected => set({ connected }));

  // ── Store → connection indicators (all [data-ws-dot] elements) ──
  subscribe('connected', connected => {
    document.querySelectorAll('[data-ws-dot]').forEach(dot => {
      dot.className = `ws-dot ${connected ? 'ws-dot--on' : 'ws-dot--off'}`;
      dot.title     = connected ? 'Live' : 'Disconnected — reconnecting…';
    });
  });

  // ── Store → transcription controls ──────────────────────────
  subscribe('transcriber', tx => {
    const dot   = document.querySelector('[data-tx-dot]');
    const lbl   = document.querySelector('[data-tx-label]');
    const count = document.querySelector('[data-tx-count]');

    if (dot) dot.className = `tx-dot${tx.running ? ' tx-dot--on' : ''}`;
    if (lbl) {
      lbl.textContent = tx.running ? 'LISTENING' : 'STOPPED';
      lbl.className   = `tx-label${tx.running ? ' tx-label--on' : ''}`;
    }

    if (txBtn && !txBtn.disabled) {
      txBtn.textContent = tx.running ? 'Stop Transcription' : 'Start Transcription';
      txBtn.className   = `tx-btn ${tx.running ? 'tx-btn--stop' : 'tx-btn--start'}`;
    }

    if (count) {
      const n = tx.count ?? 0;
      count.textContent = `${n} ticker${n !== 1 ? 's' : ''} captured today`;
    }

    const audioStatus = document.querySelector('[data-audio-status]');
    if (audioStatus) audioStatus.textContent = tx.running ? 'Listening' : 'Stopped';
  });

  // ── Store → status bar ───────────────────────────────────────
  subscribe('tickers', rows => {
    const el = document.querySelector('[data-statusbar-tickers]');
    if (el) el.textContent = rows.length || '—';
  });

  // ── Persist selected ticker across reloads ───────────────────
  const _savedTicker = localStorage.getItem('ss:selected-ticker');
  if (_savedTicker) set({ selectedTicker: _savedTicker });

  subscribe('selectedTicker', ticker => {
    if (ticker) localStorage.setItem('ss:selected-ticker', ticker);
    else        localStorage.removeItem('ss:selected-ticker');
  });

  // ── Boot ─────────────────────────────────────────────────────
  connect();
});

// ── Helpers ───────────────────────────────────────────────────

function _syncNotifBtn(btn, granted) {
  if (!btn) return;
  if (granted) {
    btn.textContent = '🔔 Alerts On';
    btn.classList.add('btn--alert-on');
    btn.title = 'BUY signal alerts enabled';
  } else {
    btn.textContent = '🔔 Alerts';
    btn.classList.remove('btn--alert-on');
    btn.title = 'Click to enable BUY signal alerts';
  }
}
