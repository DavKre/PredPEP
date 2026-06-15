#!/usr/bin/env bash
# Build the predpep:<version> image (version read from ./VERSION), also tagging :latest.
#
# The Dockerfile uses `RUN --mount=type=bind` to extract blobs without copying
# them into an image layer — this requires BuildKit. BuildKit is the default in
# Docker 23+; the explicit DOCKER_BUILDKIT=1 below is kept for compatibility
# with older Docker versions where BuildKit must be opted into.
# --progress=plain so the long Rosetta tar extraction is visible in the log.

set -euo pipefail

# Build context is the repo root (one level up from scripts/).
cd "$(dirname "$0")/.."

# Opt-in blob integrity check: CHECK_BLOBS=1 ./scripts/build.sh
if [ "${CHECK_BLOBS:-0}" = "1" ] && [ -f blobs/blobs.sha256 ]; then
  echo "Verifying blob checksums (CHECK_BLOBS=1)…"
  ( cd blobs && sha256sum -c blobs.sha256 ) || { echo "ERROR: blob checksum mismatch." >&2; exit 1; }
fi

VERSION="$(cat VERSION)"

DOCKER_BUILDKIT=1 docker build \
  --progress=plain \
  --build-arg VERSION="${VERSION}" \
  -t "predpep:${VERSION}" \
  -t predpep:latest \
  .

echo "Built predpep:${VERSION} (also tagged predpep:latest)."
