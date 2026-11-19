# Upgrading a node from v1.4.0 to v1.6.0

What changes on a fleet node, and what the dispatcher has to change with it.

The dev's own notes for v1.6.0 are vendored under [`upstream-v1.6.0/`](upstream-v1.6.0/)
— `UPGRADE-v1.6.0.md` (the HTTP surface, split jobs, the NCAA pass) and
`CONNECTIONS.md` (the app/pipeline boundary, what a job writes, where resources
come from). This file covers only what is different about **our** node image and
what an operator has to do.

## Where this image differs from the dev's predpep:v1.6.0

Same application, same pipeline, same conda environment, same paths. Two
deliberate differences:

| | dev's `predpep:v1.6.0` | this node image |
|---|---|---|
| Rosetta + FoldX Pro | mounted read-only from the host; refuses to start without them | **baked in**, pruned to what the pipeline runs |
| Image size | ~1.5 GB + whatever the host's trees cost | one self-contained image |
| Host port | 8383 | **6363** (what the DDN dispatcher already dials), mapped to 8383 inside |

A fleet node is a workstation that does not have its own licensed Rosetta and
FoldX trees, and a node that refuses to start without them is not deployable.
Everything else stays identical on purpose: a path in a log, a flag file or a
traceback means the same thing on a node as on the group server.

The prune keeps `pepspec.static.linuxgccrelease` (the design loop),
`rosetta_scripts.static.linuxgccrelease` (**new in v1.6.0** — the non-canonical
design pass), `main/database`, and `main/tools/protein_tools` (`clean_pdb.py`,
which `run_pepSpecPipe.sh` calls by bare name). Dropping `rosetta_scripts` would
not have failed the build or any job: a failure in the NCAA pass deliberately
cannot change a job's verdict, so every job would still have reported
`complete`, with the pass silently never run.

## What the operator has to do

### 1. The data directory moved: `/tmp/pepspec` → `/data`

v1.4.0 kept results and uploads under `/tmp/pepspec`. v1.6.0 uses `/data`, with
four subdirectories — `results/`, `uploads/`, `archive/`, `state/`. `state/`
holds the token store, which is new.

`scripts/deploy.sh`, `scripts/run.sh` and `docker-compose.yml` are already
updated. On a node whose bind-mount still has the v1.4.0 layout, move the two
directories once, before starting the new container:

    cd "$PREDPEP_DATA"          # e.g. ~/predpep_data
    mkdir -p archive state
    # results/ and uploads/ are already at the top level — nothing to move.
    # If the host dir instead CONTAINS a pepspec/ folder, flatten it:
    #   mv pepspec/results pepspec/uploads . && rmdir pepspec

Job history that is not moved is not lost, only invisible: the jobs list reads
`PREDPEP_RESULT_DIR`.

### 2. Drain the node first

`deploy.sh` now refuses to recreate the container while `/jobs` reports anything
`Running` or `Queued` (`FORCE=1` overrides). This is not caution for its own
sake: recreating SIGKILLs the pipeline with no grace period, and an orphaned job
is reconciled to **failed** on the next boot — never resumed. A 14-hour job dies
with it.

    curl -fsS http://localhost:6363/jobs | grep -c '"status": *"Running"'

### 3. Mint the first token

v1.6.0 has bearer-token auth, and it is enforced **only once a token exists**.
A fresh node with zero tokens is open to anyone who can reach the port, which is
also how the first admin token gets minted unauthenticated:

    curl -fsS -X POST http://localhost:6363/api/tokens \
         -H 'Content-Type: application/json' \
         -d '{"label":"ddn-dispatcher","owner":"ddn","scope":"submit"}'

The plaintext is returned exactly once and stored only as a SHA-256 hash. Scopes
rank `read < submit < admin`. `/health` stays unauthenticated by design — a
liveness probe must not depend on holding a token — and reports the node
version, which is what lets a dispatcher refuse to mix results across a staged
rollout.

### 4. Verify the node matches the fleet

The build already fails if the residue catalogue inside the image does not match
the build host's counts, so a built image cannot be wrong about this. To confirm
a *running* node:

    docker exec predpep_app /home/spacepep/miniforge3/envs/predPEP/bin/python -c \
      "import sys; sys.path.insert(0,'/opt/spacepep-predPEP'); import residue_catalogue; \
       print(residue_catalogue.load_catalogue()['counts'])"

Expect `noncanonical 397`, `retired 6`, `d_offerable 208`, `foldx_scorable 2`.
`retired: 6` is the field that matters — an image whose withheld lists did not
arrive reports `retired: 0`, offers residues the catalogue hides, raises no
error, and silently widens the design space on that one machine.

## What the DDN dispatcher must change

**One change is mandatory. It will not raise an error; it will undercount.**

`POST /upload` can now return several job ids. When the uploaded peptide chain is
discontinuous, v1.6.0 splits the submission into one sub-job per segment:

```json
{ "success": true, "split": true, "parent_job": "<parent id>",
  "jobs": [ {"job_id": "...", "segment": 1, "cores": 6, "cores_note": null},
            {"job_id": "...", "segment": 2, "cores": 6} ],
  "job_id": "<jobs[0].job_id>", "status": "queued", "failures": null }
```

The top-level `job_id` is backward compatibility: a v1.4-era client reads a valid
id and does not crash. It reads **only the first** sub-job. The others run,
reserve cores, and are invisible to it — so the dispatcher under-counts capacity,
calls the submission finished while a sibling is still running, and fetches one
segment's designs as if they were all of them.

The fix is one branch: **if `split` is true, enumerate `jobs[]` and track every
`job_id`.** With it:

- **Do not poll `parent_job`.** Its result directory is deliberately removed, so
  `/status` on it finds nothing.
- **Surface `failures[]`.** A split can partly succeed; `failures` is then a list
  of reasons and should not be swallowed.

Two further fields are new on the non-split response and harmless to a tolerant
client: `cores` (what was actually reserved) and `cores_note` (why a 32-core
request ran on 6). Show `cores_note` to whoever submitted.

Everything else in the HTTP surface is unchanged — same 21 routes, and
`/status/<job_id>` returns the same keys it always did.

### Measured on this fork, 19 November 2026

Three real jobs were run on the merged image (and one on the v1.4.0 prod node for
comparison). What they showed, beyond what the dev's notes state:

**Polling `parent_job` reports FAILURE, not nothing.** The note says a parent
"does not appear in the results list" and that polling it "will not find
anything". In practice `GET /status/<parent>` returns:

    {"message": "Job failed to start.", "status": "Pending/Failed"}

while both sub-jobs run normally. A dispatcher that polls the parent does not
merely lose track of the work — it reports a healthy submission as failed. Poll
the ids in `jobs[]`, never the parent.

**Capacity undercount, measured.** The split submission reserved **4** cores on
an 8-core node while a client reading only `jobs[0]` would believe 2.

**No `design_spec` means no non-canonical design.** `design_spec.json` is only
written when the submission carries a `design_spec` form field. A v1.4-style
POST (protein_symbol / user_name / cpus / file1) produces no spec, so the NCAA
pass logs `could not read design_spec.json` into `ncaa_status.json` and skips —
and the job still completes normally. The headline v1.6.0 feature is therefore
**opt-in per submission**: until the dispatcher sends a palette, every job it
submits is canonical-only.

**The pass placed no non-canonical in any test here.** Across a 4-glycine toy
peptide and the 1YCR MDM2/p53 complex, with 2-residue and 16-residue D palettes,
under both `talaris2013_split_unfolded.wts` and `pre_talaris_2013_standard.wts`:
`designs_with_noncanonical: 0` every time. The machinery is demonstrably correct
— params load, rotamer libraries load, the packer palette parses, the control
runs, `failures: []` — the packer simply did not select a D residue onto a
backbone already optimised for canonicals, which is the limitation
`run_ncaaDesign.py` prints itself. Treat "the pass ran" and "a non-canonical was
designed" as separate claims, and read `designs_with_noncanonical` for the
second. The dev measured 4 of 6 designs beating the control on hakg; that was
not reproduced here, on a different target with a different palette.

**`delta_dG_vs_control` has a noise floor of roughly 0.25 kcal/mol.** On three
designs that contained **no** non-canonical at all, the column still read
+0.227, -0.193 and +0.227 — design and control are both canonical-only there, so
that spread is packer run-to-run variance at `nstruct 1`/`nloop 10`. The column
is described as what the non-canonical itself contributed; below about
0.25 kcal/mol it says nothing of the sort.

**Two notes for the dev** (neither affects this merge):
`/opt/spacepep-predPEP/catalogue.csv` is a dangling symlink to
`all-residues-list.csv`, which the shipped residue library does not contain —
identical in their own image, and only `test_dres.py` reads it. And the group
server's compose health-checks `/state`, which starts returning 401 the moment
the first token is minted on that node.

### Still gated off: `Partial`

`PREDPEP_PARTIAL_STATUS` remains **0**. Until the dispatcher treats `Partial` as
a terminal status, a job that reports it would be mapped to *running* and polled
forever by a client with no wall-clock timeout. Until then, partial jobs report
`Complete` with `partial: true` and `failed_stage`. Flip the gate on the nodes
only after the dispatcher ships support — see
[incident-SPILRH_c70d2d4c.md](incident-SPILRH_c70d2d4c.md).

## Results from v1.4.0 nodes are not comparable to v1.6.0 results

Worth stating plainly, because nothing in the output records it:

- **FoldX changed force field.** v1.4.0 nodes bundled a real FoldX 5; v1.6.0 uses
  FoldX Pro, which the dev measured at 0.46 kcal/mol lower (sd 0.42) across 15
  designs — enough to reorder the leaders. Every v1.6.0 job logs
  `FoldX binary: <path>` to stderr, so from now on each job says what scored it.
- **FoldX emitted two rows per model** before v1.6.0, the second a whole-complex
  stability energy rather than an interaction energy.
- **Non-canonical scoring changed on 18 November 2026** with
  `PREDPEP_NCAA_WEIGHTS=talaris2013_split_unfolded.wts`, which this image bakes
  in. Unset, the pass charges every non-canonical some canonical residue's
  reference energy.

Re-run anything whose ranking matters rather than comparing across the versions.
