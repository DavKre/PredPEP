# UI Cleanup + Stop Button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stale "do not close this page" message, add a Stop button that cancels a running job (kills its pipeline tree), and remove the two non-functional TMAP tabs (3 & 6).

**Architecture:** Backend launches the manager in its own process group + records `manager.pid`; a new `POST /jobs/<id>/stop` kills that group and writes a `STOPPED` marker; `GET /jobs` gains a `Stopped` status; `DELETE` kills first. Frontend: update the loading text, add a Stop button to the Jobs table, and remove the tab3/tab6 buttons+views+wiring (renumber the rest to 1–5).

**Tech Stack:** Flask (`os.killpg`, `signal`), vanilla JS, Docker.

---

### Task 0: Branch + rollback tag
- [ ] **Step 1**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git checkout -b feature/stop-and-cleanup
docker tag predpep:local predpep:preui2
git status -sb | head -1
```
Expected: `## feature/stop-and-cleanup`.

---

### Task 1: Backend — process group, manager.pid, stop endpoint, status
**Files:** Modify `app/predPEP.py`

- [ ] **Step 1: import signal**

Change:
```python
import json
from datetime import datetime, timezone
```
to:
```python
import json
import signal
from datetime import datetime, timezone
```

- [ ] **Step 2: add the kill helper** (after `count_peptide_residues`)

Insert immediately after the `count_peptide_residues` function's `return len(seen) or None` line:
```python

def _kill_job(jdir):
    """SIGTERM the job's manager process group (manager + its bash/Rosetta children). Best-effort."""
    try:
        with open(os.path.join(jdir, 'manager.pid')) as f:
            pid = int(f.read().strip())
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
        return False
```

- [ ] **Step 3: process group + write manager.pid**

Change:
```python
        subprocess.Popen(
            manager_command, close_fds=True,
            stdout=open(os.path.join(master_result_folder, f'{job_folder_name}_manager_stdout.log'), 'w'),
            stderr=open(os.path.join(master_result_folder, f'{job_folder_name}_manager_stderr.log'), 'w')
        )
        
        return jsonify({
```
to:
```python
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
```

- [ ] **Step 4: Stopped status in `list_jobs`**

Change:
```python
            if os.path.exists(os.path.join(jdir, f"{entry}.zip")):
                meta['status'] = 'Complete'
                meta['download_url'] = f"/download/{entry}/{entry}.zip"
            else:
                meta['status'] = 'Processing'
                meta['download_url'] = None
```
to:
```python
            if os.path.exists(os.path.join(jdir, f"{entry}.zip")):
                meta['status'] = 'Complete'
                meta['download_url'] = f"/download/{entry}/{entry}.zip"
            elif os.path.exists(os.path.join(jdir, 'STOPPED')):
                meta['status'] = 'Stopped'
                meta['download_url'] = None
            else:
                meta['status'] = 'Processing'
                meta['download_url'] = None
```

- [ ] **Step 5: stop endpoint + delete-also-kills**

Change the `delete_job` body start:
```python
    removed = []
    for base in (BASE_RESULT_FOLDER, BASE_UPLOAD_FOLDER):
```
to:
```python
    _kill_job(os.path.join(BASE_RESULT_FOLDER, job_id))
    removed = []
    for base in (BASE_RESULT_FOLDER, BASE_UPLOAD_FOLDER):
```
Then insert a new route immediately before the `@predPEP.route('/download/<master_dir_name>/<filename>')` line:
```python
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


```

- [ ] **Step 6: verify + commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
python3 -c "import ast; ast.parse(open('app/predPEP.py').read()); print('OK')"
grep -nE "def _kill_job|/jobs/<job_id>/stop|start_new_session|STOPPED" app/predPEP.py
git add app/predPEP.py
git commit -q -m "feat(api): stop a running job (POST /jobs/<id>/stop) + Stopped status; delete kills first"
```
Expected: `OK`; helper, route, start_new_session, STOPPED all shown.

---

### Task 2: Loading message
**Files:** Modify `app/templates/index.html`

- [ ] **Step 1**

Change:
```html
            <div id="loading">Processing... (Do not close this page)</div>
```
to:
```html
            <div id="loading">Processing… you can leave this page — the job keeps running. Track it in the <b>Jobs</b> tab.</div>
```

- [ ] **Step 2: commit** (combined with Task 3's HTML — commit happens at end of Task 3)

---

### Task 3: Remove tabs 3 (T-Maps) & 6 (Tree-Map MST)
**Files:** Modify `app/templates/index.html`, `app/static/index.js`; delete `app/static/tab3_tmaps.js`, `app/static/tab6_tmap.js`

- [ ] **Step 1: `index.html` — remove the two buttons + renumber labels**

Change the whole button block:
```html
            <button id="tab1Button" class="active">1. Template</button>
            <button id="tab2Button" disabled>2. Results Table & Viewer</button>
            <button id="tab3Button" disabled>3. T-Maps</button>
            <button id="tab4Button" disabled>4. Score Comparison Plots</button>
            <button id="tab5Button" disabled>5. Clustering & Heatmaps</button>
            <button id="tab6Button" disabled>6. Tree-Map (MST)</button>
            <button id="tab7Button">7. Jobs</button>
```
to:
```html
            <button id="tab1Button" class="active">1. Template</button>
            <button id="tab2Button" disabled>2. Results Table & Viewer</button>
            <button id="tab4Button" disabled>3. Score Comparison Plots</button>
            <button id="tab5Button" disabled>4. Clustering & Heatmaps</button>
            <button id="tab7Button">5. Jobs</button>
```

- [ ] **Step 2: `index.html` — delete the `tab3-view` block**

Delete the entire block from `<div id="tab3-view" class="tab-content hidden clearfix">` through its closing `</div>` (the one right before `<div id="tab4-view"`), i.e. the T-Maps view including its `tmapDropdown` select and the `tmap-foldx-best`/`tmap-all-scores` containers.

- [ ] **Step 3: `index.html` — delete the `tab6-view` block**

Delete the entire block from `<div id="tab6-view" class="tab-content hidden clearfix">` through its closing `</div>` (it ends with `<div id="tmap6-container" ...></div>` then `</div>`), i.e. the Tree-Map view including its `tmap6Dropdown` select. (The `tab7-view` Jobs block remains immediately after.)

- [ ] **Step 4: `index.js` — drop TMAP imports**

Change:
```javascript
import { processAllData, renderTSNEPlots, renderComparisonPlots, renderAdvancedPlots, renderMSTTree } from './plots_utils.js';
```
to:
```javascript
import { processAllData, renderComparisonPlots, renderAdvancedPlots } from './plots_utils.js';
```

- [ ] **Step 5: `index.js` — remove tab3/tab6 from the arrays**

Change:
```javascript
    const tabButtons = [
        document.getElementById('tab1Button'),
        document.getElementById('tab2Button'),
        document.getElementById('tab3Button'),
        document.getElementById('tab4Button'),
        document.getElementById('tab5Button'),
        document.getElementById('tab6Button'),
        document.getElementById('tab7Button')
    ];
    const tabViews = [
        document.getElementById('tab1-view'),
        document.getElementById('tab2-view'),
        document.getElementById('tab3-view'),
        document.getElementById('tab4-view'),
        document.getElementById('tab5-view'),
        document.getElementById('tab6-view'),
        document.getElementById('tab7-view')
    ];
```
to:
```javascript
    const tabButtons = [
        document.getElementById('tab1Button'),
        document.getElementById('tab2Button'),
        document.getElementById('tab4Button'),
        document.getElementById('tab5Button'),
        document.getElementById('tab7Button')
    ];
    const tabViews = [
        document.getElementById('tab1-view'),
        document.getElementById('tab2-view'),
        document.getElementById('tab4-view'),
        document.getElementById('tab5-view'),
        document.getElementById('tab7-view')
    ];
```

- [ ] **Step 6: `index.js` — Jobs index 6 → 4**

Change:
```javascript
                if (index === 6 && window.startJobsPolling) window.startJobsPolling();
```
to:
```javascript
                if (index === 4 && window.startJobsPolling) window.startJobsPolling();
```

- [ ] **Step 7: `index.js` — rewrite `handlePlotRendering` for the new positions**

Change:
```javascript
        switch (tabIndex) {
            case 2:
                renderTSNEPlots(document.getElementById('tmapDropdown').value || 'FoldX');
                break;
            case 3:
                renderComparisonPlots();
                break;
            case 4:
                renderAdvancedPlots();
                break;
            case 5: // Tab 6 logic
                renderMSTTree(document.getElementById('tmap6Dropdown').value || 'FoldX_Score');
                break;
        }
```
to:
```javascript
        switch (tabIndex) {
            case 2:
                renderComparisonPlots();
                break;
            case 3:
                renderAdvancedPlots();
                break;
        }
```

- [ ] **Step 8: `index.js` — remove the TMAP dropdown listeners**

Delete this block:
```javascript
    document.getElementById('tmapDropdown').addEventListener('change', function() {
        if (!this.disabled) renderTSNEPlots(this.value);
    });

    // Listener for new Tab 6 dropdown
    const tmap6Drop = document.getElementById('tmap6Dropdown');
    if (tmap6Drop) {
        tmap6Drop.addEventListener('change', function() {
            if (!this.disabled) renderMSTTree(this.value);
        });
    }
```

- [ ] **Step 9: `index.js` — drop tab3/tab6 from the post-results enable list**

Change:
```javascript
            ['tab2Button', 'tab3Button', 'tab4Button', 'tab5Button', 'tab6Button'].forEach(id => {
```
to:
```javascript
            ['tab2Button', 'tab4Button', 'tab5Button'].forEach(id => {
```

- [ ] **Step 10: delete the now-unused TMAP tab scripts**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git rm --quiet app/static/tab3_tmaps.js app/static/tab6_tmap.js
```

- [ ] **Step 11: verify + commit (HTML message + tab removal together)**
```bash
node --check app/static/index.js && echo "index.js OK"
grep -cE "tab3|tab6|renderTSNEPlots|renderMSTTree|tmapDropdown|tmap6Dropdown" app/static/index.js app/templates/index.html
echo "^ should be 0 (no tab3/tab6/TMAP refs remain)"
git add app/templates/index.html app/static/index.js
git commit -q -m "feat(ui): update loading message; remove non-functional TMAP tabs (renumber to 1-5)"
```
Expected: `index.js OK`; the grep count is `0`.

---

### Task 4: Stop button in the Jobs table
**Files:** Modify `app/static/tab7_jobs.js`, `app/templates/index.html` (one CSS line)

- [ ] **Step 1: `tab7_jobs.js` — render a Stop button on Processing rows**

Change the actions cell + listener wiring. Replace:
```javascript
                <td class="${cls}">${esc(j.status)}</td><td>${dl}</td>
                <td><button class="job-delete" data-id="${encodeURIComponent(j.job_id)}">Delete</button></td>
            </tr>`;
        }).join('');
        tbody.querySelectorAll('.job-delete').forEach(b =>
            b.addEventListener('click', () => window.deleteJob(decodeURIComponent(b.dataset.id))));
```
with:
```javascript
                <td class="${cls}">${esc(j.status)}</td><td>${dl}</td>
                <td>${j.status === 'Processing' ? `<button class="job-stop" data-id="${encodeURIComponent(j.job_id)}">Stop</button> ` : ''}<button class="job-delete" data-id="${encodeURIComponent(j.job_id)}">Delete</button></td>
            </tr>`;
        }).join('');
        tbody.querySelectorAll('.job-stop').forEach(b =>
            b.addEventListener('click', () => window.stopJob(decodeURIComponent(b.dataset.id))));
        tbody.querySelectorAll('.job-delete').forEach(b =>
            b.addEventListener('click', () => window.deleteJob(decodeURIComponent(b.dataset.id))));
```

- [ ] **Step 2: `tab7_jobs.js` — add `window.stopJob`**

Immediately before `window.deleteJob = async function (jobId) {`, insert:
```javascript
window.stopJob = async function (jobId) {
    if (!confirm(`Stop job ${jobId}? Its pipeline will be terminated.`)) return;
    try {
        const res = await fetch(`/jobs/${encodeURIComponent(jobId)}/stop`, { method: 'POST' });
        const data = await res.json();
        if (!data.success) alert(`Stop failed: ${data.error || 'unknown'}`);
    } catch (e) { alert(`Stop error: ${e}`); }
    window.loadJobs();
};

```

- [ ] **Step 3: `index.html` — Stop button CSS**

Change:
```css
        .job-delete { background: #c5221f; color: #fff; border: none; padding: 3px 8px; cursor: pointer; border-radius: 3px; }
```
to:
```css
        .job-delete { background: #c5221f; color: #fff; border: none; padding: 3px 8px; cursor: pointer; border-radius: 3px; }
        .job-stop { background: #b06000; color: #fff; border: none; padding: 3px 8px; cursor: pointer; border-radius: 3px; }
```

- [ ] **Step 4: verify + commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
node --check app/static/tab7_jobs.js && echo "tab7_jobs.js OK"
git add app/static/tab7_jobs.js app/templates/index.html
git commit -q -m "feat(ui): Stop button on Processing jobs"
```
Expected: `tab7_jobs.js OK`.

---

### Task 5: Build + smoke
**Files:** none — `predpep_smoke2` (6365) + throwaway volume `predpep_data_smoke`

- [ ] **Step 1: Build**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
./scripts/build.sh 2>&1 | tee build.log | tail -n 6
```
Expected: image written, no ERROR.

- [ ] **Step 2: Launch + gentle wait (NO poll-loop — it wedges the worker)**
```bash
docker rm -f predpep_smoke2 2>/dev/null; docker volume rm predpep_data_smoke 2>/dev/null
docker run -d --name predpep_smoke2 -v predpep_data_smoke:/tmp/pepspec -p 6365:6363 predpep:local >/dev/null
sleep 12
curl -fsS --max-time 8 http://localhost:6365/health; echo
```
Expected: health JSON.

- [ ] **Step 3: UI changes present**
```bash
H=$(curl -fsS --max-time 8 http://localhost:6365/)
echo "$H" | grep -c "Do not close this page"   # expect 0
echo "$H" | grep -oE "id=\"tab[0-9]Button\"[^>]*>[^<]*"   # expect tab1,2,4,5,7 labelled 1-5; no tab3/tab6
```
Expected: `0`; buttons `tab1..tab2,tab4,tab5,tab7` labelled `1.`–`5.`, no T-Maps/Tree-Map.

- [ ] **Step 4: Submit → manager.pid; Stop → Stopped**
```bash
R=$(curl -fsS -F protein_symbol=EGF -F user_name=t -F cpus=4 -F file1=@examples/quicktest.pdb http://localhost:6365/upload)
JOB=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
echo "JOB=$JOB"
sleep 5
docker exec predpep_smoke2 bash -lc "cat /tmp/pepspec/results/$JOB/manager.pid; echo; pgrep -af run_iteMAN | head -1"
echo "stop: $(curl -fsS --max-time 8 -X POST http://localhost:6365/jobs/$JOB/stop)"
sleep 3
echo "manager alive after stop? $(docker exec predpep_smoke2 bash -lc 'pgrep -f run_iteMAN >/dev/null && echo YES || echo no')"
echo "status: $(curl -fsS --max-time 8 http://localhost:6365/jobs | python3 -c "import sys,json;print([(j['job_id'],j['status']) for j in json.load(sys.stdin)['jobs']])")"
```
Expected: `manager.pid` has a number, manager process exists; stop returns `killed:true`; manager **no** longer alive; `/jobs` shows the job `Stopped`.

- [ ] **Step 5: Teardown**
```bash
docker rm -f predpep_smoke2 >/dev/null; docker volume rm predpep_data_smoke >/dev/null; echo cleaned
```

---

### Task 6: Merge + cut over
- [ ] **Step 1: Merge**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git merge --ff-only feature/stop-and-cleanup
test -z "$(git status --porcelain --untracked-files=all)" && echo CLEAN || git status --short
git branch -d feature/stop-and-cleanup
```
Expected: ff-merge; `CLEAN`.

- [ ] **Step 2: Cut over (gentle wait), preserving the data volume**
```bash
docker rm -f predpep_app >/dev/null
./scripts/run.sh >/dev/null
sleep 12
echo "health: $(curl -fsS --max-time 8 http://localhost:6363/health)"
echo "UI:     $(curl -fsS --max-time 8 http://localhost:6363/ | grep -ioE '<title>[^<]*</title>')"
docker inspect predpep_app --format 'vol:{{range .Mounts}}{{.Name}}{{end}} health:{{.State.Health.Status}}'
docker images predpep --format '{{.Repository}}:{{.Tag}}' | grep -E 'local|preui2'
```
Expected: health ok; UI title; `vol:predpep_data`; `predpep:local` + `predpep:preui2` present.

> **Rollback:** `docker rm -f predpep_app && docker tag predpep:preui2 predpep:local && ./scripts/run.sh` (volume retained).

---

## Self-Review
**Spec coverage:** loading message (Task 2) ✓; process group + manager.pid (Task 1.2–1.3) ✓; `POST /jobs/<id>/stop` + STOPPED + Stopped status (Task 1.4–1.5) ✓; delete-kills-first (Task 1.5) ✓; Stop button UI (Task 4) ✓; remove tab3/tab6 buttons+views+wiring+renumber (Task 3) ✓; verify incl. stop→Stopped + tabs gone (Task 5) ✓; cutover w/ volume + `predpep:preui2` rollback (Task 0/6) ✓.

**Placeholder scan:** none — full code for every edit; exact verify commands.

**Type/name consistency:** `_kill_job` defined (1.2), used by stop (1.5) + delete (1.5); `manager.pid`/`STOPPED` filenames consistent across write (1.3), status (1.4), kill (1.2); `window.stopJob` defined (4.2) and wired (4.1); `/jobs/<id>/stop` path identical in backend (1.5) and tab7_jobs.js (4.2); tab arrays `[tab1,tab2,tab4,tab5,tab7]` consistent with Jobs index `4` (3.5/3.6) and handlePlotRendering cases 2/3 (3.7); `predpep:preui2` rollback consistent (0/6).
