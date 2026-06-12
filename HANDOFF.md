# predPEP Local Deployment — Handoff

> **Note (2026-06-12):** the repository was restructured — `sp-predPEP/`→`app/`,
> `pepspec_pipe/`→`pipeline/`, and the helper scripts moved into `scripts/`. In-image
> paths are unchanged. This document is kept as a historical record of the extraction;
> see `README.md` for current paths and commands.

This folder contains everything extracted from a running production Docker container on a shared GPU server, for the purpose of rebuilding the same tool as a clean, reproducible Docker image on a local workstation (and later deploying to other machines).

## What the tool is

`predPEP` is a **peptide design / prediction web application**. Architecture:

- **Flask + gunicorn web UI** (`predPEP.py`) listens on port 6363, accepts job submissions via browser.
- On submission, it calls `/usr/local/bin/run_iteMAN.py` (via `subprocess.Popen`), which is the asynchronous **iterative manager** script.
- `run_iteMAN.py` orchestrates a pipeline of bash + Python scripts (in `/usr/local/pepspec_pipe/`) that run **Rosetta** macromolecular modeling and **FoldX** energy calculations, iteratively refining peptide designs. Outputs are zipped result bundles (`SPPDCK_*.zip`).
- JS-based visualization in the browser uses **Mol\***, **NGL Viewer**, and **TMAP / faerun** for molecular + chemical-space views.

Typical job: user submits peptide sequence + PDB structure → pipeline runs several Rosetta + FoldX iterations → results zipped for download.

## What was in the production image

Original image: `predpep:v2`, ~77 GB total (NVIDIA CUDA 12.4 on Ubuntu 22.04 base). It was **hand-built interactively** (not from a Dockerfile — discovered via `docker history` showing 56.7 GB in a single `/bin/bash` layer followed by `docker commit` layers). No original Dockerfile exists. This handoff is the first time a reproducible build is being created.

Container runtime config (from `docker inspect`):

- User inside container: `spacepep`, UID/GID **1003** (important — conda env paths are baked to this user's home)
- Working dir: `/opt/sp-predPEP`
- Exposed port: **6363/tcp**
- GPU access: `--gpus all` (NVIDIA runtime, `NVIDIA_VISIBLE_DEVICES=all`)
- Env vars: `FLASK_APP=predPEP.py`, `HOME=/home/spacepep`, `PATH` includes `/home/spacepep/miniforge3/condabin`
- Entry command: `mamba run -n predPEP gunicorn -w 1 -b 0.0.0.0:6363 --worker-class=gevent --timeout 0 --capture-output --log-level debug --access-logfile - --error-logfile - predPEP:predPEP`
- Healthcheck in original: `curl --fail http://localhost:6363/ || exit 1` — **but curl is not installed in the image**, so it was permanently "unhealthy". We should install curl or pick a different healthcheck.

## What's in this folder

```
predpep_local/
├── HANDOFF.md                      # this file
├── blobs/                          # 23 GB — pre-built binaries extracted from the container
│   ├── rosetta.tar.gz              # 21 GB — Rosetta release-408, unpacks to /usr/local/rosetta_pkgs/
│   ├── miniforge3.tar.gz           # 1.2 GB — full miniforge3 incl. predPEP env, unpacks to /home/spacepep/miniforge3/
│   ├── tmap.tar.gz                 # 624 MB — TMAP (compiled), unpacks to /usr/local/tmap/
│   ├── foldx.tar.gz                # 62 MB — FoldX binary + rotamer lib, unpacks to /usr/local/foldx26Linux64_0/
│   └── ogdf.tar.gz                 # 46 MB — Open Graph Drawing Framework, unpacks to /usr/local/ogdf/
├── sp-predPEP/                     # Flask app code (copy into /opt/sp-predPEP/ in image)
│   ├── predPEP.py                  # Main Flask app
│   ├── tmap_utils.py               # TMAP helper
│   ├── pipelines.txt
│   ├── templates/                  # Flask HTML templates
│   ├── static/                     # 586 MB of JS — mostly molstar/ and ngl-master/
│   └── old_scripts/                # archive, can be kept or stripped
├── pepspec_pipe/                   # Pipeline scripts (copy into /usr/local/pepspec_pipe/ in image)
│   ├── run_iteMAN.py               # THE orchestrator, called by Flask via subprocess
│   ├── run_pepSpecPipe.sh, run_foldX.sh, run_foldX2.sh, run_catFiles.sh,
│   ├── run_createflag2_2.sh (+ 2A, 2B variants), run_anaSCORES.py,
│   ├── run_catSPEC.py, run_mergeScores.py
│   └── README.txt
├── usr_local_bin/                  # The dereferenced /usr/local/bin from the container — FOR REFERENCE.
│   # Contains the actual files that /usr/local/bin's symlinks pointed to.
│   # In the new image, we recreate /usr/local/bin as symlinks into /usr/local/pepspec_pipe/
│   # and /usr/local/foldx26Linux64_0/ — NOT by copying these files.
└── docs/                           # Reference docs from the extraction process
    ├── predPEP_environment.yml     # Full conda env spec (216 deps). Not used directly — we ship miniforge3.tar.gz — but kept for audit
    ├── pip_freeze.txt              # pip freeze from inside the predPEP env
    ├── apt_manual_packages.txt     # apt-mark showmanual from container
    ├── docker_inspect_container.json
    ├── docker_inspect_image.json
    ├── docker_history.txt          # Full build history of the original image
    └── container_filesystem.txt    # ls -la of /opt, /home/spacepep, /tmp/jobs in the container
```

## Critical constraints for the Dockerfile

**Paths must match exactly.** The miniforge3 conda env has thousands of files with hardcoded paths (Python shebangs, compiled extension rpaths, etc.) pointing to `/home/spacepep/miniforge3/envs/predPEP/`. The Rosetta path `/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/` is hardcoded in `run_iteMAN.py`. Do not change these.

**User must be spacepep, UID/GID 1003.** Anything else and the conda env breaks.

**Base image:** `nvidia/cuda:12.4.0-cudnn9-devel-ubuntu22.04` (matching the original, readable from `docs/docker_history.txt`).

**Blobs extract to these exact locations:**

- `rosetta.tar.gz` → tar contains `rosetta_pkgs/…` at top level, extract into `/usr/local/` (ends up at `/usr/local/rosetta_pkgs/`)
- `foldx.tar.gz` → tar contains `foldx26Linux64_0/…`, extract into `/usr/local/`
- `ogdf.tar.gz` → tar contains `ogdf/…`, extract into `/usr/local/`
- `tmap.tar.gz` → tar contains `tmap/…`, extract into `/usr/local/`
- `miniforge3.tar.gz` → tar contains `miniforge3/…`, extract into `/home/spacepep/`. **Must be owned by spacepep:spacepep after extraction.**

**Symlinks in /usr/local/bin.** Original image had these symlinks. Recreate them:

```
run_iteMAN.py        -> /usr/local/pepspec_pipe/run_iteMAN.py
run_pepSpecPipe.sh   -> /usr/local/pepspec_pipe/run_pepSpecPipe.sh
run_catFiles.sh      -> /usr/local/pepspec_pipe/run_catFiles.sh
run_catSPEC.py       -> /usr/local/pepspec_pipe/run_catSPEC.py
run_createflag2_2.sh -> /usr/local/pepspec_pipe/run_createflag2_2.sh
run_foldX.sh         -> /usr/local/pepspec_pipe/run_foldX.sh
run_foldX2.sh        -> /usr/local/pepspec_pipe/run_foldX2.sh
run_anaSCORES.py     -> /usr/local/pepspec_pipe/run_anaSCORES.py
run_mergeScores.py   -> /usr/local/pepspec_pipe/run_mergeScores.py
foldx_20270131       -> /usr/local/foldx26Linux64_0/foldx_20270131
```

**Runtime directories.** `/tmp/pepspec/uploads` and `/tmp/pepspec/results` are referenced in `predPEP.py`. `/tmp/jobs/` exists with spacepep ownership in the original. Create these and chown to spacepep.

**Apt packages needed** (from original `apt-get install`, see `docs/docker_history.txt`):

- Base setup: `wget git bzip2 sudo ca-certificates libxml2 nano`
- Python: `python3-pip python3-dev`
- **Plus `curl`** — missing in original, caused healthcheck to fail. Add it.

## Goal of the Claude Code session

Produce a working setup in this directory containing:

1. `Dockerfile` — builds a functionally identical image using the blobs + source
2. `docker-compose.yml` (optional but nice) — runs it with correct GPU/port/volume config
3. `build.sh` — one-command build
4. `run.sh` — one-command launch
5. `README.md` — how to use it going forward

### Dockerfile approach (sketch — refine as needed)

```dockerfile
FROM nvidia/cuda:12.4.0-cudnn9-devel-ubuntu22.04

# System deps (same as original history + curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget git bzip2 sudo ca-certificates libxml2 nano curl \
      python3-pip python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create user with exact UID/GID used in original
ARG USER_NAME=spacepep USER_UID=1003 USER_GID=1003
RUN groupadd --gid $USER_GID $USER_NAME \
    && useradd -ms /bin/bash --uid $USER_UID --gid $USER_GID $USER_NAME \
    && echo "$USER_NAME ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
    && mkdir -p /home/$USER_NAME/app /tmp/jobs /tmp/pepspec/uploads /tmp/pepspec/results \
    && chown -R $USER_UID:$USER_GID /home/$USER_NAME /tmp/jobs /tmp/pepspec

# Drop in the heavy pre-built tools as blobs.
# Each tar archive already has the top-level folder name inside,
# so we extract into the parent directory.
COPY blobs/rosetta.tar.gz /tmp/
RUN tar -xzf /tmp/rosetta.tar.gz -C /usr/local/ && rm /tmp/rosetta.tar.gz
COPY blobs/foldx.tar.gz /tmp/
RUN tar -xzf /tmp/foldx.tar.gz -C /usr/local/ && rm /tmp/foldx.tar.gz
COPY blobs/ogdf.tar.gz /tmp/
RUN tar -xzf /tmp/ogdf.tar.gz -C /usr/local/ && rm /tmp/ogdf.tar.gz
COPY blobs/tmap.tar.gz /tmp/
RUN tar -xzf /tmp/tmap.tar.gz -C /usr/local/ && rm /tmp/tmap.tar.gz

# miniforge3 must land in /home/spacepep and be owned by spacepep
COPY blobs/miniforge3.tar.gz /tmp/
RUN tar -xzf /tmp/miniforge3.tar.gz -C /home/spacepep/ \
    && rm /tmp/miniforge3.tar.gz \
    && chown -R $USER_UID:$USER_GID /home/spacepep/miniforge3

# Pipeline scripts
COPY pepspec_pipe/ /usr/local/pepspec_pipe/
RUN chmod +x /usr/local/pepspec_pipe/*.sh /usr/local/pepspec_pipe/*.py

# Recreate /usr/local/bin symlinks
RUN cd /usr/local/bin \
    && for f in /usr/local/pepspec_pipe/run_*.sh /usr/local/pepspec_pipe/run_*.py; do \
         ln -sf "$f" "$(basename "$f")"; \
       done \
    && ln -sf /usr/local/foldx26Linux64_0/foldx_20270131 foldx_20270131

# The app
COPY --chown=$USER_UID:$USER_GID sp-predPEP/ /opt/sp-predPEP/

# Runtime config matching the original
ENV HOME=/home/spacepep \
    FLASK_APP=predPEP.py \
    PATH=/home/spacepep/miniforge3/condabin:/usr/local/nvidia/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/sp-predPEP
EXPOSE 6363

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl --fail http://localhost:6363/ || exit 1

USER spacepep
CMD ["/bin/bash", "-c", "mamba run -n predPEP gunicorn -w 1 -b 0.0.0.0:6363 --worker-class=gevent --timeout 0 --capture-output --log-level debug --access-logfile - --error-logfile - predPEP:predPEP"]
```

### Build command

```bash
docker build -t predpep:local .
```

Expected build time on a modern workstation: 10-15 minutes, dominated by Rosetta extraction (21 GB tar decompression). Final image size: roughly 55-60 GB.

### Run command

```bash
docker run -d --name predpep_app \
  --gpus all \
  -p 6363:6363 \
  --restart unless-stopped \
  predpep:local
```

### Verification

1. `docker ps` shows the container running.
2. `docker logs predpep_app` shows gunicorn starting and binding to `0.0.0.0:6363` without Python import errors.
3. Browser → `http://localhost:6363` → Flask UI loads.
4. Submit a small test job through the UI; monitor logs for subprocess calls to `run_iteMAN.py`; verify a result `.zip` eventually appears under `/tmp/pepspec/results/` inside the container (`docker exec predpep_app ls /tmp/pepspec/results/`).

### Known risks / likely issues during build/run

1. **GPU access**: requires nvidia-container-toolkit installed on host. Test with `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` first if unsure.
2. **Conda env path integrity**: if extraction of miniforge3 doesn't land the env at exactly `/home/spacepep/miniforge3/envs/predPEP/`, Python shebangs will break. Quick test after build: `docker run --rm predpep:local mamba run -n predPEP python -c "import flask; print(flask.__version__)"`.
3. **Symlink targets in /usr/local/bin**: if any symlink points to a path that doesn't exist after blob extraction, the pipeline will fail at runtime, not build time. After build, verify: `docker run --rm predpep:local ls -la /usr/local/bin/run_iteMAN.py` should show a valid symlink.
4. **Healthcheck with curl**: curl is now installed so healthcheck should pass. If it doesn't pass within 30s startup + 3 retries × 30s interval, Flask itself isn't responding — check gunicorn logs.
5. **FoldX license**: the binary is bundled. User has an institutional academic license covering its use; don't publicly redistribute the image.
6. **Rosetta license**: same concern — academic license. Image is for internal institutional use only.

## Development workflow (modifying code)

For iterating on `predPEP.py` or pipeline scripts without rebuilding the 55 GB image every time, add bind mounts at runtime:

```bash
docker run -d --name predpep_app_dev \
  --gpus all \
  -p 6363:6363 \
  -v "$(pwd)/sp-predPEP:/opt/sp-predPEP" \
  -v "$(pwd)/pepspec_pipe:/usr/local/pepspec_pipe" \
  predpep:local
```

Edit files on the host; gunicorn doesn't auto-reload in this config, so `docker restart predpep_app_dev` after changes to `.py`. HTML/JS changes in `templates/` and `static/` don't need a restart.

## Questions to resolve while building

- Does your GPU + driver combo support CUDA 12.4? (`nvidia-smi` should show driver version 550+ for CUDA 12.4. Older drivers work too via the compat package already in the CUDA image, but confirm.)
- Is there ~60 GB of free disk for the final image + its build cache?
- Any firewall on the host blocking port 6363?

---

End of handoff. The sibling folders (`blobs/`, `sp-predPEP/`, `pepspec_pipe/`, `docs/`) contain everything referenced above.
