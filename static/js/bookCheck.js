// Book check (operator 11/19): read-only view of tools/book_coverage_watch.py.
// Pillar 1 - is every seated name a leader with a catalyst, and which leaders are missing (and why).
// Pillar 2 - opens in the last 10 / 60 minutes and today (goal 30-40 a day, stretch 40-50), and how each recent open was timed.
import { getToken, isAuthenticated, getBackendUrl, getQueryUser } from './auth.js?v=134';

const POLL_MS = 30_000;
const GOAL_DAY = 30;

function url(path) {
  const user = getQueryUser();
  return getBackendUrl() + path + (user ? `?user=${encodeURIComponent(user)}` : '');
}

async function load() {
  const token = isAuthenticated() ? getToken() : '';
  const res = await fetch(url('/api/book-quality'), {
    credentials: 'include', headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const chip = (txt, cls = '') => `<span class="bq-chip ${cls}">${esc(txt)}</span>`;

function renderOpens(o) {
  if (!o) return '';
  const ok10 = o.last_10m >= 1;
  const pace = o.per_10m_today ?? 0;
  const proj = Math.round(pace * 39);
  return `<div class="bq-opens">
    <div class="bq-stat ${ok10 ? 'bq-good' : 'bq-bad'}" title="Standard: at least 1 open every 10 minutes">
      <span class="bq-num">${o.last_10m}</span><span class="bq-lbl">opens, last 10 min</span></div>
    <div class="bq-stat"><span class="bq-num">${o.last_60m}</span><span class="bq-lbl">last hour</span></div>
    <div class="bq-stat ${o.today >= GOAL_DAY ? 'bq-good' : ''}" title="Goal 30-40 a day (stretch 40-50); pace projects the day at the current rate">
      <span class="bq-num">${o.today}</span><span class="bq-lbl">today · pace ≈${proj}/day</span></div>
    <div class="bq-stat"><span class="bq-num">${o.open_now}</span><span class="bq-lbl">open now</span></div>
    <div class="bq-stat" title="Share of the session so far with at least 1 / at least 2 positions open">
      <span class="bq-num">${o.share_ge1_pct}%</span><span class="bq-lbl">time ≥1 open · ${o.share_ge2_pct}% ≥2</span></div>
  </div>`;
}

function renderBook(d) {
  const rows = (d.book || []).map(b => {
    const st = b.status === 'leader' ? chip(`#${b.rank} leader`, 'bq-good')
      : b.status === 'mover' ? chip(`#${b.rank} mover`, 'bq-warn') : chip('not a mover', 'bq-bad');
    const cats = (b.catalysts || []).length ? b.catalysts.map(c => chip(c, 'bq-cat')).join('') : chip('no catalyst', 'bq-bad');
    const pct = b.pct != null ? `${b.pct >= 0 ? '+' : ''}${b.pct}%` : '';
    return `<div class="bq-row ${b.good ? '' : 'bq-row--flag'}"><span class="bq-sym">${esc(b.symbol)}</span>
      <span class="bq-pct">${esc(pct)}</span>${st}${cats}</div>`;
  }).join('');
  const head = d.book_n ? `${d.book_good} of ${d.book_n} seated names are leaders with a catalyst (${d.purity_pct}%)` : 'book empty';
  return `<div class="bq-sec"><div class="bq-h">On the book · ${esc(head)}</div>${rows}</div>`;
}

function renderMissing(c) {
  if (!c || !c.leaders) return '';
  const rows = (c.missing_detail || []).slice(0, 12).map(m => {
    const why = Object.keys(m.top_reasons || {})[0] || m.status;
    const cats = (m.catalysts || []).map(x => chip(x, 'bq-cat')).join('');
    return `<div class="bq-row"><span class="bq-sym">${esc(m.symbol)}</span><span class="bq-pct">+${esc(m.pct)}%</span>${chip(why, 'bq-bad')}${cats}</div>`;
  }).join('');
  return `<div class="bq-sec"><div class="bq-h">Leaders not on the book · ${c.seated}/${c.leaders} covered (${c.coverage_pct}%)</div>${rows}</div>`;
}

function renderOpensList(list) {
  if (!list || !list.length) return '';
  const rows = list.map(o => {
    const g = o.green_3m === undefined ? chip('3-min check pending')
      : o.green_3m ? chip(`green in 3m (+${o.best_3m_bp} bp)`, 'bq-good') : chip('never green in 3m', 'bq-bad');
    const res = o.result_bp != null ? chip(`${o.result_bp >= 0 ? '+' : ''}${o.result_bp} bp in ${o.held_min}m`, o.result_bp >= 0 ? 'bq-good' : 'bq-bad') : chip('open');
    const rp = o.range_pos != null ? chip(`range ${o.range_pos}%`, o.range_pos >= 90 ? 'bq-warn' : '') : '';
    const low = o.min_since_low != null ? chip(`${o.min_since_low}m after the low`) : '';
    const wr = o.wr_fast_raw != null ? chip(`%R ${o.wr_fast_raw}`) : '';
    return `<div class="bq-row"><span class="bq-sym">${esc(o.symbol)}</span><span class="bq-pct">${esc(o.time)}</span>${rp}${low}${wr}${g}${res}</div>`;
  }).join('');
  return `<div class="bq-sec"><div class="bq-h">Recent opens · timing</div>${rows}</div>`;
}

function render(el, d) {
  if (!d.ok) { el.innerHTML = `<div class="bq-msg">${esc(d.error || 'no data')}</div>`; return; }
  const stale = d.age_sec > 300 ? ` <span class="bq-warn-txt">(scan ${Math.round(d.age_sec / 60)} min old)</span>` : '';
  el.innerHTML = `<div class="bq-top"><span class="bq-title">Book check</span><span class="bq-ts">${esc((d.ts || '').slice(11, 16))} ET${stale}</span></div>
    ${renderOpens(d.opens)}${renderBook(d)}${renderMissing(d.coverage)}${renderOpensList(d.recent_opens)}`;
}

export function init() {
  const el = document.querySelector('[data-book-check]');
  if (!el) return;
  const tick = async () => {
    try { render(el, await load()); } catch (e) { el.innerHTML = `<div class="bq-msg">Book check unavailable (${esc(e.message)})</div>`; }
  };
  tick();
  setInterval(tick, POLL_MS);
}
