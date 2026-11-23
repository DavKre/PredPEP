#!/usr/bin/env bash
# One-command deploy of a predPEP node on this machine — for anyone, no build needed.
#
# Job data lives in a HOST bind-mount ($PREDPEP_DATA, default ~/predpep_data). Because it's a
# plain host directory, it is immune to `docker volume rm`, `docker volume prune`,
# `docker system prune --volumes`, and `docker compose down -v`. Re-running this script updates
# the container in place; the data directory is NEVER touched. Only `rm -rf $PREDPEP_DATA` removes it.
#
# Usage:
#   ./scripts/deploy.sh                                              # use predpep:<VERSION> (already loaded/pulled)
#   ./scripts/deploy.sh forgejo.lan.peptide.space/spacepeptidesdev/predpep:v1.3.0   # pull from the registry, then run
#   PREDPEP_DATA=/srv/predpep ./scripts/deploy.sh                    # custom data location
#
# Side-by-side rollout (what docs/UPGRADE-v1.6.0-node.md recommends): give the new
# node its own name, port and data directory so the running one is untouched and
# stays the rollback.
#
#   PREDPEP_NAME=predpep-v160-node PREDPEP_PORT=6365 \
#   PREDPEP_DATA=$HOME/predpep_data_v160 ./scripts/deploy.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(cat "$HERE/../VERSION" 2>/dev/null || echo latest)"
IMAGE="${1:-predpep:$VERSION}"
DATA="${PREDPEP_DATA:-$HOME/predpep_data}"
# Name and host port are overridable so a new build can run BESIDE the live node
# rather than replacing it. Defaults are the long-standing ones, so an existing
# deploy command keeps doing exactly what it did.
NAME="${PREDPEP_NAME:-predpep_app}"
PORT="${PREDPEP_PORT:-6363}"
APP_UID=1003   # the image's app user (spacepep); the bind dir must be writable by it

command -v docker >/dev/null 2>&1 || { echo "error: docker is not installed / not on PATH" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "error: cannot reach the docker daemon (need 'sudo usermod -aG docker $USER' + re-login?)" >&2; exit 1; }

# Get the image: use the local copy if present, else pull (works for any registry-qualified ref).
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "image '$IMAGE' not present locally — pulling..."
  if ! docker pull "$IMAGE"; then
    echo "error: could not get image '$IMAGE'. Either:" >&2
    echo "  - pull from the company registry:  ./scripts/deploy.sh forgejo.lan.peptide.space/spacepeptidesdev/predpep:$VERSION" >&2
    echo "  - or load a shared tarball first:   docker load < predpep-$VERSION.tgz   then re-run ./scripts/deploy.sh" >&2
    exit 1
  fi
fi

# Prepare the persistent data dir and make it writable by the container's app user (uid 1003).
mkdir -p "$DATA"
docker run --rm --user 0 -v "$DATA":/d "$IMAGE" \
  sh -c "mkdir -p /d/results /d/uploads /d/archive /d/state && chown -R $APP_UID:$APP_UID /d"

# Refuse to recreate the container while a job is running: `docker rm -f` SIGKILLs
# the pipeline with no grace period, and an orphaned job is reconciled to failed on
# the next boot, never resumed. Set FORCE=1 to override deliberately.
#
# This check FAILS CLOSED. /jobs is a protected route: once the node has its first
# token, an unauthenticated request gets 401, and reading that as "no jobs running"
# is exactly the wrong answer — it would green-light destroying a container with
# 14 hours of work in it. So anything other than a 200 that we can actually parse
# stops the deploy and says why. Pass a read-scope token as PREDPEP_TOKEN.
if [ "${FORCE:-0}" != "1" ] && docker container inspect "$NAME" >/dev/null 2>&1; then
  AUTH=()
  [ -n "${PREDPEP_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${PREDPEP_TOKEN}")
  body="$(curl -sS --max-time 10 -w '\n%{http_code}' "${AUTH[@]+"${AUTH[@]}"}" \
          http://localhost:${PORT}/jobs 2>/dev/null)" || body=""
  code="$(printf '%s' "$body" | tail -n1)"
  if [ "$code" != "200" ]; then
    echo "error: could not read /jobs on this node (HTTP ${code:-no response})." >&2
    case "$code" in
      401|403) echo "  the node has auth enabled — export PREDPEP_TOKEN=<a read-scope token> and re-run." >&2 ;;
      *)       echo "  the node is not answering; check 'docker ps' and 'docker logs predpep_app'." >&2 ;;
    esac
    echo "  refusing to recreate the container without knowing whether a job is running." >&2
    echo "  override with FORCE=1 only if you are certain the node is idle." >&2
    exit 1
  fi
  busy="$(printf '%s' "$body" | grep -o '"status": *"\(Running\|Queued\)"' | wc -l)"
  if [ "${busy:-0}" -gt 0 ]; then
    echo "error: $busy job(s) Running/Queued on this node — recreating would kill them." >&2
    echo "  wait for them to finish, or re-run with FORCE=1 to proceed anyway." >&2
    exit 1
  fi
fi

# Tuning knobs, forwarded only when set in the environment. run.sh has always
# done this; deploy.sh did not, which meant the documented deploy path could not
# set a retention policy at all -- and the image's default of 30 days will evict
# any job older than that, archiving it first but dropping it out of the jobs
# list. Migrating an older node's history onto a new one needs this.
ENV_ARGS=()
for v in PREDPEP_CORE_BUDGET PREDPEP_RETENTION_BYTES PREDPEP_RETENTION_DAYS PREDPEP_PARTIAL_STATUS PREDPEP_FINGERPRINT; do
  [ -n "${!v:-}" ] && ENV_ARGS+=( -e "$v=${!v}" )
done
[ -n "${PREDPEP_MEMORY:-}" ] && ENV_ARGS+=( --memory "${PREDPEP_MEMORY}" )

# (Re)create the container. This removes only the CONTAINER — $DATA is never touched.
# Host 6363 (the port the DDN dispatcher dials) -> container 8383 (upstream's bind).
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  "${ENV_ARGS[@]+"${ENV_ARGS[@]}"}" \
  -v "$DATA":/data \
  --log-opt max-size=10m --log-opt max-file=3 \
  --pids-limit 4096 --stop-timeout 120 \
  -p "${PORT}:8383" \
  --restart unless-stopped \
  "$IMAGE"

echo
echo "predPEP node is up:  http://localhost:${PORT}/   (container ${NAME})"
echo "  image:  $IMAGE"
echo "  data:   $DATA   (persists across updates and docker cleanups)"
echo "  health: curl -fsS http://localhost:${PORT}/health"
echo "  update: get the new image, then re-run this script — your data is preserved."
