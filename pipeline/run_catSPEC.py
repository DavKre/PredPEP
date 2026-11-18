#!/home/spacepep/miniforge3/envs/predPEP/bin/python
"""Aggregate pepspec .spec files into the score tables the dashboard reads.

Changes from the previous version, each marked CHANGE below:

1. Deduplication no longer keys on the one-letter pepSeq. Rosetta writes every
   non-canonical residue as 'X' (their params declare IO_STRING '<CODE> X'), so
   two designs differing only in WHICH non-canonical they carry produced the same
   pepSeq string and drop_duplicates silently discarded one of them. With ~141
   non-canonical residues in play that loses real designs. The key is now the
   three-letter residue sequence read from the model PDB, falling back to pepSeq
   only when the PDB cannot be read.

2. A pepResidues column is emitted (space-separated three-letter codes). The
   front end and job_analytics.py prefer it over pepSeq for the same reason, and
   it is what makes a D/L pair distinguishable in the results table.

3. BASE_RESULT_FOLDER comes from the environment, matching predPEP.py and
   scheduler.py. It is used to build pdb_relative_path, which /stream_final_pdb
   resolves against, so a mismatch breaks the structure viewer.

Everything else -- the score regex, the artifact-column drop, the three output
files, the damage tally and the exit codes -- is unchanged.
"""

import glob
import json
import os
import re
import sys

import pandas as pd

# CHANGE 3: was hardcoded '/tmp/pepspec/results'.
BASE_RESULT_FOLDER = os.environ.get('PREDPEP_RESULT_DIR', '/tmp/pepspec/results')

# Residues Rosetta writes with their own three-letter name. Anything else in
# chain B is a non-canonical or a D-residue and is the whole reason this script
# now reads residue names instead of trusting the one-letter sequence.
CANONICAL_3 = {
    'ALA', 'ARG', 'ASN', 'ASP', 'CYS', 'GLN', 'GLU', 'GLY', 'HIS', 'ILE',
    'LEU', 'LYS', 'MET', 'PHE', 'PRO', 'SER', 'THR', 'TRP', 'TYR', 'VAL',
}

_pdb_residue_cache = {}


def template_disulfides(root='.', chain='B'):
    """Chain-<chain> disulfide pairs from the template's SSBOND records.

    Read from any iteration's inputPDB/*.mod.pdb -- they all stage the same
    template. Returned as 1-based SEQUENCE positions (not resSeq), because that
    is what indexes the pepResidues column.

    Rosetta keeps these cysteines in most runs, but not all: the packer will
    substitute one when the score favours it, breaking the bond. Recording
    whether each design kept it turns that from an invisible event into a
    column you can filter and count.
    """
    for dirpath, _dirs, files in os.walk(root):
        if os.path.basename(dirpath) != 'inputPDB':
            continue
        for name in sorted(files):
            if not name.endswith('.pdb'):
                continue
            path = os.path.join(dirpath, name)
            # resSeq -> 1-based sequence position for this chain.
            index, order = {}, []
            try:
                with open(path, errors='replace') as f:
                    for line in f:
                        if not line.startswith(('ATOM', 'HETATM')) or len(line) < 27:
                            continue
                        if line[21:22] != chain or line[12:16].strip() != 'CA':
                            continue
                        if line[76:78].strip() not in ('C', ''):
                            continue
                        key = line[22:27]
                        if key in index:
                            continue
                        index[key] = None
                        order.append(int(line[22:26]))
            except OSError:
                continue
            if not order:
                continue
            index = {rs: i + 1 for i, rs in enumerate(order)}
            pairs = set()
            try:
                with open(path, errors='replace') as f:
                    for line in f:
                        if not line.startswith('SSBOND') or len(line) < 36:
                            continue
                        if line[15] != chain or line[29] != chain:
                            continue
                        try:
                            a, b = int(line[17:21]), int(line[31:35])
                        except ValueError:
                            continue
                        if a in index and b in index:
                            pairs.add(tuple(sorted((index[a], index[b]))))
            except OSError:
                continue
            if pairs:
                return sorted(pairs), path
    return [], None


def chain_b_residues(pdb_path):
    """Chain-B residue names in order, as three-letter codes.

    Keyed on resSeq + iCode so a multi-model file collapses to one copy, and
    guarded on the element column so calcium (atom name CA) is not mistaken for
    an alpha carbon. Returns [] when the file cannot be read -- the caller then
    falls back to the one-letter sequence rather than dropping the design.
    """
    if pdb_path in _pdb_residue_cache:
        return _pdb_residue_cache[pdb_path]

    seen = {}
    try:
        with open(pdb_path, errors='replace') as f:
            for line in f:
                if not line.startswith(('ATOM', 'HETATM')):
                    continue
                if len(line) < 27:
                    continue
                if line[21:22] != 'B':
                    continue
                if line[12:16].strip() != 'CA':
                    continue
                if line[76:78].strip() not in ('C', ''):
                    continue
                key = line[22:27]
                if key not in seen:
                    seen[key] = line[17:20].strip()
    except OSError:
        seen = {}

    residues = list(seen.values())
    # Bounded cache: a large job has thousands of models and we only ever need
    # each path once, but a stray re-read should not grow memory without limit.
    if len(_pdb_residue_cache) < 20000:
        _pdb_residue_cache[pdb_path] = residues
    return residues


def process_peptide_spec_files(project_dir='.'):
    """Find all .spec files, clean and structure the data, save three CSVs."""
    PROCESSED_SCORES_FILE = 'processed_peptide_scores.csv'
    ALL_SCORES_FILE = 'all_model_scores.csv'
    NON_MC_PDB_FILE = 'non_mc_pdbs.csv'
    REPORT_FILE = 'aggregation_report.json'

    print(f"🔍 Searching for .spec files in '{os.path.abspath(project_dir)}'...")
    spec_files = glob.glob(os.path.join(project_dir, '**', '*.spec'), recursive=True)

    if not spec_files:
        print('❌ No .spec files found. Exiting.')
        return False

    print(f'✅ Found {len(spec_files)} .spec files. Starting processing...')

    all_data = []
    # Pattern to extract key-value scores (e.g. total_score: -435.778)
    score_pattern = re.compile(r'(\w+-\w+|\w+):?\s*?([+-]?\s*\d+\.\d+|\d+)')

    # Damage tally — a .spec file can be truncated or corrupted on disk (seen in job
    # SPILRH_c70d2d4c: a 61-byte burst of invalid UTF-8 inside one of 480 files).
    # One bad file must cost that file, never the whole aggregation, so every file is
    # read leniently AND isolated in its own try/except. Counts are reported at the
    # end and drive the exit code so the caller can flag the run as degraded.
    unreadable_files = []
    corrupt_line_count = 0
    residues_recovered = 0
    residues_missing = 0

    for file_path in spec_files:

        # Determine the iteration folder from the .spec file's location
        # Example: /tmp/pepspec/results/SPCRCK/SPCRCK_iter6_2f0e/score.spec
        spec_dir = os.path.dirname(file_path)
        iteration_folder = os.path.basename(spec_dir)

        try:
            # errors='replace': undecodable bytes become U+FFFD instead of raising,
            # so the readable records of a damaged file are still recovered.
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = list(f)
        except OSError as e:
            unreadable_files.append(file_path)
            print(f'⚠️ Skipping unreadable .spec file {file_path}: {e}')
            continue

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # A record containing replacement characters was damaged on disk;
            # any number parsed out of it is untrustworthy, so drop the record
            # rather than importing mangled column names and values.
            if '�' in line:
                corrupt_line_count += 1
                print(f'⚠️ Skipping corrupted line in {file_path} (undecodable bytes)')
                continue

            parts = line.split()
            if len(parts) < 3:
                print(f'⚠️ Skipping malformed line in {file_path}: {line}')
                continue

            full_pdb_path = parts[0]
            pep_seq = parts[1]
            pep_id = os.path.basename(full_pdb_path)

            # 1. Calculate the relative path from BASE_RESULT_FOLDER
            try:
                relative_path_from_spec = os.path.relpath(full_pdb_path, BASE_RESULT_FOLDER)
            except ValueError:
                # Fallback for paths that might span different drives/file systems
                relative_path_from_spec = full_pdb_path.replace(
                    BASE_RESULT_FOLDER, '', 1).lstrip(os.sep)

            # --- Insert the iteration folder in the right position -------------
            # 'SPCRCK/SPCRCKLIL5.pdbs/..._10.pdb'
            #   -> 'SPCRCK/SPCRCK_iter3_45ce/SPCRCKLIL5.pdbs/..._10.pdb'
            try:
                first_sep_index = relative_path_from_spec.index(os.sep)
                path_prefix_with_sep = relative_path_from_spec[:first_sep_index + 1]
                path_suffix = relative_path_from_spec[first_sep_index + 1:]
            except ValueError:
                print('⚠️ Warning: Malformed path fragment, cannot insert iteration '
                      f'folder: {relative_path_from_spec}')
                relative_path = relative_path_from_spec
            else:
                relative_path = os.path.join(path_prefix_with_sep, iteration_folder,
                                             path_suffix)

            # CHANGE 2: three-letter residue codes, read from the model itself.
            # The one-letter pep_seq cannot carry them: every non-canonical is 'X'.
            residues = chain_b_residues(full_pdb_path)
            if not residues:
                # The .spec references the path before cleanup; try the
                # reconstructed job-relative location before giving up.
                alt = os.path.join(project_dir, *relative_path.split(os.sep)[1:])
                residues = chain_b_residues(alt)
            if residues:
                residues_recovered += 1
            else:
                residues_missing += 1

            row_data = {
                'pepID': pep_id,
                'pepSeq': pep_seq,
                'pepResidues': ' '.join(residues),
                'pdb_relative_path': relative_path,
            }

            score_string = ' '.join(parts[2:])
            for key, value_str in score_pattern.findall(score_string):
                cleaned_value = value_str.strip().replace(' ', '')
                try:
                    row_data[key] = float(cleaned_value)
                except ValueError:
                    row_data[key] = cleaned_value

            all_data.append(row_data)

    # 2. Convert the list of dictionaries to a pandas DataFrame
    df_all = pd.DataFrame(all_data)
    original_rows = len(df_all)

    if 'total_score' not in df_all.columns:
        print("🛑 Error: 'total_score' column not found in data. "
              'Cannot proceed with sorting/filtering.')
        df_all.to_csv(ALL_SCORES_FILE, index=False)
        return False

    # 3. Remove artifact columns.
    #
    # CHANGE 4: this was a hardcoded list, UNWANTED_COLS = ['43','42','40','41','29'].
    # It does not generalise: the score regex turns any stray number in a .spec
    # line into a column, and which numbers appear varies per run. A real TYK2
    # aggregation produced 45, 44, 46, 47, 39, 37, 36 and 27 -- none of them on
    # the list, all of them surviving into the output. The rule is what was
    # meant: a column whose NAME is purely numeric is never a score term.
    cols_to_drop = [c for c in df_all.columns if str(c).strip().isdigit()]
    if cols_to_drop:
        df_all.drop(columns=cols_to_drop, inplace=True)
        print(f'🧹 Removed {len(cols_to_drop)} numeric artifact column(s): '
              f'{sorted(cols_to_drop, key=lambda c: int(c))}')
    else:
        print('✔️ No numeric artifact columns found.')

    # CHANGE 5: disulfide integrity per design.
    #
    # The template's SSBOND cysteines are usually preserved, but not always --
    # the packer substitutes one when that scores better, silently losing the
    # bond. This records it per design so a broken bond is filterable rather
    # than invisible, and reports how often it happened.
    ss_pairs, ss_source = template_disulfides(project_dir)
    ss_broken = 0
    if ss_pairs and 'pepResidues' in df_all.columns:
        ss_positions = sorted({p for pair in ss_pairs for p in pair})

        def _intact(residues_str):
            if not residues_str:
                return None                       # unknown, not "broken"
            codes = str(residues_str).split()
            for pos in ss_positions:
                if pos > len(codes) or codes[pos - 1] != 'CYS':
                    return False
            return True

        df_all['disulfide_intact'] = df_all['pepResidues'].map(_intact)
        ss_broken = int((df_all['disulfide_intact'] == False).sum())  # noqa: E712
        print(f'🔗 Template disulfide(s) at sequence position(s) '
              f'{ss_pairs} (from {os.path.basename(ss_source or "?")}).')
        if ss_broken:
            print(f'⚠️ {ss_broken} of {len(df_all)} design(s) lost a disulfide '
                  f'cysteine ({100.0 * ss_broken / max(1, len(df_all)):.1f}%). '
                  'Filter on disulfide_intact to exclude them.')
        else:
            print('✔️ Every design kept the template disulfide cysteine(s).')
    elif ss_pairs:
        print('🔗 Template has a disulfide but pepResidues is unavailable; '
              'integrity not checked.')

    # 4. Consistent column order (identity columns first)
    lead = ['pepID', 'pepSeq', 'pepResidues', 'pdb_relative_path']
    if 'disulfide_intact' in df_all.columns:
        lead.append('disulfide_intact')
    cols_ordered = lead + [c for c in df_all.columns if c not in lead]

    # 5. Save ALL scores before filtering (for completeness/debugging)
    df_all[cols_ordered].to_csv(ALL_SCORES_FILE, index=False)
    print(f'💾 All model scores saved to **{ALL_SCORES_FILE}** ({len(df_all)} rows).')

    # 6. Best model per unique peptide.
    #
    # CHANGE 1: the dedup key. pepSeq renders every non-canonical as 'X', so
    # 'W L 004 K' and 'W L NAL K' were both 'WLXK' and one was discarded. Key on
    # the three-letter sequence where it was recovered, and fall back to pepSeq
    # only for rows whose PDB could not be read.
    print('📈 Identifying best-scoring model (lowest total_score) for each unique '
          'peptide...')
    df_all_sorted = df_all.sort_values(by='total_score', ascending=True)
    dedup_key = df_all_sorted['pepResidues'].where(
        df_all_sorted['pepResidues'].astype(bool), df_all_sorted['pepSeq'])
    df_unique = df_all_sorted.loc[~dedup_key.duplicated(keep='first')].copy()
    rows_removed = original_rows - len(df_unique)

    # Report what the old key WOULD have collapsed, so the effect of the fix is
    # visible in the run log rather than invisible.
    legacy_unique = int(df_all_sorted['pepSeq'].nunique())
    if legacy_unique < len(df_unique):
        print(f'ℹ️ Residue-level keys kept {len(df_unique)} designs; the one-letter '
              f'sequence would have kept only {legacy_unique} '
              f'({len(df_unique) - legacy_unique} distinct design(s) recovered).')

    # 7. Save the BEST scores (for merging with FoldX)
    df_unique[cols_ordered].to_csv(PROCESSED_SCORES_FILE, index=False)
    print(f'💾 Best scores saved to **{PROCESSED_SCORES_FILE}** ({len(df_unique)} rows).')

    # 8. Create the NON-MC PDB list (with all scores)
    print('📋 Identifying base PDBs (non-_mc, non-_full, non-_soft) with all scores...')
    # Non-capturing group: a capturing one makes pandas warn that the match
    # groups are being discarded (seen in the real run's output).
    mc_pattern = r'(?:_full|_soft|_mc\d+)\.pdb$'
    df_non_mc = df_all[~df_all['pepID'].str.contains(mc_pattern, regex=True, case=False)].copy()
    df_non_mc.drop_duplicates(subset=['pepID'], keep='first', inplace=True)
    df_non_mc[cols_ordered].to_csv(NON_MC_PDB_FILE, index=False)
    print(f'💾 Base PDB IDs and all scores saved to **{NON_MC_PDB_FILE}** '
          f'({len(df_non_mc)} rows).')

    print('\n--- Results Summary ---')
    print(f'Total rows processed: **{original_rows}**')
    print(f'Rows kept in {PROCESSED_SCORES_FILE} (best-scoring model): **{len(df_unique)}**')
    print(f'Base PDBs kept in {NON_MC_PDB_FILE}: **{len(df_non_mc)}**')
    print(f'Rows removed (suboptimal duplicates): **{rows_removed}**')
    print(f'Residue names read from model PDBs: **{residues_recovered}** '
          f'(fell back to one-letter for {residues_missing})')
    if unreadable_files or corrupt_line_count:
        print(f'⚠️ Degraded input: {len(unreadable_files)} unreadable file(s), '
              f'{corrupt_line_count} corrupted line(s) skipped.')
        for p in unreadable_files:
            print(f'   unreadable: {p}')
    print('-------------------------')

    # Machine-readable damage report, zipped with the results so a consumer can
    # tell a clean aggregation from a degraded one without parsing stderr.
    try:
        with open(REPORT_FILE, 'w') as f:
            json.dump({
                'spec_files_found': len(spec_files),
                'spec_files_unreadable': len(unreadable_files),
                'unreadable_files': unreadable_files,
                'corrupt_lines_skipped': corrupt_line_count,
                'rows_processed': int(original_rows),
                'unique_designs': int(len(df_unique)),
                'unique_designs_one_letter_key': legacy_unique,
                'residues_recovered': residues_recovered,
                'residues_missing': residues_missing,
                'non_mc_rows': int(len(df_non_mc)),
                'template_disulfides': [list(p) for p in ss_pairs],
                'designs_disulfide_broken': ss_broken,
                'degraded': bool(unreadable_files or corrupt_line_count),
            }, f, indent=2)
    except OSError as e:
        print(f'⚠️ Could not write {REPORT_FILE}: {e}')

    return True


if __name__ == '__main__':
    # Exit non-zero when the aggregation did not produce its critical output.
    # Degraded-but-complete runs (a few skipped files/lines) still exit 0 — the
    # damage is recorded in aggregation_report.json.
    ok = process_peptide_spec_files()
    if not ok or not os.path.exists('non_mc_pdbs.csv'):
        print('🛑 Aggregation failed to produce non_mc_pdbs.csv.', file=sys.stderr)
        sys.exit(1)
