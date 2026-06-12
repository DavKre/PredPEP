# Persistent Storage + Job List & Management (Design)

**Date:** 2026-06-13
**Status:** Approved
**Scope:** Add persistent job storage (Docker volume) + a job metadata record, two API
endpoints (`GET /jobs`, `DELETE /jobs/<id>`), and a "Jobs" tab in the UI. First persistence
slice of the eventual Phase 2B control API.

## Context

Jobs are written to `/tmp/pepspec/{uploads,results}` **inside the container**, so every
container recreate (including the slimming cutovers) wipes all history. There's no way to
see or re-download past jobs after leaving the page, and old results accumulate with no way
to prune them. This adds persistence + a job list with download/delete.

`predPEP.py` and the verbatim pipeline script `run_catSPEC.py` reference `/tmp/pepspec`, so
the data root **stays at `/tmp/pepspec`** (no code/path changes, honors the verbatim-pipeline
rule); persistence comes from mounting a volume there.

## Components

### 1. Persistence — named volume `predpep_data` at `/tmp/pepspec`
- `scripts/run.sh`, `scripts/run-dev.sh`, `docker-compose.yml` mount `predpep_data:/tmp/pepspec`.
- Docker initialises the fresh volume from the image's empty `uploads/`+`results/` (preserving
  `spacepep:spacepep` ownership); it then persists across recreate/redeploy. Each machine gets
  its own volume (correct for the distributed model). The `/tmp` name is cosmetic — the volume
  is durable.

### 2. Job metadata — `job.json` per job (`app/predPEP.py`)
At submission, `upload_file()` writes `<result_dir>/job.json`:
```json
{ "job_id": "...", "submitted_at": "<ISO8601 UTC>", "protein_symbol": "...",
  "user_name": "...", "cpus": 8, "pdb_filename": "...", "peptide_length": 4 }
```
`peptide_length` = count of unique chain-B residues (by Cα) parsed from the saved PDB via a new
`count_peptide_residues(pdb_path)` helper (fixed-column PDB parse: chain at col 22, atom name
cols 13–16, resSeq+iCode cols 23–27).

### 3. API (`app/predPEP.py`) — existing routes unchanged
- `GET /jobs` → `{success, jobs:[...]}`, newest first. Scans `BASE_RESULT_FOLDER` subdirs,
  reads each `job.json` (tolerates missing), derives **status** (`Complete` if `<id>.zip`
  exists else `Processing`) and `download_url` (`/download/<id>/<id>.zip` when complete).
- `DELETE /jobs/<job_id>` → removes the job's result dir **and** upload dir (reclaims disk).
  Validates `job_id` (reject `/`, `..`, empty/`.`/`..`); 404 if neither dir exists.

### 4. UI — new always-enabled "Jobs" tab (`app/templates/index.html`, `app/static/tab7_jobs.js`, wired in `index.js`)
- Tab button "7. Jobs" (NOT disabled, unlike tabs 2–6) + a `tab7-view` table.
- Columns: **Date · Protein · User · CPUs · Pep. length · Status · Download · Delete**.
- `tab7_jobs.js`: `loadJobs()` fetches `/jobs` and renders rows; polls every 10 s while the tab
  is active; **Download** links to `download_url`; **Delete** confirms, calls `DELETE /jobs/<id>`,
  then reloads. No auth — all jobs shown.
- `index.js`: wire `tab7Button` into the existing tab-switch logic; load jobs when the tab opens.

## Verification + cutover

1. Tag current `predpep:local` (UI image) as `predpep:prejobs` (rollback). Build.
2. Throwaway `predpep_smoke2` on **6365** with a **throwaway** volume `predpep_data_smoke:/tmp/pepspec`.
3. Submit `examples/quicktest.pdb` → confirm `job.json` written with correct fields; `GET /jobs`
   lists it (status `Processing`, metadata correct).
4. Simulate a completed job: create a dir `<vol>/results/FAKE_test/` with a `job.json` + a
   `FAKE_test.zip` → `GET /jobs` shows it `Complete` with a `download_url`; the download serves
   the zip.
5. **Persistence check:** `docker rm` + recreate the smoke container on the same volume → `/jobs`
   still lists both jobs (proves durability).
6. `DELETE /jobs/<id>` → files gone from the volume, job no longer listed.
7. Tear down `predpep_smoke2` + its test volume. Cut `predpep_app` over to the new image
   **with `predpep_data` mounted**; confirm `/health` + UI + `/jobs` (empty initially).

## Caveats (documented in UI Known-limitations; resolved by 2B queue)
- A job orphaned by a container restart shows `Processing` indefinitely (no run-state tracking yet).
- Deleting a *running* job removes its dirs; its background subprocess then fails on its own
  (proper cancel is 2B).

## Out of scope (Phase 2B)
CPU-aware queue, reserved-CPU admission, node-state/capacity endpoint, progress %, cancel, auth.

## Success criteria
- A named volume persists `/tmp/pepspec` across container recreate (proven in smoke step 5).
- Submitting writes `job.json`; `GET /jobs` lists jobs with status + download; `DELETE /jobs/<id>`
  removes files and reclaims disk.
- A "Jobs" tab shows the table with working download + delete; no auth.
- `predpep_app` cut over with the volume; `predpep:prejobs` retained as rollback.
- Clean git history on `main`.
