/**
 * hotkeys.js — Keyboard shortcut manager
 *
 * Single responsibility: register and dispatch Alt+Key shortcuts.
 * Other modules call registerHotkey() to add their own shortcuts.
 * The reference panel in the dashboard reads getHotkeys() to render
 * the documented list.
 */

/** @type {Map<string, {label: string, action: () => void}>} */
const _registry = new Map();

let _panelEl  = null;   // floating reference panel element
let _btnEl    = null;   // ⌨ Keys toggle button

// ── Public ─────────────────────────────────────────────────────

/**
 * Register a hotkey.
 * @param {string}   key    Single character, case-insensitive  (e.g. 'a')
 * @param {string}   label  Human-readable description shown in the panel
 * @param {Function} action Called when Alt+key is pressed
 */
export function registerHotkey(key, label, action) {
  _registry.set(key.toLowerCase(), { label, action });
}

/** Returns a sorted array of { key, label } for display. */
export function getHotkeys() {
  return [..._registry.entries()]
    .map(([key, { label }]) => ({ key: key.toUpperCase(), label }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

/**
 * Initialize the hotkey system.
 * @param {HTMLElement} panelEl   The floating reference panel element
 * @param {HTMLElement} btnEl     The toggle button that opens/closes the panel
 */
export function init(panelEl, btnEl) {
  _panelEl = panelEl;
  _btnEl   = btnEl;

  // Global keydown listener
  document.addEventListener('keydown', e => {
    // Ignore when typing in an input / textarea / select
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return;

    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      const entry = _registry.get(e.key.toLowerCase());
      if (entry) {
        e.preventDefault();
        entry.action();
      }
    }
  });

  // Toggle panel open/close
  if (_btnEl) {
    _btnEl.addEventListener('click', _togglePanel);
  }

  // Close panel when clicking outside
  document.addEventListener('click', e => {
    if (_panelEl && _btnEl &&
        !_panelEl.contains(e.target) &&
        !_btnEl.contains(e.target)) {
      _closePanel();
    }
  });
}

// ── Panel ───────────────────────────────────────────────────────

function _togglePanel() {
  if (!_panelEl) return;
  const isOpen = _panelEl.classList.contains('hotkey-panel--open');
  isOpen ? _closePanel() : _openPanel();
}

function _openPanel() {
  if (!_panelEl) return;
  _renderPanel();
  _panelEl.classList.add('hotkey-panel--open');
  if (_btnEl) _btnEl.classList.add('btn--active');
}

function _closePanel() {
  if (!_panelEl) return;
  _panelEl.classList.remove('hotkey-panel--open');
  if (_btnEl) _btnEl.classList.remove('btn--active');
}

function _renderPanel() {
  if (!_panelEl) return;
  const hotkeys = getHotkeys();
  _panelEl.innerHTML = `
    <div class="hotkey-panel-title">Keyboard Shortcuts</div>
    <table class="hotkey-table">
      ${hotkeys.map(({ key, label }) => `
        <tr>
          <td><kbd class="hotkey-kbd">Alt</kbd><span class="hotkey-sep">+</span><kbd class="hotkey-kbd">${key}</kbd></td>
          <td class="hotkey-label">${label}</td>
        </tr>`).join('')}
    </table>
  `;
}
