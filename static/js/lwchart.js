/**
 * lwchart.js — Real-time candlestick chart via TradingView Lightweight Charts
 *
 * Data sources:
 *   Historical bars  → GET /api/candles/{ticker}?tf=1Min  (Alpaca)
 *   Live price ticks → /ws snapshot tickers[].price       (Finnhub stream)
 */

import { subscribe } from './store.js?v=46';

const LWC_SRC = 'https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js';

const TF_MS = { '1Min': 60, '5Min': 300, '15Min': 900, '1Hour': 3600 };

let _panel        = null;
let _placeholder  = null;
let _widgetWrap   = null;
let _symbolEl     = null;
let _ohlcvEl      = null;
let _tfBtns       = [];

let _chart        = null;
let _candleSeries = null;
let _volSeries    = null;
let _current      = null;
let _tf           = '1Min';
let _liveBar      = null;   // current incomplete candle built from ticks

// ── Public API ────────────────────────────────────────────────

export function init(panelEl) {
  _panel       = panelEl;
  _placeholder = panelEl.querySelector('[data-tv-placeholder]');
  _widgetWrap  = panelEl.querySelector('[data-tv-widget]');
  _symbolEl    = panelEl.querySelector('[data-tv-symbol]');
  _ohlcvEl     = panelEl.querySelector('[data-chart-ohlcv]');
  _tfBtns      = [...panelEl.querySelectorAll('[data-tf]')];

  _tfBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      _tf = btn.dataset.tf;
      _tfBtns.forEach(b => b.classList.toggle('chart-tf-btn--active', b === btn));
      _liveBar = null;
      if (_current) _loadCandles(_current);
    });
  });

  _ensureLwc().then(() => _buildChart());

  subscribe('selectedTicker', ticker => {
    if (ticker && ticker !== _current) {
      _current = ticker;
      _liveBar = null;
      _loadCandles(ticker);
    }
  });

  // Real-time price ticks from the WS snapshot
  subscribe('tickers', rows => {
    if (!_current || !_candleSeries || !rows) return;
    const row = rows.find(r => r.ticker === _current);
    if (!row || row.price == null) return;
    _tickLiveBar(parseFloat(row.price));
  });
}

// ── Chart construction ────────────────────────────────────────

function _buildChart() {
  const container = document.getElementById('tv_chart_container');
  if (!container || !window.LightweightCharts) return;

  _chart = window.LightweightCharts.createChart(container, {
    layout: {
      background: { color: '#090f1d' },
      textColor:  '#94a3b8',
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize:   11,
    },
    grid: {
      vertLines: { color: 'rgba(94,234,212,0.05)' },
      horzLines: { color: 'rgba(94,234,212,0.05)' },
    },
    crosshair: { mode: window.LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: {
      borderColor: 'rgba(94,234,212,0.12)',
      textColor:   '#94a3b8',
    },
    timeScale: {
      borderColor:    'rgba(94,234,212,0.12)',
      textColor:      '#94a3b8',
      timeVisible:    true,
      secondsVisible: false,
      fixLeftEdge:    false,
    },
    width:  container.clientWidth,
    height: container.clientHeight,
  });

  _candleSeries = _chart.addCandlestickSeries({
    upColor:         '#22c55e',
    downColor:       '#ef4444',
    borderUpColor:   '#22c55e',
    borderDownColor: '#ef4444',
    wickUpColor:     '#22c55e',
    wickDownColor:   '#ef4444',
  });

  // Volume pane — occupies bottom 20% of the chart area
  _volSeries = _chart.addHistogramSeries({
    priceFormat:   { type: 'volume' },
    priceScaleId:  'vol',
    scaleMargins:  { top: 0.82, bottom: 0 },
    lastValueVisible: false,
    priceLineVisible: false,
  });
  _chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  // OHLCV tooltip on crosshair move
  if (_ohlcvEl) {
    _chart.subscribeCrosshairMove(param => {
      if (!param || !param.time || !_candleSeries) {
        _ohlcvEl.classList.add('hidden');
        return;
      }
      const candle = param.seriesData?.get(_candleSeries);
      if (!candle) { _ohlcvEl.classList.add('hidden'); return; }

      _ohlcvEl.classList.remove('hidden');
      _ohlcvEl.querySelector('[data-ohlcv-o]').textContent = _fmt(candle.open);
      _ohlcvEl.querySelector('[data-ohlcv-h]').textContent = _fmt(candle.high);
      _ohlcvEl.querySelector('[data-ohlcv-l]').textContent = _fmt(candle.low);
      _ohlcvEl.querySelector('[data-ohlcv-c]').textContent = _fmt(candle.close);

      const vol = param.seriesData?.get(_volSeries);
      _ohlcvEl.querySelector('[data-ohlcv-v]').textContent =
        vol ? _fmtVol(vol.value) : '—';

      const isUp = candle.close >= candle.open;
      _ohlcvEl.querySelector('[data-ohlcv-c]').style.color =
        isUp ? '#22c55e' : '#ef4444';
    });
  }

  // Auto-resize when the panel resizes
  new ResizeObserver(() => {
    if (_chart && container) {
      _chart.applyOptions({
        width:  container.clientWidth,
        height: container.clientHeight,
      });
    }
  }).observe(container);
}

// ── Data loading ──────────────────────────────────────────────

async function _loadCandles(ticker) {
  if (_symbolEl) {
    _symbolEl.textContent   = ticker;
    _symbolEl.style.display = '';
  }
  if (_placeholder) _placeholder.classList.add('hidden');
  if (_widgetWrap)  _widgetWrap.classList.remove('hidden');

  if (!_chart) {
    await _ensureLwc();
    _buildChart();
  }

  try {
    const res = await fetch(`/api/candles/${ticker}?tf=${_tf}`);
    if (!res.ok) return;
    const { candles } = await res.json();
    if (!candles?.length) return;

    _candleSeries.setData(candles);
    _volSeries.setData(candles.map(c => ({
      time:  c.time,
      value: c.volume,
      color: c.close >= c.open ? '#22c55e28' : '#ef444428',
    })));

    // Seed live bar from the most-recent historical bar
    const last = candles[candles.length - 1];
    const barSec = TF_MS[_tf] ?? 60;
    const nowSec = Math.floor(Date.now() / 1000);
    const barTime = nowSec - (nowSec % barSec);
    _liveBar = last.time === barTime ? { ...last } : null;

    _chart.timeScale().scrollToRealTime();
  } catch (err) {
    console.error('[lwc] fetch error:', err);
  }
}

// ── Real-time candle building ─────────────────────────────────

function _tickLiveBar(price) {
  const barSec  = TF_MS[_tf] ?? 60;
  const nowSec  = Math.floor(Date.now() / 1000);
  const barTime = nowSec - (nowSec % barSec);

  if (!_liveBar || _liveBar.time < barTime) {
    _liveBar = { time: barTime, open: price, high: price, low: price, close: price, volume: 0 };
  } else {
    _liveBar.high  = Math.max(_liveBar.high, price);
    _liveBar.low   = Math.min(_liveBar.low,  price);
    _liveBar.close = price;
  }

  _candleSeries.update(_liveBar);
  _volSeries.update({
    time:  _liveBar.time,
    value: _liveBar.volume,
    color: _liveBar.close >= _liveBar.open ? '#22c55e28' : '#ef444428',
  });
}

// ── Helpers ───────────────────────────────────────────────────

function _ensureLwc() {
  if (window.LightweightCharts) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src     = LWC_SRC;
    s.onload  = resolve;
    s.onerror = () => reject(new Error('[lwc] failed to load script'));
    document.head.appendChild(s);
  });
}

function _fmt(v) {
  if (v == null) return '—';
  return v < 10 ? v.toFixed(4) : v.toFixed(2);
}

function _fmtVol(v) {
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + 'M';
  if (v >= 1_000)     return (v / 1_000).toFixed(1) + 'K';
  return String(v);
}
