# predPEP local

Flask + gunicorn backend for peptide design, running Rosetta + FoldX pipelines (CPU-only). It serves a JSON API for programmatic/orchestrator use **and** the original browser UI, both on port 6363 (`/` = UI, `/health` = liveness). It ships as a self-contained Docker image with an in-process, CPU-aware job queue.

## Prerequisites

- Docker 23+ (the Dockerfile uses `RUN --mount=type=bind`, which requires BuildKit — default in 23+)
- Building needs ~50 GB free transiently (Rosetta is extracted in full, then pruned in the same layer); the **resulting image is ~4.8 GB**, so machines that only *run* a pre-built image need ~7 GB. Final size is printed at the end of `./scripts/build.sh`.
- Port 6363 free on the host

CPU-only — no GPU, NVIDIA driver, or nvidia-container-toolkit required. Runs on any x86-64 Linux host with Docker.

## First-time build

```
./scripts/build.sh
```

Takes ~5–10 min on a fresh build (Rosetta tar extraction is the bottleneck). Full log captured to `build.log`. Subsequent rebuilds reuse the cached blob layer and finish in seconds unless that layer is invalidated.

## Versioning

The image version lives in the **`VERSION`** file (currently `v1.3.0`) — the single source of truth. `./scripts/build.sh` tags the build `predpep:$(cat VERSION)` **and** `predpep:latest`; `run.sh` / `run-dev.sh` launch the versioned tag. To cut a new release, bump `VERSION` (and the `image:` pin in `docker-compose.yml`), rebuild, and re-export the tarball. Commands below hard-code `v1.3.0`; substitute the current version.

## Daily use

```
./scripts/run.sh
```

Launches `predpep_app` detached with `--restart unless-stopped`. Open the **web UI** at http://localhost:6363/ , or use the JSON API (`curl http://localhost:6363/health`). See [Web UI](#web-ui) below.

## Web UI

The node serves the original browser interface at **http://&lt;host&gt;:6363/** alongside the JSON API — open it in a browser to submit a job (protein symbol, user name, PDB upload, CPU count) and view results (score table, NGL structure viewer, Plotly plots). It's **baked into every image**, so it's available on each deployed machine with no extra steps or flags.

A **Jobs** tab lists every job on the machine (date, submission details, status, a download link, and a delete button) — persisted on a Docker volume so they survive page reloads and container restarts. Deleting a job removes its files from disk (use it to reclaim space). No login: all jobs are visible to anyone who can reach the node.

- **The browser needs internet access.** The page loads NGL + Plotly from public CDNs (`cdn.jsdelivr.net`, `cdn.plot.ly`); only the small app scripts (plus a vendored `static/js/ngl.umd.js`, currently unused) are served locally. The backend itself (job execution) does **not** need internet.
- `/` serves the UI; `/health` returns JSON (`{"service":"predpep-node","status":"ok"}`) for liveness checks — used by a controller and the Docker healthcheck.
- The **TMAP tree tab is non-functional** by design (see [Known limitations](#known-limitations)).

## Tuning & reliability (env vars on `scripts/run.sh`)

- `PREDPEP_CORE_BUDGET` — max CPU cores the node will commit across running jobs (default: the machine's core count). Web-submitted jobs reserve their CPU count and queue when the budget is full.
- `PREDPEP_RETENTION_BYTES` / `PREDPEP_RETENTION_DAYS` — job-storage caps (default 50 GB / 180 days). Oldest finished jobs are evicted first; completed jobs keep only their result `.zip`.
- `PREDPEP_MEMORY` — optional Docker memory cap (e.g. `PREDPEP_MEMORY=32g`), passed to `--memory`.
- **Auto-heal:** the gunicorn worker preloads the app to avoid the fork-time boot wedge; for an unattended fleet, also run a restarter that reacts to `health=unhealthy` (e.g. the `willfarrell/autoheal` sidecar, or have your orchestrator `docker restart` a node whose `/health` fails).

## Code iteration

```
./scripts/run-dev.sh
```

Launches `predpep_app_dev` with `app/` and `pipeline/` bind-mounted from the host. Edit files locally and reload:

- `.py` changes → `docker restart predpep_app_dev`
- HTML/JS in `templates/` and `static/` → picked up on the next request

Uses the same port 6363, so it can't coexist with `predpep_app` — stop the other one first.

## Stopping / updating / tearing down

```
docker stop predpep_app                                     # graceful stop
docker rm predpep_app                                       # remove the stopped container
docker rm -f predpep_app                                    # stop and remove in one go
./scripts/build.sh && docker rm -f predpep_app && ./scripts/run.sh    # rebuild and relaunch
```

## Docker Compose alternative

Equivalent to `scripts/run.sh` (no bind mounts):

```
docker compose up -d
docker compose down
```

Compose and `run.sh` both create a container named `predpep_app` on port 6363 — they can't coexist. Pick one; if you've started the container via `scripts/run.sh`, stop it before `docker compose up -d` (and vice versa).

## Distributing the image (deploying machine-by-machine)

The built image is self-contained (~4.8 GB) — to roll it out to other machines without each one rebuilding (or needing the build-time tool blobs), copy the image directly. `predpep:v1.3.0` already includes everything (scheduler, UI, all fixes); no rebuild is needed on the source machine.

**Over SSH, no temp files (simplest):**

```
docker save predpep:v1.3.0 | gzip | ssh USER@TARGET 'gunzip | docker load'
```

**Or via a file with resume (better for flaky links):**

```
docker save predpep:v1.3.0 | gzip > predpep-v1.3.0.tgz
rsync -P predpep-v1.3.0.tgz USER@TARGET:        # -P = progress + resume
ssh USER@TARGET 'docker load < predpep-v1.3.0.tgz && rm predpep-v1.3.0.tgz'
```

Then launch the node on the target (the browser UI is baked in and served at `/`):

```
docker run -d --name predpep_app \
  -v predpep_data:/tmp/pepspec \
  --log-opt max-size=10m --log-opt max-file=3 --pids-limit 4096 \
  -p 6363:6363 --restart unless-stopped predpep:v1.3.0
```

The `-v predpep_data:/tmp/pepspec` volume is **required for jobs to persist** across restarts. Targets need only Docker (the SSH user must be able to run it) and ~7 GB free disk — no GPU, no build toolchain, no blobs. A private registry works too (`docker push`/`pull`). For the controller-side integration + control API, see [docs/INTEGRATION.md](docs/INTEGRATION.md).

## Known limitations

- **TMAP tree-view tab is non-functional by design.** `tmap_utils.py` needs `mhfp` plus a working `tmap`/`libOGDF`, none of which are present; predPEP.py falls back to a no-op via `except ImportError`. The slim image goes further and **drops `tmap`/`faerun` and their matplotlib GUI/LLVM backend entirely** (~0.64 GB saved), so the feature is not implemented.
- **Job data persists on a Docker volume** (`predpep_data`, mounted at `/tmp/pepspec`). It survives container recreate/redeploy; back it up with `docker run --rm -v predpep_data:/data -v "$PWD":/backup busybox tar czf /backup/predpep_data.tgz -C /data .`. Removing the volume (`docker volume rm predpep_data`) erases all job history.
- **FoldX binary is bundled** at `/usr/local/foldx26Linux64_0/`. Covered by an academic license — do **not** publicly redistribute this image.
- **Rosetta binaries are bundled** at `/usr/local/rosetta_pkgs/`. Same academic-license constraint.
- The gevent worker needs a few seconds after container start to fork and import the Flask app. The Dockerfile healthcheck has `--start-period=90s` to cover this; the UI itself is typically serving within ~5 seconds.

## When things break

- Is the container running? `docker ps | grep predpep`
- Healthcheck state: `docker inspect --format '{{.State.Health.Status}}' predpep_app`
- Live logs: `docker logs -f predpep_app`
- Poke around inside: `docker exec -it predpep_app bash`
- Startup import errors: grep the log for `Traceback` or `ImportError`. The conda env at `/home/spacepep/miniforge3/envs/predPEP/` is baked into the image — adding packages requires rebuilding, not a runtime install.
