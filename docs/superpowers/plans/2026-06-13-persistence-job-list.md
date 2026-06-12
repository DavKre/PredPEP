# Persistent Storage + Job List & Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist jobs on a Docker volume and add a "Jobs" tab (list, status, download, delete-with-files) backed by `GET /jobs` + `DELETE /jobs/<id>`, so past jobs survive page exit and container recreate, and disk can be reclaimed.

**Architecture:** Mount a named volume `predpep_data` at `/tmp/pepspec` (data root unchanged → no pipeline edits). `upload_file()` writes a `job.json` per job; two new Flask routes list/delete jobs by scanning the results dir; a new `tab7_jobs.js` + a Jobs tab render the table. Verify with a throwaway container + throwaway volume (incl. a recreate-persistence check), then cut `predpep_app` over with the volume.

**Tech Stack:** Flask/gunicorn (Python stdlib `json`, `datetime`, `shutil`), vanilla JS (fetch), Docker named volumes.

---

## Constraints
- Data root stays `/tmp/pepspec` (predPEP.py + verbatim `run_catSPEC.py` reference it).
- `pipeline/` scripts unchanged. `predpep_app` cutover authorized; keep `predpep:prejobs` rollback.
- Smoke uses a SEPARATE container `predpep_smoke2` (port 6365) + throwaway volume so it never
  touches the live `predpep_app` or the in-progress timing job on `predpep_smoke` (6364).

## File map
| File | Change |
|---|---|
| `app/predPEP.py` | imports (`json`,`datetime`); `count_peptide_residues()`; write `job.json` in `upload_file`; `GET /jobs`; `DELETE /jobs/<id>` |
| `app/static/tab7_jobs.js` | **new** — `loadJobs`/`deleteJob`/`start|stopJobsPolling` on `window` |
| `app/templates/index.html` | Jobs tab button + `tab7-view` table + `tab7_jobs.js` script + small table CSS |
| `app/static/index.js` | add tab7 to `tabButtons`/`tabViews`; start/stop jobs polling in `switchTab` |
| `scripts/run.sh`, `scripts/run-dev.sh`, `docker-compose.yml` | mount `predpep_data:/tmp/pepspec` |
| `README.md` | Jobs in Web-UI section + Known-limitations caveats |

---

### Task 0: Branch + rollback tag

- [ ] **Step 1: Branch + tag current image**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git checkout -b feature/job-list
docker tag predpep:local predpep:prejobs
docker image inspect predpep:prejobs --format 'rollback size: {{.Size}}'
git status -sb | head -1
```
Expected: `## feature/job-list`; rollback size printed.

---

### Task 1: Backend — metadata, `/jobs`, `DELETE /jobs/<id>`

**Files:** Modify `app/predPEP.py`

- [ ] **Step 1: Add stdlib imports**

After the existing `import glob` line, add `import json` and `from datetime import datetime, timezone`. Change:
```python
import glob
from flask import Flask, request, render_template, send_from_directory, jsonify
```
to:
```python
import glob
import json
from datetime import datetime, timezone
from flask import Flask, request, render_template, send_from_directory, jsonify
```

- [ ] **Step 2: Add the peptide-length helper**

Immediately after the `get_master_id` function definition (before the `## 🌐 Flask Routes` banner), add:
```python
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
```

- [ ] **Step 3: Write `job.json` at submission**

In `upload_file()`, locate the file-save block:
```python
    filepath = os.path.join(upload_folder, new_filename)
    try:
        file.save(filepath)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Failed to save file: {e}'})
```
and insert, immediately after it:
```python
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
```
(`cpus` is already the clamped string from earlier in the function; `int(cpus)` stores it as a number.)

- [ ] **Step 4: Add `GET /jobs` and `DELETE /jobs/<id>` routes**

Immediately before the `download_file` route (the `@predPEP.route('/download/...')` block), add:
```python
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
    removed = []
    for base in (BASE_RESULT_FOLDER, BASE_UPLOAD_FOLDER):
        d = os.path.join(base, job_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    if not removed:
        return jsonify({'success': False, 'error': 'Job not found.'}), 404
    return jsonify({'success': True, 'deleted': job_id})
```

- [ ] **Step 5: Verify syntax + routes**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
python3 -c "import ast; ast.parse(open('app/predPEP.py').read()); print('OK')"
grep -nE "@predPEP.route\('/jobs" app/predPEP.py
```
Expected: `OK`; both `/jobs` and `/jobs/<job_id>` routes shown.

- [ ] **Step 6: Commit**
```bash
git add app/predPEP.py
git commit -q -m "feat(api): job.json metadata + GET /jobs and DELETE /jobs/<id>"
```

---

### Task 2: Frontend — Jobs tab

**Files:** Create `app/static/tab7_jobs.js`; modify `app/templates/index.html`, `app/static/index.js`

- [ ] **Step 1: Create `app/static/tab7_jobs.js`**
```javascript
// static/tab7_jobs.js — Jobs list tab (no module deps; attaches to window)
window.loadJobs = async function () {
    const tbody = document.getElementById('jobsTableBody');
    if (!tbody) return;
    try {
        const res = await fetch('/jobs');
        const data = await res.json();
        if (!data.success) { tbody.innerHTML = `<tr><td colspan="8">Error: ${data.error || 'failed'}</td></tr>`; return; }
        if (!data.jobs.length) { tbody.innerHTML = `<tr><td colspan="8">No jobs yet.</td></tr>`; return; }
        tbody.innerHTML = data.jobs.map(j => {
            const date = j.submitted_at ? new Date(j.submitted_at).toLocaleString() : '—';
            const dl = j.download_url ? `<a href="${j.download_url}">Download</a>` : '—';
            const cls = j.status === 'Complete' ? 'status-complete' : 'status-processing';
            const esc = s => String(s ?? '—').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
            return `<tr>
                <td>${date}</td><td>${esc(j.protein_symbol)}</td><td>${esc(j.user_name)}</td>
                <td>${esc(j.cpus)}</td><td>${esc(j.peptide_length)}</td>
                <td class="${cls}">${esc(j.status)}</td><td>${dl}</td>
                <td><button class="job-delete" data-id="${encodeURIComponent(j.job_id)}">Delete</button></td>
            </tr>`;
        }).join('');
        tbody.querySelectorAll('.job-delete').forEach(b =>
            b.addEventListener('click', () => window.deleteJob(decodeURIComponent(b.dataset.id))));
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="8">Error loading jobs: ${e}</td></tr>`;
    }
};

window.deleteJob = async function (jobId) {
    if (!confirm(`Delete job ${jobId} and its files? This cannot be undone.`)) return;
    try {
        const res = await fetch(`/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
        const data = await res.json();
        if (!data.success) alert(`Delete failed: ${data.error || 'unknown'}`);
    } catch (e) { alert(`Delete error: ${e}`); }
    window.loadJobs();
};

window.startJobsPolling = function () {
    window.loadJobs();
    if (window.jobsInterval) clearInterval(window.jobsInterval);
    window.jobsInterval = setInterval(window.loadJobs, 10000);
};
window.stopJobsPolling = function () {
    if (window.jobsInterval) { clearInterval(window.jobsInterval); window.jobsInterval = null; }
};
```

- [ ] **Step 2: `index.html` — add the Jobs tab button**

Find the tab button row containing `<button id="tab6Button" disabled>6. Tree-Map (MST)</button>` and add a 7th button right after it:
```html
            <button id="tab7Button">7. Jobs</button>
```
(no `disabled` — the Jobs tab is always available).

- [ ] **Step 3: `index.html` — add the Jobs view**

After the `<div id="tab6-view" ...> ... </div>` block (the last tab view), add:
```html
        <div id="tab7-view" class="tab-content hidden">
            <h2>Jobs</h2>
            <p style="font-size:0.9em;color:#555;">All jobs on this machine — no login, everyone sees all jobs. <b>Delete</b> permanently removes a job's files from disk. (A job left "Processing" after a restart is orphaned — proper run-state tracking comes later.)</p>
            <table id="jobsTable" class="jobs-table">
                <thead><tr>
                    <th>Date</th><th>Protein</th><th>User</th><th>CPUs</th><th>Pep. length</th><th>Status</th><th>Download</th><th>Delete</th>
                </tr></thead>
                <tbody id="jobsTableBody"><tr><td colspan="8">Loading…</td></tr></tbody>
            </table>
        </div>
```

- [ ] **Step 4: `index.html` — load the script + minimal CSS**

Find the line loading `index.js` (a `<script ... src="{{ url_for('static', filename='index.js') }}"></script>`) and add ABOVE it (a plain, non-module script):
```html
    <script src="{{ url_for('static', filename='tab7_jobs.js') }}"></script>
```
Then inside the existing `<style>` block (near `.tab-buttons`), add:
```css
        .jobs-table { width: 100%; border-collapse: collapse; font-size: 0.9em; }
        .jobs-table th, .jobs-table td { border: 1px solid #ddd; padding: 6px 8px; text-align: left; }
        .jobs-table th { background: #f3f3f3; }
        .status-complete { color: #137333; font-weight: bold; }
        .status-processing { color: #b06000; }
        .job-delete { background: #c5221f; color: #fff; border: none; padding: 3px 8px; cursor: pointer; border-radius: 3px; }
```

- [ ] **Step 5: `index.js` — register tab7 in the arrays**

Add the 7th button/view to the two arrays. Change:
```javascript
        document.getElementById('tab6Button')
    ];
```
to:
```javascript
        document.getElementById('tab6Button'),
        document.getElementById('tab7Button')
    ];
```
and change:
```javascript
        document.getElementById('tab6-view')
    ];
```
to:
```javascript
        document.getElementById('tab6-view'),
        document.getElementById('tab7-view')
    ];
```

- [ ] **Step 6: `index.js` — start/stop jobs polling in `switchTab`**

At the very start of the `window.switchTab = (targetIndex) => {` body (before `tabViews.forEach`), add:
```javascript
        if (window.stopJobsPolling) window.stopJobsPolling();
```
and inside the `if (index === targetIndex) {` block, after the existing `if (index > 1) { window.handlePlotRendering(index); }`, add:
```javascript
                if (index === 6 && window.startJobsPolling) window.startJobsPolling();
```

- [ ] **Step 7: Syntax check + commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
node --check app/static/tab7_jobs.js && echo "tab7_jobs.js OK" || echo "(node not present — skip)"
grep -nE "tab7Button|tab7-view|startJobsPolling" app/static/index.js app/templates/index.html | head
git add app/static/tab7_jobs.js app/templates/index.html app/static/index.js
git commit -q -m "feat(ui): Jobs tab — list/download/delete with 10s polling"
```
Expected: tab7 references present in both files.

---

### Task 3: Mount the persistent volume

**Files:** Modify `scripts/run.sh`, `scripts/run-dev.sh`, `docker-compose.yml`

- [ ] **Step 1: `scripts/run.sh`**

In the `docker run -d \` block, add a volume line. Change:
```bash
  --name "${CONTAINER}" \
  -p 6363:6363 \
```
to:
```bash
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
  -p 6363:6363 \
```

- [ ] **Step 2: `scripts/run-dev.sh`**

In its `docker run -d \` block, change:
```bash
  --name "${CONTAINER}" \
  -p "${HOST_PORT}:6363" \
```
to:
```bash
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
  -p "${HOST_PORT}:6363" \
```

- [ ] **Step 3: `docker-compose.yml`**

Change:
```yaml
    ports:
      - "6363:6363"
    restart: unless-stopped
```
to:
```yaml
    ports:
      - "6363:6363"
    volumes:
      - predpep_data:/tmp/pepspec
    restart: unless-stopped
```
and append at the end of the file (top-level):
```yaml

volumes:
  predpep_data:
```

- [ ] **Step 4: Commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
grep -rn "predpep_data" scripts/ docker-compose.yml
git add scripts/run.sh scripts/run-dev.sh docker-compose.yml
git commit -q -m "deploy: mount persistent named volume predpep_data at /tmp/pepspec"
```
Expected: `predpep_data` referenced in all three.

---

### Task 4: Build + smoke (volume, metadata, list, persistence, delete)

**Files:** none (build + verify) — uses `predpep_smoke2` (6365) + throwaway volume `predpep_data_smoke`

- [ ] **Step 1: Build**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
./scripts/build.sh 2>&1 | tee build.log | tail -n 8
```
Expected: `naming to docker.io/library/predpep:local … done`, no `ERROR`. (Only `COPY app/` rebuilds; heavy layers cached.)

- [ ] **Step 2: Launch throwaway with throwaway volume**
```bash
docker rm -f predpep_smoke2 2>/dev/null || true
docker volume rm predpep_data_smoke 2>/dev/null || true
docker run -d --name predpep_smoke2 -v predpep_data_smoke:/tmp/pepspec -p 6365:6363 predpep:local >/dev/null
for i in $(seq 1 60); do curl -fsS --max-time 3 http://localhost:6365/health >/dev/null 2>&1 && break; sleep 1; done
echo "health: $(curl -fsS http://localhost:6365/health)"
echo "jobs (empty): $(curl -fsS http://localhost:6365/jobs)"
```
Expected: health ok; `{"jobs":[],"success":true}` (or empty list).

- [ ] **Step 3: Submit a job → job.json written, listed as Processing**
```bash
R=$(curl -fsS -F protein_symbol=EGF -F user_name=tester -F cpus=4 -F file1=@examples/quicktest.pdb http://localhost:6365/upload)
JOB=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
echo "JOB=$JOB"
docker exec predpep_smoke2 cat /tmp/pepspec/results/$JOB/job.json; echo
curl -fsS http://localhost:6365/jobs | python3 -m json.tool | head -20
```
Expected: `job.json` shows protein_symbol=EGF, user_name=tester, cpus=4, peptide_length=4; `/jobs` lists the job with `status:"Processing"`.

- [ ] **Step 4: Simulate a Complete job → Download path**
```bash
docker exec predpep_smoke2 bash -lc 'd=/tmp/pepspec/results/FAKECT_demo0001; mkdir -p $d; printf "%s" "{\"job_id\":\"FAKECT_demo0001\",\"submitted_at\":\"2026-06-13T00:00:00+00:00\",\"protein_symbol\":\"FAK\",\"user_name\":\"demo\",\"cpus\":2,\"pdb_filename\":\"FAKECT.pdb\",\"peptide_length\":3}" > $d/job.json; echo hello > $d/payload.txt; (cd $d && zip -q FAKECT_demo0001.zip payload.txt)'
curl -fsS http://localhost:6365/jobs | python3 -c "import sys,json;[print(j['job_id'],j['status'],j['download_url']) for j in json.load(sys.stdin)['jobs']]"
curl -fsS -o /tmp/dl.zip http://localhost:6365/download/FAKECT_demo0001/FAKECT_demo0001.zip && unzip -l /tmp/dl.zip | tail -2
```
Expected: the FAKE job shows `Complete` with a `download_url`; the zip downloads and lists `payload.txt`.

- [ ] **Step 5: Persistence across container recreate (the key check)**
```bash
docker rm -f predpep_smoke2 >/dev/null
docker run -d --name predpep_smoke2 -v predpep_data_smoke:/tmp/pepspec -p 6365:6363 predpep:local >/dev/null
for i in $(seq 1 60); do curl -fsS --max-time 3 http://localhost:6365/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://localhost:6365/jobs | python3 -c "import sys,json;print('jobs after recreate:', [j['job_id'] for j in json.load(sys.stdin)['jobs']])"
```
Expected: both `$JOB` and `FAKECT_demo0001` still listed → the volume persisted across recreate.

- [ ] **Step 6: Delete removes files + reclaims disk**
```bash
curl -fsS -X DELETE http://localhost:6365/jobs/FAKECT_demo0001; echo
docker exec predpep_smoke2 bash -lc 'ls /tmp/pepspec/results/FAKECT_demo0001 2>&1 | head -1'
curl -fsS http://localhost:6365/jobs | python3 -c "import sys,json;print('remaining:', [j['job_id'] for j in json.load(sys.stdin)['jobs']])"
```
Expected: delete returns `success:true`; the dir is gone (`No such file or directory`); the job no longer listed.

- [ ] **Step 7: Tear down throwaway container + volume**
```bash
docker rm -f predpep_smoke2 >/dev/null
docker volume rm predpep_data_smoke >/dev/null
echo "throwaway cleaned"
```

---

### Task 5: README

**Files:** Modify `README.md`

- [ ] **Step 1: Add Jobs to the Web UI section**

In the `## Web UI` section, after the first paragraph, add:
```markdown

A **Jobs** tab lists every job on the machine (date, submission details, status, a download link, and a delete button) — persisted on a Docker volume so they survive page reloads and container restarts. Deleting a job removes its files from disk (use it to reclaim space). No login: all jobs are visible to anyone who can reach the node.
```

- [ ] **Step 2: Add a persistence + caveats note under Known limitations**

In `## Known limitations`, add two bullets:
```markdown
- **Job data persists on a Docker volume** (`predpep_data`, mounted at `/tmp/pepspec`). It survives container recreate/redeploy; back it up with `docker run --rm -v predpep_data:/data -v "$PWD":/backup busybox tar czf /backup/predpep_data.tgz -C /data .`. Removing the volume (`docker volume rm predpep_data`) erases all job history.
- A job interrupted by a container restart shows **"Processing"** indefinitely (no run-state tracking yet); deleting a *running* job removes its files and its background run then fails. Both are resolved by the planned job-queue work.
```

- [ ] **Step 3: Commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git add README.md
git commit -q -m "docs: document the Jobs tab, volume persistence, and caveats"
```

---

### Task 6: Merge + cut over `predpep_app` (with the volume)

**Files:** none (git + docker)

- [ ] **Step 1: Merge to main**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git merge --ff-only feature/job-list
test -z "$(git status --porcelain --untracked-files=all)" && echo CLEAN || git status --short
git branch -d feature/job-list
```
Expected: ff-merge; `CLEAN`.

- [ ] **Step 2: Cut over with the volume**
```bash
docker rm -f predpep_app
./scripts/run.sh
for i in $(seq 1 60); do curl -fsS --max-time 3 http://localhost:6363/health >/dev/null 2>&1 && break; sleep 1; done
echo "health: $(curl -fsS http://localhost:6363/health)"
echo "jobs:   $(curl -fsS http://localhost:6363/jobs)"
docker inspect predpep_app --format 'volume: {{range .Mounts}}{{.Name}}->{{.Destination}} {{end}} | health: {{.State.Health.Status}}'
```
Expected: health ok; `/jobs` returns an (initially empty) list; mount shows `predpep_data->/tmp/pepspec`; health `starting`→`healthy`.

- [ ] **Step 3: Confirm the volume exists + rollback retained**
```bash
docker volume ls | grep predpep_data
docker images predpep --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'local|prejobs'
```
Expected: `predpep_data` volume present; `predpep:local` (new) + `predpep:prejobs` (rollback).

> **Rollback:** `docker rm -f predpep_app && docker tag predpep:prejobs predpep:local && ./scripts/run.sh` (the volume `predpep_data` is untouched by rollback, so job data is retained).

---

## Self-Review

**Spec coverage:** volume at /tmp/pepspec in run/run-dev/compose (Task 3) ✓; `job.json` w/ peptide length (Task 1.2–1.3) ✓; `GET /jobs` status+download (Task 1.4) ✓; `DELETE /jobs/<id>` removes result+upload dirs w/ validation (Task 1.4) ✓; Jobs tab table + poll + download + delete (Task 2) ✓; verify incl. persistence-across-recreate + delete (Task 4.5–4.6) ✓; cutover with volume + `predpep:prejobs` rollback (Task 0, 6) ✓; caveats documented (Task 5.2) ✓.

**Placeholder scan:** none — full code for every route, the JS file, and each edit; exact verification commands with expected output.

**Type/name consistency:** `BASE_RESULT_FOLDER`/`BASE_UPLOAD_FOLDER` (existing constants) used in both new routes; `job.json` keys (`job_id, submitted_at, protein_symbol, user_name, cpus, pdb_filename, peptide_length`) identical between write (1.3), list (1.4), and the table columns (2.1/2.3); `window.loadJobs/deleteJob/startJobsPolling/stopJobsPolling` defined in tab7_jobs.js and called in index.js (2.6); container `predpep_smoke2`/port 6365/volume `predpep_data_smoke` for test, `predpep_app`/6363/`predpep_data` for cutover; `predpep:prejobs` rollback consistent (Task 0/6).
