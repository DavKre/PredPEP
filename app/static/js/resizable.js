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

function applySide(split, px) {
  split.style.setProperty('--side-w', px + 'px');
  split.classList.add('split--sized');
}

/**
 * @param {object} opts
 * @param {string} opts.splitId   id of the flex row holding .wide and .side
 * @param {string} opts.handleId  id of the drag handle between them
 * @param {string} opts.viewerId  id of the NGL container
 * @param {function} opts.onResize called (debounced by rAF) after any change
 */
export function makeResultsResizable({ splitId, handleId, viewerId, onResize }) {
  const split = document.getElementById(splitId);
  const handle = document.getElementById(handleId);
  const viewer = document.getElementById(viewerId);
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

  if (viewer) {
    const h = parseInt(readStore(STORE_VIEWER) || '', 10);
    if (Number.isFinite(h)) {
      viewer.style.height = Math.min(Math.max(h, MIN_VIEWER), MAX_VIEWER) + 'px';
    }
    if ('ResizeObserver' in window) {
      let last = viewer.clientHeight;
      new ResizeObserver(() => {
        notify();                                   // the canvas must follow
        const now = viewer.clientHeight;
        if (now !== last && now >= MIN_VIEWER) {     // remember a deliberate drag
          last = now;
          writeStore(STORE_VIEWER, String(now));
        }
      }).observe(viewer);
    }
  }
}
