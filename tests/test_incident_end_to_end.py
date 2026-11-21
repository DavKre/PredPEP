"""End-to-end replay of the SPILRH_c70d2d4c chain.

The incident was not one bug but a chain: corrupt .spec -> run_catSPEC.py aborts ->
non_mc_pdbs.csv missing -> merge skipped -> tab2_final_scores.csv missing -> job zipped
and reported "Complete". Unit tests pin each link; this replays the whole chain by
running the real scripts as subprocesses, the way run_iteMAN.py invokes them.
"""
import csv
import json
import os
import subprocess
import sys

import pytest

import conftest  # noqa: F401  (path setup)
from conftest import PIPELINE, CORRUPT_BURST, spec_line
import run_iteMAN
import scheduler

CAT_SPEC = os.path.join(PIPELINE, 'run_catSPEC.py')
MERGE = os.path.join(PIPELINE, 'run_mergeScores.py')

N_DESIGNS = 40          # stands in for the 480 .spec files of the real job


@pytest.fixture
def incident_job(job_tree, monkeypatch):
    """A job tree carrying one corrupted .spec file among many clean ones,
    plus the FoldX ranking that survived in the real incident."""
    job_id, job_dir = job_tree
    monkeypatch.setattr(scheduler, 'BASE_RESULT_FOLDER', os.path.dirname(job_dir))
    # The FoldX aggregate is named by the PDB base -- the job id minus its _<uuid8>
    # suffix -- exactly as run_mergeScores.py resolves it (SPILRH for SPILRH_c70d2d4c).
    base = job_id.split('_')[0]

    designs = []
    for i in range(N_DESIGNS):
        design = '%s1Q6%02dF1' % (base, i)
        designs.append(design)
        d = os.path.join(job_dir, '%s_iter2_0125' % job_id, '%s.pdbs' % design)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '%s_B.fasta' % design), 'w') as f:
            f.write('>%s_B\nACDEFGHIKLMNPQRSTVWY\n' % design)
        with open(os.path.join(d, '%s_1.pdb' % design), 'w') as f:
            f.write('ATOM      1  CA  ALA B   1       0.0   0.0   0.0\n')
        with open(os.path.join(job_dir, '%s_iter2_0125' % job_id, '%s.spec' % design), 'wb') as f:
            rec = spec_line(job_id, design, 'ACDEFGHIKLMNPQRSTVWY', -400.0 - i).encode()
            f.write(rec)
            if i == 25:                      # the one corrupted file, as on the affected node
                f.write(rec.rstrip(b'\n') + CORRUPT_BURST + b'\n')

    with open(os.path.join(job_dir, '%s.all.txt' % base), 'w') as f:
        for i, design in enumerate(designs):
            f.write('%s_1.pdb\t-4%d.5\n' % (design, i % 10))

    with open(os.path.join(job_dir, 'job.json'), 'w') as f:
        json.dump({'job_id': job_id, 'status': 'running', 'cpus': 15,
                   'submitted_at': '2026-08-03T11:43:15.192526+00:00'}, f)
    return job_id, job_dir


def _run(script, *args, cwd):
    return subprocess.run([sys.executable, script, *args], cwd=cwd,
                          capture_output=True, text=True)


def _finish(job_id, job_dir, cat_rc, merge_rc):
    """Replay run_iteMAN.py's phase 3 bookkeeping for the given stage results."""
    failed_stage, errors = None, []
    if cat_rc:
        failed_stage = 'score_aggregation'
        errors.append('run_catSPEC.py failed: returned non-zero exit status %d.' % cat_rc)
    if merge_rc:
        failed_stage = failed_stage or 'score_merge'
        errors.append('run_mergeScores.py failed: returned non-zero exit status %d.' % merge_rc)
    status, stage, message = run_iteMAN._assess_final_outcome(
        job_dir, job_id, failed_stage, errors)
    run_iteMAN._write_final_status(job_dir, job_id, status, stage, message)
    with open(os.path.join(job_dir, '%s.zip' % job_id), 'wb') as f:
        f.write(b'PK\x03\x04stub')           # stands in for the real 523 MB zip
    run_iteMAN._stamp_job_json(job_dir, status, stage, message)
    return status, stage, message


def test_corrupt_spec_no_longer_breaks_the_chain(incident_job):
    """The exact incident: aggregation survives, every downstream link is produced."""
    job_id, job_dir = incident_job

    cat = _run(CAT_SPEC, cwd=job_dir)
    assert cat.returncode == 0, cat.stderr
    assert os.path.exists(os.path.join(job_dir, 'non_mc_pdbs.csv'))

    merge = _run(MERGE, job_id, cwd=job_dir)
    assert merge.returncode == 0, merge.stderr
    assert os.path.exists(os.path.join(job_dir, 'tab2_final_scores.csv'))

    status, stage, _msg = _finish(job_id, job_dir, cat.returncode, merge.returncode)
    assert status == 'partial', "data was dropped, so the table is incomplete"
    assert stage == 'spec_file_corruption'

    view = scheduler.public_status(job_id)
    assert view['partial'] is True
    assert view['download_url'] is not None
    assert 'tab2_final_scores.csv' in view['artifacts']


def test_the_damage_is_reported_not_hidden(incident_job):
    """The corrupted record is skipped, counted, and reported in a parseable file."""
    job_id, job_dir = incident_job
    _run(CAT_SPEC, cwd=job_dir)

    report = json.loads(open(os.path.join(job_dir, 'aggregation_report.json')).read())
    assert report['degraded'] is True
    assert report['corrupt_lines_skipped'] == 1
    assert report['spec_files_found'] == N_DESIGNS


def test_total_aggregation_failure_still_yields_a_usable_table(incident_job):
    """If aggregation produces nothing at all, the FoldX ranking still becomes a
    score table -- and the job reports partial, never complete."""
    job_id, job_dir = incident_job
    for root, _dirs, files in os.walk(job_dir):
        for n in files:
            if n.endswith('.spec'):
                os.remove(os.path.join(root, n))

    cat = _run(CAT_SPEC, cwd=job_dir)
    assert cat.returncode == 1, "aggregation that produced nothing must exit non-zero"

    merge = _run(MERGE, job_id, cwd=job_dir)
    assert merge.returncode == 0, merge.stderr

    # Read the marker by column name, not by position: v1.6.0 appends the
    # post-filter columns (disulfide_ok / native_ok / passes_filters) after
    # rosetta_available, so it is no longer the last field on the row.
    with open(os.path.join(job_dir, 'tab2_final_scores.csv')) as fh:
        table = list(csv.DictReader(fh))
    assert len(table) == N_DESIGNS
    assert all(r['energy_available'] == 'False' for r in table), \
        "degradation must be marked in-band"

    status, stage, message = _finish(job_id, job_dir, cat.returncode, merge.returncode)
    assert status == 'partial'
    assert stage == 'score_aggregation'
    assert 'run_catSPEC.py failed' in message

    view = scheduler.public_status(job_id)
    assert view['partial'] is True
    assert view['failed_stage'] == 'score_aggregation'


def test_job_json_is_never_left_running(incident_job):
    """The reported job.json still said "running" 14 h after the job ended."""
    job_id, job_dir = incident_job
    cat = _run(CAT_SPEC, cwd=job_dir)
    merge = _run(MERGE, job_id, cwd=job_dir)
    _finish(job_id, job_dir, cat.returncode, merge.returncode)

    meta = json.loads(open(os.path.join(job_dir, 'job.json')).read())
    assert meta['status'] in scheduler.TERMINAL
    assert meta['completed_at']
    assert meta['cpus'] == 15


def test_clean_run_is_still_complete(incident_job):
    """No false alarms: with undamaged input the job reports a plain Complete."""
    job_id, job_dir = incident_job
    base = job_id.split('_')[0]
    bad = os.path.join(job_dir, '%s_iter2_0125' % job_id, '%s1Q625F1.spec' % base)
    with open(bad, 'w') as f:
        f.write(spec_line(job_id, '%s1Q625F1' % base, 'ACDEFGHIKLMNPQRSTVWY', -425.0))

    cat = _run(CAT_SPEC, cwd=job_dir)
    merge = _run(MERGE, job_id, cwd=job_dir)
    status, stage, _msg = _finish(job_id, job_dir, cat.returncode, merge.returncode)

    assert status == 'complete'
    assert stage is None
    view = scheduler.public_status(job_id)
    assert view['status'] == 'Complete'
    assert view['partial'] is False
