/**
 * transcription.js — Discord Alerts panel component
 *
 * Renders three columns: regular alerts, squeeze alerts, and a swing-candidate
 * list. Swing candidates come from the screener (swing_candidates.json — Alpaca
 * technicals + Finnhub fundamentals); clicking one adds the ticker to the watchlist.
 */

import { subscribe } from './store.js?v=64';
import { api }       from './api.js?v=64';

let _regularBox    = null;
let _squeezeBox    = null;
let _swingBox      = null;
let _checkForm     = null;
let _checkInput    = null;
let _checkResults  = null;

let _lastRegularCount  = -1;
let _lastRegularKey    = '';
let _lastSqueezeCount  = -1;
let _lastSqueezeKey    = '';
let _lastSwingKey      = '';

export function init(panelEl) {
  _regularBox   = panelEl.querySelector('[data-regular-box]');
  _squeezeBox   = panelEl.querySelector('[data-squeeze-box]');
  _swingBox     = panelEl.querySelector('[data-swing-box]');

  subscribe('discord', d => {
    const all     = d.alerts ?? [];
    const regular = all.filter(a => !a.burst);
    const squeeze = all.filter(a =>  a.burst);
    _renderFeed(_regularBox, regular, 'regular');
    _renderFeed(_squeezeBox, squeeze, 'squeeze');
  });

  // Swing candidates arrive as their own top-level state slice.
  subscribe('swing', rows => _renderSwing(rows ?? []));

  // Manual refresh button — triggers an on-demand screen; fresh candidates arrive
  // on the next /api/state poll.
  const refreshBtn = panelEl.querySelector('[data-swing-refresh]');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.classList.add('swing-refresh-btn--spin');
      refreshBtn.disabled = true;
      try {
        await api.refreshSwing();
      } catch (err) {
        console.error('[swing] refresh failed', err);
      } finally {
        // Screen takes a few seconds; keep the spinner briefly, then re-enable.
        setTimeout(() => {
          refreshBtn.classList.remove('swing-refresh-btn--spin');
          refreshBtn.disabled = false;
        }, 4000);
      }
    });
  }

  // Ad-hoc symbol checker — test any tickers against the swing filters on demand.
  _checkForm    = panelEl.querySelector('[data-swing-check-form]');
  _checkInput   = panelEl.querySelector('[data-swing-check-input]');
  _checkResults = panelEl.querySelector('[data-swing-check-results]');
  if (_checkForm) {
    _checkForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const symbols = (_checkInput.value || '').trim();
      if (!symbols) return;
      _checkForm.classList.add('swing-check--busy');
      _renderCheckMessage('Checking…');
      try {
        const { results = [] } = await api.checkSwing(symbols);
        _renderCheckResults(results);
      } catch (err) {
        console.error('[swing] check failed', err);
        _renderCheckMessage('Check failed — try again.');
      } finally {
        _checkForm.classList.remove('swing-check--busy');
      }
    });
  }
}

// ── Ad-hoc symbol checker ──────────────────────────────────

function _renderCheckMessage(msg) {
  if (!_checkResults) return;
  _checkResults.hidden = false;
  _checkResults.innerHTML = `<div class="swing-check-msg">${_esc(msg)}</div>`;
}

function _renderCheckResults(results) {
  if (!_checkResults) return;
  if (!results.length) { _checkResults.hidden = true; return; }
  _checkResults.hidden = false;
  _checkResults.innerHTML =
      `<button class="swing-check-close" data-swing-check-close title="Dismiss">×</button>`
    + results.map(_renderCheckCard).join('');
  const close = _checkResults.querySelector('[data-swing-check-close]');
  if (close) close.addEventListener('click', () => { _checkResults.hidden = true; });
}

function _renderCheckCard(r) {
  if (r.error) {
    return `<div class="swing-check-card swing-check-card--err">`
         + `<div class="swing-check-head"><strong>${_esc(r.ticker)}</strong>`
         + `<span class="swing-check-verdict swing-check-verdict--err">${_esc(r.error)}</span></div></div>`;
  }
  const verdict = r.ok
    ? `<span class="swing-check-verdict swing-check-verdict--pass">✓ Qualifies${r.score != null ? ` · ${r.score}` : ''}</span>`
    : `<span class="swing-check-verdict swing-check-verdict--fail">✗ No</span>`;
  const price = r.price != null ? `<span class="swing-check-price">$${_esc(String(r.price))}</span>` : '';

  const rows = (r.checks || []).map(ch => {
    const icon = ch.kind === 'info' ? '·' : (ch.pass ? '✓' : '✗');
    const cls  = ch.kind === 'info' ? 'swing-check-row--info'
                                    : (ch.pass ? 'swing-check-row--pass' : 'swing-check-row--fail');
    return `<div class="swing-check-row ${cls}">`
         + `<span class="swing-check-icon">${icon}</span>`
         + `<span class="swing-check-name">${_esc(ch.name)}</span>`
         + `<span class="swing-check-detail">${_esc(ch.detail)}</span></div>`;
  }).join('');

  return `<div class="swing-check-card">`
       + `<div class="swing-check-head"><strong>${_esc(r.ticker)}</strong>${price}${verdict}</div>`
       + `<div class="swing-check-rows">${rows}</div></div>`;
}

// ── Swing-candidate column ─────────────────────────────────
// Screener output, ranked strongest-first. Each row shows the confirmation signals
// (RSI, rating, EPS growth, R:R) plus tags for reversal / upgrade / earnings / ⚡
// confluence. Click a row to add the ticker to the watchlist.

function _renderSwing(rows) {
  if (!_swingBox) return;

  if (!rows.length) {
    if (_lastSwingKey !== '∅') {
      _swingBox.innerHTML = '<span class="tx-placeholder">No candidates…</span>';
      _lastSwingKey = '∅';
    }
    return;
  }

  // Cheap change-detect: ticker+score signature so we only re-render on change.
  const key = rows.map(r => `${r.ticker}:${r.score}`).join('|');
  if (key === _lastSwingKey) return;
  _lastSwingKey = key;

  _swingBox.innerHTML = rows.map(_renderSwingRow).join('');
  _swingBox.querySelectorAll('[data-swing-ticker]').forEach(el => {
    el.addEventListener('click', () => _addSwing(el, el.dataset.swingTicker));
  });
}

function _renderSwingRow(r) {
  const skipped  = new Set(r.skipped || []);
  const tierCls  = r.signal_tier === 'full' ? ' tx-swing-ticker--full'
                 : r.signal_tier === 'partial' ? ' tx-swing-ticker--partial' : '';
  const tkr   = `<strong class="tx-swing-ticker${tierCls}">${_esc(r.ticker)}</strong>`;
  const price = r.price != null ? `<span class="tx-swing-price">$${r.price}</span>` : '';

  const chips = [];
  if (r.rsi != null)        chips.push(_chip('RSI', r.rsi, false));
  if (r.rel_vol != null)    chips.push(_chip('RVOL', `${r.rel_vol}×`, false, r.rel_vol_tier));
  if (r.rating)             chips.push(_chip('★', r.rating, skipped.has('rating')));
  if (r.eps_growth != null) chips.push(_chip('EPS', `${r.eps_growth}×`, skipped.has('metric')));
  if (r.rr != null)         chips.push(_chip('R:R', r.rr, skipped.has('metric')));

  const tags = [];
  if (r.reversal_confirmed)  tags.push('<span class="tx-swing-tag tx-swing-tag--rev" title="Oversold and turning up">🔄 turning</span>');
  if (r.recent_upgrade)      tags.push('<span class="tx-swing-tag tx-swing-tag--up" title="Analyst rating improving month-over-month">▲ improving</span>');
  if (r.confluence?.count)   tags.push(`<span class="tx-swing-tag tx-swing-tag--conf" title="Also live in Discord: ${_esc((r.confluence.sources||[]).join(', '))}">⚡ ${r.confluence.count}</span>`);
  if (r.earnings_in_window)  tags.push('<span class="tx-swing-tag tx-swing-tag--earn" title="Earnings inside the swing window — risk">⚠ earnings</span>');

  return `<div class="tx-line tx-swing-row" data-swing-ticker="${_esc(r.ticker)}" `
       + `title="Click to add ${_esc(r.ticker)} to the watchlist">`
       + `<div class="tx-swing-head">${tkr}${price}</div>`
       + `<div class="tx-swing-chips">${chips.join('')}</div>`
       + (tags.length ? `<div class="tx-swing-tags">${tags.join('')}</div>` : '')
       + `</div>`;
}

function _chip(label, val, degraded, tier) {
  let cls = 'tx-swing-chip';
  if (degraded)          cls += ' tx-swing-chip--degraded';
  if (tier === 'high')   cls += ' tx-swing-chip--high';
  else if (tier === 'low') cls += ' tx-swing-chip--low';
  return `<span class="${cls}"><em>${_esc(label)}</em> ${_esc(String(val))}</span>`;
}

async function _addSwing(el, ticker) {
  el.classList.add('tx-swing-row--firing');
  try {
    await api.addTicker(ticker);
  } catch (err) {
    console.error('[swing] add to watchlist failed', err);
  } finally {
    setTimeout(() => el.classList.remove('tx-swing-row--firing'), 800);
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
