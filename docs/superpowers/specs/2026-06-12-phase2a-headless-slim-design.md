# Phase 2A — Headless, CPU-only Slim Image (Design)

**Date:** 2026-06-12
**Status:** Approved (design); pending spec review
**Scope:** First sub-project of Phase 2. Make the deployed image headless and CPU-only,
drop dead weight, verify it still computes with a real job, then cut over the running
`predpep_app` to the slim image.

## Context

Phase 1 produced a clean, version-controlled repo (`app/ pipeline/ scripts/ blobs/ docs/`)
with the image building and running identically to production. Phase 2 turns this into a
deployable backend compute node for DDN, split into 2A (this spec — headless slim image)
and 2B (control API + CPU-aware queue, separate spec later).

**Key findings driving 2A:**
- The GPU is **vestigial**: the frozen conda env has no torch/tensorflow/cupy/cuda
  packages, nothing in `pipeline/` or `app/` references GPU/cuda/nvidia, and the Rosetta
  binary the pipeline runs is `pepspec.static.linuxgccrelease` (statically-linked CPU).
  FoldX is CPU. → drop the CUDA base and the GPU requirement entirely.
- TMAP is dead: `app/tmap_utils.py` needs `mhfp` (absent from the env), so it already
  ImportError-fallbacks to a no-op; no pipeline script touches tmap/ogdf. → remove it and
  the `tmap`/`ogdf` blobs.
- The browser UI (`templates/`, `static/` ≈ 586 MB) is not needed by a headless node, and
  `index.html` even loads NGL from a CDN. → remove it.

## Authorization & constraints

- **The user authorized stopping/replacing `predpep_app`** (2026-06-12) — it is no longer in
  use. 2A is a real cutover. (Earlier "never touch" rule is lifted for this work; re-confirm
  in future sessions before disrupting any running container.)
- In-image paths stay fixed: `/opt/sp-predPEP`, `/usr/local/pepspec_pipe`,
  `/home/spacepep/miniforge3/...`, `/usr/local/rosetta_pkgs/...`, `/usr/local/foldx26Linux64_0/...`.
- Pipeline scripts in `pipeline/` are NOT modified (verbatim from production).
- Keep a rollback image (`predpep:phase1-cuda`) before building the slim one.

## Changes

### 1. App → headless (`app/predPEP.py`)
- Remove routes: `index()` (served `index.html`) and `get_tmap_tree()` (dead tmap).
- Remove now-unused imports: `pandas` (only used by `get_tmap_tree`), `render_template`,
  the `tmap_utils` try/except import block, and `secure_filename` if a grep confirms it's
  unused.
- Add a health route covering `/` and `/health`:
  ```python
  @predPEP.route('/')
  @predPEP.route('/health')
  def health():
      return jsonify({"service": "predpep-node", "status": "ok"})
  ```
- Keep unchanged: `upload`, `status` (`check_status`), `results_data` (`get_results_data`),
  `stream_final_pdb`, `download`. (Their redesign into the DDN control API is 2B.)

### 2. Remove the frontend
- `git rm -r app/templates app/static` then `rm -rf app/templates app/static` (clears the
  gitignored `ngl-master/`/`molstar/` left on disk).
- `git rm app/tmap_utils.py`.
- `.gitignore`: delete the now-moot `app/static/ngl-master/` and `app/static/molstar/` lines.

### 3. Dockerfile — slim + CPU-only
- Base: `FROM nvidia/cuda:12.4.0-devel-ubuntu22.04` → `FROM ubuntu:22.04`.
- Blob extraction RUN: delete the `tmap.tar.gz` and `ogdf.tar.gz` `tar -xzf` lines (keep
  rosetta, foldx, miniforge3 + the `chown`).
- Delete the OGDF `ld.so.conf.d/ogdf.conf` + `ldconfig` RUN (and its comment block).
- `ENV PATH`: drop the `/usr/local/nvidia/bin:/usr/local/cuda/bin:` segment (those dirs
  don't exist on the ubuntu base).
- `HEALTHCHECK`: `curl --fail http://localhost:6363/` → `http://localhost:6363/health`.
- apt list, user setup, pipeline COPY/symlinks, CMD: unchanged. (`COPY app/` now carries no
  `static/`/`templates/`, so the image is headless automatically.)

### 4. Runtime configs (CPU-only)
- `scripts/run.sh`, `scripts/run-dev.sh`: remove the `--gpus all \` line.
- `docker-compose.yml`: remove the `deploy.resources.reservations.devices` GPU block.
- `README.md`: drop NVIDIA GPU/driver/nvidia-container-toolkit prerequisites and the GPU
  troubleshooting/verification lines; note the image is now CPU-only and headless (JSON API
  on 6363, no browser UI); update the size estimate after the build measures it.

### 5. Blob manifest
- Regenerate for the 3 blobs still used:
  `cd blobs && sha256sum foldx.tar.gz miniforge3.tar.gz rosetta.tar.gz > blobs.sha256`.
  The `tmap.tar.gz`/`ogdf.tar.gz` tarballs stay on disk (gitignored archive), out of the manifest.

### 6. Test data (gitignored)
- `.gitignore`: add `testdata/`.

## Verification + cutover

All on the slim image; old `predpep_app` stays up (idle) until the test passes.

1. **Preserve a real input:** `docker cp predpep_app:/tmp/pepspec/uploads/SPEGFH_1ed56ddc/SPEGFH.pdb testdata/SPEGFH.pdb`.
2. **Rollback point:** `docker tag predpep:local predpep:phase1-cuda`.
3. **Build slim:** `./scripts/build.sh` → new `predpep:local`. Record the new image size.
4. **Throwaway on 6364 (no `--gpus`):** `docker run -d --name predpep_smoke -p 6364:6363 predpep:local`.
5. **Headless boot:** `/health` returns `{"status":"ok"}`; logs show gunicorn `Booting worker`,
   no Traceback.
6. **Toolchain runs on ubuntu base (CPU):** `docker exec predpep_smoke bash -lc 'pepspec.static.linuxgccrelease -help'`
   exits cleanly (binary loads); FoldX binary executes (`foldx_20270131 -h` or version).
7. **Full job (gold standard):** submit the real PDB via curl
   (`-F protein_symbol=EGF -F user_name=test -F cpus=2 -F file1=@testdata/SPEGFH.pdb`),
   capture `job_id`, poll `/status/<job_id>` until `Complete` with a `download_url`,
   confirm the result `.zip` exists. Run/monitor in the background (a full FlexPepDock run is
   several Rosetta+FoldX iterations and may take a while); report progress.
8. **Cut over (only after the job completes):** `git` merge to main first, then
   `docker rm -f predpep_app` and `./scripts/run.sh` (slim, no `--gpus`) → new `predpep_app`
   on 6363; confirm `/health`. Remove `predpep_smoke`.
9. **Rollback if needed:** `docker rm -f predpep_app && docker run … predpep:phase1-cuda` (the
   preserved CUDA image) restores the prior behavior.

## Git strategy

Branch `phase2a/headless-slim` off `main`; logical commits:
1. app headless (`predPEP.py`),
2. remove frontend (`templates/`, `static/`, `tmap_utils.py`, `.gitignore`),
3. Dockerfile slim + CPU-only,
4. runtime configs + README,
5. regenerate `blobs.sha256`.
After the full-job verification passes, fast-forward merge to `main`, then cut over.
Commit messages: no `Co-Authored-By` trailer.

## Out of scope (2B)

The control API redesign (submit/status/progress/health-state/cancel as a coherent DDN
interface), the CPU-aware queue with reserved-CPU admission + phase awareness, progress
reporting, the "stop accepting jobs" flag, conda-env slimming, and Rosetta pruning.

## Success criteria

- Image builds `FROM ubuntu:22.04`, no tmap/ogdf, no frontend; smaller than the Phase-1
  image (record before/after sizes).
- Throwaway container boots headless, `/health` OK, Rosetta + FoldX binaries run on the
  ubuntu base, and a real `SPEGFH.pdb` job runs to a result `.zip`.
- `predpep_app` is replaced by the slim CPU-only image (no `--gpus`) and serves `/health`.
- `predpep:phase1-cuda` retained as a rollback.
- Clean git history on `main`; no heavy files committed.
