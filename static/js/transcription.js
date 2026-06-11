/**
 * transcription.js — Discord Alerts panel component
 *
 * Renders two columns: regular alerts (left) and squeeze alerts (right).
 * Regular alerts show alert type, price, and volume.
 * Squeeze alerts show the ticker and price-level chips.
 */

import { subscribe } from './store.js?v=49';

let _regularBox = null;
let _squeezeBox = null;
let _known = new Set();

let _lastRegularCount = -1;
let _lastRegularKey   = '';
let _lastSqueezeCount = -1;
let _lastSqueezeKey   = '';

export function init(panelEl) {
  _regularBox = panelEl.querySelector('[data-regular-box]');
  _squeezeBox = panelEl.querySelector('[data-squeeze-box]');

  subscribe('tickers', rows => {
    _known = new Set(rows.map(r => r.ticker));
  });

  subscribe('discord', d => {
    const all     = d.alerts ?? [];
    const regular = all.filter(a => !a.burst);
    const squeeze = all.filter(a =>  a.burst);
    _renderFeed(_regularBox, regular, false);
    _renderFeed(_squeezeBox, squeeze, true);
  });
}

// ── Rendering ──────────────────────────────────────────────────

function _renderFeed(box, alerts, isSqueeze) {
  if (!box) return;

  if (!alerts.length) {
    const placeholder = isSqueeze ? 'No squeezes…' : 'Waiting…';
    const lastCount   = isSqueeze ? _lastSqueezeCount : _lastRegularCount;
    if (lastCount !== 0) {
      box.innerHTML = `<span class="tx-placeholder">${placeholder}</span>`;
      if (isSqueeze) { _lastSqueezeCount = 0; _lastSqueezeKey = ''; }
      else           { _lastRegularCount = 0; _lastRegularKey = ''; }
    }
    return;
  }

  const tail    = alerts[alerts.length - 1];
  const tailKey = `${tail.ts}|${tail.ticker}`;
  const lastCount = isSqueeze ? _lastSqueezeCount : _lastRegularCount;
  const lastKey   = isSqueeze ? _lastSqueezeKey   : _lastRegularKey;

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
