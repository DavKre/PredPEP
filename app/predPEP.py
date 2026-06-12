#!/home/spacepep/miniforge3/envs/predPEP/bin/python

import os
import shutil
import subprocess
import uuid
import re
import glob
import json
import signal
from datetime import datetime, timezone
import pandas as pd
from flask import Flask, request, render_template, send_from_directory, jsonify
from werkzeug.utils import secure_filename

# Import TMAP logic from the local utility
try:
    from tmap_utils import generate_tmap_coordinates
except ImportError:
    print("Warning: tmap_utils.py not found. TMAP Tree functionality will be limited.")
    # FIX: Return 5 values (x, y, s, t, valid_indices) to match the new signature
    # FIX: 5 Werte zurückgeben, um der neuen Signatur zu entsprechen
    def generate_tmap_coordinates(seqs): return [], [], [], [], []

predPEP = Flask(__name__)

# Base directories for temporary files
BASE_UPLOAD_FOLDER = '/tmp/pepspec/uploads'
BASE_RESULT_FOLDER = '/tmp/pepspec/results'

# Path to the asynchronous manager script
MANAGER_SCRIPT_PATH = '/usr/local/bin/run_iteMAN.py'
# Path to the Python executable in the environment
PYTHON_EXECUTABLE = '/home/spacepep/miniforge3/envs/predPEP/bin/python'

# ----------------------------------------------------------------------
# ## 🐍 Helper Functions
# ----------------------------------------------------------------------

def generate_base_name(protein_symbol, user_name):
    """
    Generates the unique PDB base name: SP + Protein Symbol (first 3) + User Name (first letter)
    """
    prefix = "SP"
    if protein_symbol:
        letters = re.sub(r'[^a-zA-Z]', '', protein_symbol)
        prot_part = letters[:3].upper() if letters else "XXX"
    else:
        prot_part = "XXX"

    user_part = user_name[0].upper() if user_name else "Z"
    return f"{prefix}{prot_part}{user_part}"

def get_master_id(job_id):
    """
    Derives the master aggregation ID from the full job ID (e.g., SPTXKK_uuid -> SPTXKK).
    """
    #pdb_base = job_id.split('_')[0]
    #return pdb_base
    return job_id

def count_peptide_residues(pdb_path):
    """Count unique chain-B residues (the peptide) by their Cα atoms. Returns int or None."""
    seen = set()
    try:
        with open(pdb_path) as f:
            for line in f:
                if line.startswith(("ATOM", "HETATM")) and line[21:22] == "B" and line[12:16].strip() == "CA":
                    seen.add(line[22:27])  # resSeq + iCode (fixed columns)
    except Exception:
        return None
    return len(seen) or None

def _kill_job(jdir):
    """SIGTERM the job's manager process group (manager + its bash/Rosetta children). Best-effort."""
    try:
        with open(os.path.join(jdir, 'manager.pid')) as f:
            pid = int(f.read().strip())
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
        return False

# ----------------------------------------------------------------------
# ## 🌐 Flask Routes
# ----------------------------------------------------------------------

@predPEP.route('/')
def index():
    """Renders the main page with the file upload form."""
    return render_template('index.html')

@predPEP.route('/health')
def health():
    """Liveness probe for DDN / the Docker healthcheck (JSON, no UI)."""
    return jsonify({"service": "predpep-node", "status": "ok"})

@predPEP.route('/upload', methods=['POST'])
def upload_file():
    """Handles file upload and job submission (asynchronously)."""
    protein_symbol = request.form.get('protein_symbol')
    user_name = request.form.get('user_name')

    if not all([protein_symbol, user_name]):
        return jsonify({'success': False, 'error': 'Protein Symbol and User Name are required.'}), 400

    if 'file1' not in request.files:
        return jsonify({'success': False, 'error': 'No file part'}), 400

    file = request.files['file1']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No selected file'}), 400

    try:
        cpus = max(2, min(32, int(request.form.get('cpus', '8'))))
    except (TypeError, ValueError):
        cpus = 8
    cpus = str(cpus)  # downstream Popen expects a string in the argv list
    predPEP.logger.info(f"[submit] clamped cpus={cpus} (raw={request.form.get('cpus')!r})")

    # 1. GENERATE NEW PDB BASE NAME AND JOB ID
    new_pdb_base = generate_base_name(protein_symbol, user_name)
    job_uuid = str(uuid.uuid4())
    job_folder_name = f"{new_pdb_base}_{job_uuid[:8]}"
    new_filename = f"{new_pdb_base}.pdb"

    # MODIFIED: Use job_folder_name for the master result directory to ensure isolation
    # GEÄNDERT: job_folder_name für das Master-Ergebnisverzeichnis verwenden, um Isolierung zu gewährleisten
    master_result_folder = os.path.join(BASE_RESULT_FOLDER, job_folder_name)
    upload_folder = os.path.join(BASE_UPLOAD_FOLDER, job_folder_name)
    
    # 2. CREATE DIRECTORIES
    try:
        os.makedirs(upload_folder, exist_ok=True)
        os.makedirs(master_result_folder, exist_ok=True)
    except OSError as e:
        return jsonify({'success': False, 'error': f'Failed to create directories: {e}'})

    # 3. SAVE UPLOADED FILE
    filepath = os.path.join(upload_folder, new_filename)
    try:
        file.save(filepath)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Failed to save file: {e}'})

    # Persist submission metadata for the Jobs list (survives on the volume)
    try:
        with open(os.path.join(master_result_folder, 'job.json'), 'w') as jf:
            json.dump({
                'job_id': job_folder_name,
                'submitted_at': datetime.now(timezone.utc).isoformat(),
                'protein_symbol': protein_symbol,
                'user_name': user_name,
                'cpus': int(cpus),
                'pdb_filename': new_filename,
                'peptide_length': count_peptide_residues(filepath),
            }, jf)
    except Exception as e:
        predPEP.logger.warning(f"[submit] could not write job.json: {e}")

    # 4. LAUNCH ASYNCHRONOUS ITERATIVE MANAGER
    try:
        if not os.path.exists(MANAGER_SCRIPT_PATH) or not os.access(MANAGER_SCRIPT_PATH, os.X_OK):
            return jsonify({'success': False, 'error': 'Manager script not found/executable.'})

        # MODIFIED: Last argument passed as job_folder_name instead of new_pdb_base
        # GEÄNDERT: Letztes Argument als job_folder_name anstelle von new_pdb_base übergeben
        manager_command = [
            PYTHON_EXECUTABLE, MANAGER_SCRIPT_PATH,
            filepath, master_result_folder, cpus, 
            job_folder_name, master_result_folder, job_folder_name
        ]

        proc = subprocess.Popen(
            manager_command, close_fds=True, start_new_session=True,
            stdout=open(os.path.join(master_result_folder, f'{job_folder_name}_manager_stdout.log'), 'w'),
            stderr=open(os.path.join(master_result_folder, f'{job_folder_name}_manager_stderr.log'), 'w')
        )
        try:
            with open(os.path.join(master_result_folder, 'manager.pid'), 'w') as pf:
                pf.write(str(proc.pid))
        except Exception as e:
            predPEP.logger.warning(f"[submit] could not write manager.pid: {e}")

        return jsonify({
            'success': True,
            'message': f'Job submitted for {new_pdb_base} (ID: {job_folder_name}).',
            'job_id': job_folder_name
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@predPEP.route('/status/<job_id>', methods=['GET'])
def check_status(job_id):
    """Checks for the existence of the final zipped result."""
    master_pdb_base = get_master_id(job_id)
    master_result_dir = os.path.join(BASE_RESULT_FOLDER, master_pdb_base)
    zip_filename = f"{job_id}.zip"
    output_zip_path = os.path.join(master_result_dir, zip_filename)

    if os.path.exists(output_zip_path):
        return jsonify({
            'status': 'Complete',
            'download_url': f'/download/{master_pdb_base}/{zip_filename}'
        })
    else:
        if os.path.exists(master_result_dir):
            return jsonify({'status': 'Processing', 'message': 'Job is running iterations...'})
        return jsonify({'status': 'Pending/Failed', 'message': 'Job failed to start.'})

@predPEP.route('/results_data/<job_id>', methods=['GET'])
def get_results_data(job_id):
    """Serves FoldX and Rosetta data for visualization."""
    try:
        master_id = get_master_id(job_id)
        base_result_dir = os.path.join(BASE_RESULT_FOLDER, master_id)

        # The FoldX aggregate is named by the PDB BASE (e.g. "SPILRH"), i.e. the
        # job id with the "_<uuid8>" suffix stripped — NOT by the full job id.
        # Resolve it robustly: prefer the base-named file, fall back to the
        # job-id-named one, then glob any *.all.txt in the result dir.
        pdb_base = master_id.split('_')[0]
        foldx_candidates = [
            os.path.join(base_result_dir, f"{pdb_base}.all.txt"),
            os.path.join(base_result_dir, f"{master_id}.all.txt"),
        ]
        foldx_path = next((p for p in foldx_candidates if os.path.exists(p)), None)
        if foldx_path is None:
            globbed = glob.glob(os.path.join(base_result_dir, "*.all.txt"))
            foldx_path = globbed[0] if globbed else None

        rosetta_csv_path = os.path.join(base_result_dir, "tab2_final_scores.csv")

        foldx_data = ""
        if foldx_path and os.path.exists(foldx_path):
            with open(foldx_path, 'r') as f: foldx_data = f.read()
        rosetta_data = ""
        if os.path.exists(rosetta_csv_path):
            with open(rosetta_csv_path, 'r') as f: rosetta_data = f.read()

        # The peptide score table (CSV) is the primary artifact — serve results
        # whenever EITHER file is present; only 404 when neither exists yet.
        if not foldx_data and not rosetta_data:
            return jsonify({'success': False, 'error': "Results not found yet."}), 404

        return jsonify({
            'success': True, 'job_id': job_id, 'master_id': master_id,
            'foldx_txt_content': foldx_data, 'rosetta_csv_content': rosetta_data
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@predPEP.route('/stream_final_pdb/<job_id>/<path:relative_path>', methods=['GET'])
def stream_final_pdb(job_id, relative_path):
    """Securely streams PDB files based on the CSV paths."""
    master_pdb_base = get_master_id(job_id)
    if '..' in relative_path or not relative_path.startswith(master_pdb_base):
        return jsonify({'success': False, 'error': "Insecure path."}), 403

    base_dir_to_send = os.path.join(BASE_RESULT_FOLDER, master_pdb_base)
    inner_relative_path = relative_path.removeprefix(master_pdb_base + os.sep)
    
    return send_from_directory(
        base_dir_to_send, inner_relative_path,
        as_attachment=False, mimetype='chemical/x-pdb'
    )

# ----------------------------------------------------------------------
# ## 🌳 TMAP TREE ROUTE (MODIFIED FOR STABILITY)
# ----------------------------------------------------------------------

@predPEP.route('/get_tmap_tree/<job_id>', methods=['GET'])
def get_tmap_tree(job_id):
    """
    Generates TMAP layout coordinates for the sequence similarity tree.
    Filters metadata based on valid_indices to prevent frontend crashes.
    """
    try:
        master_id = get_master_id(job_id)
        csv_path = os.path.join(BASE_RESULT_FOLDER, master_id, "tab2_final_scores.csv")
        
        if not os.path.exists(csv_path):
            return jsonify({'success': False, 'error': "Scores CSV not found yet."}), 404

        df = pd.read_csv(csv_path)
        
        if 'pepSeq' not in df.columns:
            return jsonify({'success': False, 'error': "CSV missing 'pepSeq' column."}), 500
            
        # Ensure sequences are clean strings and uppercase for RDKit
        sequences = df['pepSeq'].fillna('').astype(str).str.upper().tolist()
        
        # Call the updated TMAP utility with 5 return values
        x, y, s, t, valid_indices = generate_tmap_coordinates(sequences)
        
        if not valid_indices:
             return jsonify({'success': False, 'error': "No valid peptide sequences found for T-MAP."}), 500

        # Filter the original dataframe so that the metadata array matches the x/y coordinate arrays
        filtered_metadata = df.iloc[valid_indices].to_dict(orient='records')
        
        return jsonify({
            'success': True,
            'x': x, 
            'y': y, 
            's': s, 
            't': t,
            'metadata': filtered_metadata
        })
    except Exception as e:
        print(f"TMAP Tree Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@predPEP.route('/jobs', methods=['GET'])
def list_jobs():
    """List all jobs (newest first) with derived status — no auth, all jobs visible."""
    jobs = []
    try:
        for entry in os.listdir(BASE_RESULT_FOLDER):
            jdir = os.path.join(BASE_RESULT_FOLDER, entry)
            if not os.path.isdir(jdir):
                continue
            meta = {}
            meta_path = os.path.join(jdir, 'job.json')
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                except Exception:
                    meta = {}
            meta.setdefault('job_id', entry)
            if os.path.exists(os.path.join(jdir, f"{entry}.zip")):
                meta['status'] = 'Complete'
                meta['download_url'] = f"/download/{entry}/{entry}.zip"
            elif os.path.exists(os.path.join(jdir, 'STOPPED')):
                meta['status'] = 'Stopped'
                meta['download_url'] = None
            else:
                meta['status'] = 'Processing'
                meta['download_url'] = None
            jobs.append(meta)
        jobs.sort(key=lambda j: j.get('submitted_at', ''), reverse=True)
        return jsonify({'success': True, 'jobs': jobs})
    except FileNotFoundError:
        return jsonify({'success': True, 'jobs': []})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@predPEP.route('/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    """Delete a job's result + upload dirs (reclaims disk). No auth."""
    if '/' in job_id or '..' in job_id or job_id in ('', '.', '..'):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    _kill_job(os.path.join(BASE_RESULT_FOLDER, job_id))
    removed = []
    for base in (BASE_RESULT_FOLDER, BASE_UPLOAD_FOLDER):
        d = os.path.join(base, job_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    if not removed:
        return jsonify({'success': False, 'error': 'Job not found.'}), 404
    return jsonify({'success': True, 'deleted': job_id})


@predPEP.route('/jobs/<job_id>/stop', methods=['POST'])
def stop_job(job_id):
    """Stop a running job: kill its process group + mark it Stopped. No auth."""
    if '/' in job_id or '..' in job_id or job_id in ('', '.', '..'):
        return jsonify({'success': False, 'error': 'Invalid job id.'}), 400
    jdir = os.path.join(BASE_RESULT_FOLDER, job_id)
    if not os.path.isdir(jdir):
        return jsonify({'success': False, 'error': 'Job not found.'}), 404
    killed = _kill_job(jdir)
    try:
        open(os.path.join(jdir, 'STOPPED'), 'w').close()
    except Exception:
        pass
    return jsonify({'success': True, 'stopped': job_id, 'killed': killed})


@predPEP.route('/download/<master_dir_name>/<filename>')
def download_file(master_dir_name, filename):
    return send_from_directory(os.path.join(BASE_RESULT_FOLDER, master_dir_name), filename, as_attachment=True)

#if __name__ == '__main__':
#    predPEP.run(host='0.0.0.0', port=8000)
