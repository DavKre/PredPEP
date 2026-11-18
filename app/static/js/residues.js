// residues.js — the design residue palette: which residues pepspec may place.
//
// Save as static/js/residues.js. Imported by submit.js; not a view, so it has
// no show/hide -- it is a component of the New job form and redraws from
// submit.js's refreshAll().
//
// WHAT IS AND IS NOT ENFORCEABLE
// ------------------------------
// ADDING a non-canonical residue works, and is enforced: the codes travel in
// the design spec, predPEP.py re-validates them against the installed params,
// and run_createflags.py writes them to the file named by
// -packing:packer_palette:extra_base_type_file. Rosetta then adds those types
// to the ones the packer may place.
//
// REMOVING a canonical residue is NOT enforceable here, for the same reason
// this form has no per-position design control:
//
//   * CustomBaseTypePackerPalette is ADDITIVE. get_base_residue_types() always
//     calls set_up_default_base_types() (the canonical 20) and then adds; the
//     XSD text says it "expands the default (canonical) residue type set".
//     There is no flag that omits the defaults.
//   * Subtracting a type needs a TaskOperation, and pepspec pushes only
//     InitializeFromCommandline, IncludeCurrent, RestrictResidueToRepacking
//     and PreventRepacking -- none of them subtractive.
//   * pepspec ignores resfiles (already verified for the anchor strip: a NATRO
//     resfile naming one designable position still varied eight others).
//
// So a checkbox that unticked CYS would look like it worked and change
// nothing. The canonical section is therefore shown as always-available with
// the reason stated, exactly as the anchor strip states why it selects RUNS
// rather than "residues allowed to change". It becomes interactive when the
// design stage can apply a subtractive task operation.
//
// THE FOUR SECTIONS, AND WHY A MIRROR BLOCKS ITS PARENT
// ----------------------------------------------------
// The server offers L-canonical, L-non-canonical, D-canonical and
// D-non-canonical. The D halves need no parameters of their own: Rosetta's
// D_AA patch selects on the L_AA property and mirrors every type it finds,
// including the ones we load with -extra_res_fa, so a D residue costs nothing
// to offer.
//
// Measured with PeptideStubMover on this build, appending each name in turn:
//
//     ALA              -> ALA
//     DALA             -> DAL     its own code
//     D0A1             -> 0A1     the L PARENT's code
//     DBiphenylAlanine -> BIF     the L parent's IO_STRING
//
// So the 19 D-canonicals carry their own three-letter code and stay
// distinguishable anywhere. The D-non-canonicals do NOT: CHIRAL_FLIP_NAMING
// has no het code to give them, so they inherit their parent's and are
// byte-identical to it in an output PDB.
//
// One job gets ONE palette file, applied to every designed position. So if a
// job offers D0A1 and not 0A1, an observed 0A1 can only be the D form -- the L
// type was never available to the packer. That is why selecting a residue
// disables its mirror and vice versa: with at most one hand per molecule, the
// three-letter code plus the job's recorded palette always recovers the
// handedness. Allow both and nothing downstream can tell them apart.

import { api, esc } from './api.js';

const state = {
  catalogue: null,      // as served by /api/residues
  error: null,
  applied: new Set(),   // committed selection
  draft: new Set(),     // the modal's working copy; discarded unless applied
  lastFocus: null,
  onChange: null,       // submit.js's refreshAll
  byCode: new Map(),    // code -> entry
  byMolecule: new Map(),// l_code -> [entries]  (an L and its mirror share one)
};

const D_CANONICAL_CATEGORY = 'D-canonical';

// The provenance half of a category name, shown as a tag on the row rather
// than as a section of its own: it says how the residue entered the library,
// which is not a reason to put it under a separate heading.
const PROVENANCE = {
  'L-noncanonical (by name)': 'by name',
  'L-noncanonical (struct match)': 'struct match',
  'D-noncanonical (by name)': 'by name',
  'D-noncanonical (struct match)': 'struct match',
};

/* ------------------------------------------------------------------ indexes */

function reindex() {
  state.byCode = new Map();
  state.byMolecule = new Map();
  const all = (state.catalogue && state.catalogue.noncanonical) || [];
  all.forEach(e => {
    state.byCode.set(e.code, e);
    const key = e.l_code || e.code;
    if (!state.byMolecule.has(key)) state.byMolecule.set(key, []);
    state.byMolecule.get(key).push(e);
  });
}

/** Every other offerable code for the same molecule -- in practice its mirror.
 *  Joined on l_code, which both hands carry, so it works even where the
 *  palette names are unrelated (BIF's type is BiphenylAlanine, its mirror's is
 *  DBiphenylAlanine). */
function mirrorsOf(code) {
  const e = state.byCode.get(code);
  if (!e) return [];
  return (state.byMolecule.get(e.l_code || code) || [])
    .filter(x => x.code !== code)
    .map(x => x.code);
}

/* --------------------------------------------------------------- accessors */

/** Committed non-canonical codes, sorted. Read by buildSpec(). */
export function selected() {
  return [...state.applied].sort();
}

/** The palette this selection describes.
 *
 *  Now that the D halves are offerable these are all reachable. Derived from
 *  the selection rather than tracked, so it cannot drift out of step with it. */
export function paletteMode() {
  const codes = [...state.applied];
  if (!codes.length) return 'l-canonical';
  const hands = new Set(codes.map(c => (state.byCode.get(c) || {}).hand || 'L'));
  if (hands.size > 1) return 'all-mixed';
  if (hands.has('L')) return 'l-mixed';
  const cats = new Set(codes.map(c => (state.byCode.get(c) || {}).category));
  return (cats.size === 1 && cats.has(D_CANONICAL_CATEGORY))
    ? 'd-canonical' : 'd-mixed';
}

export function count() { return state.applied.size; }

/* ----------------------------------------------------------------- summary */

/* The <summary> line, so the collapsed block says what the palette is without
   being opened -- the same job disulfideModeSummary does. */
export function render() {
  const sum = document.getElementById('residueSummary');
  const chip = document.getElementById('residueCount');
  const btn = document.getElementById('openResiduePicker');

  if (chip) {
    chip.textContent = state.applied.size ? '+' + state.applied.size : '';
  }
  if (!sum) return;

  if (state.error) {
    sum.innerHTML = '<span style="color:var(--warn-ink)">' + esc(state.error) +
      '</span> Design will use the canonical twenty only.';
    if (btn) btn.disabled = true;
    return;
  }
  if (!state.catalogue) {
    sum.textContent = 'Loading the residue catalogue…';
    return;
  }
  if (btn) btn.disabled = false;

  const c = state.catalogue.counts || {};
  const n = state.applied.size;
  if (!n) {
    // Says L and D separately: "413 available" reads as one pool and hides
    // that more than half of it is the mirror set.
    sum.textContent = c.d_offerable
      ? 'Canonical twenty only — ' + c.l_offerable + ' L and ' +
        c.d_offerable + ' D residues available.'
      : 'Canonical twenty only — ' + (c.noncanonical || 0) +
        ' non-canonical residues available.';
    return;
  }
  const codes = selected();
  const shown = codes.slice(0, 10).map(c2 => {
    const e = state.byCode.get(c2) || {};
    return '<span class="res ' + (e.hand === 'D' ? 'dres' : 'ncaa') + '">' +
      esc(shortLabel(e.code ? e : { code: c2 })) + '</span>';
  }).join('');
  const nolib = codes.filter(c2 => !hasRotlib(c2)).length;
  const nd = codes.filter(c2 => (state.byCode.get(c2) || {}).hand === 'D').length;
  sum.innerHTML = 'Canonical twenty plus ' + n +
    (nd ? ' (' + nd + ' D)' : '') + ': ' +
    '<span class="seq" style="display:inline-flex;vertical-align:middle">' +
    shown + '</span>' +
    (codes.length > 10 ? ' and ' + (codes.length - 10) + ' more' : '') +
    (nolib ? ' · <span style="color:var(--warn-ink)">' + nolib +
      ' with no rotamer library</span>' : '');
}

function hasRotlib(code) {
  if (!state.catalogue) return true;
  const e = state.byCode.get(code);
  // null means "unknown" -- Rosetta keeps its own NCAA libraries in a tree we
  // do not read -- and must not be reported as missing.
  return e ? e.rotlib !== false : true;
}

/** What to print in the chip. A Rosetta type name can be far longer than a
 *  three-letter code (BiphenylAlanine, DBiphenylAlanine) and would break the
 *  grid, so the chip shows the short form and the title gives the real name
 *  that goes into the palette file. */
function shortLabel(e) {
  if (!e || !e.code) return '';
  if (e.code.length <= 4) return e.code;
  if (e.hand === 'D' && e.csv_code) return 'D' + e.csv_code;
  return e.csv_code || e.code;
}

/* --------------------------------------------------------------- the modal */

function sectionNode(title, note, entries, locked, withActions) {
  const wrap = document.createElement('div');
  wrap.className = 'res-section';

  const head = document.createElement('div');
  head.className = 'res-section__head';
  const t = document.createElement('span');
  t.className = 'res-section__title';
  t.textContent = title + ' (' + entries.length + ')';
  head.append(t);

  if (withActions) {
    const acts = document.createElement('span');
    acts.className = 'chips res-section__acts';
    [['Select all shown', true], ['None', false]].forEach(([label, on]) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip';
      b.textContent = label;
      b.addEventListener('click', () => {
        // Acts on what is VISIBLE, so "select all shown" after a filter means
        // the ones being looked at, not all 197.
        wrap.querySelectorAll('.res-row').forEach(r => {
          if (r.hidden) return;
          const box = r.querySelector('input');
          if (box.disabled || box.checked === on) return;
          box.checked = on;
          if (on) state.draft.add(r.dataset.code);
          else state.draft.delete(r.dataset.code);
        });
        // Re-run after the whole sweep rather than per row: selecting a block
        // of mirrors blocks their parents, and the parents may be above the
        // row being ticked.
        syncExclusions();
        updateModalCount();
      });
      acts.append(b);
    });
    head.append(acts);
  }
  wrap.append(head);

  if (note) {
    const n = document.createElement('p');
    n.className = 'chart-note';
    n.style.margin = '2px 0 8px';
    n.textContent = note;
    wrap.append(n);
  }

  const grid = document.createElement('div');
  grid.className = 'res-grid';
  entries.forEach(e => grid.append(rowNode(e, locked)));
  wrap.append(grid);

  const empty = document.createElement('p');
  empty.className = 'empty';
  empty.textContent = 'Nothing here matches the filter.';
  empty.hidden = true;
  wrap.append(empty);
  wrap._empty = empty;
  return wrap;
}

function rowNode(entry, locked) {
  const isD = entry.hand === 'D';
  const label = document.createElement('label');
  label.className = 'res-row' + (locked ? ' res-row--locked' : '');
  label.dataset.code = entry.code;
  if (isD) label.dataset.hand = 'D';
  // Built once per row rather than per keystroke. Carries the short label and
  // the csv code too, so searching "BIF" finds BiphenylAlanine.
  label.dataset.hay = [entry.code, shortLabel(entry), entry.csv_code,
                       entry.name].filter(Boolean).join(' ').toLowerCase();
  if (entry.rotlib === false) label.dataset.nolib = '1';
  if (entry.foldx === false) label.dataset.nofoldx = '1';

  const box = document.createElement('input');
  box.type = 'checkbox';
  box.style.width = 'auto';
  if (locked) {
    box.checked = true;
    box.disabled = true;
    label.title = 'Always available — see the note above.';
  } else {
    box.checked = state.draft.has(entry.code);
    box.addEventListener('change', () => {
      if (box.checked) state.draft.add(entry.code);
      else state.draft.delete(entry.code);
      syncExclusions();
      updateModalCount();
    });
  }

  // Reuses the .res chip the sequence strips and the logo already use, so a
  // code looks the same here as it does in a result row. .dres is the D form.
  const code = document.createElement('span');
  code.className = 'res' + (locked ? '' : (isD ? ' dres' : ' ncaa'));
  code.textContent = shortLabel(entry);
  if (shortLabel(entry) !== entry.code) {
    code.title = 'Rosetta type name: ' + entry.code;
  }

  const name = document.createElement('span');
  name.className = 'res-row__name';
  name.textContent = entry.name;

  // Markers, all about how far through the pipeline this residue gets.
  const flag = document.createElement('span');
  flag.className = 'small muted';
  const marks = [];
  const tips = [];

  const prov = PROVENANCE[entry.category];
  if (prov) marks.push('· ' + prov);

  if (entry.foldx === true) {
    // Positive marker, because only a handful qualify: FoldX has parameters
    // for these, so a design containing one gets a real FoldX ddG and can be
    // ranked and selected like any canonical design.
    marks.push('· FoldX ✓');
    tips.push('FoldX has parameters for this residue, so a design containing '
            + 'it gets a valid ddG and is ranked like any other.');
  } else if (entry.foldx === false && !isD) {
    marks.push('· Rosetta score only');
    tips.push('FoldX has no parameters for this residue. It does not fail on '
            + 'it — it silently drops the residue and scores the rest of '
            + 'the peptide — so no FoldX ddG is recorded, and designs '
            + 'containing it are ranked on Rosetta’s interface and '
            + 'binding scores instead.');
  } else if (isD) {
    // A D residue's FoldX problem is NOT the dropped-residue one. rotabase is
    // keyed on L names, so where the mirror inherits its parent's code FoldX
    // scores it AS the L form and returns a believable number for the wrong
    // molecule. Stated separately because the remedy is different.
    marks.push('· Rosetta score only');
    tips.push('FoldX cannot score a D residue: rotabase is keyed on L names, '
            + 'so no FoldX ddG is recorded and designs containing it are '
            + 'ranked on Rosetta’s interface and binding scores.');
  }

  if (entry.rotlib === false) {
    marks.push('· 1 rotamer');
    tips.push('No rotamer library: the packer gets one fixed side-chain '
            + 'conformation for this residue.');
  } else if (entry.rotlib === null) {
    tips.push('Rosetta supplies this type and keeps its rotamer library in '
            + 'its own database, which predPEP does not read — so whether '
            + 'one exists is not reported here.');
  }

  if (isD && entry.category !== D_CANONICAL_CATEGORY) {
    marks.push('· shares its L code');
    tips.push('Rosetta mirrors this from the L form and has no separate '
            + 'three-letter code for it, so in the output PDB it is written '
            + 'under its L parent’s code. Selecting it excludes that '
            + 'parent, which is what keeps the two tellable apart.');
  } else if (isD && entry.pdb_code) {
    tips.push('Written into the output PDB as ' + entry.pdb_code
            + ', so it stays distinguishable from its L form.');
  }

  flag.textContent = marks.join(' ');
  if (tips.length) flag.title = tips.join(' ');

  label.append(box, code, name, flag);
  return label;
}

/** One hand per molecule. Recomputed from the draft rather than toggled, so
 *  the state cannot depend on the order boxes were clicked. */
function syncExclusions() {
  document.querySelectorAll('#residueSections .res-row').forEach(r => {
    if (r.classList.contains('res-row--locked')) return;
    const box = r.querySelector('input');
    if (!box) return;
    const code = r.dataset.code;
    if (state.draft.has(code)) {
      box.disabled = false;
      r.classList.remove('res-row--blocked');
      r.removeAttribute('title');
      return;
    }
    const blocking = mirrorsOf(code).filter(m => state.draft.has(m));
    box.disabled = blocking.length > 0;
    r.classList.toggle('res-row--blocked', blocking.length > 0);
    if (blocking.length) {
      r.title = 'Unavailable while ' + blocking.join(', ') + ' is selected. '
              + 'A job may not contain both a residue and its mirror: they '
              + 'share a three-letter code in the output PDB, so nothing '
              + 'downstream could tell which one a design used.';
    } else {
      r.removeAttribute('title');
    }
  });
}

function buildSections() {
  const host = document.getElementById('residueSections');
  if (!host || !state.catalogue) return;
  host.textContent = '';

  const cat = state.catalogue;
  const c = cat.counts || {};
  const all = cat.noncanonical || [];
  const lNon = all.filter(e => e.hand !== 'D');
  const dCanon = all.filter(e => e.hand === 'D' &&
                                 e.category === D_CANONICAL_CATEGORY);
  const dNon = all.filter(e => e.hand === 'D' &&
                               e.category !== D_CANONICAL_CATEGORY);

  host.append(sectionNode(
    'L-canonical', cat.canonical_note, cat.canonical, true, false));

  let lNote = 'From SwissSidechain, built and verified against this Rosetta '
            + 'build.';
  if (c.foldx_scorable) {
    lNote += ' ' + c.foldx_scorable + ' of them are marked "FoldX ✓": FoldX '
          + 'has parameters for those, so a design containing one gets a real '
          + 'ΔΔG and is ranked like any canonical design. For the rest FoldX '
          + 'silently drops the residue rather than failing, so no ΔΔG is '
          + 'recorded and the design is ranked on Rosetta’s interface and '
          + 'binding scores.';
  }
  if (c.without_rotlib) {
    lNote += ' ' + c.without_rotlib + ' have no rotamer library — marked '
          + '"1 rotamer" — so the packer cannot explore their side chain and '
          + 'will under-sample them against the canonicals they compete with.';
  }
  host.append(sectionNode('L-non-canonical', lNote, lNon, false, true));

  if (dCanon.length) {
    host.append(sectionNode('D-canonical',
      'Mirror images of the standard twenty. Rosetta builds them itself from '
      + 'the L forms — its D_AA patch flips any type carrying the L_AA '
      + 'property — so they need no parameters of ours. Each has its own '
      + 'three-letter code in the output PDB (DAL, DAR, DSG …), so a D design '
      + 'stays distinguishable from its L counterpart everywhere downstream. '
      + 'Glycine is achiral and has no mirror, so there are 19, not 20.',
      dCanon, false, true));
  }

  if (dNon.length) {
    host.append(sectionNode('D-non-canonical',
      'Mirror images of the L non-canonicals above, also generated by Rosetta '
      + 'from the L parameters. Unlike the D-canonicals these have no '
      + 'three-letter code of their own, so in the output PDB each is written '
      + 'under its L parent’s code. Selecting one therefore disables that '
      + 'parent: with at most one hand per molecule, the code plus the job’s '
      + 'recorded palette always says which was used. FoldX cannot score any '
      + 'of them.',
      dNon, false, true));
  }

  applyFilter();
  syncExclusions();
  updateModalCount();
}

function applyFilter() {
  const q = (document.getElementById('residueSearch')?.value || '')
    .trim().toLowerCase();
  const libOnly = !!document.getElementById('residueOnlyRotlib')?.checked;
  const fxOnly = !!document.getElementById('residueOnlyFoldx')?.checked;
  const lOnly = !!document.getElementById('residueHideD')?.checked;
  document.querySelectorAll('#residueSections .res-section').forEach(sec => {
    let shown = 0;
    sec.querySelectorAll('.res-row').forEach(r => {
      const hit = (!q || r.dataset.hay.includes(q)) &&
                  !(libOnly && r.dataset.nolib === '1') &&
                  !(fxOnly && r.dataset.nofoldx === '1') &&
                  !(lOnly && r.dataset.hand === 'D');
      r.hidden = !hit;
      if (hit) shown++;
    });
    if (sec._empty) sec._empty.hidden = shown > 0;
  });
}

function updateModalCount() {
  const el = document.getElementById('residueModalCount');
  if (!el) return;
  const n = state.draft.size;
  if (!n) { el.textContent = 'No non-canonical residues selected'; return; }
  const nd = [...state.draft]
    .filter(c => (state.byCode.get(c) || {}).hand === 'D').length;
  el.textContent = n + ' non-canonical residue' + (n === 1 ? '' : 's') +
    ' selected' + (nd ? ' (' + nd + ' D)' : '');
}

/* ---------------------------------------------------------- open and close */

function open() {
  const modal = document.getElementById('residueModal');
  if (!modal || !state.catalogue) return;
  state.draft = new Set(state.applied);
  state.lastFocus = document.activeElement;
  modal.hidden = false;
  // Rebuilt on open so the boxes match the committed selection rather than
  // the discarded edits of a cancelled visit.
  buildSections();
  document.getElementById('residueSearch')?.focus();
  document.addEventListener('keydown', onKey);
}

function close() {
  const modal = document.getElementById('residueModal');
  if (modal) modal.hidden = true;
  document.removeEventListener('keydown', onKey);
  if (state.lastFocus?.focus) state.lastFocus.focus();
}

function onKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); close(); }
}

function commit() {
  state.applied = new Set(state.draft);
  close();
  render();
  if (state.onChange) state.onChange();     // refreshAll: spec preview, etc.
}

/* -------------------------------------------------------------------- wire */

export function init(onChange) {
  state.onChange = onChange || null;

  document.getElementById('openResiduePicker')?.addEventListener('click', open);
  document.querySelectorAll('#residueModal [data-res-close]').forEach(el =>
    el.addEventListener('click', close));
  document.getElementById('residueApply')?.addEventListener('click', commit);
  document.getElementById('residueClearAll')?.addEventListener('click', () => {
    state.draft.clear();
    document.querySelectorAll('#residueSections .res-row input').forEach(b => {
      if (!b.disabled) b.checked = false;
    });
    syncExclusions();
    updateModalCount();
  });
  document.getElementById('residueSearch')?.addEventListener('input', applyFilter);
  document.getElementById('residueOnlyRotlib')?.addEventListener('change', applyFilter);
  document.getElementById('residueOnlyFoldx')?.addEventListener('change', applyFilter);
  document.getElementById('residueHideD')?.addEventListener('change', applyFilter);

  render();

  // Fetched rather than templated in: the offerable set is the intersection of
  // the residue CSV and the params actually installed, so a regeneration shows
  // up on the next page load instead of needing the template edited to match.
  // The server re-validates regardless -- these codes reach a Rosetta command
  // line, so anything the browser sends is a suggestion.
  api.residues()
    .then(data => {
      if (data && data.error) {
        state.error = data.error;
      } else {
        state.catalogue = data;
        reindex();
        // Drop anything no longer installed, so a restored form cannot carry a
        // code the node would now reject.
        const ok = new Set((data.noncanonical || []).map(e => e.code));
        [...state.applied].forEach(c => {
          if (!ok.has(c)) state.applied.delete(c);
        });
        // And drop a mirror whose parent is also still committed: the rule may
        // not have existed when the form was last filled in.
        [...state.applied].forEach(c => {
          if (mirrorsOf(c).some(m => state.applied.has(m))) {
            const e = state.byCode.get(c);
            if (e && e.hand === 'D') state.applied.delete(c);
          }
        });
      }
      render();
    })
    .catch(e => {
      state.error = 'Could not load the residue catalogue (' + e.message + ').';
      render();
    });
}

/* Cleared with the form, alongside anchors and native positions. */
export function reset() {
  state.applied = new Set();
  state.draft = new Set();
  render();
}
