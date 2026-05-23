/**
 * resizer.js — Drag-to-resize splitter between the scanner and watchlist panels
 *
 * Resizes the right (watchlist) column. Scanner takes the remaining 1fr.
 * Persists the chosen width to localStorage so it survives page reloads.
 */

const TRANSCRIPT_W   = 180;
const RESIZER_W      = 6;
const MIN_WATCHLIST  = 140;
const MIN_SCANNER    = 260;
const STORAGE_KEY    = 'ss:watchlist-w3';

export function init(gridEl, resizerEl) {
  if (!gridEl || !resizerEl) return;

  const saved = parseInt(localStorage.getItem(STORAGE_KEY), 10);
  if (saved >= MIN_WATCHLIST) {
    gridEl.style.gridTemplateColumns = `${TRANSCRIPT_W}px 1fr ${RESIZER_W}px ${saved}px`;
  }

  let _startX = 0;
  let _startW = 250;

  resizerEl.addEventListener('mousedown', e => {
    e.preventDefault();
    const cols = getComputedStyle(gridEl).gridTemplateColumns.split(' ');
    _startX = e.clientX;
    _startW = parseFloat(cols[3]) || 280;
    document.addEventListener('mousemove', _onMove);
    document.addEventListener('mouseup', _onUp, { once: true });
    gridEl.classList.add('main-grid--resizing');
  });

  function _onMove(e) {
    const delta  = e.clientX - _startX;
    const maxW   = gridEl.offsetWidth - TRANSCRIPT_W - RESIZER_W - MIN_SCANNER;
    // Dragging resizer right → watchlist shrinks; left → watchlist grows
    const newW   = Math.max(MIN_WATCHLIST, Math.min(_startW - delta, maxW));
    gridEl.style.gridTemplateColumns = `${TRANSCRIPT_W}px 1fr ${RESIZER_W}px ${newW}px`;
  }

  function _onUp() {
    document.removeEventListener('mousemove', _onMove);
    gridEl.classList.remove('main-grid--resizing');
    const cols = getComputedStyle(gridEl).gridTemplateColumns.split(' ');
    const w    = Math.round(parseFloat(cols[3]));
    if (w >= MIN_WATCHLIST) localStorage.setItem(STORAGE_KEY, String(w));
  }
}
