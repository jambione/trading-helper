/**
 * Browser-tab signal while the AI book holds a position (operator 2026-10-28): the title leads with a dot, the open
 * count and the symbols, and the favicon gains a green badge, so the tab stands out among the others. Both return to
 * normal when the book is flat. Reads the same ai_positions slice as the header's AI status.
 */
import { subscribe } from './store.js?v=134';

const BASE_TITLE = document.title || 'Brasfield Momentum';
let _baseIcon = null;      // original favicon href
let _badged = null;        // data: URL of the badged icon, built once

/** Open positions as a sorted symbol list (the header's count uses the same keys). */
export function openSymbols(p) {
  const pos = p && typeof p.positions === 'object' && p.positions ? p.positions : {};
  return Object.keys(pos).sort();
}

/** The tab title for a list of open symbols. */
export function tabTitle(syms, base = BASE_TITLE) {
  if (!syms.length) return base;
  const shown = syms.slice(0, 3).join(' ') + (syms.length > 3 ? ` +${syms.length - 3}` : '');
  return `● ${syms.length} open · ${shown} — ${base}`;
}

function _iconLink() {
  return document.querySelector('link[rel="icon"]');
}

function _setIcon(on) {
  const link = _iconLink();
  if (!link) return;
  if (_baseIcon === null) _baseIcon = link.href;
  if (!on) {
    if (link.href !== _baseIcon) link.href = _baseIcon;
    return;
  }
  if (_badged) {
    link.href = _badged;
    return;
  }
  const img = new Image();
  img.onload = () => {
    try {
      const c = document.createElement('canvas');
      c.width = c.height = 64;
      const g = c.getContext('2d');
      g.drawImage(img, 0, 0, 64, 64);
      g.beginPath();
      g.arc(46, 18, 16, 0, Math.PI * 2);
      g.fillStyle = '#22c55e';
      g.fill();
      g.lineWidth = 4;
      g.strokeStyle = '#0b0b10';
      g.stroke();
      _badged = c.toDataURL('image/png');
      if (_iconLink() && document.title !== BASE_TITLE) _iconLink().href = _badged;
    } catch (e) {
      console.warn('[tabSignal] badge', e);
    }
  };
  img.src = _baseIcon;
}

export function init() {
  subscribe('ai_positions', p => {
    const syms = openSymbols(p || {});
    const title = tabTitle(syms);
    if (document.title !== title) document.title = title;
    _setIcon(syms.length > 0);
  });
}
