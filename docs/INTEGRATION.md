# predPEP Node — Integration Guide

**Audience:** anyone building a central **controller / orchestrator** (a dispatcher that deploys,
monitors, and assigns jobs to one or more predPEP compute nodes across machines — including a node
on the controller's own host).
**Node status:** fleet-deployable. As of **v1.6.0** the node has bearer-token auth, enforced from
the moment the first token exists — see §8.

> **v1.6.0 readers start here:** [UPGRADE-v1.6.0-node.md](UPGRADE-v1.6.0-node.md) carries the delta
> from v1.4.0, including the one change a dispatcher **must** make (`POST /upload` can return
> several job ids) and the `/data` migration. The dev's own notes are in
> [upstream-v1.6.0/](upstream-v1.6.0/). This guide describes the steady state.

---

## 1. Model in one paragraph

A **predPEP node** is a single self-contained Docker image (`predpep:v1.6.0`, CPU-only,
`ubuntu:22.04` base) that runs a Flask/gunicorn service on **host port 6363** (container 8383). It accepts a
peptide-design job (a PDB complex + parameters), runs a Rosetta + FoldX pipeline, and produces a
result `.zip`. Each node is **autonomous**: it has its own CPU-aware queue, disk retention, and a
browser UI. A controller's job is to (a) deploy/maintain the image on machines, (b) watch each
node's capacity via `GET /state`, (c) dispatch jobs via `POST /upload`, and (d) track + collect
results. Nodes are identical regardless of host, so the **controller's own machine can run a node**
and be dispatched to like any other — just include its `host:6363` in the node list.

---

## 2. Deploying / updating / removing a node

**Image distribution** (no rebuild on targets, no blobs needed on targets):
```bash
# from a machine that has the image — stream it over SSH, no temp files:
docker save predpep:v1.3.0 | gzip | ssh USER@TARGET 'gunzip | docker load'
```
(A private registry works too: `docker push`/`pull`. Do not push to a public registry — the image
bundles academic-licensed Rosetta/FoldX.) See the README "Distributing the image" section for the
resumable-via-file variant.

**Run a node** (this is what `scripts/run.sh` does):
```bash
docker run -d --name predpep_app \
  -e PREDPEP_CORE_BUDGET=32 \                  # optional; default = machine cores
  -v predpep_data:/data \                      # persistent job storage — REQUIRED
  --log-opt max-size=10m --log-opt max-file=3 \
  --pids-limit 4096 --stop-timeout 120 \
  -p 6363:8383 \
  --restart unless-stopped \
  predpep:v1.3.0
```
- **Persistent volume `predpep_data:/tmp/pepspec`** holds all jobs (metadata, results, zips). It
  **survives container recreate/redeploy** — each machine keeps its own job history.
- **Healthcheck:** the image has a Docker `HEALTHCHECK` polling `/health` every 30 s.
- **No GPU / driver / toolkit** required.

**Update a node to a new image version** (jobs preserved via the volume):
```bash
docker load < predpep-v1.3.0.tgz                 # or registry pull
docker rm -f predpep_app && docker run -d ... predpep:v1.3.0   # volume re-attaches, jobs persist
```
On restart the node **reconciles** its state from the volume (see §4).

**Tuning env vars** (`-e` at `docker run`):
| Var | Default | Meaning |
|---|---|---|
| `PREDPEP_CORE_BUDGET` | machine cores (`nproc`) | max CPU cores committed across running jobs |
| `PREDPEP_RETENTION_BYTES` | `53687091200` (50 GB) | job-storage size cap |
| `PREDPEP_RETENTION_DAYS` | `180` | job-storage age cap |
| `PREDPEP_MEMORY` | unset | optional Docker `--memory` cap (e.g. `32g`) |

---

## 3. Control API (port 6363)

All responses are JSON unless noted. `job_id` format: `SP<3-letter protein><1-letter user>_<8 hex>`
e.g. `SPEGFT_99b60e04`. Nothing in a job id says which host holds it — nodes share no state, so the
dispatcher must keep the job-to-host mapping; asking the wrong node returns "not found".

**Auth:** send `Authorization: Bearer <token>` on every route except `/health`. On a node with zero
tokens every route is open, which is how the first token is minted; after that it is required. See §8.

### Liveness / capacity (poll these)
| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{"service":"predpep-node","status":"ok","version":"v1.6.0"}` — JSON liveness probe (use this, not `/`); **unauthenticated by design**, and the version is what lets a dispatcher refuse to mix results across a staged rollout |
| `GET` | `/` | the browser **UI** (HTML) — not for the controller; for liveness use `/health` |
| `GET` | `/state` | capacity + disk — **the dispatch signal** (below) |

`GET /state` →
```json
{
  "version": "v1.6.0",       // node image version (baked from the VERSION file at build) — for update checks
  "core_budget": 32,         // max cores this node will commit
  "reserved_cores": 8,       // cores held by running jobs right now
  "available_cores": 24,     // core_budget - reserved
  "running": 2,              // running job count
  "queued": 1,               // queued job count (waiting for cores)
  "accepting": true,         // always true today (see §7 draining)
  "disk": { "used_bytes": 360115252, "cap_bytes": 53687091200, "used_pct": 0.7 }
}
```
Browser-UI-submitted jobs also reserve cores and show up here, so `/state` is the **true** load —
the controller is not the only source of jobs.

Three things about dispatch that are easy to assume wrongly:

- **`accepting` is hardcoded `true`.** It never flips under load. Dispatch on `available_cores`.
- **`/upload` never refuses for lack of capacity — it queues.** A node with two free cores accepts a
  sixteen-core job and parks it. A dispatcher that records success without reading `status` builds an
  invisible backlog on one machine while another sits idle.
- **Nothing reserves capacity between reading `/state` and posting `/upload`.** Two concurrent
  dispatches can read the same numbers and pick the same node; the second queues. Serialising
  dispatch fixes it properly, but reading `status` in the response covers it without coordination.
- **`core_budget` differs per machine** (`PREDPEP_CORE_BUDGET`, else derived from the host), so judge
  "has free CPU" against each node's own `core_budget`, never a fleet-wide constant.

### Submit a job
`POST /upload` — `multipart/form-data`:
| field | type | notes |
|---|---|---|
| `protein_symbol` | string | required; first 3 letters used in the job id |
| `user_name` | string | required; first letter used in the job id |
| `cpus` | int | cores to reserve; server-clamps to `[2, min(32, core_budget)]` |
| `file1` | file | the complex PDB; **peptide must be chain B** |

Requested cores are clamped twice: `max(2, min(32, CORE_BUDGET, requested))` on submit, then reduced
again to the work actually available to parallelise (one pepspec process per anchor). So the reserved
count **cannot be predicted from the submission** — reconcile against `cores` in the response, or
re-poll `/state`. `cores_note` is the human-readable reason a 32-core request ran on 6; show it.

Response:
```json
{ "success": true, "job_id": "SPEGFT_99b60e04",
  "status": "running",          // or "queued"
  "queue_position": 0,          // jobs ahead of it in the FIFO queue
  "cores": 6,                   // what was ACTUALLY reserved (v1.6.0)
  "cores_note": null,           // why, when it differs from the request (v1.6.0)
  "message": "Job SPEGFT_99b60e04 running." }
```
The node reserves `cpus` for the job's **entire lifetime** and starts it only if
`reserved_cores + cpus ≤ core_budget`; otherwise it sits **queued** (FIFO) and starts automatically
when cores free up. On bad input: `{"success": false, "error": "..."}`.

#### One submission can return SEVERAL job ids (v1.6.0) — the one mandatory client change

When the uploaded peptide chain is **discontinuous**, v1.6.0 splits the submission into one sub-job
per segment, each designing its own segment with the others treated as part of the receptor:

```json
{ "success": true, "split": true, "parent_job": "<parent id>",
  "jobs": [ {"job_id": "...", "segment": 1, "queue_position": 0, "cores": 6,
             "cores_note": null, "peptide": "...", "receptor_chains": ["..."],
             "anchor_positions": [...], "caveat": null, "warning": null},
            {"job_id": "...", "segment": 2, "...": "..."} ],
  "job_id": "<jobs[0].job_id>", "status": "queued",
  "message": "<why it was split>", "failures": null }
```

The top-level `job_id` exists for backward compatibility, so a v1.4-era client reads a valid id and
does not crash. **It reads only the first sub-job.** The others run, reserve cores, and are invisible
to it — which shows up as capacity under-count, premature completion when sub-job 1 finishes while
sub-job 2 is still running, and partial results (`/results_data/<first id>` returns one segment's
designs with nothing saying the rest exist). None of it raises an error.

The fix is one branch: **if `split` is true, enumerate `jobs[]` and track every `job_id`.** Also:

- **Do not poll `parent_job`.** Its result directory is deliberately removed, so a parent with no
  work of its own does not appear in `/jobs` and `/status` on it finds nothing.
- **Surface `failures[]`.** A split can partly succeed — some segments created, others not — and
  `failures` is then a list of reasons, not something to swallow.

Why the split exists at all: a discontinuous chain reaching Rosetta turns the break into a
fabricated ~14 Å "peptide bond", and the run completes with plausible scores for a molecule that
cannot exist.

### Track a job
| Method | Path | Returns |
|---|---|---|
| `GET` | `/status/<job_id>` | `{"status": "...", "download_url": "..."|null, "message": "...", "partial": bool, "failed_stage": "..."|null, "artifacts": [...]}` |
| `GET` | `/jobs` | `{"success": true, "jobs": [ {...}, ... ]}` newest-first (all jobs on the node) |

`/jobs` entries:
```json
{ "job_id": "...", "submitted_at": "2026-06-13T...Z", "protein_symbol": "EGF",
  "user_name": "alice", "cpus": 8, "pdb_filename": "SPEGFA.pdb", "peptide_length": 13,
  "status": "Running", "download_url": "/download/<id>/<id>.zip" | null,
  "partial": false, "failed_stage": null, "message": null, "artifacts": [] }
```
**Status values** (see §4): `Queued`, `Running`, `Complete`, `Stopped`, `Failed` — plus `Partial`
**only when the node enables `PREDPEP_PARTIAL_STATUS`** (see below)
(and `Pending/Failed` from `/status` when the job dir is absent). ⚠️ **Casing wrinkle:** `/upload`
returns lowercase (`"running"`/`"queued"`); `/status` and `/jobs` return Title-case. **Compare
case-insensitively.**

#### Partial results — read `partial`, not just `status`
A job can finish with usable per-design results while its **final score aggregation** fails, so
`tab2_final_scores.csv` is missing or incomplete. That case is reported with:

| Field | Meaning |
|---|---|
| `partial` | `true` when the score table is missing or incomplete |
| `failed_stage` | which stage failed: `score_aggregation`, `score_merge`, `spec_file_corruption`, `zip`, `manager` |
| `message` | short human-readable reason |
| `artifacts` | result files present, e.g. `["SPILRH.all.txt", "tab2_final_scores.csv", "*_B.fasta", "*.spec"]` |

⚠️ **`status` alone is not sufficient.** By default a partial job still reports
`status: "Complete"` with `partial: true`, so existing consumers keep working unchanged. The node
only emits the distinct `Partial` status value when it is started with
`PREDPEP_PARTIAL_STATUS=1` — **do not enable that until your client treats `Partial` as a terminal,
results-available state**, otherwise a client that maps unknown statuses to *running* will poll a
finished job forever.

Treat a partial job as **terminal and downloadable**: the zip is complete, and
`tab2_final_scores.csv` (when present) carries a `rosetta_available` column that is `False` for rows
whose Rosetta score is unavailable. Those `Rosetta_Total_Score` cells are **empty — never `0`**.

> #### 🔻 Behaviour change in v1.4.0 — "missing scores file" no longer detects a bad run
>
> Before v1.4.0, a failed aggregation left **no `tab2_final_scores.csv` at all**, so consumers could
> detect the failure by the file's absence. v1.4.0 always writes the table (FoldX-only, Rosetta
> column empty, `rosetta_available=False`) — deliberately, so a degraded run still yields usable
> results.
>
> **Consequence: the missing-file signal stops firing.** A degraded run now looks clean to any
> consumer that isn't reading `partial` / `failed_stage`. After v1.4.0 **the flags are the only way
> to distinguish a degraded run from a good one** — check `partial` (or `rosetta_available` in the
> CSV), not the presence of the file.

### Get the result
`GET <download_url>` → the result `.zip` (binary; `Content-Type: application/zip`). Present for
**every terminal state that produced a zip** — `Complete`, partial, and `Stopped` jobs included, so
a stopped or degraded run never strands the user's results.

### Control a job
| Method | Path | Effect |
|---|---|---|
| `POST` | `/jobs/<job_id>/stop` | SIGKILL the pipeline (or dequeue if queued); status→`Stopped`, cores freed. `{"success":true,"stopped":"<id>"}` |
| `DELETE` | `/jobs/<job_id>` | stop (if running) **and delete** its result + upload dirs (reclaim disk). `{"success":true,"deleted":"<id>"}` |

Both validate the id (reject `/`, `..`); `404` if unknown.

### Inspect a job in depth (v1.6.0)
| Method | Path | Returns |
|---|---|---|
| `GET` | `/jobs/<job_id>/metrics` | live per-iteration progress from `iteration_metrics.json`, `flags_manifest.json`, `runs_status.json`, `foldx_status.json` |
| `GET` | `/jobs/<job_id>/spec` | the `design_spec.json` as submitted — what the job actually ran |
| `GET` | `/jobs/<job_id>/log` | the manager log (where `FoldX binary: <path>` and stage failures appear) |
| `GET` | `/jobs/<job_id>/analytics` | the server-computed analytics bundle (convergence, agreement, correlation, PCA, sequence map, logo, descriptors, sampling) |

`sampling` is worth reading even if nothing else is: pepspec crashes intermittently, and **a crashed
run yields fewer models rather than failing the job**, so anchors that sampled less are ranked
against anchors that got the full budget. Nothing else reports that bias. A job from before the
pipeline recorded it returns `None` rather than zero — unknown losses and no losses are different
claims.

### Residues and tokens (v1.6.0)
| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/residues` | the design-residue catalogue the node will accept (397 non-canonical: 189 L, 208 D) |
| `POST` | `/api/residues/reload` | re-read the catalogue from disk |
| `GET`/`POST`/`DELETE` | `/api/tokens`, `/api/tokens/<id>` | token admin — see §8 |
| `GET` | `/api/audit` | recent authenticated actions |

A dispatcher that submits a residue palette should validate it against the **node's own**
`/api/residues`: the catalogue is baked into the image, so a node on an older image may not offer
what another accepts.

### UI-oriented (a controller can usually ignore)
`GET /results_data/<job_id>` (score tables for the results view),
`GET /stream_final_pdb/<job_id>/<path>` (PDB streaming for the 3D viewer; serves models out of the
result zip once the post-job sweep has removed the directories),
`GET /get_tmap_tree/<job_id>` (the similarity tree — **functional again in v1.6.0**: this image
carries `tmap`/`mhfp`, which the v1.4.0 slim env had dropped).

---

## 4. Job lifecycle & status model

`job.json["status"]` on the node is the source of truth; the node's scheduler owns it:

```
Queued ──(cores free)──> Running ──> Complete   (zip produced; node keeps only the .zip)
                                  └─> Partial    (results usable, but the final score table
                                                   is missing/incomplete — see `partial`)
                                  └─> Stopped    (POST /stop, or DELETE)
                                  └─> Failed     (manager exited without a zip, OR
                                                   interrupted by a node restart)
```
- **Restart reconciliation:** on container/node restart, a job that was `Running` becomes
  **`Failed`** (its subprocess died with the container) and a job that was `Queued` is **re-queued**
  automatically. So a restart never leaves a job stuck "Processing." **Treat `Failed` as
  "re-dispatch if still wanted."**
- **Terminal states:** `Complete`, `Partial`, `Stopped`, `Failed`. Poll until one of these — and
  treat `partial: true` as terminal too, since it is reported as `Complete` until the node enables
  `PREDPEP_PARTIAL_STATUS`.
- **Retention:** terminal jobs are auto-evicted once the node exceeds **50 GB** or a job is older
  than **180 days** (oldest-first; completed jobs keep only their `.zip`). **Fetch a result before
  it's evicted** — don't assume a `Complete` zip lives forever.

---

## 5. Dispatch algorithm (recommended)

```text
for each pending job J (needs J.cpus cores):
    candidates = [ node for node in fleet
                   if GET node/health == ok
                   and GET node/state .accepting
                   and node/state.available_cores >= J.cpus ]
    pick the node with the most available_cores (or least queued)   # load-balance
    POST node/upload (J)            # → job_id; node reserves J.cpus for J's whole life
    record (node, job_id)
    poll node/status/<job_id> until terminal
        Complete -> GET download_url, store the zip, optionally DELETE the job
        Failed   -> re-dispatch elsewhere
        Stopped  -> per policy
```
Notes:
- You **can** over-dispatch (the node queues FIFO), but dispatching by `available_cores` avoids
  head-of-line waiting and keeps `/state` meaningful for the whole fleet.
- A job reserves its cores for its **entire lifetime**, including phases where actual CPU use dips —
  by design, so the node won't admit a second job that would oversubscribe.
- Cores reserved by **browser-submitted** jobs are already reflected in `available_cores`.

---

## 6. Reliability & health

- **Liveness:** `GET /health` and the Docker `HEALTHCHECK` (30 s). **Readiness/capacity:** `/state`.
- **Boot reliability:** gunicorn runs with `preload_app` to avoid an intermittent gevent fork-time
  wedge; the node boots cleanly. **However:** Docker's `--restart unless-stopped` recovers a
  *crashed* container, **not** an *unhealthy-but-running* one. So: **the controller (or an
  `autoheal` sidecar) should `docker restart` any node whose `/health` fails for >~1 min.** This is
  the recommended fleet auto-heal hook.
- **Don't hammer a *starting* node's `/health`** faster than ~1/sec during its ~5–12 s boot — a
  tight poll loop can wedge the worker. Use the Docker healthcheck cadence or a single check after a
  short wait.

---

## 7. Draining / pausing a node ("stop accepting jobs")

`/state.accepting` is reported but is **always `true`** today. There is **no built-in toggle yet**
to make a node reject new `/upload`s. To take a node out of rotation now:
1. stop dispatching to it (ignore it in candidate selection), and
2. optionally `POST /jobs/<id>/stop` its running jobs (re-dispatch elsewhere), then let it idle or
   `docker stop` it.

If you want the node to **enforce** "not accepting" itself (so the web UI is also blocked), that's a
small future endpoint (e.g. `POST /admin/accepting {false}` flipping `/state.accepting` and having
`/upload` return 503).

---

## 8. Security (must read)

v1.6.0 has **bearer-token auth**, with one sharp edge: it is enforced *only once a token exists*.

**A freshly deployed node has zero tokens and every route open to anyone who can reach the port.**
That is not a bug — it is how the first admin token gets minted without a bootstrap secret — but it
means minting that token is part of deploying the node, not a later hardening step:

```bash
curl -fsS -X POST http://NODE:6363/api/tokens \
     -H 'Content-Type: application/json' \
     -d '{"name":"ddn-dispatcher","scope":"submit"}'
```

- The plaintext is returned **exactly once**, at creation, and stored only as a SHA-256 hash. A lost
  token is replaced, never recovered.
- Scopes rank `read < submit < admin`. `/state` needs `read`; `/upload` needs `submit`;
  `/api/tokens` and `/api/audit` need `admin`.
- `/health` stays unauthenticated by design — a liveness probe must not depend on holding a token.
  Anything that health-checks `/state` instead will start returning 401 the moment a token exists.
- The token store lives in `/data/state`, so it survives container recreation. A node redeployed
  with a **fresh** data directory is unauthenticated again until you mint a token.
- `GET /api/audit` lists recent authenticated actions (`PREDPEP_AUDIT_KEEP`, default 500).

Network posture still matters: deploy on a private/trusted network, do not expose 6363 to the
internet, and bind to a private interface where you can (`run.sh` publishes `0.0.0.0:6363`).

---

## 9. Current limits / not-yet

- **Coarse progress only.** Status is `Queued/Running/Complete/Stopped/Failed` — there is **no
  fine-grained progress %** (e.g. "iteration 3/6") exposed yet. (The pipeline runs up to 6
  refinement iterations internally.)
- **No auth** (§8).
- **No accepting-toggle endpoint** (§7).
- **Status casing** differs between `/upload` (lowercase) and `/status`/`/jobs` (Title-case) —
  compare case-insensitively.
- **Single worker:** the node is `gunicorn -w 1` (gevent). Fine for the control API + one local
  scheduler; the heavy compute runs as separate OS processes, so the web layer isn't the bottleneck.

---

## 10. Quick reference (curl)

```bash
NODE=http://10.0.0.5:6363
curl -s $NODE/health
curl -s $NODE/state
JOB=$(curl -s -F protein_symbol=EGF -F user_name=ctl -F cpus=8 -F file1=@complex.pdb $NODE/upload | jq -r .job_id)
curl -s $NODE/status/$JOB                       # poll until status is terminal
curl -s -o result.zip $NODE/download/$JOB/$JOB.zip   # when Complete
curl -s -X POST $NODE/jobs/$JOB/stop            # cancel
curl -s -X DELETE $NODE/jobs/$JOB               # delete + reclaim disk
```

*Generated from the implemented endpoints in `app/predPEP.py` + `app/scheduler.py`. Update this doc
when the API changes.*
