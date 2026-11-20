// app.js — entry point: view switching and cross-view navigation.
//
// Views are sections in one document, not routes on the server: the hash picks
// which is visible. Each module exposes init/show/hide so polling only runs for
// the view on screen.

import * as ops from './ops.js';
import * as submit from './submit.js';
import * as results from './results.js';
import * as analytics from './analytics.js';
import * as admin from './admin.js';

const VIEWS = {
  ops:       { mod: ops,       el: 'view-ops' },
  submit:    { mod: submit,    el: 'view-submit' },
  results:   { mod: results,   el: 'view-results' },
  analytics: { mod: analytics, el: 'view-analytics' },
  admin:     { mod: admin,     el: 'view-admin' }
};

let current = null;

function go(name, { silent = false } = {}) {
  if (!VIEWS[name]) name = 'ops';
  if (current === name) return;

  if (current && VIEWS[current].mod.hide) {
    try { VIEWS[current].mod.hide(); } catch (e) { console.warn(e); }
  }

  for (const [key, v] of Object.entries(VIEWS)) {
    const node = document.getElementById(v.el);
    if (node) node.classList.toggle('hidden', key !== name);
  }
  document.querySelectorAll('.rail-nav a[data-view]').forEach(a => {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });

  current = name;
  if (!silent && location.hash !== '#' + name) {
    history.replaceState(null, '', '#' + name);
  }

  if (VIEWS[name].mod.show) {
    try { VIEWS[name].mod.show(); } catch (e) { console.warn(e); }
  }

  // Move focus to the heading so keyboard and screen-reader users land in the
  // new view rather than at the top of the nav.
  const h = document.querySelector('#' + VIEWS[name].el + ' h1');
  if (h) {
    h.setAttribute('tabindex', '-1');
    h.focus({ preventScroll: true });
  }
}

const ctx = {
  go,
  /** Jump to Operations with a job selected (from a submission, say). */
  openJob(id) {
    ops.selectJob(id);
    go('ops');
  },
  /** Jump to Results for a finished job, loading it on the way. */
  openResults(id) {
    go('results');
    const sel = document.getElementById('resultsJob');
    if (sel) sel.value = id;
    results.load(id);
  },
  /** Jump to Results for one specific design, loading the job if needed.
   *  Used when a point is clicked in the similarity tree or either 3D map. */
  async openDesign(jobId, pdbId) {
    go('results');
    const sel = document.getElementById('resultsJob');
    if (sel) sel.value = jobId;
    if (results.currentJob() !== jobId) await results.load(jobId);
    if (!results.selectDesign(pdbId)) {
      // The design is in the plot but not in the table -- possible when the
      // embedding was computed over a larger set than the table now holds.
      // Say so rather than leaving the click looking broken.
      const sub = document.getElementById('resultsSub');
      if (sub) {
        sub.innerHTML += ' · <span style="color:var(--warn-ink)">'
          + 'could not find ' + String(pdbId) + ' in this table</span>';
      }
    }
  },
  /** Jump to Analytics for a finished job. */
  openAnalytics(id) {
    go('analytics');
    const sel = document.getElementById('analyticsJob');
    if (sel) sel.value = id;
    analytics.load(id);
  }
};

function boot() {
  for (const v of Object.values(VIEWS)) {
    if (v.mod.init) {
      try { v.mod.init(ctx); } catch (e) { console.error('[init]', e); }
    }
  }

  document.querySelectorAll('.rail-nav a[data-view]').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      go(a.dataset.view);
    });
  });

  window.addEventListener('hashchange', () => {
    go((location.hash || '#ops').slice(1), { silent: true });
  });

  go((location.hash || '#ops').slice(1), { silent: true });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
