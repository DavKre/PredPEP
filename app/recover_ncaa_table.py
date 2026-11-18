#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""Rebuild tab3_ncaa_designs.csv from an NCAA pass that was killed before it
could write one.

WHY THIS EXISTS
---------------
run_ncaaDesign.py tabulates only at the very end. When the manager abandons it
on PREDPEP_NCAA_TIMEOUT (3600s by default), every design already written to
disk is discarded: the PDBs and per-model score.sc files are there, but
tab3_ncaa_designs.csv -- the file the Results table reads -- never gets
written, so the dashboard shows nothing and the job still reports `complete`.

On 16 November both SPTCT_e9a62ad5 and SPTDT_bdf407e1 hit that timeout after
producing 2 and 14 designs respectively. Those designs carry the D-canonical
and L-non-canonical residues that were requested; only the bookkeeping was
lost. This script recovers the bookkeeping without re-running Rosetta.

WHAT IT DOES NOT DO
-------------------
It does not invent numbers.

  * FoldX_Score is left EMPTY. The pass computes it by invoking FoldX per
    design; this script does not. An empty column is honest -- and for most
    of these designs FoldX has no parameters anyway, so it would be empty
    regardless. foldx_valid/foldx_unknown are left empty for the same reason.

  * parent_dG_separated and delta_dG_vs_parent are left EMPTY. The parent's
    dG_separated was never measured on the same scale as these designs.

  * control_dG_separated and delta_dG_vs_control ARE filled wherever a
    _baseline/<model>/score.sc exists, because that baseline run scored the
    undesigned parent through the same mover. A model whose baseline was not
    reached before the kill gets empty control columns rather than a guess.

  * verdict is RECOMPUTED here from delta_dG_vs_control, and verdict_basis
    says so explicitly, so nobody mistakes a reconstructed judgement for the
    pass's own.

The report it writes carries "reconstructed": true and the reason. That
matters: ncaa_status.json's ABSENCE is what the system treats as success, and
this script must not forge a success signal for a pass that was killed.

USAGE
-----
    python3 recover_ncaa_table.py <job_dir> [--dry-run]

    # inside the container, for one job:
    python3 /tmp/recover_ncaa_table.py /data/results/SPTCT_e9a62ad5 --dry-run
    python3 /tmp/recover_ncaa_table.py /data/results/SPTCT_e9a62ad5

Writes <job_dir>/tab3_ncaa_designs.csv and <job_dir>/ncaa_design_report.json,
matching the ownership of the job directory so the app can read them. Both
files are on scheduler.py's _KEEP_JOB_FILES whitelist, so they survive the
post-zip sweep.
"""
import csv
import json
import os
import sys

CANONICAL = {'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 'HIS',
             'ILE', 'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 'THR', 'TRP',
             'TYR', 'VAL'}

IFACE_COLUMNS = ('dG_separated', 'dG_separated/dSASAx100', 'dSASA_int',
                 'delta_unsatHbonds', 'hbonds_int', 'sc_value',
                 'nres_int', 'complex_normalized')

COLS = (['pdbId', 'parent', 'parentResidues', 'pepResidues',
         'n_noncanonical', 'noncanonical', 'verdict', 'verdict_basis',
         'delta_dG_vs_control', 'delta_dG_vs_parent',
         'control_dG_separated', 'parent_dG_separated']
        + list(IFACE_COLUMNS)
        + ['Rosetta_Total_Score', 'FoldX_Score', 'foldx_valid',
           'foldx_unknown', 'pdb_relative_path'])

OUT_CSV = 'tab3_ncaa_designs.csv'
OUT_REPORT = 'ncaa_design_report.json'
DESIGN_DIR = 'ncaa_designs'
BASELINE = '_baseline'
PEPTIDE_CHAIN = 'B'


def read_scorefile(path):
    """{description: {column: value}} -- same parser shape as the pass uses.

    The header is the SCORE: line containing total_score; every later SCORE:
    line is a row. InterfaceAnalyzerMover writes dG_separated into this same
    file, which is why the whole row is kept rather than just total_score.
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


def chain_residues(pdb_path, chain=PEPTIDE_CHAIN):
    """name3 per residue of `chain`, in order, from fixed PDB columns.

    Columns 18-20 are name3 and 22 is the chain id by the PDB standard; the
    residue number (23-26) plus insertion code (27) identifies a residue, so
    repeated ATOM lines for the same residue collapse to one entry.
    """
    seen = []
    last = None
    try:
        with open(pdb_path, errors='replace') as f:
            for line in f:
                if not (line.startswith('ATOM') or line.startswith('HETATM')):
                    continue
                if len(line) < 27 or line[21] != chain:
                    continue
                key = line[22:27]
                if key == last:
                    continue
                last = key
                seen.append(line[17:20].strip())
    except OSError:
        return []
    return seen


def fnum(row, col):
    """A float from a score row, or None -- never a fabricated 0.0."""
    try:
        return float(row[col])
    except (KeyError, TypeError, ValueError):
        return None


def collect(job_dir):
    design_root = os.path.join(job_dir, DESIGN_DIR)
    if not os.path.isdir(design_root):
        sys.exit('no %s/ in %s -- the pass never created its working '
                 'directory, so there is nothing to recover.'
                 % (DESIGN_DIR, job_dir))

    models = sorted(d for d in os.listdir(design_root)
                    if d != BASELINE
                    and os.path.isdir(os.path.join(design_root, d)))
    if not models:
        sys.exit('no model directories under %s/ -- nothing to recover.'
                 % DESIGN_DIR)

    rows = []
    notes = []
    per_model = {}

    for model in models:
        mdir = os.path.join(design_root, model)
        scores = read_scorefile(os.path.join(mdir, 'score.sc'))

        # The baseline run scores the UNDESIGNED parent through the same
        # mover, so it is the only control on a comparable scale. A model
        # killed before its baseline was written simply has none.
        bdir = os.path.join(design_root, BASELINE, model)
        control_dg = None
        if os.path.isdir(bdir):
            for row in read_scorefile(os.path.join(bdir, 'score.sc')).values():
                v = fnum(row, 'dG_separated')
                if v is not None:
                    control_dg = v
                    break
        if control_dg is None:
            notes.append('%s: no baseline score, control columns left empty'
                         % model)

        parent_res = chain_residues(os.path.join(mdir, model + '.pdb'))

        designs = sorted(n for n in os.listdir(mdir)
                         if n.startswith('ncaa_') and n.endswith('.pdb'))
        per_model[model] = len(designs)
        if not designs:
            notes.append('%s: no ncaa_*.pdb written' % model)

        for name in designs:
            pdb_id = name[:-4]
            path = os.path.join(mdir, name)
            res = chain_residues(path)
            ncaa = [r for r in res if r not in CANONICAL]

            row = scores.get(pdb_id) or {}
            if not row:
                notes.append('%s: %s has no row in score.sc' % (model, pdb_id))

            dg = fnum(row, 'dG_separated')
            delta = None if (dg is None or control_dg is None) else dg - control_dg

            if delta is None:
                verdict = 'unscored'
                basis = ('reconstructed: no comparable control, so no verdict '
                         'could be formed')
            elif delta < 0:
                verdict = 'improvement'
                basis = ('reconstructed: dG_separated %.3f vs baseline %.3f'
                         % (dg, control_dg))
            else:
                verdict = 'no_improvement'
                basis = ('reconstructed: dG_separated %.3f vs baseline %.3f'
                         % (dg, control_dg))

            entry = {
                'pdbId': pdb_id,
                'parent': model,
                'parentResidues': ' '.join(parent_res),
                'pepResidues': ' '.join(res),
                'n_noncanonical': len(ncaa),
                'noncanonical': ' '.join(sorted(set(ncaa))),
                'verdict': verdict,
                'verdict_basis': basis,
                'delta_dG_vs_control': '' if delta is None else round(delta, 3),
                'delta_dG_vs_parent': '',
                'control_dG_separated': ('' if control_dg is None
                                         else round(control_dg, 3)),
                'parent_dG_separated': '',
                'Rosetta_Total_Score': row.get('total_score', ''),
                'FoldX_Score': '',
                'foldx_valid': '',
                'foldx_unknown': '',
                'pdb_relative_path': os.path.join(DESIGN_DIR, model, name),
            }
            for col in IFACE_COLUMNS:
                entry[col] = row.get(col, '')
            rows.append(entry)

    # Same ordering the pass uses: scored-by-binding first, then by total
    # score, then the unscored -- dG_separated and total_score are different
    # quantities and must not be mixed in one sort.
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
    return rows, notes, per_model, models


def main(argv):
    dry = '--dry-run' in argv
    args = [a for a in argv if not a.startswith('--')]
    if not args:
        sys.exit(__doc__.strip().splitlines()[0] +
                 '\n\nusage: recover_ncaa_table.py <job_dir> [--dry-run]')
    job_dir = os.path.abspath(args[0].rstrip('/'))
    if not os.path.isdir(job_dir):
        sys.exit('not a directory: %s' % job_dir)

    existing = os.path.join(job_dir, OUT_CSV)
    if os.path.exists(existing):
        sys.exit('REFUSING: %s already exists. The pass wrote its own table, '
                 'so there is nothing to recover and overwriting it would '
                 'replace measured values with reconstructed ones.' % existing)

    rows, notes, per_model, models = collect(job_dir)

    placed = sum(1 for r in rows if r['n_noncanonical'])
    all_ncaa = sorted({c for r in rows for c in r['noncanonical'].split() if c})

    print('job:              %s' % os.path.basename(job_dir))
    print('models recovered: %d  (%s)'
          % (len(models), ', '.join('%s=%d' % (m, per_model[m])
                                    for m in models)))
    print('designs:          %d' % len(rows))
    print('with NCAA:        %d' % placed)
    print('residues placed:  %s' % (' '.join(all_ncaa) or '(none)'))
    for n in notes:
        print('  note: %s' % n)

    report = {
        'job': os.path.basename(job_dir),
        'reconstructed': True,
        'reconstructed_reason': (
            'run_ncaaDesign.py was abandoned on PREDPEP_NCAA_TIMEOUT before '
            'writing its table. This file was rebuilt from the per-model '
            'score.sc files and design PDBs left on disk. It is NOT the '
            'pass\'s own report: FoldX scores and parent comparisons were '
            'never computed and are empty, and every verdict here was '
            'recomputed against the baseline run.'),
        'designs_produced': len(rows),
        'designs_with_noncanonical': placed,
        'models_designed': len(models),
        'designs_per_model': per_model,
        'noncanonical_placed': all_ncaa,
        'notes': notes,
        'foldx': False,
    }

    if dry:
        print()
        print('--dry-run: nothing written. Would write:')
        print('  %s' % os.path.join(job_dir, OUT_CSV))
        print('  %s' % os.path.join(job_dir, OUT_REPORT))
        print()
        print('first row:')
        if rows:
            for k in COLS:
                print('  %-24s %s' % (k, rows[0].get(k, '')))
        return

    out_csv = os.path.join(job_dir, OUT_CSV)
    with open(out_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in COLS})

    out_report = os.path.join(job_dir, OUT_REPORT)
    with open(out_report, 'w') as f:
        json.dump(report, f, indent=2)

    # The app may not run as the user this script does; match the job
    # directory's ownership rather than leaving root-owned files behind.
    try:
        st = os.stat(job_dir)
        for p in (out_csv, out_report):
            os.chown(p, st.st_uid, st.st_gid)
            os.chmod(p, 0o644)
    except (OSError, AttributeError) as e:
        print('could not set ownership (%s); check the app can read them' % e)

    print()
    print('wrote %s' % out_csv)
    print('wrote %s' % out_report)
    print()
    print('The Results table reads the CSV; PDBs stream from '
          'pdb_relative_path, which points into %s/.' % DESIGN_DIR)


if __name__ == '__main__':
    main(sys.argv[1:])
