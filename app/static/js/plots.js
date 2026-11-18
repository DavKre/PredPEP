// plots.js — interactive chart layer over Plotly.
//
// WHY A WRAPPER RATHER THAN CALLING PLOTLY DIRECTLY
// -------------------------------------------------
// Three things have to be true of every panel, and none of them are Plotly
// defaults:
//
//   1. It has to look like the rest of the dashboard. Colours, fonts and grid
//      lines are read from the CSS custom properties in style.css at render
//      time, so a theme change (or a dark-mode switch) is picked up without
//      duplicating the palette in JavaScript.
//   2. It has to recolour without refetching. Changing "colour by" on a plot of
//      800 designs must not re-request the job's analytics, so colour mapping is
//      computed here from metadata the panel already holds, and applied with
//      Plotly.restyle rather than a full redraw.
//   3. It has to be honest about what is plotted. Hover shows the design's own
//      identity, not just coordinates, because a point you cannot name is not
//      something you can act on.
//
// Plotly is loaded as a plain script (window.Plotly), not an ES module, because
// this project has no build step. Every function here no-ops safely if it is
// missing, so a failed script load degrades the panels instead of breaking the
// page.

/* -------------------------------------------------------------------- theme */

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v && v.trim()) || fallback;
}

/* Read once per render, so a dark-mode flip is reflected on the next draw. */
export function theme() {
  return {
    ink: cssVar('--ink', '#1a1a1a'),
    muted: cssVar('--muted', '#6b7280'),
    line: cssVar('--line', '#e5e7eb'),
    surface: cssVar('--surface', '#ffffff'),
    accent: cssVar('--accent', '#2563eb'),
    warn: cssVar('--warn-ink', '#b45309'),
    ok: cssVar('--ok-ink', '#047857'),
    font: cssVar('--font-sans',
      'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif'),
    mono: cssVar('--font-mono', 'ui-monospace, SFMono-Regular, Menlo, monospace'),
  };
}

/* Both scales are defined in style.css (--ramp-div, --ramp-seq) so the theme
   owns them. The dark theme reverses their LIGHTNESS direction on purpose: on
   white the salient end is dark, on a dark panel it has to be the bright end,
   or the best scores and the strongest correlations recede into the
   background while the dullest values glow. Hue families are unchanged.

   Falls back to the original light-theme literals if the token is missing. */

function rampFrom(token, fallback) {
  const raw = cssVar(token, '');
  const parts = raw.split(',').map(x => x.trim()).filter(Boolean);
  const cols = parts.length >= 2 ? parts : fallback;
  const last = cols.length - 1;
  return cols.map((c, i) => [i / last, c]);
}

const DIVERGING_FALLBACK = ['#b45309', '#d99a4e', '#f3f4f6', '#6aa0e8', '#1d4ed8'];
const SEQUENTIAL_FALLBACK = ['#0b2f6b', '#1d4ed8', '#6aa0e8', '#b9cdf2', '#e8eefb'];

/* Read at call time, not at import, so a theme flip lands on the next draw. */
const DIVERGING = () => rampFrom('--ramp-div', DIVERGING_FALLBACK);
/* Note the token: --ramp-seq-plotly, NOT --ramp-seq. A Plotly colorscale
   maps position 0.0 to zmin (the lowest value, the BEST ddG), while
   charts.js maps its index 0 to the highest value. The two ramps are stored
   reversed from each other for that reason; style.css says so at both. */
const SEQUENTIAL = () => rampFrom('--ramp-seq-plotly', SEQUENTIAL_FALLBACK);

/* Categorical palette, owned by style.css (--series-1..5) so the theme sets
   it and one validated set serves all four themes.

   FIVE, not the eight this used to carry. The old list was never checked for
   colour-vision separation, and eight categories cannot be told apart anyway:
   across every pair -- which is what a scatter shows at once, not just
   neighbours in a legend -- even a well-chosen SIX puts green against magenta
   at deuteranopia dE 2.9, which is to say identical. Overflow folds into one
   grey bucket instead of reusing a hue, and ordered fields use the sequential
   ramp, where lightness carries the order. */
const CATEGORICAL_FALLBACK = ['#0E93B2', '#DE2E86', '#BE8A10', '#8260E6', '#20A070',
                             '#90D800'];

/* SIX, not five. The sixth was added so iteration -- which runs to
   PREDPEP_MAX_ITERATIONS, 6 by default -- can be categorical without the last
   and most interesting round folding into the grey overflow bucket.

   The separation claim above was measured rather than argued, with CIEDE2000
   over Vienot dichromat projections, worst pair across the whole set:

                    normal  deuter.  protan.  tritan.
     the five        26.0     5.0      5.0      1.3
     with #90D800    26.0     5.0      5.0      1.3

   Adding the sixth costs nothing, because the worst pairs were already set by
   the five: magenta against green at dE 5.0 for deuteranopes, cyan against
   green at 1.3 for tritanopes. "Six hues cannot be told apart" is true, but it
   is equally true of the five, so it was never a reason to keep iteration off
   this palette. What it IS a reason for: colour alone does not separate these
   categories for a colour-blind reader at any size of palette, which is why
   the legend lists every value and hover names it. */
function categorical() {
  const out = [];
  for (let i = 1; i <= 8; i++) {
    const v = cssVar('--series-' + i, '');
    if (v) out.push(v);
  }
  return out.length ? out : CATEGORICAL_FALLBACK;
}

function available() {
  return typeof window !== 'undefined' && window.Plotly;
}

function baseLayout(t, extra) {
  return Object.assign({
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { family: t.font, size: 11, color: t.muted },
    margin: { l: 52, r: 16, t: 16, b: 44 },
    hoverlabel: {
      bgcolor: t.surface, bordercolor: t.line,
      font: { family: t.mono, size: 11, color: t.ink },
      align: 'left',
    },
    showlegend: false,
    dragmode: 'zoom',
  }, extra || {});
}

function axis(t, title, extra) {
  return Object.assign({
    title: title ? { text: title, font: { size: 11, color: t.muted } } : undefined,
    gridcolor: t.line,
    zerolinecolor: t.line,
    linecolor: t.line,
    tickfont: { size: 10, color: t.muted },
    automargin: true,
  }, extra || {});
}

/* The mode bar is kept but trimmed: zoom, pan, box/lasso select and reset are
   the ones that matter here. Image export is left in deliberately -- a figure
   headed for a slide should not need a screenshot. */
function config(filename) {
  return {
    displaylogo: false,
    responsive: true,
    toImageButtonOptions: { format: 'png', filename: filename || 'predpep', scale: 2 },
    modeBarButtonsToRemove: ['sendDataToCloud', 'hoverClosestCartesian',
                             'hoverCompareCartesian', 'toggleSpikelines'],
  };
}

function host(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  el.classList.remove('empty');
  return el;
}

/* Draw, tolerating a container that has no box yet.

   Plotly measures the container during its auto-margin pass. If the element has
   zero width or height it throws from deep inside that pass -- "can't access
   property _redrawFromAutoMarginCount" -- and the error surfaces as an
   unhandled rejection with no useful attribution. Two ways that happens here:

     * an empty div with no CSS height is 0 px tall (the panels now carry an
       explicit height, which is the real fix);
     * the Analytics view is still display:none when the first draw runs,
       because the router can load a job before the section is shown.

   So: draw immediately when the element has a box, otherwise watch it and draw
   once it gets one. newPlot rather than react on first use, because react
   expects a div Plotly already owns and these start out holding a placeholder. */
function draw(el, traces, layout, cfg) {
  const go = () => {
    try {
      if (!el._fullLayout) {
        el.innerHTML = '';
        window.Plotly.newPlot(el, traces, layout, cfg);
      } else {
        window.Plotly.react(el, traces, layout, cfg);
      }
    } catch (e) {
      console.error('[plots] draw failed', el.id, e);
      // Plotly can leave state on the div even when the draw threw. Purge it,
      // or the next call takes the react branch against a div Plotly no longer
      // owns -- a second failure stacked on the first.
      try { window.Plotly.purge(el); } catch { /* ignore */ }
      el.innerHTML = '<p class="empty">This chart could not be drawn: '
        + String((e && e.message) || e).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        + '</p>';
    }
  };

  /* Plotly's auto-margin pass divides the axis range by the plot area in
     pixels. When the container is smaller than the margins this layout asks
     for, that area is zero or negative, the scale factor comes out non-finite,
     and Plotly throws "Something went wrong with axis scaling".

     That is what the term-correlation heatmap did on every first paint. Its
     margins are l:110 b:110 to fit the rotated term labels, against l:52 b:44
     from baseLayout for every other panel -- so it is the only figure whose
     margins can exceed a panel that is still settling. Switching away and back
     "fixed" it because by then the panel was at full size.

     So readiness is "has a box big enough for the margins", derived from the
     layout rather than hardcoded, plus 24px of actual plot area. The observer
     below already re-tests, so a panel that starts too small draws once it
     reaches full size instead of failing permanently. */
  const _m = (layout && layout.margin) || {};
  const _needW = (_m.l || 0) + (_m.r || 0) + 24;
  const _needH = (_m.t || 0) + (_m.b || 0) + 24;
  const ready = () => el.offsetWidth >= _needW && el.offsetHeight >= _needH;

  if (ready()) { go(); return; }

  if (el._predpepRO) { try { el._predpepRO.disconnect(); } catch { /* ignore */ } }
  if (typeof ResizeObserver === 'undefined') {
    // No observer: try once on the next frame and give up quietly after that.
    requestAnimationFrame(() => {
      if (ready()) go();
    });
    return;
  }
  const ro = new ResizeObserver(() => {
    if (ready()) {
      ro.disconnect();
      el._predpepRO = null;
      go();
    }
  });
  ro.observe(el);
  el._predpepRO = ro;
}

export function clear(id) {
  const el = document.getElementById(id);
  if (el && available()) { try { window.Plotly.purge(el); } catch { /* ignore */ } }
  if (el) el.innerHTML = '';
}

/* ------------------------------------------------------------- colour-by */

/* Field registry for "colour by". Each entry turns a metadata row into either a
   number (continuous) or a string (categorical).

   `residueAt` is generated per position when a peptide length is known, so a
   user can ask "which residue sits at position 3" and see the map split by it.
   That is the question a position-preference logo cannot answer, because the
   logo aggregates away which DESIGN each residue came from. */
export const COLOUR_FIELDS = [
  { key: 'foldx', label: 'FoldX ΔΔG', kind: 'num', group: 'Scores',
    get: r => num(r.FoldX_Score) },
  { key: 'rosetta', label: 'Rosetta total', kind: 'num', group: 'Scores',
    get: r => num(r.Rosetta_Total_Score) },
  /* ORDINAL: categorical while the iterations fit the palette, the sequential
     ramp when they do not.

     This was numeric-only, on the argument that six distinguishable hues do
     not exist. Measurement (see CATEGORICAL above) showed the five already in
     use fail that test just as badly, so the argument ruled out nothing it was
     not already allowing. What a lightness ramp actually costs is the question
     people bring to these plots: "where did iteration 5 land?" is answered at
     a glance by a hue and by squinting at adjacent shades otherwise, and the
     iteration-by-iteration distribution is the whole reason to colour by it.

     Order still matters, so the ordinal path assigns colours by iteration
     NUMBER rather than by group size, which is what the categorical path does
     for every other field. Past the palette it falls back to the ramp: greying
     out the largest-numbered iterations would hide the end of the search. */
  { key: 'iteration', label: 'Iteration', kind: 'cat', ordinal: true, group: 'Run',
    fmt: v => 'iteration ' + v,
    get: r => { const i = iterationOf(r); return i == null ? null : Number(i); } },
  { key: 'disulfide', label: 'Disulfide', kind: 'cat', group: 'Run',
    get: r => tri(r.disulfide_ok ?? r.disulfide_intact,
                  'Intact', 'Broken', 'Not evaluated') },
  { key: 'filters', label: 'Submitted filters', kind: 'cat', group: 'Run',
    get: r => tri(r.passes_filters, 'Passes', 'Excluded', 'Not evaluated') },
  { key: 'length', label: 'Peptide length', kind: 'cat', group: 'Run',
    get: r => { const n = residuesOf(r).length; return n ? `${n} residues` : null; } },

  // --- physicochemical, from the residue tables above.
  { key: 'gravy', label: 'GRAVY (hydropathy)', kind: 'num', group: 'Composition',
    get: r => gravyOf(r) },
  { key: 'charge', label: 'Net charge (pH 7)', kind: 'num', group: 'Composition',
    get: r => resSum(r, CHARGE) },
  { key: 'hbd', label: 'H-bond donors (side chain)', kind: 'num', group: 'Composition',
    get: r => resSum(r, SC_DONORS) },
  { key: 'hba', label: 'H-bond acceptors (side chain)', kind: 'num', group: 'Composition',
    get: r => resSum(r, SC_ACCEPTORS) },
  { key: 'hbtot', label: 'H-bond capacity (D+A)', kind: 'num', group: 'Composition',
    get: r => {
      const d = resSum(r, SC_DONORS), a = resSum(r, SC_ACCEPTORS);
      return (d == null || a == null) ? null : d + a;
    } },
  { key: 'aromatic', label: 'Aromatic fraction (F/W/Y)', kind: 'num', group: 'Composition',
    get: r => resFraction(r, c => AROMATIC.has(c)) },
  { key: 'hydrophobic', label: 'Hydrophobic fraction', kind: 'num', group: 'Composition',
    get: r => resFraction(r, c => (KD[c] ?? -99) > 0) },
  { key: 'mw', label: 'Molecular weight', kind: 'num', group: 'Composition',
    get: r => { const s = resSum(r, RES_MASS); return s == null ? null : s + 18.02; } },
  { key: 'pro', label: 'Proline count (rigidity)', kind: 'num', group: 'Composition',
    get: r => { const res = residuesOf(r);
                return res.length ? res.filter(c => String(c).toUpperCase() === 'PRO').length : null; } },
  { key: 'gly', label: 'Glycine count (flexibility)', kind: 'num', group: 'Composition',
    get: r => { const res = residuesOf(r);
                return res.length ? res.filter(c => String(c).toUpperCase() === 'GLY').length : null; } },
  { key: 'cys', label: 'Cysteine count', kind: 'num', group: 'Composition',
    get: r => { const res = residuesOf(r);
                return res.length ? res.filter(c => String(c).toUpperCase() === 'CYS').length : null; } },
  { key: 'ncaa', label: 'Non-canonical residues', kind: 'num', group: 'Composition',
    get: r => { const res = residuesOf(r);
                return res.length ? res.filter(c => KD[String(c).toUpperCase()] === undefined).length : null; } },
];

/* Rosetta per-term energies, offered only for the terms the score table
   actually carries. run_mergeScores.py keeps total_score plus whichever of
   these it finds in non_mc_pdbs.csv, so an older job simply shows fewer
   options rather than a list of empty ones.

   The rationale matters when reading the map: a design with a good total but a
   high fa_rep is strained rather than well packed, and fa_dun says whether a
   side chain sits in a rotamer it would plausibly adopt. Colouring by those
   separates "scores well" from "scores well for a good reason". */
const ROSETTA_TERMS = [
  // The two composite measures of binding: what you reach for first.
  ['interface_score', 'Interface energy'],
  ['binding_score', 'Binding energy'],
  // Specific interactions across the interface.
  ['hbond_sc', 'Side-chain H-bonds'],
  ['hbond_bb_sc', 'Backbone–side-chain H-bonds'],
  ['hbond_lr_bb', 'Long-range backbone H-bonds'],
  ['hbond_sr_bb', 'Short-range backbone H-bonds'],
  ['fa_pair', 'Residue pair interaction (fa_pair)'],
  ['fa_elec', 'Electrostatics (fa_elec)'],
  // Packing and solvation.
  ['fa_atr', 'Packing contact (fa_atr)'],
  ['fa_rep', 'Steric repulsion (fa_rep)'],
  ['fa_sol', 'Desolvation (fa_sol)'],
  ['fa_intra_rep', 'Intra-residue repulsion'],
  // Internal strain: whether a good total is bought with a bad conformation.
  ['fa_dun', 'Rotamer strain (fa_dun)'],
  ['p_aa_pp', 'Backbone propensity (p_aa_pp)'],
  ['rama', 'Ramachandran (rama)'],
  ['omega', 'Omega torsion'],
  ['pro_close', 'Proline closure'],
  // Disulfides: the pre-Talaris score function emits dslf_ss_dst / dslf_ss_dih
  // rather than the dslf_fa13 of later functions, so both spellings are listed
  // and only the ones present in the table are offered.
  ['dslf_ss_dst', 'Disulfide distance'],
  ['dslf_ss_dih', 'Disulfide dihedral'],
  ['dslf_ca_dih', 'Disulfide Cα dihedral'],
  ['dslf_fa13', 'Disulfide energy (dslf_fa13)'],
  ['ref', 'Reference energy'],
];

export function rosettaTermFields(rows) {
  const out = [];
  for (const [col, label] of ROSETTA_TERMS) {
    if (!rows.some(r => num(r[col]) != null)) continue;
    out.push({ key: `rt_${col}`, label, kind: 'num', group: 'Rosetta terms',
               get: r => num(r[col]) });
  }
  return out;
}

/* Similarity to the template peptide the job started from.

   Compared position by position on THREE-LETTER codes, so a non-canonical
   substitution counts as a substitution rather than matching a native residue
   that merely shares a one-letter code. Designs of a different length to the
   template return null instead of being compared over the overlap, which
   would understate the difference. */
export function similarityFields(templateResidues) {
  const tpl = (templateResidues || []).map(c => String(c).toUpperCase());
  if (tpl.length < 2) return [];
  const subsOf = r => {
    const res = residuesOf(r).map(c => String(c).toUpperCase());
    if (res.length !== tpl.length) return null;
    let n = 0;
    for (let i = 0; i < tpl.length; i++) if (res[i] !== tpl[i]) n++;
    return n;
  };
  return [
    { key: 'sim_identity', label: 'Identity to template (%)', kind: 'num',
      group: 'Similarity',
      get: r => { const s = subsOf(r);
                  return s == null ? null : 100 * (tpl.length - s) / tpl.length; } },
    { key: 'sim_subs', label: 'Substitutions from template', kind: 'num',
      group: 'Similarity', get: r => subsOf(r) },
  ];
}

/* Descriptors computed server-side with RDKit, supplied as {pdbId: {...}}.
   Offered only for the keys actually present, and empty for any design whose
   sequence RDKit could not build into a molecule. */
const RDKIT_DESCRIPTORS = [
  ['tpsa', 'Polar surface area (TPSA)'],
  ['logp', 'logP'],
  ['mol_hbd', 'H-bond donors (molecule)'],
  ['mol_hba', 'H-bond acceptors (molecule)'],
  ['rotatable', 'Rotatable bonds'],
];

export function rdkitFields(byId, keyOf) {
  if (!byId) return [];
  const out = [];
  for (const [k, label] of RDKIT_DESCRIPTORS) {
    let any = false;
    for (const v of Object.values(byId)) {
      if (v && num(v[k]) != null) { any = true; break; }
    }
    if (!any) continue;
    out.push({
      key: `rd_${k}`, label, kind: 'num', group: 'RDKit descriptors',
      get: r => {
        const d = byId[keyOf(r.pdbId || r.id || '')];
        return d ? num(d[k]) : null;
      },
    });
  }
  return out;
}

function num(v) {
  const f = parseFloat(v);
  return isFinite(f) ? f : null;
}

function tri(v, yes, no, unknown) {
  const s = String(v ?? '').trim().toLowerCase();
  if (s === 'true') return yes;
  if (s === 'false') return no;
  return unknown;
}

function iterationOf(r) {
  if (r.iteration != null && r.iteration !== '') {
    const i = parseInt(r.iteration, 10);
    if (isFinite(i)) return i;
  }
  const m = /_iter(\d+)_/.exec(String(r.pdb_relative_path || ''));
  return m ? parseInt(m[1], 10) : null;
}

/* Three-letter codes preferred: the one-letter column collapses every
   non-canonical to X, which would merge residues that are chemically unrelated. */
export function residuesOf(r) {
  const three = String(r.pepResidues || '').trim();
  if (three) return three.split(/\s+/);
  const one = String(r.pepSeq || '').trim();
  return one ? one.split('') : [];
}

const KD = { ALA: 1.8, ARG: -4.5, ASN: -3.5, ASP: -3.5, CYS: 2.5, GLN: -3.5,
             GLU: -3.5, GLY: -0.4, HIS: -3.2, ILE: 4.5, LEU: 3.8, LYS: -3.9,
             MET: 1.9, PHE: 2.8, PRO: -1.6, SER: -0.8, THR: -0.7, TRP: -0.9,
             TYR: -1.3, VAL: 4.2 };

/* ------------------------------------------------ residue property tables

   CONVENTIONS ARE STATED EXPLICITLY because different tools count these
   differently, and a descriptor is only comparable if you know its definition:

   * H-bond donors / acceptors count SIDE-CHAIN heavy atoms capable of the
     interaction, not hydrogens, and exclude the backbone (every residue
     contributes the same backbone N-H and C=O, so including them would add a
     constant and tell you nothing about the design). Sulfur is not counted:
     Cys SG and Met SD are weak and inconsistently treated elsewhere.
   * Charge is the nominal value at pH 7: Asp/Glu -1, Lys/Arg +1, His +0.1 to
     reflect partial protonation near neutral pH rather than pretending it is
     a full charge or none.
   * "Aromatic" is Phe/Trp/Tyr. His is excluded: it is aromatic in the chemical
     sense but behaves differently at an interface, and lumping it in would
     blur the hot-spot signal these three carry.
   * "Hydrophobic" is Kyte-Doolittle > 0, so the set follows from the same
     scale GRAVY uses instead of a second, inconsistent definition.
   * Residue masses are the residue contribution (free amino acid minus water);
     peptide MW adds one water for the terminal groups.

   Every table covers the 20 canonical residues only. A design containing a
   non-canonical returns null for these -- see hasUnknownResidue below -- rather
   than a figure computed over the residues that happen to be recognised, which
   would be confidently wrong. Supplying per-residue values for your
   non-canonicals would extend all of them at once. */

const SC_DONORS = { ARG: 3, LYS: 1, ASN: 1, GLN: 1, HIS: 1, SER: 1, THR: 1,
                    TYR: 1, TRP: 1, ALA: 0, ASP: 0, CYS: 0, GLU: 0, GLY: 0,
                    ILE: 0, LEU: 0, MET: 0, PHE: 0, PRO: 0, VAL: 0 };

const SC_ACCEPTORS = { ASP: 2, GLU: 2, ASN: 1, GLN: 1, HIS: 1, SER: 1, THR: 1,
                       TYR: 1, ALA: 0, ARG: 0, CYS: 0, GLY: 0, ILE: 0, LEU: 0,
                       LYS: 0, MET: 0, PHE: 0, PRO: 0, TRP: 0, VAL: 0 };

const CHARGE = { ASP: -1, GLU: -1, LYS: 1, ARG: 1, HIS: 0.1, ALA: 0, ASN: 0,
                 CYS: 0, GLN: 0, GLY: 0, ILE: 0, LEU: 0, MET: 0, PHE: 0,
                 PRO: 0, SER: 0, THR: 0, TRP: 0, TYR: 0, VAL: 0 };

const RES_MASS = { ALA: 71.08, ARG: 156.19, ASN: 114.10, ASP: 115.09,
                   CYS: 103.14, GLN: 128.13, GLU: 129.12, GLY: 57.05,
                   HIS: 137.14, ILE: 113.16, LEU: 113.16, LYS: 128.17,
                   MET: 131.19, PHE: 147.18, PRO: 97.12, SER: 87.08,
                   THR: 101.10, TRP: 186.21, TYR: 163.18, VAL: 99.13 };

const AROMATIC = new Set(['PHE', 'TRP', 'TYR']);

/* True when any residue is outside the canonical twenty, so a descriptor that
   depends on per-residue values must decline rather than approximate. */
function hasUnknownResidue(res, table) {
  return res.some(c => table[String(c).toUpperCase()] === undefined);
}

/* Sum a residue table over a design, or null if any residue is unknown. */
function resSum(r, table) {
  const res = residuesOf(r);
  if (!res.length || hasUnknownResidue(res, table)) return null;
  return res.reduce((s, c) => s + table[String(c).toUpperCase()], 0);
}

function resFraction(r, predicate) {
  const res = residuesOf(r);
  if (!res.length) return null;
  // A fraction over a set membership test is safe for non-canonicals: an
  // unrecognised residue is simply not a member, which is correct rather than
  // unknown.
  return res.filter(c => predicate(String(c).toUpperCase())).length / res.length;
}

/* Null rather than a guess when any residue has no published value -- a
   non-canonical has no Kyte-Doolittle index, and averaging over the ones we do
   know would quietly misstate the hydropathy. */
function gravyOf(r) {
  const res = residuesOf(r);
  if (!res.length) return null;
  let sum = 0;
  for (const code of res) {
    const v = KD[String(code).toUpperCase()];
    if (v === undefined) return null;
    sum += v;
  }
  return sum / res.length;
}

/* Extra colour fields for "residue at position N", built from the data itself. */
export function residuePositionFields(rows) {
  const maxLen = rows.reduce((m, r) => Math.max(m, residuesOf(r).length), 0);
  const out = [];
  for (let p = 1; p <= maxLen; p++) {
    out.push({
      key: `pos${p}`, label: `Residue at ${p}`, kind: 'cat', group: 'By position',
      get: r => residuesOf(r)[p - 1] || null,
    });
  }
  return out;
}

/* Turn a field + rows into everything a trace needs to be coloured, plus a
   legend description the panel can render. */
export function colourMapping(field, rows) {
  const raw = rows.map(r => { try { return field.get(r); } catch { return null; } });

  if (field.kind === 'num') {
    const vals = raw.filter(v => v != null);
    if (!vals.length) {
      return { kind: 'none', note: `No ${field.label} values in this job.` };
    }
    const lo = Math.min(...vals), hi = Math.max(...vals);
    return {
      kind: 'num',
      values: raw.map(v => (v == null ? null : v)),
      colorscale: SEQUENTIAL(),
      cmin: lo, cmax: hi,
      colorbarTitle: field.label,
      note: `${field.label}: ${lo.toFixed(2)} to ${hi.toFixed(2)}` +
            (vals.length < raw.length
              ? ` · ${raw.length - vals.length} design(s) have no value and are grey`
              : ''),
      legend: null,
    };
  }

  // Categorical. Ordered by frequency so the biggest group takes the first
  // colour, and capped so a high-cardinality field (residue at a position, with
  // non-canonicals) does not produce an unreadable legend.
  const counts = new Map();
  for (const v of raw) {
    if (v == null) continue;
    counts.set(v, (counts.get(v) || 0) + 1);
  }
  if (!counts.size) {
    return { kind: 'none', note: `No ${field.label} values in this job.` };
  }
  const CAT = categorical();

  /* An ORDINAL field is categorical only while its values fit the palette.
     Two differences from the ordinary categorical path:

     - colours go by VALUE, not by group size. For iteration, "biggest group
       takes the first colour" would scatter the hues across the rounds and
       make the legend say nothing about order.
     - past the palette it hands over to the sequential ramp rather than
       folding the overflow into grey. Overflow is taken from the END of the
       sort, so a job with more iterations than colours would grey out its
       latest rounds -- the ones the search actually arrived at. */
  const ordered = field.ordinal
    ? (counts.size > CAT.length
        ? null
        : [...counts.entries()].sort((a, b) => Number(a[0]) - Number(b[0])))
    : [...counts.entries()].sort((a, b) => b[1] - a[1]);
  if (ordered === null) {
    const m = colourMapping(Object.assign({}, field, { kind: 'num', ordinal: false }), rows);
    if (m.kind === 'num') {
      m.note += ` · ${counts.size} iterations is more than the ${CAT.length} `
              + 'distinct colours available, so order is shown by shade';
    }
    return m;
  }
  const shown = ordered.slice(0, CAT.length);
  const rest = ordered.slice(CAT.length);
  const colourOf = new Map(shown.map(([k], i) => [k, CAT[i]]));
  const OTHER = cssVar('--series-other', '#9ca3af');

  return {
    kind: 'cat',
    values: raw,
    colours: raw.map(v => (v == null ? OTHER : (colourOf.get(v) || OTHER))),
    legend: shown.map(([k, n], i) => ({
      label: field.fmt ? field.fmt(k) : k, colour: CAT[i], count: n }))
      .concat(rest.length
        ? [{ label: `other (${rest.length} value${rest.length > 1 ? 's' : ''})`,
             colour: OTHER, count: rest.reduce((s, [, n]) => s + n, 0) }]
        : []),
    note: `${field.label} · ${counts.size} distinct value(s)`,
  };
}

/* Hover text: identity first. */
export function hoverFor(rows, extraLines) {
  return rows.map((r, i) => {
    const res = residuesOf(r);
    const lines = [
      `<b>${String(r.pdbId || r.id || '—')}</b>`,
      res.length ? res.join(' ') : '',
    ];
    const fx = num(r.FoldX_Score), rs = num(r.Rosetta_Total_Score);
    if (fx != null) lines.push(`FoldX ΔΔG ${fx.toFixed(2)}`);
    if (rs != null) lines.push(`Rosetta ${rs.toFixed(1)}`);
    const it = iterationOf(r);
    if (it != null) lines.push(`iteration ${it}`);
    if (extraLines && extraLines[i]) lines.push(extraLines[i]);
    return lines.filter(Boolean).join('<br>');
  });
}

/* ------------------------------------------------------------------ charts */

export function renderLine(id, { series, xTitle, yTitle, filename }) {
  if (!available()) return;
  const el = host(id);
  if (!el) return;
  const t = theme();
  const traces = (series || []).map((s, i) => ({
    type: 'scatter', mode: s.mode || 'lines+markers',
    name: s.name || `series ${i + 1}`,
    x: s.x, y: s.y,
    /* No modulo. Cycling a palette silently gives two series the same colour
       and nothing on the chart says so; past the palette they go grey, which
       is at least visibly "unassigned". Line charts here carry two series. */
    line: { color: s.colour || categorical()[i] || cssVar('--series-other', '#8A93A0'), width: 2,
            dash: s.dash || 'solid' },
    marker: { size: 6 },
    hovertemplate: `${s.name || ''} %{y:.2f} at %{x}<extra></extra>`,
  }));
  draw(el, traces, baseLayout(t, {
    xaxis: axis(t, xTitle), yaxis: axis(t, yTitle),
    showlegend: traces.length > 1,
    legend: { orientation: 'h', y: 1.12, x: 0, font: { size: 10 } },
  }), config(filename));
}

export function renderScatter(id, {
  x, y, rows, mapping, xTitle, yTitle, fit, filename, onPick,
}) {
  if (!available()) return;
  const el = host(id);
  if (!el) return;
  const t = theme();
  const n = (x || []).length;

  const marker = { size: 6, opacity: 0.8, line: { width: 0 } };
  if (mapping && mapping.kind === 'num') {
    Object.assign(marker, {
      color: mapping.values, colorscale: mapping.colorscale,
      cmin: mapping.cmin, cmax: mapping.cmax,
      colorbar: {
        title: { text: mapping.colorbarTitle, font: { size: 10 } },
        thickness: 10, len: 0.8, outlinewidth: 0,
        tickfont: { size: 9, color: t.muted },
      },
    });
  } else if (mapping && mapping.kind === 'cat') {
    marker.color = mapping.colours;
  } else {
    marker.color = t.accent;
  }

  const traces = [{
    type: n > 600 ? 'scattergl' : 'scatter',
    mode: 'markers', x, y, marker,
    customdata: (rows || []).map(r => String(r.pdbId || r.id || '')),
    text: hoverFor(rows || []),
    hovertemplate: '%{text}<extra></extra>',
  }];

  if (fit && isFinite(fit.slope)) {
    const xs = (x || []).filter(v => isFinite(v));
    if (xs.length) {
      const lo = Math.min(...xs), hi = Math.max(...xs);
      traces.push({
        type: 'scatter', mode: 'lines', hoverinfo: 'skip',
        x: [lo, hi], y: [fit.slope * lo + fit.intercept, fit.slope * hi + fit.intercept],
        line: { color: t.muted, width: 1, dash: 'dash' },
      });
    }
  }

  draw(el, traces, baseLayout(t, {
    xaxis: axis(t, xTitle), yaxis: axis(t, yTitle),
    dragmode: 'select',
  }), config(filename));
  bindPick(el, onPick);
}

/* Below this height the LABELLED heatmap cannot be drawn at all.

   The thumbnail rail puts every figure in a 420x150 box (style.css,
   .fig-thumb__slot > figure). This figure spends l:110 b:110 on the rotated
   term labels and then asks for a colorbar, which Plotly treats as a margin
   pusher -- so at 150px the plot area reaches zero, the axis scale factor goes
   non-finite, and Plotly throws from drawMarginPushers: "Something went wrong
   with axis scaling". The other four figures spend 60px on margins and fit,
   which is why this was the only one that failed.

   Below the threshold the figure draws as what the rail's own design note says
   a thumbnail is for -- shape, not text: no colorbar, no tick labels, 4px
   margins. The figure NODE moves between the thumbnail slot and #figFocus
   rather than being re-created, so a one-shot ResizeObserver re-renders it when
   it reaches the big box and the compact form never follows it there. */
const HEATMAP_COMPACT_MIN = 300;   // measured: labelled form fails at 150,
                                   // draws from 260. 300 leaves slack.

export function renderHeatmap(id, { z, labels, filename }) {
  if (!available()) return;
  const el = host(id);
  if (!el) return;
  const t = theme();
  const small = h => h > 0 && h < HEATMAP_COMPACT_MIN;
  const compact = small(el.offsetHeight);

  // The two layouts differ in margins, ticks and colorbar, and draw() would
  // otherwise hand the change to Plotly.react, which reuses the layout it
  // already computed. Purge so the mode switch is a clean newPlot.
  if (el._predpepHeatCompact !== undefined && el._predpepHeatCompact !== compact) {
    try { window.Plotly.purge(el); } catch { /* ignore */ }
  }
  el._predpepHeatCompact = compact;

  if (el._predpepHeatRO) {
    try { el._predpepHeatRO.disconnect(); } catch { /* ignore */ }
    el._predpepHeatRO = null;
  }
  if (typeof ResizeObserver !== 'undefined') {
    const ro = new ResizeObserver(() => {
      if (el.offsetHeight > 0 && small(el.offsetHeight) !== compact) {
        ro.disconnect();
        el._predpepHeatRO = null;
        // Next frame: the observer fires mid-layout, so the first height that
        // crosses the threshold is not necessarily the final one.
        requestAnimationFrame(() => renderHeatmap(id, { z, labels, filename }));
      }
    });
    el._predpepHeatRO = ro;
    ro.observe(el);
  }

  draw(el, [{
    type: 'heatmap', z, x: labels, y: labels,
    colorscale: DIVERGING(), zmin: -1, zmax: 1, zmid: 0,
    xgap: 1, ygap: 1,
    showscale: !compact,
    colorbar: { thickness: 10, len: 0.8, outlinewidth: 0,
                tickfont: { size: 9, color: t.muted } },
    hovertemplate: '%{y} vs %{x}<br>r = %{z:.2f}<extra></extra>',
  }], baseLayout(t, compact ? {
    xaxis: axis(t, null, {
      showticklabels: false, ticks: '', showgrid: false, automargin: false,
    }),
    yaxis: axis(t, null, {
      showticklabels: false, ticks: '', showgrid: false, automargin: false,
      autorange: 'reversed',
    }),
    margin: { l: 4, r: 4, t: 4, b: 4 },
  } : {
    xaxis: axis(t, null, { tickangle: -45, tickfont: { size: 9 } }),
    yaxis: axis(t, null, { tickfont: { size: 9 }, autorange: 'reversed' }),
    margin: { l: 110, r: 16, t: 16, b: 110 },
  }), config(filename));
}

export function renderScatter3d(id, {
  points, rows, mapping, axisTitles, filename, onPick,
}) {
  if (!available()) return;
  const el = host(id);
  if (!el) return;
  const t = theme();
  const titles = axisTitles || ['PC1', 'PC2', 'PC3'];

  const marker = { size: 3.5, opacity: 0.85, line: { width: 0 } };
  if (mapping && mapping.kind === 'num') {
    Object.assign(marker, {
      color: mapping.values, colorscale: mapping.colorscale,
      cmin: mapping.cmin, cmax: mapping.cmax,
      colorbar: {
        title: { text: mapping.colorbarTitle, font: { size: 10 } },
        thickness: 10, len: 0.7, outlinewidth: 0,
        tickfont: { size: 9, color: t.muted },
      },
    });
  } else if (mapping && mapping.kind === 'cat') {
    marker.color = mapping.colours;
  } else {
    marker.color = t.accent;
  }

  const sceneAxis = title => ({
    title: { text: title, font: { size: 10, color: t.muted } },
    gridcolor: t.line, zerolinecolor: t.line,
    showbackground: false, tickfont: { size: 9, color: t.muted },
  });

  draw(el, [{
    type: 'scatter3d', mode: 'markers',
    x: points.map(p => p.x), y: points.map(p => p.y), z: points.map(p => p.z ?? 0),
    marker,
    customdata: points.map((p, i) => String((rows && rows[i] && rows[i].pdbId) || p.id || '')),
    text: hoverFor(rows && rows.length ? rows : points),
    hovertemplate: '%{text}<extra></extra>',
  }], baseLayout(t, {
    margin: { l: 0, r: 0, t: 0, b: 0 },
    // MUST be set for a 3D scene. baseLayout defaults to 'zoom', which is a
    // valid 3D dragmode meaning "drag to zoom" -- so the plot looked
    // un-rotatable when in fact dragging was bound to the wrong gesture.
    // 'turntable' is Plotly's own 3D default: drag rotates, scroll zooms.
    dragmode: 'turntable',
    scene: {
      xaxis: sceneAxis(titles[0]), yaxis: sceneAxis(titles[1]),
      zaxis: sceneAxis(titles[2]),
      camera: { eye: { x: 1.5, y: 1.5, z: 1.1 } },
      dragmode: 'turntable',
    },
  }), config(filename));
  bindPick(el, onPick);
}

/* TMAP: edges as one trace with null separators, nodes as a second trace that
   carries the colour. Keeping them separate is what lets "colour by" restyle
   only the nodes. */
export function renderTree(id, { x, y, s, t: edgesTo, rows, mapping, filename, onPick }) {
  if (!available()) return;
  const el = host(id);
  if (!el) return;
  const th = theme();

  const ex = [], ey = [];
  for (let i = 0; i < (s || []).length; i++) {
    const a = s[i], b = edgesTo[i];
    if (x[a] == null || x[b] == null) continue;
    ex.push(x[a], x[b], null);
    ey.push(y[a], y[b], null);
  }

  const marker = { size: 5, opacity: 0.9, line: { width: 0 } };
  if (mapping && mapping.kind === 'num') {
    Object.assign(marker, {
      color: mapping.values, colorscale: mapping.colorscale,
      cmin: mapping.cmin, cmax: mapping.cmax,
      colorbar: {
        title: { text: mapping.colorbarTitle, font: { size: 10 } },
        thickness: 10, len: 0.8, outlinewidth: 0,
        tickfont: { size: 9, color: th.muted },
      },
    });
  } else if (mapping && mapping.kind === 'cat') {
    marker.color = mapping.colours;
  } else {
    marker.color = th.accent;
  }

  const traces = [
    { type: 'scattergl', mode: 'lines', x: ex, y: ey, hoverinfo: 'skip',
      line: { color: th.line, width: 1 } },
    { type: 'scattergl', mode: 'markers', x, y, marker,
      customdata: (rows || []).map(r => String(r.pdbId || '')),
      text: hoverFor(rows || []),
      hovertemplate: '%{text}<extra></extra>' },
  ];

  draw(el, traces, baseLayout(th, {
    xaxis: axis(th, null, { showgrid: false, zeroline: false, showticklabels: false }),
    yaxis: axis(th, null, { showgrid: false, zeroline: false, showticklabels: false,
                            scaleanchor: 'x' }),
    dragmode: 'pan',
  }), config(filename));
  bindPick(el, onPick);
}

/* Recolour in place: no refetch, no relayout, keeps the current zoom. */
export function recolour(id, traceIndex, mapping) {
  if (!available()) return;
  const el = document.getElementById(id);
  if (!el || !el.data) return;
  const update = {};
  if (mapping.kind === 'num') {
    update['marker.color'] = [mapping.values];
    update['marker.colorscale'] = [mapping.colorscale];
    update['marker.cmin'] = [mapping.cmin];
    update['marker.cmax'] = [mapping.cmax];
  } else if (mapping.kind === 'cat') {
    update['marker.color'] = [mapping.colours];
  }
  try { window.Plotly.restyle(el, update, [traceIndex]); } catch { /* ignore */ }
}

function bindPick(el, onPick) {
  if (!onPick || !el) return;
  if (el._predpepPick) el.removeAllListeners && el.removeAllListeners('plotly_click');
  el._predpepPick = true;
  el.on('plotly_click', ev => {
    const pt = ev && ev.points && ev.points[0];
    if (pt && pt.customdata) onPick(String(pt.customdata));
  });
}

export function isAvailable() { return !!available(); }
