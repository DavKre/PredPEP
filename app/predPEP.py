#!/home/spacepep/miniforge3/envs/predPEP/bin/python

"""predPEP — Flask node for the pepspec peptide-design pipeline.

Routes fall into three groups:
  * submission + status (what the DDN client and the dashboard both use),
  * results + analytics (read models, score tables, projections),
  * admin (API tokens, audit log).

Auth is bearer-token, enforced only once a token exists -- see auth.py. The
scheduler owns job lifecycle and is the single source of truth for status; this
module never derives a verdict of its own.
"""

import csv
import glob
import io
import zipfile
from flask import send_file
import json
import os
import re
import shutil
import uuid
import math
from datetime import datetime, timezone

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_from_directory
from flask.json.provider import DefaultJSONProvider

import auth
import job_analytics
import residue_catalogue
import scheduler

# Submission-time structural check. Unlike tmap this is NOT optional: its job is
# to stop a discontinuous peptide reaching Rosetta, where the break becomes a
# fabricated 14 A "peptide bond" and the run completes with plausible scores for
# a molecule that cannot exist. A node that cannot import it must refuse
# submissions rather than accept them unchecked -- failing closed is the whole
# point of a guard. Everything else in the app keeps working.
try:
    import segment_inspector
    INSPECTOR_IMPORT_ERROR = None
except ImportError as _insp_err:
    segment_inspector = None
    INSPECTOR_IMPORT_ERROR = str(_insp_err)
    print(f'ERROR: segment_inspector not importable ({_insp_err}); '
          'submissions will be refused until it is available.')

# Splits a discontinuous peptide into one design problem per segment. Optional
# in a narrower sense than the inspector: without it a segmented peptide is
# REFUSED rather than run, which is correct but less useful. Never silently
# designed as one chain.
try:
    import split_segments
    SPLITTER_IMPORT_ERROR = None
except ImportError as _split_err:
    split_segments = None
    SPLITTER_IMPORT_ERROR = str(_split_err)
    print(f'Warning: split_segments not importable ({_split_err}); '
          'segmented peptides will be refused instead of split.')

# Import TMAP lazily-ish: the import pulls in RDKit, and a node without it
# should still serve everything else.
try:
    from tmap_utils import generate_tmap_coordinates
    TMAP_AVAILABLE, TMAP_IMPORT_ERROR = True, None
except ImportError as _tmap_err:
    # Recorded rather than only printed. Without this the endpoint could not
    # tell "the dependency is missing" from "the sequences are unusable", and
    # reported the latter either way -- which sent us looking for non-canonical
    # residues in a peptide that was entirely canonical.
    TMAP_AVAILABLE, TMAP_IMPORT_ERROR = False, str(_tmap_err)
    print(f'Warning: tmap_utils not importable ({_tmap_err}); '
          'the similarity tree is disabled.')

    def generate_tmap_coordinates(seqs):
        return [], [], [], [], []

predPEP = Flask(__name__)


# ----------------------------------------------------------------------
# JSON safety
# ----------------------------------------------------------------------
# Python's json emits a bare NaN / Infinity literal for non-finite floats.
# That is NOT valid JSON, and JSON.parse rejects it outright -- so an endpoint
# returns HTTP 200, curl shows plausible bytes, and the browser silently throws
# while parsing. The similarity-tree panel failed exactly this way: its metadata
# came from df.to_dict(), which carries NaN for every empty cell, producing
# '"Rosetta_Total_Score":NaN' and a panel that reported "no data" while the
# server believed it had succeeded.
#
# Fixed once, at the provider, rather than per endpoint: every jsonify() in this
# module goes through here, so no future endpoint can reintroduce it. Non-finite
# floats become null, which is what "no value" should have been all along.
#
# allow_nan is deliberately NOT set to False: that would turn any value this
# sanitiser misses into a 500 on endpoints that work today. Sanitising is the
# conservative choice.
def _json_finite(obj):
    """Recursively replace non-finite floats with None."""
    if isinstance(obj, float):                     # np.float64 subclasses float
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_finite(v) for v in obj]
    return obj


class _SafeJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        return super().dumps(_json_finite(obj), **kwargs)


predPEP.json = _SafeJSONProvider(predPEP)

# Node software version, stamped into the image at build time (Dockerfile
# ARG VERSION -> ENV PREDPEP_VERSION, sourced from the repo VERSION file by
# scripts/build.sh). Surfaced in /state + /health so a controller/DDN can detect
# out-of-date nodes.
NODE_VERSION = os.environ.get('PREDPEP_VERSION', 'unknown')

# Base directories. Overridable because the defaults live under /tmp, which is
# cleared on reboot and aged out by systemd-tmpfiles -- a direct contradiction
# of the scheduler's 180-day retention policy. scheduler.py and run_catSPEC.py
# read the same two variables; all three must agree or pdb_relative_path breaks.
BASE_UPLOAD_FOLDER = os.environ.get('PREDPEP_UPLOAD_DIR', '/tmp/pepspec/uploads')
BASE_RESULT_FOLDER = os.environ.get('PREDPEP_RESULT_DIR', '/tmp/pepspec/results')

MANAGER_SCRIPT_PATH = os.environ.get('PREDPEP_MANAGER', '/usr/local/bin/run_iteMAN.py')
PYTHON_EXECUTABLE = os.environ.get(
    'PREDPEP_PYTHON', '/home/spacepep/miniforge3/envs/predPEP/bin/python')

# Manager log candidates, newest-modified wins. iterative_manager.log carries the
# bash stages; the stdout/stderr pair carries the manager's own prints.
LOG_CANDIDATES = ('iterative_manager.log', '%s_manager_stderr.log', '%s_manager_stdout.log')

MAX_LOG_BYTES = 200_000


@predPEP.before_request
def _ensure_scheduler():
    scheduler.start_scheduler()  # idempotent; starts in the worker on first request


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def generate_base_name(protein_symbol, user_name):
    """SP + protein symbol (first 3 letters) + user initial."""
    prefix = 'SP'
    if protein_symbol:
        letters = re.sub(r'[^a-zA-Z]', '', protein_symbol)
        prot_part = letters[:3].upper() if letters else 'XXX'
    else:
        prot_part = 'XXX'
    user_part = user_name[0].upper() if user_name else 'Z'
    return f'{prefix}{prot_part}{user_part}'


def get_master_id(job_id):
    """The job's result-directory name. Historically this stripped the uuid
    suffix; it no longer does, and callers rely on that."""
    return job_id


def _safe_job_id(job_id):
    """Reject anything that could escape BASE_RESULT_FOLDER."""
    return bool(job_id) and '/' not in job_id and '\\' not in job_id \
        and '..' not in job_id and job_id not in ('.', '..')


def _job_dir(job_id):
    return os.path.join(BASE_RESULT_FOLDER, get_master_id(job_id))


def count_peptide_residues(pdb_path):
    """Unique chain-B residues by their Cα atoms. Returns int or None.

    The element guard ("C"/empty) excludes calcium, whose atom name is also CA.
    resSeq + iCode is stable across NMR models, so the set collapses duplicates
    without tracking MODEL/ENDMDL.
    """
    seen = set()
    try:
        with open(pdb_path) as f:
            for line in f:
                if (line.startswith(('ATOM', 'HETATM'))
                        and line[21:22] == 'B'
                        and line[12:16].strip() == 'CA'
                        and line[76:78].strip() in ('C', '')):
                    seen.add(line[22:27])
    except Exception:
        return None
    return len(seen) or None


# Flag values the submission form may set. Anything else in a posted spec is
# dropped rather than passed through to a Rosetta command line.
_ALLOWED_FLAG_KEYS = {
    'n_peptides': int, 'n_build_loop': int, 'diversify_lvl': int,
    'n_cgrelax_loop': int, 'n_dock_loop': int, 'n_prepend': int, 'n_append': int,
    'extrachi_cutoff': int,
    'interface_cutoff': float, 'clash_cutoff': float,
    'diversify_pep_seqs': bool, 'upweight_interface': bool, 'calc_sasa': bool,
    'no_cen': bool, 'no_cen_rottrials': bool, 'run_sequential': bool,
    'save_all_pdbs': bool, 'save_low_pdbs': bool, 'no_design': bool,
    'use_input_bb': bool, 'gen_pep_bb_sequential': bool, 'rmsd_analysis': bool,
    'ex1': bool, 'ex2': bool, 'restore_pre_talaris': bool,
    'ignore_unrecognized_res': bool,
    'soft_wts': str, 'score_weights': str, 'pep_chain': str,
}

MAX_SPEC_BYTES = 64 * 1024


def _sanitise_design_spec(raw):
    """Validate a posted design spec. Returns (spec_or_None, error_or_None).

    The spec reaches a Rosetta command line via make_flags.py, so it is treated
    as untrusted: only known keys survive, numbers are range-checked, and
    strings are restricted to a safe character set. A rejected spec fails the
    submission rather than being silently ignored -- a job that quietly runs
    with different settings than the user chose is worse than no job.
    """
    if not raw:
        return None, None
    if len(raw) > MAX_SPEC_BYTES:
        return None, 'design_spec is too large.'
    try:
        spec = json.loads(raw)
    except ValueError as e:
        return None, f'design_spec is not valid JSON: {e}'
    if not isinstance(spec, dict):
        return None, 'design_spec must be a JSON object.'

    out = {'spec_version': 1}

    mode = spec.get('mode', 'all')
    if mode not in ('all', 'score'):
        return None, f'unknown mode {mode!r}.'
    out['mode'] = mode

    # Anchors: positive ints, de-duplicated, order preserved by sorting.
    anchors = spec.get('anchors') or []
    if not isinstance(anchors, list):
        return None, 'anchors must be a list.'
    try:
        anchors = sorted({int(a) for a in anchors})
    except (TypeError, ValueError):
        return None, 'anchors must be integers.'
    if any(a < 1 or a > 100000 for a in anchors):
        return None, 'an anchor residue number is out of range.'
    if len(anchors) > 500:
        return None, 'too many anchors (max 500).'
    out['anchors'] = anchors

    flags = spec.get('flags') or {}
    if not isinstance(flags, dict):
        return None, 'flags must be an object.'
    clean_flags = {}
    for key, value in flags.items():
        caster = _ALLOWED_FLAG_KEYS.get(key)
        if caster is None:
            continue                       # unknown flag: dropped, not passed on
        try:
            if caster is bool:
                clean_flags[key] = bool(value)
            elif caster is int:
                n = int(value)
                if not 0 <= n <= 100000:
                    return None, f'{key} is out of range.'
                clean_flags[key] = n
            elif caster is float:
                f = float(value)
                if not 0 <= f <= 1000:
                    return None, f'{key} is out of range.'
                clean_flags[key] = f
            else:
                s = str(value)
                if not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', s):
                    return None, f'{key} contains unexpected characters.'
                clean_flags[key] = s
        except (TypeError, ValueError):
            return None, f'{key} is not a valid value.'
    out['flags'] = clean_flags

    pf = spec.get('post_filters') or {}
    if not isinstance(pf, dict):
        return None, 'post_filters must be an object.'
    try:
        native_at = sorted({int(p) for p in (pf.get('require_native_at') or [])})
    except (TypeError, ValueError):
        return None, 'require_native_at must be integers.'
    out['post_filters'] = {
        'require_disulfide_intact': bool(pf.get('require_disulfide_intact')),
        'require_native_at': native_at,
    }

    # Disulfide handling. This one DOES reach the Rosetta command line
    # (-in:fix_disulf / -in:detect_disulf), so the mode is checked against a
    # closed set rather than passed through.
    ssd = spec.get('disulfides') or {}
    if not isinstance(ssd, dict):
        return None, 'disulfides must be an object.'
    ss_mode = ssd.get('mode', 'auto')
    if ss_mode not in ('auto', 'preserve', 'allow_design'):
        return None, (f'unknown disulfide mode {ss_mode!r}; expected auto, '
                      'preserve or allow_design.')
    out['disulfides'] = {'mode': ss_mode}

    # Carried through untouched for the record; not used to build commands.
    for key in ('peptide', 'mechanism_notes'):
        if isinstance(spec.get(key), dict):
            out[key] = spec[key]

    # The design residue palette: which residues the packer may place.
    #
    # NOTE the two unrelated 'mode' fields. spec['mode'] above is 'all'|'score'
    # (what the pipeline does). spec['palette']['mode'] is which residue set it
    # may draw from. They are nested separately so neither can shadow the other.
    #
    # Only the non-canonical list is honoured. The canonical twenty cannot be
    # narrowed: CustomBaseTypePackerPalette always calls
    # set_up_default_base_types() before adding, and pepspec pushes no
    # subtractive task operation (and ignores resfiles, as recorded in
    # mechanism_notes). A client asking for a restricted canonical set is told
    # so rather than being given a job that quietly ignores the request -- the
    # same principle as the rejected-spec rule above.
    pal = spec.get('palette') or {}
    if not isinstance(pal, dict):
        return None, 'palette must be an object.'
    if pal.get('canonical') not in (None, 'all'):
        return None, ('palette.canonical must be "all": the packer palette is '
                      'additive and pepspec cannot exclude canonical residues.')
    pal_mode = pal.get('mode', 'l-canonical')
    if pal_mode not in residue_catalogue.PALETTE_MODES:
        return None, (f'unsupported palette mode {pal_mode!r}; this node can '
                      'build %s.' % ', '.join(residue_catalogue.PALETTE_MODES))
    ncaa, pal_rejected = residue_catalogue.validate_design_residues(
        pal.get('noncanonical'))
    if pal_rejected:
        detail = '; '.join('%s: %s' % (r['value'], r['reason'])
                           for r in pal_rejected[:5])
        return None, f'palette.noncanonical — {detail}'
    if pal_mode != 'l-canonical' and not ncaa:
        return None, (f'palette.mode is {pal_mode} but no non-canonical '
                      'residues were named.')
    # Derived from the ACCEPTED codes rather than taken from the client. The
    # mode describes the selection, and this record is what lets the results
    # table recover handedness: a mirrored non-canonical is written into the
    # output PDB under its L parent's three-letter code, so the palette beside
    # it is the only thing that says which hand was designed.
    out['palette'] = {'mode': residue_catalogue.palette_mode(ncaa),
                      'canonical': 'all', 'noncanonical': ncaa}

    extra = spec.get('extra_res_fa') or []
    if not isinstance(extra, list):
        return None, 'extra_res_fa must be a list.'
    safe_extra = []
    for p in extra:
        p = str(p)
        # Params files come from the node's own residue library, never from a
        # client-supplied path.
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,256}', p) or '..' in p:
            return None, 'an extra_res_fa path is not acceptable.'
        safe_extra.append(p)
    # Resolve the selected residues to their params files HERE, from the node's
    # own directory, so the flag generator receives paths this process has
    # checked rather than codes it would have to resolve itself. Appended to any
    # the client sent, de-duplicated with the order kept.
    for p in residue_catalogue.params_paths(ncaa):
        if p not in safe_extra:
            safe_extra.append(p)
    out['extra_res_fa'] = safe_extra

    return out, None


# The peptide chain. The form and the pipeline both assume B; a sub-job keeps
# that chain ID for its own segment so nothing downstream has to change.
PEPTIDE_CHAIN = 'B'


def _segment_description(chain):
    """Human-readable account of a discontinuous chain, with the measurements.

    Used in a refusal and in a submission response, so the person is told what
    was detected and on what evidence rather than just that something was
    wrong. The C-N distances are the point: 14.52 A against a bond length of
    1.33 A is not a borderline call.
    """
    segs = '; '.join(
        f'segment {s["index"]}: resSeq {s["start_resseq"]}-{s["end_resseq"]} '
        f'({s["length"]} residues)' for s in chain['segments'])
    breaks = ', '.join(
        f'{j["after_resseq"]}/{j["before_resseq"]} = {j["c_n_distance"]} A'
        for j in chain['junctions'] if j['bonded'] is False)
    return (f'{chain["segment_count"]} segments ({segs}); backbone C-N at the '
            f'break(s): {breaks}, against 1.33 A for a peptide bond')


def _prepare_spec_for_pdb(pdb_path, spec):
    """Structural check on the upload, plus numbering translation for the spec.

    Returns (spec, error_message, inspection). A non-None error fails the
    submission; the inspection report is stored either way, so a later job can
    be explained without re-reading the PDB.

    This does two jobs that both used to fail silently and produce confident
    wrong answers.

    1. REFUSE A DISCONTINUOUS PEPTIDE. A chain can be two physically separate
       pieces sharing one chain ID. clean_pdb.py renumbers chains from 1 and
       drops SSBOND, so the break and the staples both vanish before Rosetta
       opens the file; the pieces become consecutive residues and pepspec grows
       the peptide across the void with ideal geometry, dragging one piece
       through space. Observed: chain B as 1-21 plus 99-107 with a 14.52 A gap.
       Designing that as one chain is not an approximation of the molecule, so
       it is refused until the per-segment split exists.

    2. PUT ANCHORS IN THE PIPELINE'S NUMBERING. The form reads resSeq straight
       out of the UPLOADED file, and the pipeline works on the RENUMBERED one.
       run_createflags.py filtered anchors it could not find with a bare
       comprehension, so unmappable ones disappeared without a word -- four
       selected cysteines became two flag files -- and, worse, an anchor that
       happens to be in range survives while pointing at a DIFFERENT residue.
       A silently relocated anchor is worse than a dropped one: the job
       succeeds and the anchor was somewhere else.

       Translating to 1-based positions fixes both, because position is
       invariant under renumbering. Unmappable anchors fail the submission here,
       while the user is present to fix the selection.
    """
    if segment_inspector is None:
        return None, ('this node cannot check uploaded structures '
                      f'(segment_inspector unavailable: {INSPECTOR_IMPORT_ERROR}). '
                      'Submissions are refused rather than run unchecked.'), None
    try:
        report = segment_inspector.inspect(pdb_path, peptide_chain=PEPTIDE_CHAIN)
    except (OSError, ValueError) as e:
        return None, f'could not read the uploaded PDB: {e}', None

    chain = report['chains'].get(PEPTIDE_CHAIN)
    if chain is None:
        return None, (f'the upload has no chain {PEPTIDE_CHAIN}; the pipeline '
                      f'designs chain {PEPTIDE_CHAIN} against the rest. '
                      f'Chains found: {", ".join(report["chains"]) or "none"}.'), report

    # A segmented peptide is NOT refused here any more -- the caller splits it
    # into one sub-job per segment. It is still refused if the splitter is
    # unavailable, because the one thing that must never happen is designing it
    # as a single chain across a 14 A void.
    #
    # Translation below runs for a segmented chain too, and must: the splitter
    # consumes whole-chain anchor_positions and re-maps them into each segment's
    # own numbering.
    if chain['is_segmented'] and split_segments is None:
        return None, (
            f'chain {PEPTIDE_CHAIN} is not one continuous peptide '
            f'({_segment_description(chain)}), and this node cannot split it '
            f'(split_segments unavailable: {SPLITTER_IMPORT_ERROR}). Designing '
            'it as one chain would have Rosetta bond the segments across that '
            'gap and move one to close it, so the submission is refused.'), report

    if not spec:
        return spec, None, report

    position_map = chain['position_map']
    anchors = spec.get('anchors') or []
    mapped, unmapped = [], []
    for a in anchors:
        p = position_map.get(a)
        (mapped if p else unmapped).append(p if p else a)
    if unmapped:
        valid = ', '.join(str(r) for r in sorted(position_map))
        return None, (
            f'anchor residue(s) {sorted(unmapped)} are not in chain '
            f'{PEPTIDE_CHAIN}. Residues present: {valid}.'), report

    spec['spec_version'] = 2
    # The numbering the pipeline will see. run_createflags.py maps each position
    # back through the renumbered input's own residue order, so this stays
    # correct even if clean_pdb.py's renumbering ever changes.
    spec['anchor_positions'] = sorted(set(mapped))
    # Kept for the record and for older readers; NOT what drives the run.
    spec['anchors_uploaded_numbering'] = list(anchors)

    pep = spec.get('peptide')
    if isinstance(pep, dict):
        pairs, bad = [], []
        for pair in (pep.get('disulfides') or []):
            try:
                a, b = int(pair[0]), int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            pa, pb = position_map.get(a), position_map.get(b)
            if pa and pb:
                pairs.append([pa, pb])
            else:
                bad.append([a, b])
        pep['disulfide_positions'] = pairs
        if bad:
            # Not fatal: a pair naming a receptor residue is a real thing, and
            # refusing the job over it would be worse than designing without it.
            pep['disulfides_unmapped'] = bad

    # What the geometry actually supports, independent of any SSBOND record.
    spec['disulfides_observed'] = [
        {'positions': [d['position1'], d['position2']],
         'sg_distance': d['sg_distance'], 'state': d['state'],
         'declared': d['declared']}
        for d in report['disulfides']
        if d['chain1'] == PEPTIDE_CHAIN and d['chain2'] == PEPTIDE_CHAIN
        and d['position1'] and d['position2']]

    return spec, None, report


def _chain_b_codes(pdb_path):
    """Chain-B residue names in order, as three-letter codes.

    Recorded at submission so the dashboard can show WHICH residues a design
    contains: the one-letter FASTA sequence the pipeline derives later maps every
    non-canonical to 'X' (their params declare IO_STRING '<CODE> X'), which makes
    distinct non-canonicals indistinguishable downstream.
    """
    seen = {}
    try:
        with open(pdb_path) as f:
            for line in f:
                if not line.startswith(('ATOM', 'HETATM')):
                    continue
                if line[21:22] != 'B' or line[12:16].strip() != 'CA':
                    continue
                if line[76:78].strip() not in ('C', ''):
                    continue
                key = line[22:27]
                if key not in seen:
                    seen[key] = line[17:20].strip()
    except Exception:
        return []
    return list(seen.values())


# ----------------------------------------------------------------------
# Pages + probes
# ----------------------------------------------------------------------

@predPEP.route('/')
def index():
    return render_template('index.html', node_version=NODE_VERSION)


@predPEP.route('/health')
def health():
    """Liveness probe for an orchestrator / the Docker healthcheck. Never authed:
    a health check must not depend on holding a token."""
    return jsonify({'service': 'predpep-node', 'status': 'ok', 'version': NODE_VERSION})


@predPEP.route('/state', methods=['GET'])
@auth.require('read')
def node_state():
    """Capacity + disk for orchestrator dispatch and the UI bar."""
    state = scheduler.get_state()
    state['version'] = NODE_VERSION
    state['auth'] = auth.status()
    return jsonify(state)


@predPEP.route('/api/residues', methods=['GET'])
@auth.require('read')
def list_residues():
    """The design residue palette this node can offer.

    The residue catalogue intersected with the params actually installed, so the
    form cannot offer a residue Rosetta would fail to load. Read-only and
    cached; see residue_catalogue for why the cache is explicit.
    """
    return jsonify(residue_catalogue.public_payload())


@predPEP.route('/api/residues/reload', methods=['POST'])
@auth.require('admin')
def reload_residues():
    """Re-read the catalogue after params have been regenerated."""
    cat = residue_catalogue.reload()
    auth.audit('reload residue catalogue')
    return jsonify({'success': cat['error'] is None, 'error': cat['error'],
                    'counts': cat.get('counts', {})})


# ----------------------------------------------------------------------
# Submission
# ----------------------------------------------------------------------

CORE_FLOOR = 2


def _effective_cores(requested, pdb_path, spec):
    """Cap cores at the work there actually is to parallelise. Never raises.

    Returns (cores, reason_or_None).

    A pepspec wave runs ONE process per anchor, so cores beyond the anchor count
    idle through the whole wave while still being charged against the scheduler's
    budget. Observed: a 2-anchor sub-job holding 8 of an 8-core budget, with six
    doing nothing and the sibling sub-job unable to start.

    The bound is the anchor count when the spec names anchors, and the peptide's
    residue count otherwise -- which is the same thing, since a spec with no
    anchor list anchors every position. Residue count alone would be a much
    weaker bound: it only bites when someone asks for more cores than the peptide
    is long, while anchors bite whenever positions have been deselected.

    This only ever reduces. A user who asks for fewer cores than there are
    anchors gets what they asked for: they may be deliberately leaving room for
    other work, and that is their call to make.
    """
    bound = None
    what = ''
    if spec:
        anchors = spec.get('anchor_positions') or spec.get('anchors')
        if anchors:
            bound, what = len(anchors), 'anchor'
    if bound is None:
        n = count_peptide_residues(pdb_path)
        if n:
            bound, what = n, 'peptide residue'
    if not bound or requested <= bound:
        return requested, None
    cores = max(CORE_FLOOR, bound)
    if cores >= requested:
        return requested, None
    return cores, (f'{requested} cores requested but there are only {bound} '
                   f'{what}(s) to run in parallel; using {cores}')


def _queue_one_job(job_id, pdb_path, result_dir, spec, cpus, meta, inspection=None):
    """Write one job's files and hand it to the scheduler. Returns (ahead, error).

    Shared by the ordinary single-peptide path and by each per-segment sub-job,
    deliberately: a sub-job is an ORDINARY job in every respect. That is what
    makes splitting cheap -- the scheduler, the pipeline, the analytics and the
    archive need no notion of segments at all, and the two paths cannot drift
    because there is only one of them.

    The core count is clamped HERE rather than once at submission, so each
    sub-job is sized by its own segment: a 21-residue segment and a 9-residue
    one should not be given the same number of cores just because they came
    from one upload.
    """
    cpus, core_note = _effective_cores(int(cpus), pdb_path, spec)
    if core_note:
        predPEP.logger.info(f'[submit] {job_id}: {core_note}')
    if inspection is not None:
        try:
            with open(os.path.join(result_dir, 'structure_report.json'), 'w') as rf:
                json.dump(inspection, rf, indent=2)
        except OSError as e:
            predPEP.logger.warning(f'[submit] {job_id}: no structure report: {e}')

    # run_pepSpecPipe.sh runs with OUTPATH = <result dir>/<job>_iterN_xxxx as its
    # cwd, so the generator reads this at ../design_spec.json.
    if spec:
        try:
            with open(os.path.join(result_dir, 'design_spec.json'), 'w') as sf:
                json.dump(spec, sf, indent=2)
        except OSError as e:
            return None, f'could not store the design spec: {e}', cpus, core_note

    design = None
    if spec:
        # A v2 spec carries positions; a sub-job's spec has no 'anchors' at all,
        # since the uploaded file's resSeq stop meaning anything once a segment
        # is renumbered. Report whichever is present rather than assuming.
        anchors = spec.get('anchors') or spec.get('anchor_positions') or []
        design = {
            'mode': spec.get('mode'),
            'anchor_count': len(anchors),
            'anchors': list(anchors),
            'flags_changed': sorted(spec.get('flags') or {}),
            'disulfide_mode': (spec.get('disulfides') or {}).get('mode'),
            'post_filters': spec.get('post_filters'),
            # Which residues the packer was allowed to place. Carried here so
            # the Jobs table can say whether a job was canonical-only without
            # opening design_spec.json -- and so a job that asked for
            # non-canonicals is distinguishable afterwards from one that did
            # not, which is the whole point of the NCAA pass.
            'palette_mode': (spec.get('palette') or {}).get('mode'),
            'noncanonical': (spec.get('palette') or {}).get('noncanonical', []),
        }
        if spec.get('segment'):
            design['segment'] = spec['segment']

    try:
        with open(os.path.join(result_dir, 'job.json'), 'w') as jf:
            record = dict(meta)
            record.update({
                'job_id': job_id,
                'submitted_at': datetime.now(timezone.utc).isoformat(),
                'cpus': int(cpus),
                'peptide_length': count_peptide_residues(pdb_path),
                'peptide_residues': _chain_b_codes(pdb_path),
                'design': design,
                'status': 'queued',
            })
            # Promoted out of design_spec so the Jobs table can label a row
            # without opening the spec.
            if spec and spec.get('segment'):
                record['segment'] = spec['segment']
            # Recorded so a later reader can tell a deliberate core choice from
            # one the node reduced, without re-deriving the anchor count.
            if core_note:
                record['cores_note'] = core_note
            json.dump(record, jf)
    except Exception as e:
        predPEP.logger.warning(f'[submit] {job_id}: could not write job.json: {e}')

    manager_command = [
        PYTHON_EXECUTABLE, MANAGER_SCRIPT_PATH,
        pdb_path, result_dir, str(cpus),
        job_id, result_dir, job_id
    ]
    try:
        ahead = scheduler.enqueue(job_id, manager_command, int(cpus))
    except Exception as e:
        return None, f'failed to queue job: {e}', cpus, core_note
    auth.audit('submit job', job_id)
    return ahead, None, cpus, core_note


def _submit_segmented(parent_id, pdb_path, upload_folder, result_folder,
                      spec, cpus, meta, inspection, chain):
    """Split a discontinuous peptide and queue one ordinary job per segment.

    Each segment takes a turn as the peptide with every other segment moved
    into the receptor, which is the only honest way to run this through a
    pipeline built around one contiguous chain.

    The sub-jobs are siblings, not children: <parent>_s1, <parent>_s2, each a
    normal job directory. They are linked by `segment.parent_job` in both
    job.json and design_spec.json so the Jobs table can label them and a later
    version can group them without re-deriving the relationship.

    Returns (response_dict, error).
    """
    split_dir = os.path.join(upload_folder, '_segments')
    try:
        manifest = split_segments.split(pdb_path, PEPTIDE_CHAIN, split_dir, spec)
    except (OSError, ValueError) as e:
        return None, f'could not split the segmented peptide: {e}'

    created, failures = [], []
    for seg in manifest['segments']:
        k = seg['index']
        sub_id = f'{parent_id}_s{k}'
        sub_result = os.path.join(BASE_RESULT_FOLDER, sub_id)
        sub_upload = os.path.join(BASE_UPLOAD_FOLDER, sub_id)
        try:
            os.makedirs(sub_result, exist_ok=True)
            os.makedirs(sub_upload, exist_ok=True)
            sub_pdb = os.path.join(sub_upload, meta['pdb_filename'])
            shutil.copy2(seg['pdb'], sub_pdb)
            with open(os.path.join(seg['dir'], 'design_spec.json')) as f:
                sub_spec = json.load(f)
        except (OSError, ValueError) as e:
            failures.append(f'segment {k}: {e}')
            continue

        sub_spec.setdefault('segment', {}).update({
            'parent_job': parent_id,
            'job_id': sub_id,
            'index': k,
            'of': manifest['segment_count'],
            'label': f'sub-job {k}',
            'source_resseq': seg['source_resseq'],
            'sequence': seg['sequence'],
            'peptide_chain': seg['peptide_chain'],
            'receptor_chains': seg['receptor_chains'],
            'cross_segment_disulfides': seg['cross_segment_disulfides'],
        })
        sub_meta = dict(meta)
        sub_meta['parent_job'] = parent_id

        ahead, err, sub_cores, sub_note = _queue_one_job(
            sub_id, sub_pdb, sub_result, sub_spec, cpus, sub_meta, inspection)
        if err:
            failures.append(f'{sub_id}: {err}')
            shutil.rmtree(sub_result, ignore_errors=True)
            shutil.rmtree(sub_upload, ignore_errors=True)
            continue
        created.append({
            'job_id': sub_id, 'segment': k, 'label': f'sub-job {k}',
            'queue_position': ahead,
            'cores': sub_cores, 'cores_note': sub_note,
            'peptide': seg['sequence'],
            'source_resseq': seg['source_resseq'],
            'receptor_chains': seg['receptor_chains'],
            'anchor_positions': seg['anchor_positions'],
            'caveat': seg.get('caveat'),
            'warning': seg.get('warning'),
        })

    if not created:
        return None, ('the peptide is discontinuous and no sub-job could be '
                      'created: ' + '; '.join(failures))

    # The parent result directory would otherwise sit empty in the results list,
    # looking like a job that never started. The upload directory is kept: it
    # holds the original PDB and the split inputs, which is the provenance for
    # what these sub-jobs were derived from.
    shutil.rmtree(result_folder, ignore_errors=True)

    note = (f'Chain {PEPTIDE_CHAIN} is discontinuous '
            f'({_segment_description(chain)}), so it was split into '
            f'{len(created)} sub-job(s), each designing one segment with the '
            'other(s) as part of the receptor.')
    if any(c['caveat'] for c in created):
        note += (' Note: staples spanning two segments are not passed to '
                 'Rosetta as fixed disulfides, so each run designs against a '
                 'receptor that contains the other segment rather than one '
                 'covalently joined to it.')
    return {
        'success': True,
        'split': True,
        'parent_job': parent_id,
        'jobs': created,
        'job_id': created[0]['job_id'],      # for clients expecting one id
        'status': 'queued',
        'message': note,
        'failures': failures or None,
    }, None


@predPEP.route('/upload', methods=['POST'])
@auth.require('submit')
def upload_file():
    protein_symbol = request.form.get('protein_symbol')
    user_name = request.form.get('user_name')

    if not all([protein_symbol, user_name]):
        return jsonify({'success': False,
                        'error': 'Protein Symbol and User Name are required.'}), 400
    if 'file1' not in request.files:
        return jsonify({'success': False, 'error': 'No file part'}), 400

    file = request.files['file1']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'}), 400

    # Validate the design spec BEFORE creating any directories, so a rejected
    # spec leaves nothing behind.
    design_spec, spec_error = _sanitise_design_spec(request.form.get('design_spec'))
    if spec_error:
        return jsonify({'success': False, 'error': f'Invalid design spec: {spec_error}'}), 400

    # Cores. An explicit request is honoured up to the node's budget; a
    # submission that names no count is sized from the work in the spec.
    #
    # One pepspec process runs per anchor, so a job can never use more cores
    # than it has anchors. _effective_cores() enforces that later and only
    # ever reduces, deliberately: asking for fewer cores than anchors can be a
    # choice to leave room for other work. What was wrong was the DEFAULT --
    # a submission with no core count got 8 whatever the peptide looked like,
    # so an 11-anchor job ran 8 wide while a 2-anchor job reserved 8 and left
    # 6 idle for its whole run.
    raw_cpus = request.form.get('cpus')
    if raw_cpus in (None, ''):
        anchors = (design_spec or {}).get('anchors') or []
        want = len(anchors) or scheduler.CORE_BUDGET
    else:
        try:
            want = int(raw_cpus)
        except (TypeError, ValueError):
            want = scheduler.CORE_BUDGET
    cpus = max(2, min(32, scheduler.CORE_BUDGET, want))
    cpus = str(cpus)  # downstream Popen expects a string in the argv list
    predPEP.logger.info(f'[submit] clamped cpus={cpus} (raw={request.form.get("cpus")!r})')

    new_pdb_base = generate_base_name(protein_symbol, user_name)
    job_uuid = str(uuid.uuid4())
    job_folder_name = f'{new_pdb_base}_{job_uuid[:8]}'
    new_filename = f'{new_pdb_base}.pdb'

    master_result_folder = os.path.join(BASE_RESULT_FOLDER, job_folder_name)
    upload_folder = os.path.join(BASE_UPLOAD_FOLDER, job_folder_name)

    try:
        os.makedirs(upload_folder, exist_ok=True)
        os.makedirs(master_result_folder, exist_ok=True)
    except OSError as e:
        return jsonify({'success': False, 'error': f'Failed to create directories: {e}'})

    filepath = os.path.join(upload_folder, new_filename)
    try:
        file.save(filepath)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Failed to save file: {e}'})

    # Structural check and numbering translation. Has to run after the file is
    # on disk and before the job is queued. A refusal removes the directories
    # just created, so a rejected submission leaves nothing behind -- the same
    # contract as the spec validation above, which runs before any mkdir.
    design_spec, inspect_error, inspection = _prepare_spec_for_pdb(filepath, design_spec)
    if inspect_error:
        for d in (upload_folder, master_result_folder):
            shutil.rmtree(d, ignore_errors=True)
        predPEP.logger.warning(f'[submit] refused: {inspect_error}')
        return jsonify({'success': False, 'error': inspect_error}), 400

    if not os.path.exists(MANAGER_SCRIPT_PATH) or not os.access(MANAGER_SCRIPT_PATH, os.X_OK):
        return jsonify({'success': False, 'error': 'Manager script not found/executable.'})

    meta = {
        'protein_symbol': protein_symbol,
        'user_name': user_name,
        'user_email': request.form.get('user_email') or None,
        'pdb_filename': new_filename,
        'submitted_by': getattr(request, 'predpep_actor', None),
    }

    # A discontinuous peptide becomes one ordinary job per segment. Everything
    # downstream -- scheduler, pipeline, analytics, archive -- sees normal jobs.
    chain_info = (inspection or {}).get('chains', {}).get(PEPTIDE_CHAIN) or {}
    if chain_info.get('is_segmented'):
        payload, split_error = _submit_segmented(
            job_folder_name, filepath, upload_folder, master_result_folder,
            design_spec, cpus, meta, inspection, chain_info)
        if split_error:
            for d in (upload_folder, master_result_folder):
                shutil.rmtree(d, ignore_errors=True)
            predPEP.logger.warning(f'[submit] split failed: {split_error}')
            return jsonify({'success': False, 'error': split_error}), 400
        predPEP.logger.info(f'[submit] {job_folder_name} split into '
                            f'{len(payload["jobs"])} sub-job(s)')
        return jsonify(payload)

    ahead, queue_error, cpus, core_note = _queue_one_job(
        job_folder_name, filepath, master_result_folder, design_spec, cpus,
        meta, inspection)
    if queue_error:
        return jsonify({'success': False, 'error': queue_error}), 500

    state = scheduler.get_state()
    placed = 'running' if ahead == 0 and state['available_cores'] >= int(cpus) else 'queued'
    return jsonify({
        'success': True,
        'job_id': job_folder_name,
        'status': placed,
        'queue_position': ahead,
        'cores': int(cpus),
        'cores_note': core_note,
        'message': ((f'Job {job_folder_name} running.' if placed == 'running'
                     else f'Job {job_folder_name} queued ({ahead} ahead).')
                    + (f' {core_note}.' if core_note else '')),
    })


# ----------------------------------------------------------------------
# Status + job list
# ----------------------------------------------------------------------

@predPEP.route('/status/<job_id>', methods=['GET'])
@auth.require('read')
def check_status(job_id):
    """Report the job's real outcome.

    A result zip used to be treated as proof of success, so a run whose final
    score aggregation had crashed still reported "Complete" (job
    SPILRH_c70d2d4c). The verdict now comes from the scheduler, together with a
    machine-readable failed_stage so a consumer never has to parse a traceback
    out of the zip.
    """
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400

    if not os.path.exists(_job_dir(job_id)):
        return jsonify({'status': 'Pending/Failed', 'message': 'Job failed to start.'})

    view = scheduler.public_status(get_master_id(job_id))
    return jsonify({
        'status': view['status'],
        'download_url': view['download_url'],
        'partial': view['partial'],
        'failed_stage': view['failed_stage'],
        'artifacts': view['artifacts'],
        'message': view['message'] or f"Job {view['status'].lower()}.",
    })


@predPEP.route('/jobs', methods=['GET'])
@auth.require('read')
def list_jobs():
    """Every job on this node, newest first, with derived status."""
    jobs = []
    try:
        for entry in os.listdir(BASE_RESULT_FOLDER):
            jdir = os.path.join(BASE_RESULT_FOLDER, entry)
            if not os.path.isdir(jdir):
                continue

            meta = {}
            meta_path = os.path.join(jdir, 'job.json')
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                except Exception:
                    meta = {}
            meta.setdefault('job_id', entry)

            view = scheduler.public_status(entry)
            disp = view['status']
            meta['status'] = disp
            meta['download_url'] = view['download_url']
            meta['partial'] = view['partial']
            meta['failed_stage'] = view['failed_stage']
            meta['message'] = view['message']
            meta['artifacts'] = view['artifacts']

            # Iteration progress: live count while running, captured value once terminal.
            if disp == 'Running':
                meta['iteration'] = scheduler.count_iterations(entry)
            else:
                meta['iteration'] = meta.get('iterations_done')

            # Best score so far. Cheap for finished jobs (reads the CSV); for a
            # running job it falls back to the FoldX aggregate, which exists
            # before any CSV does.
            try:
                meta['best_score'] = job_analytics.best_score(jdir)
            except Exception:
                meta['best_score'] = None

            # Completion date -- fall back to the result zip's mtime for jobs
            # that finished before completed_at was recorded.
            if not meta.get('completed_at') and disp == 'Complete':
                zp = os.path.join(jdir, f'{entry}.zip')
                if os.path.exists(zp):
                    meta['completed_at'] = datetime.fromtimestamp(
                        os.path.getmtime(zp), timezone.utc).isoformat()

            jobs.append(meta)

        jobs.sort(key=lambda j: j.get('submitted_at', ''), reverse=True)
        return jsonify({'success': True, 'jobs': jobs,
                        'max_iterations': scheduler.MAX_ITERATIONS})
    except FileNotFoundError:
        return jsonify({'success': True, 'jobs': [], 'max_iterations': scheduler.MAX_ITERATIONS})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predPEP.route('/jobs/<job_id>', methods=['DELETE'])
@auth.require('admin')
def delete_job(job_id):
    """Delete a job's result + upload dirs (reclaims disk)."""
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400

    scheduler.cancel(job_id)
    removed = []
    for base in (BASE_RESULT_FOLDER, BASE_UPLOAD_FOLDER):
        d = os.path.join(base, job_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    if not removed:
        return jsonify({'success': False, 'error': 'Job not found.'}), 404

    auth.audit('delete job', job_id)
    return jsonify({'success': True, 'deleted': job_id})


@predPEP.route('/jobs/<job_id>/stop', methods=['POST'])
@auth.require('admin')
def stop_job(job_id):
    """Stop a running job: kill its process group and mark it Stopped."""
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    if not os.path.isdir(_job_dir(job_id)):
        return jsonify({'success': False, 'error': 'Job not found.'}), 404

    scheduler.cancel(job_id)
    auth.audit('stop job', job_id)
    return jsonify({'success': True, 'stopped': job_id})


# ----------------------------------------------------------------------
# Live progress
# ----------------------------------------------------------------------

@predPEP.route('/jobs/<job_id>/metrics', methods=['GET'])
@auth.require('read')
def job_metrics(job_id):
    """Per-iteration metrics the manager records as it runs.

    Absent for jobs that started before the manager wrote iteration_metrics.json,
    which is reported rather than faked -- the Analytics view falls back to
    deriving the curve from the score table's iteration folders.
    """
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    jdir = _job_dir(job_id)
    if not os.path.isdir(jdir):
        return jsonify({'success': False, 'error': 'Job not found.'}), 404

    data = job_analytics.read_metrics(jdir)
    if not data:
        return jsonify({'success': True, 'iterations': [], 'current': None,
                        'note': 'No iteration_metrics.json for this job.'})
    data.setdefault('success', True)
    return jsonify(data)


@predPEP.route('/jobs/<job_id>/spec', methods=['GET'])
@auth.require('read')
def job_spec(job_id):
    """The design spec a job was submitted with, and what the flag generator
    produced from it. Absent for jobs submitted before specs existed."""
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    jdir = _job_dir(job_id)
    if not os.path.isdir(jdir):
        return jsonify({'success': False, 'error': 'Job not found.'}), 404

    out = {'success': True, 'spec': None, 'manifest': None}
    try:
        with open(os.path.join(jdir, 'design_spec.json')) as f:
            out['spec'] = json.load(f)
    except (OSError, ValueError):
        pass
    # make_flags.py writes one manifest per iteration directory; the newest
    # reflects what actually ran.
    manifests = sorted(glob.glob(os.path.join(jdir, '*', 'flags_manifest.json')),
                       key=lambda p: os.path.getmtime(p), reverse=True)
    if manifests:
        try:
            with open(manifests[0]) as f:
                out['manifest'] = json.load(f)
        except (OSError, ValueError):
            pass
    if out['spec'] is None and out['manifest'] is None:
        out['note'] = 'No design spec recorded for this job.'
    return jsonify(out)


@predPEP.route('/jobs/<job_id>/log', methods=['GET'])
@auth.require('read')
def job_log(job_id):
    """Tail of the manager log, for the live console."""
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    jdir = _job_dir(job_id)
    if not os.path.isdir(jdir):
        return jsonify({'success': False, 'error': 'Job not found.'}), 404

    try:
        want = min(MAX_LOG_BYTES, max(1000, int(request.args.get('bytes', '8000'))))
    except (TypeError, ValueError):
        want = 8000

    candidates = []
    for pattern in LOG_CANDIDATES:
        name = pattern % job_id if '%s' in pattern else pattern
        path = os.path.join(jdir, name)
        if os.path.exists(path):
            try:
                candidates.append((os.path.getmtime(path), path))
            except OSError:
                continue
    if not candidates:
        return jsonify({'success': True, 'text': '', 'source': None,
                        'note': 'No manager log yet.'})

    _mtime, path = max(candidates)
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > want:
                f.seek(size - want)
                chunk = f.read()
                # Drop a partial first line so the tail starts cleanly.
                nl = chunk.find(b'\n')
                if nl != -1:
                    chunk = chunk[nl + 1:]
            else:
                chunk = f.read()
        text = chunk.decode('utf-8', errors='replace')
    except OSError as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': True, 'text': text,
                    'source': os.path.basename(path), 'bytes': len(chunk),
                    'truncated': size > want})


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------

@predPEP.route('/results_data/<job_id>', methods=['GET'])
@auth.require('read')
def get_results_data(job_id):
    """FoldX and Rosetta score tables, as raw file contents."""
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    try:
        master_id = get_master_id(job_id)
        base_result_dir = os.path.join(BASE_RESULT_FOLDER, master_id)

        # The FoldX aggregate is named by the PDB BASE (e.g. "SPILRH"), i.e. the
        # job id with the "_<uuid8>" suffix stripped -- NOT by the full job id.
        # Resolve it robustly: prefer the base-named file, fall back to the
        # job-id-named one, then glob any *.all.txt in the result dir.
        pdb_base = master_id.split('_')[0]
        foldx_candidates = [
            os.path.join(base_result_dir, f'{pdb_base}.all.txt'),
            os.path.join(base_result_dir, f'{master_id}.all.txt'),
        ]
        foldx_path = next((p for p in foldx_candidates if os.path.exists(p)), None)
        if foldx_path is None:
            globbed = glob.glob(os.path.join(base_result_dir, '*.all.txt'))
            foldx_path = globbed[0] if globbed else None

        rosetta_csv_path = os.path.join(base_result_dir, 'tab2_final_scores.csv')

        foldx_data = ''
        if foldx_path and os.path.exists(foldx_path):
            with open(foldx_path, errors='replace') as f:
                foldx_data = f.read()
        rosetta_data = ''
        if os.path.exists(rosetta_csv_path):
            with open(rosetta_csv_path, errors='replace') as f:
                rosetta_data = f.read()
        # The NCAA design pass writes its own table; fold it in so the Results
        # view shows those designs alongside the pepspec ones.
        rosetta_data = _merge_ncaa_designs(base_result_dir, rosetta_data)

        # The peptide score table (CSV) is the primary artifact -- serve results
        # whenever EITHER file is present; only 404 when neither exists yet.
        if not foldx_data and not rosetta_data:
            return jsonify({'success': False, 'error': 'Results not found yet.'}), 404

        return jsonify({
            'success': True, 'job_id': job_id, 'master_id': master_id,
            'foldx_txt_content': foldx_data, 'rosetta_csv_content': rosetta_data,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


NCAA_CSV = 'tab3_ncaa_designs.csv'


def _merge_ncaa_designs(base_result_dir, rosetta_data):
    """Append the NCAA design pass's rows to the Results score table.

    The two tables are written by different programs and do NOT share a
    schema. tab2 carries FoldX ddG and pepspec's own interface score; tab3
    carries InterfaceAnalyzerMover's dG_separated, computed by rosetta_scripts
    after the iterations finished. They are merged as a UNION of columns with a
    'source' column telling them apart, and NO value is moved from one column
    name into another.

    That last point is the whole design. It would be easy to write tab3's
    dG_separated into tab2's interface_score so the existing column lights up,
    and it would be wrong: they are different quantities from different code,
    so the table would sort rows against each other on numbers that are not
    comparable. A reader cannot see that from the rendered page, which is
    exactly the kind of error worth refusing to introduce.

    Returns rosetta_data UNCHANGED, byte for byte, when there is no NCAA table
    -- which is every job submitted before the design pass existed.
    """
    path = os.path.join(base_result_dir, NCAA_CSV)
    if not os.path.exists(path):
        return rosetta_data
    try:
        with open(path, errors='replace', newline='') as f:
            reader = csv.DictReader(f)
            ncaa_cols = list(reader.fieldnames or [])
            ncaa_rows = [r for r in reader]
    except (OSError, csv.Error):
        # A malformed NCAA table must not take the Results page down with it:
        # the pepspec designs are the primary artifact and are already in hand.
        return rosetta_data
    if not ncaa_rows:
        return rosetta_data

    base_cols, base_rows = [], []
    if rosetta_data.strip():
        try:
            rd = csv.DictReader(io.StringIO(rosetta_data))
            base_cols = list(rd.fieldnames or [])
            base_rows = [r for r in rd]
        except csv.Error:
            return rosetta_data

    # Union, base order first so the existing table is unchanged left to right.
    cols = list(base_cols)
    for c in ncaa_cols:
        if c not in cols:
            cols.append(c)
    if 'source' not in cols:
        cols.append('source')

    # A design's own pdb_relative_path points into ncaa_designs/ and carries
    # no _iterN_, so results.js's iterationOf() finds nothing and the It.
    # column renders as a dash. Its PARENT has one, and "built on a model from
    # iteration 3" is the fact worth showing. tab3.parent matches tab2.pdbId
    # verbatim (checked 18 Nov 2026 on SPNET_94dfeddd_s2: 10 of 10), and
    # rosetta_data IS tab2, so the join needs no second file read.
    #
    # This adds a COLUMN. It does not move a value from one column name into
    # another, which is what this function's docstring refuses to do: an
    # iteration is the same quantity on either row, unlike dG_separated and
    # interface_score.
    iter_of = {}
    for r in base_rows:
        m = re.search(r'_iter(\d+)_', r.get('pdb_relative_path') or '')
        if m and r.get('pdbId'):
            iter_of[r['pdbId']] = m.group(1)
    if 'iteration' not in cols:
        cols.append('iteration')

    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=cols, extrasaction='ignore',
                       lineterminator='\n')
    w.writeheader()
    for r in base_rows:
        if not r.get('source'):
            r['source'] = 'pepspec'
        w.writerow({k: r.get(k, '') for k in cols})
    for r in ncaa_rows:
        r['source'] = 'ncaa_design'
        # Only when the pass did not already provide one, and only when the
        # parent is actually in tab2 -- an unmatched parent leaves the dash
        # that was there before, rather than inventing an iteration.
        if not r.get('iteration'):
            r['iteration'] = iter_of.get(r.get('parent') or '', '')
        w.writerow({k: r.get(k, '') for k in cols})
    return out.getvalue()


ANALYTICS_CACHE = 'analytics_cache.json'
# Bump when job_analytics changes shape, so a stale cache is not served to a
# frontend expecting new keys.
ANALYTICS_CACHE_VERSION = 3


def _analytics_fingerprint(jdir):
    """Identity of the inputs the bundle is computed from.

    A finished job's score tables never change, so the bundle is a pure
    function of these files. Size and mtime together are enough: a rewritten
    table gets a new mtime, and a hand-edited one of identical mtime and size
    is not a case worth defending against here.
    """
    parts = [str(ANALYTICS_CACHE_VERSION)]
    for name in ('tab2_final_scores.csv', 'all_model_scores.csv',
                 'iteration_metrics.json'):
        p = os.path.join(jdir, name)
        try:
            st = os.stat(p)
            parts.append('%s:%d:%d' % (name, st.st_size, int(st.st_mtime)))
        except OSError:
            parts.append('%s:-' % name)
    # The per-iteration runs_status.json files appear one at a time as a job
    # progresses, and none of the three names above changes when one lands --
    # so without this a running job would keep serving a bundle whose sampling
    # figures predate the iterations that have since finished.
    sig = []
    for p in sorted(glob.glob(os.path.join(jdir, '*_iter*', 'runs_status.json'))):
        try:
            st = os.stat(p)
            sig.append('%s:%d:%d' % (os.path.basename(os.path.dirname(p)),
                                     st.st_size, int(st.st_mtime)))
        except OSError:
            continue
    parts.append('runs:' + (','.join(sig) if sig else '-'))
    return '|'.join(parts)


def _cached_analytics(jdir):
    """bundle(), computed once per state of the input tables.

    The computation is a couple of seconds of pandas and numpy, and the result
    is identical on every page load of a finished job -- so recomputing it each
    time is pure waste. Written next to the job's other records; it is
    regenerable, so nothing depends on it surviving.
    """
    fp = _analytics_fingerprint(jdir)
    path = os.path.join(jdir, ANALYTICS_CACHE)
    try:
        with open(path) as f:
            cached = json.load(f)
        if cached.get('_fingerprint') == fp:
            return cached
    except (OSError, ValueError):
        pass

    bundle = job_analytics.bundle(jdir)
    # Only a successful bundle is cached: a failure is usually a missing input
    # that may appear later (an aggregation still running), and caching it would
    # keep reporting failure after the cause was fixed.
    if bundle.get('success'):
        bundle['_fingerprint'] = fp
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(_json_finite(bundle), f)
            os.replace(tmp, path)          # atomic: a reader never sees a part-file
        except OSError as e:
            predPEP.logger.warning('[analytics] could not cache: %s', e)
    return bundle


@predPEP.route('/jobs/<job_id>/analytics', methods=['GET'])
@auth.require('read')
def job_analytics_route(job_id):
    """Score evolution, FoldX/Rosetta agreement, term correlation, PCA, logo."""
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    jdir = _job_dir(job_id)
    if not os.path.isdir(jdir):
        return jsonify({'success': False, 'error': 'Job not found.'}), 404
    try:
        bundle = _cached_analytics(jdir)
        return jsonify(bundle), (200 if bundle.get('success') else 404)
    except Exception as e:
        predPEP.logger.exception('[analytics] %s failed', job_id)
        return jsonify({'success': False, 'error': str(e)}), 500


@predPEP.route('/stream_final_pdb/<job_id>/<path:relative_path>', methods=['GET'])
@auth.require('read')
def stream_final_pdb(job_id, relative_path):
    """Stream a model PDB named by the score table's pdb_relative_path."""
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 403
    master_pdb_base = get_master_id(job_id)
    # relative_path is relative to the job's OWN directory, because Flask has
    # already taken the job id as the first URL segment (api.js builds the URL
    # as '/stream_final_pdb/' + pdb_relative_path, whose first component IS the
    # job dir). Requiring it to start with the job name was therefore wrong:
    # that holds for pepspec's iteration directories only by coincidence --
    # they are named <job>_iterN_xxxx -- and never for ncaa_designs/..., so
    # every design from the NCAA pass returned 403 and none was ever viewable.
    # Measured 18 Nov 2026 on SPNET_94dfeddd_s2: 403 Insecure path on
    # ncaa_designs/SPNETS4_2/ncaa_SPNETS4_2_0001.pdb, 200 on an iter path.
    #
    # What must actually hold is that the path cannot escape the job
    # directory. '..' and an absolute path are rejected here, and
    # send_from_directory confines the read regardless.
    if '..' in relative_path or relative_path.startswith(('/', '\\')):
        return jsonify({'success': False, 'error': 'Insecure path.'}), 403

    base_dir_to_send = os.path.join(BASE_RESULT_FOLDER, master_pdb_base)
    inner_relative_path = relative_path.removeprefix(master_pdb_base + os.sep)

    # Fall back to top10/ when the recorded path is gone.
    #
    # The score table's pdb_relative_path points inside an iteration directory,
    # e.g. <job>_iter3_4565/SPCGRK1I262L3.pdbs/SPCGRK1I262L3_2.pdb. Those model
    # directories are removed once a job completes (scheduler._post_zip_cleanup
    # reclaims the bulk; on a save_all_pdbs job that is tens of GB), while
    # top10/ is kept precisely so the viewer has something to show. The CSV
    # still names the original path, so without this the viewer 404s on every
    # design in a finished job.
    #
    # Only the BASENAME is reused, and only against top10/ inside this job's own
    # directory, so this widens nothing: the path traversal checks above have
    # already run, and send_from_directory still confines the read.
    full = os.path.join(base_dir_to_send, inner_relative_path)
    if not os.path.exists(full):
        candidate = os.path.join('top10', os.path.basename(inner_relative_path))
        if os.path.exists(os.path.join(base_dir_to_send, candidate)):
            inner_relative_path = candidate
        else:
            # Second fallback: read it out of the result zip.
            #
            # The sweep removes the model directories, but the zip keeps every
            # model -- 720 PDBs for SPNET_94dfeddd_s2 -- and its member names
            # are EXACTLY inner_relative_path (verified 18 Nov 2026:
            # SPNET_94dfeddd_s2_iter1_920e/SPNETS4.pdbs/SPNETS4_3.pdb). A zip
            # has a central directory, so this seeks to the one member and
            # inflates a few hundred KB rather than unpacking the 39 MB
            # archive, and nothing is written to disk. Every model stays
            # viewable for as long as the zip is kept, which the retention
            # settings already govern -- so no separate window to tune.
            #
            # This runs AFTER the path guard, so a traversal cannot reach it,
            # and a member name not in the archive raises KeyError, caught
            # here rather than surfacing as a 500.
            zip_path = os.path.join(base_dir_to_send,
                                    master_pdb_base + '.zip')
            if os.path.exists(zip_path):
                try:
                    with zipfile.ZipFile(zip_path) as z:
                        data = z.read(inner_relative_path)
                except (KeyError, zipfile.BadZipFile, OSError) as e:
                    predPEP.logger.warning('[pdb] zip miss %s in %s: %s',
                                           inner_relative_path, zip_path, e)
                else:
                    return send_file(io.BytesIO(data),
                                     mimetype='chemical/x-pdb')
            return jsonify({
                'success': False,
                'error': ('This model is not on disk and could not be read '
                          'from the result zip. Download the zip for the '
                          'full set.'),
            }), 404

    return send_from_directory(
        base_dir_to_send, inner_relative_path,
        as_attachment=False, mimetype='chemical/x-pdb'
    )


@predPEP.route('/get_tmap_tree/<job_id>', methods=['GET'])
@auth.require('read')
def get_tmap_tree(job_id):
    """TMAP layout coordinates for the sequence similarity tree.

    Metadata is filtered to valid_indices so the x/y arrays and the metadata
    array stay the same length -- a mismatch used to crash the front end.
    """
    if not _safe_job_id(job_id):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    try:
        master_id = get_master_id(job_id)
        csv_path = os.path.join(BASE_RESULT_FOLDER, master_id, 'tab2_final_scores.csv')
        if not os.path.exists(csv_path):
            return jsonify({'success': False, 'error': 'Scores CSV not found yet.'}), 404

        df = pd.read_csv(csv_path)
        if 'pepSeq' not in df.columns:
            return jsonify({'success': False, 'error': "CSV missing 'pepSeq' column."}), 500

        # Distinguish the three reasons this panel can come up empty, because
        # they need completely different responses from whoever reads it.
        if not TMAP_AVAILABLE:
            return jsonify({
                'success': False,
                'error': ('The similarity tree is disabled on this node: '
                          f'tmap_utils could not be imported ({TMAP_IMPORT_ERROR}). '
                          'It needs rdkit, tmap and mhfp in the service '
                          'environment. Nothing is wrong with this job\'s data.'),
            }), 503

        sequences = df['pepSeq'].fillna('').astype(str).str.upper().tolist()
        non_empty = [s_ for s_ in sequences if s_]
        if not non_empty:
            return jsonify({'success': False,
                            'error': ("The pepSeq column is empty for every row, so "
                                      "there are no sequences to compare.")}), 500

        x, y, s, t, valid_indices = generate_tmap_coordinates(sequences)

        if not valid_indices:
            # Report what was actually in the data instead of asserting a cause.
            with_x = sum(1 for s_ in non_empty if 'X' in s_)
            lengths = sorted({len(s_) for s_ in non_empty})
            detail = (f'{len(non_empty)} sequence(s), length(s) {lengths}, '
                      f'{with_x} containing "X"')
            hint = ('Non-canonical residues are written "X" in the one-letter '
                    'column and RDKit cannot build a molecule from them.'
                    if with_x else
                    'None of them contain "X", so the sequences themselves look '
                    'usable — the layout step is what failed.')
            return jsonify({'success': False,
                            'error': f'T-MAP produced no layout. {detail}. {hint}'}), 500

        filtered_metadata = df.iloc[valid_indices].to_dict(orient='records')
        return jsonify({'success': True, 'x': x, 'y': y, 's': s, 't': t,
                        'metadata': filtered_metadata})
    except Exception as e:
        predPEP.logger.exception('[tmap] %s failed', job_id)
        return jsonify({'success': False, 'error': str(e)}), 500


@predPEP.route('/download/<master_dir_name>/<filename>')
@auth.require('read')
def download_file(master_dir_name, filename):
    if not _safe_job_id(master_dir_name) or '/' in filename or '..' in filename:
        return jsonify({'success': False, 'error': 'Invalid path.'}), 403
    return send_from_directory(os.path.join(BASE_RESULT_FOLDER, master_dir_name),
                               filename, as_attachment=True)


# ----------------------------------------------------------------------
# Admin: tokens + audit
# ----------------------------------------------------------------------

@predPEP.route('/api/tokens', methods=['GET'])
@auth.require('admin')
def list_tokens():
    return jsonify({'success': True, 'tokens': auth.listing(),
                    'auth': auth.status()})


@predPEP.route('/api/tokens', methods=['POST'])
@auth.require('admin', bootstrap=True)
def create_token():
    """Issue a token. Allowed without auth ONLY while no tokens exist, so the
    first admin token can be created on a running node."""
    body = request.get_json(silent=True) or {}
    try:
        plaintext, record = auth.issue(
            label=body.get('label'),
            owner=body.get('owner'),
            scope=body.get('scope', 'submit'),
        )
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    auth.audit('issue token', '%s (%s)' % (record['label'], record['scope']))
    # The plaintext is returned here and never again.
    return jsonify({'success': True, 'token': plaintext, 'record': record})


@predPEP.route('/api/tokens/<token_id>', methods=['DELETE'])
@auth.require('admin')
def delete_token(token_id):
    if not auth.revoke(token_id):
        return jsonify({'success': False, 'error': 'Token not found.'}), 404
    auth.audit('revoke token', token_id)
    return jsonify({'success': True, 'revoked': token_id})


@predPEP.route('/api/audit', methods=['GET'])
@auth.require('admin')
def list_audit():
    try:
        limit = min(500, max(1, int(request.args.get('limit', '100'))))
    except (TypeError, ValueError):
        limit = 100
    return jsonify({'success': True, 'entries': auth.read_audit(limit)})


# Run under gunicorn; see gunicorn.conf.py and the systemd unit. A single worker
# is required: scheduler.py holds the queue and core budget in process memory.
# if __name__ == '__main__':
#     predPEP.run(host='0.0.0.0', port=8000)
