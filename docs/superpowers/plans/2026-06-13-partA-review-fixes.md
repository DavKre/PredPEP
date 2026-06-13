# Part A — Review Hardening Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Apply the low-risk mechanical fixes from the deep review (status-hang regression, Ca-ion miscount, download dir validation, large-file warning, log rotation, build assertion, doc fixes).

**Architecture:** Small surgical edits to `app/predPEP.py`, two JS files, `index.html`, `Dockerfile`, run scripts, compose, README. No new components. The scheduler/retention/capacity work is Part B.

**Tech Stack:** Flask, vanilla JS, Docker.

---

### Task 0: Branch + rollback tag
- [ ] **Step 1**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git checkout -b feature/partA-fixes
docker tag predpep:local predpep:preA
git status -sb | head -1
```
Expected: `## feature/partA-fixes`.

---

### Task 1: Backend fixes (`app/predPEP.py`)
- [ ] **Step 1: `check_status` — report Stopped**

Change:
```python
        if os.path.exists(master_result_dir):
            return jsonify({'status': 'Processing', 'message': 'Job is running iterations...'})
        return jsonify({'status': 'Pending/Failed', 'message': 'Job failed to start.'})
```
to:
```python
        if os.path.exists(os.path.join(master_result_dir, 'STOPPED')):
            return jsonify({'status': 'Stopped', 'message': 'Job was stopped.'})
        if os.path.exists(master_result_dir):
            return jsonify({'status': 'Processing', 'message': 'Job is running iterations...'})
        return jsonify({'status': 'Pending/Failed', 'message': 'Job failed to start.'})
```

- [ ] **Step 2: `count_peptide_residues` — exclude calcium**

Change:
```python
                if line.startswith(("ATOM", "HETATM")) and line[21:22] == "B" and line[12:16].strip() == "CA":
                    seen.add(line[22:27])  # resSeq + iCode (fixed columns)
```
to:
```python
                if line.startswith(("ATOM", "HETATM")) and line[21:22] == "B" and line[12:16].strip() == "CA" and line[76:78].strip() in ("C", ""):
                    seen.add(line[22:27])  # resSeq + iCode; element guard ("C"/empty) excludes calcium (CA)
```

- [ ] **Step 3: `download_file` — validate the dir segment**

Change:
```python
def download_file(master_dir_name, filename):
    return send_from_directory(os.path.join(BASE_RESULT_FOLDER, master_dir_name), filename, as_attachment=True)
```
to:
```python
def download_file(master_dir_name, filename):
    if '/' in master_dir_name or '..' in master_dir_name:
        return jsonify({'success': False, 'error': 'Invalid path.'}), 403
    return send_from_directory(os.path.join(BASE_RESULT_FOLDER, master_dir_name), filename, as_attachment=True)
```

- [ ] **Step 4: verify + commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
python3 -c "import ast; ast.parse(open('app/predPEP.py').read()); print('OK')"
git add app/predPEP.py
git commit -q -m "fix(api): /status reports Stopped; ignore calcium in peptide count; validate /download dir"
```
Expected: `OK`.

---

### Task 2: Frontend fixes
**Files:** `app/static/index.js`, `app/static/tab1_submission.js`, `app/templates/index.html`

- [ ] **Step 1: `index.js` `pollStatus` — stop polling on terminal non-complete states**

Change:
```javascript
            await fetchAndLoadResults(jobId);
        }
    } catch (e) { console.error(e); }
```
to:
```javascript
            await fetchAndLoadResults(jobId);
        } else if (data.status === 'Stopped' || data.status === 'Pending/Failed') {
            clearInterval(window.statusInterval);
            document.getElementById('loading').style.display = 'none';
        }
    } catch (e) { console.error(e); }
```

- [ ] **Step 2: `tab1_submission.js` — Ca-ion guard in the client twin**

Read `app/static/tab1_submission.js`, find `countChainBResidues` (the loop that tests a chain-B atom whose name is `CA`). Add the same element guard: only count when the PDB element column (chars 77–78, 0-indexed `line.substring(76,78).trim()`) is `'C'` or empty. Show the exact before/after at execution time (the function exists around line 18); the change is: add `&& ['C',''].includes(line.substring(76,78).trim())` to the CA condition.

- [ ] **Step 3: `tab1_submission.js` — large-file warning (no block)**

In the file-input change handler / submit path, after a file is selected, if `file.size > 25*1024*1024` set a visible non-blocking warning (reuse the existing `#cpus-hint` style or the `#message`/a new span): text `Large file (${(file.size/1048576).toFixed(0)} MB) — upload may be slow; PDBs are usually < 1 MB.` Do NOT prevent submission. Wire it to the existing FileReader/`change` listener on `#file1` (the file is already read there for peptide-length detection).

- [ ] **Step 4: `index.html` — Jobs table header label**

Change:
```html
<th>Download</th><th>Delete</th>
```
to:
```html
<th>Download</th><th>Actions</th>
```

- [ ] **Step 5: verify + commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
for f in index.js tab1_submission.js; do node --check app/static/$f && echo "$f OK"; done
git add app/static/index.js app/static/tab1_submission.js app/templates/index.html
git commit -q -m "fix(ui): stop polling on Stopped/Failed; calcium guard; large-file warning; header label"
```

---

### Task 3: Ops fixes (logs, pids, build assertion)
**Files:** `Dockerfile`, `scripts/run.sh`, `scripts/run-dev.sh`, `docker-compose.yml`

- [ ] **Step 1: `Dockerfile` — quieter logs**

Change:
```dockerfile
     "--log-level", "debug", \
```
to:
```dockerfile
     "--log-level", "info", \
```

- [ ] **Step 2: `Dockerfile` — build-time prune assertion**

Immediately AFTER the extraction+prune `RUN` (the one ending with `... ! -name tools -exec rm -rf {} +`), add a new instruction:
```dockerfile

# Fail the build if the Rosetta prune dropped anything the pipeline needs at runtime.
RUN R=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408 \
 && test -f "$R/main/source/build/src/release/linux/5.4/64/x86/gcc/7/static/pepspec.static.linuxgccrelease" \
 && test -d "$R/main/database" \
 && test -f "$R/main/tools/protein_tools/scripts/clean_pdb.py" \
 && echo "OK: Rosetta prune kept pepspec binary + database + protein_tools."
```

- [ ] **Step 3: `scripts/run.sh` — log rotation + pids limit**

Change:
```bash
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
  -p 6363:6363 \
```
to:
```bash
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
  --log-opt max-size=10m --log-opt max-file=3 \
  --pids-limit 4096 \
  -p 6363:6363 \
```

- [ ] **Step 4: `scripts/run-dev.sh` — same**

Change:
```bash
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
  -p "${HOST_PORT}:6363" \
```
to:
```bash
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
  --log-opt max-size=10m --log-opt max-file=3 \
  --pids-limit 4096 \
  -p "${HOST_PORT}:6363" \
```

- [ ] **Step 5: `docker-compose.yml` — logging + pids_limit**

Change:
```yaml
    volumes:
      - predpep_data:/tmp/pepspec
    restart: unless-stopped
```
to:
```yaml
    volumes:
      - predpep_data:/tmp/pepspec
    restart: unless-stopped
    pids_limit: 4096
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

- [ ] **Step 6: commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git add Dockerfile scripts/run.sh scripts/run-dev.sh docker-compose.yml
git commit -q -m "ops: gunicorn log-level info, container log rotation + pids-limit, build-time prune assertion"
```

---

### Task 4: README NGL note
**Files:** `README.md`

- [ ] **Step 1:** Change the Web-UI bullet:
```markdown
- **The browser needs internet access.** The viewer libraries (NGL, Plotly) are loaded from public CDNs (`cdn.jsdelivr.net`, `cdn.plot.ly`); only the small app scripts are served locally. The backend itself (job execution) does **not** need internet.
```
to:
```markdown
- **The browser needs internet access.** The page loads NGL + Plotly from public CDNs (`cdn.jsdelivr.net`, `cdn.plot.ly`); only the small app scripts (and a vendored `static/js/ngl.umd.js`, currently unused) are served locally. The backend itself (job execution) does **not** need internet.
```

- [ ] **Step 2: commit**
```bash
git add README.md
git commit -q -m "docs: clarify NGL is loaded from CDN (vendored bundle unused)"
```

---

### Task 5: Build + smoke
- [ ] **Step 1: Build** (the new assertion RUN runs — build fails if the prune is bad)
```bash
cd /home/david/DATA/OFFLINE/predpep_local
./scripts/build.sh 2>&1 | tee build.log | tail -n 8
```
Expected: ends with image written; the assertion `OK: Rosetta prune kept...` appears; no ERROR.

- [ ] **Step 2: Smoke (gentle wait + restart fallback)**
```bash
docker rm -f predpep_smoke2 2>/dev/null; docker volume rm predpep_data_smoke 2>/dev/null
docker run -d --name predpep_smoke2 -v predpep_data_smoke:/tmp/pepspec -p 6365:6363 predpep:local >/dev/null
sleep 12
curl -fsS --max-time 6 http://localhost:6365/health >/dev/null 2>&1 || { docker restart predpep_smoke2 >/dev/null; sleep 12; }
echo "health: $(curl -fsS --max-time 6 http://localhost:6365/health)"
R=$(curl -fsS --max-time 15 -F protein_symbol=EGF -F user_name=t -F cpus=4 -F file1=@examples/quicktest.pdb http://localhost:6365/upload)
JOB=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
sleep 5
curl -fsS --max-time 8 -X POST http://localhost:6365/jobs/$JOB/stop >/dev/null
sleep 3
echo "status via /status (expect Stopped): $(curl -fsS --max-time 6 http://localhost:6365/status/$JOB)"
echo "download .. rejected (expect 403): $(curl -fsS --max-time 6 -o /dev/null -w '%{http_code}' http://localhost:6365/download/../foo)"
docker rm -f predpep_smoke2 >/dev/null; docker volume rm predpep_data_smoke >/dev/null; echo cleaned
```
Expected: health ok; `/status` returns `Stopped`; the `..` download returns `403` (or `404` from routing normalization — both mean rejected, NOT a file).

- [ ] **Step 3: Cut over**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git merge --ff-only feature/partA-fixes && git branch -d feature/partA-fixes
docker rm -f predpep_app >/dev/null; ./scripts/run.sh >/dev/null
sleep 12
curl -fsS --max-time 6 http://localhost:6363/health >/dev/null 2>&1 || { docker restart predpep_app >/dev/null; sleep 12; }
echo "health: $(curl -fsS --max-time 6 http://localhost:6363/health)  | $(docker inspect predpep_app --format '{{.State.Health.Status}}')"
docker images predpep --format '{{.Repository}}:{{.Tag}}' | grep -E 'local|preA'
```
Expected: healthy; `predpep:local` + `predpep:preA` present.

> **Rollback:** `docker rm -f predpep_app && docker tag predpep:preA predpep:local && ./scripts/run.sh`.

---

## Self-Review
**Spec coverage:** #1 status hang (Task1.1 + Task2.1) ✓; Ca-ion (Task1.2 + Task2.2) ✓; download dir (Task1.3) ✓; large-file warning (Task2.3) ✓; log rotation + pids + log-level (Task3) ✓; build assertion (Task3.2) ✓; header label (Task2.4) ✓; README NGL (Task4) ✓; verify Stopped + download-reject (Task5) ✓; cutover + preA rollback (Task0/5) ✓.
**Placeholder scan:** Task2.2/2.3 reference reading `tab1_submission.js` at execution (its exact lines weren't pre-read) but specify the precise change (element guard `['C',''].includes(...)`; `>25MB` warning wired to the existing `#file1` change listener) — concrete, not a placeholder.
**Consistency:** `STOPPED` marker filename matches the stop endpoint; `predpep:preA` rollback consistent; `feature/partA-fixes` branch consistent; element columns `76:78` match between Python (Task1.2) and JS (Task2.2).
