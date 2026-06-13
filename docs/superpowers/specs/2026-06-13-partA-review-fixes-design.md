# Part A — Review Hardening Fixes (Design)

**Date:** 2026-06-13
**Status:** Approved
**Scope:** The low-risk, mechanical fixes from the pre-ship deep review. No new architecture.
The CPU-aware scheduler + retention + capacity endpoint are **Part B** (separate cycle).

## Context

A deep review surfaced one regression (Stop leaves the submit page hanging) plus several small
correctness/ops/doc issues. This part fixes the independent, mechanical ones. Anything that
needs the job scheduler (proper status lifecycle, queue, retention, /state, container CPU/mem
caps) is deferred to Part B so each part stays focused.

## Fixes

### 1. Stop → submit page hangs forever (regression)
- **`app/predPEP.py` `check_status`:** add a `STOPPED`-marker branch returning
  `{'status': 'Stopped'}` (mirrors `list_jobs`), before the generic Processing/Pending fallback.
- **`app/static/index.js` `pollStatus`:** currently only `clearInterval`+hide-loading on
  `Complete`. Also stop polling and hide the spinner on `Stopped` and `Pending/Failed`, showing a
  short message ("Job stopped." / "Job failed to start.").

### 2. `count_peptide_residues` counts calcium ions as residues
- **`app/predPEP.py`:** the Cα test `line[12:16].strip()=="CA"` also matches calcium (atom name
  `CA`, element `CA`). Require the element column to be carbon: only count when
  `line[76:78].strip()` is `"C"` **or empty** (older PDBs without the element column).
- **`app/static/tab1_submission.js`:** apply the identical element-column guard to the client-side
  `countChainBResidues` twin so the live "Pep. length" hint matches.

### 3. `/download/<dir>/<file>` doesn't validate the directory segment
- **`app/predPEP.py` `download_file`:** reject `master_dir_name` containing `/` or `..`
  (return 403), the same guard already used by `delete_job`/`stop_job`. (`send_from_directory`
  protects `<filename>`; this closes the one-level-up gap on the directory.)

### 4. Large-file upload — warn, don't block
- **`app/static/tab1_submission.js`:** on file-select/submit, if the chosen PDB is unusually large
  (> 25 MB), show a non-blocking warning near the form ("Large file (NN MB) — upload may be slow;
  PDBs are usually < 1 MB"). No server-side size cap (per user: don't restrict users).

### 5. Container log rotation + quieter logs
- **`scripts/run.sh`, `scripts/run-dev.sh`:** add `--log-opt max-size=10m --log-opt max-file=3`
  and `--pids-limit 4096` to the `docker run`.
- **`docker-compose.yml`:** add the equivalent `logging:` (json-file, max-size 10m, max-file 3)
  and `pids_limit: 4096`.
- **`Dockerfile` CMD:** change gunicorn `--log-level debug` → `--log-level info`.
- (Container `--cpus`/`--memory` are intentionally deferred to Part B, where the scheduler's core
  budget defines the CPU policy.)

### 6. Build-time assertion that the Rosetta prune kept what's needed
- **`Dockerfile`:** after the extraction+prune `RUN`, add a `RUN` that fails the build if any kept
  path is missing:
  `test -f .../static/pepspec.static.linuxgccrelease && test -d .../main/database && test -f
  .../protein_tools/scripts/clean_pdb.py`. Turns a bad prune into a build failure, not a runtime
  job failure. (Blob checksum stays opt-in via `CHECK_BLOBS=1`.)

### 7. Doc/label fixes
- **`app/templates/index.html`:** Jobs table header `<th>Delete</th>` → `<th>Actions</th>` (the
  column holds Stop + Delete).
- **`README.md`:** reconcile the "NGL/Plotly load from CDN" note with the vendored
  `app/static/js/ngl.umd.js` (state that a small NGL bundle is vendored but the page uses the CDN).

## Verification + cutover
Build → throwaway smoke (gentle wait + restart-fallback): submit a job and Stop it, confirm
`/status` returns `Stopped` (not Processing); confirm `download` rejects a `..` dir; confirm the
build assertion is present; spot-check the Jobs UI header + log flags. Then cut `predpep_app` over
(volume preserved), tag `predpep:preA` rollback.

## Out of scope (Part B)
The scheduler/queue, reserved-core admission + `Queued` status, `/state` capacity endpoint,
retention (50 GB / 6 months) + post-zip cleanup + size bar, the boot-wedge `--preload`/autoheal,
and container `--cpus`/`--memory`. Proper lifecycle-based status (orphan/zip-race) also lands with
the scheduler; #1 here is the minimal band-aid for the regression.

## Success criteria
- Stopping a job no longer hangs the submit page; `/status` reports `Stopped`.
- `count_peptide_residues` ignores calcium; `/download` rejects `..` dirs.
- Large files warn (not block); container logs rotate; build fails fast on a bad prune.
- Header reads "Actions"; README NGL note accurate. `predpep_app` cut over; `predpep:preA` rollback.
