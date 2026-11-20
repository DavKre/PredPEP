// viewer.js — NGL wrapper.
//
// Fixes two problems in the old code. (1) A Stage built while its container is
// display:none has zero size and throws WebGL framebuffer errors; here creation
// is deferred until the element actually has a box, watched with a
// ResizeObserver instead of a setTimeout guess. (2) Each view gets its own
// instance rather than sharing window.stage.

/* Resolve the NGL namespace.
 *
 * Builds differ in what they put on the global: the old CDN script set
 * window.NGL with Stage directly on it, while an ES-module build wrapped for
 * UMD sets window.NGL = { default: {...} } -- so NGL.Stage is undefined and
 * `new NGL.Stage()` throws "not a constructor". Probe the shapes instead of
 * assuming one, and report what WAS found if none match. */
function resolveNGL() {
  const roots = [];
  if (typeof NGL !== 'undefined' && NGL) roots.push(NGL, NGL.default);
  if (typeof window !== 'undefined' && window.NGL) {
    roots.push(window.NGL, window.NGL.default, window.NGL.NGL);
  }
  for (const root of roots) {
    if (root && typeof root.Stage === 'function') return root;
  }
  return null;
}

function nglDiagnostic() {
  if (typeof NGL === 'undefined') return 'window.NGL is not defined — ngl.umd.js did not load.';
  try {
    const keys = Object.keys(NGL).slice(0, 12).join(', ');
    return 'NGL loaded but no Stage constructor found. Top-level keys: ' + (keys || '(none)');
  } catch {
    return 'NGL loaded but could not be inspected.';
  }
}

export function createViewer(containerId, opts = {}) {
  const state = {
    stage: null,
    component: null,
    pending: null,          // {kind:'url'|'text', value, label}
    rep: opts.rep || 'cartoon',
    observer: null
  };

  function container() {
    return document.getElementById(containerId);
  }

  function hasBox(node) {
    return node && node.offsetWidth > 0 && node.offsetHeight > 0;
  }

  function ensureStage() {
    const node = container();
    if (!node || state.stage) return state.stage;
    const lib = resolveNGL();
    if (!lib) {
      node.innerHTML = '<p class="empty">' + nglDiagnostic() + '</p>';
      console.error('[viewer]', nglDiagnostic());
      return null;
    }
    if (!hasBox(node)) {
      // Watch for the view becoming visible, then build and replay.
      if (!state.observer && 'ResizeObserver' in window) {
        state.observer = new ResizeObserver(() => {
          if (hasBox(container()) && !state.stage) {
            state.observer.disconnect();
            state.observer = null;
            if (ensureStage() && state.pending) replay();
          }
        });
        state.observer.observe(node);
      }
      return null;
    }
    node.innerHTML = '';
    state.stage = new lib.Stage(containerId, {
      backgroundColor: opts.background || '#0E1116',
      clipNear: 0,
      quality: 'medium'
    });
    window.addEventListener('resize', () => state.stage && state.stage.handleResize());
    return state.stage;
  }

  function overlay(text) {
    const node = container();
    if (!node) return;
    let o = node.querySelector('.viewer-overlay');
    if (!text) { if (o) o.remove(); return; }
    if (!o) {
      o = document.createElement('div');
      o.className = 'viewer-overlay';
      node.append(o);
    }
    o.textContent = text;
  }

  function legend() {
    const node = container();
    if (!node || node.querySelector('.viewer-legend')) return;
    const l = document.createElement('div');
    l.className = 'viewer-legend';
    l.innerHTML = '<span><i style="background:#3E6FD9"></i> chain A</span>' +
                  '<span><i style="background:#E08A3C"></i> peptide B</span>';
    node.append(l);
  }

  function represent(component, kind) {
    component.removeAllRepresentations();
    if (kind === 'surface') {
      component.addRepresentation('surface', {
        sele: 'protein and :A', color: '#3E6FD9', opacity: 0.55, side: 'front'
      });
      component.addRepresentation('licorice', { sele: ':B or hetero', color: '#E08A3C' });
    } else if (kind === 'contacts') {
      component.addRepresentation('cartoon', { sele: ':A', color: '#2A4C8F', opacity: 0.5 });
      component.addRepresentation('licorice', { sele: ':B or hetero', color: '#E08A3C' });
      component.addRepresentation('contact', {
        sele: ':A or :B', weakHydrogenBond: false, hydrophobic: true, maxDistance: 4.0
      });
      // Side chains of chain A within reach of the peptide.
      component.addRepresentation('licorice', {
        sele: ':A and sidechainAttached and (:B around 5)', color: '#8FA2C4', opacity: 0.85
      });
    } else {
      component.addRepresentation('cartoon', { sele: 'protein', color: '#3E6FD9' });
      component.addRepresentation('licorice', { sele: ':B or hetero', color: '#E08A3C' });
    }
  }

  async function replay() {
    const p = state.pending;
    if (!p) return;
    const stage = ensureStage();
    if (!stage) return;
    overlay('Loading ' + (p.label || '') + '…');
    stage.removeAllComponents();
    try {
      const source = p.kind === 'text'
        ? new Blob([p.value], { type: 'text/plain' })
        : p.value;
      const component = await stage.loadFile(source, { ext: 'pdb' });
      state.component = component;
      represent(component, state.rep);
      stage.autoView();
      legend();
      overlay(null);
    } catch (e) {
      overlay(null);
      const node = container();
      if (node) {
        node.insertAdjacentHTML('afterbegin',
          '<div class="viewer-overlay" style="background:#FBE3E1;color:#8E1B12">' +
          'Could not load ' + (p.label || 'structure') + '</div>');
      }
      console.warn('[viewer]', e);
    }
  }

  return {
    /** Load by URL (served through /stream_final_pdb). */
    loadUrl(url, label) {
      state.pending = { kind: 'url', value: url, label };
      return replay();
    },
    /** Load PDB text directly — used for the local file preview before upload. */
    loadText(text, label) {
      state.pending = { kind: 'text', value: text, label };
      return replay();
    },
    setRepresentation(kind) {
      state.rep = kind;
      if (state.component) {
        represent(state.component, kind);
        if (state.stage) state.stage.autoView();
      }
    },
    reset() { if (state.stage) state.stage.autoView(); },
    resize() { if (state.stage) state.stage.handleResize(); },
    clear() {
      if (state.stage) state.stage.removeAllComponents();
      state.component = null;
      state.pending = null;
      overlay(null);
    }
  };
}

/* ------------------------------------------------------ chain-B utilities */

/** Unique chain-B residues, in order, as three-letter codes.
 *  Dedup key is resSeq + iCode, which is stable across NMR models, so no
 *  MODEL/ENDMDL tracking is needed. The element guard on CA excludes calcium. */
export function chainBResidues(pdbText) {
  if (!pdbText) return [];
  const seen = new Map();
  for (const line of pdbText.split('\n')) {
    if (line.length < 27) continue;
    if (!(line.startsWith('ATOM') || line.startsWith('HETATM'))) continue;
    if (line.slice(12, 16).trim() !== 'CA') continue;
    const elem = line.slice(76, 78).trim();
    if (elem && elem !== 'C') continue;
    if (line[21] !== 'B') continue;
    const key = line.slice(22, 27);
    if (!seen.has(key)) seen.set(key, line.slice(17, 20).trim());
  }
  return [...seen.values()];
}

/** Chain-B residues with their PDB numbering, in order.
 *  Returns [{pos, resSeq, code}] where pos is 1-based sequence position and
 *  resSeq is the number in the file — these differ whenever the peptide does
 *  not start at residue 1, which is exactly the case the pipeline's hardcoded
 *  `declare -i x=1` got wrong. */
export function chainBProfile(pdbText) {
  if (!pdbText) return [];
  const seen = new Map();
  for (const line of pdbText.split('\n')) {
    if (line.length < 27) continue;
    if (!(line.startsWith('ATOM') || line.startsWith('HETATM'))) continue;
    if (line.slice(12, 16).trim() !== 'CA') continue;
    const elem = line.slice(76, 78).trim();
    if (elem && elem !== 'C') continue;
    if (line[21] !== 'B') continue;
    const key = line.slice(22, 27);
    if (!seen.has(key)) {
      seen.set(key, { resSeq: parseInt(line.slice(22, 26), 10), code: line.slice(17, 20).trim() });
    }
  }
  return [...seen.values()].map((r, i) => ({ pos: i + 1, resSeq: r.resSeq, code: r.code }));
}

/** Chain-B residue numbers involved in a disulfide, read from SSBOND records.
 *
 *  Rosetta reads SSBOND, gives those cysteines the disulfide variant type, and
 *  the packer will not design them away — which is why they came out invariant
 *  in every pepspec run regardless of the anchor. The form shows them as locked
 *  rather than offering a choice the pipeline would silently override.
 *  Returns a Map of resSeq -> partner resSeq. */
export function chainBDisulfides(pdbText) {
  const pairs = new Map();
  if (!pdbText) return pairs;
  for (const line of pdbText.split('\n')) {
    if (!line.startsWith('SSBOND')) continue;
    // Columns are fixed-width: chain at 15/29, resSeq at 17-21/31-35.
    const c1 = line[15], c2 = line[29];
    const r1 = parseInt(line.slice(17, 21), 10);
    const r2 = parseInt(line.slice(31, 35), 10);
    if (!isFinite(r1) || !isFinite(r2)) continue;
    if (c1 === 'B' && c2 === 'B') {
      pairs.set(r1, r2);
      pairs.set(r2, r1);
    }
  }
  return pairs;
}

/** Divisors of n in [min,max] — used for the core-count hint. */
export function divisorsInRange(n, min, max) {
  const out = [];
  const upper = Math.min(n, max);
  for (let i = min; i <= upper; i++) if (n % i === 0) out.push(i);
  return out;
}
