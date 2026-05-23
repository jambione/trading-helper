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
  transcriber: {
    running: false,
    lines:   /** @type {string[]} */ ([]),
    count:   0,
  },
  config:         {},
  selectedTicker: /** @type {string|null} */ (null),
};

const _subs = /** @type {Map<string, Function[]>} */ (new Map());

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
    if (JSON.stringify(_state[k]) !== JSON.stringify(v)) {
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
