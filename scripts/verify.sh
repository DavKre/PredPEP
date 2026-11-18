#!/usr/bin/env bash
# Verify a running predPEP node: is this machine like the others?
#
# The dev's package ships a verify.sh that compares the container's residue
# catalogue against the build host. This does that and the rest of what a node
# can get wrong without raising an error:
#
#   * the catalogue counts (retired=6 is the one that proves the withheld lists
#     arrived -- an image missing them reports 0, offers residues the catalogue
#     hides, and says nothing)
#   * the binaries the pipeline actually shells out to, resolved as the app user
#   * the FoldX path AND its identity, because a node quietly scoring on a
#     different FoldX build produces results that cannot be compared with the
#     fleet's, and the only evidence is one line in a job log
#   * whether auth is enforced -- a node with zero tokens is open to anyone who
#     can reach the port
#
# Usage:  ./scripts/verify.sh [container] [host-port]
#         ./scripts/verify.sh predpep_app 6363
set -uo pipefail

NAME="${1:-predpep_app}"
PORT="${2:-6363}"
PY=/home/spacepep/miniforge3/envs/predPEP/bin/python
fail=0
ok()  { printf '  %-6s %s\n' "PASS" "$*"; }
bad() { printf '  %-6s %s\n' "FAIL" "$*"; fail=$((fail + 1)); }
note(){ printf '  %-6s %s\n' "info" "$*"; }

docker container inspect "$NAME" >/dev/null 2>&1 || {
  echo "no such container: $NAME" >&2; exit 1; }
[ "$(docker inspect -f '{{.State.Status}}' "$NAME")" = running ] \
  && ok "container $NAME is running" || bad "container $NAME is not running"

echo "node"
h=$(curl -fsS --max-time 10 "http://localhost:$PORT/health" 2>/dev/null)
if [ -n "$h" ]; then
  note "$h"
  v=$(printf '%s' "$h" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p')
  exp="$(cat "$(dirname "$0")/../VERSION" 2>/dev/null | tr -d '[:space:]')"
  [ -n "$exp" ] && { [ "$v" = "$exp" ] && ok "version $v matches ./VERSION" \
      || bad "version $v but ./VERSION says $exp -- a staged rollout mixing two releases"; }
else
  bad "/health did not answer on :$PORT"
fi

echo "residue catalogue (must match the build host)"
c=$(docker exec "$NAME" "$PY" -c \
  "import sys;sys.path.insert(0,'/opt/spacepep-predPEP');import residue_catalogue as r;print(r.load_catalogue()['counts'])" 2>/dev/null)
if [ -n "$c" ]; then
  note "$c"
  for pair in "'noncanonical': 397" "'retired': 6" "'d_offerable': 208" \
              "'l_offerable': 189" "'foldx_scorable': 2" "'canonical': 20"; do
    case "$c" in *"$pair"*) ok "$pair" ;; *) bad "$pair -- got something else" ;; esac
  done
else
  bad "could not read the catalogue inside the container"
fi

echo "binaries (resolved as the app user, which is the user that runs them)"
for exe in pepspec.static.linuxgccrelease rosetta_scripts.static.linuxgccrelease \
           clean_pdb.py run_iteMAN.py run_ncaaDesign.py zip unzip; do
  p=$(docker exec "$NAME" bash -lc "command -v $exe" 2>/dev/null)
  [ -n "$p" ] && ok "$exe -> $p" || bad "$exe is not on PATH"
done

echo "FoldX (identity, not just presence)"
fx=$(docker exec "$NAME" bash -lc 'echo "${PREDPEP_FOLDX}"' 2>/dev/null)
if docker exec "$NAME" test -x "$fx" 2>/dev/null; then
  sum=$(docker exec "$NAME" sh -c "sha256sum '$fx' | cut -c1-16" 2>/dev/null)
  sz=$(docker exec "$NAME" sh -c "stat -c %s '$fx'" 2>/dev/null)
  ok "$fx"
  note "sha256:$sum  bytes:$sz  <- record this; every node in the fleet must match"
else
  bad "PREDPEP_FOLDX ('$fx') is not executable by the app user"
fi
docker exec "$NAME" bash -lc 'command -v foldx_20270131' >/dev/null 2>&1 \
  && ok "foldx_20270131 shim present" \
  || bad "foldx_20270131 shim missing -- a bare-name FoldX call would score nothing, silently"

echo "auth"
s=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://localhost:$PORT/state")
case "$s" in
  401) ok "/state requires a token (auth enforced)" ;;
  200) bad "/state answered 200 -- this node has NO tokens and is open to anyone who can reach :$PORT. Mint one: see docs/UPGRADE-v1.6.0-node.md" ;;
  *)   bad "/state returned $s" ;;
esac

echo "work in flight (check before any redeploy -- rm -f kills jobs with no grace period)"
j=$(curl -fsS --max-time 10 "http://localhost:$PORT/jobs" 2>/dev/null)
if [ -n "$j" ]; then
  busy=$(printf '%s' "$j" | grep -o '"status": *"\(Running\|Queued\)"' | wc -l)
  [ "$busy" -eq 0 ] && ok "nothing Running or Queued" || note "$busy job(s) Running/Queued -- do not redeploy yet"
else
  note "/jobs needs a token on an authenticated node; check it with one before redeploying"
fi

echo
echo "===== $fail failure(s) ====="
exit $fail
