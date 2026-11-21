#!/home/spacepep/miniforge3/envs/predPEP/bin/python

"""Merge FoldX and Rosetta scores into the table the dashboard reads.

WHAT CHANGED AND WHY
--------------------
1. POST-RUN FILTERS ARE NOW APPLIED. The submission form has offered
   "keep only designs with the template disulfide intact" and "require the
   native residue at these positions" for a while, and predPEP.py recorded both
   in design_spec.json -- but nothing acted on them. A ticked box that does
   nothing is worse than no box, so this reads the spec and evaluates them.

   They are applied NON-DESTRUCTIVELY. tab2_final_scores.csv keeps every row
   and gains boolean columns (disulfide_ok, native_ok, passes_filters); the
   subset is written separately to tab2_filtered_scores.csv. Reason: on a real
   job the final table held 230 base designs, and requiring the native residue
   at a single position cut it to roughly a dozen. Silently shipping a table
   with 5% of the rows -- and no way to see what was removed or why -- is how a
   filter becomes a bug report. The counts are printed and the full table stays
   available, so the UI can offer the filter as a toggle.

2. The shebang pointed at /home/spacepep/miniforge3/..., which does not exist on
   this node. The environment is /root/mambaforge/envs/predPEP.

3. disulfide_intact and pepResidues are carried through the merge. run_catSPEC.py
   computes them; the old final_cols list dropped them, so the information was
   produced and then discarded one step later.

4. top10 is selected from the designs that PASS the filters when any do, since
   a "best 10" that includes designs the user asked to exclude is misleading.
   Falls back to the unfiltered ranking with a warning when none pass.

INDEXING NOTE
-------------
pepResidues is indexed by 1-based SEQUENCE POSITION (run_catSPEC.py builds it
that way). require_native_at in the spec holds PDB resSeq values, because the
form's residue strip is numbered from the template. The two coincide only when
the peptide starts at residue 1. They are converted here using
peptide.start_resseq from the spec; if that is missing, resSeq is assumed to be
the sequence position and a warning is printed rather than guessing silently.
"""

import pandas as pd
import os
import re
import sys
import json
import shutil

# --- Configuration ---
ROSETTA_INPUT_FILE = "non_mc_pdbs.csv"          # Rosetta scores, pepSeq, paths
OUTPUT_CLEANED_CSV = "tab2_final_scores.csv"    # read by the results table
OUTPUT_FILTERED_CSV = "tab2_filtered_scores.csv"
DESIGN_SPEC_FILE = "design_spec.json"
FILTER_REPORT_FILE = "filter_report.json"

# Strips the per-model suffix from a FoldX pdbId to get the design base:
# 'SPILRH1Q672F1_7.pdb' / 'SPILRH1Q672F1_7.mod.pdb' -> 'SPILRH1Q672F1'
_MODEL_SUFFIX = re.compile(r'_\d+(\.mod)?\.pdb$', re.IGNORECASE)

THREE_TO_ONE = {
    'ALA': 'A', 'ARG': 'R', 'ASN': 'N', 'ASP': 'D', 'CYS': 'C', 'GLN': 'Q',
    'GLU': 'E', 'GLY': 'G', 'HIS': 'H', 'ILE': 'I', 'LEU': 'L', 'LYS': 'K',
    'MET': 'M', 'PHE': 'F', 'PRO': 'P', 'SER': 'S', 'THR': 'T', 'TRP': 'W',
    'TYR': 'Y', 'VAL': 'V',
}


def _design_base(pdb_id):
    return _MODEL_SUFFIX.sub('', str(pdb_id))


def _scan_job_tree(root='.'):
    """One walk of the job dir collecting what the Rosetta aggregate would have given us:
    the designed peptide sequence per design (from the chain-B fastas clean_pdb.py writes)
    and a basename -> job-relative path index for the model PDBs.

    Returns (sequences, pdb_paths). Paths are prefixed with the job dir name to match the
    format produced by run_catSPEC.py, which /stream_final_pdb expects.
    """
    sequences = {}
    pdb_paths = {}
    job_dir_name = os.path.basename(os.path.abspath(root))
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith('_B.fasta'):
                design = name[:-len('_B.fasta')]
                if design in sequences:
                    continue
                try:
                    with open(os.path.join(dirpath, name), encoding='utf-8', errors='replace') as f:
                        seq = ''.join(l.strip() for l in f if not l.startswith('>'))
                except OSError:
                    continue
                if seq:
                    sequences[design] = seq
            elif name.endswith('.pdb') and name not in pdb_paths:
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                pdb_paths[name] = os.path.join(job_dir_name, rel)
    return sequences, pdb_paths


# ------------------------------------------------------------- post-filters

def load_spec(path=DESIGN_SPEC_FILE):
    """The submission's design spec, or {} when the job predates it."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            spec = json.load(f)
        return spec if isinstance(spec, dict) else {}
    except (OSError, ValueError) as e:
        print(f"⚠️ Could not read {path}: {e}. Post-run filters not applied.")
        return {}


def _template_has_no_disulfide(path='aggregation_report.json'):
    """True only when run_catSPEC.py positively reported ZERO template disulfides.

    Returns False when the report is missing or unreadable, so an absent report
    is treated as "unknown" and the filter stays unevaluable rather than being
    silently waved through.
    """
    try:
        with open(path) as f:
            rep = json.load(f)
    except (OSError, ValueError):
        return False
    pairs = rep.get('template_disulfides')
    return isinstance(pairs, list) and len(pairs) == 0


def _native_at_positions(spec):
    """{sequence_position: three_letter_native} for the requested resSeqs.

    Returns ({}, note) when nothing was requested or the mapping is impossible.
    """
    pf = spec.get('post_filters') or {}
    want = [int(p) for p in (pf.get('require_native_at') or [])]
    if not want:
        return {}, None

    pep = spec.get('peptide') or {}
    residues = pep.get('residues') or []
    if not residues:
        return {}, ('require_native_at was requested but the spec records no '
                    'template residues; cannot evaluate.')

    start = pep.get('start_resseq')
    note = None
    if start is None:
        start = 1
        note = ('peptide.start_resseq missing from the spec; assuming the '
                'peptide starts at residue 1 when mapping require_native_at.')

    out = {}
    bad = []
    for resseq in want:
        pos = resseq - int(start) + 1
        if 1 <= pos <= len(residues):
            out[pos] = str(residues[pos - 1]).upper()
        else:
            bad.append(resseq)
    if bad:
        extra = (f'require_native_at listed residue(s) {bad} outside the '
                 f'peptide ({start}..{int(start) + len(residues) - 1}); ignored.')
        note = f'{note} {extra}' if note else extra
    return out, note


def apply_post_filters(df, spec):
    """Add disulfide_ok / native_ok / passes_filters. Drops nothing.

    A filter that cannot be evaluated for a row leaves that row's flag as NA and
    counts it as NOT passing, because "we could not check" must not read as
    "it passed".
    """
    pf = spec.get('post_filters') or {}
    want_ss = bool(pf.get('require_disulfide_intact'))
    native_map, note = _native_at_positions(spec)
    if note:
        print(f"⚠️ {note}")

    report = {
        'require_disulfide_intact': want_ss,
        'require_native_at': sorted(
            int(p) for p in (pf.get('require_native_at') or [])),
        'native_positions_resolved': {str(k): v for k, v in sorted(native_map.items())},
        'rows_total': int(len(df)),
        'notes': [note] if note else [],
    }

    # --- disulfide
    if want_ss:
        if 'disulfide_intact' in df.columns:
            # run_catSPEC.py writes True / False / None (None = not checkable).
            df['disulfide_ok'] = df['disulfide_intact'].map(
                lambda v: True if v is True or str(v).strip().lower() == 'true'
                else (False if v is False or str(v).strip().lower() == 'false'
                      else pd.NA))
            unknown = int(df['disulfide_ok'].isna().sum())
            report['disulfide_unknown'] = unknown
            if unknown:
                print(f"⚠️ {unknown} row(s) have no disulfide_intact value; "
                      "they are reported as not passing rather than assumed intact.")
        elif _template_has_no_disulfide():
            # INAPPLICABLE, not unevaluable. run_catSPEC.py omits the column in
            # both cases, so without this check a linear peptide reported
            # "0 of 800 pass" -- which reads as "every design is broken" when
            # the truth is there is no bond to break. A filter with nothing to
            # act on is satisfied, not failed.
            df['disulfide_ok'] = True
            msg = ('require_disulfide_intact was requested but the template '
                   'has no disulfide; nothing to preserve, so the filter does '
                   'not exclude anything.')
            print(f"ℹ️ {msg}")
            report['notes'].append(msg)
            report['disulfide_filter_inapplicable'] = True
        else:
            df['disulfide_ok'] = pd.NA
            msg = ('require_disulfide_intact was requested but no '
                   'disulfide_intact column reached this step; the filter '
                   'could not be evaluated.')
            print(f"⚠️ {msg}")
            report['notes'].append(msg)
    else:
        df['disulfide_ok'] = True

    # --- native residues
    if native_map:
        if 'pepResidues' in df.columns:
            positions = sorted(native_map)

            def _native_ok(residues_str):
                if not residues_str or pd.isna(residues_str):
                    return pd.NA
                codes = str(residues_str).split()
                for pos in positions:
                    if pos > len(codes):
                        return pd.NA
                    if codes[pos - 1].upper() != native_map[pos]:
                        return False
                return True

            df['native_ok'] = df['pepResidues'].map(_native_ok)
            unknown = int(df['native_ok'].isna().sum())
            report['native_unknown'] = unknown
            if unknown:
                print(f"⚠️ {unknown} row(s) have no usable pepResidues; "
                      "native-residue filter not evaluable for them.")
        else:
            df['native_ok'] = pd.NA
            msg = ('require_native_at was requested but no pepResidues column '
                   'reached this step; the filter could not be evaluated.')
            print(f"⚠️ {msg}")
            report['notes'].append(msg)
    else:
        df['native_ok'] = True

    # Written elementwise rather than via fillna(False).astype(bool): on an
    # object/NA column pandas emits a downcasting FutureWarning that ends up in
    # the job log looking like an error. Only True counts as passing, so NA and
    # False both fail here, which is the intended semantics.
    def _is_true(v):
        return v is True or str(v).strip().lower() == 'true'

    df['passes_filters'] = [bool(_is_true(a) and _is_true(b)) for a, b in
                            zip(df['disulfide_ok'], df['native_ok'])]

    passed = int(df['passes_filters'].sum())
    report['rows_passing'] = passed
    report['filters_active'] = bool(want_ss or native_map)

    if report['filters_active']:
        pct = 100.0 * passed / max(1, len(df))
        print(f"🔎 Post-run filters: {passed} of {len(df)} design(s) pass "
              f"({pct:.1f}%).")
        if want_ss and 'disulfide_ok' in df.columns:
            broke = int((df['disulfide_ok'] == False).sum())  # noqa: E712
            if broke:
                print(f"   · {broke} excluded for a broken disulfide.")
        if native_map:
            nat = int((df['native_ok'] == False).sum())  # noqa: E712
            shown = ', '.join(f'{p}:{native_map[p]}' for p in sorted(native_map))
            if nat:
                print(f"   · {nat} excluded for not matching {shown}.")
        if passed == 0:
            print("⚠️ NO design passes the requested filters. The full table is "
                  "still written; the filtered table will be empty.")
        elif passed < 20:
            print(f"⚠️ Only {passed} design(s) pass. That is a small sample — "
                  "consider relaxing the filters rather than ranking on it.")
    else:
        print("🔎 No post-run filters requested.")

    return df, report


def _write_foldx_only(df_foldx, output_file, spec):
    """Emit the score table we CAN build when Rosetta aggregation produced nothing.

    Rosetta_Total_Score is left EMPTY -- never 0 and never invented -- and the
    degradation is marked in-band via rosetta_available so a consumer reading only
    the CSV can tell it is partial.
    """
    print("⚠️ Building a FoldX-only score table (Rosetta scores unavailable).")
    sequences, pdb_paths = _scan_job_tree('.')

    df = df_foldx[['pdbId', 'ddG_Score']].copy()
    df['pepSeq'] = df['pdbId'].map(lambda p: sequences.get(_design_base(p), ''))
    df['pdb_relative_path'] = df['pdbId'].map(lambda p: pdb_paths.get(str(p), ''))
    df['Total_Energy'] = pd.NA          # -> empty cell in the CSV
    df['energy_available'] = False

    # The filters need pepResidues / disulfide_intact, which only run_catSPEC.py
    # produces. In this degraded path they are absent, so apply_post_filters
    # reports them as unevaluable instead of pretending they passed.
    df, report = apply_post_filters(df, spec)
    report['degraded'] = True

    cols = ['pdbId', 'pepSeq', 'pdb_relative_path', 'ddG_Score',
            'Total_Energy', 'energy_available',
            'disulfide_ok', 'native_ok', 'passes_filters']
    df = df[[c for c in cols if c in df.columns]]
    df.to_csv(output_file, index=False)
    _write_filtered(df, report)

    missing_seqs = int((df['pepSeq'] == '').sum())
    print(f"💾 Degraded score table saved to **{output_file}** ({len(df)} rows, "
          f"Rosetta scores empty).")
    if missing_seqs:
        print(f"⚠️ {missing_seqs} row(s) have no recoverable peptide sequence.")
    return df


def _write_filtered(df_final, report):
    """The filtered subset and the report, as separate files.

    Written even when empty: a zero-row file with a header is an answer, a
    missing file is an ambiguity.
    """
    try:
        if report.get('filters_active'):
            df_final[df_final['passes_filters']].to_csv(
                OUTPUT_FILTERED_CSV, index=False)
            print(f"💾 Filtered score table saved to **{OUTPUT_FILTERED_CSV}** "
                  f"({report.get('rows_passing', 0)} rows).")
        elif os.path.exists(OUTPUT_FILTERED_CSV):
            # Stale file from an earlier run with different filters.
            os.remove(OUTPUT_FILTERED_CSV)
    except Exception as e:
        print(f"⚠️ Could not write {OUTPUT_FILTERED_CSV}: {e}")
    try:
        with open(FILTER_REPORT_FILE, 'w') as f:
            json.dump(report, f, indent=2)
    except OSError as e:
        print(f"⚠️ Could not write {FILTER_REPORT_FILE}: {e}")


def merge_and_clean_scores(foldx_file, rosetta_file, output_file):

    """
    Merges FoldX scores with Rosetta scores, including all necessary
    columns (pepSeq, pdb_relative_path) for the front end.
    """

    spec = load_spec()

    # 1. Load FoldX Data
    if not os.path.exists(foldx_file):
        print(f"❌ FoldX master file not found: {foldx_file}. Skipping merge.")
        return None

    print(f"📂 Loading FoldX scores from {foldx_file}...")
    df_foldx = pd.read_csv(foldx_file, sep=r'\s+', header=None, names=['pdbId', 'ddG_Score'], usecols=[0, 1])

    # 2. Load Rosetta Data (includes pepSeq and pdb_relative_path)
    # If it is missing the aggregation step failed, but the FoldX ranking above is
    # still valid: emit a degraded table rather than nothing at all (job
    # SPILRH_c70d2d4c produced 4,795 scored designs and no score table).
    if not os.path.exists(rosetta_file):
        print(f"❌ Rosetta scores file not found: {rosetta_file}. Falling back to FoldX-only table.")
        return _write_foldx_only(df_foldx, output_file, spec)


    print(f"📂 Loading Rosetta scores from {rosetta_file}...")
    df_rosetta = pd.read_csv(rosetta_file)

    # 3. Prepare Rosetta data for merging
    df_rosetta.rename(columns={'pepID': 'pdbId_full'}, inplace=True)

    # Create a more robust key for merging
    # Einen robusteren Key für das Merging erstellen
    df_rosetta['pdbId_cleaned'] = df_rosetta['pdbId_full'].apply(os.path.basename)
    df_rosetta['pdbId_key'] = df_rosetta['pdbId_cleaned'].str.replace(r'\.mod\.pdb$', '', regex=True).str.replace(r'\.pdb$', '', regex=True)
    df_foldx['pdbId_key'] = df_foldx['pdbId'].str.replace(r'\.mod\.pdb$', '', regex=True).str.replace(r'\.pdb$', '', regex=True)

    # --- DEBUG LOGGING ---
    print(f"DEBUG: Rosetta Unique Keys (first 3): {df_rosetta['pdbId_key'].unique()[:3].tolist()}")
    print(f"DEBUG: FoldX Unique Keys (first 3):   {df_foldx['pdbId_key'].unique()[:3].tolist()}")

    # Check for intersection
    common_keys = set(df_rosetta['pdbId_key']).intersection(set(df_foldx['pdbId_key']))
    print(f"DEBUG: Found {len(common_keys)} matching keys between Rosetta and FoldX.")

    if len(common_keys) == 0:
        print("❌ ERROR: Zero matching keys! Check if run_iteMAN.py naming matches both outputs.")
    # ---------------------

    # 4. Merge the dataframes on the common PDB ID key
    print("융 Merging FoldX and Rosetta scores...")
    df_merged = pd.merge(
        df_foldx,
        df_rosetta,
        on='pdbId_key',
        how='left'
    )

    # Drop the temporary keys
    df_merged.drop(columns=['pdbId_key', 'pdbId_full', 'pdbId_cleaned'], inplace=True, errors='ignore')

    # 5. Select the final columns needed by the JavaScript table (CRITICAL STEP)
    # MUST include 'pepSeq' and 'pdb_relative_path'.
    # pepResidues and disulfide_intact are carried through because the post-run
    # filters below are evaluated from them -- the old list dropped both, so
    # run_catSPEC.py's work was discarded one step later.
    # Per-term Rosetta energies, carried through so they reach the dashboard.
    #
    # non_mc_pdbs.csv already holds every term pepspec emitted, but this step
    # kept only total_score -- so the Results table, the term-correlation
    # heatmap and the colour-by control could never show them. Each term says
    # something the total hides: a design with a good total but a high fa_rep is
    # strained rather than well packed, and fa_dun says whether a side chain
    # sits in a rotamer it would plausibly adopt. Colouring a map by those
    # separates "scores well" from "scores well for a good reason".
    #
    # Selected by intersection with what is actually present, so a job whose
    # score function emitted different terms loses the missing ones rather than
    # the whole merge.
    ROSETTA_TERMS = [
        'interface_score', 'binding_score', 'hbond_sc', 'hbond_bb_sc',
        'fa_rep', 'fa_atr', 'fa_sol', 'fa_dun', 'fa_elec', 'fa_pair',
        'fa_intra_rep', 'pro_close', 'dslf_fa13', 'dslf_ss_dst', 'omega',
        'rama', 'p_aa_pp', 'ref', 'rmsd', 'sasa_score',
    ]
    present_terms = [t for t in ROSETTA_TERMS if t in df_merged.columns]
    dropped_numeric = [c for c in df_merged.columns
                       if c not in present_terms and str(c).isdigit()]
    print(f"🔬 Carrying {len(present_terms)} Rosetta term(s) into the score "
          f"table: {', '.join(present_terms) or 'none found'}")
    if dropped_numeric:
        print(f"   · ignoring {len(dropped_numeric)} numeric artifact column(s)")

    final_cols = (['pdbId', 'pepSeq', 'pepResidues', 'pdb_relative_path',
                   'ddG_Score', 'total_score', 'disulfide_intact']
                  + present_terms)

    cols_to_select = [col for col in final_cols if col in df_merged.columns]
    df_final = df_merged[cols_to_select].copy()

    # Rename columns to match the table headers expected by tab2_results_table.js
    df_final = df_final.rename(columns={'total_score': 'Total_Energy'})

    # Per-row provenance flag: a left join leaves unmatched designs without a Rosetta
    # score, and those cells stay empty rather than being filled with a placeholder.
    if 'Total_Energy' in df_final.columns:
        df_final['energy_available'] = df_final['Total_Energy'].notna()
    else:
        df_final['Total_Energy'] = pd.NA
        df_final['energy_available'] = False

    # 5b. Post-run filters: evaluated and recorded, never silently dropped.
    df_final, report = apply_post_filters(df_final, spec)

    # 6. Save the final merged data
    df_final.to_csv(output_file, index=False)
    print(f"💾 Merged score data saved to **{output_file}** ({len(df_final)} rows)")
    _write_filtered(df_final, report)

    # --- TOP 10 ---
    # Ranked among the designs that PASS the filters, when any do. A "best 10"
    # containing designs the user asked to exclude would be misleading.
    try:
        top10_dir = "top10"
        os.makedirs(top10_dir, exist_ok=True)

        current_dir_name = os.path.basename(os.getcwd())

        pool = df_final
        if report.get('filters_active'):
            passing = df_final[df_final['passes_filters']]
            if len(passing):
                pool = passing
                print(f"📂 Ranking top 10 among the {len(passing)} design(s) "
                      "that pass the filters.")
            else:
                print("⚠️ No design passes the filters; top10 falls back to the "
                      "unfiltered ranking. Treat it accordingly.")

        # Sort by FoldX_Score (lowest is best). Rosetta ranking left commented
        # out as in the original.
        # df_top10 = pool.sort_values(by='Total_Energy').head(10)
        df_top10 = pool.sort_values(by='ddG_Score').head(10)

        print(f"📂 Copying top 10 models to {top10_dir}...")
        for idx, row in df_top10.iterrows():
            src = row['pdb_relative_path']

            # --- FIX START: Strip redundant parent directory ---
            if pd.notna(src):
                # Check if path starts with "JOB_ID/" and remove it
                if src.startswith(f"{current_dir_name}/"):
                    src = src.replace(f"{current_dir_name}/", "", 1)
            # --- FIX END ---

            if pd.notna(src) and os.path.exists(src):
                shutil.copy2(src, os.path.join(top10_dir, os.path.basename(src)))
            elif pd.notna(src):
                # Final fallback: check if the file exists just by its filename
                filename = os.path.basename(src)
                if os.path.exists(filename):
                    shutil.copy2(filename, os.path.join(top10_dir, filename))
                else:
                    print(f"   ⚠️ Source PDB not found for top model: {src}")
            else:
                print("   ⚠️ Top model row has no pdb_relative_path.")

        print(f"✅ Top 10 models ready in {top10_dir}/")
    except Exception as e:
        print(f"⚠️ Failed to create top10 folder: {e}")
    # -------------------------

    return df_final

if __name__ == "__main__":

    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <master_pdb_base>")
        sys.exit(1)

    master_pdb_base = sys.argv[1]

    # --- FIX: Strip the unique ID (e.g., _f7a63da4) to find the original master file ---
    # --- KORREKTUR: Die eindeutige ID entfernen, um die ursprüngliche Master-Datei zu finden ---
    if "_" in master_pdb_base:
        # Splits 'SPCRCK_f7a63da4' at the underscore and takes the first part 'SPCRCK'
        base_only = master_pdb_base.split('_')[0]
    else:
        base_only = master_pdb_base

    FOLDX_INPUT_FILE = f"{base_only}.all.txt"

    # ----------------------------------------------------------------------------------

    result = merge_and_clean_scores(FOLDX_INPUT_FILE, ROSETTA_INPUT_FILE, OUTPUT_CLEANED_CSV)

    # Exiting 0 without producing the score table is what let a broken run look clean.
    if result is None or not os.path.exists(OUTPUT_CLEANED_CSV):
        print(f"🛑 No score table produced ({OUTPUT_CLEANED_CSV} missing).", file=sys.stderr)
        sys.exit(1)
