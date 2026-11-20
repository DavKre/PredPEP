#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""NCAA design pass — substitutes non-canonical residues into finished designs.

WHY THIS EXISTS, AND WHY IT IS NOT IN pepspec
---------------------------------------------
pepspec cannot design with non-canonical residues. Three measurements, not
assumptions:

  * -packing:packer_palette:extra_base_type_file CRASHES it. Isolated on a
    5-residue reproducer with one variable changed at a time: params + palette
    died in 84 log lines ("Cannot build coordinates for residue GLY at position
    465: missing too many atoms", Residue.cc:1376); params alone ran a full
    design in 4414 lines; neither ran in 2780. Reproduced on a second complex
    (position 685, 20-residue cyclic peptide) with the same message. It is
    always GLY -- the only canonical with no CB -- which fits the failing
    conversion being NCAA -> GLY inside ResidueFactory::create_residue, whose
    signature carries preserve_c_beta.
  * pepspec's design loop hardcodes chemical::AA(1..20) (pepspec.cc:1293), so
    it cannot be told to include an NCAA or to exclude GLY.
  * pepspec IGNORES -packing:resfile (verified: a NATRO resfile naming one
    designable position still varied eight others), and it applies no
    subtractive task operation.

rosetta_scripts can do all three. Verified present in this build
(r408 / 2025.37): CustomBaseTypePackerPalette, RestrictToSpecifiedBaseResidueTypes,
ProhibitSpecifiedBaseResidueTypes, PackRotamersMover, FastDesign. And
PackRotamersMover's own schema states the division of labour this script relies
on: "packer_palette ... specifies the set of residue types with which to design
(to be pruned with TaskOperations)."

WHERE IT RUNS IN THE PIPELINE, AND WHY THERE
--------------------------------------------
After the job's final iteration, on the models it produced -- NOT inside the
iteration loop. run_iteMAN.py feeds each round's best models back into pepspec
as input, so a model carrying an NCAA would reach pepspec on the next round and
hit the crash above. Placing the pass at the end keeps pepspec from ever seeing
a non-canonical.

The cost of that placement is real and worth stating: the backbone was
optimised for canonical residues, and an NCAA is fitted onto it afterwards. A
bulky side chain may simply not fit. Letting NCAAs influence the backbone
search needs pepspec patched, which is a separate decision -- and this pass is
what tells you whether it is worth making, by showing whether NCAA designs
score at all.

It writes its own table and touches nothing the existing aggregation produces.
That is deliberate: run_catSPEC.py reads Rosetta scores from pepspec's .spec
files and residue names from the model PDBs, so rewriting a model in place
would leave the scores describing the pre-design sequence.
"""

import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

CANONICAL = ['ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 'HIS',
             'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 'THR', 'TRP',
             'TYR', 'VAL']

# GLY is excluded from every palette this script builds.
#
# Two independent reasons. (1) The NCAA -> GLY conversion is what crashes
# pepspec, and until the mechanism is understood it is not worth finding out
# whether rosetta_scripts shares it. (2) The Meiler lab's NCAA macrocycle
# protocol excludes GLY and CYS from its design palette "to aid with
# conformational stability of the design" -- so the published workflow does not
# attempt it either.
EXCLUDE_ALWAYS = ('GLY',)

ROSETTA_ROOT = os.environ.get(
    'PREDPEP_ROSETTA',
    '/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main')
RS_BIN = os.environ.get('PREDPEP_RS_BIN', 'rosetta_scripts.static.linuxgccrelease')
FOLDX_BIN = os.environ.get('PREDPEP_FOLDX', 'foldx_20270131')

# Matches run_createflags.py's DEFAULT_FLAGS, so a design pass scores on the
# same function the rest of the job used.
DEFAULT_WEIGHTS = 'pre_talaris_2013_standard.wts'

# Packing trajectories per design. PackRotamersMover's schema default is 1.
DEFAULT_NLOOP = 10

OUT_CSV = 'tab3_ncaa_designs.csv'
OUT_REPORT = 'ncaa_design_report.json'
OUT_DIR = 'ncaa_designs'


# ------------------------------------------------------------------- the XML

def comment_safe(text):
    """Make a string safe to interpolate into an XML comment.

    XML forbids a double hyphen inside a comment. Anything built from data --
    a palette mode, an interface spec, a residue list -- can carry one, and
    the result is a protocol Rosetta refuses to parse with "Double hyphen
    within comment", once per model. A hand-written comment already cost that
    once; this stops a data-driven one doing it again.
    """
    out = str(text).replace('--', '\u2013')      # en dash, legal in a comment
    return out.replace('>', '\u203a')            # and no accidental comment close


def build_xml(codes, mode, chain, weights, iface='A_B', nstruct_note='',
              nloop=DEFAULT_NLOOP):
    """The design protocol, as a RosettaScripts XML string.

    The palette ADDS the selected non-canonicals to the canonical twenty; the
    task operations then prune that set. Which pruning depends on the palette
    mode, and that is the whole reason this runs here rather than in pepspec.
    """
    pal = ','.join(codes)
    # No codes means the canonical-only control. The attribute is OMITTED
    # rather than set to "", because an empty comma-list is not obviously
    # valid for a string_cslist, and a local XML well-formedness check would
    # not catch its rejection -- that would only surface per model.
    pal_attr = ('\n        additional_residue_types="%s"' % pal) if codes else ''

    # Mode -> what the peptide positions may become.
    if mode == 'l-noncanonical':
        # Only the selected non-canonicals. This is the palette pepspec could
        # never express, because excluding the canonical twenty needs a
        # subtractive task operation.
        prune = ('    <RestrictToSpecifiedBaseResidueTypes name="restrict_types"\n'
                 '        base_types="%s" selector="pep"/>\n' % pal)
        prune_names = ['restrict_types']
    elif mode in ('l-mixed', 'all-mixed', 'd-mixed', 'd-canonical'):
        # Canonicals plus the selected non-canonicals, minus GLY.
        prune = ('    <ProhibitSpecifiedBaseResidueTypes name="no_gly"\n'
                 '        base_types="%s" selector="pep"/>\n'
                 % ','.join(EXCLUDE_ALWAYS))
        prune_names = ['no_gly']
    elif mode == 'l-canonical':
        # Nothing to add; the pass would be a plain repack. Refused in main().
        prune, prune_names = '', []
    else:
        raise ValueError('unsupported palette mode %r' % mode)

    task_list = ','.join(['keep_current', 'extra_rot', 'fix_target'] + prune_names)

    return """<ROSETTASCRIPTS>
  <!-- Generated by run_ncaaDesign.py. %s -->
  <SCOREFXNS>
    <ScoreFunction name="sfx" weights="%s"/>
  </SCOREFXNS>

  <RESIDUE_SELECTORS>
    <!-- The peptide is the only thing designed; the target is held fixed. -->
    <Chain name="pep" chains="%s"/>
    <Not name="target" selector="pep"/>
  </RESIDUE_SELECTORS>

  <PACKER_PALETTES>
    <!-- Additive: the canonical twenty plus these. Pruned below. -->
    <CustomBaseTypePackerPalette name="ncaa_palette"%s/>
  </PACKER_PALETTES>

  <TASKOPERATIONS>
    <IncludeCurrent name="keep_current"/>
    <!-- extrachi_cutoff was 0 until 17 November 2026. That gave ex1 plus
         ex2 extra rotamers to EVERY position with no neighbour threshold,
         across a palette of about 190 residue types. Measured on one
         structure, minimiser present:
           cutoff 0,  pre_talaris weights          741 s
           cutoff 0,  talaris2013_split_unfolded  2700 s
           cutoff 18, pre_talaris weights           42 s
           cutoff 18, talaris2013_split_unfolded    73 s
         The cutoff was therefore nearly all of the pass cost. A 3600 s NCAA
         budget had been completing five structures and reporting nothing,
         which is what made the pass look as though it declined to use
         non-canonicals; it was running out of time, not out of willingness.
         18 is Rosetta's own default: extra rotamers on buried positions,
         where they matter, and not on exposed sidechains that are flexible
         anyway. NCAA placements were unchanged at 8 either way under the
         same weights, so the cutoff does not suppress incorporation.
         Override with the extrachi cutoff flag. -->
    <ExtraRotamersGeneric name="extra_rot" ex1="1" ex2="1" extrachi_cutoff="18"/>
    <OperateOnResidueSubset name="fix_target" selector="target">
      <PreventRepackingRLT/>
    </OperateOnResidueSubset>
%s  </TASKOPERATIONS>

  <MOVERS>
    <!-- nloop is the number of independent packing trajectories; the lowest
         scoring one is kept. The schema default is 1, which is why the first
         run's numbers carried packer noise: single-trajectory simulated
         annealing on a 465 residue pose does not reliably reach the same
         minimum twice, and part of the 0.3 to 0.6 REU spread between repeats
         was that, not chemistry. Cost is linear in nloop but the packer is a
         small fraction of the job, so 10 is cheap. It also makes a repeated
         nstruct near pointless on purpose: converged packing should give the
         same answer each time, and if it does not, that is the signal.
         NOTE no double hyphen and no closing angle bracket anywhere in this
         comment. XML forbids the first and the second ends the comment early;
         writing "nstruct greater than 1" here broke all 45 generated
         protocols until the generator test caught it. -->
    <PackRotamersMover name="design" scorefxn="sfx" nloop="%d"
        packer_palette="ncaa_palette" task_operations="%s"/>
    <!-- Sidechain minimisation after packing. The packer places a side
         chain at a discrete chi, so a bulky substitution can land in a
         strained rotamer that nothing afterwards relieves, and the
         energy then reported is not the energy the force field defines
         for that sequence. Backbone is held fixed, so pepspec binding
         geometry is preserved and dG_separated stays comparable with
         every number measured before this.
         TaskAwareMinMover takes the SAME task_operations as the packer,
         so fix_target definition of what may move is written once. A
         MoveMap here would restate it and could fall out of step.
         This reaches the canonical control too, which comes through
         this same function with an empty palette, so control and design
         are minimised identically and delta_dG_vs_control stays a fair
         comparison. Adding it to build_score_xml instead would make
         delta_dG_vs_parent half corrected, which is worse than leaving
         it plainly biased. -->
    <TaskAwareMinMover name="min" scorefxn="sfx" chi="1" bb="0"
        task_operations="%s"/>
    <!-- The binding metric. dG_separated is the complex minus the separated
         chains: the quantity FoldX AnalyseComplex Total estimates, but
         computed by Rosetta, which knows all of these residues because we
         gave it their params. FoldX has parameters for only five of the 195
         and silently drops the rest, so this is the only ranking metric that
         applies uniformly across the palette, D residues included.
         pack_separated repacks after separation, for a fairer unbound state.
         NOTE no double hyphen anywhere in this comment: XML forbids it, and
         one here made the whole protocol unparseable. -->
    <InterfaceAnalyzerMover name="iface" scorefxn="sfx" interface="%s"
        pack_separated="1" pack_input="0" packstat="0" interface_sc="1"/>
  </MOVERS>

  <PROTOCOLS>
    <Add mover="design"/>
    <Add mover="min"/>
    <Add mover="iface"/>
  </PROTOCOLS>

  <OUTPUT scorefxn="sfx"/>
</ROSETTASCRIPTS>
""" % (comment_safe(nstruct_note), weights, chain, pal_attr, prune,
       int(nloop), task_list, task_list, iface)


def pdb_chains(pdb_path):
    """Chain ids present in a PDB, in file order."""
    seen = []
    try:
        with open(pdb_path, errors='replace') as f:
            for line in f:
                if line.startswith(('ATOM', 'HETATM')) and len(line) > 22:
                    c = line[21]
                    if c not in seen:
                        seen.append(c)
    except OSError:
        pass
    return seen


def interface_spec(pdb_path, pep_chain):
    """InterfaceAnalyzerMover's docking-style interface string, e.g. "A_B".

    Everything that is not the peptide is the target side. Derived from the
    model rather than hardcoded "A_B", because a target with more than one
    chain would otherwise have its other chains counted on the peptide side
    and the binding energy would be wrong without saying so.
    """
    chains = [c for c in pdb_chains(pdb_path) if c.strip()]
    target = [c for c in chains if c != pep_chain]
    if not target or pep_chain not in chains:
        return None
    return '%s_%s' % (''.join(target), pep_chain)


def score_flags(weights):
    """Command-line options a given weights file REQUIRES alongside it.

    Rosetta refuses pre_talaris_2013_standard.wts unless
    -restore_pre_talaris_2013_behavior is also present, and says so before
    exiting:

        To use the 'pre_talaris_2013_standard.wts' score function please use
        the -restore_pre_talaris_2013_behavior flag on the command line.
        ...
        Weights pre_talaris_2013_standard.wts was requested with incompatible
        options.

    The flag changes atom typing and reference energies, not just the weights,
    which is why every pepspec flag file in this pipeline sets the two
    together.

    mm_std needs no companion flag and must NOT be given this one. That matters
    because Rosetta's own NCAA documentation recommends mm_std for
    non-canonicals -- the talaris-family statistical potentials were not
    trained on them -- so --weights mm_std is a plausible thing to want here,
    and hardcoding the restore flag would break it.
    """
    if 'pre_talaris' in weights:
        return ['-restore_pre_talaris_2013_behavior']
    # Checked AFTER pre_talaris: 'pre_talaris_2013_standard' also
    # contains 'talaris', so the order is load-bearing. The
    # talaris-family weights refuse to load without this flag,
    # exactly as the pre-talaris ones refuse without theirs.
    if 'talaris' in weights:
        return ['-restore_talaris_behavior']
    return []


def build_score_xml(weights, iface):
    """InterfaceAnalyzerMover alone, for scoring a model without designing it.

    Used to score each input model so the table can say whether a design
    actually IMPROVED binding. dG_separated on its own is uninterpretable: -8.15
    means nothing until you know what the parent scored.

    Identical score function, interface and pack_separated setting as the
    design protocol, because a delta between two differently-computed numbers
    is not a delta.
    """
    return """<ROSETTASCRIPTS>
  <!-- Generated by run_ncaaDesign.py: baseline scoring, no design. -->
  <SCOREFXNS>
    <ScoreFunction name="sfx" weights="%s"/>
  </SCOREFXNS>

  <MOVERS>
    <InterfaceAnalyzerMover name="iface" scorefxn="sfx" interface="%s"
        pack_separated="1" pack_input="0" packstat="0" interface_sc="1"/>
  </MOVERS>

  <PROTOCOLS>
    <Add mover="iface"/>
  </PROTOCOLS>

  <OUTPUT scorefxn="sfx"/>
</ROSETTASCRIPTS>
""" % (weights, iface)


def score_model(model, xml_path, params, weights, workdir):
    """dG_separated for one model as it stands. None if it could not be scored."""
    sub = os.path.join(workdir, '_baseline', os.path.splitext(
        os.path.basename(model))[0])
    os.makedirs(sub, exist_ok=True)
    shutil.copy(model, os.path.join(sub, os.path.basename(model)))
    cmd = [RS_BIN, '-parser:protocol', os.path.relpath(xml_path, sub),
           '-s', os.path.basename(model), '-nstruct', '1',
           '-out:prefix', 'base_', '-out:file:scorefile', 'score.sc',
           '-database', os.path.join(ROSETTA_ROOT, 'database'),
           '-ignore_unrecognized_res']
    cmd += score_flags(weights)
    for p in params:
        cmd += ['-extra_res_fa', p]
    r = subprocess.run(cmd, cwd=sub, capture_output=True, text=True)
    with open(os.path.join(sub, 'rosetta_scripts.log'), 'w') as lf:
        lf.write(r.stdout + r.stderr)
    for row in read_scorefile(os.path.join(sub, 'score.sc')).values():
        try:
            return float(row['dG_separated'])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def validate_xml(xml_path, params, workdir, weights=DEFAULT_WEIGHTS):
    """Check the protocol before spending a model on it.

    TWO THINGS, because neither alone is enough, and the history here is
    instructive:

    1. WELL-FORMED XML, parsed locally with xml.etree. This catches the class
       of mistake that actually happened: a double hyphen inside an XML comment
       (illegal in XML) made the whole protocol unparseable, and every model
       failed with "Double hyphen within comment".

    2. ROSETTA SCHEMA, by running rosetta_scripts on the protocol. Note that
       -nstruct 0 does NOT do this: with no jobs the parser is never invoked,
       so the XML is never read and the check reports success on a file Rosetta
       cannot parse. That is exactly what happened. There is also no
       -parser:validate_only option in this build. So the schema check needs a
       job, which means it cannot be done without a structure -- and is left to
       the first real model, which the per-model isolation already reports
       cleanly.

    So: XML well-formedness is verified here and schema errors surface on the
    first model. Said plainly rather than claimed as full validation, because a
    check that reports success and then fails is worse than no check.
    """
    try:
        with open(xml_path, errors='replace') as f:
            ET.fromstring(f.read())
    except ET.ParseError as e:
        return False, ('the generated protocol is not well-formed XML: %s\n'
                       'Rosetta would report this once per model. Common '
                       'cause: a double hyphen inside an XML comment.' % e)
    except OSError as e:
        return False, 'could not read %s: %s' % (xml_path, e)
    return True, ''


# ------------------------------------------------------------------- scoring

ROTABASE_CANDIDATES = (
    '/usr/local/foldxpro_20261231/rotabase.txt',
    '/usr/local/foldxLinux64_0/rotabase.txt',
)


def foldx_known_residues():
    """Residue names FoldX has parameters for, read from rotabase.txt.

    THIS IS NOT A FORMALITY. FoldX silently DROPS a residue it does not know:
    on a 5-residue design containing two BTR it reported
    "Complex Analysis went fine" and Total = -1.30, having scored the three
    residues it recognised. The parent scored -7.22. No warning, no error, and
    a number that looks entirely usable.

    That matters beyond this table: run_foldX2.sh greps the same Total out of
    stdout into <PDB_BASE>.all.txt, and parse_and_select_best picks the next
    iteration's parents from it. A FoldX score for a sequence FoldX did not
    evaluate would steer the search.

    rotabase.txt covers the canonical twenty plus roughly a dozen modified
    residues -- PTR, SEP, TPO, OME, SME and a few others -- so a handful of the
    195 are genuinely scorable and the rest are not. Each entry carries
    thousands of lines of rotamer data, so extending it for 195 residues is not
    a hand exercise.

    Returns an empty set when rotabase cannot be read, which the caller treats
    as "cannot verify" and therefore reports no FoldX score at all.
    """
    for path in ROTABASE_CANDIDATES:
        try:
            with open(path, errors='replace') as f:
                text = f.read()
        except OSError:
            continue
        found = set(re.findall(r'^([A-Z]{3})\b', text, re.M))
        if len(found) >= 20:          # sanity: it should at least hold the canonicals
            return found
    return set()


def chain_residues(pdb_path, chain='B'):
    """Chain residue names in order, three-letter, de-duplicated.

    Same element guard as the rest of the pipeline: atom name CA with an
    element column that is C or blank, so calcium is not read as an alpha
    carbon.
    """
    seen = {}
    try:
        with open(pdb_path, errors='replace') as f:
            for line in f:
                if not line.startswith(('ATOM', 'HETATM')) or len(line) < 27:
                    continue
                if line[21:22] != chain or line[12:16].strip() != 'CA':
                    continue
                if line[76:78].strip() not in ('C', ''):
                    continue
                key = line[22:27]
                if key not in seen:
                    seen[key] = line[17:20].strip()
    except OSError:
        return []
    return list(seen.values())


def foldx_total(pdb_path, workdir, chains='A,B'):
    """FoldX interaction energy for one model, or None.

    Replicates what run_foldX2.sh greps for rather than calling it: that script
    is built around pepspec's <tag>.pdbs directory layout and appends to shared
    tables, neither of which fits a one-off model.
    """
    model = os.path.basename(pdb_path)
    try:
        shutil.copy(pdb_path, os.path.join(workdir, model))
    except OSError:
        return None
    r = subprocess.run([FOLDX_BIN, '-c', 'AnalyseComplex', '--pdb=' + model,
                        '--analyseComplexChains=' + chains],
                       cwd=workdir, capture_output=True, text=True)
    best = None
    for line in (r.stdout + r.stderr).splitlines():
        if 'Total' in line and '=' in line:
            m = re.search(r'Total\s*=\s*(-?\d+\.?\d*)', line)
            if m:
                v = float(m.group(1))
                best = v if best is None else min(best, v)
    for junk in glob.glob(os.path.join(workdir, '*.fxout')):
        try:
            os.remove(junk)
        except OSError:
            pass
    try:
        os.remove(os.path.join(workdir, model))
    except OSError:
        pass
    return best


# Columns InterfaceAnalyzerMover adds that are worth carrying into the table.
# dG_separated is the binding energy; the others say whether a good number was
# bought honestly -- buried area, unsatisfied hydrogen bonds, shape
# complementarity.
IFACE_COLUMNS = ('dG_separated', 'dG_separated/dSASAx100', 'dSASA_int',
                 'delta_unsatHbonds', 'hbonds_int', 'sc_value',
                 'nres_int', 'complex_normalized')


def read_scorefile(path):
    """{description: {column: value}} from a rosetta_scripts score file.

    The whole row, not just total_score: InterfaceAnalyzerMover writes its
    results into the same file, and dG_separated is the metric the table ranks
    on for the residues FoldX cannot score.
    """
    out = {}
    try:
        with open(path, errors='replace') as f:
            header = None
            for line in f:
                p = line.split()
                if not p or p[0] != 'SCORE:':
                    continue
                if header is None and 'total_score' in p:
                    header = p
                    continue
                if header:
                    row = dict(zip(header, p))
                    out[row.get('description', '')] = row
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------- main

def pick_models(job_dir, top, chain):
    """The models to design on: top10/ if the job kept it, else the score table.

    top10/ is what survives _post_zip_cleanup, so on a completed job it is the
    only place model PDBs still exist.
    """
    t10 = os.path.join(job_dir, 'top10')
    if os.path.isdir(t10):
        models = sorted(glob.glob(os.path.join(t10, '*.pdb')))
        if models:
            return models[:top], 'top10'

    csv_path = os.path.join(job_dir, 'tab2_final_scores.csv')
    models = []
    rows = []
    try:
        with open(csv_path, newline='', errors='replace') as f:
            rows = list(csv.DictReader(f))
    except OSError:
        # No score table: a job that failed before aggregation. Fall through to
        # the iteration directories below rather than giving up here.
        rows = []
    rows = [r for r in rows if (r.get('FoldX_Score') or '').strip()]
    try:
        rows.sort(key=lambda r: float(r['FoldX_Score']))
    except (KeyError, ValueError):
        pass
    for r in rows[:top]:
        rel = (r.get('pdb_relative_path') or '').strip()
        if not rel:
            continue
        # The recorded path is prefixed with the job directory name.
        cand = os.path.join(job_dir, *rel.split(os.sep)[1:])
        if os.path.exists(cand):
            models.append(cand)
    if models:
        return models, 'tab2_final_scores.csv'

    # Last resort: the iteration directories themselves.
    #
    # A job that FAILED still has every model it produced, because
    # _post_zip_cleanup only runs for 'complete'. So a run that died in the
    # manager or in aggregation -- having done real work first -- can still be
    # designed on, instead of being written off.
    #
    # These are UNRANKED: without the score table there is nothing to sort by,
    # so this takes the newest iteration's models rather than the best. Fine
    # for testing the pass; not what you want for a real result.
    raw = glob.glob(os.path.join(job_dir, '*_iter*', '*.pdbs', '*.pdb'))
    if raw:
        raw.sort(key=lambda p: (os.path.getmtime(p), p), reverse=True)
        return raw[:top], 'iteration directories (unranked)'
    return [], 'none'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('job_dir', help="the job's result directory")
    ap.add_argument('--spec', help='design_spec.json (default: <job_dir>/design_spec.json)')
    ap.add_argument('--chain', default='B', help='peptide chain (default B)')
    ap.add_argument('--top', type=int, default=10, help='models to design on')
    ap.add_argument('--models', nargs='+', metavar='PDB',
                    help='design on THESE model files instead of choosing. '
                         'Use this for a reproducible comparison: without a '
                         'score table the automatic choice falls back to '
                         'modification time, so a job that is still running '
                         'yields a different set on every invocation')
    ap.add_argument('--nstruct', type=int, default=1,
                    help='designs per input model. Default was 5 until 17 '
                         'November 2026, when two structures of the same '
                         'model were found to be bit-identical in their '
                         'ATOM records, differing only in the filename '
                         'echoed in the POSE_ENERGIES_TABLE header. With '
                         'nloop at 10 the packer already runs ten '
                         'trajectories per structure and converges on the '
                         'same optimum, so nstruct above 1 recomputes a '
                         'result it already has. One job spent 3h41m on '
                         'five identical copies of one design and then hit '
                         'a 14400s wall. Raise this only alongside a '
                         'per-structure seed, or lower nloop first.')
    ap.add_argument('--weights', default=DEFAULT_WEIGHTS)
    ap.add_argument('--extrachi-cutoff', type=int, default=18,
                    help='neighbour count above which a position gets ex1 '
                         'plus ex2 extra rotamers; Rosetta default is 18. '
                         'Hardcoded to 0 until 17 November 2026, which gave '
                         'extra rotamers to every position across a palette '
                         'of about 190 types: 741 s per structure under '
                         'pre_talaris weights and about 2700 s under '
                         'talaris2013_split_unfolded, against 42 s and 73 s '
                         'at 18. That was nearly all of the pass cost, and is '
                         'why a 3600 s budget completed five structures and '
                         'reported nothing. Pass 0 to restore the old '
                         'exhaustive sampling.')
    ap.add_argument('--no-minimise', action='store_true',
                    help='omit TaskAwareMinMover from the protocol. Measured '
                         '17 Nov 2026 on one structure: 741s with the '
                         'minimiser under pre_talaris weights, against an '
                         'inferred <=72s without it -- about 10x. '
                         'talaris2013_split_unfolded multiplies that again to '
                         '~2700s, which is how the NCAA pass overran a 14400s '
                         'budget having completed a single design. The '
                         'minimiser buys roughly 0.70 of reference-term '
                         'relief, so the trade is poor. This flag applies to '
                         'the canonical control too: a minimised design '
                         'measured against an unminimised control is a worse '
                         'comparison than no minimisation at all.')
    ap.add_argument('--no-foldx', action='store_true',
                    help='skip FoldX; emit Rosetta scores only')
    ap.add_argument('--canonical-control', action='store_true',
                    help='also run the identical design with an EMPTY palette '
                         '(canonicals only) and report delta_dG_vs_control — '
                         'the only comparison that isolates what the '
                         'non-canonical itself contributed, since the '
                         'parent delta also includes repacking')
    ap.add_argument('--nloop', type=int, default=DEFAULT_NLOOP,
                    help='packing trajectories per design, keeping the best '
                         '(default %d; PackRotamersMover itself defaults to 1, '
                         'which leaves packer noise in the deltas)'
                         % DEFAULT_NLOOP)
    ap.add_argument('--require-improvement', action='store_true',
                    help='omit designs that did not improve binding (verdict '
                         'no_improvement) from the table. The verdict column '
                         'is always written; this only controls whether such '
                         'rows are kept. Off by default, because a design that '
                         'went the wrong way is evidence worth keeping')
    ap.add_argument('--dry-run', action='store_true',
                    help='write and validate the XML, then stop')
    a = ap.parse_args()

    job_dir = os.path.abspath(a.job_dir)
    spec_path = a.spec or os.path.join(job_dir, 'design_spec.json')
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except (OSError, ValueError) as e:
        print('ERROR: could not read %s: %s' % (spec_path, e), file=sys.stderr)
        return 1

    pal = spec.get('palette') or {}
    codes = [str(c) for c in (pal.get('noncanonical') or []) if c]
    mode = pal.get('mode') or ('l-mixed' if codes else 'l-canonical')
    params = list(spec.get('extra_res_fa') or [])

    if not codes:
        print('No non-canonical residues selected for this job; nothing to do.')
        return 0
    if mode == 'l-canonical':
        print('ERROR: palette mode is l-canonical but %d residue(s) are named; '
              'the spec disagrees with itself.' % len(codes), file=sys.stderr)
        return 1
    # The d- modes used to be refused here, because the catalogue emitted
    # PDB-style het codes (DAL, DAR) where Rosetta wants its patch names
    # (DALA, DARG). residue_catalogue.py now emits the patch name as the
    # palette code and keeps the het code separately as pdb_code, so a spec
    # reaching this script already carries Rosetta names and the refusal is
    # obsolete. Confirmed with PeptideStubMover on release-408.

    # Handedness-aware, because a D residue has no params file of its own:
    # Rosetta's D_AA patch flips a type it has ALREADY LOADED, so what must be
    # present is the L parent's params. And a type Rosetta ships needs none at
    # all -- the D-canonicals, and the l-ncaa set it already carries.
    #
    #   DALA              none needed; Rosetta mirrors its own ALA
    #   D0A1              needs 0A1.params, the L parent's
    #   DBiphenylAlanine  none needed; Rosetta supplies BiphenylAlanine
    #
    # Only one case is an error: a code whose params exist in the library but
    # were not loaded. A code with no params anywhere must be one Rosetta
    # supplies, since predPEP.py validated it against the installed catalogue
    # before it reached the spec.
    _loaded = {os.path.basename(p) for p in params}
    _pdirs = {os.path.dirname(p) for p in params if p}

    def _params_ok(code):
        if code + '.params' in _loaded:
            return True
        parent = code[1:] if len(code) > 1 and code[0] == 'D' else None
        if parent and parent + '.params' in _loaded:
            return True
        for d in _pdirs:
            for name in (code, parent):
                if name and os.path.exists(os.path.join(d, name + '.params')):
                    return False      # it IS ours, and it was not loaded
        return True                   # Rosetta's own type

    missing = [c for c in codes if not _params_ok(c)]
    if missing:
        print('ERROR: no params path in extra_res_fa for: %s. Rosetta cannot '
              'resolve a base type whose params were never loaded, and the '
              'failure does not mention the palette.' % ', '.join(missing),
              file=sys.stderr)
        return 2

    work = os.path.join(job_dir, OUT_DIR)
    os.makedirs(work, exist_ok=True)

    # The interface is read off a real model: a target with several chains
    # would otherwise be split across both sides of a hardcoded "A_B" and the
    # binding energy would be wrong without complaining.
    if a.models:
        probe, probe_src = [os.path.abspath(a.models[0])], 'the named model'
    else:
        probe, probe_src = pick_models(job_dir, 1, a.chain)
    iface = (interface_spec(probe[0], a.chain)
             if probe and os.path.exists(probe[0]) else None)
    if iface is None:
        iface = 'A_%s' % a.chain
        print('WARNING: could not read chains from a model; assuming the '
              'interface is %s. Check dG_separated if the target has more '
              'than one chain.' % iface)
    else:
        print('Interface: %s (target_peptide, from %s)' % (iface, probe_src))

    xml = build_xml(codes, mode, a.chain, a.weights, iface,
                    nstruct_note='mode=%s, %d residue(s), interface %s'
                                 % (mode, len(codes), iface),
                    nloop=a.nloop)
    if a.no_minimise:
        # Only the PROTOCOLS entry goes. The TaskAwareMinMover
        # definition stays in MOVERS unused, which RosettaScripts
        # ignores, so the XML needs no other change.
        xml = xml.replace('    <Add mover="min"/>\n', '')
    xml = xml.replace('extrachi_cutoff="18"',
                      'extrachi_cutoff="%d"' % a.extrachi_cutoff)
    xml_path = os.path.join(work, 'ncaa_design.xml')
    with open(xml_path, 'w') as f:
        f.write(xml)
    print('Wrote %s (mode %s, palette %s)' % (xml_path, mode, ','.join(codes)))

    ok, err = validate_xml(xml_path, params, work, a.weights)
    if not ok:
        print('ERROR: Rosetta refused the protocol:\n%s' % err, file=sys.stderr)
        return 1
    print('Protocol is well-formed XML. (Schema errors, if any, '
          'surface on the first model.)')
    if a.dry_run:
        return 0

    # The control protocol: byte-identical design, palette empty. Any
    # difference between it and the real run is attributable to the
    # non-canonicals and nothing else -- same packer, same task operations,
    # same score function, same repacking.
    control_xml = None
    if a.canonical_control:
        # Same nloop as the design pass, deliberately. A delta between a
        # 10 trajectory design and a 1 trajectory control is a delta between
        # two differently-computed numbers, which is the same error
        # build_score_xml exists to avoid.
        ctrl = build_xml([], 'l-mixed', a.chain, a.weights, iface,
                         nstruct_note='CONTROL: canonicals only, no palette',
                         nloop=a.nloop)
        if a.no_minimise:
            ctrl = ctrl.replace('    <Add mover="min"/>\n', '')
        ctrl = ctrl.replace('extrachi_cutoff="18"',
                            'extrachi_cutoff="%d"'
                            % a.extrachi_cutoff)
        control_xml = os.path.join(work, 'canonical_control.xml')
        with open(control_xml, 'w') as f:
            f.write(ctrl)
        ok_c, err_c = validate_xml(control_xml, params, work, a.weights)
        if not ok_c:
            print('WARNING: the control protocol is malformed, so no control '
                  'will be run:\n%s' % err_c)
            control_xml = None
        else:
            print('Control: canonicals only, same protocol otherwise.')

    score_xml = os.path.join(work, 'baseline_score.xml')
    with open(score_xml, 'w') as f:
        f.write(build_score_xml(a.weights, iface))

    if a.models:
        models = [os.path.abspath(x) for x in a.models]
        missing = [x for x in models if not os.path.exists(x)]
        if missing:
            print('ERROR: no such model file(s): %s' % ', '.join(missing),
                  file=sys.stderr)
            return 1
        source = 'named on the command line'
    else:
        models, source = pick_models(job_dir, a.top, a.chain)
    if not models:
        print('ERROR: no model PDBs to design on (looked in top10/ and '
              '%s).' % os.path.join(job_dir, 'tab2_final_scores.csv'),
              file=sys.stderr)
        return 1
    print('Designing on %d model(s) from %s.' % (len(models), source))
    if 'unranked' in source:
        print('   NOTE: with no score table these are the most recently '
              'written models, not the best. If the job is still running the '
              'set changes between invocations, so use --models for a '
              'reproducible comparison.')

    rows = []
    failures = []
    for m in models:
        tag = os.path.splitext(os.path.basename(m))[0]
        sub = os.path.join(work, tag)
        os.makedirs(sub, exist_ok=True)
        shutil.copy(m, os.path.join(sub, os.path.basename(m)))
        cmd = [RS_BIN,
               '-parser:protocol', os.path.relpath(xml_path, sub),
               '-s', os.path.basename(m),
               '-nstruct', str(a.nstruct),
               '-out:prefix', 'ncaa_',
               '-out:file:scorefile', 'score.sc',
               '-database', os.path.join(ROSETTA_ROOT, 'database'),
               '-ignore_unrecognized_res']
        cmd += score_flags(a.weights)
        for p in params:
            cmd += ['-extra_res_fa', p]
        r = subprocess.run(cmd, cwd=sub, capture_output=True, text=True)
        with open(os.path.join(sub, 'rosetta_scripts.log'), 'w') as lf:
            lf.write(r.stdout + r.stderr)
        if r.returncode != 0:
            failures.append({'model': tag, 'exit_code': r.returncode})
            print('  %-28s FAILED exit=%d' % (tag, r.returncode))
            continue

        scores = read_scorefile(os.path.join(sub, 'score.sc'))
        parent_res = chain_residues(m, a.chain)
        # The baseline: what this model scored BEFORE any design. Without it
        # the design's dG_separated cannot be read as better or worse.
        parent_dg = score_model(m, score_xml, params, a.weights, work)
        control_dg = None
        if control_xml:
            csub = os.path.join(work, '_control', tag)
            os.makedirs(csub, exist_ok=True)
            shutil.copy(m, os.path.join(csub, os.path.basename(m)))
            ccmd = [RS_BIN, '-parser:protocol',
                    os.path.relpath(control_xml, csub),
                    '-s', os.path.basename(m), '-nstruct', '1',
                    '-out:prefix', 'ctrl_', '-out:file:scorefile', 'score.sc',
                    '-database', os.path.join(ROSETTA_ROOT, 'database'),
                    '-ignore_unrecognized_res']
            ccmd += score_flags(a.weights)
            for pp in params:
                ccmd += ['-extra_res_fa', pp]
            cr = subprocess.run(ccmd, cwd=csub, capture_output=True, text=True)
            with open(os.path.join(csub, 'rosetta_scripts.log'), 'w') as lf:
                lf.write(cr.stdout + cr.stderr)
            for crow in read_scorefile(os.path.join(csub, 'score.sc')).values():
                try:
                    control_dg = float(crow['dG_separated'])
                    break
                except (KeyError, TypeError, ValueError):
                    continue
        made = sorted(glob.glob(os.path.join(sub, 'ncaa_*.pdb')))
        n_ncaa_total = 0
        for out_pdb in made:
            res = chain_residues(out_pdb, a.chain)
            ncaa = [c for c in res if c not in CANONICAL]
            n_ncaa_total += len(ncaa)
            desc = os.path.splitext(os.path.basename(out_pdb))[0]
            row = scores.get(desc, {})
            entry = {
                'pdbId': os.path.basename(out_pdb),
                'parent': os.path.basename(m),
                'parentResidues': ' '.join(parent_res),
                'pepResidues': ' '.join(res),
                'n_noncanonical': len(ncaa),
                'noncanonical': ' '.join(sorted(set(ncaa))),
                'Rosetta_Total_Score': row.get('total_score', ''),
                'FoldX_Score': '',
                'pdb_relative_path': os.path.relpath(out_pdb,
                                                     os.path.dirname(job_dir)),
            }
            for col in IFACE_COLUMNS:
                entry[col] = row.get(col, '')
            entry['parent_dG_separated'] = ('' if parent_dg is None
                                            else round(parent_dg, 3))
            # Negative delta = the design binds better than its parent.
            #
            # CAVEAT, and it belongs in the column name's reading rather than
            # buried: this delta includes the effect of REPACKING the whole
            # peptide, not only the non-canonical substitution. The design went
            # through the packer; the parent did not. To isolate what the
            # non-canonical itself contributed, run with --canonical-control,
            # which repeats the identical design with the palette empty and
            # reports delta_dG_vs_control.
            try:
                entry['delta_dG_vs_parent'] = round(
                    float(row['dG_separated']) - parent_dg, 3)
            except (KeyError, TypeError, ValueError):
                entry['delta_dG_vs_parent'] = ''
            entry['control_dG_separated'] = ('' if control_dg is None
                                             else round(control_dg, 3))
            # THE number that answers "was the non-canonical worth it".
            # Negative = the palette beat the canonical-only design on the
            # same backbone, through the same packer. Everything else is
            # held constant, so this difference is the non-canonicals.
            try:
                entry['delta_dG_vs_control'] = round(
                    float(row['dG_separated']) - control_dg, 3)
            except (KeyError, TypeError, ValueError):
                entry['delta_dG_vs_control'] = ''

            # THE OBJECTIVE MISMATCH, made visible per row.
            #
            # PackRotamersMover minimises total_score over the whole pose. This
            # table ranks on dG_separated, the interface only. Those are
            # different objectives, so the packer can hand back a design that
            # is better by its own measure and worse at binding. Model 2 of
            # job SPCGRK_b49fce6b did exactly that: +0.238 REU worse than its
            # own parent, despite IncludeCurrent keeping the parent rotamers
            # available. Nothing was broken; it optimised what it was asked to.
            #
            # This verdict does not FIX that -- see the note on
            # --require-improvement -- it stops the table presenting such a
            # design as a result. Judged against the control when there is one,
            # because that isolates the non-canonical's contribution; against
            # the parent otherwise.
            ref = ('delta_dG_vs_control'
                   if entry['delta_dG_vs_control'] != ''
                   else 'delta_dG_vs_parent')
            if entry[ref] == '':
                entry['verdict'] = 'unscored'
                entry['verdict_basis'] = ''
            else:
                entry['verdict'] = ('improved' if float(entry[ref]) < 0
                                    else 'no_improvement')
                entry['verdict_basis'] = ref
            rows.append(entry)
        print('  %-28s %d design(s), %d NCAA placement(s)'
              % (tag, len(made), n_ncaa_total))

    fx_known = set()
    fx_skipped = 0
    if not a.no_foldx:
        fx_known = foldx_known_residues()
        if not fx_known:
            print('WARNING: could not read FoldX rotabase.txt, so there is no '
                  'way to tell which residues FoldX can score. Skipping FoldX '
                  'entirely rather than recording numbers that may describe a '
                  'truncated peptide.')
        else:
            fx_dir = os.path.join(work, '_foldx')
            os.makedirs(fx_dir, exist_ok=True)
            for row in rows:
                unknown = sorted({c for c in row['pepResidues'].split()
                                  if c not in fx_known})
                if unknown:
                    # Deliberately left blank. FoldX WOULD return a number here
                    # -- computed over the residues it recognised, with these
                    # silently dropped -- and that number would be worse than
                    # no number, because it reads as a valid DDG.
                    row['foldx_valid'] = False
                    row['foldx_unknown'] = ' '.join(unknown)
                    fx_skipped += 1
                    continue
                p = os.path.join(os.path.dirname(job_dir),
                                 row['pdb_relative_path'])
                v = foldx_total(p, fx_dir)
                row['foldx_valid'] = True
                row['foldx_unknown'] = ''
                if v is not None:
                    row['FoldX_Score'] = round(v, 3)
            shutil.rmtree(fx_dir, ignore_errors=True)

    # Rank on dG_separated: it is the only metric present for EVERY design,
    # since FoldX can score just five of the 195. Deliberately NOT a fallback
    # chain through FoldX first -- dG_separated and FoldX ddG are different
    # quantities on different scales, and sorting one table by whichever
    # happened to be available would order designs by which tool scored them.
    # FoldX stays a column, for the five where it is valid.
    # TIERED, not a fallback chain. A design with no dG_separated but a
    # total_score of -500 must not outrank a real binding energy of -12: those
    # are different quantities and -500 is the whole 465-residue pose, not the
    # interface. So designs that HAVE a binding energy are ranked among
    # themselves first, then the rest by total_score, then the unscored.
    def key(r):
        try:
            return (0, float(r['dG_separated']))
        except (TypeError, ValueError, KeyError):
            pass
        try:
            return (1, float(r['Rosetta_Total_Score']))
        except (TypeError, ValueError, KeyError):
            pass
        return (2, 0.0)
    rows.sort(key=key)

    # --require-improvement: keep only designs that actually improved binding.
    #
    # OFF by default, because dropping rows destroys the evidence that a design
    # went the wrong way, and that evidence is how the mismatch above was found
    # in the first place. With it on, the dropped count is printed and the
    # reason recorded in the report, so the rows are never silently gone.
    dropped = 0
    if a.require_improvement:
        keep = [r for r in rows if r.get('verdict') != 'no_improvement']
        dropped = len(rows) - len(keep)
        rows = keep

    cols = (['pdbId', 'parent', 'parentResidues', 'pepResidues',
             'n_noncanonical', 'noncanonical', 'verdict', 'verdict_basis',
             'delta_dG_vs_control', 'delta_dG_vs_parent',
             'control_dG_separated', 'parent_dG_separated']
            + list(IFACE_COLUMNS)
            + ['Rosetta_Total_Score', 'FoldX_Score', 'foldx_valid',
               'foldx_unknown', 'pdb_relative_path'])
    out_csv = os.path.join(job_dir, OUT_CSV)
    with open(out_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in cols})

    placed = sum(1 for r in rows if r['n_noncanonical'])
    report = {
        'job': os.path.basename(job_dir),
        'interface': iface,
        'ranked_on': 'dG_separated (InterfaceAnalyzerMover); FoldX ddG is a '
                     'separate column, valid for the few residues FoldX has '
                     'parameters for, and is NOT on the same scale',
        'palette_mode': mode,
        'palette': codes,
        'excluded_always': list(EXCLUDE_ALWAYS),
        'models_designed': len(models),
        'model_source': source,
        'nstruct': a.nstruct,
        'nloop': a.nloop,
        'require_improvement': a.require_improvement,
        'designs_dropped_no_improvement': dropped,
        'designs_produced': len(rows),
        'designs_with_noncanonical': placed,
        'failures': failures,
        'weights': a.weights,
        'foldx': not a.no_foldx,
        'foldx_known_noncanonical': sorted(fx_known - set(CANONICAL)),
        'foldx_skipped_designs': fx_skipped,
    }
    with open(os.path.join(job_dir, OUT_REPORT), 'w') as f:
        json.dump(report, f, indent=2)

    print()
    print('%d design(s) from %d model(s); %d contain a non-canonical residue.'
          % (len(rows), len(models), placed))
    if dropped:
        print('%d design(s) OMITTED by --require-improvement: they did not '
              'improve binding against their reference. The PDBs are still on '
              'disk; only the table excludes them.' % dropped)
    if fx_skipped:
        print('%d design(s) have NO FoldX score: they contain residues FoldX '
              'has no parameters for, and it drops those silently rather than '
              'failing. Rank those on Rosetta_Total_Score (or interface_score '
              'in the per-model score.sc) instead. FoldX can score: %s.'
              % (fx_skipped, ' '.join(sorted(fx_known - set(CANONICAL))) or
                 '(canonicals only)'))
    scored = [r for r in rows if r.get('delta_dG_vs_parent') != '']
    better = [r for r in scored if float(r['delta_dG_vs_parent']) < 0]
    if scored:
        print('%d of %d design(s) bind better than their parent '
              '(delta_dG_vs_parent < 0).' % (len(better), len(scored)))
        print('   That delta includes repacking the whole peptide, not just '
              'the substitution.')
        ctrl_scored = [r for r in rows if r.get('delta_dG_vs_control') != '']
        if ctrl_scored:
            cb = [r for r in ctrl_scored
                  if float(r['delta_dG_vs_control']) < 0]
            print('%d of %d beat the canonical-only control on the same '
                  'backbone (delta_dG_vs_control < 0) — that one is '
                  'attributable to the non-canonicals.'
                  % (len(cb), len(ctrl_scored)))
        else:
            print('   Run with --canonical-control to find out what the '
                  'non-canonical itself contributed.')
    if rows and not placed:
        # The honest reading, said plainly: the pass ran and the packer
        # preferred canonicals everywhere. That is a result, not a failure, and
        # it is the answer to "is it worth patching pepspec".
        print('NOTE: every design came back fully canonical. The palette was '
              'available and the packer did not choose any non-canonical -- '
              'either they score worse here, or the backbone leaves no room '
              'for them. Fitting NCAAs onto a backbone optimised for '
              'canonicals is the known limitation of running this pass after '
              'the iterations rather than inside them.')
    if failures:
        print('%d model(s) failed; see ncaa_designs/<tag>/rosetta_scripts.log'
              % len(failures))
    print('Table: %s' % out_csv)
    return 0


if __name__ == '__main__':
    sys.exit(main())
