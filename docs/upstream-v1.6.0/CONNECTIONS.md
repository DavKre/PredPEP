# predPEP — how the parts connect

Nov 16, 2026 · @hakg

## Two layers, and the file between them

predPEP is two bodies of code on two paths, with different lifecycles and no import between them.

**The app** lives in `/opt/spacepep-predPEP` — a Flask application served by gunicorn. It owns the web interface, the residue catalogue, job submission, the job queue and the results views. It never invokes Rosetta or FoldX itself.

**The pipeline** lives in `/usr/local/pepspec_pipe`, with its `.py` and `.sh` files symlinked into `/usr/local/bin`. Every script there is standalone and runnable by hand. It owns everything that calls Rosetta or FoldX, and it knows nothing about Flask.

The boundary between them is **a directory and a JSON file, not a function call.** The app creates a job directory, writes `design_spec.json` into it, and starts `run_iteMAN.py` with that directory as an argument. The manager writes its outputs back into the same directory. The app then reads those files to render the results. Neither side imports the other, and neither can call into the other.

That shape is the design's main virtue and its main hazard.

The virtue: any pipeline stage can be run by hand against a job directory. That is how the FoldX stage was verified on 16 November — three models copied into a `work/SMOKE.pdbs` layout and `run_foldX2.sh SMOKE SMOKE` run directly, in under a minute, with no submission and no Flask.

The hazard: nothing enforces agreement between the two sides. The app can accept a palette mode the pipeline refuses, or validate residues against a catalogue the pipeline resolves differently. Both happened in November, and neither produced an error — one produced a job that silently designed nothing non-canonical, the other a container that offered residues the host withheld.

## A job, end to end

&#91;embedded content: the job chain · 12 stages, 5 of them repeated per iteration\]

Two things in that chain are easy to get wrong.

**The FoldX stage is two scripts deep.** `run_pepSpecPipe.sh` calls `run_foldX.sh`, which is a driver that calls `run_foldX2.sh` once per pepspec run. `run_foldX2.sh` is where the binary is resolved, where the awk parser lives, and where both November fixes landed — so a change to it is in the live path, but a test that invokes it directly skips two levels of the chain.

**The non-canonical pass runs after the loop, not inside it.** pepspec cannot place a non-canonical residue: its design loop hardcodes `chemical::AA(1..20)`, the packer-palette flag crashes it, and it ignores resfiles. So the selected residues can only take effect afterwards, on finished models, through `rosetta_scripts`. And it must be after the *last* iteration rather than each one, because `run_iteMAN.py` feeds each round's best models back into pepspec — a model carrying an NCAA would hit that crash on the next round.

The verdict is computed after the pass, so `final_status.json` is written when the job is genuinely finished. But a failure in the pass cannot change that verdict: its errors go to their own list and never reach `failed_stage`. The canonical designs and their scores are already complete and valid by then, and a 14-hour job must not be retroactively marked failed by a pass that is not what the user waited for.

## How resources are found

Almost nothing is hardcoded. Every external path is an environment variable with a default, and the default is what hakg actually has — so an unconfigured install works, and a container can be pointed elsewhere without a code change.

| Resource | Variable | Default | If absent |
| --- | --- | --- | --- |
| Rosetta root | `PREDPEP_ROSETTA` | `…/rosetta.binary.ubuntu.release-408/main` | preflight fails |
| rosetta\_scripts | `PREDPEP_RS_BIN` | `rosetta_scripts.static.linuxgccrelease` | the NCAA pass cannot run |
| FoldX binary | `PREDPEP_FOLDX` | `/usr/local/foldxpro_20261231/foldxpro20261231` | `run_foldX2.sh` exits 3 |
| `rotabase.txt` | — (candidate list) | `foldxpro_20261231/`, then `foldxLinux64_0/` | `foldx_scorable` empty; every NCAA treated as unscorable |
| residue catalogue | `PREDPEP_RESIDUE_CSV` | `all_aas3/all-residues-list-complete.csv` | the catalogue is empty |
| params | `PREDPEP_PARAMS_DIR` | `all_aas3/generated_params_rotlib` | codes report no params |
| rotamer libraries | `PREDPEP_ROTLIB_DIR` | `all_aas3/generated_rotlibs` | `without_rotlib` rises |
| retired list | `PREDPEP_RETIRED_CSV` | `all_aas3/retired_residues.csv` | **nothing is withheld** |
| D het-code map | `PREDPEP_D_NAMES_CSV` | `all_aas3/residue_names.csv` | D codes lose their `pdb_code` |
| uploads | `PREDPEP_UPLOAD_DIR` | `/tmp/pepspec/uploads` | lost at next boot |
| results | `PREDPEP_RESULT_DIR` | `/tmp/pepspec/results` | lost at next boot |
| the manager | `PREDPEP_MANAGER` | `/usr/local/bin/run_iteMAN.py` | no job can start |
| the NCAA script | — (search order) | `/usr/local/bin`, then `/usr/local/pepspec_pipe` | pass skipped, recorded in `ncaa_status.json` |

Behaviour, rather than paths, is set the same way: `PREDPEP_CORE_BUDGET`, `PREDPEP_MAX_ITERATIONS`, `PREDPEP_CONVERGENCE_MIN`, `PREDPEP_PLATEAU_ROUNDS`, `PREDPEP_RETENTION_DAYS`, `PREDPEP_RETENTION_BYTES`, `PREDPEP_NCAA_PASS`, `PREDPEP_NCAA_TOP`, `PREDPEP_NCAA_TIMEOUT`, `PREDPEP_D_RESIDUES`, `PREDPEP_ALLOW_RETIRED`, `PREDPEP_ALLOW_ROSETTA_NCAA`, `PREDPEP_SKIP_PREFLIGHT`.

### Two patterns worth knowing

**A missing resource should fail loudly, and mostly now does.** `run_foldX2.sh` exits 3 when its binary is not executable, rather than running nothing and producing an empty score column — which is what happened on 9 November, when a job completed successfully with every FoldX score absent and its rankings therefore meaningless. The two residue CSVs are the remaining exception: absent, they change the answer silently.

**Two of the pipeline scripts are found by search, not by path.** The stages are symlinked into `/usr/local/bin`, but `run_ncaaDesign.py` is not, so `run_iteMAN.py` tries `/usr/local/bin` first and falls back to `/usr/local/pepspec_pipe`. A single hardcoded path there would mean the pass silently never runs — and since a failure in that pass deliberately cannot change the job's verdict, the job would still report `complete`.

## What a job writes, and who reads it

Every file below lives in the job directory. The app and the pipeline communicate only through these — there is no shared memory, no database, no socket.

| File | Written by | Read by |
| --- | --- | --- |
| `design_spec.json` | the app, at submission | `run_iteMAN.py` (palette), `run_ncaaDesign.py`, kept as provenance |
| `flags_manifest.json` | `run_createflags.py`, per iteration | the metrics endpoint |
| `runs_status.json` | the manager, per iteration | the metrics endpoint |
| `foldx_status.json` | the FoldX stage, per iteration | the metrics endpoint |
| `iteration_metrics.json` | the manager | live progress in the UI |
| `<TAG>.all.txt` | `run_foldX2.sh` | the manager; one row per model |
| `<TAG>.min.txt` | `run_foldX2.sh` | the manager, to seed the next iteration |
| `all_model_scores.csv` | aggregation | `job_analytics.py` — correlation, PCA, logo |
| `tab2_final_scores.csv` | aggregation | the Results table and most analytics |
| `tab2_filtered_scores.csv` | the post-run filter | the Results table |
| `filter_report.json` | the post-run filter | what was filtered and why |
| `non_mc_pdbs.csv` | aggregation | fallback score source |
| `tab3_ncaa_designs.csv` | `run_ncaaDesign.py` | the Results table |
| `ncaa_design_report.json` | `run_ncaaDesign.py` | palette, interface, failures |
| `ncaa_status.json` | the manager, **only on a problem** | a consumer checking the pass took effect |
| `aggregation_report.json` | `run_catSPEC.py` | damage report |
| `final_status.json` | the manager | the job's verdict |
| `lineage.json` | the app | provenance chain |
| `<job>.zip` | the scheduler | the complete archive |
| `top10/` | aggregation | structures streamed to the 3D viewer |

Two of those deserve a note.

`ncaa_status.json` **exists only when something went wrong.** Its absence is the success case. That is deliberate — a failure in the non-canonical pass must not change a job's verdict, since the canonical designs and their scores are already complete and valid by the time it runs.

`<TAG>.min.txt` is how one iteration feeds the next. It holds the best model of that run, selected from `<TAG>.all.txt`. This is why the FoldX double-row bug mattered less than it might have: `.min.txt` always picked the interaction row, so selection was unaffected even while the tables carried two rows per model.

### What the post-job sweep keeps

Once the zip exists, `_post_zip_cleanup` in `scheduler.py` deletes everything not on a whitelist — the `.pdbs` model directories, the `.spec` files, the per-run logs. On a `save_all_pdbs` job that reclaims tens of GB and keeps a few MB. `_strip_iteration_dir` empties each iteration directory down to four small records but leaves the directory, so the metrics endpoint can still find them.

**The zip is the complete archive.** `SPNECT_024e788c_s1.zip` holds 4,064 entries including 96 `.min.txt` files and 2,090 model PDBs — so a swept job has lost nothing, and auditing its search path costs an `unzip`, not a re-run.

## The analytics layer

Everything the Analytics view draws is computed server-side in `job_analytics.py`, from files the pipeline already writes. The browser recomputes nothing.

| Panel | Computed from | Method |
| --- | --- | --- |
| evolution | `iteration_metrics.json` while running, else `tab2_final_scores.csv` | the running minimum over per-iteration minima **is** the convergence curve |
| agreement | `tab2_final_scores.csv` | FoldX against Rosetta total, capped at 1,500 points |
| correlation | `all_model_scores.csv` | Pearson over ten terms; constant columns dropped first |
| pca | `all_model_scores.csv` | plain numpy SVD, three components, not scikit-learn |
| seqmap | `tab2_final_scores.csv` | classical MDS on MHFP fingerprints — sequence space, not score space |
| logo | `tab2_final_scores.csv` | residue frequency per position over the best 100 |
| descriptors | `pepSeq` via RDKit | memoised per sequence, not per design |
| sampling | `runs_status.json`, per iteration directory | pepspec runs lost to crashes |

No new pipeline output was needed for any of it — the per-iteration curve is derived from `pdb_relative_path`, which embeds the iteration folder.

### `pepResidues` is load-bearing

`_residues()` prefers the three-letter `pepResidues` column and falls back to the one-letter `pepSeq`. In `pepSeq`, **every non-canonical is written `X`**, because their params carry `IO_STRING '<CODE> X'`.

So while `pepResidues` is empty — which it is for 99.5% of rows — the sequence logo cannot tell one non-canonical from another, and the flags that mark NCAA-carrying designs in the PCA and agreement plots go blank. That is the concrete cost of the open `pepResidues` item: not a missing column, but three panels quietly losing the distinction the tool exists to make.

### `sampling()` surfaces a bias nothing else reports

pepspec crashes intermittently, and **a crashed run yields fewer models rather than failing the job.** The anchors that run sampled are then ranked against anchors that received the full sampling budget, so a silent loss biases the ranking toward whatever survived.

`runs_status.json` records this per iteration, and `_post_zip_cleanup` keeps it, so the readout is identical for a running job and a finished one. A job from before the pipeline recorded it returns `None` rather than zero — unknown losses and no losses are different claims, and the readout renders them differently.

### Two notes from the code worth keeping

`seqmap` uses SciPy's subset eigensolver rather than `np.linalg.eigh`. Under gunicorn's `preload_app`, the full decomposition took about 80 seconds inside a forked worker against 2.6 seconds standalone, because OpenBLAS's thread pool does not survive the fork.

`descriptors()` gives **no entry** for a design containing a non-canonical, because RDKit cannot build a molecule from `X`. An absent entry colours grey, which is the honest outcome; the alternative would be a descriptor computed over a molecule missing a side chain.

## The residue library

Everything the picker offers comes from one directory, `/opt/spacepep-createFF/all_aas3`, through five inputs that `residue_catalogue.py` reads:

| Input | Holds |
| --- | --- |
| `all-residues-list-complete.csv` | the catalogue: `CATEGORY`, `L_FOLDER`, `RESIDUE_DESCRIPTIVE_NAME` |
| `generated_params_rotlib/` | `<CODE>.params`, one per L residue |
| `generated_rotlibs/` | `<CODE>.rotlib`, the rotamer libraries |
| `retired_residues.csv` | codes withheld from the picker |
| `residue_names.csv` | the D het-code map (`was` → `code`) |

The last two were the ones missing from `stage.sh` until 16 November, which is why they are worth naming: without them the catalogue still loads, reports a *higher* offerable count, and withholds nothing. Nothing errors.

As of 16 November the catalogue reports **397 offerable** — 189 L and 208 D, where the 208 is 19 D-canonicals plus 189 mirrors of the L set. Withheld: 5 name collisions, 6 retired, 8 unavailable, 0 pending. 359 have a rotamer library, 38 do not. 2 are scorable by FoldX.

### Two naming conventions, deliberately

Every D residue carries two names, and they are not interchangeable:

- **the Rosetta patch name** is the palette code — `DALA`, `D0A1`, `DBiphenylAlanine`. This is what reaches the design spec and the XML.
- **the PDB het code** is what lands in an ATOM record — `DAL`, `04D`, `A1D`. The catalogue keeps it separately as `pdb_code`.

The reason is structural, not stylistic: `name3()` is three characters by construction, because it occupies PDB columns 18–20, while `NAME` can be any length. So a four-letter palette name cannot be written into a PDB, and a three-letter het code cannot address a patched type in Rosetta. Both are needed, and conflating them is what blocked the D modes until 10 November.

One caveat, found on 16 November: **the catalogue's `pdb_code` is not always what Rosetta writes.** It stores the official PDB chemical component id — D-serine is `DSN` — while Rosetta writes its own `name3` derived from the `D_AA` patch, and for serine that is `DSE`. `DSE` is what appears in the 11 November structures and in `tab3_ncaa_designs.csv`; `DHI` for D-histidine does match the catalogue.

Nobody has measured `name3` for the 208 D residues, so the picker's tooltip — *"Written into the output PDB as …"* — is wrong for D-serine and unverified for the rest. A `PeptideStubMover` run over the set would settle it, after which the catalogue could carry the real codes and the classifiers could stop guessing.

That guessing is the shape of a bug fixed on 16 November. The Results table and `job_analytics.py` both re-derived handedness from the code string, because the score CSV carries codes but not `hand`. Testing the first character matched 30 of 216 — the D-canonicals, whose het codes begin with D — and silently classified the other 186 as ordinary non-canonicals. `residues.js` never had the bug: it is served the catalogue and reads `entry.hand`. The fix takes the set from the catalogue, unions it with a prefix test to cover Rosetta's own `name3`, and subtracts every known L het code.

### Why D residues need no params of their own

There is no `d-caa` or `d-ncaa` directory in Rosetta, and none is needed. Rosetta's `D_AA` patch selects on `PROPERTY L_AA` and applies `CHIRAL_FLIP_NAMING` and `CHIRAL_FLIP_ATOMS`, mirroring **any L type already loaded** into a D base type, in memory. So the params that must be present are the L parent's:

| Code | What it needs |
| --- | --- |
| `DALA` | nothing — Rosetta mirrors its own `ALA` |
| `D0A1` | `0A1.params`, the L parent's |
| `DBiphenylAlanine` | nothing — Rosetta supplies `BiphenylAlanine` |

This is why `_params_ok()` in `run_ncaaDesign.py` is handedness-aware rather than requiring `<code>.params` for every code. Only one case is an error: a code whose params exist in the library but were never loaded. A code with no params anywhere must be one Rosetta supplies, since the app validated it against the installed catalogue before it reached the spec.

### Switches

`PREDPEP_D_RESIDUES=0` removes the D half of the catalogue entirely. `PREDPEP_ALLOW_RETIRED` and `PREDPEP_ALLOW_ROSETTA_NCAA` each re-admit one withheld set. All three default to the safe setting, and each reports its reason rather than silently omitting a code.

## Where the same thing exists twice

Every defect that cost real time in November was a copy that looked authoritative. None of them errored.

| The thing | Copies | Status |
| --- | --- | --- |
| the FoldX binary | `foldx_20270131` was a real FoldX 5 on hakg and a shim to Pro in the container | fixed 12 Nov — Pro named by absolute path and logged |
| the app code | `/opt/spacepep-predPEP` vs `/opt/predpep-docker/app` | `stage.sh`; 8 of 10 files differed on 16 Nov |
| the pipeline scripts | `/usr/local/pepspec_pipe` vs `/opt/predpep-docker/pipeline` | `stage.sh`; all 3 differed on 16 Nov |
| the residue inputs | 5 on hakg, 3 staged into the image | fixed 16 Nov |
| the palette gate | live in `predPEP.py`, and an older copy inside `apply_patches.py` | defused 16 Nov |
| superseded code | `.bak-*` copies beside every live file | excluded from staging 16 Nov |
| the residue directory | `/opt/spacepep-predPEP/residues/` holds `all-residues-list.csv` and is **not** what the catalogue reads | unused; worth removing or labelling |
| the deployment | 6364 on v1.6.0, 6363 on v1.4.0 | deliberate; 6363 left alone |
| the bare FoldX name | `entrypoint.sh` still makes the shim | vestigial; to remove |

### The two that actually bit

**The FoldX binary.** On hakg `/usr/local/bin/foldx_20270131` pointed at `/usr/local/foldx26Linux64_0/` — a genuine FoldX 5. Inside the container image, the same name was a symlink to FoldX Pro. So the same script, same version number, scored with two different force fields depending on where it ran, and nothing in any output recorded which. Measured across 15 designs, Pro reads 0.46 kcal/mol lower than FoldX 5 with a standard deviation of 0.42 — enough to reorder the leaders. Any ddG compared across the two deployments was comparing force fields, not designs.

**The residue CSVs.** `residue_catalogue.py` reads five inputs; `stage.sh` copied three. The image therefore loaded, reported a residue picker that looked right, and withheld nothing — offering the five name collisions and six retired codes that hakg hides, and losing the D het codes. The only visible symptom would have been a job failing much later on a code whose params do not exist.

### What catches this class of defect

Not reasoning about which copy is current. Comparing them:

- **md5 the live tree against the build tree** before any build. `build.sh` checks that `app/predPEP.py` *exists*, not that it is current, so a stale tree builds successfully.
- **run the catalogue inside the image and diff the counts against the host.** All thirteen fields must match, except `foldx_scorable`, which reads 0 without a FoldX mount and 2 with one. `retired: 6` is the field that proves the withheld sets loaded.
- **log which external binary ran.** `run_foldX2.sh` now echoes `FoldX binary: <path>` to stderr on every invocation, so every future job's log says what scored it.

## Where it runs

Three places, one of them authoritative.

### hakg — the install everything else is copied from

`spacepeppredpep.service` runs gunicorn on `0.0.0.0:8383`. It runs **as root**, and the unit says why: the conda environment lives under `/root` at mode 700, which an unprivileged user cannot traverse, and every pipeline input is already root-owned. The unit also states the cost — this process binds a port, so a bug in a route is a root compromise rather than a contained one — and records the steps to drop privileges later.

`StartLimitIntervalSec=300` and `StartLimitBurst=5` sit in `[Unit]`, where they work; in `[Service]` they are ignored. They exist because a wrong path fails in milliseconds and would otherwise restart every 5 seconds forever.

Two drop-ins under `spacepeppredpep.service.d/`:

- `killmode.conf` — `KillMode=process`, so a service restart does not kill running pipeline children
- `paths.conf` — `PREDPEP_UPLOAD_DIR` and `PREDPEP_RESULT_DIR` pointing at `/var/lib/predpep/`, added 12 November

### The build tree — `/opt/predpep-docker`

`stage.sh` populates three directories from three sources, and refuses to stage into its own source tree:

| Staged into | From |
| --- | --- |
| `app/` | `/opt/spacepep-predPEP` |
| `pipeline/` | `/usr/local/pepspec_pipe` |
| `residues/` | `/opt/spacepep-createFF/all_aas3` |

The `Dockerfile` then copies each back to the path it came from, so a container's filesystem mirrors hakg's layout. `build.sh` takes `{slim|bundled|both}`: **slim** produces `:vX` and `:vX-slim` from one image (1.54 GB, tools mounted at runtime), **bundled** produces `:vX-bundled` with Rosetta (60.7 GB) and FoldX (13.4 GB) copied in.

The bundled image holds licensed binaries. It moves by `docker save` and rsync, never to a registry outside the licence.

### The group server — 10.100.10.10

|  | port | image | data |
| --- | --- | --- | --- |
| `predpep-v160` | 6364 | `predpep:v1.6.0` (slim) | volume `predpep-v150-data` → `/data`, Rosetta + FoldX mounted read-only |
| `predpep_app` | 6363 | `predpep:v1.4.0` | bind `/home/david/predpep_data` → `/tmp/pepspec`, no Rosetta or FoldX mount |

6364 carries nineteen `PREDPEP_*` variables, `--restart unless-stopped` and 8 CPUs. 6363 is a separate older service and is deliberately left on v1.4.0 — which predates the FoldX interaction fix, so FoldX scores it produced carry the double-row defect.

When redeploying 6364, copy the env out of the running container rather than retyping it. A hand-written run line carrying only `PREDPEP_CORE_BUDGET` lets `PREDPEP_RESULT_DIR` fall back to its default.

### That default, and why it matters

Unset, `PREDPEP_UPLOAD_DIR` and `PREDPEP_RESULT_DIR` fall back to `/tmp/pepspec/uploads` and `/tmp/pepspec/results` (`predPEP.py:129-130`). `/tmp` is emptied at boot by `systemd-tmpfiles`' `D /tmp` line, and an `x` exemption excludes a path from **age-based** cleaning but not from the boot-time empty. A job's results therefore lived only until the next reboot. That is how the 10 November `/tmp/ncaatest` proof of D-residue design was lost, and why both deployments now set those variables explicitly.

### The distributable package

`/opt/predpep-docker/dist/predpep-tool-v1.6.0.tar.gz` — 440 MB compressed. It holds the README, `docker-compose.yml`, `docker-compose.yml.v160` (pinned, so the shipped file records exactly what went out), `.env.example`, and three scripts.

| script | does |
| --- | --- |
| `load.sh` | loads both tags and confirms they arrived |
| `run.sh` | runs the container; **refuses to start** without an executable FoldX binary, rather than failing hours into a job |
| `verify.sh` | preflight, then the container's residue catalogue against the build host's counts |

**No licensed binary travels in it.** The image holds the application and the residue library; Rosetta and FoldX are mounted read-only from the host at run time. Verified on 16 November: `predpep:v1.6.0` and `predpep:v1.6.0-slim` are one image under two tags (id `4e4df5e0`), 1.5 GB unpacked, and neither `/usr/local/rosetta*` nor `/usr/local/foldx*` exists inside it. An earlier draft of this section called that "the slim pair", which was wrong — there is one image, not two. `run.sh` starts the plain tag; the compose file's `bundled` profile names the image that is deliberately never shipped.

`verify.sh` names `retired: 6` as the field that matters: it is the one that proves the withheld-residue lists reached the image. An image missing them reports `retired: 0`, offers residues the build host hides, and raises no error.

Built from the 15:25 image of 16 November — the first build was made before that afternoon's fixes, so it was rebuilt to match its own changelog.

## Which file to open

| The symptom | Open | Look for |
| --- | --- | --- |
| a residue is missing from the picker | `residue_catalogue.py` | the withheld sets and their stated reasons; then `retired_residues.csv` |
| the picker shows four sections wrongly, or a chip is unclickable | `static/js/residues.js` | `mirrorsOf()`, `syncExclusions()`, `shortLabel()` |
| a palette mode was rejected | `predPEP.py` | the gate, which reads `residue_catalogue.PALETTE_MODES` |
| no D residues in the output | `run_ncaaDesign.py` | `build_xml()` mode list, then `_params_ok()` |
| the NCAA pass did not run | `run_iteMAN.py`, then `ncaa_status.json` | the pass sits between the score merge and the verdict |
| the FoldX column is empty | the job's manager log | the `FoldX binary:` line, then exit 3 from `run_foldX2.sh` |
| FoldX numbers look wrong | `run_foldX2.sh` | the awk: one row per model, interaction `Total` only |
| a FoldX score exists for a D design | `run_ncaaDesign.py` | the `foldx_scorable()` pre-check — that score should not exist |
| a chart will not draw | browser console, then `static/js/plots.js` | `[plots] draw failed <id>`, then `draw()`'s size guard |
| a panel says it cannot be drawn | `static/js/analytics.js` | `panel()` and `placeholder()`; the note holds the exception text |
| correlation or PCA looks wrong | `job_analytics.py` | `correlation()` drops constant columns; the source is `all_model_scores.csv` |
| a finished job lost its intermediates | `scheduler.py` | `_post_zip_cleanup` and `_KEEP_JOB_FILES`; the zip has everything |
| jobs are queueing | `scheduler.py` | the core budget, `PREDPEP_CORE_BUDGET` |
| results went to the wrong place | `predPEP.py:129-130` | the two env vars, and the unit's drop-in or the container's env |
| the image behaves unlike hakg | `stage.sh` | what it stages, and what it excludes |
| what did this job actually run | `design_spec.json`, `flags_manifest.json`, `final_status.json` | the spec as submitted, the flags as built, the verdict |

### Three checks worth keeping

Each of these takes under a minute and each corresponds to a defect that really happened.

**The FoldX stage, without a submission.** Copy three models from any job archive into `work/<TAG>.pdbs/<TAG>_1.pdb` and run `run_foldX2.sh <TAG> <TAG>` from `work/`. Expect the `FoldX binary:` line, one row per model in `<TAG>.all.txt`, and the minimum in `<TAG>.min.txt`. This is the check that would have caught the double-row bug on the day it appeared rather than weeks later.

**The catalogue, inside the image against the host.** `load_catalogue()['counts']` must match on all thirteen fields, with `foldx_scorable` the only expected difference (0 without a FoldX mount, 2 with one). `retired: 6` is the field that proves the withheld sets loaded.

**The build tree against the live tree.** md5 the app and pipeline files before building. `build.sh` verifies that `app/predPEP.py` exists, not that it is current.

## Changed since 16 November

The `v1.6.0` image was rebuilt on 18 November. Everything above still holds except the details below.

**The image id is now `8e1525575f51`.** The `4e4df5e0` named earlier was the 16 November build; it is retained on both hosts as `predpep:v1.6.0-pre-20261117` for rollback. The group server's `predpep-v160` runs the new one, still on volume `predpep-v150-data`, still with 8 CPUs.

**Two environment variables join the behaviour list:**

| variable | default | what it does |
| --- | --- | --- |
| `PREDPEP_NCAA_WEIGHTS` | *(unset — script default)* | the scorefunction the NCAA design pass uses. **This one changes results.** |
| `PREDPEP_NCAA_CONTROL` | on | runs a canonical-only control on the same backbone, so the table can say what the non-canonical itself contributed |

Unset, the pass uses `pre_talaris_2013_standard.wts`, which scores non-canonicals through `ref`. `ref` is indexed by the AA enum and every non-canonical params declares `AA UNK`, so it cannot hold a per-type reference energy — it charges each non-canonical some canonical residue's value. `talaris2013_split_unfolded.wts` replaces `ref` with `unfolded` and `split_unfolded_two_body`, assembled from MM atom types. The 6364 container and the package's `run.sh` both set it; anything building its own run line must pass it.

**`extrachi_cutoff` on the design protocol is now 18, Rosetta's own default.** It was 0, giving ex1 plus ex2 extra rotamers to every position with no neighbour threshold across about 190 residue types. That was nearly all of the pass cost: 741 s per structure under pre-talaris weights and about 2700 s under split-unfolded, against 42 s and 73 s at cutoff 18. A 3600 s budget had been completing five structures and then being abandoned — silently, because `ncaa_status.json`, which records exactly that, was itself being deleted by the post-job sweep until 16 November. `--nstruct` also defaulted to 5 and produced bit-identical structures, since `nloop` 10 already converges the packer; it is now 1.

**Three defects in the file-serving and table paths**, all of which predate v1.6.0:

- `/stream_final_pdb` required the model path to begin with the job directory name. Pepspec's `<job>_iterN_xxxx/` paths satisfy that by coincidence; `ncaa_designs/` paths cannot, so **every design from the NCAA pass returned 403** and none was ever viewable. The guard now rejects `..` and absolute paths and leaves confinement to `send_from_directory`.
- The route now falls back to **reading the member out of the job zip** when the recorded path has been swept. Previously only the ten models in `top10/` survived; now all of them are reachable, at no disk cost, for as long as the zip is kept.
- The Results table's `ΔG sep` column is conditional on some row carrying a value, and the header was rendered once at page load while the table was empty — so the heading never appeared and every column after Interface sat one place left of its label. `_merge_ncaa_designs` also now carries an `iteration` onto NCAA rows, joined from `tab2.pdbId` via `tab3.parent`, since a design's own path points into `ncaa_designs/` and has no `_iterN_` for the front end to parse.

**On `name3`:** the open question above is now partly answered. The 18 November designs write `DSE` (D-serine), `DTH` (D-threonine) and `DAL` (D-alanine) — Rosetta's own `name3`, not the catalogue's `pdb_code`. The remaining 205 are still unmeasured, and a `PeptideStubMover` run over the set would settle it.

**And a caution for the group server specifically:** do not run the package's `scripts/run.sh` there. Its volume default is `predpep-data`, but that deployment uses `predpep-v150-data` — the wrong one attaches an empty volume and every past job vanishes from the dashboard. `/tmp/predpep-v160-recreate.sh` on that host carries the correct run line. A plain `docker restart` is always safe.
