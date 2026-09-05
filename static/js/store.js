/**
 * store.js — Reactive centralized state
 *
 * Single responsibility: hold and broadcast application state.
 * No business logic. Modules subscribe to specific slices.
 *
 * Object/array equality uses a one-shot JSON cache per key so 4Hz WebSocket
 * pushes do not double-stringify large ticker lists on every message.
 */

const _state = {
  connected:      false,
  scan_running:   false,
  scan_ts:        '',
  tickers:        /** @type {Object[]} */ ([]),
  funnel:         /** @type {Object} */ ({}),
  trending:           /** @type {Object} */ ({}),
  movers:             /** @type {Object} */ ({}),
  claude_suggestions: /** @type {Object} */ ({}),
  ai_suggestions: /** @type {Object} */ ({}),
  /** Shared AI paper book (Grok/AGY owner): positions, mode, book_owner. */
  ai_positions:       /** @type {Object} */ ({}),
  price_spikes:       /** @type {Object[]} */ ([]),
  config:         {},
  selectedTicker: /** @type {string|null} */ (null),
};

const _subs = /** @type {Map<string, Function[]>} */ (new Map());
/** @type {Map<string, string>} last JSON for object/array slices */
const _ser  = new Map();

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
 */
export function set(updates) {
  const changed = [];
  for (const [k, v] of Object.entries(updates)) {
    if (v !== null && typeof v === 'object') {
      let s;
      try {
        s = JSON.stringify(v);
      } catch {
        _state[k] = v;
        _ser.delete(k);
        changed.push(k);
        continue;
      }
      if (_ser.get(k) === s) continue;
      _ser.set(k, s);
      _state[k] = v;
      changed.push(k);
    } else {
      if (Object.is(_state[k], v)) continue;
      _ser.delete(k);
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
