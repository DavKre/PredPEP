# Residue palette picker — install

Seven files. Five are your originals with edits applied; two are new.
`run_createflags.py` is the only one outside `/opt/spacepep-predPEP`.

| file | destination | change |
|---|---|---|
| `residue_catalogue.py` | `/opt/spacepep-predPEP/` | **new** |
| `residues.js` | `/opt/spacepep-predPEP/static/js/` | **new** |
| `predPEP.py` | `/opt/spacepep-predPEP/` | +69 lines |
| `index.html` | `/opt/spacepep-predPEP/templates/` | +124 lines |
| `api.js` | `/opt/spacepep-predPEP/static/js/` | +4 lines |
| `submit.js` | `/opt/spacepep-predPEP/static/js/` | +22 lines |
| `run_createflags.py` | `/usr/local/pepspec_pipe/` | +80 lines |

`residue_routes.py`, `residue_picker.js`, `residue_picker.html` and
`residue_block.html` from earlier in this session are **superseded** — don't
install them.

Back up first; these are whole-file replacements:

```bash
cd /opt/spacepep-predPEP
tar czf ~/predPEP-backup-$(date +%F).tgz predPEP.py templates/index.html \
    static/js/api.js static/js/submit.js
cp /usr/local/pepspec_pipe/run_createflags.py ~/run_createflags.py.bak
```

Then copy the files in, and restart the service (a single gunicorn worker holds
the scheduler queue in memory, so a restart drops nothing but does requeue):

```bash
systemctl restart spacepep-predpep    # or whatever the unit is called
```

`run_createflags.py` needs no restart — it is exec'd per iteration. Keep its
mode: `chmod 755 /usr/local/pepspec_pipe/run_createflags.py`.

## Paths to check

`residue_catalogue.py` defaults to:

```python
DEFAULT_CSV     = '/opt/spacepep-createFF/all_aas3/all-residues-list-complete.csv'
DEFAULT_PARAMS  = '/opt/spacepep-createFF/all_aas3/generated_params_rotlib'
DEFAULT_ROTLIBS = '/opt/spacepep-createFF/all_aas3/generated_rotlibs'
```

The CSV path is from your own `cut -d, -f1` run, so that one is right. **The
params and rotlib directory names are my guess** — I never saw them listed.
Either correct the constants or set `PREDPEP_PARAMS_DIR` and
`PREDPEP_ROTLIB_DIR` in the systemd unit alongside `PREDPEP_RESULT_DIR`.

Check before restarting, without touching the service:

```bash
cd /opt/spacepep-predPEP
python3 residue_catalogue.py
```

Expect:

```
canonical      : 20 (read-only: True)
non-canonical  : 195 offerable, 176 with a rotamer library, 19 without
   L-noncanonical (by name)           129
   L-noncanonical (struct match)       66
```

195 and 176 are the figures from your own runs. If it prints 0 offerable, the
params directory is wrong, not the catalogue.

## What it does

A **Design residues** block in New job, between *Design parameters* and
*Disulfide handling* — it acts during the run, so it sits with the other
during-run controls rather than among the post-run filters.

**Choose residues** opens a dialog with two sections. L-non-canonical is the
195 residues with working params, each row `[✓] CODE — Descriptive name`, with
a filter box, select-all-shown/none, and a toggle to hide the 19 with no
rotamer library (marked *1 rotamer*, because the packer gets one fixed
conformation for those and will under-sample them against the canonicals they
compete with).

L-canonical shows all twenty, ticked and disabled.
`CustomBaseTypePackerPalette` always calls `set_up_default_base_types()` before
adding, pepspec applies no subtractive task operation and ignores resfiles, so
a checkbox that unticked `CYS` would look like it worked and change nothing.
The note in the section says so. It becomes interactive when the design stage
can subtract types — the same decision still open on the four pure palettes.

The selection travels as `palette` in the design spec and reaches Rosetta as
`-packing:packer_palette:extra_base_type_file`.

## Verifying end to end

**1. The endpoint.**

```bash
curl -s localhost:8000/api/residues | python3 -m json.tool | head -20
```

`counts` should read `{canonical: 20, noncanonical: 195, with_rotlib: 176,
without_rotlib: 19}`. If auth is live, add `-H "Authorization: Bearer <token>"`
— the route is `@auth.require('read')`.

**2. The form.** Open New job, expand **Design residues**, click **Choose
residues**. Tick two or three, **Apply**, then **View spec**:

```json
"palette": { "mode": "l-mixed", "canonical": "all",
             "noncanonical": ["0A1", "SEP"] }
```

**Reset** should return it to `l-canonical` with an empty list.

**3. The flag file.** Submit a short job and look in the first iteration
directory:

```bash
cd /tmp/pepspec/results/<job>/<job>_iter1_*/
tail -5 flags2_1.txt
cat flags2.palette
```

The flag file must end with the `-extra_res_fa` lines, then
`-packing:packer_palette:extra_base_type_file flags2.palette`, then `-o <tag>`.
`flags2.palette` holds bare codes, one per line, no comments.

**4. The only check that proves it works.** Steps 1–3 show the flag was
*written*. Whether the packer *placed* any is in the score table's
`pepResidues` column:

```bash
cut -d, -f3 /tmp/pepspec/results/<job>/tab2_final_scores.csv \
  | grep -c '0A1\|SEP'
```

Zero on a job that selected them means the palette is not reaching the packer.
First thing I would check: whether `extra_base_type_file` tolerates comment
lines. I verified bare names work on this build (`0A1 DALA DVAL` loads, a bogus
name fails loudly) but never tested `#`, which is why the file has none — the
same assumption produced three wrong diagnoses of the `.rotlib` format
earlier. If it turns out comments *are* fine, `write_palette_file` is the one
place to add them.

## Rolling back

Restore the backup and restart. A job already queued keeps its
`design_spec.json`; an old `run_createflags.py` ignores the `palette` key
(`spec.get('palette')` simply goes unread), so a queued job runs
canonical-only rather than failing. Jobs already finished are unaffected —
nothing here touches the aggregation or the score tables.

## Verified before shipping

Built by exact string replacement on your uploaded originals — 19 edits, each
required to match exactly once, the whole build aborting otherwise — so these
files are provably your originals plus the listed changes, with nothing
retyped.

- `predPEP.py`, `run_createflags.py`, `residue_catalogue.py` parse; `api.js`,
  `submit.js`, `residues.js` pass `node --check`; `index.html` tags balance and
  the dialog sits outside the form element.
- `run_createflags.py` run end to end on a synthetic two-chain complex: the
  palette flag is emitted in the right position, `flags2.palette` holds the
  right codes, the manifest records them.
- **A spec with no palette produces byte-identical flag files to the original
  script**, and the manifest gains only the `palette` key. An existing job
  submitted before this change behaves exactly as it did.
- The guard works: a palette code with no matching `-extra_res_fa` exits 2 and
  writes no flag files, rather than letting Rosetta fail at load with a message
  that never mentions the palette.
- `_sanitise_design_spec` tested against twelve inputs — a restricted canonical
  set, a `d-mixed` mode, a canonical in the non-canonical list, an uninstalled
  code, `SEP; rm -rf /`, `../../etc/passwd`, a non-object palette — all
  rejected with a specific reason; valid input resolves to params paths and
  de-duplicates against any the client sent.

## Not done

- **`style.css`** — the dialog's CSS is a `<style>` block at the end of
  `index.html` using your custom properties with fallbacks. Send the file and
  I'll move it in and check the class names against the real definitions
  instead of inferring them from usage.
- **The four pure palettes.** `palette.mode` is the field; `predPEP.py` rejects
  `d-canonical`, `d-non-canonical`, `d-mixed` and `all-mixed` today with the
  reason. All four need subtraction, and the route is still undecided
  (two-stage rosetta_scripts / patched pepspec / ship three). The D codes will
  also need the PDB-to-Rosetta name map (`DAL` → `DALA`).
- **A pre-existing bug this surfaced.** `job_analytics.py`, `results.js`,
  `submit.js` and `plots.js` each classify a D-residue as `len(c) == 3 and
  c.startswith('D')`. Several of the 195 L non-canonicals are three letters
  beginning with D, so once they appear in designs they will be labelled and
  filtered as D-residues in the composition chips, the *Has D-residue* filter,
  the logo and the `ncaa` colour-by field. Cosmetic, and the right fix is to
  classify from the catalogue rather than the spelling.
- **One line in `run_mergeScores.py`.** `ROSETTA_TERMS` (line 441) lists
  `dslf_fa13` and `dslf_ss_dst` but not `dslf_ss_dih` or `dslf_ca_dih`, which
  the pre-Talaris function also emits and which `plots.js` already offers as
  colour-by fields. Unrelated to the palette.
