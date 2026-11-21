# Incident: a 14-hour job produced no final scores and reported success
> **Note (Nov 2026):** the score columns named below were renamed after this
> incident — `Rosetta_Total_Score` → `Total_Energy`, `FoldX_Score` → `ddG_Score`,
> `rosetta_available` → `energy_available`. Readers accept both; this record is
> left in the names it was written with.

**Job** `SPILRH_c70d2d4c` · protein `ILRH_113` · 15 CPUs · peptide length 30
**Node** internal compute node · **2026-08-03 11:43 → 2026-08-04 01:32 UTC (~13.8 h)**

All 6 iterations completed and **4,795 designs were scored**, but `tab2_final_scores.csv` was never
written. The job was zipped and reported **Complete**, so the DDN platform saw a successful job with
no scores table and errored. No data was lost — the results were recovered from the 523 MB zip — but
a successful computation looked like a failed one, and any future failure would have been hidden the
same way.

## The chain

```
one corrupted .spec file (16 invalid UTF-8 bytes in a 61-byte burst, 1 of 480)
  └─> run_catSPEC.py aborts with UnicodeDecodeError at run_catSPEC.py:44
        └─> non_mc_pdbs.csv never created
              └─> run_mergeScores.py skips the merge, writes nothing
                    └─> tab2_final_scores.csv never created
                          └─> job zipped, manager exits 0, status = Complete
```

The trigger looked like transient storage/memory corruption on the node (clean ASCII either side of
the burst; the other 479 files intact) — **worth checking that node's ECC / `dmesg` / SMART.** But the
corruption only broke final aggregation; the design work itself was fine, so lenient decoding alone
would have saved the run.

## What was fixed

| # | Defect | Fix |
|---|---|---|
| 1 | A fatal stage error reported as a successful job | The manager now derives a verdict from what it **produced**, not from exit codes, and records it in `final_status.json` + `job.json`; the scheduler no longer treats "a zip exists" as proof of success |
| 2 | One bad file aborted all 480 | `run_catSPEC.py` reads every file with `errors='replace'` **and** isolates each file in its own `try/except`; damaged records are skipped and counted, not silently scored |
| 3 | `job.json` still said `"status": "running"` | The manager stamps the terminal state itself; `scheduler.public_status()` also self-heals a job left "running" whose manager is gone |
| 4 | No download for stopped/partial jobs | The download URL is offered for **every** terminal state that produced a zip |
| + | `run_mergeScores.py` wrote nothing when Rosetta scores were missing | Emits a FoldX-only table with `Rosetta_Total_Score` **empty** (never `0`) and `rosetta_available=False` |

Two further silent-success paths were found and closed while fixing #1: `run_catSPEC.py` also exits
0 when no `.spec` files are found or `total_score` is absent, and `run_mergeScores.py` exited 0
after writing nothing. Both now exit non-zero, and the manager's verdict does not depend on exit
codes anyway.

### New status: `partial`

> **partial** — the computation produced usable per-design results, but the final aggregated score
> table (`tab2_final_scores.csv`) is missing or incomplete.

A run is `partial` when the score table is absent, when a final stage failed, or when corrupted
input was skipped (the table is then demonstrably incomplete). It is `failed` only when nothing
usable came out, and `complete` only when every stage succeeded with no data dropped.

### ⚠️ Wire-compatibility gate — read before enabling

The DDN client **maps unrecognised `/status` values to *running*, and its poll loop has no
wall-clock timeout.** Emitting `Partial` before that client supports it would make it poll a
finished job forever, holding its fleet core lease.

So the node does **not** emit `Partial` by default:

| `PREDPEP_PARTIAL_STATUS` | `/status` reports | Always present |
|---|---|---|
| unset / `0` (default) | `"Complete"` | `partial: true`, `failed_stage`, `message`, `artifacts` |
| `1` | `"Partial"` | same |

**Rollout order:** DDN ships `partial` handling (terminal + results-available) → confirm → set
`PREDPEP_PARTIAL_STATUS=1` on the node. No code change is needed to flip it, just an env var.

**Status as of 2026-08-04 (confirmed by the DDN team):**

| | |
|---|---|
| DDN `partial` support | written, tested, **merged in v1.26.18** |
| DDN **production** | still on **v1.25.86** — does *not* read the flags yet |
| Ship predPEP v1.4.0 with the gate **off** | ✅ approved — their prod treats it as a normal `Complete` and downloads the degraded CSV; only the user-facing "partial run" warning is missing until they deploy |
| Set `PREDPEP_PARTIAL_STATUS=1` | ❌ **blocked** until DDN prod is on **v1.26.18+**; they will ping us |

Until DDN prod is on v1.26.18+, flipping the gate makes their poller — which maps unknown statuses
to *running* and has no wall-clock timeout — poll a finished job forever, holding its fleet core
lease.

### Knock-on effect of the degraded CSV

Because v1.4.0 always writes `tab2_final_scores.csv`, the old **"scores file is missing"** detection
no longer fires. Any consumer that relied on it now sees a degraded run as a clean one; the
`partial` / `failed_stage` flags (or the CSV's `rosetta_available` column) are the only remaining
signal. This is why the reporting change mattered more than the crash fix, and it should be
communicated to **every** consumer, not just DDN.

The node's own UI is unaffected by the gate: it renders an amber **Partial** from the `partial`
flag, never a green Complete.

## Machine-readable outputs

| File | Written by | Contents |
|---|---|---|
| `final_status.json` | `run_iteMAN.py`, before zipping (so it travels inside the zip) | `status`, `failed_stage`, `message`, `recorded_at` |
| `aggregation_report.json` | `run_catSPEC.py` | files found/unreadable, corrupted lines skipped, rows processed, `degraded` |

A consumer can now detect the failure from `/status` alone, without downloading and parsing a
523 MB zip for a traceback in `manager_stderr.log`.

## Not done (separate ticket)

**Re-runnable aggregation** (`POST /jobs/<id>/reaggregate`). `_post_zip_cleanup` deletes the raw
`.spec` files once a job is terminal, so this only works pre-cleanup or by re-extracting the zip —
that drags artifact retention and lifecycle design into an incident fix. Its value also drops
sharply now: with tolerant decoding the aggregation usually won't fail, and when it does the
degraded table still gives a usable result. If built later, it should operate on the **zip**, which
is retained, so it works for any historical job.

## Regression tests

`tests/` (pytest; needs only `pandas` + `pytest`):

| File | Covers |
|---|---|
| `test_catspec_corruption.py` | the exact `UnicodeDecodeError`, per-file isolation, corrupted records not scored |
| `test_merge_degraded.py` | FoldX-only table, empty (never `0`) Rosetta cells, sequence recovery |
| `test_manager_outcome.py` | the verdict: partial vs failed vs complete, `job.json` stamping |
| `test_job_status.py` | terminal state, the wire gate, downloads, self-healing a stale "running" |
| `test_incident_end_to_end.py` | replays the whole chain with the real scripts as subprocesses |

The corrupted byte sequence from `SPILRH1Q672F1.spec` is embedded in `tests/conftest.py`, so the
suite reproduces the incident without needing the 1.7 MB file.
