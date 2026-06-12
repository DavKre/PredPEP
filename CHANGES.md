# Changes

This file tracks what's been added or changed since the reproducible rebuild began. For the extraction history, blob inventory, and the story of how this codebase got here, see [HANDOFF.md](HANDOFF.md). Pipeline scripts in `pepspec_pipe/` are copied verbatim from the production container and are **not** modified here — all changes live in `Dockerfile`, `sp-predPEP/`, and the helper shell scripts.

## 2026-04-22 — Initial rebuild

### Reproducible image from extracted blobs
Replaces the hand-committed `predpep:v2` production image (no Dockerfile, ~77 GB) with a from-scratch reproducible build (~64 GB). Base image: `nvidia/cuda:12.4.0-devel-ubuntu22.04`. Heavy pre-built trees (Rosetta 21 GB, miniforge3 env, FoldX, OGDF, TMAP) are extracted via `RUN --mount=type=bind` in a single layer, so the 23 GB of tarballs never enter any image layer. Gunicorn is invoked directly via the conda env's binary (not `mamba run`) so its gevent worker fork no longer deadlocks on the wrapper's signal traps. OGDF's shared libraries are registered via `/etc/ld.so.conf.d/ogdf.conf + ldconfig` so the conda env's `tmap` native extension can find `libOGDF.so.2025.10.01`.

## 2026-04-23 — Pipeline execution fixes

### `zip` added to apt packages
The iterative manager (`run_iteMAN.py`) invokes `/usr/bin/zip` at the end of every job to bundle results into `SPPDCK_*.zip`. `zip` wasn't in the Dockerfile's apt list because it wasn't recorded in the docker-history-derived package list (production installed it during the hand-committed layer). First real job run produced `FATAL ERROR: [Errno 2] No such file or directory: '/usr/bin/zip'`; fix adds `zip unzip` to the install line.

### Conda env `bin/` prepended to `PATH`
Production relied on `mamba run -n predPEP gunicorn ...` to activate the env at startup, which put `/home/spacepep/miniforge3/envs/predPEP/bin/` on `PATH`. Our direct-exec CMD skipped that activation, breaking bare `python` references inside the pipeline shell scripts (first-job log: `/usr/bin/env: 'python': No such file or directory`, which silently caused iteration 1 to produce zero Rosetta output). Fix prepends the env's `bin/` to the Dockerfile's `ENV PATH`, restoring the same interpreter resolution without re-introducing the `mamba run` wrapper.

## 2026-04-23 — Dynamic CPU selection

### Slider + synced number input (2–32, default 8)
Replaces the 3-option `<select>` (2/4/8 cores) with a range slider and a number input that stay synchronized (drag the slider, the number updates; type a value, the slider updates). Default raised from 4 to 8. Server-side clamping in `predPEP.py` coerces the submitted value to `int` and clamps to `[2, 32]`, silently defaulting to 8 on invalid input — UI isn't trusted for validation and a typo doesn't become a red error.

### Client-side peptide-length detection
On PDB upload, `tab1_submission.js` parses the file text (piggybacking on the NGL-viewer's existing `FileReader`) and counts unique chain B residues via their alpha-carbon atoms. Chain B is hardcoded throughout the pipeline (`-pepspec::pep_chain B` in every flag-generator script), so this matches the pipeline's own expectation. NMR multi-model PDBs dedupe correctly via the `(resSeq, iCode)` key, and `HETATM` records are included so modified residues (MSE, SEP, D-amino acids) count.

### Divisor-aware efficiency hint
A hint span beneath the CPU control explains the relationship between the chosen `cpus` and the detected peptide length. The pipeline's Rosetta fanout in `run_pepSpecPipe.sh` is one process per peptide residue, throttled by `cpus` — so `cpus > length` wastes cores and non-divisor `cpus` produces an uneven final wave. Five hint states cover: no file (neutral), parse failed (soft-fail italic), over-provisioned (yellow), suboptimal divisor (mild info with nearest clean values), and efficient (neutral). The divisor list is computed once at PDB upload and cached on `window.peptideDivisors`.

## 2026-06-12 — Phase 1: repo restructure & clean baseline

Behavior-identical reorganization ahead of the headless-service work. Directories
renamed for clarity (`sp-predPEP/`→`app/`, `pepspec_pipe/`→`pipeline/`, helper scripts
into `scripts/`); in-image paths unchanged so runtime is identical. Added `.gitignore`
(keeps the ~23 GB `blobs/`, the ~586 MB vendored frontend libs, and build/cache
artifacts out of git) and a tracked `blobs/blobs.sha256` integrity manifest
(`CHECK_BLOBS=1 ./scripts/build.sh` verifies it). Superseded cruft (`*_bak*`,
`old_scripts/`, `usr_local_bin/`, `*.py3`) is gitignored and `.dockerignore`d rather
than deleted. Repo brought under version control with a clean initial history.
