# Phase 2A — Headless, CPU-only Slim Image — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the deployed image headless (no browser UI) and CPU-only (base `ubuntu:22.04`, no `--gpus`), drop the dead `tmap`/`ogdf` weight, verify it still computes with a real `SPEGFH.pdb` job, and cut over the running `predpep_app` to the slim image with a rollback safety net.

**Architecture:** Edit the Flask app to remove presentation routes and add `/health`; delete the frontend files; swap the Dockerfile base to `ubuntu:22.04` and remove tmap/ogdf extraction + the OGDF ldconfig step; drop `--gpus` from run configs. The "test" is a real end-to-end pipeline job through a throwaway container, then a container cutover.

**Tech Stack:** Docker/BuildKit, Flask/gunicorn (unchanged versions), Rosetta `pepspec.static.linuxgccrelease` + FoldX (CPU), bash, git, curl.

---

## Constraints

- The user **authorized** stopping/replacing `predpep_app` (no longer in use). Cutover is intended.
- Keep a rollback image `predpep:phase1-cuda` (the current good CUDA image) before building.
- In-image paths unchanged; `pipeline/` scripts not modified.
- Verify the full job **before** cutting over; old container stays up (idle) until then.

## File map

| File | Change |
|---|---|
| `app/predPEP.py` | remove `index`/`get_tmap_tree` routes + unused imports; add `/health` |
| `app/templates/`, `app/static/`, `app/tmap_utils.py` | deleted |
| `.gitignore` | drop moot `app/static/*` lines; add `testdata/` |
| `Dockerfile` | base→ubuntu:22.04; drop tmap/ogdf extraction + ldconfig; PATH; HEALTHCHECK→/health |
| `scripts/run.sh`, `scripts/run-dev.sh`, `docker-compose.yml` | drop `--gpus all` |
| `README.md` | drop GPU prereqs/troubleshooting; headless + CPU-only notes; size |
| `blobs/blobs.sha256` | regenerate for foldx+miniforge3+rosetta only |

---

### Task 0: Pre-flight — branch, preserve test input, rollback tag

- [ ] **Step 1: Confirm live container, branch off main**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
docker ps --filter name=predpep_app --format '{{.ID}}  {{.Image}}  {{.Status}}' | tee /tmp/predpep_app_pre2a.txt
git checkout main && git checkout -b phase2a/headless-slim
git status -sb | head -1
```
Expected: shows `predpep_app` running; `## phase2a/headless-slim`.

- [ ] **Step 2: Copy a real test PDB out of the live container (kept after cutover)**

Run:
```bash
mkdir -p testdata
SRC=$(docker exec predpep_app bash -lc 'ls /tmp/pepspec/uploads/SPEGFH_*/SPEGFH.pdb 2>/dev/null | head -1')
echo "source in container: $SRC"
docker cp "predpep_app:$SRC" testdata/SPEGFH.pdb
ls -l testdata/SPEGFH.pdb && head -1 testdata/SPEGFH.pdb
```
Expected: `testdata/SPEGFH.pdb` exists, non-empty, first line looks like a PDB record (`HEADER`/`ATOM`/`REMARK`/`MODEL`).

- [ ] **Step 3: Tag the current good image as rollback**

Run:
```bash
docker tag predpep:local predpep:phase1-cuda
docker image inspect predpep:phase1-cuda --format 'rollback size: {{.Size}} bytes'
```
Expected: prints the byte size of the current (CUDA) image. Record it.

---

### Task 1: App → headless (`app/predPEP.py`)

**Files:** Modify `app/predPEP.py`

- [ ] **Step 1: Remove unused imports (pandas, render_template, secure_filename)**

Change:
```python
import pandas as pd
from flask import Flask, request, render_template, send_from_directory, jsonify
from werkzeug.utils import secure_filename
```
to:
```python
from flask import Flask, request, send_from_directory, jsonify
```
(Removes `import pandas as pd` and the `secure_filename` import entirely; drops `render_template` from the flask import.)

- [ ] **Step 2: Remove the tmap_utils import block**

Delete these lines:
```python
# Import TMAP logic from the local utility
try:
    from tmap_utils import generate_tmap_coordinates
except ImportError:
    print("Warning: tmap_utils.py not found. TMAP Tree functionality will be limited.")
    # FIX: Return 5 values (x, y, s, t, valid_indices) to match the new signature
    # FIX: 5 Werte zurückgeben, um der neuen Signatur zu entsprechen
    def generate_tmap_coordinates(seqs): return [], [], [], [], []
```

- [ ] **Step 3: Replace the index route with a health route**

Change:
```python
@predPEP.route('/')
def index():
    """Renders the main page with the file upload form."""
    return render_template('index.html')
```
to:
```python
@predPEP.route('/')
@predPEP.route('/health')
def health():
    """Liveness probe for the headless node."""
    return jsonify({"service": "predpep-node", "status": "ok"})
```

- [ ] **Step 4: Remove the TMAP tree section and route**

Delete the whole block (section header comment through the end of the function):
```python
# ----------------------------------------------------------------------
# ## 🌳 TMAP TREE ROUTE (MODIFIED FOR STABILITY)
# ----------------------------------------------------------------------

@predPEP.route('/get_tmap_tree/<job_id>', methods=['GET'])
def get_tmap_tree(job_id):
```
...through its final line...
```python
        print(f"TMAP Tree Error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
```

- [ ] **Step 5: Verify no dangling references and Python parses**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
grep -nE "render_template|tmap|pandas|pd\.|secure_filename" app/predPEP.py || echo "no stale refs"
python3 -c "import ast; ast.parse(open('app/predPEP.py').read()); print('predPEP.py parses OK')"
```
Expected: `no stale refs` and `predPEP.py parses OK`. (Plain `python3 ast.parse` only checks syntax — flask need not be installed on the host.)

- [ ] **Step 6: Commit**

Run:
```bash
git add app/predPEP.py
git commit -q -m "feat: headless app — drop UI/tmap routes, add /health"
```

---

### Task 2: Remove the frontend files

**Files:** delete `app/templates/`, `app/static/`, `app/tmap_utils.py`; edit `.gitignore`

- [ ] **Step 1: Remove tracked frontend files from git, then clear on-disk leftovers**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git rm -r --quiet app/templates app/static app/tmap_utils.py
rm -rf app/templates app/static        # clears gitignored ngl-master/molstar left on disk
ls app
```
Expected: `git rm` removes tracked files; `ls app` now shows just `predPEP.py` and `pipelines.txt`.

- [ ] **Step 2: Drop the moot frontend lines from `.gitignore` and add testdata/**

In `.gitignore`, delete:
```gitignore
app/static/ngl-master/
app/static/molstar/
```
and (under the heavy-assets section) add:
```gitignore
# Local test inputs copied out of the running container
testdata/
```

- [ ] **Step 3: Verify testdata is ignored, app is lean**

Run:
```bash
git check-ignore testdata/SPEGFH.pdb && echo "testdata ignored"
git status --short
```
Expected: `testdata ignored`; status shows the deletions of `app/static/*`, `app/templates/*`, `app/tmap_utils.py` staged, plus modified `.gitignore`. No `testdata/` or `ngl-master/molstar` appear.

- [ ] **Step 4: Commit**

Run:
```bash
git add .gitignore
git commit -q -m "refactor: remove frontend (templates/static/tmap_utils) for headless node"
```

---

### Task 3: Dockerfile — slim + CPU-only

**Files:** Modify `Dockerfile`

- [ ] **Step 1: Swap the base image**

Change:
```dockerfile
FROM nvidia/cuda:12.4.0-devel-ubuntu22.04
```
to:
```dockerfile
FROM ubuntu:22.04
```

- [ ] **Step 2: Drop tmap + ogdf from blob extraction**

Change:
```dockerfile
RUN --mount=type=bind,source=./blobs,target=/tmp/blobs,readonly \
    tar -xzf /tmp/blobs/rosetta.tar.gz    -C /usr/local/ \
 && tar -xzf /tmp/blobs/foldx.tar.gz      -C /usr/local/ \
 && tar -xzf /tmp/blobs/ogdf.tar.gz       -C /usr/local/ \
 && tar -xzf /tmp/blobs/tmap.tar.gz       -C /usr/local/ \
 && tar -xzf /tmp/blobs/miniforge3.tar.gz -C /home/${USER_NAME}/ \
 && chown -R ${USER_UID}:${USER_GID} /home/${USER_NAME}/miniforge3
```
to:
```dockerfile
RUN --mount=type=bind,source=./blobs,target=/tmp/blobs,readonly \
    tar -xzf /tmp/blobs/rosetta.tar.gz    -C /usr/local/ \
 && tar -xzf /tmp/blobs/foldx.tar.gz      -C /usr/local/ \
 && tar -xzf /tmp/blobs/miniforge3.tar.gz -C /home/${USER_NAME}/ \
 && chown -R ${USER_UID}:${USER_GID} /home/${USER_NAME}/miniforge3
```

- [ ] **Step 3: Delete the OGDF ldconfig step (comment block + RUN)**

Delete this entire block:
```dockerfile
# Register OGDF's shared libs with the dynamic linker. Needed at runtime by the
# conda env's `tmap` native extension, which links against libOGDF.so.2025.10.01
# (located in /usr/local/ogdf/build/ after blob extraction). Production image's
# LD_LIBRARY_PATH didn't cover this — how it resolved upstream is unknown
# (likely an ld.so.conf.d entry in a hand-committed layer, or tmap was silently
# broken there and masked by predPEP.py's try/except ImportError fallback).
# Kept as its own small layer so blob extraction stays cached on rebuild.
RUN echo "/usr/local/ogdf/build" > /etc/ld.so.conf.d/ogdf.conf && ldconfig
```

- [ ] **Step 4: Drop the CUDA/nvidia PATH entries**

Change:
```dockerfile
    PATH=/home/spacepep/miniforge3/envs/predPEP/bin:/home/spacepep/miniforge3/condabin:/usr/local/nvidia/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```
to:
```dockerfile
    PATH=/home/spacepep/miniforge3/envs/predPEP/bin:/home/spacepep/miniforge3/condabin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

- [ ] **Step 5: Point HEALTHCHECK at /health**

Change:
```dockerfile
  CMD curl --fail http://localhost:6363/ || exit 1
```
to:
```dockerfile
  CMD curl --fail http://localhost:6363/health || exit 1
```

- [ ] **Step 6: Sanity check + commit**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
grep -nE "^FROM|ogdf|tmap|ldconfig|nvidia|cuda|/health" Dockerfile
git add Dockerfile
git commit -q -m "build: ubuntu:22.04 base, drop tmap/ogdf + GPU, healthcheck /health"
```
Expected: `FROM ubuntu:22.04`; no `ogdf`/`tmap`/`ldconfig`/`nvidia`/`cuda` lines remain; HEALTHCHECK shows `/health`.

---

### Task 4: Runtime configs + README (CPU-only)

**Files:** Modify `scripts/run.sh`, `scripts/run-dev.sh`, `docker-compose.yml`, `README.md`

- [ ] **Step 1: Drop `--gpus all` from `scripts/run.sh`**

Delete the line:
```bash
  --gpus all \
```
(from the `docker run -d \` block — the one with `--restart unless-stopped`).

- [ ] **Step 2: Drop `--gpus all` from `scripts/run-dev.sh`**

Delete the line:
```bash
  --gpus all \
```
(from its `docker run -d \` block — the one with the bind mounts).

- [ ] **Step 3: Remove the GPU reservation from `docker-compose.yml`**

Change:
```yaml
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```
to:
```yaml
    restart: unless-stopped
```

- [ ] **Step 4: README — headless + CPU-only**

In `README.md`:

(a) Change the opening description:
```markdown
Flask + gunicorn web UI for peptide design, running Rosetta + FoldX pipelines.
```
to:
```markdown
Headless Flask + gunicorn JSON backend for peptide design, running Rosetta + FoldX pipelines (CPU-only). No browser UI — it exposes an HTTP API on port 6363 (`/health` for liveness).
```

(b) Replace the Prerequisites list:
```markdown
- NVIDIA GPU, driver 550+ (for CUDA 12.4 compatibility)
- `nvidia-container-toolkit` installed, `nvidia` runtime registered in Docker
- Docker 23+ (the Dockerfile uses `RUN --mount=type=bind`, which requires BuildKit — default in 23+)
- ~60 GB free disk under Docker's storage root (final image is ~64 GB)
- Port 6363 free on the host
```
with:
```markdown
- Docker 23+ (the Dockerfile uses `RUN --mount=type=bind`, which requires BuildKit — default in 23+)
- ~50 GB free disk under Docker's storage root (final image size printed at the end of `./scripts/build.sh`)
- Port 6363 free on the host

CPU-only — no GPU, NVIDIA driver, or nvidia-container-toolkit required. Runs on any x86-64 Linux host with Docker.
```

(c) Remove the GPU troubleshooting bullet:
```markdown
- GPU visible to the app: `docker exec predpep_app nvidia-smi` (if that fails, the nvidia runtime isn't configured — check `docker info | grep Runtimes`)
```

- [ ] **Step 5: Commit**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
grep -rn "gpus" scripts/ docker-compose.yml || echo "no --gpus remaining"
git add scripts/run.sh scripts/run-dev.sh docker-compose.yml README.md
git commit -q -m "deploy: CPU-only run configs and README (no GPU)"
```
Expected: `no --gpus remaining`.

---

### Task 5: Regenerate the blob manifest

**Files:** Modify `blobs/blobs.sha256`

- [ ] **Step 1: Manifest for the 3 used blobs only**

Run (~30–60 s; rosetta is 22 GB):
```bash
cd /home/david/DATA/OFFLINE/predpep_local/blobs
sha256sum foldx.tar.gz miniforge3.tar.gz rosetta.tar.gz > blobs.sha256
cat blobs.sha256
cd ..
```
Expected: exactly 3 lines (no tmap/ogdf).

- [ ] **Step 2: Commit**

Run:
```bash
git add blobs/blobs.sha256
git commit -q -m "chore: drop tmap/ogdf from blob manifest (no longer extracted)"
```

---

### Task 6: Build the slim image + headless boot smoke

**Files:** none (build + verify)

- [ ] **Step 1: Build**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
./scripts/build.sh 2>&1 | tee build.log | tail -n 20
```
Expected: ends with `naming to docker.io/library/predpep:local … done`, no `ERROR`. (The blob-extraction layer re-runs because the base image and the extraction command changed — expected, ~5 min.)

- [ ] **Step 2: Record before/after sizes**

Run:
```bash
echo "CUDA  (phase1): $(docker image inspect predpep:phase1-cuda --format '{{.Size}}') bytes"
echo "slim  (2A):     $(docker image inspect predpep:local       --format '{{.Size}}') bytes"
```
Expected: slim is meaningfully smaller (~7 GB less). Use the slim number to update README's size note if desired.

- [ ] **Step 3: Launch throwaway, check headless boot**

Run:
```bash
docker rm -f predpep_smoke 2>/dev/null || true
docker run -d --name predpep_smoke -p 6364:6363 predpep:local
curl -fsS --retry 60 --retry-delay 1 --retry-connrefused http://localhost:6364/health
echo
docker logs predpep_smoke 2>&1 | grep -iE 'Booting worker|Listening at|Traceback' | tail -n 3
```
Expected: `{"service":"predpep-node","status":"ok"}`; a gunicorn `Booting worker` line; no `Traceback`.

- [ ] **Step 4: Toolchain runs on the ubuntu base (CPU, no GPU)**

Run:
```bash
docker exec predpep_smoke bash -lc 'pepspec.static.linuxgccrelease -help 2>&1 | head -n 5; echo "rosetta exit: ${PIPESTATUS[0]}"'
docker exec predpep_smoke bash -lc 'ls -l /usr/local/bin/foldx_20270131; /usr/local/bin/foldx_20270131 --version 2>&1 | head -n 3 || true'
docker exec predpep_smoke /home/spacepep/miniforge3/envs/predPEP/bin/python -c "import flask, numpy; print('env OK')"
```
Expected: the Rosetta binary loads and prints usage/option text (it links and runs on ubuntu:22.04 — that's the point of the check); FoldX binary is present and executes; `env OK`. If the Rosetta binary fails with a missing shared library, STOP — that's the base-swap risk; investigate before proceeding (see Rollback).

---

### Task 7: Full end-to-end job (gold-standard verification)

**Files:** none (verify)

- [ ] **Step 1: Submit the real job to the throwaway container**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
RESP=$(curl -fsS -F protein_symbol=EGF -F user_name=test -F cpus=2 -F file1=@testdata/SPEGFH.pdb http://localhost:6364/upload)
echo "$RESP"
JOB=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "JOB=$JOB" | tee /tmp/phase2a_job.txt
```
Expected: JSON `{"success": true, ..., "job_id": "SPEGFT_xxxxxxxx"}`; `JOB` captured.

- [ ] **Step 2: Poll status to completion** (a full FlexPepDock run is several Rosetta+FoldX iterations — may take many minutes; run the poll detached and check back)

Run:
```bash
JOB=$(cut -d= -f2 /tmp/phase2a_job.txt)
for i in $(seq 1 240); do
  S=$(curl -fsS "http://localhost:6364/status/$JOB")
  echo "[$i] $S"
  echo "$S" | grep -q '"status": "Complete"' && break
  curl -fsS --max-time 2 "http://localhost:6364/health" >/dev/null   # cheap idle between polls
done
echo "$S" | grep -q '"status": "Complete"' && echo "JOB COMPLETE" || echo "still running / not complete"
```
Expected (eventually): `{"status": "Complete", "download_url": "/download/SPEGFT_xxxx/SPEGFT_xxxx.zip"}` → `JOB COMPLETE`. If it errors or `Pending/Failed`, inspect `docker exec predpep_smoke cat /tmp/pepspec/results/$JOB/${JOB}_manager_stderr.log` and the iterative manager log, and debug before cutover.

- [ ] **Step 3: Confirm the result zip exists and is non-trivial**

Run:
```bash
JOB=$(cut -d= -f2 /tmp/phase2a_job.txt)
docker exec predpep_smoke bash -lc "ls -l /tmp/pepspec/results/$JOB/$JOB.zip && unzip -l /tmp/pepspec/results/$JOB/$JOB.zip | tail -n 3"
```
Expected: the `.zip` exists with a sensible size and a populated file listing → the slim CPU-only image **computes correctly**.

---

### Task 8: Merge to main + cut over `predpep_app`

**Files:** none (git + docker)

- [ ] **Step 1: Fast-forward merge to main**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main
git merge --ff-only phase2a/headless-slim
git log --oneline | head -8
test -z "$(git status --porcelain --untracked-files=all)" && echo "CLEAN" || git status --short
```
Expected: ff-merge; `CLEAN` working tree (testdata/ and blobs ignored).

- [ ] **Step 2: Cut over — replace the running container with the slim image**

Run:
```bash
docker rm -f predpep_smoke
docker rm -f predpep_app
./scripts/run.sh
curl -fsS --retry 60 --retry-delay 1 --retry-connrefused http://localhost:6363/health && echo "  <- new predpep_app healthy"
docker ps --filter name=predpep_app --format '{{.ID}}  {{.Image}}  {{.Status}}'
```
Expected: new `predpep_app` from `predpep:local` (slim) serving `/health` on 6363; `docker ps` shows it Up.

- [ ] **Step 3: Confirm rollback image retained**

Run:
```bash
docker images | grep -E 'predpep\s' | grep -E 'local|phase1-cuda'
```
Expected: both `predpep:local` (slim, new) and `predpep:phase1-cuda` (rollback) present.

> **Rollback (if the slim node misbehaves later):**
> `docker rm -f predpep_app && docker run -d --name predpep_app --gpus all -p 6363:6363 --restart unless-stopped predpep:phase1-cuda`

---

## Self-Review

**Spec coverage:** headless app (Task 1) ✓; remove frontend (Task 2) ✓; Dockerfile base/tmap/ogdf/ldconfig/PATH/HEALTHCHECK (Task 3) ✓; CPU-only run configs + README (Task 4) ✓; manifest regen (Task 5) ✓; build + headless + toolchain smoke (Task 6) ✓; full real job (Task 7) ✓; merge + cutover + rollback retained (Task 8) ✓; preserve test input + rollback tag (Task 0) ✓.

**Placeholder scan:** none — every edit shows exact old/new text; the README size note is an explicit "printed by build" instruction, not a blank.

**Type/name consistency:** container `predpep_smoke` (port 6364) for tests, `predpep_app` (6363) for cutover; images `predpep:local` (slim) and `predpep:phase1-cuda` (rollback); branch `phase2a/headless-slim`; health route `/health` matches HEALTHCHECK — all consistent across tasks.
