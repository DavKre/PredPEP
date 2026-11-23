// resizable.js — let the Results view be re-proportioned by the person using it.
//
// Two independent adjustments, because "I cannot see the structure" has two
// different causes:
//
//   * the SPLIT, left/right. A drag handle between the designs table and the
//     structure column. The table is wide by default (flex 999 against 1), which
//     is right for reading scores and wrong the moment you want to look at a
//     model.
//   * the VIEWER HEIGHT. The 3D canvas is 260px in this view. Widening the
//     column does not help a structure that is short rather than narrow, so the
//     canvas also gets a native resize grip.
//
// Both persist per browser in localStorage, which is the right store for a
// per-viewer preference: it is not shared state and losing it costs nothing.
// Every access is wrapped -- a private window or blocked site data throws on
// read, and a layout preference must never take the view down with it.
//
// NGL draws into a fixed-size canvas and does not notice its container moving,
// so every size change ends in stage.handleResize() via the viewer's resize().
// A ResizeObserver covers the drag, the grip, and anything else that reflows.

const STORE_SPLIT = 'predpep.results.sideWidth';
const STORE_VIEWER = 'predpep.results.viewerHeight';
const MIN_SIDE = 300;      // narrower and the structure card is useless
const MIN_TABLE = 420;     // keep enough table for the score columns to read
const MIN_VIEWER = 180;
const MAX_VIEWER = 1200;

function readStore(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function writeStore(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* not worth reporting */ }
}

/** Clamp the side column so neither pane can be dragged out of existence. */
function clampSide(split, px) {
  const total = split.clientWidth;
  const gap = 22;                       // the flex gap plus the handle
  const max = Math.max(MIN_SIDE, total - MIN_TABLE - gap);
  return Math.round(Math.min(Math.max(px, MIN_SIDE), max));
}

/** Clamp the viewer height to something a structure is actually visible in. */
function clampH(px) {
  return Math.round(Math.min(Math.max(px, MIN_VIEWER), MAX_VIEWER));
}

function applySide(split, px) {
  split.style.setProperty('--side-w', px + 'px');
  split.classList.add('split--sized');
}

/**
 * @param {object} opts
 * @param {string} opts.splitId   id of the flex row holding .wide and .side
 * @param {string} opts.handleId  id of the drag handle between them
 * @param {string} opts.viewerId  id of the NGL container
 * @param {string} opts.gripId    id of the bar under it that drags its height
 * @param {function} opts.onResize called (debounced by rAF) after any change
 */
export function makeResultsResizable({ splitId, handleId, viewerId, gripId, onResize }) {
  const split = document.getElementById(splitId);
  const handle = document.getElementById(handleId);
  const viewer = document.getElementById(viewerId);
  const grip = document.getElementById(gripId || 'resultViewerGrip');
  if (!split) return;

  let frame = 0;
  const notify = () => {
    if (!onResize || frame) return;
    frame = requestAnimationFrame(() => { frame = 0; onResize(); });
  };

  /* ---------------------------------------------------------- the split */

  const saved = parseInt(readStore(STORE_SPLIT) || '', 10);
  if (Number.isFinite(saved)) applySide(split, clampSide(split, saved));

  if (handle) {
    let dragging = false;

    const move = ev => {
      if (!dragging) return;
      const x = ev.touches ? ev.touches[0].clientX : ev.clientX;
      // The side column is on the right, so its width is whatever remains
      // between the pointer and the right edge of the row.
      const width = clampSide(split, split.getBoundingClientRect().right - x);
      applySide(split, width);
      notify();
    };
    const end = () => {
      if (!dragging) return;
      dragging = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      const px = parseInt(split.style.getPropertyValue('--side-w'), 10);
      if (Number.isFinite(px)) writeStore(STORE_SPLIT, String(px));
      notify();
    };
    const start = ev => {
      dragging = true;
      document.body.style.cursor = 'col-resize';
      // Without this a drag selects the table text it passes over.
      document.body.style.userSelect = 'none';
      ev.preventDefault();
    };

    handle.addEventListener('mousedown', start);
    handle.addEventListener('touchstart', start, { passive: false });
    window.addEventListener('mousemove', move);
    window.addEventListener('touchmove', move, { passive: true });
    window.addEventListener('mouseup', end);
    window.addEventListener('touchend', end);

    // Keyboard: a drag handle that only works with a mouse is not a control.
    handle.addEventListener('keydown', ev => {
      const step = ev.shiftKey ? 64 : 16;
      let delta = 0;
      if (ev.key === 'ArrowLeft') delta = step;        // left = wider structure
      else if (ev.key === 'ArrowRight') delta = -step;
      else if (ev.key === 'Home') delta = 9999;
      else if (ev.key === 'End') delta = -9999;
      else return;
      ev.preventDefault();
      const current = split.querySelector('.side')?.getBoundingClientRect().width || MIN_SIDE;
      const px = clampSide(split, current + delta);
      applySide(split, px);
      writeStore(STORE_SPLIT, String(px));
      notify();
    });

    // Double-click restores the stylesheet's proportions.
    handle.addEventListener('dblclick', () => {
      split.classList.remove('split--sized');
      split.style.removeProperty('--side-w');
      try { localStorage.removeItem(STORE_SPLIT); } catch (e) { /* ignore */ }
      notify();
    });
  }

  /* --------------------------------------------------------- the viewer */

  // `resize: vertical` was tried here and does not work: NGL fills the element
  // with its own canvas, which paints over the browser's resize corner. So the
  // height is driven by an explicit grip under the canvas instead.
  if (viewer) {
    const h = parseInt(readStore(STORE_VIEWER) || '', 10);
    if (Number.isFinite(h)) viewer.style.height = clampH(h) + 'px';

    if (grip) {
      let dragging = false, startY = 0, startH = 0;

      const move = ev => {
        if (!dragging) return;
        const y = ev.touches ? ev.touches[0].clientY : ev.clientY;
        viewer.style.height = clampH(startH + (y - startY)) + 'px';
        notify();
      };
      const end = () => {
        if (!dragging) return;
        dragging = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        writeStore(STORE_VIEWER, String(viewer.clientHeight));
        notify();
      };
      const start = ev => {
        dragging = true;
        startY = ev.touches ? ev.touches[0].clientY : ev.clientY;
        startH = viewer.clientHeight;
        document.body.style.cursor = 'row-resize';
        document.body.style.userSelect = 'none';
        ev.preventDefault();
      };

      grip.addEventListener('mousedown', start);
      grip.addEventListener('touchstart', start, { passive: false });
      window.addEventListener('mousemove', move);
      window.addEventListener('touchmove', move, { passive: true });
      window.addEventListener('mouseup', end);
      window.addEventListener('touchend', end);

      grip.addEventListener('keydown', ev => {
        const step = ev.shiftKey ? 80 : 20;
        let delta = 0;
        if (ev.key === 'ArrowDown') delta = step;
        else if (ev.key === 'ArrowUp') delta = -step;
        else if (ev.key === 'End') delta = 9999;
        else if (ev.key === 'Home') delta = -9999;
        else return;
        ev.preventDefault();
        viewer.style.height = clampH(viewer.clientHeight + delta) + 'px';
        writeStore(STORE_VIEWER, String(viewer.clientHeight));
        notify();
      });

      grip.addEventListener('dblclick', () => {
        viewer.style.removeProperty('height');   // back to the stylesheet's 260px
        try { localStorage.removeItem(STORE_VIEWER); } catch (e) { /* ignore */ }
        notify();
      });
    }

    // Covers the drag, the split, and anything else that reflows the card.
    if ('ResizeObserver' in window) new ResizeObserver(notify).observe(viewer);
  }
}

/* ===========================================================================
   The nav rail, collapsed to an icon strip.

   Width is a CSS class, not a measured pixel value, so .main reflows on its
   own. The only thing JS must do afterwards is tell the pixel-sized canvases
   to refit: Plotly figures are created with responsive:true and the NGL stage
   listens for window resize, so one dispatched resize event reaches both --
   after the 140ms width transition, or it measures the old width.
   =========================================================================== */

const STORE_RAIL = 'predpep.rail.collapsed';

export function makeRailCollapsible({ railId, toggleId } = {}) {
  const rail = document.getElementById(railId || 'rail');
  const btn = document.getElementById(toggleId || 'railToggle');
  if (!rail || !btn) return;

  const labels = [...rail.querySelectorAll('.rail-nav a')];

  const apply = (collapsed, persist) => {
    rail.classList.toggle('rail--collapsed', collapsed);
    btn.setAttribute('aria-expanded', String(!collapsed));
    btn.title = collapsed ? 'Expand the sidebar' : 'Collapse the sidebar';
    // Collapsed, the link text is off-screen, so the icon needs its own name.
    labels.forEach(a => {
      if (collapsed) a.title = a.textContent.trim();
      else a.removeAttribute('title');
    });
    if (persist) writeStore(STORE_RAIL, collapsed ? '1' : '0');
    setTimeout(() => window.dispatchEvent(new Event('resize')), 180);
  };

  apply(readStore(STORE_RAIL) === '1', false);
  btn.addEventListener('click', () => apply(!rail.classList.contains('rail--collapsed'), true));
}
