# syntax=docker/dockerfile:1.7
#
# predPEP node image — the v1.6.0 application, built self-contained for the fleet.
#
# Two things make this image different from the dev's predpep:v1.6.0:
#
#   1. Rosetta and FoldX Pro are BAKED IN, not mounted from the host. The dev's
#      image mounts both read-only and refuses to start without them, which is
#      right for a workstation that has its own licensed trees and wrong for a
#      fleet node that does not. We hold the licences; see LICENSING in README.
#   2. The image is pruned. Rosetta ships ~45 GB of other protocols, variants and
#      source; the pipeline runs exactly two of its binaries.
#
# Everything else is deliberately identical to the dev's image -- same conda
# environment (taken from it), same paths, same shebangs, same gunicorn config --
# so a path in a log, a flag file or a traceback means the same thing on a node
# as it does on the group server.
#
# Layers are ordered least-volatile (top) to most-volatile (bottom) so that
# editing app code only invalidates the final few layers.

# ubuntu:24.04, not 22.04, and that is not a refresh for its own sake: the
# conda env and libOGDF both come out of the dev's image, which is 24.04, and
# libOGDF is linked against GLIBC_2.38. On 22.04 (glibc 2.35) `import tmap`
# fails with a version error -- which predPEP.py catches and turns into a
# silently disabled similarity tree.
FROM ubuntu:24.04

# ---- 1. System packages -----------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget git bzip2 sudo ca-certificates libxml2 nano curl \
      zip unzip \
      python3-pip python3-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- 2. User and runtime directories ---------------------------------------
# The conda env carries hardcoded paths under /home/spacepep, so the HOME path
# is load-bearing; the UID is not (everything is chown'd below), but it stays
# 1003 because the fleet's bind-mounted data directories are already owned by it.
ARG USER_NAME=spacepep
ARG USER_UID=1003
ARG USER_GID=1003
RUN groupadd --gid ${USER_GID} ${USER_NAME} \
    && useradd -ms /bin/bash --uid ${USER_UID} --gid ${USER_GID} ${USER_NAME} \
    && echo "${USER_NAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers \
    && mkdir -p /home/${USER_NAME}/app /tmp/jobs /tmp/pepspec/uploads /tmp/pepspec/results \
                /data/results /data/uploads /data/archive /data/state \
    && chown -R ${USER_UID}:${USER_GID} /home/${USER_NAME} /tmp/jobs /tmp/pepspec /data

# ---- 3. Pre-built tool blobs ------------------------------------------------
# Bind-mounted (not COPY'd) so the 22 GB of tarballs never enter an image layer.
# Extraction + prune happen in ONE RUN so the pruned bytes never persist.
#
# blobs/ogdf.tar.gz holds libOGDF/libCOIN, which live in /usr/local/lib in the
# dev's image rather than inside the conda env -- `import tmap` fails without
# them, and tmap's failure mode is a silent one: predPEP.py catches the
# ImportError and disables the similarity tree.
#
# blobs/miniforge3-v160.tar.gz is the conda environment lifted out of the dev's
# predpep:v1.6.0 image (python 3.12), NOT our old 3.10 one. That is a
# correctness decision, not convenience: the v1.6.0 analytics need sklearn,
# seaborn and mhfp, and the old env had none of them -- the seqmap and
# descriptor panels would have come up degraded on nodes and nowhere else. Its
# pkgs/ download cache (984 MB) was excluded at capture; nothing invokes conda
# or mamba at runtime.
#
# Rosetta prune: the pipeline runs pepspec.static.linuxgccrelease (the design
# loop) and rosetta_scripts.static.linuxgccrelease (the non-canonical design
# pass, new in v1.6.0 -- pruning it away would leave the NCAA pass silently
# unable to run, and a failure there deliberately cannot change a job's
# verdict, so the job would still report complete). Both need main/database,
# and run_pepSpecPipe.sh calls clean_pdb.py from main/tools/protein_tools.
# Everything else goes.
RUN --mount=type=bind,source=./blobs,target=/tmp/blobs,readonly \
    tar -xzf /tmp/blobs/rosetta.tar.gz         -C /usr/local/ \
 && tar -xzf /tmp/blobs/foldxpro.tar.gz        -C /usr/local/ \
 && tar -xzf /tmp/blobs/residues-v160.tar.gz   -C /opt/ \
 && tar -xzf /tmp/blobs/miniforge3-v160.tar.gz -C /home/${USER_NAME}/ \
 && tar -xzf /tmp/blobs/ogdf.tar.gz            -C /usr/local/lib/ \
 && ldconfig \
 && MF=/home/${USER_NAME}/miniforge3 \
 && E=$MF/envs/predPEP \
 && find $E -name '*.a' -delete \
 && rm -rf $E/lib/cmake $E/include $E/lib/pkgconfig \
 && find $MF -mindepth 1 -maxdepth 1 ! -name envs -exec rm -rf {} + \
 && chown -R ${USER_UID}:${USER_GID} $MF /opt/spacepep-createFF \
 && R=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408 \
 && B=$R/main/source/build/src/release/linux/5.4/64/x86/gcc/7/static \
 && mkdir -p /tmp/keep \
 && mv "$B/pepspec.static.linuxgccrelease" "$B/rosetta_scripts.static.linuxgccrelease" /tmp/keep/ \
 && rm -rf "$R/main/source/build" \
 && mkdir -p "$B" \
 && mv /tmp/keep/pepspec.static.linuxgccrelease /tmp/keep/rosetta_scripts.static.linuxgccrelease "$B/" \
 && rmdir /tmp/keep \
 && mkdir -p "$R/main/source/bin" \
 && ln -sf "$B/pepspec.static.linuxgccrelease"         "$R/main/source/bin/pepspec.static.linuxgccrelease" \
 && ln -sf "$B/rosetta_scripts.static.linuxgccrelease" "$R/main/source/bin/rosetta_scripts.static.linuxgccrelease" \
 && find "$R/main/source" -mindepth 1 -maxdepth 1 ! -name bin ! -name build -exec rm -rf {} + \
 && find "$R/main/tools"  -mindepth 1 -maxdepth 1 ! -name protein_tools -exec rm -rf {} + \
 && find "$R/main"        -mindepth 1 -maxdepth 1 ! -name database ! -name source ! -name tools -exec rm -rf {} +

# Fail the build if the prune dropped anything the pipeline needs at runtime.
RUN R=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408 \
 && B=$R/main/source/build/src/release/linux/5.4/64/x86/gcc/7/static \
 && test -x "$B/pepspec.static.linuxgccrelease" \
 && test -x "$B/rosetta_scripts.static.linuxgccrelease" \
 && test -x "$R/main/source/bin/rosetta_scripts.static.linuxgccrelease" \
 && test -d "$R/main/database" \
 && test -f "$R/main/tools/protein_tools/scripts/clean_pdb.py" \
 && test -x /usr/local/foldxpro_20261231/foldxpro20261231 \
 && test -f /usr/local/foldxpro_20261231/rotabase.txt \
 && test -f /opt/spacepep-createFF/all_aas3/all-residues-list-complete.csv \
 && test -f /opt/spacepep-createFF/all_aas3/retired_residues.csv \
 && test -f /opt/spacepep-createFF/all_aas3/residue_names.csv \
 && echo "OK: pepspec + rosetta_scripts + database + protein_tools + FoldX Pro + residue library."

# Fail the build if the env is missing anything the v1.6.0 app imports. sklearn,
# seaborn and mhfp are listed because the old env lacked them and nothing failed
# loudly -- the analytics panels just lost their content.
RUN /home/${USER_NAME}/miniforge3/envs/predPEP/bin/python -c \
      "import flask, rdkit, numpy, pandas, scipy, sklearn, seaborn, mhfp, tmap, \
              gevent, gunicorn, werkzeug, matplotlib.pyplot, mpl_toolkits.mplot3d; \
       print('OK: every live dependency imports.')"

# MAP4C (chiral MinHashed Atom-Pair, Orsi et al.) backs the similarity views.
# The ONLY package added on top of the environment lifted from the dev's image,
# pinned so a rebuild cannot silently move it. It needs nothing but rdkit and
# numpy, both already present, so pip resolves no transitive dependencies.
#
# Asserted immediately: tmap_utils falls back to MHFP when the import fails, so
# a broken install would not raise -- it would quietly change every similarity
# map in the UI back to the old fingerprint, with only the backend string in a
# panel note to say so.
RUN /home/${USER_NAME}/miniforge3/envs/predPEP/bin/pip install --no-cache-dir \
      mapchiral==0.0.7 \
 && /home/${USER_NAME}/miniforge3/envs/predPEP/bin/python -c \
      "from mapchiral.mapchiral import encode; from rdkit import Chem; \
       fp = encode(Chem.MolFromSequence('AAA'), max_radius=2, n_permutations=1024, seed=42); \
       assert len(fp) == 1024, len(fp); \
       print('OK: MAP4C encodes (vector length %d).' % len(fp))"

# ---- 4. Pipeline scripts + /usr/local/bin symlinks --------------------------
# Every pipeline script is linked by bare name, which is how PREDPEP_MANAGER can
# default to /usr/local/bin/run_iteMAN.py and how run_iteMAN.py finds
# run_ncaaDesign.py (it tries /usr/local/bin first, then /usr/local/pepspec_pipe).
# foldx_20270131 is a legacy-name shim for the FoldX Pro binary. The shipped
# v1.6.0 run_foldX2.sh resolves FoldX through $PREDPEP_FOLDX, but the dev's
# entrypoint still creates this shim because an earlier revision called it by
# bare name -- and that failure mode is silent: every FoldX call returns
# "command not found", every model scores empty, and the job runs to completion
# ranking designs on nothing. A symlink is cheaper than the risk.
#
# NOT kept by the prune: MakeRotLib.static.linuxgccrelease, which only generates
# rotamer libraries. The libraries are pre-generated and baked into the residue
# library, and the dev's own preflight calls its absence non-fatal.
#
# clean_pdb.py is linked the same way: run_pepSpecPipe.sh calls it bare, and
# without it every job dies before its first pepspec run.
SHELL ["/bin/bash", "-c"]

COPY pipeline/ /usr/local/pepspec_pipe/
RUN chmod +x /usr/local/pepspec_pipe/*.sh /usr/local/pepspec_pipe/*.py \
    && cd /usr/local/bin \
    && shopt -s nullglob \
    && for f in /usr/local/pepspec_pipe/*.sh /usr/local/pepspec_pipe/*.py; do \
         ln -sf "$f" "$(basename "$f")"; \
       done \
    && ln -sf /usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/tools/protein_tools/scripts/clean_pdb.py clean_pdb.py \
    && ln -sf /usr/local/foldxpro_20261231/foldxpro20261231 foldxpro20261231 \
    && ln -sf /usr/local/foldxpro_20261231/foldxpro20261231 foldx_20270131

# Every script's shebang must name an interpreter this image actually has. A
# script copied in by hand later keeps the build host's shebang and fails with a
# bare "not found", which names the file but not the cause.
RUN for f in /usr/local/pepspec_pipe/*.py /usr/local/pepspec_pipe/*.sh; do \
      i=$(head -1 "$f" | sed -n 's|^#!\([^ ]*\).*|\1|p'); \
      if [ -n "$i" ] && [ ! -x "$i" ]; then echo "BAD SHEBANG in $f: $i" >&2; exit 1; fi; \
    done; echo "OK: every pipeline shebang resolves."

# ---- 5. Flask app (most-volatile layer) -------------------------------------
# /opt/spacepep-predPEP, not our old /opt/sp-predPEP: the dev's logs, docs and
# PREDPEP_APP_DIR default all name this path, and divergence here costs more
# than it saves.
COPY --chown=${USER_UID}:${USER_GID} app/ /opt/spacepep-predPEP/

# Every symlink in the app tree must resolve. `test -e` follows the link, so a
# dangling one fails the build instead of shipping. app/catalogue.csv is the
# reason: upstream points it at all-residues-list.csv, which the residue library
# does not contain -- harmless in their image because only their test_dres.py
# reads it, but a broken link inside a shipped image is a trap for whoever opens
# it next. Ours points at all-residues-list-complete.csv, the file the catalogue
# itself reads, and this check keeps it that way.
RUN set -e; bad=0; \
    for l in $(find /opt/spacepep-predPEP -type l); do \
      if [ ! -e "$l" ]; then echo "DANGLING SYMLINK: $l -> $(readlink "$l")" >&2; bad=1; fi; \
    done; \
    [ "$bad" = 0 ] || { echo "a symlink in the app tree does not resolve." >&2; exit 1; }; \
    echo "OK: every symlink in the app tree resolves."

# ---- 6. Runtime configuration ----------------------------------------------
ARG VERSION=dev
ENV PREDPEP_VERSION=${VERSION} \
    HOME=/home/spacepep \
    FLASK_APP=predPEP.py \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    PREDPEP_BIND=0.0.0.0:8383 \
    PREDPEP_APP_DIR=/opt/spacepep-predPEP \
    PREDPEP_MANAGER=/usr/local/pepspec_pipe/run_iteMAN.py \
    ROSETTA_ROOT=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408 \
    PREDPEP_ROSETTA=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main \
    PREDPEP_RS_BIN=rosetta_scripts.static.linuxgccrelease \
    PREDPEP_FOLDX=/usr/local/foldxpro_20261231/foldxpro20261231 \
    PREDPEP_RESIDUE_CSV=/opt/spacepep-createFF/all_aas3/all-residues-list-complete.csv \
    PREDPEP_PARAMS_DIR=/opt/spacepep-createFF/all_aas3/generated_params_rotlib \
    PREDPEP_ROTLIB_DIR=/opt/spacepep-createFF/all_aas3/generated_rotlibs \
    PREDPEP_RESULT_DIR=/data/results \
    PREDPEP_UPLOAD_DIR=/data/uploads \
    PREDPEP_ARCHIVE_DIR=/data/archive \
    PREDPEP_STATE_DIR=/data/state \
    PREDPEP_MAX_ITERATIONS=6 \
    PREDPEP_CONVERGENCE_MIN=0.01 \
    PREDPEP_PLATEAU_ROUNDS=3 \
    PREDPEP_RETENTION_DAYS=90 \
    PREDPEP_RETENTION_BYTES=53687091200 \
    PREDPEP_NCAA_TOP=10 \
    PREDPEP_NCAA_TIMEOUT=14400 \
    PREDPEP_NCAA_CONTROL=1 \
    PREDPEP_NCAA_WEIGHTS=talaris2013_split_unfolded.wts \
    PATH=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408/main/source/bin:/usr/local/foldxpro_20261231:/usr/local/pepspec_pipe:/usr/local/bin:/home/spacepep/miniforge3/envs/predPEP/bin:/usr/bin:/bin:/usr/sbin:/sbin

# PREDPEP_NCAA_WEIGHTS is set above because it CHANGES RESULTS. Unset, the
# non-canonical pass scores through talaris' ref term, which is indexed by the
# AA enum and so charges every non-canonical some canonical residue's reference
# energy. A node launched without it scores non-canonicals the way they were
# scored before 18 November 2026, and nothing in the output records which.

# Fold the dev's verify.sh into the build: the residue catalogue read from
# inside the image must match the counts the build host reports. retired=6 is
# the field that matters -- an image whose withheld lists did not arrive reports
# retired=0, silently offers residues the catalogue hides, and raises no error.
# foldx_scorable is 2 rather than 0 here because FoldX Pro's rotabase.txt is
# baked in rather than mounted.
RUN cd /opt/spacepep-predPEP \
 && /home/${USER_NAME}/miniforge3/envs/predPEP/bin/python -c "\
import sys; sys.path.insert(0, '/opt/spacepep-predPEP'); \
import residue_catalogue; c = residue_catalogue.load_catalogue()['counts']; \
print('catalogue counts:', c); \
expected = {'noncanonical': 397, 'retired': 6, 'd_offerable': 208, 'foldx_scorable': 2, 'canonical': 20}; \
bad = {k: (c.get(k), v) for k, v in expected.items() if c.get(k) != v}; \
sys.exit('CATALOGUE MISMATCH (got, want): %s' % bad) if bad else print('OK: catalogue matches the build host.')"

WORKDIR /opt/spacepep-predPEP
EXPOSE 8383

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl --fail http://localhost:8383/health || exit 1

USER spacepep

# gunicorn.conf.py carries the two settings that are correctness rather than
# tuning (workers=1, gevent) and is used verbatim from upstream -- including
# preload_app=False, which reverses our old boot-wedge workaround: with a single
# worker, preloading only moved the scheduler greenlet into a process that does
# not survive a worker restart. Exec form so gunicorn is PID 1 and `docker stop`
# reaches the arbiter.
CMD ["/home/spacepep/miniforge3/envs/predPEP/bin/gunicorn", \
     "-c", "/opt/spacepep-predPEP/gunicorn.conf.py", \
     "predPEP:predPEP"]
