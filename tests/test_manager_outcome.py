"""The manager must decide the job's outcome from what it actually produced.

run_iteMAN.py caught the aggregation crash, printed FATAL ERROR to stderr, zipped the
results and exited 0 -- so every consumer saw a clean run. It also cannot rely on exit
codes alone: run_catSPEC.py has paths that exit 0 without producing non_mc_pdbs.csv.
"""
import json
import os

import pytest

import conftest  # noqa: F401  (path setup)
import run_iteMAN


@pytest.fixture
def job_dir(tmp_path):
    d = tmp_path / 'SPILRH_c70d2d4c'
    d.mkdir()
    return d


def _foldx(d, base='SPILRH'):
    (d / ('%s.all.txt' % base)).write_text('SPILRH1Q672F1_1.pdb\t-40.5\n')


def _final_csv(d):
    (d / 'tab2_final_scores.csv').write_text('pdbId,pepSeq,FoldX_Score\nx.pdb,ACDEF,-40.5\n')


def _agg_report(d, **kw):
    report = {'spec_files_found': 480, 'spec_files_unreadable': 0,
              'corrupt_lines_skipped': 0, 'degraded': False}
    report.update(kw)
    (d / 'aggregation_report.json').write_text(json.dumps(report))


def test_the_incident_is_partial_not_complete(job_dir):
    """catSPEC crashed, no score table, but 4,795 designs were scored."""
    _foldx(job_dir)
    status, stage, msg = run_iteMAN._assess_final_outcome(
        str(job_dir), 'SPILRH_c70d2d4c', 'score_aggregation',
        ["run_catSPEC.py failed: returned non-zero exit status 1."])
    assert status == 'partial'
    assert stage == 'score_aggregation'
    assert 'tab2_final_scores.csv' in msg


def test_missing_table_with_no_foldx_is_failed(job_dir):
    status, stage, _msg = run_iteMAN._assess_final_outcome(
        str(job_dir), 'SPILRH_c70d2d4c', None, [])
    assert status == 'failed'
    assert stage == 'score_aggregation'


def test_clean_run_is_complete(job_dir):
    _foldx(job_dir)
    _final_csv(job_dir)
    _agg_report(job_dir)
    status, stage, _msg = run_iteMAN._assess_final_outcome(
        str(job_dir), 'SPILRH_c70d2d4c', None, [])
    assert status == 'complete'
    assert stage is None


def test_silent_success_without_a_table_is_still_caught(job_dir):
    """run_catSPEC.py exits 0 when no .spec files are found or total_score is absent.
    Exit-code checking alone would call that a clean run."""
    _foldx(job_dir)
    status, _stage, _msg = run_iteMAN._assess_final_outcome(
        str(job_dir), 'SPILRH_c70d2d4c', None, [])   # no failed stage reported
    assert status == 'partial'


def test_dropped_input_makes_the_table_incomplete(job_dir):
    """A table built after skipping corrupted .spec data is incomplete, and saying so
    is the whole point -- silently dropping designs is what this incident was about."""
    _foldx(job_dir)
    _final_csv(job_dir)
    _agg_report(job_dir, degraded=True, spec_files_unreadable=1, corrupt_lines_skipped=3)
    status, stage, msg = run_iteMAN._assess_final_outcome(
        str(job_dir), 'SPILRH_c70d2d4c', None, [])
    assert status == 'partial'
    assert stage == 'spec_file_corruption'
    assert '1 unreadable' in msg


def test_final_status_file_is_written_for_the_zip(job_dir):
    run_iteMAN._write_final_status(str(job_dir), 'SPILRH_c70d2d4c', 'partial',
                                   'score_aggregation', 'boom')
    data = json.loads((job_dir / 'final_status.json').read_text())
    assert data['status'] == 'partial'
    assert data['failed_stage'] == 'score_aggregation'
    assert data['job_id'] == 'SPILRH_c70d2d4c'


def test_job_json_is_stamped_terminal(job_dir):
    """job.json read "status": "running" 14 h after the job ended."""
    (job_dir / 'job.json').write_text(json.dumps({'job_id': 'SPILRH_c70d2d4c',
                                                  'status': 'running', 'cpus': 15}))
    run_iteMAN._stamp_job_json(str(job_dir), 'partial', 'score_aggregation', 'boom')
    meta = json.loads((job_dir / 'job.json').read_text())
    assert meta['status'] == 'partial'
    assert meta['failed_stage'] == 'score_aggregation'
    assert meta['completed_at']
    assert meta['cpus'] == 15, "existing submission metadata must be preserved"
