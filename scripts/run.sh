#!/usr/bin/env bash
# Launch predpep_app for production-ish use (detached, GPU, restart=unless-stopped).
# Assumes ./scripts/build.sh has produced the image for the version in ./VERSION.

set -euo pipefail

IMAGE="predpep:$(cat "$(dirname "$0")/../VERSION")"
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

ENV_ARGS=()
for v in PREDPEP_CORE_BUDGET PREDPEP_RETENTION_BYTES PREDPEP_RETENTION_DAYS PREDPEP_PARTIAL_STATUS; do
  [ -n "${!v:-}" ] && ENV_ARGS+=( -e "$v=${!v}" )
done
[ -n "${PREDPEP_MEMORY:-}" ] && ENV_ARGS+=( --memory "${PREDPEP_MEMORY}" )

# Host port 6363 (what the DDN dispatcher dials) -> container 8383 (upstream's
# bind, so a log line means the same here as on the group server).
# Data at /data, not /tmp/pepspec: see MIGRATING in README.md.
docker run -d \
  --name "${CONTAINER}" \
  "${ENV_ARGS[@]}" \
  -v predpep_data:/data \
  --log-opt max-size=10m --log-opt max-file=3 \
  --pids-limit 4096 --stop-timeout 120 \
  -p 6363:8383 \
  --restart unless-stopped \
  "${IMAGE}"

echo
echo "${CONTAINER} started."
echo "  open:  http://localhost:6363"
echo "  logs:  docker logs -f ${CONTAINER}"
echo "  stop:  docker stop ${CONTAINER}"
