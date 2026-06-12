#!/usr/bin/env bash
# Launch predpep_app_dev with host bind-mounts for iteration.
# Edit files on the host; `docker restart predpep_app_dev` to reload .py.
# HTML/JS in templates/ and static/ don't need a restart.
#
# Override the host port with HOST_PORT=6364 ./run-dev.sh when predpep_app
# (production) is already holding 6363.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

IMAGE=predpep:local
CONTAINER=predpep_app_dev
HOST_PORT="${HOST_PORT:-6363}"

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "error: image '${IMAGE}' not found — run ./scripts/build.sh first" >&2
  exit 1
fi

if docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
  echo "error: container '${CONTAINER}' already exists" >&2
  echo "  reload after .py edit  → docker restart ${CONTAINER}" >&2
  echo "  tear down              → docker rm -f ${CONTAINER}" >&2
  exit 1
fi

docker run -d \
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
  -p "${HOST_PORT}:6363" \
  -v "${ROOT}/app:/opt/sp-predPEP" \
  -v "${ROOT}/pipeline:/usr/local/pepspec_pipe" \
  "${IMAGE}"

echo
echo "${CONTAINER} started (dev mode, bind-mounted from ${ROOT})."
echo "  open:    http://localhost:${HOST_PORT}"
echo "  logs:    docker logs -f ${CONTAINER}"
echo "  reload:  docker restart ${CONTAINER}"
echo "  stop:    docker stop ${CONTAINER}"
