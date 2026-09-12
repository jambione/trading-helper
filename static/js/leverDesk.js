/**
 * leverDesk.js — Operator keep/kill strip for the active product lever.
 *
 * Load /api/lever-desk, switch/edit the topic, score it, record a verdict,
 * classify new ideas. Never writes bot_config / trading knobs.
 */

import { api } from './api.js?v=182';

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

function _topicEditor(data) {
  const lever = data.lever || {};
  const rules = lever.score_rules || {};
  const pas = rules.pass || {};
  const kill = rules.kill || {};
  const levers = data.levers || [];
  const active = data.active_id || lever.id || '';
  const opts = levers.map(L =>
    `<option value="${_esc(L.id)}" ${L.id === active ? 'selected' : ''}>${_esc(L.title || L.id)} (${_esc(L.status)})</option>`
  ).join('');
  const knobsJson = JSON.stringify(lever.knobs || {}, null, 2);
  const src = data.registry_source || 'config';
  return `
    <div class="ld-topic">
      <div class="ld-record-label">Topic (what we are scoring)</div>
      <div class="ld-topic-row">
        <select data-ld-pick class="ld-select">${opts || '<option value="">(none yet)</option>'}</select>
        <button type="button" class="btn btn--ghost btn--sm" data-ld-activate>Activate</button>
        <button type="button" class="btn btn--ghost btn--sm" data-ld-new-topic>New topic</button>
      </div>
      <div class="ld-note">Registry: ${_esc(src)} · edits save on the mini under ai_reports/lever_desk/ — not bot_config</div>
      <div class="ld-form">
        <label class="ld-field"><span>Id</span>
          <input data-ld-f-id class="ld-input" value="${_esc(lever.id || '')}" placeholder="green_catchup_trail" /></label>
        <label class="ld-field"><span>Title</span>
          <input data-ld-f-title class="ld-input" value="${_esc(lever.title || '')}" /></label>
        <label class="ld-field ld-field--wide"><span>Hypothesis</span>
          <textarea data-ld-f-hypothesis class="ld-input" rows="2">${_esc(lever.hypothesis || '')}</textarea></label>
        <label class="ld-field"><span>Shipped (ET day)</span>
          <input data-ld-f-shipped class="ld-input" value="${_esc(lever.shipped_at || '')}" placeholder="YYYY-MM-DD" /></label>
        <label class="ld-field"><span>Score kind</span>
          <select data-ld-f-kind class="ld-select">
            <option value="hold_capture" ${rules.kind !== 'session_r' ? 'selected' : ''}>hold_capture</option>
            <option value="session_r" ${rules.kind === 'session_r' ? 'selected' : ''}>session_r</option>
          </select></label>
        <label class="ld-field"><span>Min MFE R</span>
          <input data-ld-f-min-mfe class="ld-input" type="number" step="0.05" value="${_esc(rules.min_mfe_r ?? 0.25)}" /></label>
        <label class="ld-field"><span>Min n</span>
          <input data-ld-f-min-n class="ld-input" type="number" step="1" value="${_esc(rules.min_n ?? 5)}" /></label>
        <label class="ld-field"><span>Pass capture ≥</span>
          <input data-ld-f-pass class="ld-input" type="number" step="0.05" value="${_esc(pas.median_capture_gte ?? 0.40)}" /></label>
        <label class="ld-field"><span>Kill capture &lt;</span>
          <input data-ld-f-kill class="ld-input" type="number" step="0.05" value="${_esc(kill.median_capture_lt ?? 0.15)}" /></label>
        <label class="ld-field ld-field--wide"><span>Knobs (JSON, docs only)</span>
          <textarea data-ld-f-knobs class="ld-input" rows="3">${_esc(knobsJson)}</textarea></label>
      </div>
      <div class="ld-topic-row">
        <button type="button" class="btn btn--sm btn--primary" data-ld-save-topic>Save &amp; score this topic</button>
        <span data-ld-topic-msg class="ld-note"></span>
      </div>
    </div>`;
}

function _readTopicForm() {
  const knobsRaw = $body.querySelector('[data-ld-f-knobs]')?.value || '{}';
  let knobs;
  try {
    knobs = JSON.parse(knobsRaw);
  } catch (e) {
    throw new Error(`Knobs JSON: ${e.message}`);
  }
  return {
    id: $body.querySelector('[data-ld-f-id]')?.value || '',
    title: $body.querySelector('[data-ld-f-title]')?.value || '',
    hypothesis: $body.querySelector('[data-ld-f-hypothesis]')?.value || '',
    shipped_at: $body.querySelector('[data-ld-f-shipped]')?.value || '',
    status: 'live',
    knobs,
    score_kind: $body.querySelector('[data-ld-f-kind]')?.value || 'hold_capture',
    min_mfe_r: $body.querySelector('[data-ld-f-min-mfe]')?.value,
    min_n: $body.querySelector('[data-ld-f-min-n]')?.value,
    pass_median_capture_gte: $body.querySelector('[data-ld-f-pass]')?.value,
    kill_median_capture_lt: $body.querySelector('[data-ld-f-kill]')?.value,
    make_active: true,
  };
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
  html += _topicEditor(data);
  html += `<div class="ld-note">Corpus: ${corpus.outcomes_lines ?? '—'} outcomes · ${corpus.events_lines ?? '—'} events</div>`;
  html += `<p class="ld-hypothesis">${_esc(lever.hypothesis || '')}</p>`;
  html += _knobChips(lever.knobs);
  html += `<div class="ld-verdict-line"><strong>${_esc(score.verdict || '—')}</strong>
    — ${_esc(score.reason || '')}</div>`;
  html += _scoreRows(score);

  if (latest) {
    html += `<div class="ld-note">Latest session ${_esc(latest.session)}
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
  $body.querySelector('[data-ld-activate]')?.addEventListener('click', async () => {
    const id = $body.querySelector('[data-ld-pick]')?.value;
    const msg = $body.querySelector('[data-ld-topic-msg]');
    if (!id) return;
    try {
      await api.activateLever(id);
      if (msg) msg.textContent = `Activated ${id}`;
      await _load(true);
    } catch (e) {
      if (msg) msg.textContent = e.message;
      else alert(e.message);
    }
  });

  $body.querySelector('[data-ld-new-topic]')?.addEventListener('click', () => {
    const idEl = $body.querySelector('[data-ld-f-id]');
    const titleEl = $body.querySelector('[data-ld-f-title]');
    const hypEl = $body.querySelector('[data-ld-f-hypothesis]');
    const shipEl = $body.querySelector('[data-ld-f-shipped]');
    const knobsEl = $body.querySelector('[data-ld-f-knobs]');
    if (idEl) idEl.value = '';
    if (titleEl) titleEl.value = '';
    if (hypEl) hypEl.value = '';
    if (shipEl) shipEl.value = new Date().toISOString().slice(0, 10);
    if (knobsEl) knobsEl.value = '{\n  \n}';
    titleEl?.focus();
  });

  $body.querySelector('[data-ld-save-topic]')?.addEventListener('click', async () => {
    const msg = $body.querySelector('[data-ld-topic-msg]');
    try {
      const body = _readTopicForm();
      if (!body.title && !body.id) throw new Error('Title or id required');
      const res = await api.saveLever(body);
      if (msg) msg.textContent = res.created ? `Created ${res.lever?.id}` : `Updated ${res.lever?.id}`;
      await _load(true);
    } catch (e) {
      if (msg) msg.textContent = e.message;
      else alert(e.message);
    }
  });

  $body.querySelector('[data-ld-pick]')?.addEventListener('change', (ev) => {
    const id = ev.target.value;
    const L = (_data?.levers || []).find(x => x.id === id);
    // Prefill id/title from summary; full fields reload after Activate.
    if (L) {
      const idEl = $body.querySelector('[data-ld-f-id]');
      const titleEl = $body.querySelector('[data-ld-f-title]');
      if (idEl) idEl.value = L.id || '';
      if (titleEl) titleEl.value = L.title || '';
    }
  });

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
