/**
 * leverDesk.js — Operator keep/kill strip for the active product lever.
 *
 * One job: load /api/lever-desk on demand, show the scorecard, record a
 * verdict, and classify new ideas. Never writes trading knobs.
 */

import { api } from './api.js?v=134';

let $root, $verdict, $title, $meta, $body, $toggle;
let _open = false;
let _data = null;
let _loading = false;

function _esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _fmt(n, digits = 3) {
  if (n == null || Number.isNaN(n)) return '—';
  const v = Number(n);
  const sign = v > 0 ? '+' : '';
  return `${sign}${v.toFixed(digits)}`;
}

function _pct(n) {
  if (n == null || Number.isNaN(n)) return '—';
  return `${(Number(n) * 100).toFixed(0)}%`;
}

function _setChip(verdict) {
  if (!$verdict) return;
  const v = String(verdict || '—').toUpperCase();
  $verdict.textContent = v;
  $verdict.className = 'ld-chip';
  if (v === 'KEEP') $verdict.classList.add('ld-chip--keep');
  else if (v === 'KILL') $verdict.classList.add('ld-chip--kill');
  else if (v === 'MEASURE') $verdict.classList.add('ld-chip--measure');
}

function _headerFrom(data) {
  const lever = data?.lever || {};
  const score = lever.score || {};
  const cur = score.current || {};
  const latest = data?.latest || {};
  _setChip(score.verdict || '—');
  if ($title) $title.textContent = lever.title || data?.active_id || 'No active lever';
  if ($meta) {
    const session = latest.session || (score.window_days || []).slice(-1)[0] || '—';
    const n = cur.n != null ? cur.n : (latest.n != null ? latest.n : '—');
    $meta.textContent = `${session} · n=${n}`;
  }
}

function _scoreRows(score) {
  const cur = score?.current || {};
  const base = score?.baseline || null;
  const kind = score?.kind || '';
  if (kind === 'hold_capture') {
    const row = (label, key, fmt) => {
      const c = fmt(cur[key]);
      const b = base ? fmt(base[key]) : '—';
      return `<tr><td>${_esc(label)}</td><td>${c}</td><td>${b}</td></tr>`;
    };
    return `
      <table class="ld-table">
        <thead><tr><th></th><th>Current</th><th>Baseline</th></tr></thead>
        <tbody>
          ${row('n', 'n', v => v == null ? '—' : String(v))}
          ${row('n qualifying', 'n_qualifying', v => v == null ? '—' : String(v))}
          ${row('max MFE R', 'max_mfe_r', v => _fmt(v, 3))}
          ${row('median capture', 'median_capture', _pct)}
          ${row('median MFE R', 'median_mfe_r', v => _fmt(v, 3))}
          ${row('median realized R', 'median_realized_r', v => _fmt(v, 3))}
          ${row('give &lt; MFE (trail)', 'give_lt_mfe_frac', _pct)}
        </tbody>
      </table>`;
  }
  if (kind === 'session_r') {
    return `
      <table class="ld-table">
        <thead><tr><th></th><th>Current</th><th>Baseline</th></tr></thead>
        <tbody>
          <tr><td>n</td><td>${cur.n ?? '—'}</td><td>${base?.n ?? '—'}</td></tr>
          <tr><td>sessions</td><td>${cur.n_sessions ?? '—'}</td><td>${base?.n_sessions ?? '—'}</td></tr>
          <tr><td>median live R</td><td>${_fmt(cur.median_live_r)}</td><td>${_fmt(base?.median_live_r)}</td></tr>
        </tbody>
      </table>`;
  }
  return `<div class="ld-note">No score table for kind ${_esc(kind || '—')}</div>`;
}

function _knobChips(knobs) {
  const entries = Object.entries(knobs || {});
  if (!entries.length) return '';
  return `<div class="ld-knobs">${entries.map(([k, v]) =>
    `<span class="ld-knob">${_esc(k)}=${_esc(v)}</span>`).join('')}</div>`;
}

function _goLiveDetails(go) {
  if (!go) return '';
  const bar = go.bar || {};
  const mfe = go.median_mfe_less_spread;
  const mfeS = mfe == null ? '—' : _fmt(mfe);
  return `
    <details class="ld-details">
      <summary>Go-live (context)</summary>
      <ul class="ld-list">
        <li>[${go.sessions_ok ? 'x' : ' '}] ${go.wins}/${go.n_sessions} sessions live-positive
          (need ${bar.win_sessions_need} of ${bar.sessions_need})</li>
        <li>[${go.trades_ok ? 'x' : ' '}] ${go.trades} trades (need ${bar.trades_need})</li>
        <li>[${go.mfe_ok ? 'x' : ' '}] median MFE−spread ${mfeS} (need &gt; 0)</li>
        <li>pass=${go.pass} — capital gate, not approval</li>
      </ul>
    </details>`;
}

function _doctrineDetails(doctrine) {
  const keep = doctrine?.keep || [];
  const kill = doctrine?.kill || [];
  if (!keep.length && !kill.length) return '';
  return `
    <details class="ld-details">
      <summary>Doctrine KEEP / KILL (not today’s lever)</summary>
      <div class="ld-doctrine">
        <div><strong>KEEP</strong>
          <ul class="ld-list">${keep.map(k =>
            `<li>${_esc(k.item)} <span class="ld-anchor">${_esc(k.anchor || '')}</span></li>`).join('')}</ul>
        </div>
        <div><strong>KILL</strong>
          <ul class="ld-list">${kill.map(k =>
            `<li>${_esc(k.item)} <span class="ld-anchor">${_esc(k.anchor || '')}</span></li>`).join('')}</ul>
        </div>
      </div>
    </details>`;
}

function _renderBody(data) {
  if (!$body) return;
  if (!data) {
    $body.innerHTML = '<div class="ld-note">No data yet.</div>';
    return;
  }
  const lever = data.lever || {};
  const score = lever.score || {};
  const latest = data.latest;
  const gap = data.gap;
  const corpus = data.corpus || {};

  let html = '';
  if (gap) {
    html += `<div class="ld-banner ld-banner--warn">${_esc(gap)}</div>`;
  }
  html += `<div class="ld-note">Corpus: ${corpus.outcomes_lines ?? '—'} outcomes · ${corpus.events_lines ?? '—'} events</div>`;
  html += `<p class="ld-hypothesis">${_esc(lever.hypothesis || '')}</p>`;
  html += _knobChips(lever.knobs);
  html += `<div class="ld-verdict-line"><strong>${_esc(score.verdict || '—')}</strong>
    — ${_esc(score.reason || '')}</div>`;
  html += _scoreRows(score);

  if (latest) {
    html += `<div class="ld-note">Latest session ${ _esc(latest.session) }
      (context, not the verdict): n=${latest.n}
      paper ${_fmt(latest.paper_r)} R · live ${_fmt(latest.live_r)} R</div>`;
  }
  html += _goLiveDetails(data.go_live);

  html += `
    <div class="ld-record">
      <div class="ld-record-label">Record verdict (does not write bot_config)</div>
      <input type="text" data-ld-note class="ld-note-input" placeholder="optional note" />
      <div class="ld-record-btns">
        <button type="button" class="btn btn--sm ld-btn--keep" data-ld-verdict-btn="KEEP">KEEP</button>
        <button type="button" class="btn btn--sm ld-btn--kill" data-ld-verdict-btn="KILL">KILL</button>
        <button type="button" class="btn btn--sm" data-ld-verdict-btn="MEASURE">MEASURE</button>
      </div>
      <div data-ld-next class="ld-note"></div>
    </div>`;

  html += `
    <div class="ld-classify">
      <div class="ld-record-label">Classify a <em>new</em> idea — not today’s lever</div>
      <textarea data-ld-classify-text class="ld-classify-input" rows="2"
        placeholder="e.g. widen the give to cut stomps"></textarea>
      <button type="button" class="btn btn--ghost btn--sm" data-ld-classify-btn>Classify</button>
      <div data-ld-classify-out class="ld-note"></div>
    </div>`;

  html += _doctrineDetails(data.doctrine);
  $body.innerHTML = html;
  _wireBody();
}

function _wireBody() {
  $body.querySelectorAll('[data-ld-verdict-btn]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const verdict = btn.getAttribute('data-ld-verdict-btn');
      const note = $body.querySelector('[data-ld-note]')?.value || '';
      if (!confirm(`Record ${verdict} for the active lever? This does not change bot_config.`)) return;
      try {
        const res = await api.recordLeverVerdict(verdict, note);
        const next = $body.querySelector('[data-ld-next]');
        if (next) {
          const killHint = _data?.lever?.next_on_kill || '';
          next.textContent = verdict === 'KILL' && killHint
            ? `Recorded. Next: ${killHint}`
            : `Recorded ${verdict}. active_id=${res.active_id ?? 'null'}`;
        }
        await _load(true);
      } catch (e) {
        alert(`Could not record: ${e.message}`);
      }
    });
  });

  $body.querySelector('[data-ld-classify-btn]')?.addEventListener('click', async () => {
    const text = $body.querySelector('[data-ld-classify-text]')?.value || '';
    const out = $body.querySelector('[data-ld-classify-out]');
    if (!out) return;
    out.textContent = '…';
    try {
      const res = await api.classifyLeverIdea(text);
      const fams = (res.families || [])
        .map(f => `${f.id}: ${f.label} (${f.anchor || ''})`)
        .join('; ');
      out.innerHTML = `<strong>${_esc(res.verdict)}</strong> — ${_esc(res.reason || '')}`
        + (fams ? `<div class="ld-anchor">${_esc(fams)}</div>` : '');
    } catch (e) {
      out.textContent = `Classify failed: ${e.message}`;
    }
  });
}

async function _load(refresh = false) {
  if (_loading || !$root) return;
  _loading = true;
  try {
    const data = await api.getLeverDesk(10, refresh);
    _data = data;
    _headerFrom(data);
    if (_open) _renderBody(data);
  } catch (e) {
    if ($meta) $meta.textContent = `load failed: ${e.message}`;
    if (_open && $body) {
      $body.innerHTML = `<div class="ld-note ld-note--err">Could not load Lever Desk: ${_esc(e.message)}</div>`;
    }
  } finally {
    _loading = false;
  }
}

function _toggle() {
  _open = !_open;
  if (!$body || !$toggle) return;
  $body.classList.toggle('hidden', !_open);
  $toggle.textContent = _open ? 'Hide' : 'Score';
  if (_open) {
    if (_data) _renderBody(_data);
    else _load(false);
  }
}

export function init(rootEl) {
  if (!rootEl) return;
  $root = rootEl;
  $verdict = rootEl.querySelector('[data-ld-verdict]');
  $title = rootEl.querySelector('[data-ld-title]');
  $meta = rootEl.querySelector('[data-ld-meta]');
  $body = rootEl.querySelector('[data-ld-body]');
  $toggle = rootEl.querySelector('[data-ld-toggle]');

  rootEl.querySelector('[data-ld-refresh]')?.addEventListener('click', () => _load(true));
  $toggle?.addEventListener('click', _toggle);

  // Header chips on first paint; body stays collapsed until Score.
  _load(false);
}
