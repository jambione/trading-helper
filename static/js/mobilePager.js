/**
 * mobilePager.js — mobile-only horizontal pager across Momentum, Trending,
 * AI Watch, and AI Research panels.
 *
 * The actual swiping is native CSS scroll-snap on .main-grid (see styles.css).
 * This module only wires the tab bar to it: tap a tab to scroll to that panel,
 * and keep the active tab in sync as the user swipes. No-op on desktop.
 */

export function init() {
  if (!document.body.classList.contains('mobile')) return;

  const grid = document.querySelector('.main-grid');
  const tabs = Array.from(document.querySelectorAll('[data-pager-target]'));
  if (!grid || !tabs.length) return;

  const panelFor = t => grid.querySelector(`[data-panel="${t}"]`);

  // Tap a tab → scroll its panel into view.
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const panel = panelFor(tab.dataset.pagerTarget);
      if (panel) panel.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' });
    });
  });

  // Swipe → mark the panel nearest the viewport centre as active.
  const setActive = () => {
    const g = grid.getBoundingClientRect();
    const mid = g.left + g.width / 2;
    let best = null, bestDist = Infinity;
    tabs.forEach(tab => {
      const panel = panelFor(tab.dataset.pagerTarget);
      if (!panel) return;
      const r = panel.getBoundingClientRect();
      const dist = Math.abs((r.left + r.width / 2) - mid);
      if (dist < bestDist) { bestDist = dist; best = tab; }
    });
    tabs.forEach(t => t.classList.toggle('is-active', t === best));
  };

  let raf = 0;
  grid.addEventListener('scroll', () => {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; setActive(); });
  }, { passive: true });

  // Momentum loads first (leftmost slide).
  grid.scrollLeft = 0;
  setActive();
}
