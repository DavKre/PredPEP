# syntax=docker/dockerfile:1.7
#
# predPEP local image — Flask + gunicorn peptide-design node (Rosetta + FoldX, CPU-only).
# Heavy tool blobs (Rosetta, FoldX, conda env) are provided alongside the repo under blobs/.
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
# layer. Extraction + ownership fix + Rosetta prune + conda slim happen in a
# single RUN so there's one layer for all tools AND the pruned bytes never
# persist: the pipeline only runs pepspec.static.linuxgccrelease (a 184 MB
# static binary) with main/database, so we keep those + protein_tools/scripts
# and drop the rest (~45 GB of other apps/variants/source/tests).
#
# Conda slim (Tier 1, no science risk): keep only envs/predPEP. We drop
#   (a) the pkgs/ download cache + base conda/mamba (~1.6 GB) — runtime invokes
#       the env's gunicorn/python directly (see CMD) and the pipeline resolves
#       tools via PATH; no `conda`/`mamba` call exists in app/ or pipeline/;
#   (b) the dead TMAP feature (tmap/faerun) — libOGDF + mhfp are absent so
#       `import tmap` always fails (caught by the try/except in predPEP.py) —
#       plus the matplotlib -> pyside6 -> qt6 -> libllvm/libclang GUI tail it
#       dragged in (~0.67 GB); the live path (flask/rdkit/numpy/pandas/gevent)
#       needs none of it;
#   (c) build-only cruft: *.a, cmake/, include/, pkgconfig/, package tests/.
RUN --mount=type=bind,source=./blobs,target=/tmp/blobs,readonly \
    tar -xzf /tmp/blobs/rosetta.tar.gz    -C /usr/local/ \
 && tar -xzf /tmp/blobs/foldx.tar.gz      -C /usr/local/ \
 && tar -xzf /tmp/blobs/miniforge3.tar.gz -C /home/${USER_NAME}/ \
 && MF=/home/${USER_NAME}/miniforge3 \
 && E=$MF/envs/predPEP \
 && SP=$E/lib/python3.10/site-packages \
 && rm -rf $SP/matplotlib $SP/matplotlib-*.dist-info $SP/mpl_toolkits $SP/pylab.py \
           $SP/PySide6 $SP/PySide6-*.dist-info $SP/shiboken6 $SP/shiboken6-*.dist-info \
           $SP/tmap $SP/tmap-*.dist-info $SP/faerun $SP/faerun-*.dist-info \
 && rm -rf $E/lib/qt6 $E/lib/libQt6* \
           $E/lib/libLLVM.so* $E/lib/libLLVM-*.so $E/lib/libclang.so* $E/lib/libclang-cpp.so* \
 && find $E -name '*.a' -delete \
 && rm -rf $E/lib/cmake $E/include $E/lib/pkgconfig \
 && find $E -type d -name tests -path '*/site-packages/*' -prune -exec rm -rf {} + \
 && find $MF -mindepth 1 -maxdepth 1 ! -name envs -exec rm -rf {} + \
 && chown -R ${USER_UID}:${USER_GID} $MF \
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

# Fail the build if the conda slim dropped anything the live service imports.
RUN /home/${USER_NAME}/miniforge3/envs/predPEP/bin/python -c \
      "import flask, rdkit, numpy, pandas, gevent, gunicorn, werkzeug; print('OK: live deps import on slimmed env.')"

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
