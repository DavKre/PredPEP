#!/home/spacepep/miniforge3/envs/predPEP/bin/python

import os
import sys
import time
import subprocess
import re
import glob
import uuid 
import shutil 

# Ensure all necessary paths are available for subprocess calls to bash scripts
os.environ['PATH'] = (
    "/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/tools/protein_tools/scripts:"
    "/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/source/bin:"
    "/usr/local/pepspec_pipe:" + os.environ['PATH']
)

def run_bash_script(script_path, *args, log_file=None):
    """Utility to run a bash script and wait for its completion."""
    command = ['/usr/bin/bash', script_path] + list(args)
    
    # We open the log file to capture both stdout and stderr of the bash script
    with open(log_file, 'a') if log_file else open(os.devnull, 'w') as log_out:
        process = subprocess.Popen(command, stdout=log_out, stderr=log_out)
        process.wait()
        
    if process.returncode != 0:
        raise Exception(f"Script failed with code {process.returncode}: {script_path}")

## --- HELPER FUNCTION: Copy and Rename ---
def copy_and_rename_pdb(original_path, destination_dir, iteration, manager_log):
    """
    Copies a PDB file to a destination directory, applying the required naming convention
    for the worker script. Returns the new path and filename.
    """
    os.makedirs(destination_dir, exist_ok=True)
    
    old_filename = os.path.basename(original_path)
    base_name, ext = os.path.splitext(old_filename)
    
    #new_base_name = base_name

    prefix = base_name[:6]

    if iteration == 1:
        # Iteration 1: Remove the first underscore only (e.g., SPCRCKW5_7 -> SPCRCKW57)
        new_base_name = base_name.replace('_', '', 1)
        
    elif iteration >= 2:
        # Iteration >= 2: Remove the first underscore AND the innermost number 
        # (e.g., SPCRCKW52W5_7 -> SPCRCKW52W57)
        
        # Step A: Remove the first underscore
        #temp_name = base_name.replace('_', '', 1)
 
        # 1. User Prefix (first 6 chars) -> 'SPCRCK'
        # prefix = base_name[:6]

        # NEW: Capture the model number (e.g., '9' from 'SPCRCKIWWWW2_9')
        # NEU: Die Modellnummer erfassen
        #model_suffix = ""
        #if '_' in base_name:
        #    model_suffix = base_name.split('_')[-1]
        
        # 2. Get the part before the underscore -> 'SPCRCKI312D15'
        pre_underscore = base_name.split('_')[0]
        model_num = base_name.split('_')[-1] if '_' in base_name else ""

        # Step B: Identify and remove the FIRST sequence of digits in the variable part (after the 6-char prefix)
        #prefix = temp_name[:6]
        #variable_part = temp_name[6:]

        # 3. Get the previous iteration number (the last digit of the base name that started this round)
        # In 'SPCRCKI312D15', the input was 'SPCRCKI312', so '2' is the prev iteration
        # We find it by looking at the input name before the new residue was added
        # However, a simpler way is to grab the last digit of the string used to name the current folder
        # For Iteration 3, the previous iteration is always iteration - 1
        prev_iter_marker = str(iteration - 1)
        
        # 4. Extract the "New Residue" (The letter + numericals immediately before the underscore)
        # We use regex to find a Letter followed by one or more Digits at the end of pre_underscore
        # e.g., 'D15' from 'SPCRCKI312D15'
        #res_match = re.search(r'([A-Za-z][0-9]+)$', pre_underscore)
        #new_residue = res_match.group(1) if res_match else ""

        # Use regex to find and remove the FIRST sequence of digits in the variable part
        # new_variable_part = re.sub(r'[0-9]+', '', variable_part, 1)
        
        # Recombine
        # new_base_name = prefix + new_variable_part

        #if model_suffix:
        #    new_base_name = f"{prefix}{new_variable_part}_{model_suffix}"
        #else:
        #    new_base_name = prefix + new_variable_part
        # 5. Get the model number (after the underscore) -> '1'
        #model_num = base_name.split('_')[-1] if '_' in base_name else ""
        
        # 6. Combine: Prefix + PrevIter + NewRes + Model + CurrentIter
        # SPCRCK + 2 + D15 + 1 + 3 -> SPCRCK2D1513
        #new_base_name = f"{prefix}{prev_iter_marker}{new_residue}{model_num}{iteration}"

        # Use [0-9]+ to handle any number of trailing digits
        # [0-9]+ verwenden, um beliebig viele folgende Ziffern zu verarbeiten
        res_match = re.search(r'([A-Za-z][0-9]+)$', pre_underscore)
        
        if res_match:
            new_residue = res_match.group(1)
            raw_lineage = pre_underscore[:res_match.start()]
            
            
            raw_var_part = raw_lineage[6:]
            if raw_var_part:
                # This regex finds a Letter followed by digits. 
                # It removes all digits EXCEPT the very last one in that sequence.
                # Dieser Regex findet einen Buchstaben gefolgt von Ziffern.
                # Er entfernt alle Ziffern AUSSER der allerletzten in dieser Sequenz.
                cleaned_lineage = re.sub(r'([A-Za-z])[0-9]+(?=[0-9])', r'\1', raw_var_part)
            else:
                cleaned_lineage = ""

            # Iteration 1 needs to manually add the '1' because it's not in the parent name yet
            # Iteration 1 muss die '1' manuell hinzufügen, da sie noch nicht im Parent-Namen steht
            if iteration == 2: # (When we are preparing the input for Iteration 2)
                prev_iter_marker = "1"
                new_base_name = f"{prefix}{prev_iter_marker}{new_residue}{model_num}{iteration}"
            else:
                # For Iteration 3+, the lineage already contains the numbers we need
                # Für Iteration 3+ enthält die Lineage bereits die benötigten Zahlen
                new_base_name = f"{prefix}{cleaned_lineage}{new_residue}{model_num}{iteration}"

    else:
        new_base_name = base_name

    # Construct the final new path
    new_filename = new_base_name + ext
    new_path = os.path.join(destination_dir, new_filename)
    if os.path.abspath(original_path) == os.path.abspath(new_path):
        log_message = f"      -> PDB already at destination: {new_filename}"
        print(log_message, file=sys.stderr)
        return new_path, new_filename
    
    try:
        shutil.copy(original_path, new_path)
        log_message = f"      -> Copied and renamed PDB (Iter {iteration}): {old_filename} -> {new_filename} at {new_path}"
        print(log_message, file=sys.stderr)
        with open(manager_log, 'a') as f:
            f.write(log_message + "\n")
        return new_path, new_filename # Return both path and new filename
    except Exception as e:
        print(f"      -> ERROR during PDB copy/rename: {e}", file=sys.stderr)
        return original_path, old_filename
## --- END HELPER FUNCTION ---


## --- MODIFIED FUNCTION: parse_and_select_best ---
def parse_and_select_best(master_pdb_base, master_dir, iteration, manager_log, previously_selected_pdbs):
    """
    1. Runs run_catFiles.sh to aggregate FoldX results.
    2. Parses the master .all.txt file and selects the top 3 novel PDBs.
    """
    
    run_catfiles_script = 'run_catFiles.sh' 

    # NEW: Strip the ID to match the bash script output
    # NEU: ID kürzen, um dem Output des Bash-Skripts zu entsprechen
    stripped_id = master_pdb_base.split('_')[0]

    # 1. Run the aggregation script
    print(f"  -> Aggregating results for iteration {iteration} using ID: {master_pdb_base}...")
    run_bash_script(run_catfiles_script, master_pdb_base, master_dir, log_file=manager_log) 
    
    #master_file = os.path.join(master_dir, f"{master_pdb_base}.all.txt")
    # Use stripped_id for the filename
    # Verwende stripped_id für den Dateinamen
    master_file = os.path.join(master_dir, f"{stripped_id}.all.txt")
    
    if not os.path.exists(master_file):
        print(f"WARNING: Master file not found at {master_file}. Stopping.")
        return [], 999.0 

    selected_pdbs = []
    current_best_score = 999.0
    
    # 2. Parse all results
    print(f"  -> Selecting top 3 PDBs from {master_file}...")
    
    all_results = []
    with open(master_file, 'r') as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 2:
                try:
                    pdb_filename = parts[0]
                    score = float(parts[1])
                    all_results.append({'pdb_filename': pdb_filename, 'score': score})
                except ValueError:
                    continue

    all_results.sort(key=lambda x: x['score'])
    
    # --- FILTERING STEP ---
    filtered_results = []
    for result in all_results:
        # Check if the filename from the score file (.all.txt) is already in the used set
        if result['pdb_filename'] not in previously_selected_pdbs:
            filtered_results.append(result)
            
    if all_results:
        current_best_score = all_results[0]['score']
        print(f"    -> Current Global Best Score found: {current_best_score:.2f}")

    if not filtered_results:
        print("    -> WARNING: All models have been previously selected or pool is empty. Stopping selection.")
        return [], current_best_score
    
    # Select the top 3 from the filtered list
    print(f"    -> Selecting top 3 *novel* PDBs from {len(filtered_results)} candidates.")
    for result in filtered_results[:3]:
        pdb_filename = result['pdb_filename']
        score = result['score']
        
        # --- ENHANCEMENT: Construct the PDB subdirectory name from the PDB filename ---
        # PDB Filename example: SPCRCKLLLFF1_5.pdb
        # The subdirectory name should be: SPCRCKLLLFF1.pdbs
        base_name_no_ext = os.path.splitext(pdb_filename)[0] # e.g., SPCRCKLLLFF1_5
        
        # Use regex to find and strip the last underscore and model number suffix (e.g., _5)
        match = re.search(r'(_[0-9]+)$', base_name_no_ext)
        if match:
            # Strip the model number part to get the directory name base
            pdb_dir_base = base_name_no_ext[:match.start()] # e.g., SPCRCKLLLFF1
            pdb_subdir_name = f"{pdb_dir_base}.pdbs"        # e.g., SPCRCKLLLFF1.pdbs
        else:
            # Fallback for filenames without a model number (unlikely in Rosetta decoys)
            pdb_subdir_name = f"{base_name_no_ext}.pdbs"
        # -----------------------------------------------------------------------------
        
        # Use robust glob search for PDB location (the file still exists under its original name)
        # Search Pattern: {master_dir}/{master_pdb_base}_iter*/{SPCRCKLLLFF1.pdbs}/{SPCRCKLLLFF1_5.pdb}
        search_pattern = os.path.join(master_dir, f"{master_pdb_base}_iter*", pdb_subdir_name, pdb_filename)
        found_paths = glob.glob(search_pattern)

        if found_paths:
            original_pdb_path = found_paths[0]
            
            # Store the original filename and path for the next worker to process
            print(f"    -> Selected NEW model: {pdb_filename} (Score: {score:.2f})")
            selected_pdbs.append({
                'pdb_filename': pdb_filename,      # Original filename from .all.txt (Used for filtering next time)
                'original_path': original_pdb_path,    # Original path (Used for copying/renaming next time)
                'score': score
            })
        else:
            print(f"    -> WARNING: Selected PDB not found for filename {pdb_filename}. Searching pattern: {search_pattern}")

    return selected_pdbs, current_best_score 
## --- END MODIFIED FUNCTION ---


## --- MODIFIED FUNCTION: iterative_pipeline_manager (main loop) ---
def iterative_pipeline_manager(input_pdb_path, master_result_dir, cpus, job_id, base_result_dir_unused, master_pdb_base):
    """
    The main iterative loop controller, fixed to run the initial input only once.
    """
    
    master_id = master_pdb_base 
    manager_log = os.path.join(master_result_dir, 'iterative_manager.log')
    rosetta_script = 'run_pepSpecPipe.sh' 
    
    TEMP_INPUT_DIR = os.path.join(master_result_dir, 'input_staging', job_id) 
    
    print(f"--- Iterative Pipeline Manager Started: {job_id} ---")
    
    global_best_score = 999.0 
    CONVERGENCE_THRESHOLD = 0.01 
    
    consecutive_no_improvement = 0 
    previously_selected_pdbs = set() 
    
    # ------------------------------------------------------------------
    # --- PHASE 1: EXECUTE ITERATION 1 (INITIAL INPUT) ONCE ---
    # ------------------------------------------------------------------
    initial_pdb_filename = os.path.basename(input_pdb_path)
    
    # Copy the initial PDB to the staging area 
    temp_input_path = os.path.join(TEMP_INPUT_DIR, initial_pdb_filename)
    os.makedirs(TEMP_INPUT_DIR, exist_ok=True)
    shutil.copy(input_pdb_path, temp_input_path) 
    
    print("\n*** Starting Iteration 1 (Initial Input) ***")
    
    # 1. Copy and rename the PDB for Iteration 1
    source_pdb_path = temp_input_path
    renamed_input_path, renamed_filename = copy_and_rename_pdb(
        source_pdb_path, 
        TEMP_INPUT_DIR, 
        1,  # Force iteration 1 renaming
        manager_log
    )
    
    # 2. Set up the worker run for Iteration 1
    original_pdb_file = renamed_input_path
    run_job_id = f"{master_pdb_base}_iter1_{str(uuid.uuid4())[:4]}"
    run_out_path = os.path.join(master_result_dir, run_job_id) 

    os.makedirs(run_out_path, exist_ok=True)
    
    input_pdb_dir = os.path.join(run_out_path, 'inputPDB')
    os.makedirs(input_pdb_dir, exist_ok=True)
    
    pdb_base = os.path.splitext(os.path.basename(original_pdb_file))[0] 
    mod_pdb_filename = f"{pdb_base}.mod.pdb"
    
    temp_pdb_path = os.path.join(input_pdb_dir, mod_pdb_filename)
    shutil.copy(original_pdb_file, temp_pdb_path)
    
    # 3. Run the Rosetta Pipeline for Iteration 1
    run_bash_script(
        rosetta_script, 
        original_pdb_file, 
        run_out_path, 
        cpus, 
        log_file=manager_log
    )
    print(f"  -> Iteration 1 run completed: {run_job_id}.")
    
    # 4. Aggregate results and select the next inputs for Iteration 2
    # The PDB ID in .all.txt uses the full filename format (e.g., SPCRCKW5_7.pdb).
    previously_selected_pdbs.add(initial_pdb_filename) 

    selected, iteration_best_score = parse_and_select_best(
        master_id, master_result_dir, 1, manager_log, previously_selected_pdbs
    )
    
    # 5. Initialize tracking variables for the loop
    global_best_score = iteration_best_score
    current_inputs = selected
    
    # ------------------------------------------------------------------
    # --- PHASE 2: ITERATIVE LOOP (STARTS FROM ITERATION 2) ---
    # ------------------------------------------------------------------
    
    MAX_ITERATIONS = 6 
    
    try:
        # Loop starts from iteration 2 up to MAX_ITERATIONS
        for iteration in range(2, MAX_ITERATIONS + 1): 
            print(f"\n*** Starting Iteration {iteration} ***")
            
            if not current_inputs:
                print("No new PDBs selected for next iteration (check filtering logic). Stopping.")
                break
                
            # 1. RUN WORKERS
            for input_data in current_inputs:
                
                source_pdb_path = input_data['original_path'] 
                
                # Copy and rename the PDB for this specific run's input (PDBPATH)
                renamed_input_path, renamed_filename = copy_and_rename_pdb(
                    source_pdb_path, 
                    TEMP_INPUT_DIR, 
                    iteration, 
                    manager_log
                )
                
                original_pdb_file = renamed_input_path # The PDBPATH argument for run_pepSpecPipe.sh
                
                # ... (Worker execution setup) ...
                run_job_id = f"{master_pdb_base}_iter{iteration}_{str(uuid.uuid4())[:4]}"
                run_out_path = os.path.join(master_result_dir, run_job_id) 

                os.makedirs(run_out_path, exist_ok=True)
                
                input_pdb_dir = os.path.join(run_out_path, 'inputPDB')
                os.makedirs(input_pdb_dir, exist_ok=True)
                
                pdb_base = os.path.splitext(os.path.basename(original_pdb_file))[0] 
                mod_pdb_filename = f"{pdb_base}.mod.pdb"
                
                # Copy the (already renamed) input PDB from the staging area into the worker's inputPDB folder
                temp_pdb_path = os.path.join(input_pdb_dir, mod_pdb_filename)
                shutil.copy(original_pdb_file, temp_pdb_path)
                
                # Run the Rosetta Pipeline
                run_bash_script(
                    rosetta_script, 
                    original_pdb_file, 
                    run_out_path, 
                    cpus, 
                    log_file=manager_log
                )

                print(f"  -> Run completed for {input_data['pdb_filename']}.")
            
            # 2. Aggregate results and select the next inputs
            selected, iteration_best_score = parse_and_select_best(
                master_id, master_result_dir, iteration, manager_log, previously_selected_pdbs
            )
            
            # --- CONVERGENCE CHECK AND TRACKING ---
            improvement = global_best_score - iteration_best_score
            
            if iteration_best_score < global_best_score:
                global_best_score = iteration_best_score
                consecutive_no_improvement = 0 
                print(f"Global Best Score improved to: {global_best_score:.2f}")
            elif improvement < CONVERGENCE_THRESHOLD:
                consecutive_no_improvement += 1
                print(f"Score did not significantly improve (< {CONVERGENCE_THRESHOLD:.2f}). Consecutive non-improvement count: {consecutive_no_improvement}/3")
            
            # --- CUSTOM EXIT CONDITION: 3 consecutive rounds without improvement ---
            if consecutive_no_improvement >= 3:
                print(f"\n*** CONVERGENCE REACHED ***")
                print(f"No significant score improvement for {consecutive_no_improvement} consecutive iterations. Stopping.")
                break 
            
            # 3. Prepare for the next iteration
            for pdb_data in selected:
                previously_selected_pdbs.add(pdb_data['pdb_filename'])
                
            current_inputs = selected 

        # ------------------------------------------------------------------
        # --- PHASE 3: FINAL SCORE AGGREGATION AND MERGING ---
        # ------------------------------------------------------------------
        print("\n*** Iterations complete. Preparing final results and zipping. ***")
        
        # Define script paths
        cat_spec_script = '/usr/local/bin/run_catSPEC.py' 
        merge_script = '/usr/local/bin/run_mergeScores.py' 

        original_cwd = os.getcwd()
        os.chdir(master_result_dir)

        # 1. Run Rosetta Score Aggregation (run_catSPEC.py)
        print("\n*** Running Rosetta Score Aggregation (run_catSPEC.py) ***")
        try:
            subprocess.run([cat_spec_script], check=True, stdout=sys.stderr, stderr=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"FATAL ERROR running run_catSPEC.py: {e}", file=sys.stderr)
            
        # 2. Run Score Merge (run_mergeScores.py)
        print("\n*** Running Score Merge (run_mergeScores.py) ***")
        try:
            subprocess.run([merge_script, master_pdb_base], check=True, stdout=sys.stderr, stderr=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"FATAL ERROR running run_mergeScores.py: {e}", file=sys.stderr)
            
        #os.chdir(original_cwd)

        # --- ZIPPING FINAL OUTPUTS ---
        sys.stdout.flush()  # <--- ADD THIS LINE
        sys.stderr.flush()  # <--- AND THIS ONE 
        
        # --- ZIPPING FINAL OUTPUTS ---
        final_zip_name = f"{job_id}.zip"  
        final_zip_path = os.path.join(master_result_dir, final_zip_name)
        
        #master_foldx_file = os.path.join(master_result_dir, f"{master_id}.all.txt")
        #final_scores_file = os.path.join(master_result_dir, "tab2_final_scores.csv")
        
        #files_to_zip = [master_foldx_file, final_scores_file]
        
        #zip_command = ['/usr/bin/zip', '-j', final_zip_path] + [f for f in files_to_zip if os.path.exists(f)]

        zip_command = [
            '/usr/bin/zip', '-r', 
            final_zip_path, 
            '.', 
            '-x', f'*{final_zip_name}*',   # Exclude the zip being created / Das entstehende Zip ausschliessen
            '-x', 'input_staging/*'         # Exclude staging if it still exists / Staging ausschliessen
        ]

        try:
            subprocess.run(zip_command, check=True, stdout=sys.stderr, stderr=sys.stderr)
            print(f"Final recursive zip created at {final_zip_path}")
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to create zip: {e}")

        os.chdir(original_cwd)

        #if len(zip_command) > 3: 
        #    subprocess.run(zip_command, check=True)
        #    print(f"Final zip created at {final_zip_path}")
        #else:
        #     print("WARNING: No master score files found for zipping.")
        
        # Clean up the staging directory
        if os.path.exists(TEMP_INPUT_DIR):
            shutil.rmtree(TEMP_INPUT_DIR)
            print(f"Cleaned up temporary staging directory: {TEMP_INPUT_DIR}")

    except Exception as e:
        print(f"FATAL ERROR in iterative manager: {e}")
        with open(manager_log, 'a') as f:
            f.write(f"FATAL ERROR: {e}\n")
        sys.exit(1)
## --- END MODIFIED FUNCTION ---

if __name__ == '__main__':
    if len(sys.argv) != 7:
        print(f"Usage: {sys.argv[0]} <input_pdb_path> <master_result_dir> <cpus> <job_id> <base_result_dir_unused> <master_pdb_base>")
        sys.exit(1)
        
    input_pdb_path = sys.argv[1]
    master_result_dir = sys.argv[2]
    cpus = sys.argv[3]
    job_id = sys.argv[4]
    base_result_dir_unused = sys.argv[5] 
    master_pdb_base = sys.argv[6]
    
    # Ensure the script directory is added to sys.path for finding utility scripts if necessary
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.append(script_dir)
        
    iterative_pipeline_manager(input_pdb_path, master_result_dir, cpus, job_id, base_result_dir_unused, master_pdb_base)
