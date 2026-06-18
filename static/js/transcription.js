/**
 * transcription.js — Discord Alerts panel component
 *
 * Renders three columns: regular alerts, squeeze alerts, and a ranked sentiment
 * list. Sentiment tickers come only from human chat + Market Update scanner
 * tables; clicking one fires a burst alert.
 */

import { subscribe } from './store.js?v=59';
import { api }       from './api.js?v=59';

let _regularBox    = null;
let _squeezeBox    = null;
let _sentimentBox  = null;

let _lastRegularCount  = -1;
let _lastRegularKey    = '';
let _lastSqueezeCount  = -1;
let _lastSqueezeKey    = '';
let _lastSentKey       = '';

export function init(panelEl) {
  _regularBox   = panelEl.querySelector('[data-regular-box]');
  _squeezeBox   = panelEl.querySelector('[data-squeeze-box]');
  _sentimentBox = panelEl.querySelector('[data-sentiment-box]');

  subscribe('discord', d => {
    const all     = d.alerts ?? [];
    const regular = all.filter(a => !a.burst);
    const squeeze = all.filter(a =>  a.burst);
    _renderFeed(_regularBox, regular, 'regular');
    _renderFeed(_squeezeBox, squeeze, 'squeeze');
    _renderSentimentRanked(d.sentiment_ranked ?? []);
  });
}

// ── Ranked sentiment column ────────────────────────────────
// A short list of tickers ordered strongest-bullish first. Click a row to fire
// a burst alert for that ticker (same seam as clicking a watchlist ticker).

function _renderSentimentRanked(ranked) {
  if (!_sentimentBox) return;

  if (!ranked.length) {
    if (_lastSentKey !== '∅') {
      _sentimentBox.innerHTML = '<span class="tx-placeholder">No signals…</span>';
      _lastSentKey = '∅';
    }
    return;
  }

  // Cheap change-detect: ticker+score signature so we only re-render on change.
  const key = ranked.map(r => `${r.ticker}:${r.score}`).join('|');
  if (key === _lastSentKey) return;
  _lastSentKey = key;

  _sentimentBox.innerHTML = ranked.map(_renderSentRow).join('');
  _sentimentBox.querySelectorAll('[data-sent-ticker]').forEach(el => {
    el.addEventListener('click', () => _fireBurst(el, el.dataset.sentTicker));
  });
}

function _renderSentRow(r) {
  const score    = r.score ?? 0;
  const dir      = score >= 0.05 ? 'bull' : score <= -0.05 ? 'bear' : 'flat';
  const scoreTxt = `${score >= 0 ? '+' : ''}${score.toFixed(2)}`;
  const tkr      = `<strong class="tx-sent-ticker">${_esc(r.ticker)}</strong>`;
  const scoreSp  = `<span class="tx-sent-score tx-sent-score--${dir}">${scoreTxt}</span>`;
  return `<div class="tx-line tx-sent-row" data-sent-ticker="${_esc(r.ticker)}" `
       + `title="Click to fire a burst alert for ${_esc(r.ticker)}">${tkr}${scoreSp}</div>`;
}

async function _fireBurst(el, ticker) {
  el.classList.add('tx-sent-row--firing');
  try {
    await api.burstAlert(ticker);
  } catch (err) {
    console.error('[sentiment] burst alert failed', err);
  } finally {
    setTimeout(() => el.classList.remove('tx-sent-row--firing'), 800);
  }
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
