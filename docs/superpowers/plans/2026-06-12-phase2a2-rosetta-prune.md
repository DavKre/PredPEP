# Phase 2A.2 — Rosetta Runtime Prune — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the image from ~52 GB to ~7–8 GB by pruning the Rosetta tree to only what the pipeline runs (`database` + the one static `pepspec` binary + `protein_tools/scripts`) — entirely at image-build time, with no local files deleted.

**Architecture:** Add a prune step to the existing Dockerfile blob-extraction `RUN`, so the 49 GB Rosetta tree is reduced to ~3.5 GB before that layer is committed (the 49 GB never persists, and `blobs/rosetta.tar.gz` + the local extracted tree are untouched). Verify with a real `SPEGFH.pdb` job (iteration 1), then cut `predpep_app` over, keeping `predpep:phase2a-slim` as rollback.

**Tech Stack:** Docker/BuildKit, bash/sh, Rosetta `pepspec.static.linuxgccrelease`, git, curl.

---

## Constraints

- **No local deletion** — `blobs/rosetta.tar.gz` and the local extracted Rosetta tree stay intact; prune is inside the Dockerfile `RUN` only.
- `predpep_app` cutover authorized; keep `predpep:phase2a-slim` (current 52 GB) as rollback.
- `pipeline/` scripts unchanged; in-image paths unchanged.

## Keep-list (verified)

- `main/database/` (3.3 GB, whole)
- `main/source/build/src/release/linux/5.4/64/x86/gcc/7/static/pepspec.static.linuxgccrelease` (184 MB)
- `main/source/bin/` (symlinks)
- `main/tools/protein_tools/` (`clean_pdb.py` + `amino_acids.py`)

---

### Task 0: Pre-flight — branch, rollback tag, confirm test input

- [ ] **Step 1: Branch off main**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git checkout -b phase2a2/rosetta-prune
git status -sb | head -1
```
Expected: `## phase2a2/rosetta-prune`.

- [ ] **Step 2: Tag the current slim image as rollback + confirm test PDB**

Run:
```bash
docker tag predpep:local predpep:phase2a-slim
docker image inspect predpep:phase2a-slim --format 'rollback (2A slim) size: {{.Size}} bytes'
ls -l testdata/SPEGFH.pdb
```
Expected: prints the 52 GB rollback size; `testdata/SPEGFH.pdb` exists (~57 KB). If it's missing, re-copy: `mkdir -p testdata && docker cp "predpep_app:$(docker exec predpep_app bash -lc 'ls /tmp/pepspec/uploads/SPEGFH_*/SPEGFH.pdb | head -1')" testdata/SPEGFH.pdb`.

---

### Task 1: Add the prune to the Dockerfile extraction RUN

**Files:** Modify `Dockerfile`

- [ ] **Step 1: Replace the extraction RUN with extract-then-prune**

Change:
```dockerfile
RUN --mount=type=bind,source=./blobs,target=/tmp/blobs,readonly \
    tar -xzf /tmp/blobs/rosetta.tar.gz    -C /usr/local/ \
 && tar -xzf /tmp/blobs/foldx.tar.gz      -C /usr/local/ \
 && tar -xzf /tmp/blobs/miniforge3.tar.gz -C /home/${USER_NAME}/ \
 && chown -R ${USER_UID}:${USER_GID} /home/${USER_NAME}/miniforge3
```
to:
```dockerfile
RUN --mount=type=bind,source=./blobs,target=/tmp/blobs,readonly \
    tar -xzf /tmp/blobs/rosetta.tar.gz    -C /usr/local/ \
 && tar -xzf /tmp/blobs/foldx.tar.gz      -C /usr/local/ \
 && tar -xzf /tmp/blobs/miniforge3.tar.gz -C /home/${USER_NAME}/ \
 && chown -R ${USER_UID}:${USER_GID} /home/${USER_NAME}/miniforge3 \
 && R=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408 \
 && BINREL=main/source/build/src/release/linux/5.4/64/x86/gcc/7/static/pepspec.static.linuxgccrelease \
 && mv "$R/$BINREL" /tmp/pepspec.bin \
 && rm -rf "$R/main/source/build" \
 && mkdir -p "$(dirname "$R/$BINREL")" \
 && mv /tmp/pepspec.bin "$R/$BINREL" \
 && find "$R/main/source" -mindepth 1 -maxdepth 1 ! -name bin ! -name build -exec rm -rf {} + \
 && find "$R/main/tools"  -mindepth 1 -maxdepth 1 ! -name protein_tools -exec rm -rf {} + \
 && find "$R/main"        -mindepth 1 -maxdepth 1 ! -name database ! -name source ! -name tools -exec rm -rf {} +
```
(This RUN runs under `/bin/sh` — the `SHELL ["/bin/bash"...]` directive comes later in the file. The syntax above is POSIX-sh compatible.)

- [ ] **Step 2: Add a comment above the RUN explaining the prune**

Immediately above `# ---- 3. Pre-built tool blobs` keep that header, and change the block comment:
```dockerfile
# ---- 3. Pre-built tool blobs ------------------------------------------------
# Bind-mounted (not COPY'd) so the 23 GB of tarballs never enter any image
# layer. Extraction + ownership fix happen in a single RUN so there's one
# layer for all tools.
```
to:
```dockerfile
# ---- 3. Pre-built tool blobs ------------------------------------------------
# Bind-mounted (not COPY'd) so the 23 GB of tarballs never enter any image
# layer. Extraction + ownership fix + Rosetta prune happen in a single RUN so
# there's one layer for all tools AND the 49 GB Rosetta tree never persists:
# the pipeline only runs pepspec.static.linuxgccrelease (a 184 MB static binary)
# with main/database, so we keep those + protein_tools/scripts and drop the rest
# (~45 GB of other apps/variants/source/tests). See
# docs/superpowers/specs/2026-06-12-phase2a2-rosetta-prune-design.md.
```

- [ ] **Step 3: Commit**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git add Dockerfile
git commit -q -m "build: prune Rosetta to database + the one static pepspec binary

Reduces the in-image Rosetta tree ~49 GB -> ~3.5 GB inside the extraction RUN
(blob + local tree untouched). Keeps main/database, the resolved static pepspec
binary + bin/ symlinks, and protein_tools/scripts."
```

---

### Task 2: Update README disk note

**Files:** Modify `README.md`

- [ ] **Step 1: Clarify build-vs-image-vs-deploy disk**

Change:
```markdown
- ~50 GB free disk under Docker's storage root (final image size is printed at the end of `./scripts/build.sh`)
```
to:
```markdown
- Building needs ~50 GB free transiently (Rosetta is extracted in full, then pruned in the same layer); the **resulting image is ~8 GB**, so machines that only *run* a pre-built image need ~10 GB. Final size is printed at the end of `./scripts/build.sh`.
```

- [ ] **Step 2: Commit**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git add README.md
git commit -q -m "docs: clarify build-transient vs final-image disk after Rosetta prune"
```

---

### Task 3: Build the pruned image

**Files:** none (build)

- [ ] **Step 1: Build** (Rosetta extracts in full ~5 min, then prune drops it)

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
./scripts/build.sh 2>&1 | tee build.log | tail -n 20
```
Expected: ends with `naming to docker.io/library/predpep:local … done`, no `ERROR`. The extraction RUN now also runs the prune `find`/`rm` commands.

- [ ] **Step 2: Record the size drop**

Run:
```bash
P=$(docker image inspect predpep:phase2a-slim --format '{{.Size}}')
N=$(docker image inspect predpep:local        --format '{{.Size}}')
echo "2A slim : $P bytes ($(numfmt --to=iec $P))"
echo "pruned  : $N bytes ($(numfmt --to=iec $N))"
echo "saved   : $((P-N)) bytes ($(numfmt --to=iec $((P-N))))"
```
Expected: pruned image ~7–8 GB; saved ~44–45 GB.

---

### Task 4: Smoke — headless boot + Rosetta keep-list intact

**Files:** none (verify)

- [ ] **Step 1: Launch throwaway on 6364**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
docker rm -f predpep_smoke 2>/dev/null || true
docker run -d --name predpep_smoke -p 6364:6363 predpep:local
curl -fsS --retry 60 --retry-delay 1 --retry-connrefused http://localhost:6364/health; echo
```
Expected: `{"service":"predpep-node","status":"ok"}`.

- [ ] **Step 2: Confirm the kept Rosetta pieces exist and the binary still loads**

Run:
```bash
docker exec predpep_smoke bash -lc '
R=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408
echo "rosetta tree size: $(du -sh $R | cut -f1)"
echo "database present : $(test -d $R/main/database && du -sh $R/main/database | cut -f1 || echo MISSING)"
echo "binary present   : $(ls -lL $R/main/source/bin/pepspec.static.linuxgccrelease | awk "{print \$5}")"
echo "clean_pdb.py     : $(test -f $R/main/tools/protein_tools/scripts/clean_pdb.py && echo yes || echo MISSING)"
pepspec.static.linuxgccrelease -help 2>&1 | head -n 2; echo "pepspec exit: ${PIPESTATUS[0]}"
'
```
Expected: rosetta tree ~3.5 GB; database ~3.3 GB; binary ~192 MB; `clean_pdb.py` yes; `pepspec` prints `Usage:`-style text (loads). If `database MISSING` or the binary fails to load → STOP, the prune removed something needed.

---

### Task 5: Full-job iteration-1 verification

**Files:** none (verify)

- [ ] **Step 1: Submit the real job**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
RESP=$(curl -fsS -F protein_symbol=EGF -F user_name=test -F cpus=2 -F file1=@testdata/SPEGFH.pdb http://localhost:6364/upload)
echo "$RESP"
JOB=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
echo "JOB=$JOB" | tee /tmp/phase2a2_job.txt
```
Expected: `{"success": true, …, "job_id": "SPEGFT_xxxxxxxx"}`.

- [ ] **Step 2: Confirm pepspec actually runs (no missing-file error in the first minute)**

Run (after ~60–120 s):
```bash
JOB=$(cut -d= -f2 /tmp/phase2a2_job.txt)
docker exec predpep_smoke bash -lc "
ls -d /tmp/pepspec/results/$JOB/*_iter1_* 2>/dev/null;
echo '--- pepspec log tail ---';
tail -n 8 /tmp/pepspec/results/$JOB/*_iter1_*/*.log 2>/dev/null;
echo '--- any error? ---';
grep -aiE 'error|cannot|no such file|caught exception|ERROR' /tmp/pepspec/results/$JOB/*_iter1_*/*.log 2>/dev/null | head"
```
Expected: an `_iter1_` dir exists, the pepspec `.log` shows Rosetta startup/run output, and **no** "no such file"/missing-database errors. A startup error here means the prune was too aggressive → STOP, rollback (`predpep:phase2a-slim`), investigate.

- [ ] **Step 3: Monitor until iteration 1 completes (iter2 dir appears)** — backgrounded; it re-invokes on completion/failure

Run (background):
```bash
cd /home/david/DATA/OFFLINE/predpep_local
JOB=$(cut -d= -f2 /tmp/phase2a2_job.txt)
for i in $(seq 1 90); do
  if docker exec predpep_smoke bash -lc "ls -d /tmp/pepspec/results/$JOB/*_iter2_* >/dev/null 2>&1"; then
    echo "ITER1 COMPLETE (iter2 dir appeared) after ~${i} min — pruned Rosetta computes end-to-end through selection"; exit 0
  fi
  ALIVE=$(docker exec predpep_smoke bash -lc "pgrep -f run_iteMAN >/dev/null && echo yes || echo NO")
  echo "[$(date +%H:%M:%S)] #$i iter1 running, manager=$ALIVE"
  [ "$ALIVE" = "NO" ] && { echo "MANAGER EXITED before iter2 — investigate"; exit 2; }
  sleep 60
done
echo "timed out waiting for iter1"; exit 3
```
Expected: `ITER1 COMPLETE …` (clean_pdb → pepspec → FoldX → aggregation/selection all worked on the pruned image).

---

### Task 6: Merge to main + cut over

**Files:** none (git + docker)

- [ ] **Step 1: Merge**

Run:
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main
git merge --ff-only phase2a2/rosetta-prune
git log --oneline | head -5
test -z "$(git status --porcelain --untracked-files=all)" && echo CLEAN || git status --short
```
Expected: ff-merge; `CLEAN`.

- [ ] **Step 2: Cut over predpep_app to the pruned image**

Run:
```bash
docker rm -f predpep_smoke
docker rm -f predpep_app
./scripts/run.sh
curl -fsS --retry 60 --retry-delay 1 --retry-connrefused http://localhost:6363/health && echo "  <- pruned predpep_app healthy"
docker ps --filter name=predpep_app --format '{{.Names}}  {{.Image}}  {{.Status}}'
docker inspect --format '{{.State.Health.Status}}' predpep_app
```
Expected: new `predpep_app` from pruned `predpep:local`, `/health` ok, health `healthy`.

- [ ] **Step 3: Confirm rollback chain retained**

Run:
```bash
docker images predpep --format '{{.Repository}}:{{.Tag}}  {{.Size}}'
```
Expected: `predpep:local` (~8 GB, pruned), `predpep:phase2a-slim` (~52 GB), `predpep:phase1-cuda` (~64 GB).

> **Rollback (if the pruned node misbehaves):**
> `docker rm -f predpep_app && docker tag predpep:phase2a-slim predpep:local && ./scripts/run.sh`

---

## Self-Review

**Spec coverage:** build-time prune in extraction RUN (Task 1) ✓; no local deletion — only the Dockerfile `RUN` touches Rosetta, blob/local tree untouched (Task 1) ✓; keep-list database+binary+bin+protein_tools (Task 1 find filters) ✓; size ~7–8 GB recorded (Task 3) ✓; rollback `predpep:phase2a-slim` (Task 0/6) ✓; iteration-1 verification via iter2-dir signal + early error check (Tasks 4–5) ✓; cutover (Task 6) ✓; README disk note (Task 2) ✓.

**Placeholder scan:** none — exact prune commands, exact find filters, exact verification commands. README "~8 GB" is the spec's estimate; Task 3 prints the real number.

**Type/name consistency:** branch `phase2a2/rosetta-prune`; images `predpep:local` (pruned), `predpep:phase2a-slim` (rollback), `predpep:phase1-cuda` (older rollback); container `predpep_smoke` (6364) for test, `predpep_app` (6363) for cutover; `$R`/`$BINREL` used consistently in the prune. The kept binary path in Task 1, Task 4, and the spec match exactly.
