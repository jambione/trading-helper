/**
 * tradingview.js — TradingView chart widget manager
 *
 * Single responsibility: load and reload the TradingView chart
 * when a ticker is selected from the table.
 *
 * Loads tv.js on first need (not in <head>) so the desk can paint sooner.
 */

import { subscribe, get } from './store.js?v=87';

let _panel       = null;   // outer panel element
let _placeholder = null;   // empty-state element
let _widgetWrap  = null;   // widget host element
let _symbolEl    = null;   // badge showing current symbol
let _current     = null;   // last loaded symbol
/** @type {Promise<void> | null} */
let _tvScript    = null;

export function init(panelEl) {
  _panel       = panelEl;
  _placeholder = panelEl.querySelector('[data-tv-placeholder]');
  _widgetWrap  = panelEl.querySelector('[data-tv-widget]');
  _symbolEl    = panelEl.querySelector('[data-tv-symbol]');

  subscribe('selectedTicker', ticker => {
    if (ticker && ticker !== _current) {
      _current = ticker;
      _loadChart(ticker);
    }
  });

  // Restore chart if a ticker was already selected before this module inited
  const existing = get('selectedTicker');
  if (existing && existing !== _current) {
    _current = existing;
    _loadChart(existing);
  }
}

// ── Chart loading ──────────────────────────────────────────────

function _ensureTvScript() {
  if (window.TradingView) return Promise.resolve();
  if (_tvScript) return _tvScript;
  _tvScript = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://s3.tradingview.com/tv.js';
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => {
      _tvScript = null;
      reject(new Error('tv.js failed to load'));
    };
    document.head.appendChild(s);
  });
  return _tvScript;
}

async function _loadChart(symbol) {
  if (_symbolEl) {
    _symbolEl.textContent   = symbol;
    _symbolEl.style.display = '';
  }
  if (_placeholder) _placeholder.classList.add('hidden');
  if (_widgetWrap)  _widgetWrap.classList.remove('hidden');

  const containerId = 'tv_chart_container';
  const container = document.getElementById(containerId);
  if (container) container.innerHTML = '';

  try {
    await _ensureTvScript();
  } catch (err) {
    console.error('[tv]', err);
    if (container) {
      container.innerHTML =
        '<div style="color:#ff6b6b;padding:20px;font-size:13px;">Chart failed to load.<br>Check your internet connection.</div>';
    }
    return;
  }

  // Another ticker may have been selected while the script was loading
  if (_current !== symbol) return;
  _createWidget(containerId, symbol);
}

function _createWidget(containerId, symbol) {
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
  } catch (err) {
    console.error('[tv] widget creation failed:', err);
  }
}
