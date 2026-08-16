/**
 * panelFlash.js — brief cyan highlight when a scan source's list grows.
 *
 * First snapshot per panel is baseline (no flash). Later membership growth
 * restarts a 5s glow on the visible Scan tab label (Momentum / Trend /
 * Research). Nested pane headers stay hidden, so they are not the target.
 */

const DURATION_MS = 5000;

/** data-panel → <label for="…"> of the Scan source tab. */
const SCAN_TAB_FOR = {
  tickers:   'scan-src-tickers',
  trending:  'scan-src-trending',
  claude:    'scan-src-claude',
};

/** @type {WeakMap<Element, ReturnType<typeof setTimeout>>} */
const _timers = new WeakMap();

/**
 * Restart a timed CSS class on el (reflow so the animation retriggers).
 * @param {Element} el
 * @param {string} cls
 */
function _pulse(el, cls) {
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);

  const prev = _timers.get(el);
  if (prev) clearTimeout(prev);
  const t = setTimeout(() => {
    el.classList.remove(cls);
    _timers.delete(el);
  }, DURATION_MS);
  _timers.set(el, t);
}

/**
 * Pulse the Scan tab for this pane (and the hidden pane header if present).
 * @param {Element | null} panelEl  section[data-panel=…]
 */
export function flashPanelHeader(panelEl) {
  if (!panelEl) return;

  const tabId = SCAN_TAB_FOR[panelEl.getAttribute('data-panel') || ''];
  if (tabId) {
    const tab = document.querySelector(`.scan-source-tab[for="${tabId}"]`);
    if (tab) _pulse(tab, 'scan-source-tab--new-entry');
  }

  const header = panelEl.querySelector('.panel-header');
  if (header) _pulse(header, 'panel-header--new-entry');
}

/**
 * Track symbol membership; flash when any new symbol appears after baseline.
 * @returns {(panelEl: Element | null, symbols: Iterable<string>) => void}
 */
export function createSymbolMembershipWatcher() {
  /** @type {Set<string> | null} */
  let known = null;
  /** @type {string} */
  let lastRev = '';

  /**
   * @param {Element | null} panelEl
   * @param {Iterable<string>} symbols
   * @param {string} [revision]  optional content fingerprint; flash when it
   *   changes after the baseline (quote ticks should omit this)
   */
  return function noteSymbols(panelEl, symbols, revision) {
    const next = new Set();
    for (const s of symbols) {
      const u = String(s || '').toUpperCase();
      if (u) next.add(u);
    }
    const rev = revision == null ? '' : String(revision);

    if (known === null) {
      known = next;
      lastRev = rev;
      return;
    }

    let added = false;
    for (const s of next) {
      if (!known.has(s)) {
        added = true;
        break;
      }
    }
    const revChanged = lastRev !== '' && rev !== '' && rev !== lastRev;
    known = next;
    lastRev = rev || lastRev;
    if (added || revChanged) flashPanelHeader(panelEl);
  };
}
