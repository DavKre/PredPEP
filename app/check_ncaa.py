#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""Scan a predPEP job's PDBs for non-canonical residues. READ ONLY.

Answers "did any non-canonical actually get placed?" from the structures
themselves, independent of tab3_ncaa_designs.csv -- because if the table is
wrong, reading the table cannot tell you so.

Usage:
    python3 check_ncaa.py /path/to/results/SPCGRK_b49fce6b
    python3 check_ncaa.py /path/to/results/SPCGRK_b49fce6b --chain B

Writes nothing, deletes nothing, runs no Rosetta.
"""
import argparse
import collections
import os
import sys

CANONICAL = {
    'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 'HIS', 'ILE',
    'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 'THR', 'TRP', 'TYR', 'VAL',
}

# Rosetta's own variant spellings of canonicals. Without these the scan
# reports a protonated histidine or a disulphide cysteine as a non-canonical
# insertion, which is a false positive that would send you hunting a design
# that never happened.
CANONICAL_VARIANTS = {
    'HID', 'HIE', 'HIP', 'HSD', 'HSE', 'HSP',   # histidine protonation states
    'CYD', 'CYS:disulfide', 'CYX', 'CYZ',       # cystine / disulphide-bonded
    'MSE',                                      # selenomethionine, usually crystallographic
    'ASH', 'GLH', 'LYN', 'ARN', 'TYM',          # neutral/charged variants
    'NMA', 'ACE', 'NME', 'NH2',                 # terminal capping groups
}

# Solvent, ions, buffer and cryoprotectant. Present as HETATM in almost every
# experimental structure and NOT a designed insertion. Reported separately.
SOLVENT_AND_IONS = {
    'HOH', 'WAT', 'DOD', 'TIP', 'SPC',
    'NA', 'CL', 'K', 'MG', 'CA', 'ZN', 'FE', 'FE2', 'MN', 'CU', 'CU1', 'NI',
    'CO', 'CD', 'HG', 'BR', 'IOD', 'F', 'LI', 'CS', 'RB', 'SR', 'BA',
    'SO4', 'PO4', 'NO3', 'ACT', 'ACY', 'EDO', 'GOL', 'PEG', 'PG4', 'PGE',
    'MPD', 'DMS', 'TRS', 'EPE', 'MES', 'IMD', 'FMT', 'CIT', 'TLA', 'MLI',
    'UNK', 'UNX', 'UNL',
}


def residues(path):
    """[(chain, resseq, icode, resname, record)] in file order, one per residue.

    Columns are read by PDB fixed-width position, not by splitting on
    whitespace: a four-character residue name or a missing chain id makes
    split() silently shift every field.
    """
    out = []
    seen = set()
    try:
        with open(path, errors='replace') as f:
            for line in f:
                rec = line[:6].strip()
                if rec not in ('ATOM', 'HETATM'):
                    continue
                name = line[17:20].strip()
                chain = line[21:22].strip() or '_'
                resseq = line[22:26].strip()
                icode = line[26:27].strip()
                key = (chain, resseq, icode)
                if key in seen:
                    continue
                seen.add(key)
                out.append((chain, resseq, icode, name, rec))
    except OSError as e:
        print('  ! could not read %s: %s' % (path, e), file=sys.stderr)
    return out


def classify(name):
    if name in CANONICAL or name in CANONICAL_VARIANTS:
        return 'canonical'
    if name in SOLVENT_AND_IONS:
        return 'solvent'
    return 'noncanonical'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('job_dir')
    ap.add_argument('--chain', default=None,
                    help='only report hits on this chain (e.g. B, the peptide)')
    ap.add_argument('--all-files', action='store_true',
                    help='list every PDB, not only those with a hit')
    a = ap.parse_args()

    job = os.path.abspath(a.job_dir)
    if not os.path.isdir(job):
        sys.exit('not a directory: %s' % job)

    print('Job: %s' % job)
    print()

    # --- what tables exist, and which one the dashboard reads ----------------
    print('Tables in the job directory')
    print('-' * 72)
    found_any = False
    for name in sorted(os.listdir(job)):
        if name.endswith(('.csv', '.json')):
            full = os.path.join(job, name)
            try:
                n = sum(1 for _ in open(full, errors='replace'))
            except OSError:
                n = -1
            note = ''
            if name == 'tab2_final_scores.csv':
                note = '  <- the dashboard reads THIS one'
            elif name == 'tab3_ncaa_designs.csv':
                note = '  <- NCAA designs land here; dashboard does NOT read it'
            print('  %-34s %6s line(s)%s'
                  % (name, n if n >= 0 else '?', note))
            found_any = True
    if not found_any:
        print('  (none)')
    print()

    # --- the ground truth: scan every PDB -----------------------------------
    pdbs = []
    for root, dirs, files in os.walk(job):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__')]
        for fn in files:
            if fn.endswith('.pdb'):
                pdbs.append(os.path.join(root, fn))
    pdbs.sort()

    print('Scanning %d PDB file(s) for residues outside the canonical 20'
          % len(pdbs))
    if a.chain:
        print('Restricted to chain %s' % a.chain)
    print('-' * 72)

    hits_by_file = {}
    ncaa_counter = collections.Counter()
    solvent_counter = collections.Counter()
    files_with_hits = 0

    for p in pdbs:
        hits = []
        for chain, resseq, icode, name, rec in residues(p):
            kind = classify(name)
            if kind == 'solvent':
                solvent_counter[name] += 1
                continue
            if kind != 'noncanonical':
                continue
            if a.chain and chain != a.chain:
                continue
            hits.append((chain, resseq + icode, name, rec))
            ncaa_counter[name] += 1
        if hits:
            files_with_hits += 1
            hits_by_file[p] = hits
            rel = os.path.relpath(p, job)
            print('  %s' % rel)
            for chain, pos, name, rec in hits:
                print('      chain %s  %-6s %-5s (%s)' % (chain, pos, name, rec))
        elif a.all_files:
            print('  %s  -- none' % os.path.relpath(p, job))

    print()
    print('=' * 72)
    print('VERDICT')
    print('=' * 72)
    print('PDB files scanned            : %d' % len(pdbs))
    print('Files with a non-canonical   : %d' % files_with_hits)
    if ncaa_counter:
        print('Non-canonical residues found :')
        for name, n in ncaa_counter.most_common():
            print('    %-6s %4d occurrence(s)' % (name, n))
    else:
        print('Non-canonical residues found : NONE')
        print()
        print('No PDB under this job contains a residue outside the canonical')
        print('20. So the empty table is not a reporting bug: nothing was')
        print('placed. Check ncaa_design_report.json for designs_produced and')
        print('designs_with_noncanonical, and the per-model logs under')
        print('_ncaa_design/ for whether rosetta_scripts ran at all.')
    if solvent_counter:
        print()
        print('Solvent/ions seen (NOT insertions, listed so you can see they')
        print('were correctly excluded): %s'
              % ', '.join('%s x%d' % (k, v)
                          for k, v in solvent_counter.most_common(8)))
    print()
    if ncaa_counter and files_with_hits:
        print('Residues WERE placed. If tab3_ncaa_designs.csv shows them but')
        print('the dashboard does not, that is the plumbing gap: predPEP.py')
        print('reads tab2_final_scores.csv only.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
