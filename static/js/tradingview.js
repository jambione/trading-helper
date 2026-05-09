/**
 * tradingview.js — TradingView chart widget manager
 *
 * Single responsibility: load and reload the TradingView chart
 * when a ticker is selected from the table.
 *
 * Uses the official TradingView Widget JS (loaded in the HTML head).
 * Creates a new widget instance each time the symbol changes.
 */

import { subscribe, get } from './store.js?v=11';

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

  subscribe('selectedTicker', ticker => {
    if (ticker && ticker !== _current) {
      _current = ticker;
      _loadChart(ticker);
    }
  });
}

// ── Chart loading ──────────────────────────────────────────────

function _loadChart(symbol) {
  if (_symbolEl) {
    _symbolEl.textContent   = symbol;
    _symbolEl.style.display = '';
  }
  if (_placeholder) _placeholder.classList.add('hidden');
  if (_widgetWrap)  _widgetWrap.classList.remove('hidden');

  // Clear previous widget (remove all children + reset id)
  const containerId = 'tv_chart_container';
  const container = document.getElementById(containerId);
  if (container) container.innerHTML = '';

  if (window.TradingView) {
    _createWidget(containerId, symbol);
  } else {
    // tv.js hasn't finished loading yet — poll briefly
    const poll = setInterval(() => {
      if (window.TradingView) {
        clearInterval(poll);
        _createWidget(containerId, symbol);
      }
    }, 100);
  }
}

function _createWidget(containerId, symbol) {
  const config = get('config') || {};
  const chartUrl = config.tv_chart_url || '';
  
  // Extract layout ID if a full URL was provided
  // e.g. https://www.tradingview.com/chart/x04Gfcu8/ -> x04Gfcu8
  let watchlistId = undefined;
  if (chartUrl) {
    const parts = chartUrl.split('/');
    watchlistId = parts.filter(p => p.length >= 6 && p.length <= 12 && p !== 'chart').pop();
  }

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
    withdateranges:      false,
    container_id:        containerId,
  };

  if (watchlistId) {
    widgetOpts.watchlist = [symbol];
    widgetOpts.chart = watchlistId;
  } else {
    // Only use default studies if NO custom layout is provided
    widgetOpts.studies = [
      'Volume@tv-basicstudies',
      { id: 'RSI@tv-basicstudies',  inputs: { length: 2 } },
      'MACD@tv-basicstudies',
      'OBV@tv-basicstudies',
      'WilliamsR@tv-basicstudies',
    ];
  }

  // eslint-disable-next-line no-new
  new window.TradingView.widget(widgetOpts);
}
