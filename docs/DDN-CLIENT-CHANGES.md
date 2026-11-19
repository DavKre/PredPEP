# What the DDN dispatcher must change for predPEP v1.6.0

For the team that owns the dispatcher. Nothing here is a node-side bug; these are
client-side changes the node's v1.6.0 surface now requires. Ordered by how much
it costs to ignore them.

Measured against predPEP v1.6.0 on 19 November 2026 — both the group server's
deployment (`predpep-v160`, 10.100.10.10:6364) and the fleet node image.

---

## 1. Without a `design_spec`, there is no non-canonical design — at all

**This is the one that matters.** It is not an edge case: it applies to every job
the dispatcher submits today.

The node writes `design_spec.json` into the job directory **only when the
submission carries a `design_spec` form field**. A v1.4-style POST —
`protein_symbol`, `user_name`, `cpus`, `file1` — carries none. The job then runs
and completes normally, but the non-canonical design pass finds no spec, records

    {"requested": [], "incorporated": false,
     "errors": ["could not read design_spec.json: ..."]}

in `ncaa_status.json`, and skips. No error, no failed status, no warning in the
job's verdict. The result is a **fully canonical job** — exactly what v1.4
produced.

So the entire reason for this upgrade does nothing until the dispatcher sends a
palette.

### What to send

`design_spec` is one more `multipart/form-data` field, a JSON string:

```json
{
  "mode": "all",
  "palette": {
    "canonical": "all",
    "mode": "all-mixed",
    "noncanonical": ["0A1", "3CF", "4CF", "ANT", "BIU", "DBY", "..."]
  }
}
```

- `canonical` **must** be `"all"`. The packer palette is additive and pepspec
  cannot exclude canonical residues; asking for a subset is rejected rather than
  silently ignored.
- `palette.mode` is one of `l-canonical`, `l-mixed`, `d-canonical`, `d-mixed`,
  `all-mixed`. It describes the selection; the node re-derives it from the codes
  it accepted.
- `noncanonical` is a list of residue codes.

### Getting valid codes

`GET /api/residues` returns the catalogue **this node** will accept — 397
non-canonical residues (189 L, 208 D). Validate against the node's own list
rather than a hard-coded set: the catalogue is baked into the image, so a node
on an older image may not offer what another accepts.

Two rules the node enforces, both of which reject the whole submission:

- **A residue and its mirror cannot both be in one job.** `0A1` and `D0A1`
  together are refused — they share a three-letter code in the output PDB, so a
  design using one could not be told from a design using the other. Pick a hand.
- A `palette.mode` other than `l-canonical` with an empty `noncanonical` list is
  refused.

### Which palette actually produces non-canonical designs

Measured, because this is not obvious and the difference is total:

| palette | designs containing a non-canonical |
|---|---|
| broad `all-mixed` (60 residues, group server: 10 designs) | **10 of 10** |
| broad `all-mixed` (60 residues, fleet node image, 3 designs) | **3 of 3** |
| `d-mixed`, 2 residues | 0 |
| `d-mixed`, 16 residues | 0 |
| `d-canonical` (group server, 50 designs, twice) | 0 |

**Send a broad mixed palette.** D-only palettes placed nothing in any run, on
either deployment, under either scorefunction. The pass fits residues onto a
finished L-peptide backbone, and a D residue needs mirrored backbone geometry it
cannot get after the fact; D residues do appear in the output when they are a
minority option inside a large mixed palette (`DAL`, `DHI`, `DTH` and `DBY` were
all placed that way), but not when they are the only choice.

### Reading the result

- `tab3_ncaa_designs.csv` — the designs, one row each.
- `ncaa_design_report.json` — `designs_produced` vs **`designs_with_noncanonical`**.
  These are different numbers. "The pass ran" and "a non-canonical was designed"
  are separate claims; read the second field for the second claim.
- `ncaa_status.json` — **exists only when something went wrong.** Its absence is
  the success case.
- `delta_dG_vs_control` has a noise floor of roughly **0.25 kcal/mol**: on three
  designs containing no non-canonical at all it still read +0.227, −0.193 and
  +0.227, because design and control are both stochastic at `nstruct 1` /
  `nloop 10`. Do not attribute anything below that to the non-canonical. Real
  effects on the group server's jobs ran −1.8 to −3.9, well clear of it.
- Designs containing residues FoldX has no parameters for come back with **no
  FoldX score** — FoldX drops them silently rather than failing. Rank those on
  `Rosetta_Total_Score` or `interface_score`. FoldX can score only: END, PHI,
  PSI, PTR, SEP, SME, TPO.

---

## 2. One submission can return several job ids

Low priority in practice — it has never fired on the group server (8 real jobs,
none split) — but it is a one-line guard and the failure is silent.

When the uploaded peptide chain has a **physical gap** (not a deliberate choice:
the realistic cause is a structure with unresolved residues), the node splits the
submission into one sub-job per segment:

```json
{ "success": true, "split": true, "parent_job": "<parent id>",
  "jobs": [ {"job_id": "..._s1", "segment": 1, "cores": 6, "peptide": "ETFS"},
            {"job_id": "..._s2", "segment": 2, "cores": 6, "peptide": "KLLPEN"} ],
  "job_id": "..._s1", "status": "queued", "failures": null }
```

The top-level `job_id` is backward compatibility — it is `jobs[0]` and a v1.4-era
client reads it without crashing. It reads **only the first sub-job**.

**If `split` is true, enumerate `jobs[]` and track every `job_id`.** Then:

- **Never poll `parent_job`.** It is a label, not a job: nothing runs under it and
  it has no result directory. `GET /status/<parent>` returns
  `{"status": "Pending/Failed", "message": "Job failed to start."}` while the
  sub-jobs run perfectly well. A dispatcher polling the parent does not merely
  lose track of the work — it reports healthy work as failed.
- **Surface `failures[]`.** A split can partly succeed; it is then a list of
  reasons.

Measured capacity cost of ignoring this: a split submission reserved **4 cores**
on an 8-core node while a `jobs[0]`-only client believed 2.

---

## 3. Smaller things

**`cores` and `cores_note`** are new on every `/upload` response. The reserved
core count **cannot be predicted from the request** — it is clamped to
`max(2, min(32, CORE_BUDGET, requested))` and then reduced again to the work
actually available to parallelise. Reconcile capacity against `cores`, not
against what was asked for, or the ledger drifts within a few jobs.
`cores_note` is the human-readable reason a 32-core request ran on 6; show it to
whoever submitted.

**Auth.** A node with zero tokens is open to anyone who can reach the port; that
is how the first token is minted. Once one exists, every protected route needs
`Authorization: Bearer <token>`. Scopes rank `read < submit < admin`: `/state`
needs `read`, `/upload` needs `submit`. `/health` stays unauthenticated by
design — health-check that, not `/state`, which starts returning 401 the moment
a token exists.

**Dispatch assumptions that are wrong.** `accepting` is hardcoded `true` and
never flips under load — dispatch on `available_cores`. `/upload` never refuses
for lack of capacity, it **queues**: a node with two free cores accepts a
sixteen-core job and parks it, so read `status` in the response (`running` vs
`queued`) or the dispatcher builds an invisible backlog on one machine while
another idles. Nothing reserves capacity between reading `/state` and posting
`/upload`, so two concurrent dispatches can pick the same node.

**`Partial` is still gated off** on the nodes (`PREDPEP_PARTIAL_STATUS=0`) because
the current client maps unknown statuses to *running* and polls without a
wall-clock timeout — a finished job would be polled forever. Until the dispatcher
treats `Partial` as a terminal, results-available state, partial jobs report
`Complete` with `partial: true` and `failed_stage` set. **Read `partial`, not just
`status`.** Once the dispatcher handles it, the nodes can have the gate turned on.

**Version skew.** `/health` reports the node version unauthenticated. During a
staged rollout, refusing to dispatch to a node whose version is not the expected
one is the cheapest guard against mixing results from two releases — which are
**not comparable**: v1.6.0 scores with FoldX Pro, measured here at ~0.23 kcal/mol
below the FoldX 5 that v1.4.0 nodes bundle, enough to reorder the ranking (on
three identical structures the order reversed completely).
