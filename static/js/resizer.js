/**
 * resizer.js — Drag-to-resize splitter between the watchlist and chart panels
 *
 * Resizes the watchlist column. TradingView chart takes the remaining 1fr.
 * In hosted mode (auth required) the transcript column is absent — the grid
 * becomes 2 columns: watchlist | resizer | chart.
 * Persists the chosen width to localStorage so it survives page reloads.
 */

const TRANSCRIPT_W  = 180;
const RESIZER_W     = 6;
const MIN_WATCHLIST = 160;
const MIN_CHART     = 400;
const STORAGE_KEY   = 'ss:watchlist-w4';

export function init(gridEl, resizerEl, { hosted = false } = {}) {
  if (!gridEl || !resizerEl) return;

  const txW = hosted ? 0 : TRANSCRIPT_W;

  function _cols(wlW) {
    return hosted
      ? `${wlW}px ${RESIZER_W}px 1fr`
      : `${TRANSCRIPT_W}px ${wlW}px ${RESIZER_W}px 1fr`;
  }

  // Which computed column index holds the watchlist width
  const wlColIdx = hosted ? 0 : 1;

  const saved = parseInt(localStorage.getItem(STORAGE_KEY), 10);
  if (saved >= MIN_WATCHLIST) {
    gridEl.style.gridTemplateColumns = _cols(saved);
  }

  let _startX = 0;
  let _startW = 250;

  resizerEl.addEventListener('mousedown', e => {
    e.preventDefault();
    const cols = getComputedStyle(gridEl).gridTemplateColumns.split(' ');
    _startX = e.clientX;
    _startW = parseFloat(cols[wlColIdx]) || 250;
    document.addEventListener('mousemove', _onMove);
    document.addEventListener('mouseup', _onUp, { once: true });
    gridEl.classList.add('main-grid--resizing');
  });

  function _onMove(e) {
    const delta = e.clientX - _startX;
    const maxW  = gridEl.offsetWidth - txW - RESIZER_W - MIN_CHART;
    const newW  = Math.max(MIN_WATCHLIST, Math.min(_startW + delta, maxW));
    gridEl.style.gridTemplateColumns = _cols(newW);
  }

  function _onUp() {
    document.removeEventListener('mousemove', _onMove);
    gridEl.classList.remove('main-grid--resizing');
    const cols = getComputedStyle(gridEl).gridTemplateColumns.split(' ');
    const w    = Math.round(parseFloat(cols[wlColIdx]));
    if (w >= MIN_WATCHLIST) localStorage.setItem(STORAGE_KEY, String(w));
  }
}
