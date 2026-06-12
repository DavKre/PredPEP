# predPEP local

Flask + gunicorn web UI for peptide design, running Rosetta + FoldX pipelines. Rebuilt from artifacts extracted from the production image `predpep:v2` on the shared GPU server. For the extraction story, blob inventory, and historical context, see [HANDOFF.md](HANDOFF.md).

## Prerequisites

- NVIDIA GPU, driver 550+ (for CUDA 12.4 compatibility)
- `nvidia-container-toolkit` installed, `nvidia` runtime registered in Docker
- Docker 23+ (the Dockerfile uses `RUN --mount=type=bind`, which requires BuildKit — default in 23+)
- ~60 GB free disk under Docker's storage root (final image is ~64 GB)
- Port 6363 free on the host

## First-time build

```
./scripts/build.sh
```

Takes ~5–10 min on a fresh build (Rosetta tar extraction is the bottleneck). Full log captured to `build.log`. Subsequent rebuilds reuse the cached blob layer and finish in seconds unless that layer is invalidated.

## Daily use

```
./scripts/run.sh
```

Launches `predpep_app` detached with GPU access and `--restart unless-stopped`. Open http://localhost:6363.

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

## Known limitations

- **TMAP tree-view tab is non-functional by design.** `tmap_utils.py` requires the `mhfp` package, which isn't in the conda env. predPEP.py falls back to a no-op via `except ImportError`. Matches production behavior — the feature is not yet implemented.
- **FoldX binary is bundled** at `/usr/local/foldx26Linux64_0/`. Covered by an academic license — do **not** publicly redistribute this image.
- **Rosetta binaries are bundled** at `/usr/local/rosetta_pkgs/`. Same academic-license constraint.
- The gevent worker needs a few seconds after container start to fork and import the Flask app. The Dockerfile healthcheck has `--start-period=90s` to cover this; the UI itself is typically serving within ~5 seconds.

## When things break

- Is the container running? `docker ps | grep predpep`
- Healthcheck state: `docker inspect --format '{{.State.Health.Status}}' predpep_app`
- Live logs: `docker logs -f predpep_app`
- Poke around inside: `docker exec -it predpep_app bash`
- GPU visible to the app: `docker exec predpep_app nvidia-smi` (if that fails, the nvidia runtime isn't configured — check `docker info | grep Runtimes`)
- Startup import errors: grep the log for `Traceback` or `ImportError`. The conda env at `/home/spacepep/miniforge3/envs/predPEP/` is baked into the image — adding packages requires rebuilding, not a runtime install.
