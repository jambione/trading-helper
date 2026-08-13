/**
 * app.js — Application bootstrap & module orchestration
 *
 * Single responsibility: wire up modules, route events between api↔store↔UI.
 * No rendering logic lives here — that belongs in the component modules.
 */

import { connect, on, api }                      from './api.js?v=107';
import { subscribe, set, selectTicker }          from './store.js?v=107';
import { init as initFeeds }                     from './feeds.js?v=113';
import { init as initTickers }                   from './tickers.js?v=112';
import { init as initTradingView }               from './tradingview.js?v=107';
import { init as initConfig, open as openConfig, updateFeedbackBadge } from './config.js?v=107';
import { init as initResizer }                   from './resizer.js?v=107';
import * as controls                             from './controls.js?v=107';
import * as notifications                        from './notifications.js?v=107';
import { isAuthenticated, logout, getQueryUser } from './auth.js?v=107';
import { init as initNews }                      from './news.js?v=107';
import { init as initLeaderboard }               from './leaderboard.js?v=107';
import { init as initPriceSpikes }               from './priceSpikes.js?v=107';
import { init as initEngine }                    from './engine.js?v=107';
import { init as initAdmin, open as openAdmin }  from './admin.js?v=107';
import { init as initHotkeys, registerHotkey }   from './hotkeys.js?v=107';
import { init as initSessions, refresh as refreshSessions } from './sessions.js';
import { init as initMobilePager }                from './mobilePager.js?v=107';

// Product badge — "Trader Bro v0.8", replacing the old WS·Discord·AI Grok
// text row. The connectivity/trader-on dots stay (they're live status, not
// clutter); this is just the product's name + version, from a single
// backend source of truth (version.py) so bumping it is a one-line change.
function _renderProductBadge(v) {
  const el = document.querySelector('[data-product-badge]');
  if (!el) return;
  const name = v.product_name || 'Trader Bro';
  const ver  = v.product_version || '?';
  el.textContent = `${name} v${ver}`;
}

// Trader Bro's call-out suggestions — "Suggests: NRXP 8:28 AM" beside the
// product badge, the few before it trailing inline, and the whole session
// behind a click. The backend only sends a symbol it could confirm against the
// universe, so an empty `current` means the caller said something we couldn't
// read a symbol out of; the chip shows no symbol rather than guessing.
const _BB_INLINE_PAST = 3;   // recent calls shown inline; the rest need a click

// When the call was made. `said` is Discord's own stamp for the message and is
// what the operator recognises; `ts` is when OCR read it off the screen, which
// is only a fallback (on a fresh start it collapses an hour of calls into one
// minute). Seconds are noise at a glance, so the fallback drops them.
function _bbWhen(c) {
  return c?.said || String(c?.ts || '').split(':').slice(0, 2).join(':');
}

function _renderBbLive(bb) {
  const wrap = document.querySelector('[data-bb-live]');
  if (!wrap) return;
  const cur     = bb?.current || null;
  const history = bb?.history || [];

  wrap.hidden = !cur && !history.length;
  const symEl = wrap.querySelector('[data-bb-live-sym]');
  const tsEl  = wrap.querySelector('[data-bb-live-ts]');
  const chip  = wrap.querySelector('[data-bb-live-chip]');
  if (symEl && tsEl && chip) {
    // No fresh call → keep the history reachable but don't show a stale symbol.
    symEl.textContent = cur ? cur.ticker : '—';
    tsEl.textContent  = cur ? _bbWhen(cur) : '';
    chip.classList.toggle('bb-suggest-chip--live', !!cur);
    chip.title = cur
      ? `${cur.ticker} — "${cur.text}" at ${_bbWhen(cur)}`
      : 'No current call-out — click for past suggestions';
  }

  // Inline recents: the calls behind the current one, newest first. `history`
  // leads with the current call whenever there is one, so skip it there.
  const past = wrap.querySelector('[data-bb-live-past]');
  if (past) {
    past.replaceChildren();
    const earlier = history.slice(cur ? 1 : 0, (cur ? 1 : 0) + _BB_INLINE_PAST);
    for (const c of earlier) {
      const el = document.createElement('button');
      el.className = 'bb-suggest-past-chip';
      el.title     = `${c.ticker} — "${c.text}" at ${_bbWhen(c)}`;
      const sym = document.createElement('span');
      sym.className   = 'bb-suggest-past-sym';
      sym.textContent = c.ticker;
      const ts  = document.createElement('span');
      ts.className    = 'bb-suggest-past-ts';
      ts.textContent  = _bbWhen(c);
      el.append(sym, ts);
      el.addEventListener('click', () => selectTicker(c.ticker));
      past.appendChild(el);
    }
  }

  const list = wrap.querySelector('[data-bb-live-list]');
  if (!list) return;
  list.replaceChildren();
  if (!history.length) {
    const li = document.createElement('li');
    li.className   = 'bb-suggest-empty';
    li.textContent = 'No suggestions yet today.';
    list.appendChild(li);
    return;
  }
  for (const c of history) {
    const li = document.createElement('li');
    li.className = 'bb-suggest-row';
    const ts  = document.createElement('span');
    ts.className   = 'bb-suggest-row-ts';
    ts.textContent = _bbWhen(c);
    const sym = document.createElement('span');
    sym.className   = 'bb-suggest-row-sym';
    sym.textContent = c.ticker || '';
    const txt = document.createElement('span');
    txt.className   = 'bb-suggest-row-txt';
    txt.textContent = c.text || '';
    li.append(ts, sym, txt);
    li.addEventListener('click', () => {
      selectTicker(c.ticker);
      const pop = wrap.querySelector('[data-bb-live-pop]');
      if (pop) pop.hidden = true;
    });
    list.appendChild(li);
  }
}

function _initBbLive() {
  const wrap = document.querySelector('[data-bb-live]');
  if (!wrap) return;
  const chip = wrap.querySelector('[data-bb-live-chip]');
  const pop  = wrap.querySelector('[data-bb-live-pop]');
  if (!chip || !pop) return;
  chip.addEventListener('click', e => {
    e.stopPropagation();
    pop.hidden = !pop.hidden;
  });
  document.addEventListener('click', e => {
    if (!pop.hidden && !wrap.contains(e.target)) pop.hidden = true;
  });
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

  // Hosted mode: settings is a localhost-only feature.
  // Use _isLocal directly so this works regardless of auth settings.
  if (!_isLocal) document.body.classList.add('hosted');

  // Trading Engine panel: owner-only when hosted, but localhost IS the owner's
  // machine — always show it there (the engine API already trusts localhost).
  if (_isLocal) document.body.classList.add('engine-visible');

  // ── Initialize UI components ─────────────────────────────────
  // Wrapped individually so one failure doesn't block the rest.
  // Trending and AI research panels are server-fed (WebSocket), so they work hosted too.
  try { initFeeds(document.querySelector('[data-panel="trending"]'), 'trending'); } catch (e) { console.error('[app] initFeeds trending', e); }
  try { initFeeds(document.querySelector('[data-panel="claude"]'), 'claude'); }      catch (e) { console.error('[app] initFeeds claude', e); }
  try { initTickers(document.querySelector('[data-panel="tickers"]')); }          catch (e) { console.error('[app] initTickers', e); }
  try { initNews(document.querySelector('[data-news]')); }                        catch (e) { console.error('[app] initNews', e); }
  try { initLeaderboard(document.querySelector('[data-leaderboard]')); }         catch (e) { console.error('[app] initLeaderboard', e); }
  try { initPriceSpikes(document.querySelector('[data-price-spikes]')); }        catch (e) { console.error('[app] initPriceSpikes', e); }
  try { initEngine(document.querySelector('[data-panel="engine"]')); }           catch (e) { console.error('[app] initEngine', e); }
  try { _initBbLive(); }                                                          catch (e) { console.error('[app] initBbLive', e); }
  if (!_isMobile) {
    try { initTradingView(document.querySelector('[data-panel="tradingview"]')); } catch (e) { console.error('[app] initTradingView', e); }
  }
  try { initConfig(document.querySelector('[data-drawer="config"]')); }           catch (e) { console.error('[app] initConfig', e); }
  try { initAdmin(document.querySelector('[data-drawer="admin"]')); }             catch (e) { console.error('[app] initAdmin', e); }
  try { initResizer(document.querySelector('.main-grid'), document.getElementById('ticker-tv-resizer'), { hosted: !_isLocal }); } catch (e) { console.error('[app] initResizer', e); }
  try { initSessions(); }                                                          catch (e) { console.error('[app] initSessions', e); }
  try { initMobilePager(); }                                                       catch (e) { console.error('[app] initMobilePager', e); }
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
  const clrWlBtn  = document.querySelector('[data-clear-watchlist-btn]');
  const settBtn   = document.querySelector('[data-settings-btn]');
  const adminBtn  = document.querySelector('[data-admin-btn]');
  const notifBtn  = document.querySelector('[data-notif-btn]');
  const logoutBtn = document.querySelector('[data-logout-btn]');

  clrWlBtn ?.addEventListener('click', () => controls.clearWatchlist());
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
    const m = await import('./admin.js?v=107');
    m.addFeedItem(type, text);
    if (feedInput) feedInput.value = '';
  };
  feedAddBtn?.addEventListener('click', _addFeedItem);
  feedInput  ?.addEventListener('keydown', e => { if (e.key === 'Enter') _addFeedItem(); });

  // ── Suggestion bar ───────────────────────────────────────────
  const suggInput = document.getElementById('suggestion-input');
  const suggBtn   = document.getElementById('suggestion-btn');
  const _submitSuggestion = async () => {
    const msg = suggInput?.value.trim();
    if (!msg) return;
    suggBtn.disabled = true;
    suggBtn.textContent = '…';
    try {
      const res = await api.addSuggestion(msg);
      suggInput.value = '';
      if (res?.email_sent) {
        suggBtn.textContent = 'Emailed ✓';
      } else if (res?.email_configured === false) {
        // Saved on server, but SMTP not set up — operator must configure secrets
        suggBtn.textContent = 'Saved';
        console.warn('[feedback] saved but email not configured (SMTP)');
      } else {
        suggBtn.textContent = 'Saved';
      }
      // Re-check count so the badge reflects the new submission
      try {
        const { suggestions = [] } = await api.getSuggestions();
        updateFeedbackBadge(suggestions.length);
      } catch { /* non-fatal */ }
      setTimeout(() => { suggBtn.textContent = 'Send'; suggBtn.disabled = false; }, 2500);
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
    if (snap.tickers           !== undefined) update.tickers          = snap.tickers;
    if (snap.funnel            !== undefined) update.funnel            = snap.funnel;
    if (snap.config)                          update.config            = snap.config;
    if (snap.discord)                         update.discord           = snap.discord;
    if (snap.news              !== undefined) update.news              = snap.news;
    if (snap.engine)                          update.engine            = snap.engine;
    if (snap.trending          !== undefined) update.trending          = snap.trending;
    // Prefer merged AI list (A/X/AX); fall back to Anthropic-only publish.
    if (snap.ai_suggestions !== undefined) {
      update.ai_suggestions = snap.ai_suggestions;
      update.claude_suggestions = snap.ai_suggestions;
    } else if (snap.claude_suggestions !== undefined) {
      update.claude_suggestions = snap.claude_suggestions;
    }
    // Shared AI paper book (Grok/Claude owner).
    if (snap.ai_positions !== undefined) {
      update.ai_positions = snap.ai_positions;
    } else if (snap.claude_positions !== undefined) {
      update.ai_positions = snap.claude_positions;
    }
    if (snap.price_spikes      !== undefined) update.price_spikes      = snap.price_spikes;
    if (Object.keys(update).length)      set(update);
    if (snap.version) _renderProductBadge(snap.version);
    if (snap.bb_live !== undefined) _renderBbLive(snap.bb_live);
  });

  on('connected', connected => set({ connected }));

  // ── Store → connection indicators (all [data-ws-dot] elements) ──
  subscribe('connected', connected => {
    document.querySelectorAll('[data-ws-dot]').forEach(dot => {
      dot.className = `ws-dot ${connected ? 'ws-dot--on' : 'ws-dot--off'}`;
      dot.title     = connected ? 'Live' : 'Disconnected — reconnecting…';
    });
  });

  // ── AI trader on/off + book owner (header chip) ───────────────
  let _aiStatus = { pos: {}, cfg: {}, sugg: {} };
  function _renderAiTraderStatus() {
    const dot = document.querySelector('[data-ai-trader-dot]');
    const lbl = document.querySelector('[data-ai-trader-label]');
    if (!dot && !lbl) return;

    const p = _aiStatus.pos || {};
    const c = _aiStatus.cfg || {};
    const s = _aiStatus.sugg || {};
    const source = String(
      c.ai_trading_source
      || (c.grok_trading_enabled ? 'grok'
        : (c.ai_trading_enabled || c.claude_trading_enabled) ? 'claude'
        : '')
    ).toLowerCase();
    const owner = String(p.book_owner || source || '').toLowerCase();
    const mode = String(p.mode || s.trading_mode || '').toLowerCase();
    const flagOn = source === 'grok' || source === 'claude'
      || s.trading === true
      || (owner && owner !== 'none' && owner !== 'off');
    const tradingOn = flagOn && mode !== 'off';

    const nPos = p.positions && typeof p.positions === 'object'
      ? Object.keys(p.positions).length : 0;
    const ownerLabel = owner === 'grok' || source === 'grok' ? 'Grok'
      : owner === 'claude' || source === 'claude' ? 'Claude'
      : 'AI';

    if (dot) {
      dot.className = `ws-dot ws-dot--sm${tradingOn ? ' ws-dot--on' : ' ws-dot--off'}`;
      dot.title = tradingOn
        ? `AI trader on · ${ownerLabel} · ${mode || 'paper'}${nPos ? ` · ${nPos} open` : ''}`
        : 'AI trader off';
    }
    if (lbl) {
      lbl.textContent = tradingOn
        ? (nPos ? `AI ${ownerLabel} · ${nPos}` : `AI ${ownerLabel}`)
        : 'AI off';
      lbl.title = (dot && dot.title) || lbl.textContent;
      lbl.classList.toggle('hdr-status-label--on', tradingOn);
    }
  }
  subscribe('ai_positions', p => { _aiStatus.pos = p || {}; _renderAiTraderStatus(); });
  subscribe('config', c => { _aiStatus.cfg = c || {}; _renderAiTraderStatus(); });
  subscribe('ai_suggestions', s => { _aiStatus.sugg = s || {}; _renderAiTraderStatus(); });

  // ── Store → Discord OCR source status ───────────────────────
  subscribe('discord', d => {
    const dot   = document.querySelector('[data-tx-dot]');
    const lbl   = document.querySelector('[data-tx-label]');
    const count = document.querySelector('[data-tx-count]');
    const live  = !!d.running;

    if (dot) dot.className = `tx-dot${live ? ' tx-dot--on' : ''}`;
    if (lbl) {
      lbl.textContent = live ? 'LIVE' : 'OFFLINE';
      lbl.className   = `tx-label${live ? ' tx-label--on' : ''}`;
    }

    if (count) {
      const n = d.count ?? 0;
      count.textContent = `${n} ticker${n !== 1 ? 's' : ''} captured today`;
    }

    const audioStatus = document.querySelector('[data-audio-status]');
    if (audioStatus) audioStatus.textContent = live ? 'Live' : 'Offline';
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

  // ── Boot WebSocket ASAP — do not wait on feedback/suggestions ─
  connect();

  // Feedback badge — non-blocking; live desk data must not wait on this.
  api.getSuggestions()
    .then(({ suggestions = [] }) => updateFeedbackBadge(suggestions.length))
    .catch(() => { /* non-fatal */ });
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
