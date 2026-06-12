# Phase 2A.2 — Rosetta Runtime Prune (Design)

**Date:** 2026-06-12
**Status:** Approved (design); pending spec review
**Scope:** Shrink the image by pruning the Rosetta tree to only what the pipeline runs —
**at image-build time only**, with no change to local files. Follow-on to Phase 2A.

## Context

The image is 52 GB; ~49 GB of it is the Rosetta binary release
(`/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408`). Investigation showed the
pipeline uses a tiny fraction of it:

- The only Rosetta binary invoked anywhere is `pepspec.static.linuxgccrelease`, a **184 MB
  statically-linked** executable (`ldd` → "not a dynamic executable" — zero shared-lib deps).
  It lives inside `main/source/build/` (43 GB), which holds every Rosetta application compiled
  in every variant (static/default/mpi × release/debug). We run exactly one.
- `pepspec` is given `-database …/main/database` (3.3 GB) explicitly — required runtime science
  data (score weights, residue chemistry, rotamer/fragment libraries).
- `clean_pdb.py` (from `main/tools/protein_tools/scripts/`, 312 KB) is called to split chains;
  it is standalone Python (imports only stdlib + its sibling `amino_acids.py`).

Everything else — the other ~hundreds of apps/variants in `build/`, `source/external` (1.4 GB,
build-time libs already statically baked in), `source/src`, `source/test`, the rest of `tools/`,
`rosetta_scripts_scripts` — is never touched by this pipeline.

## Hard constraints

- **No local deletion.** `blobs/rosetta.tar.gz` and the locally-extracted Rosetta tree stay
  fully intact. The prune happens **only inside the Dockerfile `RUN`** that extracts Rosetta,
  before that layer is committed — so the image holds the pruned tree while local disk is
  untouched.
- In-image paths unchanged; `pipeline/` scripts not modified.
- `predpep_app` cutover is authorized (replacing the current slim image). Keep a rollback.

## Keep-list (verified) vs drop-list

**Keep** (≈ 3.5 GB):
- `main/database/` — 3.3 GB (kept **whole**; pruning inside it is risky and not worth it).
- `main/source/build/src/release/linux/5.4/64/x86/gcc/7/static/pepspec.static.linuxgccrelease`
  — 184 MB (the one binary).
- `main/source/bin/` — 928 KB of symlinks (so PATH resolves the binary; dangling links to
  removed apps are harmless).
- `main/tools/protein_tools/` — holds `scripts/clean_pdb.py` + `amino_acids.py`.

**Drop** (≈ 45 GB): all of `main/source/build/` except the one binary; `main/source/external`,
`src`, `test`, `ide`, `xcode`, etc.; everything in `main/tools/` except `protein_tools`;
`main/rosetta_scripts_scripts`; and any other non-kept top-level items under `main/`.

## Prune mechanism

In the existing blob-extraction `RUN` (the `--mount=type=bind,source=./blobs` step), after the
`tar -xzf rosetta.tar.gz`, add a prune that keeps only the four items above. Approach
(delete-in-place; only the 184 MB binary is briefly stashed — the 3.3 GB database never moves):

1. `mv` the resolved static binary to `/tmp`, `rm -rf main/source/build`, recreate its exact
   directory path, `mv` it back (so `bin/` symlink resolution still works).
2. In `main/source/`, delete everything except `bin` and `build`.
3. In `main/tools/`, delete everything except `protein_tools`.
4. In `main/`, delete everything except `database`, `source`, `tools`.

All in the same `RUN` so the 49 GB never lands in a committed layer. Doing it in the bind-mount
RUN keeps `rosetta.tar.gz` itself out of every layer (unchanged from Phase 1/2A).

## Verification (iteration-1 proof)

A missing Rosetta file makes `pepspec` error within seconds of its first invocation, so one
clean iteration is conclusive. Steps:

1. Retag the current 52 GB image `predpep:phase2a-slim` (rollback) before building.
2. Build the pruned image; record the new size (expect ~7–8 GB).
3. Throwaway `predpep_smoke` on 6364; `/health` OK; `pepspec.static.linuxgccrelease -help`
   loads; the resolved binary path + `main/database` present.
4. Submit the real `testdata/SPEGFH.pdb` job (`cpus=2`); monitor until **iteration 1 completes**
   — signalled by an `*_iter2_*` directory appearing (which means `clean_pdb.py` → `pepspec`
   runs → FoldX scoring → aggregation/selection all succeeded on the pruned tree). A `pepspec`
   startup error (missing file) instead → STOP and investigate; rollback retained.

## Cutover + rollback

After iteration 1 verifies: merge to `main`, then `docker rm -f predpep_app` and
`./scripts/run.sh` (pruned `predpep:local`) on 6363; confirm `/health`. Remove `predpep_smoke`.

Rollback chain retained: `predpep:phase2a-slim` (52 GB, pre-prune) and `predpep:phase1-cuda`
(63.8 GB, pre-2A). Both can be `docker rmi`'d later to reclaim disk once confidence is high
(noted, not done here).

## Risks

- **Low–medium.** The binary is static (self-contained), the database is kept whole, and only
  one binary + one standalone script are used. Residual risk is `pepspec` reading an
  unexpected path outside `database/` — which the iteration-1 job would expose immediately.
- Build transiently needs ~49 GB free (full extract) before the prune drops it; the host has
  ample space (the un-pruned 52 GB image already builds).

## Success criteria

- Pruned image builds; size ~7–8 GB (record exact before/after).
- `pepspec` loads and a real `SPEGFH.pdb` job completes iteration 1 on the pruned image.
- `predpep_app` cut over to the pruned image, `/health` healthy.
- `predpep:phase2a-slim` retained as rollback. No local files deleted.
- Clean git history on `main`.
