// analytics.js — Analytics view.
//
// Thin on purpose: correlation, PCA, the sequence embedding and the
// position-preference counts are computed server-side (/jobs/<id>/analytics)
// with pandas and numpy, which the node already depends on. The browser draws
// and recolours.
//
// COLOURING IS SHARED ACROSS THE THREE MAPS
// -----------------------------------------
// The similarity tree, the score-space PCA and the sequence-space embedding all
// plot the same designs with different geometry. One "colour designs by"
// control drives all three, so a colour means the same thing in each and the
// three can be read together: a group that clusters in sequence space but
// scatters in score space is telling you something, and that only reads if the
// colours agree.
//
// Recolouring never refetches. The attribute values come from metadata the view
// already holds, and are applied with Plotly.restyle, so the current zoom and
// camera angle survive a change of colouring.

import { api, esc } from './api.js';
import { renderLogo } from './charts.js';
import * as consoleUI from './console.js';
import {
  isAvailable, clear, renderLine, renderScatter, renderHeatmap,
  renderScatter3d, renderTree, recolour, colourMapping,
  COLOUR_FIELDS, residuePositionFields, rosettaTermFields,
  similarityFields, rdkitFields,
} from './plots.js';

const CHART_IDS = ['chartEvolution', 'chartAgreement', 'chartHeatmap', 'chartPca',
                   'chartPca3d', 'chartSeqmap', 'chartLogo', 'chartTmap'];
const NOTE_IDS = ['noteEvolution', 'noteAgreement', 'noteHeatmap', 'notePca',
                  'notePca3d', 'noteSeqmap', 'noteTmap'];

const state = {
  jobId: null,
  loading: false,
  data: null,          // the analytics bundle
  tmap: null,          // the tree layout + per-design metadata
  rowsById: new Map(),  // pdbId key -> metadata row, for joining
  fields: [],          // colour-by fields available for this job
  colourKey: 'foldx',
  onOpenDesign: null,  // set by app.js so a click can jump to Results
};

/* Read a theme token at call time so a theme change lands on the next draw. */
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v && v.trim()) || fallback;
}

function note(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text || '';
}

function placeholder(hostId, message) {
  clear(hostId);
  const el = document.getElementById(hostId);
  if (el) el.innerHTML = '<p class="empty">' + esc(message) + '</p>';
}

function clearAll(message) {
  CHART_IDS.forEach(id => placeholder(id, message));
  NOTE_IDS.forEach(id => note(id, ''));
  const leg = document.getElementById('colourLegend');
  if (leg) leg.innerHTML = '';
}

/* Design identity keys differ between tables: the final score table uses a bare
   filename (SPCGRK1I262L3_2.pdb) while the per-model table uses pepID, which
   may carry a directory and a .mod suffix. Normalise both to the same key so
   the 3D PCA can be coloured by attributes that live in the final table. */
function idKey(v) {
  let s = String(v || '')
    .split('/').pop()
    .replace(/\.mod\.pdb$/i, '')
    .replace(/\.pdb$/i, '');
  // Strip the per-model variant suffixes run_catSPEC.py distinguishes:
  // SPCGRK1I2I3I255W1_6_mc19 and ..._full and ..._soft are all the SAME design
  // as SPCGRK1I2I3I255W1_6 at different stages of its Monte Carlo trajectory.
  // The 3D score plot is drawn from all_model_scores.csv (42k rows, variants
  // included) while the colour attributes live in the final table (800 base
  // PDBs), so without this nearly every point fails to join and renders grey.
  // Repeated because a name can carry more than one suffix.
  let prev;
  do {
    prev = s;
    s = s.replace(/_(?:mc\d*|full|soft)$/i, '');
  } while (s !== prev);
  return s;
}

function rowFor(id) {
  return state.rowsById.get(idKey(id)) || null;
}

/* ---------------------------------------------------------------- colouring */

function currentField() {
  return state.fields.find(f => f.key === state.colourKey) || state.fields[0];
}

function renderColourControl() {
  const sel = document.getElementById('colourBy');
  if (!sel) return;
  if (!state.fields.length) {
    sel.innerHTML = '<option value="">No attributes available</option>';
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  // Grouped with optgroups: with Rosetta terms, composition descriptors and one
  // entry per peptide position, a flat list of forty-odd options is unusable.
  const order = ['Scores', 'Rosetta terms', 'Similarity', 'Composition',
                 'RDKit descriptors', 'Run', 'By position'];
  const groups = new Map();
  for (const f of state.fields) {
    const g = f.group || 'Other';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(f);
  }
  const names = [...groups.keys()].sort(
    (a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
  sel.innerHTML = names.map(g =>
    `<optgroup label="${esc(g)}">`
    + groups.get(g).map(f =>
      `<option value="${esc(f.key)}">${esc(f.label)}</option>`).join('')
    + '</optgroup>').join('');
  sel.value = currentField().key;
}

function renderLegend(mapping) {
  const el = document.getElementById('colourLegend');
  if (!el) return;
  if (!mapping || mapping.kind === 'none') {
    el.innerHTML = `<span class="small muted">${esc(mapping ? mapping.note : '')}</span>`;
    return;
  }
  if (mapping.kind === 'num') {
    // A colour bar is drawn inside each figure, so the legend only explains it.
    el.innerHTML = `<span class="small muted">${esc(mapping.note)} · `
      + 'darker is lower</span>';
    return;
  }
  el.innerHTML = mapping.legend.map(item =>
    '<span class="legend-item">'
    + `<span class="legend-swatch" style="background:${esc(item.colour)}"></span>`
    + `${esc(item.label)} <span class="muted">${item.count}</span></span>`
  ).join('')
    + `<span class="small muted" style="display:block;margin-top:4px">${esc(mapping.note)}</span>`;
}

/* Build a mapping for an ordered list of ids by looking each one up in the
   final-table metadata. Reports how many could not be matched rather than
   colouring them silently, because an unmatched point is grey and a reader
   should know why. */
function mappingForIds(ids, field) {
  const rows = ids.map(id => rowFor(id) || {});
  const matched = ids.filter(id => rowFor(id)).length;
  const mapping = colourMapping(field, rows);
  mapping.matched = matched;
  mapping.total = ids.length;
  if (matched < ids.length) {
    mapping.note += ` · ${ids.length - matched} point(s) could not be matched `
      + 'to the score table and are grey';
  }
  return mapping;
}

function applyColour() {
  const field = currentField();
  if (!field) return;

  // Tree: nodes are trace 1 (trace 0 is the edge set).
  if (state.tmap && state.tmap.x) {
    const rows = state.tmap.metadata || [];
    const m = colourMapping(field, rows);
    recolour('chartTmap', 1, m);
    renderLegend(m);
    note('noteTmap', 'Branch length reflects sequence fingerprint distance, not '
      + 'score. Drag to pan, scroll to zoom, click a node to open that design.');
  }

  // 3D PCA: ids come from the per-model table, so they need the join.
  const pca = state.data && state.data.pca;
  if (pca && pca.points && pca.points.length) {
    const m = mappingForIds(pca.points.map(p => p.id), field);
    recolour('chartPca3d', 0, m);
  }

  // Sequence map: ids are final-table pdbIds, so they match directly.
  const sm = state.data && state.data.seqmap;
  if (sm && sm.points && sm.points.length) {
    const m = mappingForIds(sm.points.map(p => p.id), field);
    recolour('chartSeqmap', 0, m);
  }
}

/* Draw one panel, containing any failure to that panel.

   The console keeps the full stack -- a chart library's own error text rarely
   says which figure produced it, so the panel id is logged alongside. */
function panel(chartId, noteId, fn) {
  try {
    fn();
  } catch (e) {
    console.error('[analytics] panel ' + chartId + ' failed:', e);
    placeholder(chartId, 'This panel could not be drawn.');
    if (noteId) note(noteId, (e && e.message) || String(e));
  }
}

/* ------------------------------------------------------- computed findings

   One rule here: every sentence is derived from this job's own numbers, and a
   figure whose finding cannot be computed returns null so the panel says so
   rather than printing a template with a number dropped in. A fabricated
   finding reads exactly like a real one -- the reader has no way to tell -- so
   "no finding" is the honest output and null is not a failure.

   Each function therefore checks for the fields it needs and bails rather
   than coercing. undefined * 1 is NaN, and NaN.toFixed(2) is the string
   "NaN", which would be printed as confidently as a real measurement. */

function fx(v, d) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : null; }

function concEvolution() {
  const it = state.data && state.data.evolution && state.data.evolution.iterations;
  if (!it || it.length < 2) return null;
  const best = it.map(r => r.best).filter(v => typeof v === 'number' && isFinite(v));
  const med = it.map(r => r.median).filter(v => typeof v === 'number' && isFinite(v));
  if (best.length < 2) return null;

  const gainTotal = best[0] - best[best.length - 1];      // positive = improved
  const gainLast = best[best.length - 2] - best[best.length - 1];
  const medGain = med.length >= 2 ? med[0] - med[med.length - 1] : null;

  // A run that barely moved is a different finding from one that moved and
  // then stopped, and the two call for different next steps: the first says
  // the input was already at a plateau, the second that the search finished.
  if (Math.abs(gainTotal) < 0.1) {
    return `Best ΔΔG moved ${fx(gainTotal, 2)} kcal/mol across ${it.length} `
         + `iterations — effectively flat. This job began at or very near a `
         + `plateau, so there was little for a canonical search to find.`
         + samplingCaveat();
  }

  let out = `Best ΔΔG improved by ${fx(gainTotal, 2)} kcal/mol over ${it.length} `
          + `iterations, ending at ${fx(best[best.length - 1], 2)}.`;
  if (medGain != null) {
    out += Math.abs(medGain) > Math.abs(gainTotal) * 0.4
      ? ` The median improved by ${fx(medGain, 2)} alongside it, so the whole `
        + `population moved rather than one design pulling ahead.`
      : ` The median improved by only ${fx(medGain, 2)}, so the gain is `
        + `concentrated in a few designs rather than the population.`;
  }
  out += ` The last iteration gained ${fx(gainLast, 3)}.`;
  return out + samplingCaveat();
}

/* Appended to any conclusion drawn from per-iteration numbers. A crashed
   pepspec run yields fewer models rather than failing the job, so the anchors
   it was sampling are ranked against anchors that got the full budget. When
   the loss sits in one iteration, that iteration's best and median are drawn
   from a smaller pool than the rest of the curve -- which is a reason to
   distrust a step in the trend, not a reason to distrust the job. Returns ''
   when nothing was lost or nothing was recorded, so the usual case reads
   clean. */
function samplingCaveat() {
  const sp = (state.data && state.data.sampling) || null;
  if (!sp || !sp.runs_failed) return '';
  const w = sp.worst_iteration;
  let s = ` Note: ${sp.runs_failed} of ${sp.runs_total} pepspec runs`
        + ` (${fx(sp.loss_pct, 1)}%) did not finish`;
  if (w && w.runs_failed) {
    s += `, ${w.runs_failed} of them in iteration ${w.iteration}`
       + ` — ${fx(100 * w.runs_failed / w.runs_total, 0)}% of that round's`
       + ` sampling. Read that iteration's position on this curve with that`
       + ` in mind.`;
  } else {
    s += `, so some anchors were ranked on less sampling than others.`;
  }
  return s;
}

function concAgreement() {
  const a = state.data && state.data.agreement;
  if (!a || !a.points || a.points.length < 2) return null;
  const fit = linearFit(a.points);
  if (!fit || !isFinite(fit.r)) return null;
  const n = a.points.length;
  const total = state.data.design_count;
  let out = `The two scores agree at r = ${fx(fit.r, 2)} over ${n} design(s).`;
  if (typeof total === 'number' && total > n) {
    out += ` ${total - n} of ${total} are absent: a design FoldX could not `
         + `score has no point here at all, rather than appearing as an outlier.`;
  }
  return out;
}

function concHeatmap() {
  const c = state.data && state.data.correlation;
  if (!c || !c.labels || !c.matrix) return null;
  let bestPair = null;
  for (let i = 0; i < c.labels.length; i++) {
    for (let j = i + 1; j < c.labels.length; j++) {
      const v = c.matrix[i] && c.matrix[i][j];
      if (typeof v !== 'number' || !isFinite(v)) continue;
      if (!bestPair || Math.abs(v) > Math.abs(bestPair.v)) {
        bestPair = { a: c.labels[i], b: c.labels[j], v };
      }
    }
  }
  if (!bestPair) return null;
  return `Across ${c.labels.length} terms, the strongest pairing is `
       + `${bestPair.a} with ${bestPair.b} at r = ${fx(bestPair.v, 2)}. `
       + `Terms that move together are measuring overlapping physics, so they `
       + `do not add independent evidence about a design.`;
}

function concPca(three) {
  const p = state.data && state.data.pca;
  if (!p) return null;
  const v = three ? p.variance3 : p.variance;
  if (typeof v !== 'number' || !isFinite(v)) return null;
  const pct = (v <= 1 ? v * 100 : v);
  const n = (p.points && p.points.length) || null;
  return `The first ${three ? 'three components carry' : 'two components carry'} `
       + `${pct.toFixed(0)}% of the variance in the score vectors`
       + (n ? ` across ${n} design(s)` : '') + `. The projection is fitted on `
       + `this job alone, so these axes are not the same axes as another job's.`;
}

function concSeqmap() {
  const m = state.data && state.data.seqmap;
  if (!m || !m.points || !m.points.length) return null;
  let out = `${m.points.length} design(s) placed by sequence similarity`
          + (m.method ? ` (${m.method})` : '') + `.`;
  if (typeof m.explained === 'number' && isFinite(m.explained)) {
    const pct = m.explained <= 1 ? m.explained * 100 : m.explained;
    out += ` The embedding preserves ${pct.toFixed(0)}% of the pairwise `
         + `distances, so read grouping rather than exact position.`;
  }
  if (m.low_confidence) {
    out += ` Flagged low confidence: treat the layout as indicative only.`;
  }
  return out;
}

function concLogo() {
  const cols = state.data && state.data.logo && state.data.logo.columns;
  if (!cols || !cols.length) return null;
  let fixed = null, open = null;
  cols.forEach((col, i) => {
    const top = (col.stack || []).reduce((m, s) =>
      (typeof s.freq === 'number' && s.freq > (m ? m.freq : -1)) ? s : m, null);
    if (!top) return;
    if (!fixed || top.freq > fixed.freq) fixed = { i: i + 1, code: top.code, freq: top.freq };
    if (!open || top.freq < open.freq) open = { i: i + 1, freq: top.freq };
  });
  if (!fixed) return null;
  const ncaa = cols.some(col => (col.stack || []).some(s => s.kind && s.kind !== ''));
  let out = `Position ${fixed.i} is the most settled: ${fixed.code} in `
          + `${Math.round(fixed.freq)}% of the designs summarised here.`;
  if (open && open.i !== fixed.i) {
    out += ` Position ${open.i} is the least, with its commonest residue at `
         + `only ${Math.round(open.freq)}% — the search never decided it.`;
  }
  if (ncaa) out += ` At least one position carries a non-canonical residue.`;
  return out;
}

function concTmap() {
  const t = state.tmap;
  if (!t || !t.x || !t.x.length) return null;
  return `${t.x.length} design(s) joined by sequence similarity. Branch length `
       + `carries similarity; the direction a branch happens to take does not.`;
}

function conclusionFor(key) {
  try {
    switch (key) {
      case 'evolution': return concEvolution();
      case 'agreement': return concAgreement();
      case 'heatmap':   return concHeatmap();
      case 'pca':       return concPca(false);
      case 'pca3d':     return concPca(true);
      case 'seqmap':    return concSeqmap();
      case 'logo':      return concLogo();
      case 'tmap':      return concTmap();
      default:          return null;
    }
  } catch (e) {
    // A finding that threw is a finding we do not have.
    console.error('[analytics] conclusion for ' + key + ' failed:', e);
    return null;
  }
}

/* The readout strip. Every cell may be null, and null renders as an em dash
   rather than 0 -- a zero is a measurement, a dash is the absence of one. */
function buildStrip() {
  const d = state.data || {};
  const it = (d.evolution && d.evolution.iterations) || [];
  const bestNow = it.length
    ? it.map(r => r.best).filter(v => isFinite(v)).pop() : null;
  const fit = (d.agreement && d.agreement.points && d.agreement.points.length > 1)
    ? linearFit(d.agreement.points) : null;
  const pcv = d.pca && typeof d.pca.variance === 'number'
    ? (d.pca.variance <= 1 ? d.pca.variance * 100 : d.pca.variance) : null;

  const cells = [
    { label: 'Designs', value: d.design_count ?? null,
      sub: d.noncanonical_count != null ? d.noncanonical_count + ' non-canonical' : '' },
    { label: 'Best ΔΔG', value: fx(bestNow, 2),
      sub: it.length ? 'iteration ' + it[it.length - 1].iteration : '' },
    { label: 'FoldX ↔ Rosetta', value: fit ? 'r ' + fx(fit.r, 2) : null,
      sub: fit ? d.agreement.points.length + ' scored' : 'not comparable' },
    { label: 'PC1–PC2', value: pcv != null ? pcv.toFixed(0) + '%' : null,
      sub: 'of score variance' },
    { label: 'Iterations', value: d.iteration_count ?? null, sub: '' },
  ];

  /* Failed pepspec runs. The manager logs "N run(s), M failed" per iteration
     and a crashed run yields fewer models, so those anchors are ranked on a
     fraction of the sampling the others got -- which is worth a readout and
     is currently visible only by reading a log. Shown only when the API
     reports it; a dash here would just raise a question it cannot answer. */
  if (typeof d.runs_failed === 'number') {
    const sp = d.sampling || {};
    const w = sp.worst_iteration;
    /* A job-wide total hides concentration: 7 of 126 reads as background
       noise, while 7 of 42 inside one iteration is a third of that round's
       sampling gone. Name the round when one carries the losses. */
    let sub = d.runs_total ? 'of ' + d.runs_total : 'pepspec crashes';
    if (w && w.runs_failed === d.runs_failed && sp.iterations
        && sp.iterations.length > 1) {
      sub += ' \u00b7 all in it' + w.iteration;
    } else if (w && w.runs_failed) {
      sub += ' \u00b7 worst it' + w.iteration + ' (' + w.runs_failed
           + '/' + w.runs_total + ')';
    }
    cells.push({ label: 'Runs failed', value: d.runs_failed,
                 tone: d.runs_failed > 0 ? 'warn' : null, sub: sub });
  }
  return cells;
}

/* ------------------------------------------------------------------ draw */

function drawEvolution(data) {
  if (!data || !data.iterations || !data.iterations.length) {
    placeholder('chartEvolution', 'No per-iteration metrics for this job.');
    note('noteEvolution', 'Derived from the _iterN_ marker in each design\'s path; '
      + 'a job with a single iteration has nothing to trend.');
    return;
  }
  const it = data.iterations;
  renderLine('chartEvolution', {
    series: [
      { name: 'best', x: it.map(r => 'it' + r.iteration), y: it.map(r => r.best) },
      /* From the theme, not a literal: this grey was invisible on the dark
         panel and is the only hardcoded colour left in this file. */
      { name: 'median', x: it.map(r => 'it' + r.iteration), y: it.map(r => r.median),
        dash: 'dash', colour: cssVar('--series-3', '#9AA3AD') },
    ],
    xTitle: 'iteration', yTitle: 'FoldX ΔΔG (kcal/mol)',
    filename: 'evolution',
  });
  const first = it[0], last = it[it.length - 1];
  const gain = (isFinite(first.best) && isFinite(last.best))
    ? (first.best - last.best).toFixed(1) : null;
  note('noteEvolution', (gain
    ? `Best improved by ${gain} kcal/mol across ${it.length} iterations. `
    : '') + 'Hover a point for its value; drag to zoom.');
}

function drawAgreement(data) {
  if (!data || !data.points || data.points.length < 2) {
    placeholder('chartAgreement', 'Needs both FoldX and Rosetta scores.');
    note('noteAgreement', 'A partial run with no Rosetta aggregation cannot be compared.');
    return;
  }
  const pts = data.points;
  const fit = linearFit(pts);
  renderScatter('chartAgreement', {
    x: pts.map(p => p.x), y: pts.map(p => p.y),
    rows: pts.map(p => ({ pdbId: p.id || '', FoldX_Score: p.x, Rosetta_Total_Score: p.y })),
    xTitle: 'FoldX ΔΔG (kcal/mol)', yTitle: 'Rosetta total',
    fit, filename: 'foldx-vs-rosetta',
    onPick: id => state.onOpenDesign && state.onOpenDesign(id),
  });
  const label = document.getElementById('corrLabel');
  if (label && fit) {
    label.textContent = 'agreement between the two scores · r = ' + fit.r.toFixed(2);
  }
  note('noteAgreement', 'Dashed line is the least-squares fit. Box-select a region '
    + 'to inspect it; double-click to reset.');
}

function drawHeatmap(data) {
  if (!data || !data.labels || !data.labels.length) {
    placeholder('chartHeatmap', 'No score terms available.');
    note('noteHeatmap', 'The per-term scores live in all_model_scores.csv; if the merge '
      + 'kept only total_score, nothing reaches this view.');
    return;
  }
  renderHeatmap('chartHeatmap', {
    z: data.matrix, labels: data.labels, filename: 'term-correlation',
  });
  note('noteHeatmap', 'Blue positive, amber negative. Hover a cell for the coefficient.');
}

function drawPca(data) {
  if (!data || !data.points || data.points.length < 3) {
    placeholder('chartPca', 'Not enough designs for a projection.');
    note('notePca', 'Needs at least three designs with complete score terms.');
    return;
  }
  renderScatter('chartPca', {
    x: data.points.map(p => p.x), y: data.points.map(p => p.y),
    rows: data.points.map(p => ({ pdbId: p.id || '' })),
    xTitle: 'PC1', yTitle: 'PC2', filename: 'pca-2d',
    onPick: id => state.onOpenDesign && state.onOpenDesign(id),
  });
  const label = document.getElementById('pcaLabel');
  if (label) {
    label.textContent = 'PC1 + PC2'
      + (data.variance ? ' · ' + Math.round(data.variance * 100) + '% variance explained' : '');
  }
  note('notePca', 'Click a point to open that design in Results.');
}

function drawPca3d(data) {
  if (!data || !data.points || data.points.length < 4 || data.components < 3) {
    placeholder('chartPca3d', 'Not enough designs for a 3D projection.');
    note('notePca3d', 'Needs at least four designs and three usable score terms.');
    return;
  }
  const field = currentField();
  const mapping = field ? mappingForIds(data.points.map(p => p.id), field) : null;
  renderScatter3d('chartPca3d', {
    points: data.points,
    rows: data.points.map(p => rowFor(p.id) || { pdbId: p.id }),
    mapping,
    axisTitles: (data.variance_axes || []).length >= 3
      ? data.variance_axes.slice(0, 3).map((v, i) => `PC${i + 1} (${Math.round(v * 100)}%)`)
      : ['PC1', 'PC2', 'PC3'],
    filename: 'pca-3d',
    onPick: id => state.onOpenDesign && state.onOpenDesign(id),
  });
  const label = document.getElementById('pca3dLabel');
  if (label) {
    label.textContent = 'PC1–PC3'
      + (data.variance3 ? ' · ' + Math.round(data.variance3 * 100) + '% variance explained' : '');
  }
  note('notePca3d', 'Score space: position is set by the Rosetta energy terms. '
    + 'Drag to rotate, scroll to zoom, click a point to open that design.');
}

function drawSeqmap(data) {
  if (!data || !data.points || !data.points.length) {
    placeholder('chartSeqmap', 'No sequence embedding available.');
    note('noteSeqmap', (data && data.note) || 'Needs at least four designs with '
      + 'fingerprintable sequences.');
    return;
  }
  const field = currentField();
  const mapping = field ? mappingForIds(data.points.map(p => p.id), field) : null;
  renderScatter3d('chartSeqmap', {
    points: data.points,
    rows: data.points.map(p => rowFor(p.id) || { pdbId: p.id, pepSeq: p.seq }),
    mapping,
    axisTitles: ['MDS 1', 'MDS 2', 'MDS 3'],
    filename: 'sequence-map',
    onPick: id => state.onOpenDesign && state.onOpenDesign(id),
  });
  // The server flags an embedding that does not reduce well to three
  // dimensions. Passed through verbatim: a 3D scatter always looks like
  // clusters, so the caveat has to travel with the picture.
  note('noteSeqmap', (data.note || '')
    + (data.low_confidence ? '' : ' Drag to rotate; click a point to open that design.'));
  const label = document.getElementById('seqmapLabel');
  if (label) {
    label.textContent = (data.method || 'sequence similarity')
      + (data.explained ? ' · ' + Math.round(data.explained * 100) + '% of distances' : '');
  }
}

function drawLogo(data) {
  if (!data || !data.columns || !data.columns.length) {
    placeholder('chartLogo', 'No sequences to summarise.');
    return;
  }
  // Left on the hand-rolled SVG renderer: a stacked three-letter-code logo is
  // not a chart type Plotly does well, and this one already reads correctly.
  renderLogo('chartLogo', data.columns);
  /* How many designs the frequencies are over. Taken from the payload, which
     reports the rows actually used, rather than written in here: the server
     picks the cut (logo(top=100)) and caps it at however many designs exist,
     so a number hardcoded in the page would claim 100 for a job with 23.
     Blank when the server does not say. */
  const foot = document.getElementById('figPinnedFoot');
  if (foot) {
    foot.textContent = data.designs ? 'top ' + data.designs + ' designs' : '';
  }
}

function drawTmap(res) {
  state.tmap = res && res.success ? res : null;
  if (!res || !res.success || !res.x || res.x.length < 2) {
    placeholder('chartTmap', 'No tree layout available.');
    note('noteTmap', (res && res.error) || 'No layout was produced for this job.');
    return;
  }
  const rows = res.metadata || [];
  const field = currentField();
  const mapping = field ? colourMapping(field, rows) : null;
  renderTree('chartTmap', {
    x: res.x, y: res.y, s: res.s, t: res.t, rows, mapping,
    filename: 'similarity-tree',
    onPick: id => state.onOpenDesign && state.onOpenDesign(id),
  });
  renderLegend(mapping);
  note('noteTmap', 'Branch length reflects sequence fingerprint distance, not score. '
    + 'Drag to pan, scroll to zoom, click a node to open that design.');
}

/* Least-squares fit, kept here so the agreement panel can label its r. */
function linearFit(points) {
  const pts = points.filter(p => isFinite(p.x) && isFinite(p.y));
  const n = pts.length;
  if (n < 2) return null;
  const sx = pts.reduce((s, p) => s + p.x, 0);
  const sy = pts.reduce((s, p) => s + p.y, 0);
  const sxx = pts.reduce((s, p) => s + p.x * p.x, 0);
  const syy = pts.reduce((s, p) => s + p.y * p.y, 0);
  const sxy = pts.reduce((s, p) => s + p.x * p.y, 0);
  const denom = n * sxx - sx * sx;
  if (!denom) return null;
  const slope = (n * sxy - sx * sy) / denom;
  const intercept = (sy - slope * sx) / n;
  const r = (n * sxy - sx * sy)
    / (Math.sqrt(denom) * Math.sqrt(n * syy - sy * sy) || 1);
  return { slope, intercept, r };
}

/* ------------------------------------------------------------------ load */

export async function load(jobId) {
  state.jobId = jobId;
  const sub = document.getElementById('analyticsSub');

  if (!jobId) {
    if (sub) sub.textContent = 'No job selected.';
    clearAll('Select a job above.');
    return;
  }
  if (!isAvailable()) {
    clearAll('The chart library did not load.');
    note('noteEvolution', 'static/js/plotly.min.js is missing or failed to load; '
      + 'the panels cannot draw.');
    return;
  }
  if (state.loading) return;
  state.loading = true;
  state.data = null;
  state.tmap = null;
  state.rowsById = new Map();
  clearAll('Loading…');
  if (sub) sub.innerHTML = 'Job <span class="mono">' + esc(jobId) + '</span> · loading…';

  // The tree is a separate, slower call (RDKit + LSH forest), so it must not
  // hold up the other panels. But its metadata is also the only per-design
  // table this view has, and the 3D panels colour from it -- so the 3D panels
  // are drawn after it arrives, and the 2D ones immediately.
  const main = api.analytics(jobId);
  const tree = api.tmap(jobId).catch(e => ({ success: false, error: e.message }));

  try {
    const data = await main;
    if (!data || !data.success) {
      throw new Error((data && data.error) || 'No analytics for this job.');
    }
    state.data = data;
    if (sub) {
      sub.innerHTML = 'Job <span class="mono">' + esc(jobId) + '</span> · '
        + esc(data.design_count ?? '?') + ' designs'
        + (data.iteration_count ? ' across ' + esc(data.iteration_count) + ' iterations' : '');
    }
    // Each panel is drawn in isolation. One figure that throws -- a chart
    // library can reject data the others accept -- must not blank the six that
    // worked, and the failing panel has to name itself. The previous version
    // caught at this level and called clearAll(), so a single Plotly error
    // replaced every panel with the same message and hid which one broke.
    consoleUI.strip(buildStrip());
    panel('chartEvolution', 'noteEvolution', () => drawEvolution(data.evolution));
    panel('chartAgreement', 'noteAgreement', () => drawAgreement(data.agreement));
    panel('chartHeatmap', 'noteHeatmap', () => drawHeatmap(data.correlation));
    panel('chartPca', 'notePca', () => drawPca(data.pca));
    panel('chartLogo', null, () => drawLogo(data.logo));
  } catch (e) {
    // Only reached if the request itself failed, not a drawing error.
    clearAll('Could not load analytics: ' + e.message);
    if (sub) sub.innerHTML = 'Job <span class="mono">' + esc(jobId) + '</span>';
    state.loading = false;
    return;
  }
  state.loading = false;

  const res = await tree;

  // Index the per-design rows, then decide which colour-by fields this job can
  // actually offer -- including one per peptide position, built from the data.
  const rows = (res && res.metadata) || [];
  state.rowsById = new Map(rows.map(r => [idKey(r.pdbId), r]));
  // Build the candidate set, then keep only the fields this job can populate --
  // so an older job offers fewer options rather than a dropdown full of
  // attributes that colour everything grey.
  const template = (state.data.template && state.data.template.residues) || [];
  const candidates = COLOUR_FIELDS
    .concat(rosettaTermFields(rows))
    .concat(similarityFields(template))
    .concat(rdkitFields(state.data.descriptors, idKey))
    .concat(residuePositionFields(rows));
  state.fields = candidates
    .map(f => ({ f, m: colourMapping(f, rows) }))
    .filter(({ m }) => m.kind !== 'none')
    .map(({ f }) => f);
  if (!state.fields.some(f => f.key === state.colourKey) && state.fields.length) {
    state.colourKey = state.fields[0].key;
  }
  renderColourControl();

  panel('chartTmap', 'noteTmap', () => drawTmap(res));
  panel('chartPca3d', 'notePca3d', () => drawPca3d(state.data.pca));
  panel('chartSeqmap', 'noteSeqmap', () => drawSeqmap(state.data.seqmap));

  // The tree arrives last and the strip and the open explanation both read
  // from it, so refresh once everything is in rather than per panel.
  consoleUI.strip(buildStrip());
  consoleUI.refresh();
}

async function fillJobs() {
  const sel = document.getElementById('analyticsJob');
  if (!sel) return;
  try {
    const res = await api.jobs();
    const done = (res.jobs || []).filter(j =>
      ['Complete', 'Partial'].includes(j.partial ? 'Partial' : j.status));
    sel.innerHTML = '<option value="">Select a finished job…</option>'
      + done.map(j =>
        `<option value="${esc(j.job_id)}">${esc(j.protein_symbol)} · ${esc(j.job_id)}</option>`
      ).join('');
    if (state.jobId) sel.value = state.jobId;
  } catch { /* leave as-is */ }
}

export function setOpenDesign(fn) { state.onOpenDesign = fn; }

export function init(ctx) {
  /* The console layout: rail, focus slot, explain panel. It only moves figure
     nodes -- every draw function above still targets the same ids -- so a
     failure here leaves the figures stacked in #figPool rather than blank. */
  try {
    consoleUI.init({ conclusion: conclusionFor });
  } catch (e) {
    console.error('[analytics] console layout failed to initialise:', e);
  }
  // Clicking a point in any map opens that design in Results, with its
  // structure loaded. Routed through app.js so Analytics does not need to know
  // how the Results view selects a row.
  if (ctx && typeof ctx.openDesign === 'function') {
    state.onOpenDesign = id => ctx.openDesign(state.jobId, id);
  }
  document.getElementById('analyticsJob')?.addEventListener('change', e => load(e.target.value));
  document.getElementById('colourBy')?.addEventListener('change', e => {
    state.colourKey = e.target.value;
    applyColour();
  });
  clearAll('Select a job above.');
  fillJobs();
}

export function show() { fillJobs(); }
export function hide() {}
export function currentJob() { return state.jobId; }
