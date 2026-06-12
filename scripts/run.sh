#!/usr/bin/env bash
# Launch predpep_app for production-ish use (detached, GPU, restart=unless-stopped).
# Assumes ./scripts/build.sh has produced predpep:local.

set -euo pipefail

IMAGE=predpep:local
CONTAINER=predpep_app

if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "error: image '${IMAGE}' not found — run ./scripts/build.sh first" >&2
  exit 1
fi

if docker container inspect "${CONTAINER}" >/dev/null 2>&1; then
  echo "error: container '${CONTAINER}' already exists" >&2
  echo "  running  → docker stop ${CONTAINER}" >&2
  echo "  stopped  → docker rm ${CONTAINER}" >&2
  echo "  replace  → docker rm -f ${CONTAINER} && ./scripts/run.sh" >&2
  exit 1
fi

docker run -d \
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
  -p 6363:6363 \
  --restart unless-stopped \
  "${IMAGE}"

echo
echo "${CONTAINER} started."
echo "  open:  http://localhost:6363"
echo "  logs:  docker logs -f ${CONTAINER}"
echo "  stop:  docker stop ${CONTAINER}"
