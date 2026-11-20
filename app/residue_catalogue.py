#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""The residue catalogue the design palette picker offers, and its validation.

WHAT THIS IS FOR
----------------
The picker lets someone choose which non-canonical residues a design job may
use. Those codes end up on a Rosetta command line (in the file named by
-packing:packer_palette:extra_base_type_file), so they are untrusted input and
are allowlisted here against what is actually installed, exactly as the rest of
the design spec is.

WHY THE CATALOGUE IS AN INTERSECTION
------------------------------------
all-residues-list-complete.csv lists 282 residues; we have working params for
195 of them. Offering a residue with no params produces a job that dies at
Rosetta's load step with nothing in the UI to explain why, so the catalogue is
the INTERSECTION of the CSV and the installed params directory. Measured
breakdown:

    CATEGORY                          csv  params  rotlib
    L-canonical                        20       0       0   <- Rosetta's own
    L-noncanonical (by name)          142     129     116
    L-noncanonical (struct match)      68      66      60
    L-noncanonical (new)               20       0       0   <- no .rtp to build from
    bridge anchor (acid acceptor)      17       0       0   <- linkers, not residues
    bridge anchor (amine donor)         6       0       0
    D-in library, not yet in the list   1       0       0

The canonical 20 legitimately have no params of ours: Rosetta supplies them,
and generating ours would collide with types it already loads.

THE CANONICAL SECTION IS READ-ONLY, AND WHY
-------------------------------------------
Rosetta's CustomBaseTypePackerPalette is ADDITIVE. Its get_base_residue_types()
always calls set_up_default_base_types() first (the canonical 20) and then adds
the types named in the file; there is no option to omit the defaults, and the
XSD text says it "expands the default (canonical) residue type set".

D RESIDUES NEED NO PARAMS OF THEIR OWN
--------------------------------------
Rosetta mirrors them itself. Its patches.txt ends with

    ## The D_AA and RPEPTOID patches must be last in this list to ensure
    ## that mirrored base types are created.
    patches/D_AA.txt

and that patch selects on a PROPERTY, not on a directory:

    NAME D
    BEGIN_SELECTOR
    PROPERTY L_AA
    END_SELECTOR
    CHIRAL_FLIP_NAMING
    CHIRAL_FLIP_ATOMS

A property selector does not care where a ResidueType came from, so every type
loaded through -extra_res_fa is mirrored on the same terms as Rosetta's own.
Measured: all 195 of our params files declare `PROPERTIES PROTEIN ALPHA_AA
L_AA` (109 of them also AROMATIC), so the patch already fires on the whole set.
CHIRAL_FLIP_NAMING is what makes the mirror a BASE type named `DALA` rather
than a patched `ALA:D`, and the same rule gives `D004` for an NCAA.

Three consequences shape the code below.

  1. THE D-CANONICALS ARE SELECTABLE, unlike the L-canonicals. The default
     palette set up by set_up_default_base_types() is the twenty L residues;
     DALA is not among them. So a D-canonical must be named in the
     extra_base_type_file to be available, which puts it in the offerable list
     and not in the read-only canonical section.

  2. SELECTING A D-NONCANONICAL STILL REQUIRES THE L PARAMS. The mirror is
     derived from a loaded L type; if 004.params is not passed to
     -extra_res_fa there is no 004 to flip, and D004 does not exist. So the
     palette name and the params file are no longer the same string, and
     params_paths() maps the one to the other through `params_for`.

  3. A D RESIDUE HAS NO VALID FoldX NUMBER, and its failure mode is worse than
     an unknown L residue's. rotabase.txt is keyed on L names. If the mirror's
     name3 comes through as the L parent's, FoldX will not drop the residue --
     it will score it AS the L form and return a plausible number for the wrong
     molecule. Dropping is detectable; this is not. Hence foldx is False for
     every D entry regardless of what rotabase contains, and
     PREDPEP_D_RESIDUES=0 turns the whole D half off at the catalogue.

Glycine needs no special case: it is achiral, and the CSV already carries an
empty D cell for it, so the mirror is emitted only where the CSV names one.

Subtracting a type needs a TaskOperation, and pepspec pushes only
InitializeFromCommandline, IncludeCurrent, RestrictResidueToRepacking and
PreventRepacking -- nothing subtractive -- and it ignores resfiles. So a
checkbox that unticked CYS would appear to work and silently do nothing.
Rather than ship that, the canonical section is presented as always-available
with a note. It becomes interactive when the design stage can apply a
subtractive TaskOperation (a second rosetta_scripts stage, or a patched
pepspec).
"""

import os
import re
import csv
import threading

# A residue code as it appears in the catalogue CSV and as the stem of one of
# OUR params files. Still the only pattern allowed anywhere a filesystem path
# is built, which is why it stays this tight.
CODE_RE = re.compile(r'^[A-Za-z0-9]{1,4}$')

# A Rosetta ResidueType NAME, which is what the packer palette file actually
# contains. Usually the three-letter code, but not always: the type behind
# l-ncaa/4-phenyl-phenylalanine_tyr_rot.params is called `BiphenylAlanine`,
# and CODE_RE rejects it. Widened only for names that go INTO the palette
# file; membership in the catalogue remains the real gate, and no path is ever
# built from a string matched by this pattern.
TYPE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$')

# ---------------------------------------------------------------------------
# Residues Rosetta already ships, which we must NOT also load.
#
# Measured from ROSETTA_CRASH.log: 433 crashes, 416 of them a single cause --
#
#   ERROR: ... ResidueTypeSetCache::add_residue_type(): Attempting to add a
#   new residue type, but residue type 'HLU' already exists in the cache.
#   (Did you load a .params file with the -extra_res_fa commandline option
#   that was already listed in residue_types.txt, perhaps?)
#
# So these never failed to BUILD; the run died because our name collided with
# a type already in the default set. They are offerable with no params of ours
# at all, exactly like the canonical twenty -- the value here is the Rosetta
# NAME to write into the palette, which is not always the code we know it by.
#
# All eight are listed in residue_types.txt (so loaded by default) and all
# declare PROTEIN ALPHA_AA L_AA, so each also gets a D mirror from the D_AA
# patch. Verified against release-408.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ...BUT THIS PIPELINE CANNOT PLACE THEM.
#
# residue_types.txt lists the l-ncaa set, which is why the eight below were
# recoverable in principle. The DESIGN protocol does not load that set: it runs
# -restore_pre_talaris_2013_behavior with pre_talaris_2013_standard.wts, which
# reads residue_types.txt.slim -- and that file contains ZERO l-ncaa entries
# (measured: `grep -c l-ncaa residue_types.txt.slim` -> 0). A run under it
# creates 76 residue types rather than 100, and the palette dies:
#
#   ERROR: Error in core::pack::palette::PackerPalette::add_base_residue_type():
#   No base type corresponding to "DBiphenylAlanine" could be found.
#     src/core/pack/palette/PackerPalette.cc:295
#
# Measured on the first real run of the NCAA design pass with D residues.
# DALA and D0A1 resolved in the same palette; only the l-ncaa-derived name
# failed, and Rosetta reports the first failure, so the other two had already
# been accepted.
#
# So these eight and their mirrors are WITHHELD. Offering them would hand the
# user sixteen residues that fail at the palette and take the whole design pass
# down with them. PREDPEP_ALLOW_ROSETTA_NCAA=1 offers them anyway, for a
# protocol that does not restore pre-talaris behaviour.
#
# This is also a lesson about where to verify: residue_types.txt said these
# existed, and they do -- just not in the type set the pipeline actually uses.
# ---------------------------------------------------------------------------
def allow_rosetta_ncaa():
    return os.environ.get('PREDPEP_ALLOW_ROSETTA_NCAA', '0').strip().lower() \
        in ('1', 'yes', 'true', 'on')


ROSETTA_SUPPLIED = {
    'ABA': 'ABA',               # l-ncaa/ABA.params
    'BB8': 'BB8',               # l-ncaa/phenyl-serine.params
    'BCS': 'BCS',               # l-ncaa/BCS.params
    'BIF': 'BiphenylAlanine',   # l-ncaa/4-phenyl-phenylalanine_tyr_rot.params
    'DAB': 'DAB',               # l-ncaa/2.4-diaminobutyric_acid.params
    'DPP': 'DPP',               # l-ncaa/2.3-diaminopropionic_acid.params
    'IGL': 'IGL',               # l-ncaa/2-indanyl-glycine_puck1.params
                                #   (puck2 is a separate type named A45)
    'ORN': 'ORN',               # l-ncaa/ornithine.params
}

# ---------------------------------------------------------------------------
# Codes where Rosetta has the same three letters for a DIFFERENT MOLECULE.
#
# These are not resolvable by mapping to Rosetta's type: doing so would offer
# the residue our CSV describes and design the one Rosetta has, with no error
# anywhere and a plausible number in the results table. The collision is the
# reason our params could not load, so each needs OUR params under a code
# Rosetta does not use. Until then they are offered in neither handedness.
#
# Identified from the PROPERTIES field of Rosetta's own params, except HLU,
# which needed its atom list: N CA C O CB CG CD CE1 CE2 and their hydrogens --
# one oxygen, the backbone carbonyl, and a five-carbon side chain. That is
# homoleucine. beta-hydroxyleucine has a side-chain oxygen and would carry
# POLAR, which Rosetta's HLU does not.
# ---------------------------------------------------------------------------
PENDING_BUILD = {
    '400': 'renamed from 004 (phenylglycine) to clear a Rosetta name '
           'collision; params not built yet',
    'TOM': 'renamed from MOT (5-methoxy-tryptophan) to clear a Rosetta name '
           'collision; params not built yet',
    'SAO': 'renamed from OAS (o-acetylserine) to clear a Rosetta name '
           'collision; params not built yet',
    'HPM': 'renamed from MPH (2-methyl-phenylalanine) to clear a Rosetta '
           'name collision; params not built yet',
    'ULH': 'renamed from HLU (beta-hydroxyleucine) to clear a Rosetta name '
           'collision; params not built yet',
}
# Self-clearing: `code in have` is tested before this map, so the moment
# 400.params lands in the params directory the residue becomes offerable and
# is never looked up here again. Nothing has to be edited to retire an entry.


NAME_COLLISIONS = {
    '004': 'ours is phenylglycine; Rosetta peptoids/004.params is an '
           'N-substituted glycine',
    'MOT': 'ours is 5-methoxy-tryptophan; Rosetta '
           'nucleic/rna_nonnatural/MOT.params is an RNA residue',
    'OAS': 'ours is o-acetylserine; Rosetta polyaramids/OAS.params is a '
           'polyaramid',
    'MPH': 'ours is 2-methyl-phenylalanine; Rosetta l-ncaa/MPH.params is a '
           'PHOSPHONATE (METALBINDING, ALIPHATIC, not AROMATIC)',
    'HLU': 'ours is beta-hydroxyleucine; Rosetta l-ncaa/HLU.params is '
           'homoleucine (five-carbon side chain, no side-chain oxygen)',
}

CANONICAL_CATEGORY = 'L-canonical'
# Categories whose members are offerable as design residues, in display order.
# Omitted deliberately: "L-noncanonical (new)" (no .rtp, so no params), the two
# "bridge anchor" categories (staple and linker components, not residues for an
# ordinary designed position), and the single "D-in library" stray.
NONCANONICAL_CATEGORIES = (
    'L-noncanonical (by name)',
    'L-noncanonical (struct match)',
)

# The mirror of CANONICAL_CATEGORY. Not read-only: see note 1 in the docstring.
D_CANONICAL_CATEGORY = 'D-canonical'

# Display order for the picker's groups. The L groups come first so an existing
# user's muscle memory is undisturbed; the D groups are appended.
GROUP_ORDER = (
    NONCANONICAL_CATEGORIES[0],
    NONCANONICAL_CATEGORIES[1],
    D_CANONICAL_CATEGORY,
    'D-noncanonical (by name)',
    'D-noncanonical (struct match)',
)


def d_residues_enabled():
    """PREDPEP_D_RESIDUES=0 removes the D half of the catalogue entirely.

    A kill switch rather than a feature flag: if the mirror's name3 turns out
    to collide with the L parent's (see note 3), every D design silently
    acquires a wrong FoldX score, and the only safe response is to stop
    offering them until the pipeline can tell the two apart.
    """
    return os.environ.get('PREDPEP_D_RESIDUES', '1').strip().lower() \
        not in ('0', 'no', 'false', 'off')


def d_category(l_category):
    """The mirror's display category. 'L-' prefixes become 'D-'."""
    if l_category == CANONICAL_CATEGORY:
        return D_CANONICAL_CATEGORY
    if l_category.startswith('L-'):
        return 'D-' + l_category[2:]
    return 'D-' + l_category


def d_rosetta_name(row, l_name):
    """The ResidueType name Rosetta's CHIRAL_FLIP_NAMING produces, or ''.

    `l_name` is the L type's PALETTE name, not the CSV code: the mirror of
    BiphenylAlanine is DBiphenylAlanine, not DBIF.

    '' means this residue has no mirror and none should be offered -- glycine
    is achiral and the CSV says so with an empty cell.

    An explicit D_ROSETTA column wins if the catalogue file carries one, so a
    rebuilt file can correct any residue whose generated name is not simply
    'D' + the L code, without touching this module.
    """
    explicit = (row.get('D_ROSETTA') or '').strip()
    if explicit:
        return explicit if TYPE_NAME_RE.match(explicit) else ''
    # No explicit column: a mirror exists iff the CSV carries a CODE for one.
    # That value is a PDB het code (DAL, 04D), not a Rosetta name, so it
    # decides existence only -- never the name.
    #
    # D_NAME is deliberately NOT consulted. It is a display label and carries
    # placeholders: glycine's row reads D_FOLDER '' with D_NAME '---', so
    # treating a non-empty D_NAME as evidence of a mirror invents DGLY for an
    # achiral residue.
    if not ((row.get('D_PDB') or '').strip()
            or (row.get('D_FOLDER') or '').strip()):
        return ''
    name = 'D' + l_name
    return name if TYPE_NAME_RE.match(name) else ''


def d_pdb_code(row, het_map=None):
    """The 3-character name the mirror should carry in a PDB file, or ''.

    Rosetta's own D-canonicals use DAL, DAR, DSG -- which is exactly what the
    legacy D_FOLDER column holds, so that column is already the right source
    for 222 of 231 rows. The eight that hold a 4-character value (DGME, DHGA,
    DME0, DMEG, DMEN, DMEQ, DMHO, DOAS) have no het code assigned; 4 characters
    cannot fit columns 18-20 of an ATOM record, so they are reported as absent
    rather than silently truncated.
    """
    code = (row.get('D_PDB') or row.get('D_FOLDER') or '').strip()
    if len(code) == 3:
        return code
    # A 4-character value means no het code had been assigned when the column
    # was written. residue_names.csv carries the assignment that was made
    # later; see load_d_het_codes.
    return (het_map or {}).get(code, '')

DEFAULT_RETIRED = '/opt/spacepep-createFF/all_aas3/retired_residues.csv'
DEFAULT_D_NAMES = '/opt/spacepep-createFF/all_aas3/residue_names.csv'

DEFAULT_CSV = '/opt/spacepep-createFF/all_aas3/all-residues-list-complete.csv'
DEFAULT_PARAMS = '/opt/spacepep-createFF/all_aas3/generated_params_rotlib'
DEFAULT_ROTLIBS = '/opt/spacepep-createFF/all_aas3/generated_rotlibs'


ROTABASE_CANDIDATES = (
    '/usr/local/foldxpro_20261231/rotabase.txt',
    '/usr/local/foldxLinux64_0/rotabase.txt',
)


def foldx_scorable():
    """Residue names FoldX has parameters for, read from its rotabase.txt.

    WHY THE PICKER NEEDS THIS. FoldX DdG is this pipeline's primary metric: it
    drives run_foldX2.sh, parse_and_select_best's choice of the next
    iteration's parents, and the ranking in the results table. FoldX does not
    fail on a residue it does not know -- it DROPS it. Measured: a 5-residue
    design carrying two BTR scored Total = -1.30 and reported "Complex
    Analysis went fine", having silently evaluated the three residues it
    recognised; the all-canonical parent scored -7.22.

    So a residue outside this set can be designed in and scored by Rosetta,
    but has no valid FoldX number, and anything that ranks on FoldX must leave
    it out rather than take what FoldX returns.

    Measured on this node: 27 names, of which 7 are non-canonical (END, PHI,
    PSI, PTR, SEP, SME, TPO) and 5 of those are in our set -- PHI, PTR, SEP,
    SME, TPO. Each rotabase entry carries thousands of lines of rotamer data,
    so extending it to cover 195 residues is not a hand exercise.

    BUT THREE OF THOSE FIVE ARE RETIRED. retired_residues.csv withdraws PTR,
    SEP and TPO as phosphate MONOanions where the dianion is the species at
    pH 7.4, and FoldX can score them only because they are the older, commoner
    codes; the keepers that replace them (PT2, SE2, TO2) are dianions rotabase
    has never seen. So with the retirement enforced the honest FoldX-scorable
    set is PHI and SME, and the correct phosphorylated species cannot be
    FoldX-ranked at all. Anything that presents "FoldX-scorable" as a reason
    to prefer a residue must not quietly steer a design toward the wrong
    protonation state.

    An empty set means rotabase could not be read, and the caller should treat
    every residue as unverifiable rather than assume either answer.
    """
    for path in ROTABASE_CANDIDATES:
        try:
            with open(path, errors='replace') as f:
                text = f.read()
        except OSError:
            continue
        found = set(re.findall(r'^([A-Z]{3})\b', text, re.M))
        if len(found) >= 20:
            return found
    return set()


def allow_retired():
    """PREDPEP_ALLOW_RETIRED=1 offers retired codes anyway.

    Default is to withhold. Measured on this node: of the fifteen codes in
    retired_residues.csv, six are offerable (SEP, TPO, PTR, 2H1, 2HF, MHO)
    and ALL SIX keepers also have params, so enforcing the retirement removes
    no chemistry -- it removes a choice between two codes where one of them is
    the wrong species at pH 7.4.
    """
    return os.environ.get('PREDPEP_ALLOW_RETIRED', '0').strip().lower() \
        in ('1', 'yes', 'true', 'on')


def load_retired(path=None):
    """-> {code: (keep, reason)} from retired_residues.csv.

    These are createFF's own decisions, not ours, and the reasons are
    chemical: SEP/TPO/PTR are phosphate MONOanions where the dianion (SE2,
    TO2, PT2) dominates at physiological pH; DAP is a neutral side-chain amine
    where the protonated DPP is the species; 2H1/2HF are the wrong
    fluorohistidine tautomer and charge state; MHO is a surplus NAME for SME,
    identical atom for atom.

    That reasoning applies to a Rosetta design exactly as it does to a GROMACS
    ensemble, which is why predPEP honours it rather than merely recording it.
    A missing or unreadable file yields {} and nothing is withheld -- the same
    degradation the rest of this module uses.
    """
    path = path or os.environ.get('PREDPEP_RETIRED_CSV', DEFAULT_RETIRED)
    out = {}
    try:
        with open(path, newline='', encoding='utf-8', errors='replace') as f:
            for row in csv.DictReader(f):
                c = (row.get('code') or '').strip()
                if c and CODE_RE.match(c):
                    out[c] = ((row.get('keep') or '').strip(),
                              (row.get('reason') or '').strip())
    except OSError:
        pass
    return out


def load_d_het_codes(path=None):
    """-> {four_char_name: three_char_het_code} from residue_names.csv.

    Eight D residues in the catalogue CSV carry a 4-character D_FOLDER value
    (DGME, DHGA, DME0, DMEG, DMEN, DMEQ, DMHO, DOAS) because no het code had
    been assigned when that column was written. They have since been assigned
    -- MGD, GAD, E0D, EGD, END, EQD, HOD, ASD -- and residue_names.csv records
    the mapping, keyed the other way round: `code` is the new het code and
    `was` is the 4-character name our CSV still holds.

    Read from the file rather than hardcoded, so a reassignment does not need
    a code change. Note residue_names.csv's END is the D form of
    n-methyl-asparagine and has nothing to do with rotabase's END; no FoldX
    lookup may go through a D code (every D entry is foldx False).
    """
    path = path or os.environ.get('PREDPEP_D_NAMES_CSV', DEFAULT_D_NAMES)
    out = {}
    try:
        with open(path, newline='', encoding='utf-8', errors='replace') as f:
            for row in csv.DictReader(f):
                het = (row.get('code') or '').strip()
                was = (row.get('was') or '').strip()
                if len(het) == 3 and was and CODE_RE.match(het):
                    out[was] = het
    except OSError:
        pass
    return out


def _installed(params_dir, rotlib_dir):
    """-> (codes with params, codes with a rotamer library)."""
    def listing(d, suffix):
        try:
            return {fn[:-len(suffix)] for fn in os.listdir(d)
                    if fn.endswith(suffix)}
        except OSError:
            return set()
    return (listing(params_dir, '.params'), listing(rotlib_dir, '.rotlib'))


def load_catalogue(csv_path=None, params_dir=None, rotlib_dir=None):
    """-> the catalogue the picker renders, as plain JSON-able data.

    Never raises on a missing file: a catalogue that cannot be read comes back
    empty with the reason in 'error', so the submission form degrades to
    canonical-only design rather than failing to load.
    """
    csv_path = csv_path or os.environ.get('PREDPEP_RESIDUE_CSV', DEFAULT_CSV)
    params_dir = params_dir or os.environ.get('PREDPEP_PARAMS_DIR', DEFAULT_PARAMS)
    rotlib_dir = rotlib_dir or os.environ.get('PREDPEP_ROTLIB_DIR', DEFAULT_ROTLIBS)

    out = {'canonical': [], 'noncanonical': [], 'groups': [],
           'collisions': [], 'pending': [], 'retired': [],
           'unavailable': [],
           'params_dir': params_dir, 'error': None,
           'canonical_editable': False,
           'canonical_note': (
               "Rosetta's packer palette can only ADD residue types to the "
               "canonical twenty, never remove them, and pepspec applies no "
               "task operation that could. All twenty are therefore always "
               "available for design. Narrowing them needs the design stage "
               "to move to a protocol that can subtract types.")}

    try:
        with open(csv_path, newline='', encoding='utf-8', errors='replace') as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        out['error'] = 'could not read the residue catalogue: %s' % e
        return out

    needed = ('CATEGORY', 'L_FOLDER', 'RESIDUE_DESCRIPTIVE_NAME')
    if not rows or any(k not in rows[0] for k in needed):
        out['error'] = ('residue catalogue is missing one of the expected '
                        'columns %s' % ', '.join(needed))
        return out

    have, libs = _installed(params_dir, rotlib_dir)
    fx = foldx_scorable()

    want_d = d_residues_enabled()
    out['d_residues'] = want_d
    retired = load_retired()
    permit_retired = allow_retired()
    permit_rosetta_ncaa = allow_rosetta_ncaa()
    het_map = load_d_het_codes()
    out['retired_enforced'] = bool(retired) and not permit_retired

    # Palette name -> the code whose .params must reach -extra_res_fa, or None
    # when Rosetta supplies the type itself. A D noncanonical maps to its L
    # PARENT: the mirror is flipped from a type Rosetta has already loaded, so
    # without the L params there is nothing to flip. This is the one place the
    # palette name and the params file stop being the same string.
    params_for = {}

    seen = set()
    for row in rows:
        code = (row.get('L_FOLDER') or '').strip()
        name = (row.get('RESIDUE_DESCRIPTIVE_NAME') or '').strip()
        cat = (row.get('CATEGORY') or '').strip()
        if not code or not CODE_RE.match(code) or code in seen:
            continue
        seen.add(code)
        name = name or code
        fxv = (code in fx) if fx else None   # rotabase is keyed on 3 letters

        if cat == CANONICAL_CATEGORY:
            # No params of ours by design: Rosetta ships these, and generating
            # them would collide with types it already loads.
            out['canonical'].append({
                'code': code, 'name': name, 'category': cat, 'hand': 'L',
                'l_code': code, 'pdb_code': code, 'source': 'rosetta'})
            params_for[code] = None
            palette, needs, l_rotlib = code, None, True

        elif cat not in NONCANONICAL_CATEGORIES:
            # A category the picker does not present: the bridge anchors are
            # linker components, the "new" set has no .rtp to build from.
            continue

        elif code in retired and not permit_retired:
            keep, why = retired[code]
            out['retired'].append({'code': code, 'name': name,
                                   'category': cat, 'keep': keep,
                                   'reason': why})
            continue

        elif code in NAME_COLLISIONS:
            # Rosetta has these three letters for a different molecule.
            # Offered in NEITHER handedness until ours is renamed -- mapping
            # to Rosetta's type would design the wrong residue silently.
            out['collisions'].append({'code': code, 'name': name,
                                      'category': cat,
                                      'reason': NAME_COLLISIONS[code]})
            continue

        elif code in ROSETTA_SUPPLIED and not permit_rosetta_ncaa:
            out['unavailable'].append({
                'code': code, 'name': name, 'category': cat,
                'reason': 'Rosetta has this type but the design protocol runs '
                          'pre-talaris, whose residue type set '
                          '(residue_types.txt.slim) contains no l-ncaa '
                          'entries, so the packer palette cannot resolve it'})
            continue

        elif code in ROSETTA_SUPPLIED:
            palette, needs = ROSETTA_SUPPLIED[code], None
            # Rosetta keeps its NCAA rotamer libraries in its own tree, which
            # this module does not read. None is "unknown", not "no", the same
            # convention foldx uses when rotabase is unreadable.
            l_rotlib = None
            out['noncanonical'].append({
                'code': palette, 'csv_code': code, 'name': name,
                'category': cat, 'hand': 'L', 'l_code': code,
                'pdb_code': code, 'source': 'rosetta',
                'rotlib': l_rotlib, 'foldx': fxv})
            params_for[palette] = None

        elif code in have:
            palette, needs, l_rotlib = code, code, code in libs
            out['noncanonical'].append({
                'code': code, 'csv_code': code, 'name': name,
                'category': cat, 'hand': 'L', 'l_code': code,
                'pdb_code': code, 'source': 'local',
                'rotlib': l_rotlib, 'foldx': fxv})
            params_for[code] = code

        else:
            # No params, ours or Rosetta's. A residue awaiting a build after
            # a rename says so; anything else is a genuine absence.
            if code in PENDING_BUILD:
                out['pending'].append({'code': code, 'name': name,
                                       'category': cat,
                                       'reason': PENDING_BUILD[code]})
            continue

        if not want_d:
            continue
        # Prefixed to the PALETTE name, not the CSV code: BIF's type is called
        # BiphenylAlanine, so its mirror is DBiphenylAlanine.
        d_code = d_rosetta_name(row, palette)
        if not d_code or d_code in seen:
            continue
        seen.add(d_code)
        out['noncanonical'].append({
            'code': d_code,
            'csv_code': code,
            'name': 'D-' + name,
            'category': d_category(cat),
            'hand': 'D',
            'l_code': code,
            'pdb_code': d_pdb_code(row, het_map),
            'source': 'rosetta' if needs is None else 'local',
            # The mirror has no rotamer library of its own and needs none:
            # Rosetta scores a D_AA type against the L Dunbrack library with
            # phi, psi and every chi sign-inverted. So it inherits the L
            # parent's answer -- True for a D-canonical, whose L library is
            # Rosetta's own.
            'rotlib': l_rotlib,
            # Never None, and never a rotabase lookup. See note 3 above: an
            # unknown L residue gets dropped by FoldX, but a D residue wearing
            # its parent's name3 gets SCORED as the L form, which returns a
            # believable number for the wrong molecule.
            'foldx': False,
        })
        # None wherever Rosetta supplies the L type and therefore mirrors it
        # itself (the canonical twenty, and the eight in ROSETTA_SUPPLIED).
        # Otherwise the L parent's params, which is what gets flipped.
        params_for[d_code] = needs

    out['canonical'].sort(key=lambda e: e['code'])
    # L before D, then alphabetical, for any consumer that ignores 'groups'.
    out['noncanonical'].sort(key=lambda e: (e['hand'] == 'D', e['code']))

    # Sub-groups for the picker, so provenance AND handedness stay visible.
    for cat in GROUP_ORDER:
        members = [e['code'] for e in out['noncanonical']
                   if e['category'] == cat]
        if members:
            out['groups'].append({'category': cat, 'codes': members})

    out['params_for'] = params_for

    out['counts'] = {
        'canonical': len(out['canonical']),
        'noncanonical': len(out['noncanonical']),
        'with_rotlib': sum(1 for e in out['noncanonical'] if e['rotlib']),
        'without_rotlib': sum(1 for e in out['noncanonical']
                              if not e['rotlib']),
        'foldx_scorable': sum(1 for e in out['noncanonical']
                              if e.get('foldx')),
        'l_offerable': sum(1 for e in out['noncanonical']
                           if e['hand'] == 'L'),
        'd_offerable': sum(1 for e in out['noncanonical']
                           if e['hand'] == 'D'),
        # A D residue with no 3-character het code cannot be written into
        # columns 18-20 of an ATOM record under a name of its own. Surfaced as
        # a count because it decides whether the viewer and the results table
        # can tell it from its L parent.
        'd_without_pdb_code': sum(1 for e in out['noncanonical']
                                  if e['hand'] == 'D' and not e['pdb_code']),
        'rosetta_supplied': sum(1 for e in out['noncanonical']
                                if e['source'] == 'rosetta'),
        'collisions': len(out['collisions']),
        'pending': len(out['pending']),
        'retired': len(out['retired']),
        'unavailable': len(out['unavailable']),
    }
    # Residues that are BOTH offerable here and known to rotabase. Not the
    # raw rotabase set minus the canonicals: that also lists codes we withhold
    # as retired (PTR, SEP, TPO) and codes we do not have at all (END, PSI),
    # which printed seven names beside a count of two.
    out['foldx_known'] = sorted(e['code'] for e in out['noncanonical']
                                if e.get('foldx'))
    out['foldx_note'] = (
        "FoldX has parameters for only a few non-canonical residues. It does "
        "not fail on the others -- it silently drops them and scores the rest "
        "of the peptide, so a design containing one has no valid FoldX ddG and "
        "is ranked on Rosetta's own interface and binding scores instead."
    )
    return out


def allowed_codes(catalogue):
    """The only non-canonical codes a submission may name."""
    return {e['code'] for e in catalogue.get('noncanonical', ())}


def canonical_codes(catalogue):
    return {e['code'] for e in catalogue.get('canonical', ())}


# Every palette this node can build. All five are reachable now that the D
# halves are offerable.
#
# A D palette needs NO protocol change. The earlier belief that it did came
# from conflating two different limits: CustomBaseTypePackerPalette cannot
# REMOVE the canonical twenty, which is why palette.canonical must be "all" --
# but a D type is ADDED exactly like an L non-canonical, through the same
# extra_base_type_file. Verified with PeptideStubMover: DALA, D0A1 and
# DBiphenylAlanine all instantiate on this build with no subtractive task
# operation anywhere.
PALETTE_MODES = ('l-canonical', 'l-mixed', 'd-canonical', 'd-mixed',
                 'all-mixed')


def palette_mode(codes, catalogue=None):
    """-> the mode a set of ACCEPTED codes describes.

    The server-side twin of residues.js's paletteMode(), and the authority:
    the mode is a description of the selection, so a client whose label
    disagrees with its own list must not be able to mislabel the job.

    Unknown codes are ignored rather than guessed at -- validate_selection has
    already rejected anything not in the catalogue, so a code reaching here
    that is not recognised cannot contribute to the description.
    """
    cat = catalogue or get_catalogue()
    by = {e['code']: e for e in cat.get('noncanonical', ())}
    known = [c for c in (codes or []) if c in by]
    if not known:
        return 'l-canonical'
    hands = {by[c].get('hand', 'L') for c in known}
    if len(hands) > 1:
        return 'all-mixed'
    if 'L' in hands:
        return 'l-mixed'
    cats = {by[c].get('category') for c in known}
    return 'd-canonical' if cats == {D_CANONICAL_CATEGORY} else 'd-mixed'


def validate_selection(selection, catalogue):
    """-> (accepted, rejected). Never raises, never partially trusts.

    `selection` is whatever arrived in the request. Anything that is not a
    well-formed code present in the catalogue is rejected and reported, rather
    than dropped silently: a typo that quietly removes a residue from a design
    job is worse than an error message.
    """
    allowed = allowed_codes(catalogue)
    canon = canonical_codes(catalogue)
    accepted, rejected = [], []
    if selection is None:
        return accepted, rejected
    if isinstance(selection, str):
        selection = [s for s in re.split(r'[\s,]+', selection) if s]
    if not isinstance(selection, (list, tuple)):
        return accepted, [{'value': repr(selection)[:40],
                           'reason': 'not a list of residue codes'}]
    seen = set()
    for raw in selection:
        if not isinstance(raw, str):
            rejected.append({'value': repr(raw)[:40],
                             'reason': 'not a string'})
            continue
        code = raw.strip()
        if not TYPE_NAME_RE.match(code):
            rejected.append({'value': code[:40],
                             'reason': 'not a residue type name'})
        elif code in canon:
            # Not an error in the user's chemistry, and saying "no params"
            # here would send someone looking for a missing file. The
            # canonical twenty are already in the palette; naming one in the
            # extra-base-type file is simply redundant.
            rejected.append({'value': code,
                             'reason': 'canonical residue, always available '
                                       'and not selectable here'})
        elif code not in allowed:
            rejected.append({'value': code,
                             'reason': 'no installed params for this residue'})
        elif code in seen:
            continue
        else:
            seen.add(code)
            accepted.append(code)
    # ONE HAND PER MOLECULE.
    #
    # Measured with PeptideStubMover: D0A1 is written into the output PDB as
    # `0A1` and DBiphenylAlanine as `BIF` -- CHIRAL_FLIP_NAMING has no het code
    # to give a mirrored NCAA, so it inherits its parent's. One job gets one
    # palette file covering every designed position, so a job offering D0A1
    # and not 0A1 leaves `0A1` in the output unambiguous: the L type was never
    # available to the packer. Offer both and nothing downstream -- the results
    # table, the viewer, FoldX -- can tell which one a design used.
    #
    # The picker already blocks this, but the browser is a suggestion. Both
    # members are rejected rather than one silently kept: which hand was meant
    # is not ours to guess.
    by_molecule = {}
    for e in catalogue.get('noncanonical', ()):
        by_molecule.setdefault(e.get('l_code') or e['code'], set()).add(e['code'])
    acc = set(accepted)
    clashing = set()
    for codes in by_molecule.values():
        both = sorted(codes & acc)
        if len(both) < 2:
            continue
        clashing.update(both)
        for c in both:
            rejected.append({
                'value': c,
                'reason': 'a residue and its mirror cannot both be in one job '
                          '(with %s): they share a three-letter code in the '
                          'output PDB, so a design using one could not be told '
                          'from a design using the other. Choose one hand.'
                          % ', '.join(x for x in both if x != c)})
    if clashing:
        accepted = [c for c in accepted if c not in clashing]

    accepted.sort()
    return accepted, rejected


# ---------------------------------------------------------------------------
# Process-wide cache, so /api/residues does not re-read a 282-row CSV and two
# directory listings on every page load. A params regeneration therefore needs
# either a service restart or reload(). The cache is deliberate rather than
# incidental: silently re-reading on every request would hide the fact that the
# catalogue has a lifecycle at all.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache = None


def get_catalogue(force=False):
    global _cache
    with _lock:
        if _cache is None or force:
            _cache = load_catalogue()
        return _cache


def reload():
    return get_catalogue(force=True)


def public_payload():
    """What /api/residues returns. Excludes params_dir: a server path has no
    business in a browser response."""
    cat = get_catalogue()
    return {
        'canonical': cat['canonical'],
        'noncanonical': cat['noncanonical'],
        'groups': cat['groups'],
        'counts': cat.get('counts', {'canonical': 0, 'noncanonical': 0,
                                     'with_rotlib': 0, 'without_rotlib': 0}),
        'canonical_editable': cat['canonical_editable'],
        'canonical_note': cat['canonical_note'],
        'd_residues': cat.get('d_residues', False),
        # Shown so the picker can say WHY a residue in the CSV is absent,
        # rather than leaving a silent hole someone re-investigates later.
        'collisions': cat.get('collisions', []),
        'pending': cat.get('pending', []),
        'retired': cat.get('retired', []),
        'unavailable': cat.get('unavailable', []),
        'retired_enforced': cat.get('retired_enforced', False),
        'd_note': (
            "D residues carry no params of their own: Rosetta's D_AA patch "
            "mirrors each one from its L parent, which is why selecting a D "
            "non-canonical loads the L params file. FoldX cannot score them, "
            "so a design containing one is ranked on Rosetta's interface and "
            "binding scores alone."
        ) if cat.get('d_residues') else '',
        'foldx_known': cat.get('foldx_known', []),
        'foldx_note': cat.get('foldx_note', ''),
        'error': cat['error'],
    }


def validate_design_residues(selection):
    """validate_selection against the cached catalogue. -> (accepted, rejected)."""
    return validate_selection(selection, get_catalogue())


def params_paths(codes):
    """Absolute .params paths for already-validated codes.

    Built from the node's own params directory, never from anything a client
    sent: the client names a residue, the node decides where that residue's
    file is. A code with no file on disk is skipped rather than producing a
    path Rosetta would fail to open -- validate_selection has already rejected
    anything not installed, so this is a guard against the directory changing
    under a long-lived process.

    A D residue resolves to its L PARENT's file: there is no D params file to
    find, and Rosetta's D_AA patch can only flip a type that has been loaded.
    Selecting D004 therefore passes 004.params, and selecting both D004 and 004
    passes it once -- naming the same file twice makes Rosetta object to a
    duplicate residue type.
    """
    cat = get_catalogue()
    params_for = cat.get('params_for') or {}
    out, seen = [], set()
    for c in codes:
        # Resolved through the catalogue BEFORE any pattern match, so a path
        # is only ever built from a code the catalogue itself produced -- not
        # from the caller's string at all. None covers both "Rosetta supplies
        # this type" and "unrecognised".
        src = params_for.get(c)
        if src is None or src in seen or not CODE_RE.match(src):
            continue
        p = os.path.join(cat['params_dir'], '%s.params' % src)
        if os.path.exists(p):
            seen.add(src)
            out.append(p)
    return out


def write_palette_file(codes, path):
    """Write the file named by -packing:packer_palette:extra_base_type_file.

    Whitespace-separated residue type names, which Rosetta adds to the
    canonical twenty. Written one per line for legibility; Rosetta splits on
    any whitespace. Returns the number of codes written.

    Only call this with the ACCEPTED list from validate_selection: the path
    reaches a Rosetta command line, and an unvalidated code reaches it too.
    """
    bad = [c for c in codes if not TYPE_NAME_RE.match(c)]
    if bad:
        raise ValueError('refusing to write unvalidated residue codes: %s'
                         % ', '.join(bad[:5]))
    with open(path, 'w') as f:
        f.write('# Residue types added to the canonical twenty for this job.\n')
        f.write('# Generated by predPEP; do not edit by hand.\n')
        for c in codes:
            f.write('%s\n' % c)
    return len(codes)


if __name__ == '__main__':
    import sys
    cat = load_catalogue(*(sys.argv[1:4] or [None, None, None]))
    if cat['error']:
        print('ERROR:', cat['error'])
        sys.exit(1)
    print('canonical      : %d (read-only: %s)'
          % (cat['counts']['canonical'], not cat['canonical_editable']))
    print('offerable      : %d total -- %d L, %d D  (%d supplied by Rosetta)'
          % (cat['counts']['noncanonical'], cat['counts']['l_offerable'],
             cat['counts']['d_offerable'], cat['counts']['rosetta_supplied']))
    for c in cat['collisions']:
        print('   WITHHELD %-5s %s' % (c['code'], c['reason']))
    for c in cat['pending']:
        print('   PENDING  %-5s %s' % (c['code'], c['reason']))
    for c in cat['retired']:
        print('   RETIRED  %-5s -> %-5s %s'
              % (c['code'], c['keep'], c['reason'][:64]))
    for c in cat['unavailable']:
        print('   NO TYPE  %-5s %s' % (c['code'], c['reason'][:70]))
    print('rotamer libs   : %d with, %d without'
          % (cat['counts']['with_rotlib'], cat['counts']['without_rotlib']))
    if not cat.get('d_residues'):
        print('D residues     : DISABLED (PREDPEP_D_RESIDUES)')
    elif cat['counts']['d_without_pdb_code']:
        print('D residues     : %d have no 3-char het code and will share '
              'their L parent\'s name in PDB output'
              % cat['counts']['d_without_pdb_code'])
    print('FoldX-scorable : %d (%s)'
          % (cat['counts'].get('foldx_scorable', 0),
             ' '.join(cat.get('foldx_known') or []) or 'rotabase unreadable'))
    for g in cat['groups']:
        print('   %-34s %d' % (g['category'], len(g['codes'])))
    print()
    print('first five non-canonical:')
    for e in cat['noncanonical'][:5]:
        print('   %-5s %-44s rotlib=%s' % (e['code'], e['name'], e['rotlib']))
