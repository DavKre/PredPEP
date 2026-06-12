# Phase 1 — Repo Restructure & Clean Baseline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the predPEP-node repo into a clean, version-controlled layout (`app/ pipeline/ scripts/ blobs/ docs/`) and add a `.gitignore`/integrity manifest — with byte-for-byte identical runtime — producing a committed rollback point before Phase 2.

**Architecture:** Pure filesystem reorganization plus path-reference updates. In-image paths (`/opt/sp-predPEP`, `/usr/local/pepspec_pipe`, conda/Rosetta/FoldX trees) are unchanged; only the Dockerfile's COPY *sources* and the helper scripts' host paths move. The "test" for each change is a successful Docker build plus a throwaway-container smoke check.

**Tech Stack:** Docker / BuildKit, bash, git. No application code logic changes.

---

## Hard constraints (repeat on every build/run step)

- **NEVER** `docker stop|restart|rm` the running `predpep_app` (port 6363, in active use). Never run `scripts/run.sh` (targets that name). Verification uses a **throwaway** container `predpep_smoke` on host port **6364**, removed at the end.
- Building the `predpep:local` tag does NOT affect the already-running container — safe.
- One deliberate extension of the approved spec: `.gitignore` also excludes the heavy vendored frontend libs `app/static/ngl-master/` and `app/static/molstar/` (~586 MB, removed in Phase 2). The build still works because Docker reads the working tree, not git.

## File map (what changes)

| Path (after) | Was | Change |
|---|---|---|
| `app/` | `sp-predPEP/` | moved |
| `pipeline/` | `pepspec_pipe/` | moved |
| `scripts/{build,run,run-dev}.sh` | root | moved + edited |
| `.gitignore` | — | created |
| `blobs/blobs.sha256` | — | created (tracked) |
| `Dockerfile` | `Dockerfile` | 2 COPY lines repathed |
| `.dockerignore` | `.dockerignore` | repathed + cruft excludes |
| `README.md`, `CHANGES.md`, `HANDOFF.md` | same | path updates / notes |
| `usr_local_bin/`, `*_bak*`, `old_scripts/`, `*.py3` | — | gitignored + .dockerignored, left on disk |

---

### Task 0: Pre-flight — branch and record the live container

**Files:** none (git + docker state only)

- [ ] **Step 1: Confirm the live container and record its ID (must stay identical at the end)**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
docker ps --filter name=predpep_app --format '{{.ID}}  {{.Status}}  {{.Ports}}' | tee /tmp/predpep_app_before.txt
```
Expected: one line showing `predpep_app` `Up …` on `0.0.0.0:6363->6363/tcp`. Save it; we re-check at the end.

- [ ] **Step 2: Create the feature branch off `main`**

Run:
```bash
git checkout -b phase1/restructure
git status -sb | head -1
```
Expected: `## phase1/restructure`. (Repo already has the design-doc commit `22fc83f` on `main`.)

---

### Task 1: Add `.gitignore` + `blobs.sha256`, commit the safety baseline

**Files:**
- Create: `.gitignore`
- Create: `blobs/blobs.sha256`

- [ ] **Step 1: Create `.gitignore`**

Create `/home/david/DATA/OFFLINE/predpep_local/.gitignore` with exactly:
```gitignore
# --- Heavy, out-of-band assets (provided alongside the repo, see HANDOFF.md) ---
# Extracted tool blobs (~23 GB)
blobs/*.tar.gz
# Vendored third-party frontend libraries (~586 MB) — removed entirely in Phase 2
app/static/ngl-master/
app/static/molstar/

# --- Build / runtime artifacts ---
*.log

# --- Python ---
__pycache__/
*.py[cod]

# --- Superseded / reference cruft (kept on disk, out of the clean repo) ---
usr_local_bin/
old_scripts/
**/*_bak*
**/*.py3
```

- [ ] **Step 2: Generate the blob checksum manifest** (hashes ~23 GB — expect ~30–60 s)

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local/blobs
sha256sum *.tar.gz > blobs.sha256
cd ..
cat blobs/blobs.sha256
```
Expected: five lines, one per `*.tar.gz` (foldx, miniforge3, ogdf, rosetta, tmap).

- [ ] **Step 3: Verify the ignore rules actually work (no heavy files leak)**

Run:
```bash
git check-ignore blobs/rosetta.tar.gz app/static/ngl-master usr_local_bin || echo "MISSING IGNORE"
git status --porcelain | grep -E 'tar\.gz|ngl-master|molstar' && echo "LEAK!" || echo "clean: no heavy files visible to git"
```
Expected: the three paths echo back as ignored; final line prints `clean: no heavy files visible to git`.

- [ ] **Step 4: Stage ONLY the two new files and confirm**

Run:
```bash
git add .gitignore blobs/blobs.sha256
git diff --cached --name-only
```
Expected: exactly `.gitignore` and `blobs/blobs.sha256` — nothing else.

- [ ] **Step 5: Commit**

Run:
```bash
git commit -q -m "chore: add .gitignore and blobs.sha256 integrity manifest

Excludes the ~23 GB of blobs, the ~586 MB vendored frontend libs (removed in
Phase 2), and build/cache cruft from version control. Adds a tracked checksum
manifest for the out-of-band blob tarballs."
git log --oneline -1
```
Expected: new commit shown as HEAD.

---

### Task 2: Move directories into the new layout

**Files:** moves only (all untracked, so plain `mv`).

- [ ] **Step 1: Move the app, pipeline, and scripts**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
mv sp-predPEP app
mv pepspec_pipe pipeline
mkdir -p scripts
mv build.sh run.sh run-dev.sh scripts/
ls -d app pipeline scripts && ls scripts
```
Expected: `app pipeline scripts` exist; `scripts/` lists `build.sh run.sh run-dev.sh`.

- [ ] **Step 2: Confirm old names are gone and key files landed**

Run:
```bash
test ! -e sp-predPEP && test ! -e pepspec_pipe && test -f app/predPEP.py && test -f pipeline/run_iteMAN.py && echo OK
```
Expected: `OK`.

---

### Task 3: Update path references (Dockerfile, scripts, .dockerignore)

**Files:**
- Modify: `Dockerfile`
- Modify: `scripts/build.sh`, `scripts/run-dev.sh`
- Modify: `.dockerignore`

- [ ] **Step 1: Dockerfile — repath the two COPY sources (targets unchanged)**

In `Dockerfile`, change:
```dockerfile
COPY pepspec_pipe/ /usr/local/pepspec_pipe/
```
to:
```dockerfile
COPY pipeline/ /usr/local/pepspec_pipe/
```
and change:
```dockerfile
COPY --chown=${USER_UID}:${USER_GID} sp-predPEP/ /opt/sp-predPEP/
```
to:
```dockerfile
COPY --chown=${USER_UID}:${USER_GID} app/ /opt/sp-predPEP/
```

- [ ] **Step 2: `scripts/build.sh` — fix context dir, add opt-in blob check**

Replace the body of `scripts/build.sh` (keep the shebang + comments) so the post-comment section reads:
```bash
set -euo pipefail

# Build context is the repo root (one level up from scripts/).
cd "$(dirname "$0")/.."

# Opt-in blob integrity check: CHECK_BLOBS=1 ./scripts/build.sh
if [ "${CHECK_BLOBS:-0}" = "1" ] && [ -f blobs/blobs.sha256 ]; then
  echo "Verifying blob checksums (CHECK_BLOBS=1)…"
  ( cd blobs && sha256sum -c blobs.sha256 ) || { echo "ERROR: blob checksum mismatch." >&2; exit 1; }
fi

DOCKER_BUILDKIT=1 docker build \
  --progress=plain \
  -t predpep:local \
  .
```

- [ ] **Step 3: `scripts/run-dev.sh` — fix context dir and bind-mount paths**

In `scripts/run-dev.sh`, change:
```bash
cd "$(dirname "$0")"
ROOT="$(pwd)"
```
to:
```bash
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
```
and change the two bind mounts:
```bash
  -v "${ROOT}/sp-predPEP:/opt/sp-predPEP" \
  -v "${ROOT}/pepspec_pipe:/usr/local/pepspec_pipe" \
```
to:
```bash
  -v "${ROOT}/app:/opt/sp-predPEP" \
  -v "${ROOT}/pipeline:/usr/local/pepspec_pipe" \
```
(`scripts/run.sh` has no path references — no change needed beyond its new location. Do NOT run it.)

- [ ] **Step 4: `.dockerignore` — repath and add cruft excludes**

Replace the whole `.dockerignore` with:
```dockerignore
# Reference-only folders and docs — never needed inside the image.
*.md
HANDOFF
docs
usr_local_bin
blobs/blobs.sha256

# Superseded / backup cruft — keep it out of the image.
app/old_scripts
**/*_bak*
**/*.py3

# Python / editor junk that may reappear.
**/__pycache__
**/*.pyc
**/*.pyo
**/.DS_Store

# VCS.
.git
.gitignore

# The build tooling itself — Docker doesn't need these in the context.
scripts/
docker-compose.yml
README.md
```

- [ ] **Step 5: Sanity-check the bind source still resolves**

Run:
```bash
grep -n "source=./blobs" Dockerfile
grep -nE "COPY (pipeline|app)/" Dockerfile
```
Expected: the blob bind line is still present (unchanged); the two COPY lines now read `pipeline/` and `app/`.

---

### Task 4: Build and smoke-verify (the test) — never touching `predpep_app`

**Files:** none (verification only)

- [ ] **Step 1: Build the image** (blob-extraction layer stays CACHED; only cheap final layers rebuild)

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
./scripts/build.sh 2>&1 | tee build.log | tail -n 25
```
Expected: ends with `naming to docker.io/library/predpep:local … done`. The `RUN --mount=…/blobs` extraction line shows `CACHED`. No `ERROR`.

- [ ] **Step 2: Launch a throwaway container on port 6364, no GPU**

Run:
```bash
docker rm -f predpep_smoke 2>/dev/null || true
docker run -d --name predpep_smoke -p 6364:6363 predpep:local
```
Expected: prints a container ID. (No `--gpus`; Flask boot + `GET /` need no GPU. Port 6364, NOT 6363.)

- [ ] **Step 3: Wait for gunicorn and verify it serves** (curl retries instead of sleeping)

Run:
```bash
curl -fsS --retry 30 --retry-delay 1 --retry-connrefused http://localhost:6364/ \
  | grep -qiE '<html|<!doctype' && echo "SERVE OK"
docker logs predpep_smoke 2>&1 | grep -iE 'Booting worker|Listening at' | tail -n 3
```
Expected: `SERVE OK`, and a gunicorn `Booting worker with pid …` / `Listening at: http://0.0.0.0:6363` line. No Python `Traceback`.

- [ ] **Step 4: Verify in-image integrity (symlink + conda env import)**

Run:
```bash
docker exec predpep_smoke ls -l /usr/local/bin/run_iteMAN.py
docker exec predpep_smoke /home/spacepep/miniforge3/envs/predPEP/bin/python -c "import flask, pandas; print('flask', flask.__version__)"
```
Expected: the symlink resolves to `/usr/local/pepspec_pipe/run_iteMAN.py`; the python line prints `flask <version>` with no ImportError.

- [ ] **Step 5: Tear down ONLY the throwaway**

Run:
```bash
docker rm -f predpep_smoke
docker ps --filter name=predpep_app --format '{{.ID}}  {{.Status}}'
```
Expected: `predpep_smoke` removed; `predpep_app` still `Up` with the SAME ID as `/tmp/predpep_app_before.txt`.

---

### Task 5: Commit the restructure

**Files:** none new (stage moved/edited files)

- [ ] **Step 1: Stage intended paths and confirm no heavy files**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git add app pipeline scripts Dockerfile .dockerignore docker-compose.yml
git status --porcelain | grep -E 'tar\.gz|ngl-master|molstar|build\.log' && echo "LEAK!" || echo "clean"
git diff --cached --stat | tail -n 5
```
Expected: prints `clean` (no heavy files / build.log staged); diffstat shows app/, pipeline/, scripts/, Dockerfile, .dockerignore.

- [ ] **Step 2: Commit**

Run:
```bash
git commit -q -m "refactor: restructure repo into app/ pipeline/ scripts/

Behavior-identical reorg: sp-predPEP/->app/, pepspec_pipe/->pipeline/, helper
scripts into scripts/. Dockerfile COPY sources repathed (in-image targets
unchanged); build.sh/run-dev.sh updated for the new host paths and context;
.dockerignore repathed with cruft excludes. Verified: image builds, throwaway
container boots gunicorn and serves / on 6364."
git log --oneline -3
```
Expected: restructure commit at HEAD.

---

### Task 6: Update docs

**Files:**
- Modify: `README.md`, `CHANGES.md`, `HANDOFF.md`

- [ ] **Step 1: `README.md` — update command and directory paths**

Apply these exact replacements:
- `./build.sh` → `./scripts/build.sh` (the standalone first-time-build block)
- `./run.sh` → `./scripts/run.sh` (daily-use block)
- `./run-dev.sh` → `./scripts/run-dev.sh` (code-iteration block)
- `./build.sh && docker rm -f predpep_app && ./run.sh` → `./scripts/build.sh && docker rm -f predpep_app && ./scripts/run.sh`
- In the dev section text, `` `sp-predPEP/` and `pepspec_pipe/` bind-mounted from the host `` → `` `app/` and `pipeline/` bind-mounted from the host ``

- [ ] **Step 2: `CHANGES.md` — append a Phase 1 entry at the end**

Append:
```markdown

## 2026-06-12 — Phase 1: repo restructure & clean baseline

Behavior-identical reorganization ahead of the headless-service work. Directories
renamed for clarity (`sp-predPEP/`→`app/`, `pepspec_pipe/`→`pipeline/`, helper scripts
into `scripts/`); in-image paths unchanged so runtime is identical. Added `.gitignore`
(keeps the ~23 GB `blobs/`, the ~586 MB vendored frontend libs, and build/cache
artifacts out of git) and a tracked `blobs/blobs.sha256` integrity manifest
(`CHECK_BLOBS=1 ./scripts/build.sh` verifies it). Superseded cruft (`*_bak*`,
`old_scripts/`, `usr_local_bin/`, `*.py3`) is gitignored and `.dockerignore`d rather
than deleted. Repo brought under version control with a clean initial history.
```

- [ ] **Step 3: `HANDOFF.md` — add a restructure banner under the title**

Insert immediately after the first line (`# predPEP Local Deployment — Handoff`):
```markdown

> **Note (2026-06-12):** the repository was restructured — `sp-predPEP/`→`app/`,
> `pepspec_pipe/`→`pipeline/`, and the helper scripts moved into `scripts/`. In-image
> paths are unchanged. This document is kept as a historical record of the extraction;
> see `README.md` for current paths and commands.
```

- [ ] **Step 4: Commit the doc updates**

Run:
```bash
git add README.md CHANGES.md HANDOFF.md
git commit -q -m "docs: update paths for the restructured layout"
git log --oneline -4
```
Expected: doc commit at HEAD.

---

### Task 7: Merge to main and final safety check

**Files:** none

- [ ] **Step 1: Fast-forward merge into `main`**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main
git merge --ff-only phase1/restructure
git log --oneline
```
Expected: `main` now contains the design-doc, gitignore/manifest, restructure, and docs commits in order; fast-forward (no merge commit).

- [ ] **Step 2: Confirm the working tree is clean and the live container untouched**

Run:
```bash
git status -sb
docker ps --filter name=predpep_app --format '{{.ID}}  {{.Status}}'
diff <(docker ps --filter name=predpep_app --format '{{.ID}}') <(awk '{print $1}' /tmp/predpep_app_before.txt) && echo "predpep_app UNCHANGED"
```
Expected: only gitignored cruft/blobs remain untracked (no surprises); `predpep_app` ID and Up-status identical to pre-flight → `predpep_app UNCHANGED`.

---

### Task 8 (OPTIONAL — only on explicit user approval): physically delete cruft

**Files:** deletes `usr_local_bin/`, `app/old_scripts/`, `*_bak*`, `*.py3` from disk.

> Do NOT run without the user saying "yes, delete the cruft." These are gitignored
> (recoverable from nowhere — there is no git history of them), so deletion is permanent.

- [ ] **Step 1: Show exactly what would be deleted (dry run)**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
echo "usr_local_bin/ ($(du -sh usr_local_bin 2>/dev/null | cut -f1))"; \
find app pipeline -name '*_bak*' -o -name '*.py3'; \
ls -d app/old_scripts 2>/dev/null
```
Expected: lists `usr_local_bin/`, the `*_bak*` / `*.py3` files, and `app/old_scripts/`.

- [ ] **Step 2: Delete (only after explicit approval)**

Run:
```bash
rm -rf usr_local_bin app/old_scripts
find app pipeline \( -name '*_bak*' -o -name '*.py3' \) -delete
echo "cruft removed"
```
Expected: `cruft removed`. (No git commit needed — these were never tracked.)

---

## Self-Review

**Spec coverage:** target layout ✓ (Tasks 2–3), `.gitignore` ✓ (Task 1), `.dockerignore` ✓ (Task 3.4), `blobs.sha256` + build.sh check ✓ (Task 1, Task 3.2), Dockerfile remaps ✓ (Task 3.1), script edits ✓ (Task 3.2–3.3), doc edits ✓ (Task 6), throwaway-container verification on 6364 ✓ (Task 4), git strategy branch→ff-merge ✓ (Tasks 0,5,6,7), non-destructive cruft ✓ (Task 1 ignore + optional Task 8), never-touch-`predpep_app` ✓ (constraints + Tasks 0/4/7 checks). One intentional spec extension (vendored frontend libs gitignored) is documented in constraints.

**Placeholder scan:** none — every step has concrete commands/content.

**Type/name consistency:** container name `predpep_smoke` and port `6364` used consistently; image tag `predpep:local`; branch `phase1/restructure` consistent across Tasks 0/5/6/7.
