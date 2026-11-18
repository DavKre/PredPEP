# predPEP v1.6.0

A containerised build of predPEP: the Flask front end and the pipeline around
Rosetta's `pepspec` peptide-design protocol.

This package contains **two image tags and no licensed binaries**:

    predpep:v1.6.0        the image
    predpep:v1.6.0-slim   the same image under the tag the "slim" profile expects

Rosetta and FoldX are **not** included. They are mounted read-only from the host
at run time, so you need your own licensed copies. The residue library (544 MB,
unlicensed) *is* baked into the image, pinned to this version so results are
reproducible.

## Documentation

    docs/CONNECTIONS.md        how the parts connect: the app/pipeline boundary,
                               what a job writes and who reads it, how resources
                               are found, where the scripts live and how they are
                               linked, the residue library, where it runs
    docs/UPGRADE-v1.6.0.md     the HTTP surface, split jobs, the dispatcher,
                               deploying and configuring it, the NCAA design pass

Read `CONNECTIONS.md` first if you are integrating predPEP rather than just
running it: the app and the pipeline communicate only through files in the job
directory, and that boundary is what you would be wiring into.

One caveat. `CONNECTIONS.md` contains a diagram of the job chain which does not
survive the Markdown export -- it appears there as a bracketed placeholder. The
PDF of the same document, supplied separately, carries it rendered.

## Prerequisites

- Docker, with `docker compose` (v2) or `docker-compose` 1.29 plus `--compatibility`
- Rosetta `rosetta.binary.ubuntu.release-408`, extracted
- FoldX Pro: the directory holding `foldxpro20261231` and `rotabase.txt`
- 8 cores and about 16 GB RAM for a typical job

## Install

    ./scripts/load.sh

Loads both tags from `images/predpep-v1.6.0.tar` and confirms they arrived.

## Run

With compose:

    cp .env.example .env        # set PREDPEP_VERSION=v1.6.0 and the host paths
    docker compose --profile slim up -d
    # docker-compose 1.29 ignores the CPU limit without --compatibility:
    docker-compose --compatibility --profile slim up -d

Or directly:

    ROSETTA_HOST_DIR=/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408 \
    FOLDX_HOST_DIR=/usr/local/foldxpro_20261231 \
    ./scripts/run.sh

`run.sh` refuses to start when the FoldX binary is not executable. That is
deliberate: `run_foldX2.sh` exits 3 without it, and the failure would otherwise
surface hours into a job.

## Verify

    ./scripts/verify.sh

Prints the container's preflight and compares its residue catalogue with the host
the image was built from. These must match:

    noncanonical   397
    retired          6
    d_offerable    208
    foldx_scorable   2      (reads 0 without the FoldX mount)

`retired: 6` matters most. It is the field that proves the withheld-residue lists
reached the image. An image missing them reports `retired: 0`, offers residues the
build host hides, and raises no error at all.

## The mount points are not negotiable

The Rosetta path is written into the pipeline's flag files, and every residue
params file carries an absolute `NCAA_ROTLIB_PATH`. Whatever the host paths are,
they must land at these paths inside the container:

    /usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408
    /usr/local/foldxpro_20261231

## Where results live

`PREDPEP_RESULT_DIR` and `PREDPEP_UPLOAD_DIR` default to `/tmp/pepspec/...` inside
the container, which is lost when the container is recreated. The compose file and
`run.sh` both point them at `/data` on a named volume. If you write your own run
line, set them explicitly.

## Troubleshooting

**The FoldX stage exits 3.** The binary at `PREDPEP_FOLDX` is not executable by the
app user. `/usr/local/foldxpro_*` is often root-only while the container runs as
`spacepep`; the preflight names the failing check.

**The FoldX column is empty but the job reported success.** Builds before v1.6.0
invoked FoldX by a bare name and produced nothing when it was absent, without
failing. v1.6.0 resolves it by absolute path and fails loudly instead.

**The picker offers more residues than the build host.** The withheld lists did not
reach the image. `verify.sh` catches this; `retired` reads 0.

**An analytics panel says it could not be drawn.** A figure whose container is too
small for its margins. The term-correlation heatmap needs roughly 300 px of height.

## Licensing

Rosetta and FoldX are licensed separately and are not in this package. Do not add
them to it, and do not push any predPEP image containing them to a registry outside
your licence coverage. The `-bundled` image, which does contain both, is
deliberately not shipped here.

## What changed in v1.6.0

- D-amino acids design end to end: 397 offerable residues, 189 L and 208 D
- the non-canonical design pass runs automatically after the iterations, with no
  manual step
- FoldX is FoldX Pro, resolved by absolute path and logged on every invocation
- one FoldX row per model; earlier builds emitted two, the second a whole-complex
  stability energy rather than an interaction energy
- the term-correlation heatmap draws at thumbnail size instead of reporting failure
- the Results table's D-residue filter matches all 208 D codes, not only those
  whose het code begins with D

Added 18 November, after the first v1.6.0 build:

- the non-canonical design pass is about 17x faster: `extrachi_cutoff` on its
  packer task was 0, giving extra rotamers to every position across ~190
  residue types, and is now Rosetta's default of 18. A full pass is ten models
  plus their controls in roughly 25 minutes, inside the 3600 s default
- `PREDPEP_NCAA_WEIGHTS` selects the pass's scorefunction. **It changes
  results.** `run.sh` and `.env.example` set it; see `docs/UPGRADE-v1.6.0.md`
- a canonical-only control runs on the same backbone by default, so the table
  can say what the non-canonical itself contributed rather than only what
  repacking did
- `--nstruct` defaults to 1: at `nloop` 10 the packer already converges, and 5
  produced bit-identical structures
- designs from the NCAA pass are viewable in the 3D viewer. They returned 403
  in every earlier build, because the path guard required the model path to
  begin with the job directory name
- models removed by the post-job sweep are served from the result zip, so every
  model stays viewable rather than only the ten in `top10/`
- the Results table's ΔG sep heading renders, and NCAA rows carry an iteration
