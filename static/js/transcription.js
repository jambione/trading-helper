/**
 * transcription.js — Discord Alerts panel component
 *
 * Renders three columns: regular alerts, squeeze alerts, and a sentiment feed.
 * The panel header also shows a live market-direction sentiment pill.
 */

import { subscribe } from './store.js?v=57';

let _regularBox    = null;
let _squeezeBox    = null;
let _sentimentBox  = null;
let _marketSentEl  = null;
let _known = new Set();

let _lastRegularCount  = -1;
let _lastRegularKey    = '';
let _lastSqueezeCount  = -1;
let _lastSqueezeKey    = '';
let _lastSentCount     = -1;
let _lastSentKey       = '';

export function init(panelEl) {
  _regularBox   = panelEl.querySelector('[data-regular-box]');
  _squeezeBox   = panelEl.querySelector('[data-squeeze-box]');
  _sentimentBox = panelEl.querySelector('[data-sentiment-box]');
  _marketSentEl = panelEl.querySelector('[data-market-sent]');

  subscribe('tickers', rows => {
    _known = new Set(rows.map(r => r.ticker));
  });

  subscribe('discord', d => {
    const all     = d.alerts ?? [];
    const regular = all.filter(a => !a.burst);
    const squeeze = all.filter(a =>  a.burst);
    _renderFeed(_regularBox, regular, 'regular');
    _renderFeed(_squeezeBox, squeeze, 'squeeze');
    _renderSentimentFeed(d.sentiment_feed ?? []);
  });

  subscribe('market_sentiment', ms => {
    _renderMarketSentPill(ms);
  });
}

// ── Market sentiment pill ──────────────────────────────────

function _renderMarketSentPill(ms) {
  if (!_marketSentEl) return;
  const count = ms?.count ?? 0;
  const score = ms?.score ?? 0;
  if (count === 0) {
    _marketSentEl.hidden = true;
    return;
  }
  const abs   = Math.abs(score);
  const dir   = score >= 0.1 ? 'bull' : score <= -0.1 ? 'bear' : 'flat';
  const arrow = dir === 'bull' ? '▲' : dir === 'bear' ? '▼' : '—';
  const label = `${arrow} SPY ${score >= 0 ? '+' : ''}${score.toFixed(2)}`;

  _marketSentEl.hidden    = false;
  _marketSentEl.textContent = label;
  _marketSentEl.className = `market-sent-pill market-sent-pill--${dir}`;
  _marketSentEl.title     = `${count} signal${count !== 1 ? 's' : ''} in last 30 min`;
}

// ── Sentiment feed column ──────────────────────────────────

function _renderSentimentFeed(events) {
  if (!_sentimentBox) return;

  if (!events.length) {
    if (_lastSentCount !== 0) {
      _sentimentBox.innerHTML = '<span class="tx-placeholder">No signals…</span>';
      _lastSentCount = 0;
      _lastSentKey   = '';
    }
    return;
  }

  const tail    = events[events.length - 1];
  const tailKey = `${tail.ts}|${tail.ticker}`;

  if (tailKey === _lastSentKey && events.length === _lastSentCount) return;

  const atBottom = _sentimentBox.scrollHeight - _sentimentBox.scrollTop - _sentimentBox.clientHeight < 60;

  if (events.length > _lastSentCount && _lastSentCount > 0) {
    _sentimentBox.insertAdjacentHTML('beforeend', events.slice(_lastSentCount).map(_renderSentEvent).join(''));
  } else {
    _sentimentBox.innerHTML = events.map(_renderSentEvent).join('');
  }

  _lastSentCount = events.length;
  _lastSentKey   = tailKey;

  if (atBottom) _sentimentBox.scrollTop = _sentimentBox.scrollHeight;
}

function _renderSentEvent(e) {
  const ts      = `<span class="tx-ts">${_esc(e.ts_str || '')}</span>`;
  const score   = e.score ?? 0;
  const dir     = score >= 0.05 ? 'bull' : score <= -0.05 ? 'bear' : 'flat';
  const scoreTxt = `${score >= 0 ? '+' : ''}${score.toFixed(2)}`;
  const scoreSp = `<span class="tx-sent-score tx-sent-score--${dir}">${scoreTxt}</span>`;
  const tkr     = e.ticker
    ? `<strong class="tx-sent-ticker">${_esc(e.ticker)}</strong>`
    : `<span class="tx-sent-ticker tx-sent-ticker--mkt">mkt</span>`;
  const src     = `<span class="tx-sent-source">${_esc(_shortSource(e.source || ''))}</span>`;
  const raw     = e.raw ? `<span class="tx-sent-raw">${_esc(e.raw.slice(0, 45))}</span>` : '';
  return `<div class="tx-line tx-sent-row">${ts}${tkr}${scoreSp}${src}${raw}</div>`;
}

function _shortSource(src) {
  if (src === 'hv_alert') return 'HV';
  if (src === 'tv_chart') return 'TV';
  if (src === 'scanner')  return 'SCN';
  if (src === 'chat')     return '';
  return src.slice(0, 4).toUpperCase();
}

// ── Alert feed columns (regular + squeeze) ─────────────────

function _renderFeed(box, alerts, type) {
  if (!box) return;

  const isSqueeze   = type === 'squeeze';
  const lastCount   = isSqueeze ? _lastSqueezeCount : _lastRegularCount;
  const lastKey     = isSqueeze ? _lastSqueezeKey   : _lastRegularKey;

  if (!alerts.length) {
    const placeholder = isSqueeze ? 'No squeezes…' : 'Waiting…';
    if (lastCount !== 0) {
      box.innerHTML = `<span class="tx-placeholder">${placeholder}</span>`;
      if (isSqueeze) { _lastSqueezeCount = 0; _lastSqueezeKey = ''; }
      else           { _lastRegularCount = 0; _lastRegularKey = ''; }
    }
    return;
  }

  const tail    = alerts[alerts.length - 1];
  const tailKey = `${tail.ts}|${tail.ticker}`;

  if (tailKey === lastKey && alerts.length === lastCount) return;

  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const render   = isSqueeze ? _renderSqueeze : _renderRegular;

  if (alerts.length > lastCount && lastCount > 0) {
    box.insertAdjacentHTML('beforeend', alerts.slice(lastCount).map(render).join(''));
  } else {
    box.innerHTML = alerts.map(render).join('');
  }

  if (isSqueeze) { _lastSqueezeCount = alerts.length; _lastSqueezeKey = tailKey; }
  else           { _lastRegularCount = alerts.length; _lastRegularKey = tailKey; }

  if (atBottom) box.scrollTop = box.scrollHeight;
}

function _renderRegular(a) {
  const ts       = `<span class="tx-ts">${_esc(a.ts || '')}</span>`;
  const ticker   = `<strong class="tx-ticker">${_esc(a.ticker || '')}</strong>`;
  const typeRow  = a.alert_type
    ? `<div class="tx-alert-type">${_esc(a.alert_type)}</div>` : '';
  const price    = a.price  != null ? `<span class="tx-price">$${Number(a.price).toFixed(2)}</span>` : '';
  const vol      = a.volume != null ? `<span class="tx-vol">${_fmtVol(a.volume)}</span>` : '';
  const metaRow  = (price || vol) ? `<div class="tx-meta-row">${price}${vol}</div>` : '';
  return `<div class="tx-line">${ts}${ticker}${typeRow}${metaRow}</div>`;
}

function _renderSqueeze(a) {
  const ts     = `<span class="tx-ts">${_esc(a.ts || '')}</span>`;
  const ticker = `<strong class="tx-ticker tx-ticker--squeeze">${_esc(a.ticker || '')}</strong>`;
  const levels = (a.levels || []).filter(l => !isNaN(Number(l))).map(l => `<span class="tx-level-chip">$${Number(l).toFixed(2)}</span>`).join('');
  const levRow = levels ? `<div class="tx-levels">${levels}</div>` : '';
  return `<div class="tx-line tx-line--squeeze">${ts}${ticker}${levRow}</div>`;
}

function _fmtVol(v) {
  v = Number(v);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M vol';
  if (v >= 1_000)     return Math.round(v / 1_000) + 'K vol';
  return v + ' vol';
}

function _esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
