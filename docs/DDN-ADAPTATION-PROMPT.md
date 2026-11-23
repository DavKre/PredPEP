# predPEP v1.6.0-node — adaptation brief for the DDN dispatcher

Hand this whole file to whoever adapts the dispatcher (or to a coding agent, as
its prompt). It is self-contained: every claim below names the file and line it
comes from, so nothing here has to be taken on trust.

Node version string: **`v1.6.0-node`** (`VERSION`, reported by `/health` and
`/state` as `version`).

---

## 0. Where the code is

Three ways to read it; use whichever you have access to.

| | |
|---|---|
| **Git (private)** | `github.com:DavKre/PredPEP.git` · `ssh://git@forgejo.lan.peptide.space:2222/SpacePeptidesDev/PredPEP.git`, branch `main` |
| **Checkout on the build host** | `/home/david/DATA/OFFLINE/predpep_local` |
| **Inside a running node** | web/API layer `/opt/spacepep-predPEP/`, pipeline `/usr/local/pepspec_pipe/` — e.g. `docker exec <node> sed -n '760,800p' /usr/local/pepspec_pipe/run_iteMAN.py` |

Paths below are repo-relative. The mapping to a running container is
`app/*` → `/opt/spacepep-predPEP/*` and `pipeline/*` → `/usr/local/pepspec_pipe/*`.

The two layers talk **only through files in the job directory**. The web layer
never calls into the pipeline and vice versa, which is why a pipeline question
can always be answered by reading `run_iteMAN.py` alone.

---

## 1. THE IMPORTANT ONE: keeping it canonical-only (v1.4.0 behaviour)

v1.6.0 can design with **non-canonical** residues (NCAAs), including D-residues.
This is **additive and off unless a submission asks for it.** If you change
nothing in the dispatcher, every job you submit designs with the natural twenty
and nothing else — exactly as a v1.4.0 node did.

### Why that is true, in the code

`pipeline/run_iteMAN.py:760-787` reads the job's own `design_spec.json` and
takes `palette.noncanonical`. The design pass runs **only if that list is
non-empty**:

```python
if not ncaa_codes:
    print('\n*** No non-canonical residues selected; '
          'skipping the design pass. ***')
elif ncaa_off:          # PREDPEP_NCAA_PASS=0
    ...
```

`app/predPEP.py:222` (`_sanitise_design_spec`) returns `(None, None)` when no
`design_spec` is posted, so no spec file is written and the list cannot be
non-empty. **Posting no `design_spec` is therefore the canonical-only path.**

Verified on this node rather than inferred — job `SPPAUP_dd66980b`, submitted
with no `design_spec`, in its own `*_manager_stdout.log`:

```
*** No non-canonical residues selected; skipping the design pass. ***
```

It completed 6/6 iterations, `partial: false`, `failed_stage: null`.

### The three levers, strongest last

1. **Per job (recommended).** Omit `design_spec` entirely, or post one whose
   `palette.noncanonical` is absent or `[]`. Default `palette.mode` is
   `l-canonical` (`app/predPEP.py:341`).
2. **Node-wide kill switch.** `PREDPEP_NCAA_PASS=0` (accepts `0|no|false|off`,
   `pipeline/run_iteMAN.py:775`). This **overrides a client that did ask** for
   NCAAs: the pass is skipped, stderr says so loudly, and
   `disabled by PREDPEP_NCAA_PASS` is recorded in the job's `ncaa_errors`. Use
   this if the fleet must be canonical-only regardless of what any client sends.
3. **Catalogue level.** `PREDPEP_D_RESIDUES=0` removes the whole D half from the
   catalogue the node will even offer (`app/residue_catalogue.py:249-256`), so a
   D-residue request is rejected at submission instead of being honoured.

Belt and braces for a strictly canonical fleet: submit without a palette **and**
set `PREDPEP_NCAA_PASS=0`.

### One hard constraint, worth knowing before you design a UI

**The canonical twenty cannot be narrowed.** `palette.canonical` must be `"all"`
or the submission is **rejected with 400** (`app/predPEP.py:338-340`). Rosetta's
`CustomBaseTypePackerPalette` always calls `set_up_default_base_types()` before
adding, pepspec pushes no subtractive task operation and ignores resfiles — so a
restricted canonical set is impossible to honour. Tested against this node:

```
POST /upload  design_spec={"palette":{"canonical":["ALA","GLY"]}}
  400 palette.canonical must be "all": the packer palette is additive
      and pepspec cannot exclude canonical residues.

POST /upload  design_spec={"palette":{"mode":"martian"}}
  400 unsupported palette mode 'martian'; this node can build
      l-canonical, l-mixed, d-canonical, d-mixed, all-mixed.
```

Neither created a job directory — a rejected spec fails the submission outright
rather than running with different settings than were asked for. The node says so rather than
accepting the job and quietly ignoring the request. Do not build a "pick your
amino acids" control expecting subtraction to work.

### Related NCAA settings (only matter once NCAAs are in use)

| Variable | Default | Effect |
|---|---|---|
| `PREDPEP_NCAA_PASS` | `1` | the kill switch above |
| `PREDPEP_NCAA_TOP` | `10` (`Dockerfile:228`) | how many top models get the NCAA pass |
| `PREDPEP_NCAA_CONTROL` | `1` (`Dockerfile:230`) | include the canonical control arm |
| `PREDPEP_NCAA_WEIGHTS` | `talaris2013_split_unfolded.wts` (`Dockerfile:231`) | **changes results**; set deliberately |
| `PREDPEP_NCAA_TIMEOUT` | `3600` s (`pipeline/run_iteMAN.py:49`) | wall-clock cap on the pass |

---

## 2. The wire contract

21 routes. Auth scopes are `read`, `submit`, `admin`; `/health` is unauthenticated
by design, for version checks during a staged rollout.

```
GET    /health                                    auth=none
GET    /state                                     auth=read
GET    /jobs                                      auth=read
GET    /status/<job_id>                           auth=read
POST   /upload                                    auth=submit
POST   /jobs/<job_id>/stop                        auth=admin
POST   /jobs/<job_id>/pause                       auth=admin
POST   /jobs/<job_id>/resume                      auth=admin
DELETE /jobs/<job_id>                             auth=admin
GET    /jobs/<job_id>/metrics|spec|log|analytics  auth=read
GET    /results_data/<job_id>                     auth=read
GET    /get_tmap_tree/<job_id>                    auth=read
GET    /stream_final_pdb/<job_id>/<path>          auth=read
GET    /api/residues                              auth=read
POST   /api/residues/reload                       auth=admin
GET    /api/audit, /api/tokens; POST/DELETE tokens
```

Auth is enforced **only once a token exists** — a node with no tokens serves
`read` endpoints openly. Create the first token before exposing a node.

### Submitting: `POST /upload`

The file part is named **`file1`**. Not `pdb_file`, not `file` — a different name
returns `400 {"error":"No file part"}`. Form fields read at
`app/predPEP.py:844-930`:

| field | required | notes |
|---|---|---|
| `file1` | **yes** | the receptor PDB, multipart file part |
| `protein_symbol` | yes | run label; **no longer 4 characters** — up to 64, free text |
| `user_name` | yes | |
| `cpus` | no | clamped to the node's budget; the response reports what you got |
| `user_email` | no | |
| `design_spec` | no | JSON; **omit for canonical-only** (§1) |

`/upload` **queues rather than refuses**: a node with two free cores accepts a
sixteen-core job and parks it. Read `status` in the response (`running` vs
`queued`) or you will build an invisible backlog on one machine while another
idles. Nothing reserves capacity between reading `/state` and posting `/upload`,
so two concurrent dispatches can still pick the same node.

### Reading state

`/state` keys: `accepting`, `auth`, `available_cores`, `core_budget`, `disk`,
`queued`, `reserved_cores`, `running`, `version`.

**`accepting` is hardcoded `true`** and never flips under load. Dispatch on
`available_cores`.

`/jobs` returns `{success, max_iterations, jobs[]}`. Per job: `job_id`, `status`,
`paused`, `partial`, `failed_stage`, `message`, `iteration`, `iterations_done`,
`finish_reason`, `best_score`, `download_url`, `artifacts`, `submitted_at`,
`completed_at`, `cpus`, `peptide_length`, `peptide_residues`, `protein_symbol`,
`user_name`, `user_email`, `submitted_by`, `pdb_filename`, `design`, `lineage`.

### Status vocabulary — and the rule behind it

`Queued`, `Running`, `Complete`, `Failed`, `Stopped` (`app/scheduler.py:187`).
Shaped in exactly one place, `public_status()` at `app/scheduler.py:823`.

**New states arrive as booleans, never as new status strings.** The current
client maps an unknown status to *running* and polls without a wall-clock
timeout, so a new terminal string makes it poll a finished job for ever. Hence:

- **`partial`** — a run that produced results but failed a late stage. Still
  gated off (`PREDPEP_PARTIAL_STATUS=0`, `app/scheduler.py:196`): such jobs
  report `Complete` with `partial: true` and `failed_stage` set. **Read
  `partial`, not just `status`.** Once the dispatcher treats `Partial` as a
  terminal results-available state, the gate can be turned on node-side.
- **`paused`** — see §3.

Please keep this shape when you ask for anything new, and give the poller a
wall-clock timeout regardless.

---

## 3. Pause (new in this node)

`POST /jobs/<id>/pause` and `/resume`, admin scope
(`app/predPEP.py:1095`, `1118` → `app/scheduler.py:379`, `398`). SIGSTOP/SIGCONT
on the manager's process group.

What the dispatcher needs to know:

- A paused job keeps **`status: Running`** and sets **`paused: true`**. A client
  that ignores `paused` sees a job taking longer, which is what is happening.
- **It does not return capacity.** The core reservation stands and the frozen
  Rosetta keeps its memory, so `available_cores` does not rise. Pause frees CPU
  for whoever is at the node's console; it gives the fleet nothing.
- **A pause expires.** Past `PREDPEP_MAX_PAUSE_HOURS` (default 24,
  `app/scheduler.py:147`) the node resumes the job itself, so a forgotten pause
  cannot hold a machine or make you poll for ever.
- Cancelling a paused job works normally (SIGKILL reaches a stopped group).

Measured on `SPPAUP_dd66980b`: all four workers `T` with CPU time flat across
24 s, then `R` with time advancing after resume, and the job finished 6/6 with an
intact zip.

---

## 4. Differences from a v1.4.0 node that will bite

1. **Scores are not comparable across versions.** v1.6.0 scores with FoldX Pro,
   measured here ~0.23 kcal/mol below the FoldX 5 that v1.4.0 bundles — enough to
   reorder a ranking (on three identical structures the order reversed
   completely). During a staged rollout, refuse to mix results from two
   versions: `/health` gives you `version` unauthenticated, so gate on it.
2. **`file1`**, not `pdb_file` (§2).
3. **`protein_symbol` accepts up to 64 characters.** If your client truncates to
   4, long run names submitted elsewhere will look wrong in your UI.
4. **`partial` exists and is gated**; `paused` exists and is not.
5. **Retention.** Jobs are evicted by age then size: `PREDPEP_RETENTION_DAYS`
   (default 180) and `PREDPEP_RETENTION_BYTES` (default 50 GB), whichever bites
   first, and only ever **terminal** jobs. Archived and verified before removal.
   Do not assume a job's files are there for ever.
6. **A node restart kills running jobs.** Orphans reconcile to `failed` on boot
   and are never resumed. Before asking an operator to upgrade or reboot a node,
   check `GET /jobs` has nothing `Running` — `scripts/deploy.sh` enforces the
   same guard locally.

---

## 5. Suggested order of work

1. Give the job poller a wall-clock timeout. Everything else is downstream of
   this.
2. Read `partial` and `paused` as booleans; keep treating the five status strings
   as the whole vocabulary.
3. Dispatch on `available_cores`, and read `status` from the `/upload` response
   so a queued job is not mistaken for a running one.
4. Gate dispatch on `/health`'s `version` while the fleet is mixed.
5. Decide the NCAA policy: leave submissions palette-free for v1.4.0-identical
   behaviour, and set `PREDPEP_NCAA_PASS=0` on the nodes if it must be enforced
   regardless of client.
6. Only then, if wanted: surface pause/resume, and treat `Partial` as terminal so
   the node-side gate can be opened.

## 6. Questions this file cannot answer

`docs/DDN-CLIENT-CHANGES.md` is the older, blunter list of client-side breakage
and overlaps with §2 and §4. `docs/UPGRADE-v1.6.0-node.md` covers deploying the
node itself. `docs/upstream-v1.6.0/` holds the original developer's notes for
v1.6.0, unedited.
