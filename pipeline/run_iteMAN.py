#!/home/spacepep/miniforge3/envs/predPEP/bin/python

import os
import sys
import time
import json
import subprocess
import re
import glob
import uuid
import shutil

# ---------------------------------------------------------------- tuning
# These were hardcoded here AND in scheduler.py, with a comment in each
# telling the next person to keep them in step by hand. They are now read
# from the environment so the systemd unit is the single place they are set,
# and the Jobs table's "N / MAX" denominator cannot drift from what the
# manager actually does.
#
#   PREDPEP_MAX_ITERATIONS   how many rounds a job may run (default 6)
#   PREDPEP_PLATEAU_ROUNDS   consecutive non-improving rounds that stop a job
#   PREDPEP_CONVERGENCE_MIN  the smallest score gain that counts as progress
#
# PREDPEP_CONVERGENCE_MIN IS NOT YET CALIBRATED. The default below is the
# value this file has always carried. It is almost certainly too small: a
# threshold under the run-to-run noise of the metric means noise reads as
# progress, the plateau counter never fills, and every job runs to the cap.
# Set it from the observed round-to-round deltas of a job that started from
# an UNOPTIMISED peptide -- see lineage.json, "rounds[].best_score_this_round".

def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_ITERATIONS = _env_int('PREDPEP_MAX_ITERATIONS', 6)
# The non-canonical design pass. See the block in PHASE 3 for why it is a
# separate pass at the end rather than a flag on pepspec.
NCAA_TOP = _env_int('PREDPEP_NCAA_TOP', 10)
NCAA_TIMEOUT = _env_int('PREDPEP_NCAA_TIMEOUT', 3600)
# Empty means "whatever run_ncaaDesign.py defaults to", so this adds a
# lever without changing behaviour until it is set. Not an int, hence
# os.environ rather than _env_int.
NCAA_WEIGHTS = os.environ.get('PREDPEP_NCAA_WEIGHTS', '').strip()
# On by default. Without the control the only comparison in the table is
# delta_dG_vs_parent, which includes repacking the whole peptide and so
# cannot say what the non-canonical itself contributed. Set 0 to skip it.
NCAA_CONTROL = os.environ.get('PREDPEP_NCAA_CONTROL', '1').strip() != '0'
PLATEAU_ROUNDS = _env_int('PREDPEP_PLATEAU_ROUNDS', 3)
CONVERGENCE_MIN = _env_float('PREDPEP_CONVERGENCE_MIN', 0.01)

# Ensure all necessary paths are available for subprocess calls to bash scripts
os.environ['PATH'] = (
    "/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/tools/protein_tools/scripts:"
    "/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/source/bin:"
    "/usr/local/pepspec_pipe:" + os.environ['PATH']
)

FINAL_SCORES_FILE = 'tab2_final_scores.csv'
FINAL_STATUS_FILE = 'final_status.json'
AGGREGATION_REPORT_FILE = 'aggregation_report.json'


def _assess_final_outcome(master_result_dir, master_pdb_base, failed_stage, stage_errors):
    """Decide the job's terminal state from what phase 3 actually produced.

    Returns (status, failed_stage, message) where status is one of:
      complete — the final score table exists and nothing was lost;
      partial  — usable per-design results exist, but the score table is missing or
                 incomplete (this is what SPILRH_c70d2d4c really was);
      failed   — nothing usable came out of the final stages.
    """
    have_final_csv = os.path.exists(os.path.join(master_result_dir, FINAL_SCORES_FILE))
    base_only = master_pdb_base.split('_')[0]
    have_foldx = bool(glob.glob(os.path.join(master_result_dir, f"{base_only}.all.txt"))
                      or glob.glob(os.path.join(master_result_dir, "*.all.txt")))

    # Damage recorded by run_catSPEC.py: files/lines dropped means the score table is
    # incomplete even when every stage exited 0.
    degraded, damage = False, ''
    try:
        with open(os.path.join(master_result_dir, AGGREGATION_REPORT_FILE)) as f:
            report = json.load(f)
        if report.get('degraded'):
            degraded = True
            damage = (f"{report.get('spec_files_unreadable', 0)} unreadable .spec file(s), "
                      f"{report.get('corrupt_lines_skipped', 0)} corrupted line(s) skipped")
    except (OSError, ValueError):
        pass

    if not have_final_csv:
        message = '; '.join(stage_errors) or f'{FINAL_SCORES_FILE} was not produced'
        if have_foldx:
            return 'partial', failed_stage or 'score_aggregation', \
                f'{FINAL_SCORES_FILE} missing: {message}'
        return 'failed', failed_stage or 'score_aggregation', message

    if failed_stage or degraded:
        detail = '; '.join(stage_errors) or damage
        return 'partial', failed_stage or 'spec_file_corruption', \
            f'{FINAL_SCORES_FILE} is incomplete: {detail}'

    return 'complete', None, 'All final stages completed.'


def _write_final_status(master_result_dir, job_id, status, failed_stage, message):
    """The manager's verdict, read by the scheduler to finalise job.json."""
    payload = {'job_id': job_id, 'status': status, 'failed_stage': failed_stage,
               'message': message, 'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%S+00:00',
                                                                time.gmtime())}
    try:
        with open(os.path.join(master_result_dir, FINAL_STATUS_FILE), 'w') as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        print(f"WARNING: could not write {FINAL_STATUS_FILE}: {e}", file=sys.stderr)


def _stamp_job_json(master_result_dir, status, failed_stage, message):
    """Record the terminal state in job.json as soon as the manager knows it, instead
    of leaving "running" behind for the scheduler to correct (or not)."""
    path = os.path.join(master_result_dir, 'job.json')
    try:
        with open(path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    meta['status'] = status
    meta.setdefault('completed_at', time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()))
    if failed_stage:
        meta['failed_stage'] = failed_stage
    if message:
        meta['message'] = message
    try:
        with open(path, 'w') as f:
            json.dump(meta, f)
    except OSError as e:
        print(f"WARNING: could not stamp job.json: {e}", file=sys.stderr)


LINEAGE_FILE = 'lineage.json'


def _segment_of(pdb_filename):
    """Decompose a selected model's filename into what actually produced it.

    'SPCGRK1C2W3M4M2145M21_6.pdb' -> residue 'M', anchor 21, model 6

    Read from the IMMEDIATE name only -- the trailing '<letter><anchor>' before
    the underscore, plus the model number after it. Both are exact here.

    This is NOT recovered by parsing the accumulated lineage string, which is
    lossy: the trim in copy_and_rename_pdb keeps only the LAST digit of each
    earlier segment's number run, and that digit is the ITERATION number, not
    the anchor. So 'M2145' (anchor 21, model 4, iteration 5) becomes 'M5', which
    then reads as "Met 5" -- a position that was never an anchor. It only looks
    correct while anchor == iteration, which is why early rounds appeared fine.
    The naming is left exactly as it was, because downstream scripts and your
    existing result directories depend on it; the truth is recorded here instead.
    """
    base = os.path.splitext(os.path.basename(str(pdb_filename)))[0]
    pre = base.split('_')[0]
    model = None
    if '_' in base:
        tail = base.split('_')[-1]
        if tail.isdigit():
            model = int(tail)
    m = re.search(r'([A-Za-z])([0-9]+)$', pre)
    if m:
        return {'residue': m.group(1), 'anchor_resseq': int(m.group(2)),
                'model': model}
    return {'residue': None, 'anchor_resseq': None, 'model': model}


def _write_lineage(master_result_dir, job_id, lineage):
    """The provenance chain, as data rather than encoded in a filename.

    Written after every round, not just at the end, so a job that crashes or is
    still running has a usable record. Mirrored into job.json so the dashboard
    can offer "branch from this variant" without reading a second file.
    """
    payload = {'job_id': job_id, 'rounds': lineage,
               'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())}
    try:
        with open(os.path.join(master_result_dir, LINEAGE_FILE), 'w') as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        print(f"WARNING: could not write {LINEAGE_FILE}: {e}", file=sys.stderr)

    path = os.path.join(master_result_dir, 'job.json')
    try:
        with open(path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return                      # job.json is the scheduler's; never create it here
    meta['lineage'] = lineage
    try:
        with open(path, 'w') as f:
            json.dump(meta, f)
    except OSError as e:
        print(f"WARNING: could not record lineage in job.json: {e}", file=sys.stderr)


def _lineage_label(lineage):
    """Human-readable chain built FROM the record, e.g. 'C2#6 -> W3#8 -> M21#4'."""
    parts = []
    for r in lineage:
        # 'selected' is a LIST (a round branches into up to 3); the first entry
        # is the best-scoring pick, which is the main line of descent.
        picks = r.get('selected') or []
        sel = picks[0] if picks else {}
        if sel.get('residue') and sel.get('anchor_resseq') is not None:
            tag = f"{sel['residue']}{sel['anchor_resseq']}"
            if sel.get('model') is not None:
                tag += f"#{sel['model']}"
            parts.append(tag)
    return ' -> '.join(parts)


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

    # Iteration 1's picks must be marked used BEFORE iteration 2 selects, or they
    # remain candidates and -- being the current global best -- get chosen again,
    # so iteration 3 re-runs templates iteration 2 already ran. Every later round
    # does this correctly at step 3 of the loop; only round 1 was missing it.
    for pdb_data in selected:
        previously_selected_pdbs.add(pdb_data['pdb_filename'])

    lineage = [{
        'round': 1,
        'parent': None,
        'template_pdb_base': pdb_base,
        'iteration_dir': run_job_id,
        'best_score_this_round': (None if iteration_best_score >= 999.0
                                  else iteration_best_score),
        'selected': [dict(_segment_of(d['pdb_filename']),
                          pdb=d['pdb_filename'], foldx=d['score'])
                     for d in selected],
    }]
    # 'selected' at a round is the list the NEXT round branches from, best first.
    _write_lineage(master_result_dir, job_id, lineage)
    
    # ------------------------------------------------------------------
    # --- PHASE 2: ITERATIVE LOOP (STARTS FROM ITERATION 2) ---
    # ------------------------------------------------------------------
    
    try:
        # Loop starts from iteration 2 up to MAX_ITERATIONS
        for iteration in range(2, MAX_ITERATIONS + 1): 
            print(f"\n*** Starting Iteration {iteration} ***")
            
            if not current_inputs:
                print("No new PDBs selected for next iteration (check filtering logic). Stopping.")
                break
                
            # 1. RUN WORKERS
            round_runs = []
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

                # Which parent produced this run, and the name it was given.
                # Recorded per run because a round branches into up to 3.
                round_runs.append({
                    'parent_pdb': input_data['pdb_filename'],
                    'parent': dict(_segment_of(input_data['pdb_filename']),
                                   foldx=input_data.get('score')),
                    'template_pdb_base': pdb_base,
                    'iteration_dir': run_job_id,
                })

            # 2. Aggregate results and select the next inputs
            selected, iteration_best_score = parse_and_select_best(
                master_id, master_result_dir, iteration, manager_log, previously_selected_pdbs
            )

            lineage.append({
                'round': iteration,
                'runs': round_runs,
                'best_score_this_round': (None if iteration_best_score >= 999.0
                                          else iteration_best_score),
                'selected': [dict(_segment_of(d['pdb_filename']),
                                  pdb=d['pdb_filename'], foldx=d['score'])
                             for d in selected],
            })
            _write_lineage(master_result_dir, job_id, lineage)
            label = _lineage_label(lineage)
            if label:
                print(f"  -> Lineage so far: {label}")
            
            # --- CONVERGENCE CHECK AND TRACKING ---
            #
            # Lower is better, so a positive `improvement` means this round
            # beat the running best.
            #
            # THE BUG THIS REPLACES: the old test was
            #
            #     if iteration_best_score < global_best_score:   -> reset
            #     elif improvement < CONVERGENCE_THRESHOLD:      -> count
            #
            # The first branch caught ANY improvement, however small, so a
            # gain of 0.0001 reset the counter. The elif was therefore only
            # reachable when improvement <= 0, where `improvement < 0.01` is
            # always true -- so the threshold was never once consulted, and
            # the rule was really "three rounds with NO improvement at all"
            # rather than the intended "no SIGNIFICANT improvement". A job
            # creeping along in noise would run to the cap.
            #
            # Each round is judged against the running best, so three small
            # steps in a row count as a plateau even though they sum to
            # something. That is the intended reading of "no significant
            # improvement three rounds running"; if you ever want cumulative
            # gain to break a plateau instead, compare against the best as it
            # stood when the counter last reset, not against the running best.
            improvement = global_best_score - iteration_best_score

            if improvement >= CONVERGENCE_MIN:
                global_best_score = iteration_best_score
                consecutive_no_improvement = 0
                print(f"Global Best Score improved to: {global_best_score:.2f} "
                      f"(gain {improvement:.3f} >= {CONVERGENCE_MIN:.3f})")
            else:
                # Not progress, but still track the best score actually seen,
                # or the number this job reports would be stale.
                if improvement > 0:
                    global_best_score = iteration_best_score
                consecutive_no_improvement += 1
                print(f"No significant improvement (gain {improvement:+.3f} < "
                      f"{CONVERGENCE_MIN:.3f}). Plateau count: "
                      f"{consecutive_no_improvement}/{PLATEAU_ROUNDS}")

            # --- EXIT CONDITION: PLATEAU_ROUNDS consecutive non-improving rounds ---
            if consecutive_no_improvement >= PLATEAU_ROUNDS:
                print(f"\n*** CONVERGENCE REACHED ***")
                print(f"No significant score improvement for {consecutive_no_improvement} "
                      f"consecutive iterations (threshold {CONVERGENCE_MIN:.3f}). Stopping.")
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

        # A failing final stage must NOT report as a successful job. Each failure is
        # recorded and folded into a terminal verdict below (job SPILRH_c70d2d4c ran
        # 14 h, crashed here, and still reported "Complete").
        failed_stage = None
        stage_errors = []

        # 1. Run Rosetta Score Aggregation (run_catSPEC.py)
        print("\n*** Running Rosetta Score Aggregation (run_catSPEC.py) ***")
        try:
            subprocess.run([cat_spec_script], check=True, stdout=sys.stderr, stderr=sys.stderr)
        except subprocess.CalledProcessError as e:
            failed_stage = 'score_aggregation'
            stage_errors.append(f"run_catSPEC.py failed: {e}")
            print(f"FATAL ERROR running run_catSPEC.py: {e}", file=sys.stderr)

        # 2. Run Score Merge (run_mergeScores.py)
        print("\n*** Running Score Merge (run_mergeScores.py) ***")
        try:
            subprocess.run([merge_script, master_pdb_base], check=True, stdout=sys.stderr, stderr=sys.stderr)
        except subprocess.CalledProcessError as e:
            failed_stage = failed_stage or 'score_merge'
            stage_errors.append(f"run_mergeScores.py failed: {e}")
            print(f"FATAL ERROR running run_mergeScores.py: {e}", file=sys.stderr)

        # 2b. NON-CANONICAL DESIGN PASS (run_ncaaDesign.py)
        #
        # This is where selected non-canonical and D residues actually take
        # effect. pepspec cannot place them: its design loop hardcodes
        # chemical::AA(1..20) (pepspec.cc:1293), and the packer-palette flag
        # crashes it. So the pass runs here, on finished models, through
        # rosetta_scripts -- and here rather than inside the loop, because
        # run_iteMAN feeds each round's best models back into pepspec and a
        # model carrying an NCAA would hit that crash on the next round.
        #
        # A failure in this pass must NOT change the job's verdict. The
        # canonical designs and their scores are already complete and valid,
        # and this pass writes its own table without touching what the
        # aggregation above produced. Hence an error list of its own that never
        # reaches failed_stage or stage_errors.
        ncaa_errors = []
        # Resolved rather than assumed: the other stages live in /usr/local/bin
        # but run_ncaaDesign.py is installed in /usr/local/pepspec_pipe, so a
        # single hardcoded path silently never runs.
        ncaa_script = next(
            (c for c in ('/usr/local/bin/run_ncaaDesign.py',
                         '/usr/local/pepspec_pipe/run_ncaaDesign.py')
             if os.path.exists(c)), None)
        ncaa_codes = []
        try:
            with open(os.path.join(master_result_dir, 'design_spec.json')) as f:
                ncaa_codes = ((json.load(f).get('palette') or {})
                              .get('noncanonical') or [])
        except (OSError, ValueError) as e:
            ncaa_errors.append(f'could not read design_spec.json: {e}')

        ncaa_off = os.environ.get('PREDPEP_NCAA_PASS', '1').strip().lower() \
            in ('0', 'no', 'false', 'off')

        if not ncaa_codes:
            print('\n*** No non-canonical residues selected; '
                  'skipping the design pass. ***')
        elif ncaa_off:
            # Said loudly: the user selected residues and is not getting them.
            print(f'\n*** NCAA design pass DISABLED (PREDPEP_NCAA_PASS); '
                  f'{len(ncaa_codes)} selected residue(s) were NOT '
                  f'incorporated. ***', file=sys.stderr)
            ncaa_errors.append('disabled by PREDPEP_NCAA_PASS')
        elif not ncaa_script:
            ncaa_errors.append('run_ncaaDesign.py not found in /usr/local/bin '
                               'or /usr/local/pepspec_pipe')
            print(f'WARNING: run_ncaaDesign.py not found; {len(ncaa_codes)} '
                  f'selected residue(s) were NOT incorporated.',
                  file=sys.stderr)
        else:
            shown = ', '.join(str(c) for c in ncaa_codes[:8])
            if len(ncaa_codes) > 8:
                shown += f', ... (+{len(ncaa_codes) - 8} more)'
            print(f'\n*** Running NCAA design pass on {len(ncaa_codes)} '
                  f'residue(s) [{shown}], top {NCAA_TOP} models ***')
            try:
                ncaa_cmd = [ncaa_script, master_result_dir,
                            '--top', str(NCAA_TOP)]
                # The score function is a lever, not a constant: a
                # non-canonical's reference energy comes from the
                # `unfolded` term, which only some weights files
                # enable. Logged on every run, because a ddG means
                # nothing without knowing which function produced it --
                # the same reason run_foldX2.sh logs its binary.
                if NCAA_WEIGHTS:
                    ncaa_cmd += ['--weights', NCAA_WEIGHTS]
                # The control is the only comparison that isolates what
                # the non-canonical contributed: same backbone, same
                # protocol, same nloop, palette withheld. Measured
                # 17 Nov 2026 at 73s a structure, it roughly doubles the
                # pass -- about 25 min at top 10, well inside the
                # budget -- which is why it is on rather than optional.
                if NCAA_CONTROL:
                    ncaa_cmd.append('--canonical-control')
                print('NCAA weights: %s, control: %s'
                      % (NCAA_WEIGHTS or '(script default)',
                         'on' if NCAA_CONTROL else 'off'),
                      file=sys.stderr)
                subprocess.run(ncaa_cmd,
                               check=True, timeout=NCAA_TIMEOUT,
                               stdout=sys.stderr, stderr=sys.stderr)
                print('*** NCAA design pass complete. ***')
            except subprocess.TimeoutExpired:
                ncaa_errors.append(
                    f'run_ncaaDesign.py exceeded {NCAA_TIMEOUT}s and was '
                    'abandoned')
                print(f'WARNING: NCAA design pass exceeded {NCAA_TIMEOUT}s; '
                      'abandoned. The canonical results are unaffected.',
                      file=sys.stderr)
            except subprocess.CalledProcessError as e:
                ncaa_errors.append(f'run_ncaaDesign.py failed: {e}')
                print(f'WARNING: NCAA design pass failed: {e}. The canonical '
                      'results are unaffected.', file=sys.stderr)

        if ncaa_errors:
            # Recorded beside the job so a consumer can see that the selection
            # did not take effect, without reading the manager log.
            try:
                with open(os.path.join(master_result_dir,
                                       'ncaa_status.json'), 'w') as f:
                    json.dump({'requested': list(ncaa_codes),
                               'incorporated': False,
                               'errors': ncaa_errors}, f, indent=2)
            except OSError:
                pass

        # 3. Verdict, based on the deliverable itself rather than on exit codes alone —
        # the aggregation has silent-success paths that exit 0 without producing it.
        final_status, failed_stage, status_message = _assess_final_outcome(
            master_result_dir, master_pdb_base, failed_stage, stage_errors)
        print(f"\n*** Final outcome: {final_status} "
              f"({failed_stage or 'no failed stage'}) — {status_message} ***")

        # Written before zipping so it travels inside the result zip: a consumer can
        # then diagnose the run offline instead of parsing manager_stderr.log.
        _write_final_status(master_result_dir, job_id, final_status,
                            failed_stage, status_message)

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
            final_status = 'failed' if final_status != 'complete' else 'partial'
            failed_stage = failed_stage or 'zip'
            status_message = f'Failed to create result zip: {e}'
            _write_final_status(master_result_dir, job_id, final_status,
                                failed_stage, status_message)

        # Stamp job.json only now that the zip is on disk, so its recorded state
        # matches what a consumer can actually retrieve.
        _stamp_job_json(master_result_dir, final_status, failed_stage, status_message)

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

        # Honest exit code: a degraded or failed run must not look like a clean one to
        # anything that inspects it (the scheduler reads final_status.json, but a human
        # or a future caller reads $?).
        if final_status == 'failed':
            sys.exit(1)
        if final_status == 'partial':
            sys.exit(2)

    except Exception as e:
        print(f"FATAL ERROR in iterative manager: {e}")
        with open(manager_log, 'a') as f:
            f.write(f"FATAL ERROR: {e}\n")
        _write_final_status(master_result_dir, job_id, 'failed', 'manager',
                            f'Iterative manager crashed: {e}')
        _stamp_job_json(master_result_dir, 'failed', 'manager',
                        f'Iterative manager crashed: {e}')
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
