/**
 * panelFlash.js — brief cyan highlight on a panel header when new symbols join.
 *
 * First snapshot per panel is baseline (no flash). Later membership growth
 * restarts a 5s glow on that panel's .panel-header only.
 */

const DURATION_MS = 5000;

/** @type {WeakMap<Element, ReturnType<typeof setTimeout>>} */
const _timers = new WeakMap();

/**
 * Pulse the panel's title bar for 5s (restarts if already glowing).
 * @param {Element | null} panelEl  section[data-panel=…]
 */
export function flashPanelHeader(panelEl) {
  if (!panelEl) return;
  const header = panelEl.querySelector('.panel-header');
  if (!header) return;

  header.classList.remove('panel-header--new-entry');
  // Restart CSS animation if the class was already present.
  void header.offsetWidth;
  header.classList.add('panel-header--new-entry');

  const prev = _timers.get(header);
  if (prev) clearTimeout(prev);
  const t = setTimeout(() => {
    header.classList.remove('panel-header--new-entry');
    _timers.delete(header);
  }, DURATION_MS);
  _timers.set(header, t);
}

/**
 * Track symbol membership; flash when any new symbol appears after baseline.
 * @returns {(panelEl: Element | null, symbols: Iterable<string>) => void}
 */
export function createSymbolMembershipWatcher() {
  /** @type {Set<string> | null} */
  let known = null;

  return function noteSymbols(panelEl, symbols) {
    const next = new Set();
    for (const s of symbols) {
      const u = String(s || '').toUpperCase();
      if (u) next.add(u);
    }

    if (known === null) {
      known = next;
      return;
    }

    let added = false;
    for (const s of next) {
      if (!known.has(s)) {
        added = true;
        break;
      }
    }
    known = next;
    if (added) flashPanelHeader(panelEl);
  };
}
