#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""Generate pepspec flag files — replaces run_createflag2_2.sh.

Two ways to call it:

  Drop-in (identical output to the bash script):
      run_createflags.py pepSeq flags2 SPTYKK SPTYKK

  Spec-driven (what the dashboard submits):
      run_createflags.py pepSeq flags2 SPTYKK SPTYKK --spec design_spec.json \\
                         --input-pdb inputPDB/SPTYKK.mod.pdb

What it fixes relative to the bash version:

  * The start residue is read from the PDB instead of `declare -i x=1`, which
    per the pipeline README had to be hand-edited in five separate scripts
    whenever a peptide did not start at residue 1.
  * n_peptides / n_build_loop / interface_cutoff / clash_cutoff / diversify_lvl
    are parameters with the current hardcoded values as defaults.
  * Anchors are selectable, so a sparse set of runs can be generated. It writes
    flags_manifest.json listing what it produced, because downstream scripts
    count 1..N and must iterate over what exists instead.
  * Disulfide handling is explicit (see below).
  * Non-canonical residues can be declared via extra_res_fa.

DISULFIDE HANDLING — why this exists
------------------------------------
pepspec logs, for a peptide with an SSBOND:

    make_sequence_change failed: new_aa= ALA -- no matching residue types

That is pepspec TRYING to design a disulfide-bonded cysteine and being refused:
the residue carries the DISULFIDE variant type and there is no ALA-with-
disulfide to switch to. So those positions hold by accident of that failure, not
by any protective logic -- which is exactly why they intermittently do NOT hold.
When a run's disulfide detection does not type them as bonded, the substitution
succeeds and the bond is lost.

Hence three modes, set by spec["disulfides"]["mode"]:

  auto          (default) emit nothing -- current pipeline behaviour, detection
                left to Rosetta, bond usually but not always preserved.
  preserve      emit -in:fix_disulf with the pairs in POSE numbering, making the
                typing explicit so the bond is preserved every run.
  allow_design  emit -in:detect_disulf false, so the cysteines are plain CYS and
                design at those positions actually succeeds. The bond is lost.

POSE NUMBERING: fix_disulf counts residues across all chains from 1, not by PDB
resSeq. For a complex staged as chain A then chain B, chain B residue i is pose
index len(chain A) + i. Verified against Rosetta's own log on a 461-residue
target: "Found disulfide between residues 463 468" for chain B 2-7. The computed
pairs are recorded in the manifest so they can be cross-checked against that
log line.

What it does NOT do silently: if the spec asks to fix a SET of positions, that
needs a resfile, and pepspec IGNORES resfiles (verified: a NATRO resfile naming
one designable position still varied eight others). Rather than emit flags that
ignore the request, it refuses unless the spec sets "resfile_supported": true
or --allow-unenforced is passed. pep_anchor fixes exactly ONE residue per run.
"""

import argparse
import json
import os
import sys

# Defaults are exactly the values run_createflag2_2.sh hardcoded, so output is
# unchanged when no spec is supplied.
DEFAULT_FLAGS = {
    'restore_pre_talaris': True,
    'score_weights': 'pre_talaris_2013_standard.wts',
    'soft_wts': 'soft_rep.wts',
    'interface_cutoff': 3.0,
    'clash_cutoff': 4.0,
    'n_peptides': 10,
    'n_build_loop': 100,
    'use_input_bb': True,
    'gen_pep_bb_sequential': True,
    'diversify_pep_seqs': True,
    'rmsd_analysis': True,
    'ex1': True,
    'ex2': True,
    'extrachi_cutoff': 0,
    'pep_chain': 'B',
    'n_prepend': 0,
    'n_append': 0,
    'ignore_unrecognized_res': True,
}

# Optional extras, emitted only when present in the spec's flags.
OPTIONAL_INT = ('diversify_lvl', 'n_cgrelax_loop', 'n_dock_loop')
OPTIONAL_BOOL = ('upweight_interface', 'calc_sasa', 'no_cen', 'no_cen_rottrials',
                 'run_sequential', 'save_all_pdbs', 'save_low_pdbs', 'no_design')

THREE_TO_ONE = {
    'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C', 'GLN': 'Q',
    'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I', 'LEU': 'L', 'LYS': 'K',
    'MET': 'M', 'PHE': 'F', 'PRO': 'P', 'SER': 'S', 'THR': 'T', 'TRP': 'W',
    'TYR': 'Y', 'VAL': 'V',
}
ONE_TO_THREE = {v: k for k, v in THREE_TO_ONE.items()}

DISULFIDE_MODES = ('auto', 'preserve', 'allow_design')


# ---------------------------------------------------------------- PDB reading

def _ca_lines(pdb_path):
    """Yield (chain, resSeq+iCode key, resSeq, resName) for each CA, in file order.

    The element guard excludes calcium, whose atom name is also CA.
    """
    try:
        with open(pdb_path, errors='replace') as f:
            for line in f:
                if not line.startswith(('ATOM', 'HETATM')) or len(line) < 27:
                    continue
                if line[12:16].strip() != 'CA':
                    continue
                if line[76:78].strip() not in ('C', ''):
                    continue
                yield line[21], line[22:27], int(line[22:26]), line[17:20].strip()
    except OSError as e:
        print(f'WARNING: could not read {pdb_path}: {e}', file=sys.stderr)


def chain_residues(pdb_path, chain='B'):
    """[(resSeq, three_letter)] for one chain, in order, de-duplicated."""
    seen = {}
    for ch, key, resseq, name in _ca_lines(pdb_path):
        if ch != chain:
            continue
        if key not in seen:
            seen[key] = (resseq, name)
    return list(seen.values())


def pose_index_map(pdb_path, chain='B'):
    """{resSeq of `chain` -> 1-based POSE index}.

    Pose numbering counts every residue across all chains from 1, in the order
    Rosetta reads them, which for these staged inputs is file order.
    """
    out = {}
    pose = 0
    seen = set()
    for ch, key, resseq, _name in _ca_lines(pdb_path):
        ident = (ch, key)
        if ident in seen:
            continue
        seen.add(ident)
        pose += 1
        if ch == chain:
            out[resseq] = pose
    return out


def disulfides(pdb_path, chain='B'):
    """{resSeq: partner_resSeq} from SSBOND records for one chain."""
    pairs = {}
    try:
        with open(pdb_path, errors='replace') as f:
            for line in f:
                if not line.startswith('SSBOND') or len(line) < 36:
                    continue
                if line[15] != chain or line[29] != chain:
                    continue
                try:
                    a, b = int(line[17:21]), int(line[31:35])
                except ValueError:
                    continue
                pairs[a] = b
                pairs[b] = a
    except OSError:
        pass
    return pairs


# ------------------------------------------------------------------- resfile

def write_resfile(path, positions, chain, default='NATRO'):
    """Rosetta resfile from the per-position spec.

    NOTE: pepspec IGNORES resfiles -- verified empirically. This is written only
    when the caller has explicitly asserted resfile_supported or passed
    --allow-unenforced, so the file exists for a protocol that does honour one.

    Exactly ONE command line per listed position; two commands for the same
    position are ambiguous. A mixed alphabet becomes a single EMPTY line naming
    every allowed residue type, because NC takes a type name, not a letter.
    """
    lines = [default, 'start']
    for p in positions:
        if not p.get('design'):
            continue                      # fixed: covered by the NATRO default
        resseq = p['resseq']
        alphabet = [a for a in (p.get('alphabet') or []) if a]
        if not alphabet:
            lines.append(f'{resseq} {chain} ALLAA')
            continue
        one = [a.upper() for a in alphabet if len(a) == 1]
        named = [a.upper() for a in alphabet if len(a) != 1]
        if named:
            types = [ONE_TO_THREE.get(a, a) for a in one] + named
            ordered = list(dict.fromkeys(types))
            lines.append(f'{resseq} {chain} EMPTY ' +
                         ' '.join(f'NC {t}' for t in ordered))
        else:
            lines.append(f'{resseq} {chain} PIKAA {"".join(dict.fromkeys(one))}')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return path


def write_disulf_file(path, pose_pairs):
    """Rosetta fix_disulf file: one pair of POSE indices per line."""
    with open(path, 'w') as f:
        for a, b in pose_pairs:
            f.write(f'{a} {b}\n')
    return path


# --------------------------------------------------------------- flag writing

def flag_lines(flags, pdb_file, anchor_resseq, output_tag, resfile=None,
               extra_res_fa=(), disulf_file=None, detect_disulf=None):
    """One flag file's contents, in the order the bash script emitted."""
    out = []
    if flags.get('ignore_unrecognized_res'):
        out.append('-ignore_unrecognized_res')
    if flags.get('restore_pre_talaris'):
        out.append('-restore_pre_talaris_2013_behavior')
    if flags.get('score_weights'):
        out.append(f'-score::weights {flags["score_weights"]}')
    if flags.get('soft_wts'):
        out.append(f'-pepspec::soft_wts {flags["soft_wts"]}')
    out.append(f'-pepspec::interface_cutoff {flags["interface_cutoff"]}')
    out.append(f'-pepspec::clash_cutoff {flags["clash_cutoff"]}')
    out.append(f'-pepspec::n_peptides {flags["n_peptides"]}')
    out.append(f'-pepspec::n_build_loop {flags["n_build_loop"]}')
    out.append(f'-pepspec:use_input_bb {str(bool(flags["use_input_bb"])).lower()}')
    out.append('-pepspec:gen_pep_bb_sequential '
               f'{str(bool(flags["gen_pep_bb_sequential"])).lower()}')
    out.append('-pepspec::diversify_pep_seqs '
               f'{str(bool(flags["diversify_pep_seqs"])).lower()}')
    for key in OPTIONAL_INT:
        if flags.get(key) is not None:
            out.append(f'-pepspec::{key} {int(flags[key])}')
    out.append(f'-s {pdb_file}')
    if flags.get('rmsd_analysis'):
        out.append('-pepspec:rmsd_analysis true')
    if flags.get('ex1'):
        out.append('-ex1')
    if flags.get('ex2'):
        out.append('-ex2')
    out.append(f'-extrachi_cutoff {flags["extrachi_cutoff"]}')
    out.append(f'-pepspec::pep_chain {flags["pep_chain"]}')
    out.append(f'-pepspec::pep_anchor {anchor_resseq}')
    out.append(f'-pepspec::n_prepend {flags["n_prepend"]}')
    out.append(f'-pepspec::n_append {flags["n_append"]}')
    out.append(f'-pepspec:use_input_bb {str(bool(flags["use_input_bb"])).lower()}')
    for key in OPTIONAL_BOOL:
        if flags.get(key):
            out.append(f'-pepspec::{key} true')

    # Disulfide handling. Mutually exclusive by construction (see main()).
    if disulf_file:
        out.append(f'-in:fix_disulf {os.path.basename(disulf_file)}')
    if detect_disulf is not None:
        out.append(f'-in:detect_disulf {str(bool(detect_disulf)).lower()}')

    for params in extra_res_fa:
        out.append(f'-extra_res_fa {params}')
    if resfile:
        out.append(f'-packing:resfile {os.path.basename(resfile)}')
    out.append(f'-o {output_tag}')
    return out


# ------------------------------------------------------------------------ main

def main():
    # The pipeline runs this as `... 2>&1 | tee -a logfile`, and a piped stdout
    # is block-buffered while stderr is not -- so NOTEs printed before an ERROR
    # appeared after it in the log, which reads as though the run continued past
    # the failure. Line buffering keeps the log in the order things happened.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Positional arguments mirror run_createflag2_2.sh so this is a drop-in swap.
    ap.add_argument('infile1', help="sequence file stem, e.g. 'pepSeq' for pepSeq.txt")
    ap.add_argument('outfile', help="flag file stem, e.g. 'flags2'")
    ap.add_argument('pdbfile', help='PDB base name, for -s inputPDB/<name>.mod.pdb')
    ap.add_argument('job_id', help='prefix for the -o output tag')
    ap.add_argument('--spec', help='design_spec.json from the submission form')
    ap.add_argument('--input-pdb', help='complex PDB for numbering and SSBOND')
    ap.add_argument('--pep-chain',
                    help='chain ID of the peptide, overriding the spec. Set by '
                         'run_pepSpecPipe.sh, which built the input PDB.')
    ap.add_argument('--outdir', default='.', help='where to write the flag files')
    ap.add_argument('--start', type=int,
                    help='first residue number (default: from the PDB, else 1)')
    ap.add_argument('--allow-unenforced', action='store_true',
                    help='emit flags even when the spec asks for constraints '
                         'pepspec is not known to honour')
    args = ap.parse_args()

    spec = {}
    if args.spec:
        try:
            with open(args.spec) as f:
                spec = json.load(f)
        except (OSError, ValueError) as e:
            print(f'ERROR: could not read spec {args.spec}: {e}', file=sys.stderr)
            return 1

    flags = dict(DEFAULT_FLAGS)
    flags.update(spec.get('flags') or {})

    # --pep-chain comes from run_pepSpecPipe.sh, which is what actually built
    # the .mod.pdb being read here, so it outranks the spec: if the two
    # disagree, the spec describes a different structure than the one on disk.
    # Silently trusting the spec would read the wrong chain's residues and
    # anchor against them.
    if args.pep_chain:
        if args.pep_chain not in ('', flags.get('pep_chain')):
            print(f'NOTE: --pep-chain {args.pep_chain} overrides the spec\'s '
                  f'pep_chain {flags.get("pep_chain")!r}; the pipeline built '
                  'this input, so it decides.')
        flags['pep_chain'] = args.pep_chain
    chain = flags.get('pep_chain', 'B')

    # --- peptide: prefer the PDB (numbering, three-letter codes, SSBOND)
    pdb_for_numbering = args.input_pdb or os.path.join(
        args.outdir, 'inputPDB', f'{args.pdbfile}.mod.pdb')
    have_pdb = os.path.exists(pdb_for_numbering)
    residues = chain_residues(pdb_for_numbering, chain) if have_pdb else []
    ss = disulfides(pdb_for_numbering, chain) if have_pdb else {}
    pose_map = pose_index_map(pdb_for_numbering, chain) if have_pdb else {}

    seq_path = os.path.join(args.outdir, f'{args.infile1}.txt')
    one_letter = ''
    if os.path.exists(seq_path):
        with open(seq_path) as f:
            one_letter = ''.join(f.read().split())

    if residues:
        length = len(residues)
        codes = [c for _r, c in residues]
        if args.start is not None:
            resseqs = [args.start + i for i in range(length)]
        else:
            resseqs = [r for r, _c in residues]
    elif one_letter:
        print(f'WARNING: {pdb_for_numbering} not readable; falling back to '
              f'{seq_path} and assuming the peptide starts at '
              f'{args.start or 1}.', file=sys.stderr)
        length = len(one_letter)
        codes = ['UNK'] * length
        resseqs = [(args.start or 1) + i for i in range(length)]
    else:
        print('ERROR: no peptide found — need either a readable input PDB or '
              f'{seq_path}.', file=sys.stderr)
        return 1

    if not one_letter:
        one_letter = ''.join(THREE_TO_ONE.get(c, 'X') for c in codes)

    # --- positions
    spec_positions = {p['resseq']: p for p in (spec.get('positions') or [])
                      if 'resseq' in p}
    positions = []
    for i in range(length):
        rs = resseqs[i]
        p = dict(spec_positions.get(rs, {}))
        p.setdefault('pos', i + 1)
        p['resseq'] = rs
        p.setdefault('design', True)
        p['code'] = codes[i]
        if rs in ss:
            p['disulfide_partner'] = ss[rs]
        positions.append(p)

    mode = spec.get('mode', 'all')
    fixed = [p for p in positions if not p.get('design')]
    restricted = [p for p in positions if p.get('design') and p.get('alphabet')]

    # --- the honesty gate (pepspec ignores resfiles; verified)
    needs_resfile = bool(fixed or restricted)
    resfile_ok = bool(spec.get('resfile_supported')) or args.allow_unenforced
    if needs_resfile and not resfile_ok:
        print('ERROR: this spec asks for per-position constraints '
              f'({len(fixed)} fixed, {len(restricted)} with a restricted '
              'alphabet).\n'
              '       pep_anchor fixes exactly ONE residue per run, so anchors '
              'cannot express a set,\n'
              '       and pepspec IGNORES -packing:resfile (verified on this '
              'build).\n'
              '       Pass --allow-unenforced to generate flags anyway (the '
              'constraints WILL be ignored),\n'
              '       or use post-run filters instead.', file=sys.stderr)
        return 2

    resfile_path = None
    if needs_resfile:
        resfile_path = write_resfile(
            os.path.join(args.outdir, f'{args.outfile}.resfile'), positions, chain)

    # --- disulfide mode
    ss_cfg = spec.get('disulfides') or {}
    ss_mode = ss_cfg.get('mode', 'auto')
    if ss_mode not in DISULFIDE_MODES:
        print(f'ERROR: unknown disulfide mode {ss_mode!r}; expected one of '
              f'{", ".join(DISULFIDE_MODES)}.', file=sys.stderr)
        return 1

    # `ss` is read from the input PDB's SSBOND records -- but clean_pdb.py keeps
    # only ATOM lines, so on the pipeline's own input it is ALWAYS empty. That is
    # why "preserve" reported nothing to fix on a peptide with two real staples,
    # and why those cysteines were then freely designable: without the disulfide
    # variant type the packer has no reason to keep them.
    #
    # The spec carries the pairs as POSITIONS, translated from the uploaded
    # file's numbering at submission (predPEP.py _prepare_spec_for_pdb). That is
    # the only surviving record, so it is the fallback. Positions index THIS
    # file's own residue order, so the mapping back is exact whatever
    # clean_pdb.py renumbered them to.
    spec_pairs = []
    for pair in ((spec.get('peptide') or {}).get('disulfide_positions') or []):
        try:
            pa, pb = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 1 <= pa <= len(resseqs) and 1 <= pb <= len(resseqs):
            spec_pairs.append(tuple(sorted((resseqs[pa - 1], resseqs[pb - 1]))))
        else:
            print(f'WARNING: spec disulfide position pair {pair} is outside '
                  f'chain {chain} (1..{len(resseqs)}); ignored.', file=sys.stderr)
    spec_pairs = sorted(set(spec_pairs))

    ss_pairs_resseq = sorted({tuple(sorted((a, b))) for a, b in ss.items()})
    if not ss_pairs_resseq and spec_pairs:
        ss_pairs_resseq = spec_pairs
        print(f'NOTE: the input PDB has no SSBOND records (clean_pdb.py strips '
              f'them); using the {len(spec_pairs)} disulfide pair(s) recorded in '
              f'the design spec: {[list(p) for p in spec_pairs]}.')
    elif spec_pairs and spec_pairs != ss_pairs_resseq:
        print(f'WARNING: the spec\'s disulfide pairs {[list(p) for p in spec_pairs]} '
              f'disagree with the PDB\'s SSBOND records '
              f'{[list(p) for p in ss_pairs_resseq]}; using the PDB\'s.',
              file=sys.stderr)

    # A pair the submission measured as long is reported again here, because
    # this is where it matters: if it falls outside Rosetta's -detect_disulf
    # cutoff it is lost unless -in:fix_disulf names it explicitly.
    for obs in (spec.get('disulfides_observed') or []):
        if obs.get('state') in ('marginal', 'absent'):
            print(f'WARNING: disulfide at positions {obs.get("positions")} '
                  f'measured {obs.get("sg_distance")} A '
                  f'({obs.get("state")}); a disulfide is 2.05 A. It is being '
                  'declared explicitly, but verify it survives minimisation.',
                  file=sys.stderr)

    # Staples whose two ends are on different chains after a per-segment split.
    # They are REAL and load-bearing -- they are what holds the two strands
    # together -- but -in:fix_disulf is written from the peptide chain's own
    # pose numbering, so an inter-chain pair cannot be expressed in it yet.
    #
    # Said loudly here because the "preserve ... nothing to fix" warning below
    # is true and misleading at the same time: a segment with no INTERNAL
    # disulfides reports nothing to fix while its actual staples go unfixed.
    # Read alone, that line says the opposite of the situation.
    cross = spec.get('cross_segment_disulfides') or []
    if cross:
        print(f'NOTE: {len(cross)} staple(s) on this segment are cross-segment '
              'and are NOT passed to Rosetta as fixed disulfides:', file=sys.stderr)
        for c in cross:
            print(f'        peptide position {c.get("position_in_peptide")} '
                  f'-> chain {c.get("partner_chain")} resSeq '
                  f'{c.get("partner_resseq")}  ({c.get("sg_distance")} A, '
                  f'{c.get("state")})', file=sys.stderr)
        print('      -detect_disulf decides these by distance, so a long one may '
              'not be found at all.\n'
              '      This run designs against a receptor that CONTAINS the other '
              'segment, not one\n'
              '      covalently joined to it. Known limitation, not a problem '
              'with your input.', file=sys.stderr)

    ss_pairs_pose = []
    disulf_file = None
    detect_disulf = None

    if ss_mode == 'preserve':
        if not ss_pairs_resseq:
            print('WARNING: disulfide mode "preserve" requested but the template '
                  f'has no SSBOND record for chain {chain}; nothing to fix.',
                  file=sys.stderr)
        elif not pose_map:
            print('ERROR: disulfide mode "preserve" needs the input PDB to '
                  'compute pose numbering, and it was not readable.',
                  file=sys.stderr)
            return 1
        else:
            # A template can carry an SSBOND record inherited from a parent
            # structure whose residues no longer exist -- seen in the wild: a
            # 5-residue linear peptide whose PDB still declared CYS B 2 - CYS B
            # 7. Aborting on that would refuse to start an entire job over a
            # stale header line, so out-of-range pairs are dropped with a loud
            # warning and the run proceeds. Only a pair that cannot be mapped
            # AT ALL leaves nothing to fix, which is not an error either.
            usable, dropped = [], []
            for a, b in ss_pairs_resseq:
                if a in pose_map and b in pose_map:
                    usable.append((a, b))
                else:
                    dropped.append((a, b))
            if dropped:
                print('WARNING: ignoring disulfide pair(s) '
                      f'{[list(p) for p in dropped]} — residue(s) not present '
                      f'in chain {chain} '
                      f'({resseqs[0]}..{resseqs[-1]}). The template\'s SSBOND '
                      'record does not match its own residues; it is probably '
                      'inherited from a parent structure.', file=sys.stderr)
            if not usable:
                print('WARNING: disulfide mode "preserve" requested but no '
                      'SSBOND pair maps onto this peptide; emitting no '
                      'disulfide flags.', file=sys.stderr)
                ss_pairs_resseq = []
            else:
                ss_pairs_resseq = usable
                ss_pairs_pose = [(pose_map[a], pose_map[b]) for a, b in usable]
            # Only with real pairs: an empty .disulf plus -in:fix_disulf would
            # tell Rosetta to fix nothing, which is not the same as not asking.
            if ss_pairs_pose:
                disulf_file = write_disulf_file(
                    os.path.join(args.outdir, f'{args.outfile}.disulf'),
                    ss_pairs_pose)
    elif ss_mode == 'allow_design':
        # Plain CYS, so make_sequence_change at those positions succeeds. The
        # bond WILL be lost in designs that substitute them.
        detect_disulf = False

    # --- anchors
    #
    # Anchors arrive as 1-based POSITIONS, not resSeq. The submission form reads
    # resSeq out of the file the user uploaded; clean_pdb.py then renumbers every
    # chain from 1, so a resSeq from the spec can name a different residue here,
    # or none at all. Position survives renumbering, and resseqs[] is THIS file's
    # own residue order, so the translation below is exact regardless of what
    # clean_pdb.py did.
    #
    # This used to be `[a for a in anchors if a in resseqs]`: a bare filter that
    # dropped unmatched anchors in silence. On a two-segment peptide numbered
    # 1-21 plus 99-107 that turned four selected cysteines into two flag files
    # with no message, and the two that survived did so only because they
    # happened to fall inside 1..30 -- a surviving stale anchor points at the
    # WRONG residue and the job completes anyway. Both cases are now fatal.
    anchor_pos = spec.get('anchor_positions')
    if anchor_pos:
        out_of_range = [p for p in anchor_pos if not (1 <= p <= len(resseqs))]
        if out_of_range:
            print(f'ERROR: anchor position(s) {sorted(out_of_range)} are outside '
                  f'chain {chain}, which has {len(resseqs)} residues '
                  f'(positions 1..{len(resseqs)}, resSeq '
                  f'{resseqs[0]}..{resseqs[-1]}).\n'
                  '       The design spec and this structure disagree about the '
                  'peptide; refusing rather than\n'
                  '       designing a different set of anchors than was asked '
                  'for.', file=sys.stderr)
            return 1
        anchors = [resseqs[p - 1] for p in anchor_pos]
    elif spec.get('anchors'):
        # spec_version 1: resSeq in the uploaded file's numbering, with no
        # translation available. Only safe when this file uses that same
        # numbering, which is exactly what cannot be assumed -- so any mismatch
        # is fatal instead of filtered.
        legacy = list(spec['anchors'])
        missing = [a for a in legacy if a not in resseqs]
        if missing:
            print(f'ERROR: anchor residue(s) {sorted(missing)} are not in chain '
                  f'{chain} ({resseqs[0]}..{resseqs[-1]}).\n'
                  '       This spec predates anchor_positions, so its numbers '
                  'are in the UPLOADED file\'s\n'
                  '       numbering, which clean_pdb.py has since renumbered. '
                  'Resubmit the job so the\n'
                  '       anchors are translated, rather than running with a '
                  'subset that may point at the\n'
                  '       wrong residues.', file=sys.stderr)
            return 1
        anchors = legacy
        print(f'NOTE: spec has no anchor_positions; using its {len(anchors)} '
              'resSeq anchors, which match this structure exactly.')
    else:
        anchors = [p['resseq'] for p in positions]      # every position

    anchors = sorted(set(anchors))
    if not anchors:
        print(f'ERROR: no anchors to run. Chain {chain} has {len(resseqs)} '
              'residues and the spec selected none of them.', file=sys.stderr)
        return 1

    extra = spec.get('extra_res_fa') or []
    pdb_arg = f'inputPDB/{args.pdbfile}.mod.pdb'

    os.makedirs(args.outdir, exist_ok=True)
    written = []
    for j, anchor in enumerate(anchors, start=1):
        idx = resseqs.index(anchor)
        tag = f'{args.job_id}{one_letter[idx] if idx < len(one_letter) else "X"}{anchor}'
        path = os.path.join(args.outdir, f'{args.outfile}_{j}.txt')
        with open(path, 'w') as f:
            f.write('\n'.join(flag_lines(
                flags, pdb_arg, anchor, tag, resfile_path, extra,
                disulf_file, detect_disulf)) + '\n')
        written.append({'flag_file': os.path.basename(path), 'index': j,
                        'anchor_resseq': anchor, 'position': idx + 1,
                        'residue': codes[idx], 'output_tag': tag})

    # --- manifest: downstream scripts must iterate over THIS, not 1..N
    manifest = {
        'generated_by': 'run_createflags.py',
        'peptide': {
            'chain': chain,
            'start_resseq': resseqs[0],
            'length': length,
            'residues': codes,
            'one_letter': one_letter,
            'disulfides': [list(p) for p in ss_pairs_resseq],
        },
        'mode': mode,
        # Both numberings, so a finished job can be checked against the spec
        # without re-deriving the mapping.
        'anchor_positions': [resseqs.index(a) + 1 for a in anchors],
        'anchor_resseqs': list(anchors),
        'disulfides': {
            'mode': ss_mode,
            'source': ('spec' if (spec_pairs and spec_pairs == ss_pairs_resseq
                                  and not ss) else 'pdb_ssbond' if ss else 'none'),
            'pairs_resseq': [list(p) for p in ss_pairs_resseq],
            # Cross-check these against Rosetta's own log line
            # "Found disulfide between residues X Y".
            'pairs_pose': [list(p) for p in ss_pairs_pose],
            'fix_disulf_file': os.path.basename(disulf_file) if disulf_file else None,
            'detect_disulf': detect_disulf,
        },
        'resfile': os.path.basename(resfile_path) if resfile_path else None,
        'resfile_honoured_by_pepspec': False,   # verified; kept for the record
        'fixed_positions': [p['resseq'] for p in fixed],
        'restricted_positions': [p['resseq'] for p in restricted],
        'constraints_enforced': False if needs_resfile else None,
        'flags': flags,
        'extra_res_fa': list(extra),
        'runs': written,
    }
    try:
        with open(os.path.join(args.outdir, 'flags_manifest.json'), 'w') as f:
            json.dump(manifest, f, indent=2)
    except OSError as e:
        print(f'WARNING: could not write flags_manifest.json: {e}', file=sys.stderr)

    # The anchor list is printed in full rather than as "first..last". It used
    # to read "anchors 1..21", which looks like a contiguous range of 21 anchors
    # but was the first and last of a two-element list -- it sent a reader (and
    # me) looking for 210 missing models that were never expected.
    shown = ', '.join(str(a) for a in anchors[:12])
    if len(anchors) > 12:
        shown += f', ... (+{len(anchors) - 12} more)'
    print(f'Wrote {len(written)} flag file(s) for {len(anchors)} anchor(s) '
          f'[{shown}] (peptide {length} residues starting at {resseqs[0]}).')
    if ss_pairs_resseq:
        shown = ', '.join(f'{a}-{b}' for a, b in ss_pairs_resseq)
        if ss_mode == 'preserve':
            posed = ', '.join(f'{a}-{b}' for a, b in ss_pairs_pose)
            print(f'  disulfide {shown} (chain {chain}) -> pose {posed}: '
                  f'fixed via {os.path.basename(disulf_file)}')
        elif ss_mode == 'allow_design':
            print(f'  disulfide {shown}: detection DISABLED, those positions are '
                  'designable and the bond may be lost.')
        else:
            print(f'  disulfide {shown}: left to Rosetta detection (usually '
                  'preserved, not guaranteed).')
    if resfile_path:
        print(f'  resfile: {os.path.basename(resfile_path)} '
              f'({len(fixed)} fixed, {len(restricted)} restricted) — '
              'NOTE: pepspec ignores resfiles; these constraints will NOT apply.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
