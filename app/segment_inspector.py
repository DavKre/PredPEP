#!/home/spacepep/miniforge3/envs/predPEP/bin/python

"""Inspect a complex for discontinuous (multi-segment) chains before design.

WHY THIS EXISTS
---------------
A peptide chain can be physically two or more separate pieces sharing one chain
ID. Seen in the wild: chain B numbered 1-21 then 99-107, two strands held
together only by disulfides, with the numbering jump used by the author to mean
"these are not covalently continuous".

Rosetta does not read that meaning. Chain identity comes from the chain ID
column; a numbering gap is invisible to it. Worse, `clean_pdb.py` RENUMBERS each
chain from 1 and keeps only ATOM records, so by the time the pipeline builds its
input the gap is gone and the SSBOND records with it. The two strands become
consecutive residues and Rosetta is asked to form a peptide bond across what was
a 14.5 A void. pepspec then grows the peptide from its anchor using ideal
internal coordinates, which drags the second strand across space to close that
gap -- destroying the binding mode. Nothing in that sequence fails. A score
table comes out the other end describing a molecule that cannot exist.

So this module answers one question before any of that happens: is each chain
one piece?

IT KEYS ON GEOMETRY, NOT NUMBERING
----------------------------------
Numbering is a convention and distance is a fact, and the two disagree in both
directions:

  * a numbering gap with a normal bond length is a renumbering convention, or a
    deliberately sparse numbering, and NOT a break;
  * a break with perfectly contiguous numbering is a real break that the
    numbering hides -- missing density, a modelled construct, a spliced file.

Numbering gaps are therefore reported as a hint for the reader and never used to
decide. Every decision comes from the backbone C-N distance.

CLI
---
    segment_inspector.py <pdb> [--chain B] [--json]

Exit status: 0 = every chain is one piece, 1 = at least one chain is segmented,
2 = could not read the file. Non-zero is NOT an error in the file; it is the
answer, and the caller decides what to do with it.
"""

import json
import math
import sys
from collections import OrderedDict

# A peptide bond is 1.33 A. Anything up to this is a bond, possibly strained;
# beyond it the residues are not bonded. 2.0 is deliberately generous: a
# distorted or poorly refined bond should read as a bond, because calling a real
# bond a break would split a chain that is actually continuous. The measured
# distance is always reported so a human can overrule.
BOND_MAX = 2.0
BOND_IDEAL = 1.33
BOND_STRAINED = 1.8        # above this and still bonded: worth mentioning

# A disulfide is 2.05 A. Past DISULFIDE_MAX there is no bond. Between
# DISULFIDE_GOOD and DISULFIDE_MAX it is marginal: it may fall outside
# Rosetta's own -detect_disulf cutoff, which differs by release and is NOT
# assumed here. Marginal pairs are reported for a human to check, not resolved.
DISULFIDE_IDEAL = 2.05
DISULFIDE_GOOD = 2.5
DISULFIDE_MAX = 3.0

BACKBONE = ('N', 'CA', 'C')


def _dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def read_residues(pdb_path):
    """Residues in file order: OrderedDict[(chain, resseq, icode)] -> residue.

    Only ATOM/HETATM lines are read, by fixed column positions, because that is
    what the format guarantees and what clean_pdb.py leaves behind.

    The element guard on CA excludes calcium, whose atom name is also CA. It is
    applied ONLY to CA: for N and C the name is unambiguous in a protein, and
    some writers leave the element column blank, so guarding those would discard
    real backbone atoms.
    """
    residues = OrderedDict()
    with open(pdb_path, errors='replace') as fh:
        for line in fh:
            if not line.startswith(('ATOM', 'HETATM')):
                continue
            if len(line) < 54:
                continue
            atom = line[12:16].strip()
            if atom == 'CA' and line[76:78].strip() not in ('C', ''):
                continue                      # calcium, not alpha carbon
            try:
                resseq = int(line[22:26])
                xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
            except ValueError:
                continue
            key = (line[21], resseq, line[26])
            if key not in residues:
                residues[key] = {'chain': line[21], 'resseq': resseq,
                                 'icode': line[26].strip(), 'name': line[17:20].strip(),
                                 'atoms': {}}
            residues[key]['atoms'].setdefault(atom, xyz)
    return residues


def _ssbond_records(pdb_path):
    """Declared disulfides as [(chain1, res1, chain2, res2)], by fixed columns.

    Absence proves nothing: clean_pdb.py strips these, and many files never had
    them. Geometry is checked independently.
    """
    pairs = []
    try:
        with open(pdb_path, errors='replace') as fh:
            for line in fh:
                if not line.startswith('SSBOND') or len(line) < 36:
                    continue
                try:
                    pairs.append((line[15], int(line[17:21]),
                                  line[29], int(line[31:35])))
                except ValueError:
                    continue
    except OSError:
        pass
    return pairs


def _split_chain(chain_residues):
    """Split one chain's residues into covalently continuous segments.

    Walks consecutive residues IN FILE ORDER and cuts wherever the C-N distance
    says they are not bonded. A residue missing C or N cannot be measured; the
    cut is NOT made (splitting on absent data would invent breaks in any file
    with incomplete backbone) and the uncertainty is recorded instead.
    """
    segments, junctions = [], []
    current = [chain_residues[0]]
    for prev, this in zip(chain_residues, chain_residues[1:]):
        c = prev['atoms'].get('C')
        n = this['atoms'].get('N')
        if c is None or n is None:
            current.append(this)
            junctions.append({'after_resseq': prev['resseq'],
                              'before_resseq': this['resseq'],
                              'c_n_distance': None, 'ca_ca_distance': None,
                              'bonded': None,
                              'note': 'backbone C or N missing; not measurable'})
            continue
        d = _dist(c, n)
        ca_prev, ca_this = prev['atoms'].get('CA'), this['atoms'].get('CA')
        rec = {'after_resseq': prev['resseq'], 'before_resseq': this['resseq'],
               'c_n_distance': round(d, 2),
               'ca_ca_distance': (round(_dist(ca_prev, ca_this), 2)
                                  if ca_prev and ca_this else None),
               'bonded': d <= BOND_MAX, 'note': None}
        if d > BOND_MAX:
            rec['note'] = f'not bonded ({d:.2f} A; a peptide bond is {BOND_IDEAL} A)'
            junctions.append(rec)
            segments.append(current)
            current = [this]
        else:
            if d > BOND_STRAINED:
                rec['note'] = f'bonded but strained ({d:.2f} A)'
                junctions.append(rec)
            current.append(this)
    segments.append(current)
    return segments, junctions


def inspect(pdb_path, peptide_chain=None):
    """Full report. See module docstring; the returned dict is JSON-safe."""
    residues = read_residues(pdb_path)
    if not residues:
        raise ValueError(f'no ATOM/HETATM records read from {pdb_path}')

    by_chain = OrderedDict()
    for key, res in residues.items():
        by_chain.setdefault(res['chain'], []).append(res)

    report = {'pdb': pdb_path, 'bond_max': BOND_MAX,
              'peptide_chain': peptide_chain, 'chains': OrderedDict(),
              'warnings': []}

    for chain, items in by_chain.items():
        segs, junctions = _split_chain(items)

        # resSeq -> 1-based position in the chain, which is what clean_pdb.py's
        # renumbering produces. This is the map that translates a form's anchor
        # numbers into the numbering the pipeline will actually see. It is only
        # meaningful when resSeq is unique within the chain, so insertion codes
        # are checked rather than silently flattened.
        position_map, dupes = {}, []
        for i, res in enumerate(items, start=1):
            if res['resseq'] in position_map:
                dupes.append(res['resseq'])
            else:
                position_map[res['resseq']] = i
        if dupes:
            report['warnings'].append(
                f'chain {chain}: resSeq {sorted(set(dupes))} appear more than once '
                '(insertion codes); the resSeq->position map is ambiguous and '
                'anchors given as resSeq cannot be translated reliably.')

        nums = [r['resseq'] for r in items]
        numbering_gaps = [[nums[i], nums[i + 1]] for i in range(len(nums) - 1)
                          if nums[i + 1] != nums[i] + 1]

        seg_out = []
        pos = 1
        for j, seg in enumerate(segs, start=1):
            seg_out.append({
                'index': j,
                'length': len(seg),
                'start_resseq': seg[0]['resseq'],
                'end_resseq': seg[-1]['resseq'],
                'start_position': pos,
                'end_position': pos + len(seg) - 1,
                'resseqs': [r['resseq'] for r in seg],
                'sequence': ''.join(_one_letter(r['name']) for r in seg),
            })
            pos += len(seg)

        report['chains'][chain] = {
            'residues': len(items),
            'resseq_range': [nums[0], nums[-1]],
            'segments': seg_out,
            'segment_count': len(seg_out),
            'is_segmented': len(seg_out) > 1,
            'junctions': junctions,
            'numbering_gaps': numbering_gaps,
            'position_map': position_map,
        }

        # A numbering gap that is NOT a break, and a break that has NO numbering
        # gap, are both worth saying out loud: each is a case where the two
        # signals disagree and a reader trusting the wrong one draws the wrong
        # conclusion.
        broken_at = {(j['after_resseq'], j['before_resseq'])
                     for j in junctions if j['bonded'] is False}
        for a, b in numbering_gaps:
            if (a, b) not in broken_at:
                report['warnings'].append(
                    f'chain {chain}: numbering jumps {a}->{b} but the residues ARE '
                    'bonded; that gap is a numbering convention, not a break.')
        for a, b in broken_at:
            if [a, b] not in numbering_gaps:
                report['warnings'].append(
                    f'chain {chain}: residues {a} and {b} are numbered consecutively '
                    'but are NOT bonded; the break is real and the numbering hides it.')

    # --- disulfides: declared and geometric, reconciled
    declared = _ssbond_records(pdb_path)
    sg = {(r['chain'], r['resseq']): r['atoms']['SG']
          for r in residues.values() if 'SG' in r['atoms']}

    seen, bonds = set(), []
    for c1, r1, c2, r2 in declared:
        seen.add(frozenset(((c1, r1), (c2, r2))))
        bonds.append(_disulfide(c1, r1, c2, r2, sg, by_chain, report, declared=True))
    keys = sorted(sg)
    for i, ka in enumerate(keys):
        for kb in keys[i + 1:]:
            if frozenset((ka, kb)) in seen:
                continue
            if _dist(sg[ka], sg[kb]) <= DISULFIDE_GOOD:
                bonds.append(_disulfide(ka[0], ka[1], kb[0], kb[1], sg,
                                        by_chain, report, declared=False))
    report['disulfides'] = bonds

    if peptide_chain and peptide_chain not in report['chains']:
        report['warnings'].append(
            f'peptide chain {peptide_chain!r} not present; chains found: '
            f'{", ".join(report["chains"])}.')
    return report


def _disulfide(c1, r1, c2, r2, sg, by_chain, report, declared):
    out = {'declared': declared, 'chain1': c1, 'resseq1': r1,
           'chain2': c2, 'resseq2': r2, 'sg_distance': None,
           'state': 'unmeasurable', 'cross_segment': None,
           'position1': None, 'position2': None}
    a, b = sg.get((c1, r1)), sg.get((c2, r2))
    if a is None or b is None:
        report['warnings'].append(
            f'disulfide {c1}{r1}-{c2}{r2}: SG atom missing, cannot verify.')
        return out
    d = _dist(a, b)
    out['sg_distance'] = round(d, 2)
    if d <= DISULFIDE_GOOD:
        out['state'] = 'formed'
    elif d <= DISULFIDE_MAX:
        out['state'] = 'marginal'
        report['warnings'].append(
            f'disulfide {c1}{r1}-{c2}{r2}: SG-SG {d:.2f} A is long (ideal '
            f'{DISULFIDE_IDEAL} A). It may fall outside your Rosetta release\'s '
            '-detect_disulf cutoff, in which case the bond is lost unless '
            'declared explicitly. Check the cutoff rather than assuming.')
    else:
        out['state'] = 'absent'
        report['warnings'].append(
            f'disulfide {c1}{r1}-{c2}{r2}: SG-SG {d:.2f} A is too long to be a '
            'bond. The SSBOND record does not describe this structure.')

    for ch, rs, pk in ((c1, r1, 'position1'), (c2, r2, 'position2')):
        info = report['chains'].get(ch)
        if info:
            out[pk] = info['position_map'].get(rs)
    if out['position1'] and out['position2'] and c1 == c2:
        info = report['chains'][c1]
        of = {}
        for seg in info['segments']:
            for p in range(seg['start_position'], seg['end_position'] + 1):
                of[p] = seg['index']
        out['cross_segment'] = of.get(out['position1']) != of.get(out['position2'])
    return out


_AA3 = {
    'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C', 'GLN': 'Q',
    'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I', 'LEU': 'L', 'LYS': 'K',
    'MET': 'M', 'PHE': 'F', 'PRO': 'P', 'SER': 'S', 'THR': 'T', 'TRP': 'W',
    'TYR': 'Y', 'VAL': 'V',
}


def _one_letter(name):
    return _AA3.get(name.upper(), 'X')


def summary_lines(report):
    """Human-readable report, for a log or a dashboard panel."""
    out = []
    for chain, info in report['chains'].items():
        tag = '' if chain != report.get('peptide_chain') else '  (peptide)'
        out.append(f'chain {chain}: {info["residues"]} residues, '
                   f'resSeq {info["resseq_range"][0]}..{info["resseq_range"][1]}, '
                   f'{info["segment_count"]} segment(s){tag}')
        if info['is_segmented']:
            for seg in info['segments']:
                out.append(f'  segment {seg["index"]}: resSeq {seg["start_resseq"]}'
                           f'..{seg["end_resseq"]} -> positions '
                           f'{seg["start_position"]}..{seg["end_position"]}  '
                           f'{seg["sequence"]}')
            for j in info['junctions']:
                if j['bonded'] is False:
                    out.append(f'  BREAK between {j["after_resseq"]} and '
                               f'{j["before_resseq"]}: C-N {j["c_n_distance"]} A')
    for b in report['disulfides']:
        src = 'SSBOND' if b['declared'] else 'geometry'
        cs = ''
        if b['cross_segment'] is True:
            cs = '  [links two segments]'
        out.append(f'disulfide {b["chain1"]}{b["resseq1"]}-{b["chain2"]}{b["resseq2"]} '
                   f'({src}): SG-SG {b["sg_distance"]} A, {b["state"]}{cs}')
    for w in report['warnings']:
        out.append(f'WARNING: {w}')
    return out


def main(argv):
    args = [a for a in argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__.strip().split('CLI')[-1].strip(), file=sys.stderr)
        return 2
    chain = None
    for a in argv[1:]:
        if a.startswith('--chain='):
            chain = a.split('=', 1)[1]
    if '--chain' in argv[1:]:
        i = argv[1:].index('--chain')
        if i + 2 <= len(argv[1:]) - 1 + 1:
            try:
                chain = argv[1:][i + 1]
            except IndexError:
                pass
    try:
        report = inspect(args[0], peptide_chain=chain)
    except (OSError, ValueError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2
    if '--json' in argv[1:]:
        print(json.dumps(report, indent=2))
    else:
        print('\n'.join(summary_lines(report)))
    return 1 if any(c['is_segmented'] for c in report['chains'].values()) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
