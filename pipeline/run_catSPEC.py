#!/home/spacepep/miniforge3/envs/predPEP/bin/python

import pandas as pd
import glob
import re
import os

# --- Define the BASE RESULT FOLDER ---
# This is the directory containing all project folders (e.g., SPCRCK)
BASE_RESULT_FOLDER = '/tmp/pepspec/results'

def process_peptide_spec_files(project_dir="."):
    """
    Finds all .spec files, cleans and structures the data, and saves three output CSVs.
    It now correctly reconstructs the full PDB path, including the iteration folder.
    """
    PROCESSED_SCORES_FILE = "processed_peptide_scores.csv"
    ALL_SCORES_FILE = "all_model_scores.csv"
    NON_MC_PDB_FILE = "non_mc_pdbs.csv"

    print(f"🔍 Searching for .spec files in '{os.path.abspath(project_dir)}'...")
    spec_files = glob.glob(os.path.join(project_dir, '**', '*.spec'), recursive=True)

    if not spec_files:
        print("❌ No .spec files found. Exiting.")
        return

    print(f"✅ Found {len(spec_files)} .spec files. Starting processing...")
    
    all_data = []
    # Pattern to extract key-value scores (e.g., total_score: -435.778)
    score_pattern = re.compile(r'(\w+-\w+|\w+):?\s*?([+-]?\s*\d+\.\d+|\d+)')

    for file_path in spec_files:
        
        # Determine the iteration folder from the .spec file's location
        # Example: /tmp/pepspec/results/SPCRCK/SPCRCK_iter6_2f0e/score.spec
        spec_dir = os.path.dirname(file_path) # e.g., /tmp/pepspec/results/SPCRCK/SPCRCK_iter6_2f0e
        
        # Get the iteration name (e.g., SPCRCK_iter6_2f0e)
        iteration_folder = os.path.basename(spec_dir) 
        
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                parts = line.split()

                if len(parts) < 3:
                    print(f"⚠️ Skipping malformed line in {file_path}: {line}")
                    continue

                full_pdb_path = parts[0]
                pep_seq = parts[1]
                
                # Extract the desired 'pepID' which is the filename WITH the '.pdb' extension.
                pep_id = os.path.basename(full_pdb_path)
                
                # 1. Calculate the relative path from BASE_RESULT_FOLDER
                try:
                    relative_path_from_spec = os.path.relpath(full_pdb_path, BASE_RESULT_FOLDER)
                except ValueError:
                    # Fallback for paths that might span different drives/file systems
                    relative_path_from_spec = full_pdb_path.replace(BASE_RESULT_FOLDER, '', 1).lstrip(os.sep)
                
                # The path fragment is now: 'SPCRCK/SPCRCKLIL5.pdbs/SPCRCKLIL5_10.pdb'
                
                # --- START FIXING PATH ORDER (Robust Slicing) ---
                
                # 1. Find the index where the project name ends (first '/')
                try:
                    first_sep_index = relative_path_from_spec.index(os.sep)
                    
                    # path_prefix_with_sep: e.g., 'SPCRCK/' (Includes the first separator)
                    path_prefix_with_sep = relative_path_from_spec[:first_sep_index + 1] 
                    
                    # path_suffix: e.g., 'SPCRCKLIL5.pdbs/SPCRCKLIL5_10.pdb' (The rest of the path)
                    path_suffix = relative_path_from_spec[first_sep_index + 1:]          
                except ValueError:
                    # Handle case where there is no separator (malformed path fragment)
                    print(f"⚠️ Warning: Malformed path fragment, cannot insert iteration folder: {relative_path_from_spec}")
                    relative_path = relative_path_from_spec
                else:
                    # 2. Join the pieces in the correct order: Prefix + Iteration + Suffix
                    # Result: SPCRCK/SPCRCK_iter3_45ce/SPCRCKLIL5.pdbs/SPCRCKLIL5_10.pdb
                    
                    # Note: We use os.path.join here for safety, it will handle the trailing separator 
                    # from path_prefix_with_sep correctly.
                    relative_path = os.path.join(path_prefix_with_sep, iteration_folder, path_suffix)
                    # os.path.join is smart enough to handle a path starting with 'SPCRCK/' and
                    # an argument like 'SPCRCK_iter3_45ce' to produce 'SPCRCK/SPCRCK_iter3_45ce'
                
                # --- END FIXING PATH ORDER ---

                row_data = {
                    'pepID': pep_id,
                    'pepSeq': pep_seq,
                    'pdb_relative_path': relative_path, 
                }

                score_string = ' '.join(parts[2:])
                
                matches = score_pattern.findall(score_string)
                
                for key, value_str in matches:
                    cleaned_value = value_str.strip().replace(' ', '')
                    try:
                        row_data[key] = float(cleaned_value)
                    except ValueError:
                        row_data[key] = cleaned_value

                all_data.append(row_data)

    # 2. Convert the list of dictionaries to a pandas DataFrame
    df_all = pd.DataFrame(all_data)
    original_rows = len(df_all)
    
    # Check for critical column before sorting/filtering
    if 'total_score' not in df_all.columns:
        print("🛑 Error: 'total_score' column not found in data. Cannot proceed with sorting/filtering.")
        df_all.to_csv(ALL_SCORES_FILE, index=False)
        return

    # --- NEW STEP 3: Remove unwanted columns ---
    UNWANTED_COLS = ['43', '42', '40', '41', '29']
    
    cols_to_drop = [col for col in df_all.columns if col in UNWANTED_COLS]
    
    if cols_to_drop:
        df_all.drop(columns=cols_to_drop, inplace=True)
        print(f"🧹 Removed unwanted artifact columns: {cols_to_drop}")
    else:
        print("✔️ No artifact columns (43, 42, 40, 41, 29) found to remove.")
    # ---------------------------------------------

    # 4. Define consistent column order (pepID and pepSeq first)
    cols_ordered = ['pepID', 'pepSeq', 'pdb_relative_path'] + [col for col in df_all.columns if col not in ['pepID', 'pepSeq', 'pdb_relative_path']]

    # 5. Save ALL scores before filtering (for completeness/debugging)
    df_all[cols_ordered].to_csv(ALL_SCORES_FILE, index=False)
    print(f"💾 All model scores saved to **{ALL_SCORES_FILE}** ({len(df_all)} rows).")

    # 6. Filter for the BEST model per unique peptide (for merged scores)
    print("📈 Identifying best-scoring model (lowest total_score) for each unique peptide...")
    
    df_all_sorted = df_all.sort_values(by='total_score', ascending=True)
    
    df_unique = df_all_sorted.drop_duplicates(subset=['pepSeq'], keep='first').copy()
    rows_removed = original_rows - len(df_unique)

    # 7. Save the BEST scores (for merging with FoldX)
    df_unique[cols_ordered].to_csv(PROCESSED_SCORES_FILE, index=False)
    print(f"💾 Best scores saved to **{PROCESSED_SCORES_FILE}** ({len(df_unique)} rows).")

    # 8. Create the NON-MC PDB list (with all scores)
    print("📋 Identifying base PDBs (non-_mc, non-_full, non-_soft) with all scores...")
    
    mc_pattern = r'(_full|_soft|_mc\d+)\.pdb$'
    
    df_non_mc = df_all[~df_all['pepID'].str.contains(mc_pattern, regex=True, case=False)].copy()
    
    df_non_mc.drop_duplicates(subset=['pepID'], keep='first', inplace=True)
    
    df_non_mc[cols_ordered].to_csv(NON_MC_PDB_FILE, index=False)
    
    print(f"💾 Base PDB IDs and all scores saved to **{NON_MC_PDB_FILE}** ({len(df_non_mc)} rows).")

    print("\n--- Results Summary ---")
    print(f"Total rows processed: **{original_rows}**")
    print(f"Rows kept in {PROCESSED_SCORES_FILE} (best-scoring model): **{len(df_unique)}**")
    print(f"Base PDBs kept in {NON_MC_PDB_FILE}: **{len(df_non_mc)}**")
    print(f"Rows removed (suboptimal duplicates): **{rows_removed}**")
    print("-------------------------")

if __name__ == "__main__":
    process_peptide_spec_files()
