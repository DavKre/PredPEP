// results.js — Results explorer: score table, filters, linked structure viewer.

import { api, esc, fmtNum } from './api.js';
import { createViewer } from './viewer.js';

const PAGE = 50;

const CANONICAL = new Set(['ALA','ARG','ASN','ASP','CYS','GLN','GLU','GLY','HIS','ILE',
                           'LEU','LYS','MET','PHE','PRO','SER','THR','TRP','TYR','VAL']);

/* Which het codes denote a D residue -- taken from the catalogue, not guessed
   from the string. The two conventions do not share a shape: D-canonicals use
   a PREFIX (DAL, DSE, DHI), D-non-canonicals a SUFFIX (D0A1 -> A1D, D200 ->
   00D). Testing the first character found 30 of 216 and classified the other
   186 as ordinary non-canonicals, so "Has D-residue" under-reported silently.
   Testing the last character is no better: four L residues end in D (APD, AZD,
   BHD, LED). residues.js never had this bug because it is served the catalogue
   and reads entry.hand; only this file and job_analytics.py re-derived the
   rule, because the score CSV does not carry hand. So ask the catalogue. */
const D_HET = new Set();
const L_HET = new Set();   // het codes that are definitely L, e.g. DAB, DPP
let dHetPending = null;

function loadDHetCodes() {
  if (D_HET.size) return Promise.resolve();
  if (dHetPending) return dHetPending;
  dHetPending = api.residues().then(cat => {
    for (const e of (cat && cat.noncanonical) || []) {
      if (!e.pdb_code) continue;
      const het = String(e.pdb_code).toUpperCase();
      if (e.hand === 'D') D_HET.add(het);
      else if (e.hand === 'L') L_HET.add(het);
    }
    // Withheld entries carry no hand and no pdb_code, but they do carry an
    // L category and a code that IS the het code. DAB and DPP live here --
    // held back because the pre-talaris type set has no l-ncaa entries --
    // and their codes start with D, so the prefix test would claim them if
    // PREDPEP_ALLOW_ROSETTA_NCAA ever re-admitted them.
    for (const k of ['unavailable', 'retired', 'collisions', 'pending']) {
      for (const e of (cat && cat[k]) || []) {
        if (e.code && String(e.category || '').startsWith('L')) {
          L_HET.add(String(e.code).toUpperCase());
        }
      }
    }
  }).catch(() => { dHetPending = null; });   // retry on the next job load
  return dHetPending;
}

const COLUMNS = [
  { key: 'rank',        label: '#',             cls: 'mono',  sortable: false },
  { key: 'residues',    label: 'Sequence',      cls: '',      sortable: false },
  { key: 'iteration',   label: 'It.',           cls: 'mono',  sortable: true },
  { key: 'foldx',       label: 'ΔΔG',     cls: 'num',   sortable: true },
  { key: 'rosetta',     label: 'Total energy', cls: 'num',   sortable: true },
  { key: 'interface',   label: 'Interface',     cls: 'num',   sortable: true },
  // Its OWN column, not folded into Interface. dG_separated comes from
  // InterfaceAnalyzerMover in the NCAA design pass; Interface comes from
  // pepspec by way of run_mergeScores.py. Different quantities from different
  // code, so a column holding both would sort rows on incomparable numbers.
  { key: 'dgsep',       label: 'ΔG sep',        cls: 'num',   sortable: true },
  { key: 'gravy',       label: 'GRAVY',         cls: 'num',   sortable: true }
];

/* dG_separated comes only from InterfaceAnalyzerMover in the NCAA design pass,
   so a job without that pass has an empty column in every row. Dropped rather
   than shown blank -- the same rule the post-run filter control uses. */
function visibleColumns() {
  const anyDg = state.rows.some(r => isFinite(r.dgsep));
  return COLUMNS.filter(c => c.key !== 'dgsep' || anyDg);
}

const state = {
  jobId: null,
  rows: [],
  view: [],
  page: 0,
  sort: { key: 'foldx', dir: 'asc' },
  comp: 'all',
  // Post-run filters evaluated by run_mergeScores.py. 'all' shows every design;
  // 'pass' only those meeting the submitted filters; 'fail' the excluded ones,
  // which is how you see WHAT a filter removed rather than just how many.
  post: 'all',
  hasPostFilters: false,
  selected: null,
  viewer: null,
  rep: 'cartoon'
};

/* ------------------------------------------------------------------- CSV */

function parseCsv(text) {
  const lines = String(text || '').trim().split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) return [];
  const split = line => {
    const out = [];
    let cur = '', inQ = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inQ) {
        if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; }
        else if (c === '"') inQ = false;
        else cur += c;
      } else if (c === '"') inQ = true;
      else if (c === ',') { out.push(cur); cur = ''; }
      else cur += c;
    }
    out.push(cur);
    return out.map(s => s.trim());
  };
  const head = split(lines[0]);
  return lines.slice(1).map(line => {
    const cells = split(line);
    const o = {};
    head.forEach((h, i) => { o[h] = cells[i]; });
    return o;
  });
}

/* Kyte–Doolittle, one-letter. Non-canonicals have no scale value, so GRAVY is
   reported only for designs made entirely of canonical residues rather than
   silently treating an unknown residue as zero. */
const KD = { A:1.8, R:-4.5, N:-3.5, D:-3.5, C:2.5, Q:-3.5, E:-3.5, G:-0.4, H:-3.2,
             I:4.5, L:3.8, K:-3.9, M:1.9, F:2.8, P:-1.6, S:-0.8, T:-0.7, W:-0.9,
             Y:-1.3, V:4.2 };

const THREE_TO_ONE = { ALA:'A', ARG:'R', ASN:'N', ASP:'D', CYS:'C', GLN:'Q', GLU:'E',
                       GLY:'G', HIS:'H', ILE:'I', LEU:'L', LYS:'K', MET:'M', PHE:'F',
                       PRO:'P', SER:'S', THR:'T', TRP:'W', TYR:'Y', VAL:'V' };

function residueList(row) {
  // Prefer explicit three-letter codes when the pipeline provides them: the
  // one-letter pepSeq maps every non-canonical to "X" and cannot tell them apart.
  const explicit = row.pepResidues || row.pep_residues;
  if (explicit) {
    return String(explicit).trim().split(/[\s\-,]+/).filter(Boolean).map(code => {
      const c = code.toUpperCase();
      // Falls back to the old first-character rule only while the catalogue is
      // unavailable: a brief under-count beats labelling everything 'ncaa'.
      // Union, because neither test alone is right. The catalogue carries the
      // official PDB component id (D-serine = DSN), but Rosetta writes its own
      // name3 from the D_AA patch and for serine that is DSE -- seen in the
      // 11 November designs and in tab3_ncaa_designs.csv. The prefix rule is
      // safe to keep alongside it: no L residue in the catalogue has a het code
      // beginning with D (four END in D -- APD, AZD, BHD, LED -- which is why a
      // suffix rule is NOT added here).
      // The prefix test covers Rosetta's own name3 (DSER -> DSE), which the
      // catalogue does not carry -- it stores the PDB component id (DSN).
      // L_HET excludes the L residues whose het code also starts with D:
      // DAB, DPP and six others whose CODE begins with D while hand is L.
      const isD = D_HET.has(c)
        || (c.length === 3 && c[0] === 'D' && !L_HET.has(c));
      const kind = CANONICAL.has(c) ? '' : (isD ? 'dres' : 'ncaa');
      return { code: c, kind, one: THREE_TO_ONE[c] || 'X' };
    });
  }
  const seq = String(row.pepSeq || '').toUpperCase();
  return seq.split('').map(ch => ({
    code: ch,
    kind: ch === 'X' ? 'ncaa' : '',
    one: ch
  }));
}

function gravy(residues) {
  if (!residues.length) return null;
  let sum = 0;
  for (const r of residues) {
    const v = KD[r.one];
    if (v === undefined) return null;      // unknown residue — do not guess
    sum += v;
  }
  return sum / residues.length;
}

/* Iteration is recoverable from the stored relative path, which the aggregation
   builds as <job>/<job>_iterN_xxxx/<design>.pdbs/<model>.pdb */
function iterationOf(row) {
  if (row.iteration) return parseInt(row.iteration, 10) || null;
  const m = /_iter(\d+)_/.exec(String(row.pdb_relative_path || ''));
  return m ? parseInt(m[1], 10) : null;
}

/* CSV booleans arrive as the strings pandas writes: 'True', 'False', or empty
   for NA. Empty must stay NULL rather than becoming false -- "not evaluated"
   is not the same claim as "failed", and run_mergeScores.py is careful about
   that distinction, so the UI must not flatten it. */
function triBool(v) {
  const s = String(v ?? '').trim().toLowerCase();
  if (s === 'true') return true;
  if (s === 'false') return false;
  return null;
}

function normalise(raw) {
  return raw.map(r => {
    const residues = residueList(r);
    const g = gravy(residues);
    return {
      pdbId: r.pdbId || '',
      path: r.pdb_relative_path || '',
      residues,
      seqText: residues.map(x => x.code).join(' '),
      iteration: iterationOf(r),
      // Score columns name the quantity now (ddG_Score, Total_Energy). Jobs
      // aggregated by an older build still carry the engine-named headers and
      // must keep rendering, so read either.
      foldx: parseFloat(r.ddG_Score ?? r.FoldX_Score),
      rosetta: (r.energy_available ?? r.rosetta_available) === 'False'
        || (r.Total_Energy ?? r.Rosetta_Total_Score) === ''
        ? NaN : parseFloat(r.Total_Energy ?? r.Rosetta_Total_Score),
      interface: parseFloat(r.interface_score ?? r.Interface_Score ?? NaN),
      binding: parseFloat(r.binding_score ?? NaN),
      // Rows from tab3_ncaa_designs.csv, tagged by the server. Absent on every
      // job predating the design pass, which reads as 'pepspec'.
      source: r.source || 'pepspec',
      dgsep: parseFloat(r.dG_separated ?? NaN),
      // InterfaceAnalyzerMover's own descriptors, computed in the same run
      // as dG_separated and so available for every residue in the palette,
      // D set included -- unlike interface_score, which is parsed from
      // pepspec output and cannot exist for a non-canonical design.
      dgPerArea: parseFloat(r['dG_separated/dSASAx100'] ?? NaN),
      scValue: parseFloat(r.sc_value ?? NaN),
      dsasa: parseFloat(r.dSASA_int ?? NaN),
      hbInt: parseFloat(r.hbonds_int ?? NaN),
      unsatHb: parseFloat(r.delta_unsatHbonds ?? NaN),
      dCtrl: parseFloat(r.delta_dG_vs_control ?? NaN),
      dParent: parseFloat(r.delta_dG_vs_parent ?? NaN),
      verdict: r.verdict || '',
      parentResidues: String(r.parentResidues || '').trim(),
      gravy: g,
      hasNcaa: residues.some(x => x.kind === 'ncaa'),
      hasD: residues.some(x => x.kind === 'dres'),
      passes: triBool(r.passes_filters),
      ssOk: triBool(r.disulfide_ok ?? r.disulfide_intact),
      nativeOk: triBool(r.native_ok)
    };
  });
}

/* ------------------------------------------------- post-run filter control

   Injected from here rather than declared in index.html so it appears ONLY for
   jobs whose score table actually carries the columns. Jobs submitted before
   run_mergeScores.py evaluated the filters have no passes_filters column, and a
   toggle that silently matches nothing would be worse than no toggle. */

function renderPostFilter() {
  const host = document.getElementById('compFilter');
  if (!host || !host.parentNode) return;

  let box = document.getElementById('postFilter');
  if (!state.hasPostFilters) {
    if (box) box.remove();
    return;
  }
  if (!box) {
    box = document.createElement('div');
    box.id = 'postFilter';
    box.style.cssText = 'display:flex;gap:6px;align-items:center;flex-wrap:wrap';
    host.parentNode.insertBefore(box, host.nextSibling);
  }

  const n = k => state.rows.filter(r =>
    k === 'pass' ? r.passes === true
      : k === 'fail' ? r.passes === false
        : true).length;
  const unknown = state.rows.filter(r => r.passes === null).length;

  const opts = [['all', `All (${state.rows.length})`],
                ['pass', `Passes filters (${n('pass')})`],
                ['fail', `Excluded (${n('fail')})`]];
  box.innerHTML =
    '<span class="small muted">Submitted filters:</span>' +
    opts.map(([k, l]) =>
      `<button class="chip" data-post="${k}" aria-pressed="${state.post === k}">${esc(l)}</button>`
    ).join('') +
    (unknown
      ? `<span class="small muted" title="These rows lacked the data to evaluate the filter, so they are neither passing nor excluded.">${unknown} not evaluable</span>`
      : '');
}

/* ----------------------------------------------------------------- filter */

function applyFilters() {
  const seqQ = (document.getElementById('seqFilter')?.value || '').trim().toUpperCase();
  const iterQ = document.getElementById('iterFilter')?.value || '';
  const ddgQ = parseFloat(document.getElementById('ddgFilter')?.value);

  state.view = state.rows.filter(r => {
    if (seqQ && !r.seqText.toUpperCase().includes(seqQ) && !r.pdbId.toUpperCase().includes(seqQ)) return false;
    if (iterQ && String(r.iteration) !== iterQ) return false;
    if (isFinite(ddgQ) && !(r.foldx <= ddgQ)) return false;
    if (state.comp === 'ncaa' && !r.hasNcaa) return false;
    if (state.comp === 'dres' && !r.hasD) return false;
    if (state.comp === 'canon' && (r.hasNcaa || r.hasD)) return false;
    // A row whose filters could not be evaluated (passes === null) is shown
    // under 'all' but excluded from both 'pass' and 'fail', because it belongs
    // to neither and guessing either way would misreport it.
    if (state.post === 'pass' && r.passes !== true) return false;
    if (state.post === 'fail' && r.passes !== false) return false;
    return true;
  });

  const { key, dir } = state.sort;
  const sign = dir === 'asc' ? 1 : -1;
  state.view.sort((a, b) => {
    const av = a[key], bv = b[key];
    const an = typeof av === 'number' ? av : NaN;
    const bn = typeof bv === 'number' ? bv : NaN;
    if (isFinite(an) && isFinite(bn)) return (an - bn) * sign;
    if (isFinite(an)) return -1;          // rows with a value sort before blanks
    if (isFinite(bn)) return 1;
    return 0;
  });

  state.page = 0;
  renderTable();
}

/* ------------------------------------------------------------------ table */

function renderHead() {
  const head = document.getElementById('designsHead');
  if (!head) return;
  head.innerHTML = visibleColumns().map(c => {
    const sorted = state.sort.key === c.key;
    const aria = c.sortable ? ` aria-sort="${sorted ? (state.sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}" data-sort="${c.key}"` : '';
    return `<th scope="col" class="${c.cls}"${aria}>${c.label}</th>`;
  }).join('');
}

function renderTable() {
  const body = document.getElementById('designsBody');
  const count = document.getElementById('resultsCount');
  const pageInfo = document.getElementById('pageInfo');
  const sortNote = document.getElementById('sortNote');
  if (!body) return;

  if (count) {
    count.textContent = state.rows.length
      ? `${state.view.length} of ${state.rows.length} designs`
      : '';
  }
  if (sortNote) {
    const col = COLUMNS.find(c => c.key === state.sort.key);
    sortNote.textContent = col ? `sorted by ${col.label}, ${state.sort.dir}ending` : '';
  }

  if (!state.jobId) {
    body.innerHTML = '<tr><td colspan="' + visibleColumns().length +
      '" class="empty">Select a job above.</td></tr>';
    if (pageInfo) pageInfo.textContent = '';
    return;
  }
  if (!state.view.length) {
    body.innerHTML = '<tr><td colspan="' + visibleColumns().length + '" class="empty">' +
      (state.rows.length ? 'No designs match these filters.' : 'No score table for this job.') +
      '</td></tr>';
    if (pageInfo) pageInfo.textContent = '';
    return;
  }

  const start = state.page * PAGE;
  const slice = state.view.slice(start, start + PAGE);
  const showDg = state.rows.some(r => isFinite(r.dgsep));

  body.innerHTML = slice.map((r, i) => {
    const sel = r.path === state.selected;
    const chips = r.residues.map(x =>
      `<span class="res ${x.kind}">${esc(x.code)}</span>`).join('');
    // The design pass runs after the iterations, so these rows have no
    // iteration number and no FoldX score. Marked rather than left looking
    // like a pepspec row with missing data.
    const badge = r.source === 'ncaa_design'
      ? '<span class="chip" style="font-size:10px;padding:0 5px;margin-right:6px"' +
        ' title="From the rosetta_scripts NCAA design pass, run after the' +
        ' iterations. Ranked on \u0394G sep, not \u0394\u0394G.">NCAA</span>'
      : '';
    return `<tr class="pick" data-path="${esc(r.path)}" aria-selected="${sel}">
      <td class="mono muted">${start + i + 1}</td>
      <td><div class="seq">${badge}${chips}</div></td>
      <td class="mono">${r.iteration ?? '—'}</td>
      <td class="num"><strong>${fmtNum(r.foldx)}</strong></td>
      <td class="num">${isFinite(r.rosetta) ? fmtNum(r.rosetta, 1) : '—'}</td>
      <td class="num">${isFinite(r.interface) ? fmtNum(r.interface, 1) : '—'}</td>
      ${showDg ? `<td class="num">${isFinite(r.dgsep) ? fmtNum(r.dgsep, 2) : '—'}</td>` : ''}
      <td class="num">${r.gravy == null ? '—' : r.gravy.toFixed(2)}</td>
    </tr>`;
  }).join('');

  if (pageInfo) {
    pageInfo.textContent = `Rows ${start + 1}–${Math.min(start + PAGE, state.view.length)} of ${state.view.length}`;
  }
  document.getElementById('prevPage').disabled = state.page === 0;
  document.getElementById('nextPage').disabled = start + PAGE >= state.view.length;
}

/* ----------------------------------------------------------------- detail */

function renderDetail() {
  const host = document.getElementById('designDetail');
  if (!host) return;
  const r = state.view.find(x => x.path === state.selected) ||
            state.rows.find(x => x.path === state.selected);
  if (!r) {
    host.innerHTML = '<p class="small muted" style="margin:0">Select a design.</p>';
    return;
  }
  const chips = r.residues.map(x => `<span class="res ${x.kind}">${esc(x.code)}</span>`).join('');
  host.innerHTML = `
    <h2 style="margin-bottom:10px">Selected design</h2>
    <div class="seq" style="margin-bottom:12px">${chips}</div>
    <dl class="props">
      <div><dt>ΔΔG</dt><dd><strong>${fmtNum(r.foldx)}</strong></dd></div>
      <div><dt>Total energy</dt><dd>${isFinite(r.rosetta) ? fmtNum(r.rosetta, 1) : '—'}</dd></div>
      <div><dt>Interface</dt><dd>${isFinite(r.interface) ? fmtNum(r.interface, 1) : '—'}</dd></div>
      <div><dt>Binding</dt><dd>${isFinite(r.binding) ? fmtNum(r.binding, 1) : '—'}</dd></div>
      ${!isFinite(r.dgsep) ? '' : `
      <div><dt>ΔG separated</dt><dd><strong>${fmtNum(r.dgsep, 2)}</strong></dd></div>`}
      ${!isFinite(r.dgPerArea) ? '' : `
      <div><dt>ΔG per 100 Å²</dt><dd>${fmtNum(r.dgPerArea, 3)}</dd></div>`}
      ${!isFinite(r.scValue) ? '' : `
      <div><dt>Shape complementarity</dt><dd>${fmtNum(r.scValue, 3)}</dd></div>`}
      ${!isFinite(r.dsasa) ? '' : `
      <div><dt>Buried area</dt><dd>${fmtNum(r.dsasa, 0)} Å²</dd></div>`}
      ${!isFinite(r.hbInt) ? '' : `
      <div><dt>Interface H-bonds</dt><dd>${fmtNum(r.hbInt, 0)}</dd></div>`}
      ${!isFinite(r.unsatHb) ? '' : `
      <div><dt>Unsatisfied H-bonds</dt><dd>${fmtNum(r.unsatHb, 0)}</dd></div>`}
      ${!isFinite(r.dCtrl) ? '' : `
      <div><dt>vs canonical control</dt><dd>${fmtNum(r.dCtrl, 2)}</dd></div>`}
      ${!isFinite(r.dParent) ? '' : `
      <div><dt>vs parent</dt><dd>${fmtNum(r.dParent, 2)}</dd></div>`}
      ${!r.verdict ? '' : `
      <div><dt>Binding verdict</dt><dd>${r.verdict === 'improved'
        ? '<span style="color:var(--ok-ink,inherit)">improved</span>'
        : r.verdict === 'no_improvement'
          ? '<span style="color:var(--warn-ink,inherit)">no improvement</span>'
          : esc(r.verdict)}</dd></div>`}
      ${!r.parentResidues ? '' : `
      <div><dt>Designed from</dt><dd class="mono">${esc(r.parentResidues)}</dd></div>`}
      <div><dt>GRAVY</dt><dd>${r.gravy == null ? 'n/a' : r.gravy.toFixed(2)}</dd></div>
      <div><dt>Iteration</dt><dd>${r.iteration ?? '—'}</dd></div>
      ${r.passes === null ? '' : `
      <div><dt>Submitted filters</dt><dd>${r.passes
        ? '<span style="color:var(--ok-ink,inherit)">passes</span>'
        : '<span style="color:var(--warn-ink,inherit)">excluded</span>'}</dd></div>`}
      ${r.ssOk === null ? '' :
        `<div><dt>Disulfide</dt><dd>${r.ssOk ? 'intact' : 'broken'}</dd></div>`}
      ${r.nativeOk === null ? '' :
        `<div><dt>Native positions</dt><dd>${r.nativeOk ? 'match' : 'differ'}</dd></div>`}
    </dl>
    <p class="small muted mono" style="margin:12px 0 0;word-break:break-all">${esc(r.pdbId)}</p>
    ${r.gravy == null ? '<p class="chart-note" style="margin-top:8px">GRAVY needs a hydrophobicity value for every residue; this design contains one without one.</p>' : ''}
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:12px">
      <a class="btn btn-sm" href="${esc(api.pdbUrl(r.path))}" download>Download PDB</a>
    </div>`;
}

function select(path) {
  state.selected = path;
  renderTable();
  renderDetail();
  const r = state.rows.find(x => x.path === path);
  const lbl = document.getElementById('viewerPdb');
  if (lbl) lbl.textContent = r ? (r.pdbId || path.split('/').pop()) : '—';
  if (r && state.viewer) state.viewer.loadUrl(api.pdbUrl(r.path), r.pdbId);
}

/* ------------------------------------------------------------------- load */

export async function load(jobId) {
  state.jobId = jobId;
  state.rows = [];
  state.view = [];
  state.selected = null;
  const sub = document.getElementById('resultsSub');
  const body = document.getElementById('designsBody');

  if (!jobId) {
    if (sub) sub.textContent = 'No job selected.';
    renderTable();
    return;
  }
  if (body) body.innerHTML = '<tr><td colspan="8" class="empty"><span class="spinner"></span> Loading…</td></tr>';

  try {
    const res = await api.results(jobId);
    if (!res || !res.success) throw new Error((res && res.error) || 'No results.');
    await loadDHetCodes();
    state.rows = normalise(parseCsv(res.rosetta_csv_content));

    // The ΔG sep column is conditional on some row actually having one
    // (visibleColumns / showDg, both over state.rows). init() builds the
    // header while state.rows is still [], so before this call the heading
    // was omitted on every job WITH an NCAA pass, while renderTable emitted
    // the cell -- shifting every column after Interface one place left.
    // Only a click on a sortable heading rebuilt it, which is why the table
    // appeared to fix itself if you happened to sort.
    renderHead();  // rows are in now, so the conditional column is known

    const iters = [...new Set(state.rows.map(r => r.iteration).filter(Boolean))].sort((a, b) => a - b);
    const sel = document.getElementById('iterFilter');
    if (sel) {
      sel.innerHTML = '<option value="">All</option>' +
        iters.map(i => `<option value="${i}">${i}</option>`).join('');
    }

    // The toggle appears only when the table carries an evaluated verdict.
    state.hasPostFilters = state.rows.some(r => r.passes !== null);
    if (!state.hasPostFilters) state.post = 'all';
    renderPostFilter();

    const noRosetta = state.rows.length && state.rows.every(r => !isFinite(r.rosetta));
    const nPass = state.rows.filter(r => r.passes === true).length;
    if (sub) {
      sub.innerHTML = `Job <span class="mono">${esc(jobId)}</span> · ` +
        `${state.rows.length} designs` +
        (iters.length ? ` · iterations ${iters[0]}–${iters[iters.length - 1]}` : '') +
        (state.hasPostFilters
          ? ` · ${nPass} pass the submitted filters` : '') +
        (noRosetta ? ' · <span style="color:var(--warn-ink)">Energy scores missing (partial run)</span>' : '');
    }

    const zip = document.getElementById('downloadZip');
    if (zip) zip.onclick = () => { window.location.href = api.zipUrl(jobId); };

    applyFilters();
    if (state.view.length) select(state.view[0].path);
  } catch (e) {
    if (body) body.innerHTML = '<tr><td colspan="8" class="empty">Could not load results: ' + esc(e.message) + '</td></tr>';
    if (sub) sub.textContent = 'Job ' + jobId;
  }
}

async function fillJobs() {
  const sel = document.getElementById('resultsJob');
  if (!sel) return;
  try {
    const res = await api.jobs();
    const done = (res.jobs || []).filter(j =>
      ['Complete', 'Partial', 'Stopped'].includes(j.partial ? 'Partial' : j.status));
    sel.innerHTML = '<option value="">Select a finished job…</option>' +
      done.map(j => `<option value="${esc(j.job_id)}">${esc(j.protein_symbol)} · ${esc(j.job_id)}</option>`).join('');
    if (state.jobId) sel.value = state.jobId;
  } catch { /* leave as-is */ }
}

function exportCsv() {
  if (!state.view.length) return;
  const head = ['rank', 'pdbId', 'sequence', 'iteration', 'ddg', 'total_energy', 'interface', 'gravy'];
  const lines = [head.join(',')].concat(state.view.map((r, i) => [
    i + 1,
    '"' + r.pdbId.replace(/"/g, '""') + '"',
    '"' + r.seqText + '"',
    r.iteration ?? '',
    isFinite(r.foldx) ? r.foldx : '',
    isFinite(r.rosetta) ? r.rosetta : '',
    isFinite(r.interface) ? r.interface : '',
    r.gravy == null ? '' : r.gravy.toFixed(3)
  ].join(',')));
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (state.jobId || 'designs') + '_filtered.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* ------------------------------------------------------------------- wire */

export function init() {
  state.viewer = createViewer('resultViewer');
  renderHead();

  document.getElementById('resultsJob')?.addEventListener('change', e => load(e.target.value));
  ['seqFilter', 'ddgFilter'].forEach(id =>
    document.getElementById(id)?.addEventListener('input', applyFilters));
  document.getElementById('iterFilter')?.addEventListener('change', applyFilters);

  const comp = document.getElementById('compFilter');
  if (comp) {
    const opts = [['all', 'All'], ['canon', 'Canonical only'],
                  ['ncaa', 'Has non-canonical'], ['dres', 'Has D-residue']];
    const paint = () => {
      comp.innerHTML = opts.map(([k, l]) =>
        `<button class="chip" data-comp="${k}" aria-pressed="${state.comp === k}">${l}</button>`).join('');
    };
    paint();
    comp.addEventListener('click', e => {
      const b = e.target.closest('[data-comp]');
      if (!b) return;
      state.comp = b.dataset.comp;
      paint();
      applyFilters();
    });
  }

  // Delegated on document because #postFilter is created after init() runs.
  document.addEventListener('click', e => {
    const b = e.target.closest('#postFilter [data-post]');
    if (!b) return;
    state.post = b.dataset.post;
    renderPostFilter();
    applyFilters();
  });

  document.getElementById('designsHead')?.addEventListener('click', e => {
    const th = e.target.closest('[data-sort]');
    if (!th) return;
    const key = th.dataset.sort;
    state.sort = state.sort.key === key
      ? { key, dir: state.sort.dir === 'asc' ? 'desc' : 'asc' }
      : { key, dir: 'asc' };
    renderHead();
    applyFilters();
  });

  document.getElementById('designsBody')?.addEventListener('click', e => {
    const row = e.target.closest('tr[data-path]');
    if (row) select(row.dataset.path);
  });

  document.getElementById('prevPage')?.addEventListener('click', () => {
    if (state.page > 0) { state.page--; renderTable(); }
  });
  document.getElementById('nextPage')?.addEventListener('click', () => {
    if ((state.page + 1) * PAGE < state.view.length) { state.page++; renderTable(); }
  });

  document.getElementById('exportCsv')?.addEventListener('click', exportCsv);

  document.getElementById('viewerControls')?.addEventListener('click', e => {
    const b = e.target.closest('[data-rep]');
    if (b) {
      state.rep = b.dataset.rep;
      document.querySelectorAll('#viewerControls [data-rep]').forEach(x =>
        x.setAttribute('aria-pressed', String(x === b)));
      if (state.viewer) state.viewer.setRepresentation(state.rep);
      return;
    }
    if (e.target.closest('#resetView') && state.viewer) state.viewer.reset();
  });
}

/* Select a design by its pdbId rather than by its path.

   The Analytics maps know a design only by pdbId (that is what the score table
   and the embeddings carry), while selection here is keyed on
   pdb_relative_path. This bridges the two so clicking a point in the similarity
   tree or either 3D map opens that exact design with its structure loaded.

   Returns false when the id is not in the current table -- the caller can then
   say so instead of appearing to do nothing. */
export function selectDesign(pdbId) {
  const key = String(pdbId || '').split('/').pop()
    .replace(/\.mod\.pdb$/i, '').replace(/\.pdb$/i, '');
  if (!key) return false;
  const row = state.rows.find(r => {
    const k = String(r.pdbId || '').split('/').pop()
      .replace(/\.mod\.pdb$/i, '').replace(/\.pdb$/i, '');
    return k === key;
  });
  if (!row) return false;
  // Clear filters that would hide the row, so the selection is visible in the
  // table rather than only in the detail panel.
  if (state.post !== 'all' && row.passes !== (state.post === 'pass')) state.post = 'all';
  applyFilters();
  select(row.path);
  const el = document.querySelector(`tr[data-path="${CSS.escape(row.path)}"]`);
  if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
  return true;
}

export function show() {
  fillJobs();
  if (state.viewer) state.viewer.resize();
}
export function hide() {}
export function currentJob() { return state.jobId; }
