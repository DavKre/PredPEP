// submit.js — New job view.
//
// The form reflects what pepspec can actually honour, which was established
// empirically rather than assumed:
//
//   * -pepspec::pep_anchor is a scalar Integer, so ONE residue is fixed per run.
//     The strip therefore selects which anchor RUNS to generate, not which
//     residues are "allowed to change".
//   * -packing:resfile is IGNORED by this protocol (verified: a NATRO resfile
//     naming one designable position still varied eight others).
//   * The full pepspec: option group contains nothing per-position.
//
// So per-position constraints cannot be applied during design. They are offered
// as post-run filters instead, with the yield cost shown before submitting.

import { api, esc } from './api.js';
import { createViewer, chainBResidues, chainBProfile, chainBDisulfides,
         divisorsInRange } from './viewer.js';
import * as residues from './residues.js';

const CANONICAL = new Set(['ALA','ARG','ASN','ASP','CYS','GLN','GLU','GLY','HIS','ILE',
                           'LEU','LYS','MET','PHE','PRO','SER','THR','TRP','TYR','VAL']);

// What run_createflag2_2.sh hardcoded. Kept as the default so a submission with
// no changes reproduces today's behaviour exactly.
const LAB_PRESET = {
  n_peptides: 10, n_build_loop: 100, interface_cutoff: 3.0,
  clash_cutoff: 4.0, diversify_lvl: 10
};

// Rosetta's own defaults, from the pepspec: option group.
const ROSETTA_DEFAULTS = {
  n_peptides: 8, n_build_loop: 1000, interface_cutoff: 5,
  clash_cutoff: 5, diversify_lvl: 10
};

const LAB_TOGGLES = {
  diversify_pep_seqs: true, upweight_interface: false,
  calc_sasa: false, no_cen: false, save_all_pdbs: false
};

const state = {
  viewer: null,
  residues: [],
  profile: [],
  disulfides: new Map(),
  anchors: new Set(),          // resSeq values to anchor
  native: new Set(),           // resSeq values to require native (post-filter)
  toggles: { ...LAB_TOGGLES },
  coreMax: 32,
  coreBudget: null,            // node core budget, once /state answers
  coresFree: null,             // cores free at page load, or null if unknown
  coresTouched: false,         // latches once the user sets cores by hand
  busy: false
};

function statusPanel(html) {
  const host = document.getElementById('submitStatus');
  if (host) host.innerHTML = html;
}

function params() {
  const out = {};
  document.querySelectorAll('[data-param]').forEach(el => {
    const v = parseFloat(el.value);
    if (isFinite(v)) out[el.dataset.param] = v;
  });
  return out;
}

function kindOf(code) {
  if (CANONICAL.has(code)) return '';
  return (code.length === 3 && code.startsWith('D')) ? 'dres' : 'ncaa';
}

/* ------------------------------------------------------------------ strips */

function renderStrip() {
  const strip = document.getElementById('pepStrip');
  const note = document.querySelector('.strip-note');
  if (!strip) return;

  if (!state.profile.length) {
    strip.innerHTML = '<span class="small muted">Detected after upload.</span>';
    if (note) note.textContent = 'Upload a PDB to see the peptide.';
    return;
  }

  strip.innerHTML = state.profile.map(r => {
    const on = state.anchors.has(r.resSeq);
    const partner = state.disulfides.get(r.resSeq);
    const bonded = partner !== undefined;
    const title = `position ${r.pos} · residue ${r.resSeq}` +
      (bonded ? ` · disulfide with ${partner}` : '') +
      (on ? ' · anchored (this residue is fixed in its run)' : ' · not anchored');
    const style = [
      'cursor:pointer',
      on ? 'outline:2px solid var(--accent);outline-offset:1px' : 'opacity:.55',
      bonded ? 'border-bottom:2px dashed var(--warn)' : ''
    ].filter(Boolean).join(';');
    return `<button type="button" class="res ${kindOf(r.code)}" data-anchor="${r.resSeq}"` +
           ` aria-pressed="${on}" style="${style}" title="${esc(title)}">${esc(r.code)}</button>`;
  }).join('');

  if (note) {
    const bonds = state.profile.filter(r => state.disulfides.has(r.resSeq));
    note.textContent =
      `Numbering starts at residue ${state.profile[0].resSeq}. ` +
      `Click a position to anchor it — the anchored residue is the one position ` +
      `held fixed in that run.` +
      (bonds.length ? ` ${bonds.length} cysteine(s) form a disulfide (dashed); ` +
        'Rosetta keeps them by default but may substitute them when it scores better.' : '');
  }
}

function renderNativeStrip() {
  const strip = document.getElementById('nativeStrip');
  if (!strip) return;
  if (!state.profile.length) {
    strip.innerHTML = '<span class="small muted">Upload a PDB first.</span>';
    return;
  }
  strip.innerHTML = state.profile.map(r => {
    const on = state.native.has(r.resSeq);
    const style = 'cursor:pointer;' + (on
      ? 'outline:2px solid var(--ok);outline-offset:1px'
      : 'opacity:.5');
    return `<button type="button" class="res ${kindOf(r.code)}" data-native="${r.resSeq}"` +
           ` aria-pressed="${on}" style="${style}"` +
           ` title="position ${r.pos} · require ${esc(r.code)}">${esc(r.code)}</button>`;
  }).join('');
}

/* ----------------------------------------------------------------- summary */

function updateAnchorSummary() {
  const el = document.getElementById('anchorSummary');
  if (!el) return;
  const n = state.anchors.size;
  const total = state.profile.length;
  if (!total) {
    el.textContent = 'Each anchored position is one pepspec run.';
    return;
  }
  const cpus = parseInt(document.getElementById('cpus')?.value, 10) || 8;
  const waves = n ? Math.ceil(n / cpus) : 0;
  const p = params();
  // Cost relative to the lab preset: both factors scale the work per run.
  const rel = (p.n_peptides / LAB_PRESET.n_peptides) *
              (p.n_build_loop / LAB_PRESET.n_build_loop);

  if (!n) {
    el.innerHTML = '<span style="color:var(--bad-ink)">No anchors selected — ' +
                   'the job would generate no runs.</span>';
    return;
  }
  el.innerHTML =
    `<strong>${n}</strong> of ${total} positions anchored → ${n} run(s) per iteration, ` +
    `${waves} wave(s) of up to ${cpus} cores.` +
    (isFinite(rel) && Math.abs(rel - 1) > 0.01
      ? ` Work per run is <strong>${rel.toFixed(2)}×</strong> the lab preset.`
      : '') +
    (n < total ? ` Skipping ${total - n} position(s) shortens the job proportionally.` : '');
}

function updateDeviation() {
  const el = document.getElementById('paramDeviation');
  if (!el) return;
  const p = params();
  const off = Object.keys(LAB_PRESET).filter(k => p[k] !== LAB_PRESET[k]);
  el.textContent = off.length ? `— ${off.length} changed from the lab preset` : '— lab preset';
}

/* Crude yield estimate for the post-run native filter.
   Assumes ~20 roughly equiprobable residues per designed position, which is a
   gross simplification: pepspec's distribution is far from uniform and the
   interface cutoff leaves some positions untouched anyway. Shown as an order of
   magnitude so the cost is visible, not as a prediction. */
function updateYield() {
  const el = document.getElementById('yieldEstimate');
  if (!el) return;
  const n = state.native.size;
  if (!n) {
    el.textContent = 'None selected — every design is kept.';
    el.style.color = '';
    return;
  }
  const keep = Math.pow(1 / 20, n);
  const oneIn = Math.round(1 / keep);
  el.innerHTML = `${n} position(s) required native → roughly <strong>1 in ` +
    `${oneIn.toLocaleString()}</strong> designs survive. ` +
    'Rough order of magnitude only: it assumes ~20 equiprobable residues per ' +
    'position, and pepspec is nowhere near uniform.';
  el.style.color = n > 2 ? 'var(--bad-ink)' : (n > 1 ? 'var(--warn-ink)' : '');
}

function refreshAll() {
  renderStrip();
  renderNativeStrip();
  updateAnchorSummary();
  renderDisulfideBlock();
  residues.render();
  updateDeviation();
  updateYield();
  applyCoreRecommendation();
  updateCpusHint();
  if (!document.getElementById('specPreview')?.classList.contains('hidden')) showSpec();
}

/* -------------------------------------------------------------- core hint */

/* The pipeline runs one pepspec invocation per ANCHOR, N at a time, so cores
   divide the work evenly when they divide the anchor count -- not the peptide
   length, which is what the old hint assumed. */
/* --------------------------------------------- automatic core sizing */

/* One pepspec process runs per anchor, so the useful number of cores IS the
   anchor count, bounded by what the scheduler will grant. Proposing that is
   the difference between a job that uses the machine and one that runs eight
   wide because 8 was the value in the HTML.

   Applied only while the user has not touched the control. state.coresTouched
   latches on the first edit of the slider or the field and is cleared only by
   a form reset, so an explicit choice is never overwritten by a later anchor
   change. */
function recommendedCores() {
  const n = state.anchors.size;
  if (!n) return null;
  return Math.max(2, Math.min(n, state.coreMax));
}

function applyCoreRecommendation() {
  if (state.coresTouched) return;
  const want = recommendedCores();
  if (want === null) return;
  const slider = document.getElementById('cpus_slider');
  const number = document.getElementById('cpus');
  if (number && parseInt(number.value, 10) !== want) number.value = want;
  if (slider && parseInt(slider.value, 10) !== want) slider.value = want;
}

function updateCpusHint() {
  const hint = document.getElementById('cpusHint');
  if (!hint) return;
  const cpus = parseInt(document.getElementById('cpus')?.value, 10) || 8;
  const n = state.anchors.size;

  hint.style.color = '';
  if (!state.profile.length) {
    hint.textContent = 'Upload a PDB and select anchors \u2014 cores follow the anchor count.';
    return;
  }
  if (!n) {
    hint.textContent = 'Select at least one anchor.';
    return;
  }
  if (cpus > n) {
    hint.textContent = `${n} run(s) but ${cpus} cores — only ${n} will be used. ` +
                       `Set cores to ${n}.`;
    hint.style.color = 'var(--warn-ink)';
    return;
  }
  if (n % cpus !== 0) {
    const divs = divisorsInRange(n, 2, state.coreMax);
    const near = [...divs.filter(d => d < cpus).slice(-1), ...divs.filter(d => d > cpus).slice(0, 1)];
    hint.textContent = `${n} runs on ${cpus} cores: ${Math.floor(n / cpus)} full wave(s) ` +
      `plus a final wave of ${n % cpus}, leaving cores idle.` +
      (near.length ? ` Even split at: ${near.join(' or ')}.` : '');
    return;
  }
  const waves = n / cpus;
  hint.textContent = `${n} runs on ${cpus} cores: ${waves} wave${waves === 1 ? '' : 's'}, no idle cores.`;
  hint.style.color = 'var(--ok-ink)';
}

/* -------------------------------------------------------------------- spec */

/* The selected disulfide mode, or 'auto' when the radios are absent (an older
   cached index.html) so the spec stays valid either way. */
function ssMode() {
  const picked = document.querySelector('input[name="ssMode"]:checked');
  const v = picked ? picked.value : 'auto';
  return ['auto', 'preserve', 'allow_design'].includes(v) ? v : 'auto';
}

/* Disables the two non-default modes when the template has no SSBOND, because
   there is nothing for them to act on: 'preserve' would warn and emit nothing,
   'allow_design' would turn off detection for no reason. */
/* Pairs whose BOTH partners exist in chain B and are cysteines.

   A template can declare an SSBOND inherited from a parent structure whose
   residues no longer exist. Seen in the wild: a 5-residue linear peptide whose
   PDB still carried CYS B 2 - CYS B 7. Offering "preserve" for that produced a
   flag file referencing a residue Rosetta cannot find. Validate against the
   residues actually present rather than trusting the header. */
function validDisulfidePairs() {
  const byResSeq = new Map(state.profile.map(r => [r.resSeq, r.code]));
  const seen = new Set();
  const good = [], stale = [];
  state.disulfides.forEach((b, a) => {
    const key = Math.min(a, b) + ':' + Math.max(a, b);
    if (seen.has(key)) return;
    seen.add(key);
    const ok = byResSeq.get(a) === 'CYS' && byResSeq.get(b) === 'CYS';
    (ok ? good : stale).push([Math.min(a, b), Math.max(a, b)]);
  });
  return { good, stale };
}

function renderDisulfideBlock() {
  const { good, stale } = validDisulfidePairs();
  const pairs = good.map(([a, b]) => `${a}–${b}`);
  const has = pairs.length > 0;

  document.querySelectorAll('input[name="ssMode"]').forEach(el => {
    if (el.value === 'auto') return;
    el.disabled = !has;
    const lab = el.closest('label');
    if (lab) lab.style.opacity = has ? '1' : '0.45';
    // A disabled radio keeps its checked state, which would send a mode the
    // template cannot support. Fall back to auto.
    if (!has && el.checked) {
      el.checked = false;
      const auto = document.querySelector('input[name="ssMode"][value="auto"]');
      if (auto) auto.checked = true;
    }
  });

  const sum = document.getElementById('disulfideModeSummary');
  if (sum) {
    if (!state.profile.length) {
      sum.textContent = 'Upload a PDB first.';
    } else if (!has) {
      sum.textContent = stale.length
        ? `This template declares a disulfide at ` +
          stale.map(([a, b]) => `${a}–${b}`).join(', ') +
          `, but those residues are not cysteines in chain B (or do not exist). ` +
          `The SSBOND record looks inherited from a parent structure — ignoring it.`
        : 'No SSBOND in this template — nothing to handle.';
    } else {
      const label = { auto: 'left to Rosetta detection',
                      preserve: 'preserved via -in:fix_disulf',
                      allow_design: 'designable, bond may be lost' }[ssMode()];
      sum.textContent = `Template disulfide ${pairs.join(', ')} (chain B): ${label}.`;
    }
  }
}

function buildSpec() {
  // Only pairs that survive validation go into the spec. Recording a stale
  // SSBOND here is what made run_catSPEC.py and the submitted spec disagree
  // about whether the peptide had a disulfide at all.
  const pairs = validDisulfidePairs().good;
  return {
    spec_version: 1,
    peptide: {
      chain: 'B',
      start_resseq: state.profile.length ? state.profile[0].resSeq : null,
      length: state.profile.length,
      residues: state.profile.map(r => r.code),
      disulfides: pairs
    },
    // 'all' is the only honest design mode: pepspec designs every position it
    // considers at the interface, and cannot be told otherwise.
    mode: 'all',
    anchors: [...state.anchors].sort((a, b) => a - b),
    flags: { ...params(), ...state.toggles },
    // Acts DURING the run: decides how Rosetta types the bonded cysteines.
    // 'auto' emits nothing, which is what every job before this setting did.
    disulfides: { mode: ssMode() },
    post_filters: {
      require_disulfide_intact: !!document.getElementById('f_disulfide')?.checked,
      require_native_at: [...state.native].sort((a, b) => a - b)
    },
    // Which residues the packer may place. Acts DURING the run, via
    // -packing:packer_palette:extra_base_type_file.
    //
    // `canonical` is 'all' rather than a list because it cannot be narrowed:
    // CustomBaseTypePackerPalette is additive and pepspec applies no
    // subtractive task operation. `mode` is the field the other palettes
    // (D-canonical, D-mixed, all-mixed) will use once a protocol that can
    // subtract types is in place; only the two L modes are emitted today.
    palette: {
      mode: residues.paletteMode(),
      canonical: 'all',
      noncanonical: residues.selected()
    },
    // Recorded so a reader of the job knows these were verified, not assumed.
    mechanism_notes: {
      resfile_supported: false,
      pep_anchor_is_scalar: true,
      per_position_design_control: 'unavailable in pepspec; post-filters only',
      packer_palette_is_additive: true
    },
    // Filled server-side from palette.noncanonical: the browser sends codes,
    // predPEP.py resolves them to params paths it has validated.
    extra_res_fa: []
  };
}

function showSpec() {
  const pre = document.getElementById('specPreview');
  if (!pre) return;
  pre.classList.remove('hidden');
  pre.textContent = JSON.stringify(buildSpec(), null, 2);
}

/* -------------------------------------------------------------------- file */

function onFile(file) {
  const info = document.getElementById('pdbInfo');
  if (!file) {
    if (info) info.textContent = 'No file selected.';
    return;
  }
  if (info) {
    info.textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB`;
    if (file.size > 25 * 1024 * 1024) info.textContent += ' — large; upload may be slow.';
  }

  const reader = new FileReader();
  reader.onload = ev => {
    const text = String(ev.target.result || '');
    state.residues = chainBResidues(text);
    state.profile = chainBProfile(text);
    state.disulfides = chainBDisulfides(text);
    // Default: anchor every position, which reproduces the current pipeline.
    state.anchors = new Set(state.profile.map(r => r.resSeq));
    state.native = new Set();

    const dNote = document.getElementById('disulfideFilterNote');
    if (dNote) {
      const pairs = [];
      state.disulfides.forEach((b, a) => { if (a < b) pairs.push(`${a}–${b}`); });
      dNote.textContent = pairs.length
        ? `Template disulfide: ${pairs.join(', ')} (chain B numbering).`
        : 'No disulfide detected in this template.';
    }

    if (!state.profile.length) {
      statusPanel('<div class="alert alert-warn"><strong>No chain B found.</strong>' +
        'The peptide to design must be chain B. The job will submit, but the ' +
        'pipeline has nothing to vary.</div>');
    } else {
      statusPanel(`<p class="small muted" style="margin:0">Chain B: ` +
        `${state.profile.length} residues, numbered from ${state.profile[0].resSeq}.</p>`);
    }

    refreshAll();
    if (state.viewer) state.viewer.loadText(text, file.name);
  };
  reader.onerror = () => { if (info) info.textContent = 'Could not read that file.'; };
  reader.readAsText(file);
}

/* ------------------------------------------------------------------ submit */

async function onSubmit(e, ctx) {
  e.preventDefault();
  if (state.busy) return;

  const file = document.getElementById('file1')?.files?.[0];
  const protein = document.getElementById('protein_symbol')?.value.trim();
  const user = document.getElementById('user_name')?.value.trim();

  if (!file) return statusPanel('<div class="alert alert-bad">Select a PDB file.</div>');
  if (!protein || !user) {
    return statusPanel('<div class="alert alert-bad">Protein symbol and your name are required.</div>');
  }
  if (state.profile.length && !state.anchors.size) {
    return statusPanel('<div class="alert alert-bad"><strong>No anchors selected.</strong>' +
      'The job would generate no pepspec runs.</div>');
  }

  const spec = buildSpec();
  const fd = new FormData();
  fd.append('file1', file);
  fd.append('protein_symbol', protein);
  fd.append('user_name', user);
  fd.append('cpus', document.getElementById('cpus')?.value || '8');
  const email = document.getElementById('user_email')?.value.trim();
  if (email) fd.append('user_email', email);
  fd.append('design_spec', JSON.stringify(spec));

  state.busy = true;
  const btn = document.getElementById('submitBtn');
  if (btn) { btn.disabled = true; btn.textContent = 'Submitting…'; }
  statusPanel('<p class="small" style="margin:0"><span class="spinner"></span> Uploading…</p>');

  try {
    const res = await api.submit(fd);
    if (!res || !res.success) throw new Error((res && res.error) || 'Submission failed.');
    statusPanel(`<div class="alert alert-ok">
        <strong>Job ${esc(res.job_id)} ${esc(res.status || 'queued')}.</strong>
        ${esc(res.message || '')}</div>
      <p class="small muted" style="margin:10px 0 0">
        ${spec.anchors.length} anchor run(s) per iteration. The run continues if you
        close this page — track it in Operations.</p>`);
    if (ctx && ctx.openJob) ctx.openJob(res.job_id);
  } catch (err) {
    statusPanel('<div class="alert alert-bad"><strong>Could not submit.</strong>' +
                esc(err.message) + '</div>');
  } finally {
    state.busy = false;
    if (btn) { btn.disabled = false; btn.textContent = 'Submit design job'; }
  }
}

/* -------------------------------------------------------------------- wire */

function setParams(values) {
  document.querySelectorAll('[data-param]').forEach(el => {
    if (values[el.dataset.param] !== undefined) el.value = values[el.dataset.param];
  });
  refreshAll();
}

function renderToggles() {
  document.querySelectorAll('[data-toggle]').forEach(b => {
    b.setAttribute('aria-pressed', String(!!state.toggles[b.dataset.toggle]));
  });
}

export function init(ctx) {
  state.viewer = createViewer('templateViewer', { background: '#0E1116' });

  // Redraws the summary line and the spec preview when the selection changes.
  residues.init(refreshAll);

  document.getElementById('file1')?.addEventListener('change', e => onFile(e.target.files[0]));

  // Anchor strip
  document.getElementById('pepStrip')?.addEventListener('click', e => {
    const b = e.target.closest('[data-anchor]');
    if (!b) return;
    const rs = parseInt(b.dataset.anchor, 10);
    if (state.anchors.has(rs)) state.anchors.delete(rs); else state.anchors.add(rs);
    refreshAll();
  });
  document.getElementById('anchorAll')?.addEventListener('click', () => {
    state.anchors = new Set(state.profile.map(r => r.resSeq));
    refreshAll();
  });
  document.getElementById('anchorNone')?.addEventListener('click', () => {
    state.anchors = new Set();
    refreshAll();
  });
  document.getElementById('anchorNonSS')?.addEventListener('click', () => {
    state.anchors = new Set(state.profile
      .filter(r => !state.disulfides.has(r.resSeq)).map(r => r.resSeq));
    refreshAll();
  });

  // Native-required strip (post-filter)
  document.getElementById('nativeStrip')?.addEventListener('click', e => {
    const b = e.target.closest('[data-native]');
    if (!b) return;
    const rs = parseInt(b.dataset.native, 10);
    if (state.native.has(rs)) state.native.delete(rs); else state.native.add(rs);
    refreshAll();
  });
  document.getElementById('f_disulfide')?.addEventListener('change', refreshAll);
  document.querySelectorAll('input[name="ssMode"]').forEach(el =>
    el.addEventListener('change', refreshAll));

  // Parameters + toggles
  document.querySelectorAll('[data-param]').forEach(el =>
    el.addEventListener('input', refreshAll));
  document.getElementById('paramToggles')?.addEventListener('click', e => {
    const b = e.target.closest('[data-toggle]');
    if (!b) return;
    state.toggles[b.dataset.toggle] = !state.toggles[b.dataset.toggle];
    renderToggles();
    refreshAll();
  });
  document.getElementById('resetPreset')?.addEventListener('click', () => {
    state.toggles = { ...LAB_TOGGLES };
    renderToggles();
    setParams(LAB_PRESET);
  });
  document.getElementById('rosettaDefaults')?.addEventListener('click', () =>
    setParams(ROSETTA_DEFAULTS));
  document.getElementById('showSpec')?.addEventListener('click', () => {
    const pre = document.getElementById('specPreview');
    if (pre && !pre.classList.contains('hidden')) pre.classList.add('hidden');
    else showSpec();
  });

  // Cores
  const slider = document.getElementById('cpus_slider');
  const number = document.getElementById('cpus');
  const clamp = v => Math.max(2, Math.min(state.coreMax, parseInt(v, 10) || 8));
  if (slider && number) {
    // Any manual edit stops the automatic sizing for the rest of the session.
    const touched = () => { state.coresTouched = true; };
    slider.addEventListener('input', () => {
      touched(); number.value = clamp(slider.value); refreshAll();
    });
    number.addEventListener('input', () => {
      touched(); slider.value = clamp(number.value); refreshAll();
    });
    number.addEventListener('change', () => {
      touched();
      number.value = clamp(number.value);
      slider.value = number.value;
      refreshAll();
    });
  }

  document.getElementById('uploadForm')?.addEventListener('submit', e => onSubmit(e, ctx));
  document.getElementById('uploadForm')?.addEventListener('reset', () => {
    state.residues = [];
    state.profile = [];
    state.disulfides = new Map();
    state.anchors = new Set();
    state.native = new Set();
    residues.reset();
    state.coresTouched = false;   // a reset forgets a manual core choice
    state.toggles = { ...LAB_TOGGLES };
    renderToggles();
    if (state.viewer) state.viewer.clear();
    document.getElementById('specPreview')?.classList.add('hidden');
    statusPanel('<p class="small muted" style="margin:0">Waiting for input.</p>');
    setTimeout(() => setParams(LAB_PRESET), 0);
  });

  // Clamp cores to the node's budget so the form cannot promise more than the
  // scheduler will grant.
  api.state().then(s => {
    if (!s || !s.core_budget) return;
    state.coreMax = Math.max(2, Math.min(32, s.core_budget));
    if (slider) slider.max = state.coreMax;
    if (number) {
      number.max = state.coreMax;
      if (parseInt(number.value, 10) > state.coreMax) {
        number.value = state.coreMax;
        if (slider) slider.value = state.coreMax;
      }
    }
    state.coreBudget = s.core_budget;
    state.coresFree = (typeof s.available_cores === 'number') ? s.available_cores : null;
    const range = document.getElementById('coreRange');
    if (range) {
      // Free-core count is from page load, not live -- enough to tell someone
      // the node is busy before they wait for a queued job.
      range.textContent = state.coresFree === null
        ? `(2–${state.coreMax}, node budget)`
        : `(2–${state.coreMax}, node budget · ${state.coresFree} free)`;
    }
    refreshAll();
  }).catch(() => {});

  renderToggles();
  refreshAll();
}

export function show() { if (state.viewer) state.viewer.resize(); }
export function hide() {}
