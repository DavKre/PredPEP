#!/home/spacepep/miniforge3/envs/predPEP/bin/python

"""Split a complex whose peptide chain is several physical segments.

Each segment gets a turn as THE peptide, with every other segment moved into the
receptor under a chain ID of its own. One input complex becomes N complexes,
each of which is an ordinary single-peptide design problem the existing pipeline
already knows how to run -- which is the point: the core path stays untouched.

    split_segments.py <pdb> --peptide-chain B --outdir DIR [--spec spec.json]

Writes DIR/segment_<k>/{input.pdb,design_spec.json} and DIR/split_manifest.json.

WHAT THE PER-SEGMENT SPEC CARRIES
---------------------------------
Anchors and disulfides are rewritten into the new complex's terms:

  * anchors keep only those falling inside this segment, renumbered to positions
    WITHIN the segment (1..len), because that is what the segment's own peptide
    chain will be renumbered to by clean_pdb.py;
  * a disulfide with both ends inside this segment becomes an ordinary
    intra-peptide pair, in segment-local positions;
  * a disulfide spanning two segments is recorded separately under
    `cross_segment_disulfides`. It is REAL and load-bearing -- it is what holds
    the strands together -- but after the split its two ends are on different
    chains, and -in:fix_disulf needs whole-complex pose numbers to express that.
    See the caveat below; this script records them, it does not yet wire them.

KNOWN GAP (read this before trusting a split run)
-------------------------------------------------
Cross-segment staples are not yet passed to Rosetta as fixed disulfides. With
them unfixed, Rosetta's own -detect_disulf decides, by SG-SG distance -- so a
staple at 2.37 A is likely found and one at 2.73 A may not be, and an undetected
staple means the segment in the receptor is not bonded to the peptide at all.
Until run_createflags.py can emit whole-complex pose pairs, treat a split run as
designing one segment against a receptor that merely CONTAINS the other, not one
covalently joined to it. The manifest says so per segment.
"""

import argparse
import json
import os
import string
import sys

import segment_inspector


def _assign_chain_ids(used, count):
    """Fresh single-character chain IDs for the displaced segments.

    Taken from the ones not already present, so nothing collides with a receptor
    chain. Upper case first because that is the convention; digits last because
    some tools dislike them.
    """
    pool = [c for c in string.ascii_uppercase + string.ascii_lowercase
            + string.digits if c not in used]
    if len(pool) < count:
        raise ValueError('no free chain IDs left to relabel segments')
    return pool[:count]


def _segment_of_position(chain_info):
    """position -> segment index, for the peptide chain."""
    out = {}
    for seg in chain_info['segments']:
        for p in range(seg['start_position'], seg['end_position'] + 1):
            out[p] = seg['index']
    return out


def split(pdb_path, peptide_chain, outdir, spec=None):
    report = segment_inspector.inspect(pdb_path, peptide_chain=peptide_chain)
    chain_info = report['chains'].get(peptide_chain)
    if chain_info is None:
        raise ValueError(f'no chain {peptide_chain!r} in {pdb_path}')
    if not chain_info['is_segmented']:
        raise ValueError(f'chain {peptide_chain} is one continuous peptide; '
                         'nothing to split')

    segments = chain_info['segments']
    used = set(report['chains']) | {' '}
    # One spare ID per OTHER segment; the segment being designed keeps the
    # original peptide chain ID so no downstream default has to change.
    spare = _assign_chain_ids(used, len(segments) - 1)

    pos_to_seg = _segment_of_position(chain_info)
    position_map = chain_info['position_map']           # resseq -> position
    resseq_of_position = {p: r for r, p in position_map.items()}

    # resseq -> segment index, for rewriting the chain column
    seg_of_resseq = {r: pos_to_seg[p] for r, p in position_map.items()
                     if p in pos_to_seg}

    anchors_pos = set((spec or {}).get('anchor_positions') or [])
    spec_pairs = [tuple(p) for p in
                  (((spec or {}).get('peptide') or {}).get('disulfide_positions') or [])]
    # Geometry, independent of any spec, so a split is still correct for a job
    # submitted without one.
    observed = [(d['position1'], d['position2'], d['sg_distance'], d['state'])
                for d in report['disulfides']
                if d['chain1'] == peptide_chain and d['chain2'] == peptide_chain
                and d['position1'] and d['position2']]

    os.makedirs(outdir, exist_ok=True)
    manifest = {'source_pdb': os.path.abspath(pdb_path),
                'peptide_chain': peptide_chain,
                'segment_count': len(segments), 'segments': []}

    with open(pdb_path, errors='replace') as fh:
        source_lines = fh.readlines()

    for target in segments:
        k = target['index']
        others = [s for s in segments if s['index'] != k]
        relabel = {s['index']: cid for s, cid in zip(others, spare)}

        segdir = os.path.join(outdir, f'segment_{k}')
        os.makedirs(segdir, exist_ok=True)

        # --- the PDB. Only column 22 changes, and only for displaced segments,
        # so coordinates and everything else stay byte-identical to the input.
        out_lines = []
        for line in source_lines:
            if not line.startswith(('ATOM', 'HETATM')):
                continue                  # clean_pdb.py would drop these anyway
            if line[21] != peptide_chain:
                out_lines.append(line)
                continue
            try:
                rs = int(line[22:26])
            except ValueError:
                continue
            seg_idx = seg_of_resseq.get(rs)
            if seg_idx == k:
                out_lines.append(line)
            elif seg_idx in relabel:
                out_lines.append(line[:21] + relabel[seg_idx] + line[22:])
        out_lines.append('TER\n')
        out_lines.append('END\n')
        pdb_out = os.path.join(segdir, 'input.pdb')
        with open(pdb_out, 'w') as f:
            f.writelines(out_lines)

        # --- segment-local numbering. offset = positions before this segment.
        off = target['start_position'] - 1
        local = lambda p: p - off                                   # noqa: E731

        kept = sorted(p - off for p in anchors_pos if pos_to_seg.get(p) == k)
        dropped = sorted(p for p in anchors_pos if pos_to_seg.get(p) != k)

        internal, crossing = [], []
        for a, b, dist, state in observed:
            sa, sb = pos_to_seg.get(a), pos_to_seg.get(b)
            if sa == k and sb == k:
                internal.append({'positions': [local(a), local(b)],
                                 'sg_distance': dist, 'state': state})
            elif sa == k or sb == k:
                mine, theirs = (a, b) if sa == k else (b, a)
                crossing.append({
                    'position_in_peptide': local(mine),
                    'partner_chain': relabel.get(pos_to_seg.get(theirs)),
                    'partner_resseq': resseq_of_position.get(theirs),
                    'sg_distance': dist, 'state': state})

        receptor = [c for c in report['chains'] if c != peptide_chain]
        receptor += [relabel[s['index']] for s in others]

        seg_spec = dict(spec or {})
        seg_spec['spec_version'] = 2
        seg_spec['anchor_positions'] = kept
        seg_spec.pop('anchors', None)          # resSeq of the ORIGINAL file: no
        seg_spec.pop('anchors_uploaded_numbering', None)  # longer meaningful here
        flags = dict(seg_spec.get('flags') or {})
        flags['pep_chain'] = peptide_chain
        seg_spec['flags'] = flags
        seg_spec['peptide'] = {
            'chain': peptide_chain,
            'length': target['length'],
            'sequence': target['sequence'],
            'source_resseq': [target['start_resseq'], target['end_resseq']],
            'disulfide_positions': [d['positions'] for d in internal],
        }
        seg_spec['disulfides_observed'] = internal
        seg_spec['cross_segment_disulfides'] = crossing
        seg_spec['segment'] = {'index': k, 'of': len(segments),
                               'receptor_chains': receptor}
        # Only pairs this run can actually fix are claimed as preserved.
        ss_mode = (seg_spec.get('disulfides') or {}).get('mode', 'auto')
        seg_spec['disulfides'] = {'mode': 'preserve' if internal else ss_mode}

        with open(os.path.join(segdir, 'design_spec.json'), 'w') as f:
            json.dump(seg_spec, f, indent=2)

        entry = {
            'index': k, 'dir': segdir, 'pdb': pdb_out,
            'length': target['length'], 'sequence': target['sequence'],
            'source_resseq': [target['start_resseq'], target['end_resseq']],
            'peptide_chain': peptide_chain,
            'receptor_chains': receptor,
            'anchor_positions': kept,
            'anchors_from_other_segments': dropped,
            'internal_disulfides': internal,
            'cross_segment_disulfides': crossing,
            'env': {'PREDPEP_PEP_CHAIN': peptide_chain,
                    'PREDPEP_RECEPTOR_CHAINS': ' '.join(receptor)},
        }
        if crossing:
            entry['caveat'] = (
                f'{len(crossing)} staple(s) span segments and are NOT passed as '
                'fixed disulfides; Rosetta -detect_disulf decides by distance, '
                'so this run designs against a receptor that contains the other '
                'segment rather than one covalently joined to it.')
        if not kept:
            entry['warning'] = (
                'no anchors fall in this segment; every position will be used '
                'as an anchor, which may not be what was selected.')
        manifest['segments'].append(entry)

    manifest['warnings'] = report['warnings']
    with open(os.path.join(outdir, 'split_manifest.json'), 'w') as f:
        json.dump(manifest, f, indent=2)
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pdb')
    ap.add_argument('--peptide-chain', default='B')
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--spec')
    args = ap.parse_args()

    spec = None
    if args.spec:
        try:
            with open(args.spec) as f:
                spec = json.load(f)
        except (OSError, ValueError) as e:
            print(f'ERROR: could not read spec {args.spec}: {e}', file=sys.stderr)
            return 1
    try:
        manifest = split(args.pdb, args.peptide_chain, args.outdir, spec)
    except (OSError, ValueError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1

    print(f'{manifest["segment_count"]} segment(s) from chain '
          f'{manifest["peptide_chain"]} of {os.path.basename(args.pdb)}:')
    for s in manifest['segments']:
        print(f'  segment {s["index"]}: {s["length"]} residues '
              f'({s["sequence"]}), resSeq {s["source_resseq"][0]}..'
              f'{s["source_resseq"][1]}')
        print(f'    peptide chain {s["peptide_chain"]}, receptor '
              f'{" ".join(s["receptor_chains"])}')
        print(f'    anchors (segment-local positions): '
              f'{s["anchor_positions"] or "none selected -> all positions"}')
        if s['internal_disulfides']:
            print(f'    internal disulfide(s): '
                  f'{[d["positions"] for d in s["internal_disulfides"]]}')
        for c in s['cross_segment_disulfides']:
            print(f'    CROSS-SEGMENT staple: peptide position '
                  f'{c["position_in_peptide"]} to chain {c["partner_chain"]} '
                  f'resSeq {c["partner_resseq"]} ({c["sg_distance"]} A, '
                  f'{c["state"]})')
        if 'caveat' in s:
            print(f'    CAVEAT: {s["caveat"]}')
        if 'warning' in s:
            print(f'    WARNING: {s["warning"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
