# predPEP Node — Integration Guide

**Audience:** anyone building a central **controller / orchestrator** (a dispatcher that deploys,
monitors, and assigns jobs to one or more predPEP compute nodes across machines — including a node
on the controller's own host).
**Node status:** fleet-deployable for **trusted/private networks**. There is **no authentication
yet** (a deferred security phase) — see §8.

---

## 1. Model in one paragraph

A **predPEP node** is a single self-contained Docker image (`predpep:local`, ~7 GB, CPU-only,
`ubuntu:22.04` base) that runs a Flask/gunicorn service on **port 6363**. It accepts a
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
docker save predpep:local | gzip | ssh USER@TARGET 'gunzip | docker load'
```
(A private registry works too: `docker push`/`pull`. Do not push to a public registry — the image
bundles academic-licensed Rosetta/FoldX.) See the README "Distributing the image" section for the
resumable-via-file variant.

**Run a node** (this is what `scripts/run.sh` does):
```bash
docker run -d --name predpep_app \
  -e PREDPEP_CORE_BUDGET=32 \                  # optional; default = machine cores
  -v predpep_data:/tmp/pepspec \               # persistent job storage (named volume) — REQUIRED
  --log-opt max-size=10m --log-opt max-file=3 \
  --pids-limit 4096 \
  -p 6363:6363 \
  --restart unless-stopped \
  predpep:local
```
- **Persistent volume `predpep_data:/tmp/pepspec`** holds all jobs (metadata, results, zips). It
  **survives container recreate/redeploy** — each machine keeps its own job history.
- **Healthcheck:** the image has a Docker `HEALTHCHECK` polling `/health` every 30 s.
- **No GPU / driver / toolkit** required.

**Update a node to a new image version** (jobs preserved via the volume):
```bash
docker load < predpep-local-NEW.tgz             # or registry pull
docker rm -f predpep_app && docker run -d ... predpep:local   # volume re-attaches, jobs persist
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
e.g. `SPEGFT_99b60e04`. No auth headers (yet).

### Liveness / capacity (poll these)
| Method | Path | Returns |
|---|---|---|
| `GET` | `/health` | `{"service":"predpep-node","status":"ok"}` — JSON liveness probe (use this, not `/`) |
| `GET` | `/` | the browser **UI** (HTML) — not for the controller; for liveness use `/health` |
| `GET` | `/state` | capacity + disk — **the dispatch signal** (below) |

`GET /state` →
```json
{
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

### Submit a job
`POST /upload` — `multipart/form-data`:
| field | type | notes |
|---|---|---|
| `protein_symbol` | string | required; first 3 letters used in the job id |
| `user_name` | string | required; first letter used in the job id |
| `cpus` | int | cores to reserve; server-clamps to `[2, min(32, core_budget)]` |
| `file1` | file | the complex PDB; **peptide must be chain B** |

Response:
```json
{ "success": true, "job_id": "SPEGFT_99b60e04",
  "status": "running",          // or "queued"
  "queue_position": 0,          // jobs ahead of it in the FIFO queue
  "message": "Job SPEGFT_99b60e04 running." }
```
The node reserves `cpus` for the job's **entire lifetime** and starts it only if
`reserved_cores + cpus ≤ core_budget`; otherwise it sits **queued** (FIFO) and starts automatically
when cores free up. On bad input: `{"success": false, "error": "..."}`.

### Track a job
| Method | Path | Returns |
|---|---|---|
| `GET` | `/status/<job_id>` | `{"status": "...", "download_url": "..."|null, "message": "..."}` |
| `GET` | `/jobs` | `{"success": true, "jobs": [ {...}, ... ]}` newest-first (all jobs on the node) |

`/jobs` entries:
```json
{ "job_id": "...", "submitted_at": "2026-06-13T...Z", "protein_symbol": "EGF",
  "user_name": "alice", "cpus": 8, "pdb_filename": "SPEGFA.pdb", "peptide_length": 13,
  "status": "Running", "download_url": "/download/<id>/<id>.zip" | null }
```
**Status values** (see §4): `Queued`, `Running`, `Complete`, `Stopped`, `Failed`
(and `Pending/Failed` from `/status` when the job dir is absent). ⚠️ **Casing wrinkle:** `/upload`
returns lowercase (`"running"`/`"queued"`); `/status` and `/jobs` return Title-case. **Compare
case-insensitively.**

### Get the result
`GET <download_url>` → the result `.zip` (binary; `Content-Type: application/zip`). Only present
when `status == Complete`.

### Control a job
| Method | Path | Effect |
|---|---|---|
| `POST` | `/jobs/<job_id>/stop` | SIGKILL the pipeline (or dequeue if queued); status→`Stopped`, cores freed. `{"success":true,"stopped":"<id>"}` |
| `DELETE` | `/jobs/<job_id>` | stop (if running) **and delete** its result + upload dirs (reclaim disk). `{"success":true,"deleted":"<id>"}` |

Both validate the id (reject `/`, `..`); `404` if unknown.

### UI-only / deprecated (a controller can ignore)
`GET /results_data/<job_id>` (FoldX/Rosetta CSV+text for the browser viewer),
`GET /stream_final_pdb/<job_id>/<path>` (PDB streaming for the viewer),
`GET /get_tmap_tree/<job_id>` (**non-functional** — `mhfp` not in the env; returns empty).

---

## 4. Job lifecycle & status model

`job.json["status"]` on the node is the source of truth; the node's scheduler owns it:

```
Queued ──(cores free)──> Running ──> Complete   (zip produced; node keeps only the .zip)
                                  └─> Stopped    (POST /stop, or DELETE)
                                  └─> Failed     (manager exited without a zip, OR
                                                   interrupted by a node restart)
```
- **Restart reconciliation:** on container/node restart, a job that was `Running` becomes
  **`Failed`** (its subprocess died with the container) and a job that was `Queued` is **re-queued**
  automatically. So a restart never leaves a job stuck "Processing." **Treat `Failed` as
  "re-dispatch if still wanted."**
- **Terminal states:** `Complete`, `Stopped`, `Failed`. Poll until one of these.
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

**There is no authentication or authorization.** Any host that can reach `:6363` can submit, list,
stop, and delete jobs, and download any result. Therefore:
- Deploy nodes on a **private/trusted network only**; do **not** expose `6363` to the internet.
- The controller↔node channel must be network-isolated (VPN/overlay/firewall) until the planned
  **security phase** adds auth (e.g. a shared bearer token or mTLS between controller and nodes).
- Bind to a private interface if possible (today `run.sh` publishes `0.0.0.0:6363`; restrict via
  host firewall or change the publish to a private IP).

---

## 9. Current limits / not-yet

- **No node version** is reported yet — there's no field to compare a node's installed image
  against the latest for update decisions (planned: a `version` in `/state`).
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
