# UI Cleanup + Stop Button (Design)

**Date:** 2026-06-13
**Status:** Approved
**Scope:** Fix the now-stale "do not close this page" message, add a Stop button (cancel a
running job), and remove the two non-functional TMAP tabs (3 & 6).

## Context

Now that jobs persist on a volume and the Jobs tab can retrieve them, the front-page
`Processing... (Do not close this page)` warning ([index.html](app/templates/index.html)) is
wrong — closing the page is fine. The user also wants to cancel running jobs and to drop the
two TMAP tabs (`/get_tmap_tree` is non-functional because `mhfp` isn't in the env).

## 1. Loading message (trivial)
`<div id="loading">Processing... (Do not close this page)</div>` → text reflecting persistence:
"Processing… you can leave this page — the job keeps running. Track it in the **Jobs** tab."

## 2. Stop button (backend + UI)

**Submission (`app/predPEP.py` `upload_file`):**
- Launch the manager with `start_new_session=True` so it gets its own process group (so we can
  kill the whole tree: `run_iteMAN.py` + its `bash`/Rosetta children).
- After `Popen`, write the manager PID to `<result_dir>/manager.pid`.

**New `POST /jobs/<id>/stop`:**
- Validate `job_id` (reject `/`, `..`, empty/`.`/`..`); 404 if the result dir is absent.
- Read `manager.pid`, `os.killpg(os.getpgid(pid), SIGTERM)` (tolerate `ProcessLookupError`/
  missing pid — so it also clears a stuck "Processing" row from an orphaned job).
- Write a `STOPPED` marker file. Return `{success, stopped, killed}`.
- A shared helper `_kill_job(jdir)` is also called by `DELETE /jobs/<id>` so deleting a running
  job kills it first (closes the earlier "delete leaves a zombie run" caveat).

**Status derivation (`GET /jobs`):** `Complete` (`<id>.zip` exists) → `Stopped` (`STOPPED`
marker) → `Processing`.

**UI (`tab7_jobs.js`, table):** a **Stop** button on `Processing` rows → `POST /jobs/<id>/stop`
(with confirm) → reload. `Complete`/`Stopped` rows show no Stop button.

## 3. Remove tabs 3 (T-Maps) & 6 (Tree-Map MST) — frontend only

- `index.html`: delete the `tab3Button`/`tab6Button` and `tab3-view`/`tab6-view` blocks (incl.
  their `tmapDropdown`/`tmap6Dropdown` selects); remove the `tab3_tmaps.js`/`tab6_tmap.js`
  script tags if present; **renumber the visible button labels to 1–5** (Template · Results ·
  Score Comparison · Clustering · Jobs) — internal element IDs stay (`tab1,2,4,5,7`) to limit churn.
- `index.js`: remove `tab3-view`/`tab6-view` (and their buttons) from `tabButtons`/`tabViews`;
  the array becomes `[tab1, tab2, tab4, tab5, tab7]` (positions 0–4). Update `switchTab` so the
  Jobs special-case uses the new position (4, not 6); rewrite `handlePlotRendering` so positions
  2→`renderComparisonPlots` (tab4) and 3→`renderAdvancedPlots` (tab5); drop the `renderTSNEPlots`/
  `renderMSTTree` calls and the `tmapDropdown`/`tmap6Dropdown` change-listeners; drop those two
  names from the `import` from `plots_utils.js`; update the "enable tabs after results" list to
  `['tab2Button','tab4Button','tab5Button']`.
- `tab3_tmaps.js`/`tab6_tmap.js` files: leave on disk (unreferenced) — or delete; either is fine
  since nothing loads them. (Plan deletes them for cleanliness.)

## Verification + cutover
1. Tag current `predpep:local` as `predpep:preui2` (rollback). Build.
2. Throwaway `predpep_smoke2` (6365) + throwaway volume; gentle boot wait (single sleep — NOT a
   poll loop, which wedges the worker).
3. Submit `examples/quicktest.pdb` → `manager.pid` written; `/jobs` shows `Processing`.
4. `POST /jobs/<id>/stop` → returns `killed:true`; the `run_iteMAN`/pepspec processes are gone
   (`pgrep` empty); `/jobs` shows `Stopped`.
5. `curl /` shows the updated loading text; the HTML has no `tab3`/`tab6` buttons/views; buttons
   read 1–5.
6. Tear down throwaway; cut `predpep_app` over (with `predpep_data` volume); confirm health + UI
   + Jobs.

## Out of scope (2B)
CPU-aware queue, reserved-CPU admission, node-state endpoint, progress %, auth. Restoring TMAP
(needs `mhfp` + the `tmap`/`ogdf` blobs).

## Success criteria
- Loading message no longer says "do not close".
- Submitting writes `manager.pid`; `POST /jobs/<id>/stop` kills the pipeline and flips status to
  `Stopped`; `DELETE` also stops first.
- Tabs 3 & 6 gone; remaining tabs (Results, Score Comparison, Clustering, Jobs) still switch and
  render; buttons numbered 1–5.
- `predpep_app` cut over; `predpep:preui2` retained as rollback; clean git history.
