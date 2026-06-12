# Phase 1 — Repo Restructure & Clean Baseline (Design)

**Date:** 2026-06-12
**Status:** Approved (design); pending spec review
**Scope:** Phase 1 of the two-phase predPEP-node cleanup. Behavior-identical restructure only.

## Context

`predpep_local` is a reverse-engineered Docker rebuild of the `predPEP` peptide-design
app (Flask + gunicorn/gevent serving a browser UI that spawns Rosetta + FoldX pipelines
via `run_iteMAN.py`). It is being evolved into a deployable backend **compute node** for a
distributed system (DDN). That evolution is split into two phases:

- **Phase 1 (this spec):** organize the repo, put it under version control cleanly, and
  remove dead weight — with **no change to runtime behavior**. Produces a rollback point.
- **Phase 2 (later, separate spec):** go headless (drop frontend), slim the image, add a
  machine-facing control API and a CPU-aware job queue.

The framework question was resolved in favor of **keeping Flask**: the frozen conda env
ships Flask 3.1.2 / gunicorn 25.1 / gevent and no FastAPI/uvicorn; migrating would require
mutating the frozen env (full rebuild + re-verification) and would discard the hard-won
direct-gunicorn-exec fix. The app's concurrency needs are met by the Phase-2 queue +
separate worker processes, not by an async web framework.

## Hard constraints

1. **Never stop, restart, or remove the running `predpep_app` container** (image
   `predpep:local`, host port 6363) — it is in active use. Building images is safe;
   `./scripts/run.sh` must not be run (it targets that name). All verification uses a
   throwaway container on a different name and host port (6364).
2. **In-image paths stay byte-for-byte identical:** `/opt/sp-predPEP`,
   `/usr/local/pepspec_pipe`, `/home/spacepep/miniforge3/...`, `/usr/local/rosetta_pkgs/...`,
   `/usr/local/foldx26Linux64_0/...`. The conda env and `run_iteMAN.py` hardcode these.
   Only the **repo's on-disk layout** changes; the Dockerfile remaps to the same in-image
   targets.
3. **Behavior-identical:** the rebuilt image must boot gunicorn and serve the app exactly
   as today. No routes, no pipeline scripts, no env, no frontend behavior changes.
4. **Non-destructive:** nothing is committed yet, so a `rm` is permanent with no git
   recovery. Dead weight is *gitignored and `.dockerignore`d*, not deleted, unless the user
   explicitly approves deletion afterward.

## Target layout

Before → after:

```
sp-predPEP/        → app/
pepspec_pipe/      → pipeline/
build.sh run.sh run-dev.sh   → scripts/
blobs/             → blobs/        (unchanged on disk; tarballs gitignored, manifest tracked)
usr_local_bin/     → (gitignored + .dockerignored; left on disk as reference)
docs/              → docs/         (+ docs/superpowers/specs/ for this spec)
Dockerfile .dockerignore docker-compose.yml README.md CHANGES.md HANDOFF.md → stay at root
```

Final tree:

```
predpep_local/
├── README.md  CHANGES.md  HANDOFF.md
├── Dockerfile  .dockerignore  docker-compose.yml
├── .gitignore                      # NEW
├── scripts/        build.sh  run.sh  run-dev.sh
├── app/            predPEP.py  tmap_utils.py  pipelines.txt  templates/  static/
├── pipeline/       run_iteMAN.py  run_*.sh  run_*.py  README.txt
├── blobs/          *.tar.gz (gitignored)  blobs.sha256 (tracked)
└── docs/           extraction refs…  superpowers/specs/2026-06-12-phase1-restructure-design.md
```

`Dockerfile` and `.dockerignore` stay at the repo root (the build-context root) — the
standard arrangement; `docker build .` continues to run with context = repo root.

## Change list

### 1. Directory moves
- `git mv`-style moves (plain `mv`, since untracked): `sp-predPEP/ → app/`,
  `pepspec_pipe/ → pipeline/`, and the three `*.sh` into `scripts/`.

### 2. `.gitignore` (new, at root)
```
# Heavy extracted tool blobs (~23 GB) — provided out-of-band, see HANDOFF.md
blobs/*.tar.gz

# Build / runtime artifacts
*.log

# Python
__pycache__/
*.py[cod]

# Reference-only / superseded cruft (kept on disk, out of the clean repo)
usr_local_bin/
old_scripts/
**/*_bak*
**/*.py3
```

### 3. `.dockerignore` updates
- Repath: `sp-predPEP/old_scripts → app/old_scripts`, `sp-predPEP/__pycache__ → app/__pycache__`,
  `build.sh`/`run.sh` → `scripts/` (plus `docker-compose.yml` stays).
- Add cruft excludes so they never enter the image: `**/*_bak*`, `**/*.py3`, `app/old_scripts`.
- Keep `usr_local_bin`, `docs`, `*.md`, `.git`, `.gitignore`, `**/__pycache__`.
- Leave `blobs/*.tar.gz` available to the build (the `RUN --mount=type=bind,source=./blobs`
  reads from the context — do NOT exclude `blobs/`); only the `blobs/blobs.sha256` manifest
  is excluded from the image context.

### 4. `blobs.sha256` manifest
- Generate `sha256sum blobs/*.tar.gz > blobs/blobs.sha256`; track the manifest in git.
- `scripts/build.sh` gains an optional pre-build `sha256sum -c blobs/blobs.sha256` check
  (warn-only, non-fatal, skippable) so a corrupt/missing blob is caught early.

### 5. Dockerfile edits (path remaps only — in-image targets unchanged)
- `COPY pepspec_pipe/ /usr/local/pepspec_pipe/` → `COPY pipeline/ /usr/local/pepspec_pipe/`
- `COPY --chown=... sp-predPEP/ /opt/sp-predPEP/` → `COPY --chown=... app/ /opt/sp-predPEP/`
- `RUN --mount=type=bind,source=./blobs,...` — **unchanged** (`blobs/` stays put).
- Nothing else changes: same base image, same env, same CMD, same HEALTHCHECK.

### 6. Script edits
- `scripts/build.sh`: `cd "$(dirname "$0")/.."` so the build context is the repo root;
  `docker build .` unchanged otherwise.
- `scripts/run.sh`: no path references — unchanged except its new location.
- `scripts/run-dev.sh`: `cd "$(dirname "$0")/.."; ROOT="$(pwd)"`; bind mounts become
  `${ROOT}/app:/opt/sp-predPEP` and `${ROOT}/pipeline:/usr/local/pepspec_pipe`.

### 7. Doc edits
- `README.md`: update `./build.sh`→`./scripts/build.sh` (and run/run-dev), `sp-predPEP/`→`app/`,
  `pepspec_pipe/`→`pipeline/`.
- `CHANGES.md`: append a 2026-06-12 "Phase 1 — repo restructure" entry.
- `HANDOFF.md`: add a one-line banner at the top noting the restructure and pointing to
  README for current paths; historical body left intact.

## Verification (never touches `predpep_app`)

1. `./scripts/build.sh` completes; the expensive blob-extraction layer stays **CACHED**
   (its `RUN` command and blob content are unchanged), only the cheap final COPY layers
   rebuild. Rebuilding the `predpep:local` tag does not affect the already-running
   container.
2. Launch a throwaway container — **name `predpep_smoke`, host port 6364, no `--gpus`**
   (Flask boot + `GET /` need no GPU):
   `docker run -d --name predpep_smoke -p 6364:6363 predpep:local`
3. Confirm: `docker logs predpep_smoke` shows gunicorn booting a worker without import
   errors; `curl -fsS http://localhost:6364/` returns the index HTML; symlink
   `ls -l /usr/local/bin/run_iteMAN.py` resolves; `python -c "import flask"` works in-env.
4. **Tear down only the throwaway:** `docker rm -f predpep_smoke`. `predpep_app` and port
   6363 are never touched.

## Git strategy

Repo currently has zero commits. Establish history with a small number of **logical
commits**, not one "commit the mess" dump:

1. **Initial commit on `main`** = `.gitignore` + `blobs/blobs.sha256` + this design doc.
   Adding `.gitignore` first guarantees the ~23 GB of blobs are excluded from the very first
   content commit.
2. **Create branch `phase1/restructure`**; on it, commit the implementation in logical steps:
   - the directory moves + Dockerfile/script path remaps,
   - `.dockerignore` updates,
   - doc edits (README/CHANGES/HANDOFF).
3. After verification passes, **merge `phase1/restructure` into `main`** (fast-forward).

`git add` only intended paths; never `git add` the blob tarballs (the `.gitignore` guards
this, but stage explicitly regardless). Commit messages follow the repo/user convention
(no `Co-Authored-By` trailer).

## Out of scope (Phase 2)

Removing the frontend (`static/` + `templates/`), dropping the `tmap`/`ogdf` blobs and the
`ldconfig` step, pruning Rosetta, the control API (submit/status/progress/health), the
CPU-aware queue, reserved-CPU admission, phase-aware progress, and any security work.

## Success criteria

- Repo organized into `app/ pipeline/ scripts/ blobs/ docs/` with Dockerfile/.dockerignore
  at root; cruft out of the clean tree.
- `.gitignore` keeps the ~23 GB of blobs and all build/cache artifacts out of git.
- `blobs.sha256` tracked; `build.sh` can verify it.
- `./scripts/build.sh` produces a working `predpep:local`; throwaway smoke container on 6364
  boots gunicorn and serves `/` with no import errors.
- A clean initial git history exists on a feature branch, ready to merge to `main`.
- `predpep_app` ran undisturbed throughout.
```
