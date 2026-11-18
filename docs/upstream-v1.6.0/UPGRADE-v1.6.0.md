# predPEP v1.4 → v1.6.0 — upgrade note

Nov 18, 2026 · @hakg

For deploying predPEP v1.6.0 onto work servers currently running v1.4, where a server connector and CPU-based job distribution are already in place.

## What did not change

**The HTTP surface is identical.** A connector written against v1.4 needs no changes to dispatch or to poll.

The route table is the same 21 paths, with nothing added and nothing removed:

```
/  /health  /state  /upload  /status/<job_id>  /jobs  /jobs/<job_id>
/jobs/<job_id>/stop  /jobs/<job_id>/log  /jobs/<job_id>/spec
/jobs/<job_id>/metrics  /jobs/<job_id>/analytics
/results_data/<job_id>  /get_tmap_tree/<job_id>
/stream_final_pdb/<job_id>/<path:relative_path>
/download/<master_dir_name>/<filename>
/api/residues  /api/residues/reload
/api/tokens  /api/tokens/<token_id>  /api/audit
```

`/status/<job_id>` returns the same keys it always did — `success`, `status`, `message`, `error`, `failed_stage`, `partial`, `artifacts`, `download_url`. Polling logic carries over untouched.

`/health` is unauthenticated by design, because a liveness probe must not depend on holding a token, and it reports the node version. `/state` requires a `read` token and is the dispatch endpoint.

**One assumption to confirm.** This comparison was made against the 21 October source drop, taken to be v1.4 on the grounds that the `pre-v151` files date from 9 November, placing v1.5.1 after it. If the v1.4 actually deployed on the work servers predates that drop, the delta is larger than what is written here and should be re-measured before rollout.

## What changed: one submission can return several job ids

**This is the only change that needs code on the client side.** It will not raise an error; it will quietly undercount.

When the peptide chain is discontinuous, v1.6.0 splits the submission into one sub-job per segment — each designing its own segment with the others treated as part of the receptor — and `/upload` returns:

```json
{
  "success": true,
  "split": true,
  "parent_job": "<parent id>",
  "jobs": [
    { "job_id": "...", "segment": 1, "queue_position": 0,
      "cores": 6, "cores_note": null, "peptide": "...",
      "receptor_chains": [...], "anchor_positions": [...],
      "caveat": null, "warning": null },
    { "job_id": "...", "segment": 2, ... }
  ],
  "job_id": "<jobs[0].job_id>",
  "status": "queued",
  "message": "<why it was split>",
  "failures": null
}
```

The top-level `job_id` is deliberate backward compatibility — the source comments it `# for clients expecting one id`. So a v1.4-era client reads a valid id and does not crash.

It reads only the **first** sub-job. The others run, reserve cores, and are invisible to it. Three consequences, none of which surface as an error:

1. **Capacity undercount.** The caller believes one job is running; two or more are, each with its own core reservation. A machine thought to have headroom gets over-committed.
2. **Premature completion.** When sub-job 1 finishes, the submission looks done while sub-job 2 is still running.
3. **Partial results.** `/results_data/<first id>` returns one segment's designs, with nothing indicating the rest exist.

The fix is one branch: if `split` is true, enumerate `jobs[]` and track every `job_id`.

Two cautions that go with it:

- **Do not poll `parent_job`.** Its result directory is deliberately removed, so that a parent with no work of its own does not appear in the results list as a job that never started. Polling `/status` on it will not find anything.
- **Check `failures`.** A split can partly succeed — some segments created, others not. `failures` is then a list of reasons and should be surfaced, not swallowed.

The non-split response also gained two fields, `cores` and `cores_note`. Added fields are harmless to a tolerant client, but `cores_note` is the human-readable explanation of why a 32-core request ran on 6, and is worth showing to whoever submitted.

## Deploying it

The package is `predpep-tool-v1.6.0.tar.gz`. Unpack it and run the three scripts in order.

| script | what it does |
| --- | --- |
| `scripts/load.sh` | loads the image tags from `images/` and confirms they arrived |
| `scripts/run.sh` | starts the container; **refuses to start** if the FoldX binary is not executable, rather than failing hours into a job |
| `scripts/verify.sh` | checks the running container's residue catalogue against the build host's counts |

### What is not in the image

The image carries the application and the residue library only; both binary trees are mounted read-only from the host at run time. A machine without its own licensed copies cannot run predPEP, and copying those trees between machines is a licensing question, not a deployment step.

The FoldX binary is resolved from `PREDPEP_FOLDX`, defaulting to `/usr/local/foldxpro_20261231/foldxpro20261231`. If it is absent or not executable the container exits with status 3 and an explicit message.

### Where everything lands inside the container

The image reproduces hakg's layout rather than inventing its own, so a path in a log, a flag file or a traceback means the same thing in both places.

| Inside the container | What | Copied from |
| --- | --- | --- |
| `/opt/spacepep-predPEP` | the Flask app, served by gunicorn on `0.0.0.0:8383` | `app/` |
| `/usr/local/pepspec_pipe` | every pipeline script — the stages that call Rosetta and FoldX | `pipeline/` |
| `/opt/spacepep-createFF/all_aas3` | the residue library: 195 params, 176 rotlibs, the catalogue CSVs | `residues/` |
| `/usr/local/bin/predpep-entrypoint` | the entrypoint: preflight, then gunicorn | `entrypoint.sh` |
| `/data` | results, uploads, archive, state — the named volume's mount point | *(created empty)* |
| `/home/spacepep/miniforge3/envs/predPEP` | the conda environment | *(built from `environment.yml`)* |

The working directory is `/opt/spacepep-predPEP`, and the app runs as `spacepep` (uid 1000) after the entrypoint drops privileges with `gosu`.

**Every pipeline script is symlinked into `/usr/local/bin`.** The image walks `/usr/local/pepspec_pipe` for `*.py` and `*.sh`, makes each executable, and links it, so `run_iteMAN.py`, `run_mergeScores.py`, `run_pepSpecPipe.sh`, `run_foldX.sh`, `run_foldX2.sh`, `run_ncaaDesign.py` and the rest are all callable by bare name. That is how `PREDPEP_MANAGER` can default to `/usr/local/bin/run_iteMAN.py`, and how the FoldX stage can call `run_foldX2.sh` without a path.

This differs from hakg in one way worth knowing. On hakg only some scripts are linked — `run_iteMAN.py` and `run_mergeScores.py` are, `run_ncaaDesign.py` is not — so `run_iteMAN.py` locates the NCAA script by trying `/usr/local/bin` first and falling back to `/usr/local/pepspec_pipe`. In the container the first attempt succeeds. The fallback exists precisely so the same code works in both, and a hardcoded path there would mean the pass silently never runs on one of them — which, since a failure in that pass deliberately cannot change a job's verdict, would still report `complete`.

**The shebangs are rewritten at build time.** Twenty-two scripts carried `#!/root/mambaforge/envs/predPEP/bin/python` or `#!/usr/bin/env python` from hakg; the build rewrites each to the image's interpreter and then refuses to finish if any script names an interpreter the image does not have. So a script copied in by hand later — with `docker cp`, say — keeps hakg's shebang and fails with a bare `not found`, which names the file but not the cause.

**`clean_pdb.py` is a symlink into the mounted Rosetta tree**, at `/usr/local/bin/clean_pdb.py` pointing to `…/main/tools/protein_tools/scripts/clean_pdb.py`. It therefore dangles until Rosetta is mounted, which is why preflight checks it by name: `run_pepSpecPipe.sh` calls it bare, and without it every job fails before its first pepspec run.

### Configuring it

`run.sh` calls `docker run` directly and does not use the compose files. The variables it reads are host-side:

| variable | default | purpose |
| --- | --- | --- |
| `ROSETTA_HOST_DIR` | `/usr/local/rosetta_pkgs/rosetta.binary.ubuntu.release-408` | Rosetta tree, mounted read-only at the same path inside the container |
| `FOLDX_HOST_DIR` | `/usr/local/foldxpro_20261231` | FoldX Pro tree, mounted read-only at the same path |
| `PREDPEP_PORT` | `8383` | host port, mapped to 8383 inside |
| `PREDPEP_CORE_BUDGET` | `8` | sets both the container's `--cpus` and the scheduler's ceiling |
| `PREDPEP_NAME` | `predpep-v160` | container name |
| `PREDPEP_VOLUME` | `predpep-data` | named volume mounted at `/data` |

It refuses to start unless both directories exist and `$FOLDX_HOST_DIR/foldxpro20261231` is executable.

The container-side paths — `PREDPEP_RESULT_DIR`, `PREDPEP_UPLOAD_DIR`, `PREDPEP_ARCHIVE_DIR`, `PREDPEP_STATE_DIR` — are set by the script to locations under `/data` and need no configuration. If the FoldX tree sits at the default path, `PREDPEP_FOLDX` needs no setting either.

State lives in the named volume, not on a host path, so it survives restarts and even removal of the container.

**The compose files are a separate route.** Both services are gated behind profiles, so `docker compose up` with no profile creates the network and volume and starts nothing at all. The slim service is `docker compose --profile slim up -d`, and it reads `.env` — which is what `.env.example` is for. The `bundled` profile names an image that is deliberately not shipped, because that is the one with the licensed binaries baked in.

### `run.sh` destroys the running container

Its first act is `docker rm -f "$NAME"`, which SIGKILLs whatever is running under that name. **Jobs in flight die immediately, with no prompt and no grace period.**

That is right for a first install and wrong for an upgrade on a busy machine. Before running it anywhere with live work, check `/state` for `running` and `queued` and wait for an idle window. The compose route sets `stop_grace_period: 2m` to let the scheduler finalise jobs on stop; `run.sh` gives no such courtesy.

### First token

Authentication is a bearer token in the `Authorization` header, with scopes ranked `read < submit < admin`. On a fresh node **no tokens exist and every route is open**, which is how `POST /api/tokens` can mint the first admin token unauthenticated. From the moment one token exists, every protected route requires one.

So mint that token as the first action after the container is up. A node left with zero tokens is unauthenticated to anyone who can reach the port. The plaintext is returned exactly once, at creation, and is stored only as a SHA-256 hash — a lost token is replaced, never recovered.

## The NCAA design pass

Four environment variables govern it. One of them changes results, and a deployment that launches containers without it will score non-canonicals the way they were scored before 18 November 2026.

**`PREDPEP_NCAA_WEIGHTS`** is that one. Unset, the pass uses `pre_talaris_2013_standard.wts`, which scores non-canonicals through the `ref` term. `ref` is indexed by the AA enum, and every non-canonical params file in this build declares `AA UNK`, so `ref` structurally cannot hold a per-type reference energy — it charges each non-canonical some canonical residue's value instead. `talaris2013_split_unfolded.wts` replaces `ref` with `unfolded` and `split_unfolded_two_body`, which assemble reference energies from MM atom types; it carries no `ref` line at all, so there is no double counting, and it sets `SPLIT_UNFOLDED_LABEL_TYPE` itself.

Measured on hakg, 18 November 2026: with those weights, 4 of 6 designs beat a canonical-only control built on the same backbone with the same protocol and the same `nloop`. Without them that comparison is not meaningful. The package's `run.sh` and `.env.example` now set it, so an unpacked deployment gets it; anything that builds its own `docker run` must pass it explicitly.

The other three are safe at their defaults:

| variable | default | what it does |
| --- | --- | --- |
| `PREDPEP_NCAA_TOP` | 10 | how many of `top10/`'s models are designed on |
| `PREDPEP_NCAA_TIMEOUT` | 3600 | budget for the whole pass, in seconds |
| `PREDPEP_NCAA_CONTROL` | on | the canonical-only control; set 0 to skip it |

Without the control the table carries only `delta_dG_vs_parent`, which includes repacking the whole peptide and so cannot say what the non-canonical itself contributed.

### Why the pass used to overrun its budget

`extrachi_cutoff` on the design protocol's `ExtraRotamersGeneric` was 0 until 18 November, giving ex1 plus ex2 extra rotamers to **every** position with no neighbour threshold, across a palette of about 190 residue types. Measured on one structure:

| cutoff | weights | per structure |
| --- | --- | --- |
| 0 | pre\_talaris | 741 s |
| 0 | split\_unfolded | about 2700 s |
| 18 | pre\_talaris | 42 s |
| 18 | split\_unfolded | 73 s |

18 is Rosetta's own default — extra rotamers on buried positions, where they matter, and not on exposed sidechains. The cutoff was nearly all of the pass cost: a 3600 s budget had been completing five structures and then being abandoned, silently, because the file that records the failure was itself being deleted by the post-job cleanup sweep. `--nstruct` also defaulted to 5 and produced **bit-identical** structures, since `nloop` 10 already converges the packer; it is now 1.

A full pass is now ten distinct backbones plus their controls in roughly 25 minutes, against the same 3600 s default that used to be inadequate. So this build needs no timeout override.

### Three viewer fixes worth knowing about

All three predate v1.6.0 and affected every job, so a node upgraded from an earlier build will behave visibly differently.

- **Designs from the NCAA pass returned 403 in the structure viewer.** `/stream_final_pdb` required the model path to begin with the job directory name. Pepspec's `<job>_iterN_xxxx/` paths satisfy that by coincidence; `ncaa_designs/` paths cannot. No design from the NCAA pass had ever been viewable in any job.
- **The ΔG sep column had no heading.** The column is conditional on some row carrying a value, and the header was rendered once at page load while the table was still empty — so the heading never appeared, and every column after Interface sat one place to the left of its label. Clicking any sortable heading rebuilt it and the table snapped into alignment, which is why it looked intermittent.
- **Archived models are now readable from the result zip.** The post-job sweep removes the model directories but the zip keeps every model. The viewer now reads the member straight out of the archive — a seek, not an extraction — so all of them stay viewable rather than only the ten in `top10/`.

## Keeping the machines consistent

With one job per machine, a single results table can no longer mix force fields. What remains is **divergence between machines**: one server quietly scoring on a different FoldX build, or offering a different set of design residues, so that results from different jobs are not comparable. Nothing in the output records it.

Two checks make that a deploy-time problem instead of an archaeology problem.

### Run `verify.sh` on every machine

It compares the container's residue catalogue against the build host's counts. The field that matters most is `retired: 6`.

That field proves the withheld-residue lists reached the image. An image missing them reports `retired: 0`, offers residues the build host deliberately hides, raises no error, and silently widens the design space on that one machine. A design using a residue only one server will accept is not reproducible anywhere else in the fleet.

### Confirm the FoldX binary agrees

Each v1.6.0 job logs `FoldX binary: <path>` to its stderr. That makes every machine correct about itself, but nothing compares machines to each other.

Worth doing at rollout: record the resolved path **and the binary's version string** from each server, and confirm all of them match. A machine with an older Pro build, or a stale FoldX 5 tree sitting at the expected path, will produce scores on a different scale and the only evidence will be one line buried in a job log.

Extending `verify.sh` to print that path and version would fold this into the check already being run — one script answering "is this machine like the others".

### Version skew during a staged rollout

`/health` is unauthenticated and reports the node version, so a dispatcher can confirm what it is talking to before submitting. During a rollout spread over days, refusing to dispatch to a machine whose version is not the expected one is the cheapest guard against mixing results from two releases.

## The dispatch contract

Unchanged from v1.4, but worth stating precisely, because three details are easy to assume wrongly.

`GET /state` (needs a `read` token) returns:

```
core_budget, reserved_cores, available_cores, running, queued,
accepting, disk{used_bytes, cap_bytes, used_pct}, version, auth
```

### `accepting` is hardcoded true

It never flips, whatever the load. Dispatch on `available_cores`, not on that flag.

### `/upload` never refuses for lack of capacity — it queues

A machine with two free cores will accept a sixteen-core job and park it. There is no rejection and no error. A dispatcher that submits and records success will build an invisible queue on one machine while another sits idle.

The response says which happened:

- `status` is `running` only if nothing was ahead **and** the machine genuinely had the cores at that instant; otherwise `queued`
- `queue_position` is how many jobs are in front of it
- `cores` is what was actually reserved

Treat `queued` as "I guessed wrong" — either cancel with `POST /jobs/<job_id>/stop` and re-dispatch, or at minimum record it so a backlog does not accumulate unnoticed.

### The requested core count is clamped twice

First on submit: `max(2, min(32, CORE_BUDGET, requested))`, defaulting to 8.

Then reduced again by the work actually available to parallelise. A pepspec wave runs one process per anchor, so cores beyond the anchor count do nothing; the cap only ever reduces, and it returns `cores_note` explaining why.

Two consequences. The reserved core count **cannot be predicted from the submission** — it depends on the anchor count inside the structure. And a dispatcher keeping its own capacity ledger against *requested* cores will drift from reality within a few jobs and believe machines are fuller than they are. Reconcile against the `cores` value in the response, or simply re-poll `/state`.

### `CORE_BUDGET` differs per machine

It is `PREDPEP_CORE_BUDGET` or auto-detected, so ceilings vary across heterogeneous servers. A 32-core request on a 16-core box clamps silently to 16. Judge "adequate free CPUs" against each machine's own `core_budget` from `/state`.

### Job ids carry no host information

A job id is `<pdb_base>_<uuid4[:8]>`. Nothing in it says which server holds the job, and instances share no state — each has its own token store, uploads and results. The caller must keep the job-to-host mapping; asking the wrong machine for a job simply returns nothing found.

### The dispatch race

Nothing reserves capacity between a read of `/state` and the `POST /upload`. Two concurrent dispatches can read the same numbers and pick the same machine; the second one queues. Serialising dispatch decisions fixes it properly, but checking `status` in the response covers it without any coordination.
