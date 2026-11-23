# predPEP local

Flask + gunicorn backend for peptide design, running Rosetta + FoldX pipelines (CPU-only). It serves a JSON API for programmatic/orchestrator use **and** the browser UI, both on host port 6363 (`/` = UI, `/health` = liveness). It ships as a self-contained Docker image with an in-process, CPU-aware job queue.

**This is v1.6.0** — the upstream application as delivered by the dev on 18 November 2026, rebuilt here as a self-contained fleet node. It designs with non-canonical and D-amino acids (397 offerable residues), runs the non-canonical design pass automatically after the iterations, scores with FoldX Pro, and enforces bearer-token auth once a token exists.

Upgrading a node from v1.4.0? **Read [docs/UPGRADE-v1.6.0-node.md](docs/UPGRADE-v1.6.0-node.md) first** — the data directory moved to `/data`, the node must be drained before redeploy, and a dispatcher that submits discontinuous chains needs a one-branch change or it will silently under-count jobs. The dev's own notes are vendored under [docs/upstream-v1.6.0/](docs/upstream-v1.6.0/).

## Deploy on your machine (no build needed)

You only need **Docker** and the pre-built image — no building, no 23 GB of tool blobs.

1. **Get this repo** (for the deploy script):
   ```
   git clone https://forgejo.lan.peptide.space/SpacePeptidesDev/PredPEP.git
   cd PredPEP
   ```
2. **Get the image and launch**, either:
   - **From the company registry** (recommended) — the script pulls it for you, then runs:
     ```
     ./scripts/deploy.sh forgejo.lan.peptide.space/spacepeptidesdev/predpep:v1.6.0-node
     ```
   - **or from a tarball** someone shared:
     ```
     docker load < predpep-v1.6.0-node.tgz
     ./scripts/deploy.sh
     ```
3. Open **http://localhost:6363/**

Your job data lives in `~/predpep_data` (override with `PREDPEP_DATA=...`) — a host directory that **survives container updates and docker cleanups**. **To update later:** get the newer image and re-run `./scripts/deploy.sh`; your data is preserved.

> Building/releasing the image yourself? See [Prerequisites](#prerequisites), [First-time build](#first-time-build), and [Distributing the image](#distributing-the-image-deploying-machine-by-machine) below.

## Prerequisites

- Docker 23+ (the Dockerfile uses `RUN --mount=type=bind`, which requires BuildKit — default in 23+)
- The image is built on **ubuntu:24.04**, matching the dev's v1.6.0 base. Not cosmetic: `libOGDF` (which `tmap` links against) needs GLIBC 2.38, and on 22.04 `import tmap` fails — silently, because predPEP.py catches it and disables the similarity tree.
- Building needs ~50 GB free transiently (Rosetta is extracted in full, then pruned in the same layer); the **resulting image is 5.53 GB** (v1.4.0 was 4.82 GB — the extra is `rosetta_scripts` for the non-canonical pass, the 305 MB residue library, FoldX Pro and the full analytics stack), so machines that only *run* a pre-built image need ~8 GB. Final size is printed at the end of `./scripts/build.sh`.
- Port 6363 free on the host

CPU-only — no GPU, NVIDIA driver, or nvidia-container-toolkit required. Runs on any x86-64 Linux host with Docker.

## First-time build

```
./scripts/build.sh
```

Takes ~5–10 min on a fresh build (the Rosetta tar extraction alone is ~4 min and is the bottleneck). Full log captured to `build-v160.log`.

The build verifies itself rather than trusting the blobs: the Rosetta/FoldX/residue files the pipeline needs must exist, every live dependency must import, every pipeline shebang must name an interpreter the image has, and the residue catalogue read inside the image must match the build host's counts (`noncanonical 397`, `retired 6`, `d_offerable 208`, `foldx_scorable 2`). That last check is the dev's `verify.sh` folded into the build, so a wrong image cannot be produced in the first place. Subsequent rebuilds reuse the cached blob layer and finish in seconds unless that layer is invalidated.

## Versioning

The image version lives in the **`VERSION`** file (currently `v1.6.0`) — the single source of truth. `./scripts/build.sh` tags the build `predpep:$(cat VERSION)` **and** `predpep:latest`; `run.sh` / `run-dev.sh` launch the versioned tag. To cut a new release, bump `VERSION` (and the `image:` pin in `docker-compose.yml`), rebuild, and re-export the tarball. Commands below hard-code `v1.6.0`; substitute the current version.

## Daily use

```
./scripts/run.sh
```

Launches `predpep_app` detached with `--restart unless-stopped`. Open the **web UI** at http://localhost:6363/ , or use the JSON API (`curl http://localhost:6363/health`). See [Web UI](#web-ui) below.

## Web UI

The node serves the browser interface at **http://&lt;host&gt;:6363/** alongside the JSON API — submit a job (protein symbol, user name, PDB upload, CPU count, design-residue palette) and view results: score table, NGL structure viewer, analytics panels (convergence, FoldX-vs-Rosetta agreement, term correlation, PCA, sequence map, residue logo, sampling), an ops console, and token admin.

This is upstream's own UI, taken whole in the v1.6.0 merge; our minimal `tab*.js` set from the headless-node era is gone. It is **baked into every image**, so it is available on each deployed machine with no extra steps.

A **Jobs** view lists every job on the machine (date, submission details, iteration count, status, download, delete, stop). Jobs persist in the `/data` bind-mount, so they survive page reloads, container restarts and redeploys. Deleting a job removes its files from disk.

- **NGL and Plotly are served locally** from `static/js/` — the page no longer depends on a public CDN. The backend never needs internet.
- `/` serves the UI; `/health` returns JSON including the node version, unauthenticated by design (a liveness probe must not depend on holding a token) — used by the dispatcher and the Docker healthcheck.
- **Auth:** no token exists on a fresh node and every route is open, which is how the first admin token is minted (`POST /api/tokens`). From the moment one token exists, every protected route requires one. Mint it immediately — see [docs/UPGRADE-v1.6.0-node.md](docs/UPGRADE-v1.6.0-node.md).

## Tuning & reliability (env vars on `scripts/run.sh`)

- `PREDPEP_CORE_BUDGET` — max CPU cores the node will commit across running jobs (default: the machine's core count). Web-submitted jobs reserve their CPU count and queue when the budget is full.
- `PREDPEP_RETENTION_BYTES` / `PREDPEP_RETENTION_DAYS` — job-storage caps, **default 50 GiB / 90 days, whichever is hit first**. The sweep runs every 5 minutes: it evicts terminal jobs past the age limit, then, if the total is still over the size cap, evicts oldest-first until it is under. Eviction **archives and verifies** the job into `PREDPEP_ARCHIVE_DIR` before removing it, so the result `.zip` survives — but the job leaves the Jobs list. A job that cannot be archived is kept and retried next sweep. Running and queued jobs are never touched.
- `PREDPEP_MEMORY` — optional Docker memory cap (e.g. `PREDPEP_MEMORY=32g`), passed to `--memory`.
- **Baked into the image, not run-line options** — `PREDPEP_NCAA_WEIGHTS=talaris2013_split_unfolded.wts`, `PREDPEP_NCAA_TOP=10`, `PREDPEP_NCAA_TIMEOUT=14400`, `PREDPEP_NCAA_CONTROL=1`, and the Rosetta/FoldX/residue-library paths. The weights one **changes results**: unset, the non-canonical pass scores through talaris' `ref` term, which is indexed by the AA enum and so charges every non-canonical some canonical residue's reference energy. It is in the Dockerfile precisely so no hand-written `docker run` line can omit it.
- `PREDPEP_D_RESIDUES=0`, `PREDPEP_ALLOW_RETIRED=1`, `PREDPEP_ALLOW_ROSETTA_NCAA=1` — residue-palette switches. All three default to the safe setting and each reports its reason rather than silently omitting a code.
- `PREDPEP_PARTIAL_STATUS` — set to `1` to let `/status` and `/jobs` emit the distinct **`Partial`**
  status for a job whose final score aggregation failed. **Default off:** partial jobs report
  `Complete` with `partial: true` + `failed_stage`, so existing consumers are unaffected. Only
  enable it once every consumer treats `Partial` as terminal — a client that maps unknown statuses
  to *running* would otherwise poll a finished job forever. See
  [docs/incident-SPILRH_c70d2d4c.md](docs/incident-SPILRH_c70d2d4c.md).
- **Auto-heal:** for an unattended fleet, run a restarter that reacts to `health=unhealthy` (e.g. the `willfarrell/autoheal` sidecar, or have your orchestrator `docker restart` a node whose `/health` fails). Note that `preload_app` is now **off** — upstream's `gunicorn.conf.py` is used verbatim, and with a single worker preloading only moved the scheduler greenlet into a process that does not survive a worker restart. Give the node a few seconds after start and check `/health` once; polling it in a tight loop during the fork is what used to wedge the gevent worker.

## Code iteration

```
./scripts/run-dev.sh
```

Launches `predpep_app_dev` with `app/` (at `/opt/spacepep-predPEP`) and `pipeline/` bind-mounted from the host. Edit files locally and reload:

- `.py` changes → `docker restart predpep_app_dev`
- HTML/JS in `templates/` and `static/` → picked up on the next request

Uses the same host port 6363, so it can't coexist with `predpep_app` — stop the other one first, or `HOST_PORT=6364 ./scripts/run-dev.sh`. Host-side pipeline scripts must keep their executable bit: the image's `/usr/local/bin` symlinks resolve through the bind-mount to your files.

### Verifying a node

```
./scripts/verify.sh [container] [host-port]      # defaults: predpep_app 6363
```

Answers "is this machine like the others?" — catalogue counts, the binaries the
pipeline shells out to (resolved **as the app user**, which is what actually runs them),
the FoldX binary's identity (size + hash, so a node quietly scoring on a different FoldX
build is caught at deploy time instead of from one line in a job log), whether auth is
enforced, and whether anything is Running before you redeploy. Non-zero exit on any failure.

### Tests

```
python3 -m pytest tests/ -q
```

Runs on the host — needs only `pandas` + `pytest`, no Flask and no container. The suite is the
regression net for the `SPILRH_c70d2d4c` incident (corrupted `.spec` handling, degraded score
tables, and honest job status); see [docs/incident-SPILRH_c70d2d4c.md](docs/incident-SPILRH_c70d2d4c.md).
It writes throwaway job dirs under `/tmp/pepspec/results/SPTEST_*` and removes them afterwards.

## Stopping / updating / tearing down

```
docker stop predpep_app                                     # graceful stop
docker rm predpep_app                                       # remove the stopped container
docker rm -f predpep_app                                    # stop and remove in one go
./scripts/build.sh && docker rm -f predpep_app && ./scripts/run.sh    # rebuild and relaunch
```

## Docker Compose alternative

Equivalent to `scripts/deploy.sh` — same **host bind-mount** for job data, so it's deletion-proof:

```
docker compose up -d        # data -> ./predpep_data  (override: PREDPEP_DATA=/path docker compose up -d)
docker compose down         # stops the node; the data directory is NOT removed
```

Job data lives in a **host directory** (`./predpep_data` by default), so `docker compose down -v`, `docker volume prune`, and `docker system prune --volumes` can't wipe it — only `rm -rf` on that path. A one-shot init service makes the dir writable by the app on first `up` (needs Docker Compose v2). Compose, `deploy.sh`, and `run.sh` all create the container `predpep_app` on port 6363 — they can't coexist, so pick one. If you use both `deploy.sh` and compose on the same host, point `PREDPEP_DATA` at the **same** directory so they share job history.

## Distributing the image (deploying machine-by-machine)

The built image is self-contained (~4.8 GB) — to roll it out to other machines without each one rebuilding (or needing the build-time tool blobs), copy the image directly. `predpep:v1.6.0-node` already includes everything (scheduler, UI, all fixes); no rebuild is needed on the source machine.

**Over SSH, no temp files (simplest):**

```
docker save predpep:v1.6.0-node | gzip | ssh USER@TARGET 'gunzip | docker load'
```

**Or via a file with resume (better for flaky links):**

```
docker save predpep:v1.6.0-node | gzip > predpep-v1.6.0-node.tgz
rsync -P predpep-v1.6.0-node.tgz USER@TARGET:        # -P = progress + resume
ssh USER@TARGET 'docker load < predpep-v1.6.0-node.tgz && rm predpep-v1.6.0-node.tgz'
```

Then launch the node on the target (the browser UI is baked in and served at `/`):

```
docker run -d --name predpep_app \
  -v predpep_data:/data \
  --log-opt max-size=10m --log-opt max-file=3 --pids-limit 4096 \
  -p 6363:8383 --restart unless-stopped predpep:v1.6.0-node
```

The `-v predpep_data:/data` volume is **required for jobs to persist** across restarts, and the port mapping is `6363:8383` — the container binds upstream's 8383 while the host keeps the 6363 the dispatcher dials. Targets need only Docker (the SSH user must be able to run it) and ~8 GB free disk — no GPU, no build toolchain, no blobs. A private registry works too (`docker push`/`pull`). For the controller-side integration + control API, see [docs/INTEGRATION.md](docs/INTEGRATION.md).

## Known limitations

- **The conda environment is the dev's, not ours.** It is lifted out of `predpep:v1.6.0-node` (python 3.12) rather than rebuilt from the old 3.10 blob, because the v1.6.0 analytics import `sklearn`, `seaborn` and `mhfp`, and the old environment had none of them — the panels would have come up empty on nodes and nowhere else, without an error. `tmap` is present too, so the similarity tree is no longer a dead feature. Only the conda `pkgs/` download cache (984 MB) and build cruft are stripped.
- **Job data persists in a host bind-mount** at `/data` (`results/`, `uploads/`, `archive/`, `state/`). It survives container recreate/redeploy and every `docker volume prune` variant. v1.4.0 used `/tmp/pepspec` — see [docs/UPGRADE-v1.6.0-node.md](docs/UPGRADE-v1.6.0-node.md) before upgrading a node.
- **A node must be drained before redeploy.** `docker rm -f` SIGKILLs the pipeline with no grace period, and an orphaned job is reconciled to *failed* on the next boot, never resumed. `scripts/deploy.sh` now refuses while `/jobs` shows `Running`/`Queued` (`FORCE=1` overrides).
- **FoldX Pro is bundled** at `/usr/local/foldxpro_20261231/` (binary + `rotabase.txt` + `molecules/`; the 13 GB of unrelated databases that sit beside it on the group server are not included). Licensed — do **not** publicly redistribute this image. Scores are **not comparable** with v1.4.0's FoldX 5 output.
- **Rosetta binaries are bundled** at `/usr/local/rosetta_pkgs/`. Same academic-license constraint.
- The gevent worker needs a few seconds after container start to fork and import the Flask app. The Dockerfile healthcheck has `--start-period=90s` to cover this; the UI itself is typically serving within ~5 seconds.

## When things break

- Is the container running? `docker ps | grep predpep`
- Healthcheck state: `docker inspect --format '{{.State.Health.Status}}' predpep_app`
- Live logs: `docker logs -f predpep_app`
- Poke around inside: `docker exec -it predpep_app bash`
- Startup import errors: grep the log for `Traceback` or `ImportError`. The conda env at `/home/spacepep/miniforge3/envs/predPEP/` is baked into the image — adding packages requires rebuilding, not a runtime install.
