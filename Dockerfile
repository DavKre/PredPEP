# syntax=docker/dockerfile:1.7
#
# predPEP local image — rebuilt from blobs extracted from the production container.
# See HANDOFF.md for background and path constraints.
#
# Layers are ordered least-volatile (top) to most-volatile (bottom) so that
# editing app code only invalidates the final few layers.

FROM ubuntu:22.04

# ---- 1. System packages -----------------------------------------------------
# Same set as the original image history, plus curl (was missing — broke the
# original healthcheck).
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget git bzip2 sudo ca-certificates libxml2 nano curl \
      zip unzip \
      python3-pip python3-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- 2. User and runtime directories ---------------------------------------
# UID/GID 1003 is load-bearing: miniforge3 env has hardcoded paths under
# /home/spacepep owned by this UID.
ARG USER_NAME=spacepep
ARG USER_UID=1003
ARG USER_GID=1003
RUN groupadd --gid ${USER_GID} ${USER_NAME} \
    && useradd -ms /bin/bash --uid ${USER_UID} --gid ${USER_GID} ${USER_NAME} \
    && echo "${USER_NAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
    && mkdir -p /home/${USER_NAME}/app /tmp/jobs /tmp/pepspec/uploads /tmp/pepspec/results \
    && chown -R ${USER_UID}:${USER_GID} /home/${USER_NAME} /tmp/jobs /tmp/pepspec

# ---- 3. Pre-built tool blobs ------------------------------------------------
# Bind-mounted (not COPY'd) so the 23 GB of tarballs never enter any image
# layer. Extraction + ownership fix + Rosetta prune happen in a single RUN so
# there's one layer for all tools AND the 49 GB Rosetta tree never persists:
# the pipeline only runs pepspec.static.linuxgccrelease (a 184 MB static binary)
# with main/database, so we keep those + protein_tools/scripts and drop the rest
# (~45 GB of other apps/variants/source/tests). See
# docs/superpowers/specs/2026-06-12-phase2a2-rosetta-prune-design.md.
RUN --mount=type=bind,source=./blobs,target=/tmp/blobs,readonly \
    tar -xzf /tmp/blobs/rosetta.tar.gz    -C /usr/local/ \
 && tar -xzf /tmp/blobs/foldx.tar.gz      -C /usr/local/ \
 && tar -xzf /tmp/blobs/miniforge3.tar.gz -C /home/${USER_NAME}/ \
 && chown -R ${USER_UID}:${USER_GID} /home/${USER_NAME}/miniforge3 \
 && R=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408 \
 && BINREL=main/source/build/src/release/linux/5.4/64/x86/gcc/7/static/pepspec.static.linuxgccrelease \
 && mv "$R/$BINREL" /tmp/pepspec.bin \
 && rm -rf "$R/main/source/build" \
 && mkdir -p "$(dirname "$R/$BINREL")" \
 && mv /tmp/pepspec.bin "$R/$BINREL" \
 && find "$R/main/source" -mindepth 1 -maxdepth 1 ! -name bin ! -name build -exec rm -rf {} + \
 && find "$R/main/tools"  -mindepth 1 -maxdepth 1 ! -name protein_tools -exec rm -rf {} + \
 && find "$R/main"        -mindepth 1 -maxdepth 1 ! -name database ! -name source ! -name tools -exec rm -rf {} +

# Fail the build if the Rosetta prune dropped anything the pipeline needs at runtime.
RUN R=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408 \
 && test -f "$R/main/source/build/src/release/linux/5.4/64/x86/gcc/7/static/pepspec.static.linuxgccrelease" \
 && test -d "$R/main/database" \
 && test -f "$R/main/tools/protein_tools/scripts/clean_pdb.py" \
 && echo "OK: Rosetta prune kept pepspec binary + database + protein_tools."

# ---- 4. Pipeline scripts + /usr/local/bin symlinks --------------------------
# Use bash for the remaining RUN / shell-form instructions so `shopt -s
# nullglob` is available — protects the symlink loop from silently running
# against a literal unexpanded glob if the scripts are ever renamed.
SHELL ["/bin/bash", "-c"]

COPY pipeline/ /usr/local/pepspec_pipe/
RUN chmod +x /usr/local/pepspec_pipe/*.sh /usr/local/pepspec_pipe/*.py \
    && cd /usr/local/bin \
    && shopt -s nullglob \
    && for f in /usr/local/pepspec_pipe/run_*.sh /usr/local/pepspec_pipe/run_*.py; do \
         ln -sf "$f" "$(basename "$f")"; \
       done \
    && ln -sf /usr/local/foldx26Linux64_0/foldx_20270131 foldx_20270131

# ---- 5. Flask app (most-volatile layer) -------------------------------------
COPY --chown=${USER_UID}:${USER_GID} app/ /opt/sp-predPEP/

# ---- 6. Runtime configuration ----------------------------------------------
ENV HOME=/home/spacepep \
    FLASK_APP=predPEP.py \
    PATH=/home/spacepep/miniforge3/envs/predPEP/bin:/home/spacepep/miniforge3/condabin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /opt/sp-predPEP
EXPOSE 6363

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl --fail http://localhost:6363/health || exit 1

USER spacepep

# Invoke the env's gunicorn directly (not via `mamba run`): mamba run wraps the
# command in a subshell with stdout/stderr capture and signal traps that
# deadlock gunicorn's gevent worker fork — the arbiter logs "1 workers" and
# then hangs before "Booting worker with pid" ever appears. Direct exec form
# also makes gunicorn PID 1 so `docker stop` delivers SIGTERM to the arbiter.
CMD ["/home/spacepep/miniforge3/envs/predPEP/bin/gunicorn", \
     "-w", "1", \
     "-b", "0.0.0.0:6363", \
     "--worker-class=gevent", \
     "--timeout", "0", \
     "--capture-output", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "predPEP:predPEP"]
