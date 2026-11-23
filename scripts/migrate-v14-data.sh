#!/usr/bin/env bash
# Migrate a v1.4.0 node's job history into a v1.6.0 node's data directory.
#
# v1.4.0 kept everything under /tmp/pepspec (results/ + uploads/); v1.6.0 uses
# /data with results/ uploads/ archive/ state/. The job directories themselves
# are unchanged -- job.json is read field by field with .get(), and the score
# CSVs are read through the legacy-column fallback -- so this is a file move,
# not a conversion.
#
#   ./scripts/migrate-v14-data.sh <old-data-dir> <new-data-dir> [--move] [--dry-run]
#
#   <old-data-dir>  host path bind-mounted to /tmp/pepspec on the v1.4.0 node
#   <new-data-dir>  host path bind-mounted to /data on the v1.6.0 node
#   --move          move instead of copy. Default is COPY, so the old node keeps
#                   working and stays the rollback.
#   --dry-run       report what would happen, touch nothing.
#
# Both directories are owned by the app user (uid 1003) and are usually not
# writable by the person running this, so the work is done inside a throwaway
# container running as root, with both paths mounted. Copies use
# --reflink=auto: on the same btrfs/xfs filesystem that is copy-on-write, so it
# is near-instant and costs no extra space, while the two copies still diverge
# independently afterwards. Hard links would NOT be safe here: the new node
# rewrites job.json on boot and would corrupt the old node's view.
#
# RETENTION. The thing worth reading before you run this. The new node evicts
# terminal jobs older than PREDPEP_RETENTION_DAYS, and then oldest-first while
# the total is over PREDPEP_RETENTION_BYTES. Eviction archives and verifies the
# job first, so the result .zip survives under archive/ -- but the job leaves
# the jobs list, which is where anyone looks for it. This script reports how
# many of the migrated jobs that applies to BEFORE copying anything.
set -euo pipefail

IMAGE="${PREDPEP_IMAGE:-predpep:v1.6.0-node}"
MODE=copy
DRY=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --move)    MODE=move ;;
    --dry-run) DRY=1 ;;
    *)         ARGS+=("$a") ;;
  esac
done
[ "${#ARGS[@]}" -eq 2 ] || { sed -n '2,22p' "$0" >&2; exit 2; }
OLD="${ARGS[0]%/}"; NEW="${ARGS[1]%/}"

[ -d "$OLD/results" ] || { echo "error: $OLD has no results/ -- is that a v1.4.0 data dir?" >&2; exit 1; }
[ -d "$NEW" ]         || { echo "error: $NEW does not exist -- deploy the new node first" >&2; exit 1; }

# Retention as the NEW node will apply it, read from the image rather than guessed.
DAYS="${PREDPEP_RETENTION_DAYS:-$(docker run --rm "$IMAGE" sh -c 'echo $PREDPEP_RETENTION_DAYS')}"
BYTES="${PREDPEP_RETENTION_BYTES:-$(docker run --rm "$IMAGE" sh -c 'echo $PREDPEP_RETENTION_BYTES')}"

echo "migrating  $OLD  ->  $NEW   (${MODE}$([ "$DRY" = 1 ] && echo ', dry run'))"
echo "new node retention: ${DAYS} days, $((BYTES / 1024 / 1024 / 1024)) GiB cap -- whichever is hit first"
echo

docker run --rm -u 0 \
  -e OLD_MODE="$MODE" -e DRY="$DRY" -e DAYS="$DAYS" -e BYTES="$BYTES" \
  -v "$OLD":/src$([ "$MODE" = copy ] && echo ':ro') \
  -v "$NEW":/dst \
  "$IMAGE" bash -c '
set -euo pipefail
shopt -s nullglob

total=0; old=0; bytes=0
for d in /src/results/*/; do
  [ -d "$d" ] || continue
  total=$((total + 1))
  age=$(( ( $(date +%s) - $(stat -c %Y "$d") ) / 86400 ))
  sz=$(du -sb "$d" | cut -f1); bytes=$((bytes + sz))
  [ "$age" -gt "$DAYS" ] && old=$((old + 1))
done
printf "  %d job(s), %.1f GB\n" "$total" "$(echo "$bytes" | awk "{print \$1/1e9}")"
if [ "$old" -gt 0 ]; then
  printf "  WARNING: %d of them are older than %s days. The first sweep (every 5 min)\n" "$old" "$DAYS"
  printf "           will archive each into archive/ and drop it from the jobs list.\n"
  printf "           Raise PREDPEP_RETENTION_DAYS on the node to keep them listed.\n"
fi
if [ "$bytes" -gt "$BYTES" ]; then
  printf "  WARNING: total exceeds the %d GiB size cap; oldest jobs will be evicted too.\n" "$((BYTES/1024/1024/1024))"
fi
[ "$DRY" = 1 ] && { echo "  dry run -- nothing written."; exit 0; }

mkdir -p /dst/results /dst/uploads /dst/archive /dst/state
for sub in results uploads; do
  [ -d "/src/$sub" ] || continue
  n=0
  for d in /src/$sub/*; do
    [ -e "$d" ] || continue
    base=$(basename "$d")
    if [ -e "/dst/$sub/$base" ]; then echo "  skip (exists): $sub/$base"; continue; fi
    if [ "$OLD_MODE" = move ]; then mv "$d" "/dst/$sub/$base"
    else cp -a --reflink=auto "$d" "/dst/$sub/$base"; fi
    n=$((n + 1))
  done
  echo "  $sub: $n entr(ies) ${OLD_MODE}d"
done
chown -R 1003:1003 /dst
echo "  destination now holds $(ls -d /dst/results/*/ 2>/dev/null | wc -l) job(s)"
'
