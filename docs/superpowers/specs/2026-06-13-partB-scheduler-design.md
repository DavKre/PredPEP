# Part B — In-process Job Scheduler, Retention & Capacity (Design)

**Date:** 2026-06-13
**Status:** Approved (architecture); spec for review
**Scope:** A CPU-aware job scheduler that queues local web submissions so the node never
exceeds its core budget; a `/state` capacity endpoint so DDN sees web-submitted load; disk
retention (≤ 50 GB, ≤ 6 months) + post-zip cleanup; a Jobs-tab disk bar + `Queued`/`Running`/
`Failed` statuses; and the boot-wedge fix (`--preload` + warm-up). Also fixes the residual
status-lifecycle issues (orphans, partial-zip races, pid-reuse) by making the scheduler the
single owner of job state.

## Architecture

A new **`app/scheduler.py`** owns all job lifecycle; **`app/predPEP.py`** stays the thin web
layer. The scheduler runs as **one background gevent greenlet inside the single gunicorn worker**
(`-w 1`), started from a `post_worker_init` hook so it runs in the worker (not the preload
master). State is **filesystem-backed** (the job dirs + `job.json`) so it rebuilds on restart.

### `job.json` becomes the source of truth for status
`job.json` gains a `status` field, written **only by the scheduler**:
`queued → running → complete | stopped | failed`. Endpoints read it directly (no more deriving
from scattered markers). `download_url` is still presence-of-`<job>.zip`.

### Scheduler loop (every ~3 s)
1. **Admission (FIFO):** the oldest `queued` job starts when `reserved_cores + job.cpus ≤
   CORE_BUDGET`. Starting = launch the manager (`Popen(..., start_new_session=True)`, write
   `manager.pid`), set `status=running`, add `cpus` to `reserved_cores`. Strict FIFO (no
   skip-ahead) — predictable, no starvation. (Per-job `cpus` is clamped to `CORE_BUDGET` at
   submit so a job is always eventually startable.)
2. **Completion detection:** for each `running` job, if its manager PID is no longer alive
   (`os.kill(pid,0)` raises): `complete` if `<job>.zip` exists, else `stopped` if a `STOPPED`
   marker exists, else `failed`. Free its cores. On `complete`, run **post-zip cleanup** (below).
3. **Retention sweep** (throttled to ~every 5 min): enforce the caps (below).

### Startup reconciliation
On first start the scheduler scans all job dirs: `queued` → re-added to the queue; `running`
with a dead/absent manager (always true after a container restart) → `failed` (honest terminal
state — the subprocess died with the container); `complete/stopped/failed` left as-is; jobs with
no `status` field (pre-Part-B) → derived once from the filesystem and written. `reserved_cores`
is recomputed from whatever is genuinely `running` (≈0 after a restart), so queued jobs then
schedule fresh.

### Concurrency
Single gunicorn worker + gevent ⇒ cooperative, no true parallelism. The scheduler greenlet owns
`reserved_cores` (int) and `running` (`{job_id: cpus}`); request handlers only **append** to the
queue (write `job.json status=queued`) — safe between gevent yields. The loop uses `gevent.sleep`.

## Config (env-overridable)
- `PREDPEP_CORE_BUDGET` (default `len(os.sched_getaffinity(0))` — cores actually available to the
  process, respecting cgroup limits).
- `PREDPEP_RETENTION_BYTES` (default `50 * 1024**3`).
- `PREDPEP_RETENTION_DAYS` (default `180`).
- `cpus` submit clamp becomes `[2, min(32, CORE_BUDGET)]`.

## Endpoints (`app/predPEP.py`)
- **Submit (`upload_file`)**: no longer launches the manager directly. It builds `manager_command`,
  writes `job.json` with `status=queued`, then calls `scheduler.enqueue(job_id, result_dir,
  manager_command, cpus)`. Response includes the placement: `{status: "queued"|"running",
  "queue_position": k}` so the page can say "Queued (k ahead)" vs "Running".
- **`GET /state`** (new): `{core_budget, reserved_cores, available_cores, running, queued,
  accepting, disk:{used_bytes, cap_bytes, used_pct}}`. For DDN dispatch + the UI bar.
- **`/jobs`, `/status/<id>`**: read `job.json status` (scheduler-maintained); add `Queued`,
  `Running`, `Failed` to the values they can return.
- **`stop_job`**: for a `running` job → kill group + `status=stopped` + `STOPPED` marker (as today,
  but verify the PID is the manager first — see below); for a `queued` job → remove from queue +
  `status=stopped` (nothing to kill). `delete_job` unchanged (kills first, then rmtree).
- **PID-reuse guard:** before `killpg`, `_kill_job` checks `/proc/<pid>/cmdline` contains
  `run_iteMAN` (so a reused PID isn't signalled).

## Retention + post-zip cleanup (in `scheduler.py`)
- **Post-zip cleanup:** when a job becomes `complete`, delete everything in its result dir except
  `<job>.zip` and `job.json` (removes the redundant uncompressed tree ≈ halves footprint), and
  delete its `uploads/<job>` dir.
- **Sweep:** compute total bytes under `results/` + `uploads/` (cached, recomputed each sweep).
  Delete terminal jobs (`complete/stopped/failed`) older than `RETENTION_DAYS`; then, while total
  > `RETENTION_BYTES`, evict the **oldest terminal** job until under cap. Never touch
  `queued`/`running` jobs. Log each eviction.

## UI (`app/templates/index.html`, `app/static/tab7_jobs.js`)
- A **disk usage bar** at the top of the Jobs tab: `used / cap` (e.g. "12.3 / 50 GB"), colored
  (green/amber/red), fed by `/state` on each poll.
- Status cell renders `Queued` (grey), `Running` (amber), `Complete` (green), `Stopped`/`Failed`
  (red). Stop button shows for `Queued` and `Running`.
- Submit flow: the front page shows the returned placement ("Queued — N ahead" / "Running").

## Boot-wedge fix
- New **`app/gunicorn.conf.py`** (auto-loaded from the WORKDIR): `preload_app = True` (import the
  Flask app in the master before fork — directly targets the gevent fork-time wedge);
  `post_worker_init(worker)` → `import predPEP; predPEP.start_scheduler()`; a small warm-up that
  touches the app. The Dockerfile CMD keeps its flags (they merge over the config).
- **Container limits:** `scripts/run.sh`/`run-dev.sh` pass `--memory ${PREDPEP_MEMORY}` only when
  the env var is set (operator-tuned per machine; no wrong hardcoded default), documented in
  README. CPU is governed by the scheduler's core budget, so no `--cpus` cap is forced.
- **Autoheal:** documented in README (the willfarrell/autoheal sidecar, or DDN restarting on
  `health!=ok`) — not bundled, since it's host/fleet infra.

## Verification
Throwaway container (gentle wait + restart-fallback) with a low `PREDPEP_CORE_BUDGET` (e.g. 4):
1. Submit two jobs of `cpus=4` → first `running`, second `queued` (`queue_position` 1); `/state`
   shows `reserved=4, available=0, queued=1`.
2. Stop the first → second transitions `queued→running`; `reserved` back to 4.
3. Submit, let iter-1 produce the zip path? (too slow) — instead simulate: a fake `complete` job
   with an uncompressed tree + zip → post-zip cleanup leaves only zip + job.json.
4. Retention: set `PREDPEP_RETENTION_BYTES` tiny, drop two fake complete jobs → sweep evicts the
   oldest; `/state.disk` reflects it.
5. Restart the container → a `running` job becomes `failed`, a `queued` job re-queues; reconciles.
6. `/state` shape correct; UI disk bar + statuses render. Then cut over (preserve volume), tag
   `predpep:preB` rollback.

## Out of scope
DDN itself, auth, multi-node coordination, phase-level progress %, "stop accepting jobs" toggle
(the `accepting` field is reported but always `true` for now — the toggle is DDN-managed later).

## Success criteria
- A node never runs more reserved cores than `CORE_BUDGET`; excess submissions queue (FIFO) and
  start as cores free; submit + Jobs UI show `Queued`/`Running`.
- `/state` reports cores + disk so DDN sees web load; the Jobs tab shows a disk bar.
- Disk stays ≤ 50 GB / ≤ 6 months; completed jobs keep only zip + metadata.
- Restart reconciles cleanly (no permanent "Processing"); boot is reliable (`--preload`).
- `predpep_app` cut over; `predpep:preB` rollback; clean git history.
