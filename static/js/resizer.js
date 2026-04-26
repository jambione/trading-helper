/**
 * resizer.js — Drag-to-resize splitter between the tickers and chart panels
 *
 * Reads the current tickers column width from the computed grid style,
 * then updates gridTemplateColumns inline on mousemove.
 * Persists the chosen width to localStorage so it survives page reloads.
 */

const TRANSCRIPT_W = 240;
const RESIZER_W    = 6;
const MIN_TICKERS  = 180;
const MIN_TV       = 280;
const STORAGE_KEY  = 'ss:tickers-w';

export function init(gridEl, resizerEl) {
  if (!gridEl || !resizerEl) return;

  // Restore saved width from a previous session
  const saved = parseInt(localStorage.getItem(STORAGE_KEY), 10);
  if (saved >= MIN_TICKERS) {
    gridEl.style.gridTemplateColumns = `${TRANSCRIPT_W}px ${saved}px ${RESIZER_W}px 1fr`;
  }

  let _startX = 0;
  let _startW = 300;

  resizerEl.addEventListener('mousedown', e => {
    e.preventDefault();

    const cols = getComputedStyle(gridEl).gridTemplateColumns.split(' ');
    _startX = e.clientX;
    _startW = parseFloat(cols[1]) || 300;

    document.addEventListener('mousemove', _onMove);
    document.addEventListener('mouseup', _onUp, { once: true });
    gridEl.classList.add('main-grid--resizing');
  });

  function _onMove(e) {
    const delta = e.clientX - _startX;
    const maxW  = gridEl.offsetWidth - TRANSCRIPT_W - RESIZER_W - MIN_TV;
    const newW  = Math.max(MIN_TICKERS, Math.min(_startW + delta, maxW));
    gridEl.style.gridTemplateColumns = `${TRANSCRIPT_W}px ${newW}px ${RESIZER_W}px 1fr`;
  }

  function _onUp() {
    document.removeEventListener('mousemove', _onMove);
    gridEl.classList.remove('main-grid--resizing');

    // Persist width after each drag so the next page load restores it
    const cols = getComputedStyle(gridEl).gridTemplateColumns.split(' ');
    const w    = Math.round(parseFloat(cols[1]));
    if (w >= MIN_TICKERS) localStorage.setItem(STORAGE_KEY, String(w));
  }
}
