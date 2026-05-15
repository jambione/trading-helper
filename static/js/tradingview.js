/**
 * tradingview.js — TradingView chart widget manager
 *
 * Single responsibility: load and reload the TradingView chart
 * when a ticker is selected from the table.
 *
 * Uses the official TradingView Widget JS (loaded in the HTML head).
 * Creates a new widget instance each time the symbol changes.
 */

import { subscribe, get } from './store.js?v=31';

let _panel       = null;   // outer panel element
let _placeholder = null;   // empty-state element
let _widgetWrap  = null;   // widget host element
let _symbolEl    = null;   // badge showing current symbol
let _current     = null;   // last loaded symbol

export function init(panelEl) {
  _panel       = panelEl;
  _placeholder = panelEl.querySelector('[data-tv-placeholder]');
  _widgetWrap  = panelEl.querySelector('[data-tv-widget]');
  _symbolEl    = panelEl.querySelector('[data-tv-symbol]');

  console.log('[tv] init — panel:', !!panelEl, 'placeholder:', !!_placeholder, 'wrap:', !!_widgetWrap);

  subscribe('selectedTicker', ticker => {
    console.log('[tv] selectedTicker →', ticker, '(current:', _current, ')');
    if (ticker && ticker !== _current) {
      _current = ticker;
      _loadChart(ticker);
    }
  });
}

// ── Chart loading ──────────────────────────────────────────────

function _loadChart(symbol) {
  console.log('[tv] _loadChart', symbol, '— window.TradingView:', !!window.TradingView);

  if (_symbolEl) {
    _symbolEl.textContent   = symbol;
    _symbolEl.style.display = '';
  }
  if (_placeholder) _placeholder.classList.add('hidden');
  if (_widgetWrap)  _widgetWrap.classList.remove('hidden');

  // Clear previous widget
  const containerId = 'tv_chart_container';
  const container = document.getElementById(containerId);
  if (container) container.innerHTML = '';

  if (window.TradingView) {
    _createWidget(containerId, symbol);
  } else {
    console.warn('[tv] tv.js not ready — polling…');
    let waited = 0;
    const poll = setInterval(() => {
      waited += 100;
      if (window.TradingView) {
        clearInterval(poll);
        console.log('[tv] tv.js ready after', waited, 'ms');
        _createWidget(containerId, symbol);
      } else if (waited >= 10000) {
        clearInterval(poll);
        console.error('[tv] tv.js failed to load after 10s — check network');
        if (container) container.innerHTML =
          '<div style="color:#ff6b6b;padding:20px;font-size:13px;">Chart failed to load.<br>Check your internet connection.</div>';
      }
    }, 100);
  }
}

function _createWidget(containerId, symbol) {
  console.log('[tv] _createWidget', symbol);

  const widgetOpts = {
    autosize:            true,
    symbol,
    interval:            '1',
    timezone:            'America/New_York',
    theme:               'dark',
    style:               '1',
    locale:              'en',
    toolbar_bg:          '#090f1d',
    enable_publishing:   false,
    hide_side_toolbar:   false,
    allow_symbol_change: true,
    save_image:          false,
    container_id:        containerId,
  };

  widgetOpts.studies = [
    'MACD@tv-basicstudies',
    'Volume@tv-basicstudies',
    'VWAP@tv-basicstudies',
  ];

  try {
    new window.TradingView.widget(widgetOpts);
    console.log('[tv] widget created for', symbol);
  } catch (err) {
    console.error('[tv] widget creation failed:', err);
  }
}
