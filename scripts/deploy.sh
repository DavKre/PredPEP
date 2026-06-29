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
#   ./scripts/deploy.sh forgejo.lan.peptide.space/david/predpep:v1.3.0   # pull from the registry, then run
#   PREDPEP_DATA=/srv/predpep ./scripts/deploy.sh                    # custom data location
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(cat "$HERE/../VERSION" 2>/dev/null || echo latest)"
IMAGE="${1:-predpep:$VERSION}"
DATA="${PREDPEP_DATA:-$HOME/predpep_data}"
APP_UID=1003   # the image's app user (spacepep); the bind dir must be writable by it

command -v docker >/dev/null 2>&1 || { echo "error: docker is not installed / not on PATH" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "error: cannot reach the docker daemon (need 'sudo usermod -aG docker $USER' + re-login?)" >&2; exit 1; }

# Get the image: use the local copy if present, else pull (works for any registry-qualified ref).
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "image '$IMAGE' not present locally — pulling..."
  if ! docker pull "$IMAGE"; then
    echo "error: could not get image '$IMAGE'. Either:" >&2
    echo "  - pull from the company registry:  ./scripts/deploy.sh forgejo.lan.peptide.space/david/predpep:$VERSION" >&2
    echo "  - or load a shared tarball first:   docker load < predpep-$VERSION.tgz   then re-run ./scripts/deploy.sh" >&2
    exit 1
  fi
fi

# Prepare the persistent data dir and make it writable by the container's app user (uid 1003).
mkdir -p "$DATA"
docker run --rm --user 0 -v "$DATA":/d "$IMAGE" \
  sh -c "mkdir -p /d/results /d/uploads && chown -R $APP_UID:$APP_UID /d"

# (Re)create the container. This removes only the CONTAINER — $DATA is never touched.
docker rm -f predpep_app >/dev/null 2>&1 || true
docker run -d --name predpep_app \
  -v "$DATA":/tmp/pepspec \
  --log-opt max-size=10m --log-opt max-file=3 \
  --pids-limit 4096 \
  -p 6363:6363 \
  --restart unless-stopped \
  "$IMAGE"

echo
echo "predPEP node is up:  http://localhost:6363/"
echo "  image:  $IMAGE"
echo "  data:   $DATA   (persists across updates and docker cleanups)"
echo "  health: curl -fsS http://localhost:6363/health"
echo "  update: get the new image, then re-run this script — your data is preserved."
