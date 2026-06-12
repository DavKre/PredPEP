# predPEP local

Flask + gunicorn backend for peptide design, running Rosetta + FoldX pipelines (CPU-only). It serves a JSON API for programmatic/DDN use **and** the original browser UI, both on port 6363 (`/` = UI, `/health` = liveness). Rebuilt from artifacts extracted from the production image `predpep:v2`. For the extraction story, blob inventory, and historical context, see [HANDOFF.md](HANDOFF.md).

## Prerequisites

- Docker 23+ (the Dockerfile uses `RUN --mount=type=bind`, which requires BuildKit — default in 23+)
- Building needs ~50 GB free transiently (Rosetta is extracted in full, then pruned in the same layer); the **resulting image is ~8 GB**, so machines that only *run* a pre-built image need ~10 GB. Final size is printed at the end of `./scripts/build.sh`.
- Port 6363 free on the host

CPU-only — no GPU, NVIDIA driver, or nvidia-container-toolkit required. Runs on any x86-64 Linux host with Docker.

## First-time build

```
./scripts/build.sh
```

Takes ~5–10 min on a fresh build (Rosetta tar extraction is the bottleneck). Full log captured to `build.log`. Subsequent rebuilds reuse the cached blob layer and finish in seconds unless that layer is invalidated.

## Daily use

```
./scripts/run.sh
```

Launches `predpep_app` detached with `--restart unless-stopped`. Open the **web UI** at http://localhost:6363/ , or use the JSON API (`curl http://localhost:6363/health`). See [Web UI](#web-ui) below.

## Web UI

The node serves the original browser interface at **http://&lt;host&gt;:6363/** alongside the JSON API — open it in a browser to submit a job (protein symbol, user name, PDB upload, CPU count) and view results (score table, NGL structure viewer, Plotly plots). It's **baked into every image**, so it's available on each deployed machine with no extra steps or flags.

A **Jobs** tab lists every job on the machine (date, submission details, status, a download link, and a delete button) — persisted on a Docker volume so they survive page reloads and container restarts. Deleting a job removes its files from disk (use it to reclaim space). No login: all jobs are visible to anyone who can reach the node.

- **The browser needs internet access.** The viewer libraries (NGL, Plotly) are loaded from public CDNs (`cdn.jsdelivr.net`, `cdn.plot.ly`); only the small app scripts are served locally. The backend itself (job execution) does **not** need internet.
- `/` serves the UI; `/health` returns JSON (`{"service":"predpep-node","status":"ok"}`) for liveness checks — used by DDN and the Docker healthcheck.
- The **TMAP tree tab is non-functional** by design (see [Known limitations](#known-limitations)) — matches production.

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

The built image is self-contained (~7 GB) — to roll it out to other machines without each one rebuilding (or needing the 23 GB of blobs), copy the image directly:

```
# On the build host:
docker save predpep:local | gzip > predpep-local.tar.gz      # ~3–4 GB compressed

# Copy predpep-local.tar.gz to each target, then on the target:
docker load < predpep-local.tar.gz
docker run -d --name predpep_app -p 6363:6363 --restart unless-stopped predpep:local
```

Targets need only Docker and ~10 GB free disk — no GPU, no build toolchain, no blobs. (A private registry works too: `docker push`/`pull` instead of save/load.)

## Known limitations

- **TMAP tree-view tab is non-functional by design.** `tmap_utils.py` requires the `mhfp` package, which isn't in the conda env. predPEP.py falls back to a no-op via `except ImportError`. Matches production behavior — the feature is not yet implemented.
- **Job data persists on a Docker volume** (`predpep_data`, mounted at `/tmp/pepspec`). It survives container recreate/redeploy; back it up with `docker run --rm -v predpep_data:/data -v "$PWD":/backup busybox tar czf /backup/predpep_data.tgz -C /data .`. Removing the volume (`docker volume rm predpep_data`) erases all job history.
- A job interrupted by a container restart shows **"Processing"** indefinitely (no run-state tracking yet); deleting a *running* job removes its files and its background run then fails. Both are resolved by the planned job-queue work.
- **FoldX binary is bundled** at `/usr/local/foldx26Linux64_0/`. Covered by an academic license — do **not** publicly redistribute this image.
- **Rosetta binaries are bundled** at `/usr/local/rosetta_pkgs/`. Same academic-license constraint.
- The gevent worker needs a few seconds after container start to fork and import the Flask app. The Dockerfile healthcheck has `--start-period=90s` to cover this; the UI itself is typically serving within ~5 seconds.

## When things break

- Is the container running? `docker ps | grep predpep`
- Healthcheck state: `docker inspect --format '{{.State.Health.Status}}' predpep_app`
- Live logs: `docker logs -f predpep_app`
- Poke around inside: `docker exec -it predpep_app bash`
- Startup import errors: grep the log for `Traceback` or `ImportError`. The conda env at `/home/spacepep/miniforge3/envs/predPEP/` is baked into the image — adding packages requires rebuilding, not a runtime install.
