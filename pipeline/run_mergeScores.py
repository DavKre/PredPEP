#!/home/spacepep/miniforge3/envs/predPEP/bin/python

import pandas as pd
import os
import re
import sys
import shutil

# --- Configuration ---
ROSETTA_INPUT_FILE = "non_mc_pdbs.csv" # Source of Rosetta scores, pepSeq, and paths
OUTPUT_CLEANED_CSV = "tab2_final_scores.csv" # Target file for client-side JavaScript

# Strips the per-model suffix from a FoldX pdbId to get the design base:
# 'SPILRH1Q672F1_7.pdb' / 'SPILRH1Q672F1_7.mod.pdb' -> 'SPILRH1Q672F1'
_MODEL_SUFFIX = re.compile(r'_\d+(\.mod)?\.pdb$', re.IGNORECASE)


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


def _write_foldx_only(df_foldx, output_file):
    """Emit the score table we CAN build when Rosetta aggregation produced nothing.

    Rosetta_Total_Score is left EMPTY -- never 0 and never invented -- and the
    degradation is marked in-band via rosetta_available so a consumer reading only
    the CSV can tell it is partial.
    """
    print("⚠️ Building a FoldX-only score table (Rosetta scores unavailable).")
    sequences, pdb_paths = _scan_job_tree('.')

    df = df_foldx[['pdbId', 'FoldX_Score']].copy()
    df['pepSeq'] = df['pdbId'].map(lambda p: sequences.get(_design_base(p), ''))
    df['pdb_relative_path'] = df['pdbId'].map(lambda p: pdb_paths.get(str(p), ''))
    df['Rosetta_Total_Score'] = pd.NA          # -> empty cell in the CSV
    df['rosetta_available'] = False

    df = df[['pdbId', 'pepSeq', 'pdb_relative_path', 'FoldX_Score',
             'Rosetta_Total_Score', 'rosetta_available']]
    df.to_csv(output_file, index=False)

    missing_seqs = int((df['pepSeq'] == '').sum())
    print(f"💾 Degraded score table saved to **{output_file}** ({len(df)} rows, "
          f"Rosetta scores empty).")
    if missing_seqs:
        print(f"⚠️ {missing_seqs} row(s) have no recoverable peptide sequence.")
    return df

def merge_and_clean_scores(foldx_file, rosetta_file, output_file):
    
    """
    Merges FoldX scores with Rosetta scores, including all necessary
    columns (pepSeq, pdb_relative_path) for the front end.
    """
    
    # 1. Load FoldX Data
    if not os.path.exists(foldx_file):
        print(f"❌ FoldX master file not found: {foldx_file}. Skipping merge.")
        return None
        
    print(f"📂 Loading FoldX scores from {foldx_file}...")
    df_foldx = pd.read_csv(foldx_file, sep='\s+', header=None, names=['pdbId', 'FoldX_Score'], usecols=[0, 1])
    
    # 2. Load Rosetta Data (includes pepSeq and pdb_relative_path)
    # If it is missing the aggregation step failed, but the FoldX ranking above is
    # still valid: emit a degraded table rather than nothing at all (job
    # SPILRH_c70d2d4c produced 4,795 scored designs and no score table).
    if not os.path.exists(rosetta_file):
        print(f"❌ Rosetta scores file not found: {rosetta_file}. Falling back to FoldX-only table.")
        return _write_foldx_only(df_foldx, output_file)


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
    # MUST include 'pepSeq' and 'pdb_relative_path'
    final_cols = ['pdbId', 'pepSeq', 'pdb_relative_path', 'FoldX_Score', 'total_score'] 
    
    cols_to_select = [col for col in final_cols if col in df_merged.columns]
    df_final = df_merged[cols_to_select]
    
    # Rename columns to match the table headers expected by tab2_results_table.js
    df_final = df_final.rename(columns={'total_score': 'Rosetta_Total_Score'})

    # Per-row provenance flag: a left join leaves unmatched designs without a Rosetta
    # score, and those cells stay empty rather than being filled with a placeholder.
    if 'Rosetta_Total_Score' in df_final.columns:
        df_final['rosetta_available'] = df_final['Rosetta_Total_Score'].notna()
    else:
        df_final['Rosetta_Total_Score'] = pd.NA
        df_final['rosetta_available'] = False


    # 6. Save the final merged data
    df_final.to_csv(output_file, index=False)
    print(f"💾 Merged score data saved to **{output_file}**")

    # --- NEW: TOP 10 LOGIC ---
    # Create top10 folder and copy top 10 models based on Rosetta score
    # top10-Ordner erstellen und die besten 10 Modelle basierend auf dem Rosetta-Score kopieren
    try:
        top10_dir = "top10"
        os.makedirs(top10_dir, exist_ok=True)
        
        # 1. Define the variable immediately
        # 1. Variable sofort definieren
        current_dir_name = os.path.basename(os.getcwd())

        # Sort by Rosetta_Total_Score (lowest is best)
        # Nach Rosetta_Total_Score sortieren (niedrigster ist am besten)
        #df_top10 = df_final.sort_values(by='Rosetta_Total_Score').head(10)
        df_top10 = df_final.sort_values(by='FoldX_Score').head(10)
        
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
            else:
                # Final fallback: check if the file exists just by its filename in the current tree
                filename = os.path.basename(src)
                if os.path.exists(filename):
                     shutil.copy2(filename, os.path.join(top10_dir, filename))
                else:
                    print(f" ^z   ^o Source PDB not found for top model: {src}")
                
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
