/**
 * store.js — Reactive centralized state
 *
 * Single responsibility: hold and broadcast application state.
 * No business logic. Modules subscribe to specific slices.
 */

const _state = {
  connected:      false,
  scan_running:   false,
  scan_ts:        '',
  tickers:        /** @type {Object[]} */ ([]),
  funnel:         /** @type {Object} */ ({}),
  swing:          /** @type {Object[]} */ ([]),
  price_spikes:   /** @type {Object[]} */ ([]),
  transcriber: {
    running: false,
    lines:   /** @type {string[]} */ ([]),
    count:   0,
  },
  config:         {},
  selectedTicker: /** @type {string|null} */ (null),
};

const _subs = /** @type {Map<string, Function[]>} */ (new Map());

function _eq(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a == null || b == null) return false;

  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    if (!a.length) return true;
    const a0 = a[0], b0 = b[0];
    if (typeof a0 === 'object' && typeof b0 === 'object') {
      const key = ('ticker' in (a0 || {}) && 'ticker' in (b0 || {})) ? 'ticker' : null;
      if (key) {
        for (let i = 0; i < a.length; i++) {
          if ((a[i] && a[i][key]) !== (b[i] && b[i][key])) return false;
        }
        return JSON.stringify(a) === JSON.stringify(b);
      }
    }
    return JSON.stringify(a) === JSON.stringify(b);
  }

  if (typeof a === 'object' && typeof b === 'object') {
    const ak = Object.keys(a);
    const bk = Object.keys(b);
    if (ak.length !== bk.length) return false;
    for (const k of ak) {
      if (!(k in b)) return false;
      if (a[k] !== b[k]) return false;
    }
    return true;
  }

  return false;
}

/**
 * Subscribe to a state key. `fn` is called with the new value on each change.
 * Use `'*'` to receive the full state on any change.
 */
export function subscribe(key, fn) {
  if (!_subs.has(key)) _subs.set(key, []);
  _subs.get(key).push(fn);
}

/** Read a state slice (or full state if no key given). */
export function get(key) {
  return key !== undefined ? _state[key] : { ..._state };
}

/**
 * Merge updates into state and notify subscribers for changed keys.
 * Uses JSON serialization for deep equality (small payloads, runs at 1 Hz).
 */
export function set(updates) {
  const changed = [];
  for (const [k, v] of Object.entries(updates)) {
    if (!_eq(_state[k], v)) {
      _state[k] = v;
      changed.push(k);
    }
  }
  for (const key of changed) {
    (_subs.get(key) ?? []).forEach(fn => {
      try { fn(_state[key]); } catch (e) { console.error('[store] subscriber error', key, e); }
    });
  }
  if (changed.length) {
    (_subs.get('*') ?? []).forEach(fn => {
      try { fn({ ..._state }); } catch (e) { console.error('[store] wildcard subscriber error', e); }
    });
  }
}

/** Convenience: select a ticker for TradingView display. */
export function selectTicker(ticker) {
  set({ selectedTicker: ticker });
}
