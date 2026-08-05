"""A job that failed must never report as a successful one.

Job SPILRH_c70d2d4c crashed in run_catSPEC.py, skipped the merge, produced no
tab2_final_scores.csv -- and still reported "Complete", because completion was
inferred purely from the existence of the result zip.

These tests pin the reporting contract:
  * a failed final stage yields a terminal 'partial' state carrying a machine-readable
    failed_stage, not 'complete';
  * the wire value stays 'Complete' until PREDPEP_PARTIAL_STATUS is enabled, because
    the DDN client maps unrecognised statuses to *running* and polls without a timeout;
  * results stay downloadable for partial/stopped jobs;
  * job.json is finalised even if the scheduler never observed the job dying.
"""
import json
import os

import pytest

import conftest  # noqa: F401  (path setup)
import scheduler


@pytest.fixture
def results_root(tmp_path, monkeypatch):
    root = tmp_path / 'results'
    root.mkdir()
    monkeypatch.setattr(scheduler, 'BASE_RESULT_FOLDER', str(root))
    monkeypatch.setattr(scheduler, 'BASE_UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    return root


def _make_job(root, job_id='SPILRH_c70d2d4c', *, zip_=True, status='running',
              final_status=None, stopped=False):
    d = root / job_id
    d.mkdir()
    (d / 'job.json').write_text(json.dumps({
        'job_id': job_id, 'submitted_at': '2026-08-03T11:43:15.192526+00:00',
        'protein_symbol': 'ILRH_113', 'user_name': 'test.user',
        'cpus': 15, 'pdb_filename': 'SPILRH.pdb', 'peptide_length': 30,
        'status': status,
    }))
    if zip_:
        (d / ('%s.zip' % job_id)).write_bytes(b'PK\x03\x04stub')
    if final_status is not None:
        (d / 'final_status.json').write_text(json.dumps(final_status))
    if stopped:
        (d / 'STOPPED').write_text('')
    return job_id, d


AGG_FAILED = {'status': 'partial', 'failed_stage': 'score_aggregation',
              'message': 'run_catSPEC.py failed; tab2_final_scores.csv was not produced'}


# --- the incident itself -------------------------------------------------------

def test_aggregation_failure_is_not_complete(results_root):
    job_id, _d = _make_job(results_root, final_status=AGG_FAILED)
    assert scheduler._terminal_for_dead(job_id) == 'partial'


def test_failed_stage_is_recorded_in_job_json(results_root):
    """The only evidence today is a traceback inside a 523 MB zip."""
    job_id, d = _make_job(results_root, final_status=AGG_FAILED)
    scheduler._finalize(job_id, scheduler._terminal_for_dead(job_id))
    meta = json.loads((d / 'job.json').read_text())
    assert meta['status'] == 'partial'
    assert meta['failed_stage'] == 'score_aggregation'
    assert meta['message']
    assert meta['completed_at']


def test_clean_job_still_completes(results_root):
    """No regression for healthy jobs, including ones predating final_status.json."""
    job_id, _d = _make_job(results_root)
    assert scheduler._terminal_for_dead(job_id) == 'complete'


def test_crashed_job_without_zip_is_failed(results_root):
    job_id, _d = _make_job(results_root, zip_=False)
    assert scheduler._terminal_for_dead(job_id) == 'failed'


def test_stopped_job_is_stopped(results_root):
    job_id, _d = _make_job(results_root, zip_=False, stopped=True)
    assert scheduler._terminal_for_dead(job_id) == 'stopped'


# --- wire compatibility (DDN maps unknown statuses to running, polls forever) ----

def test_partial_reports_as_complete_by_default(results_root, monkeypatch):
    monkeypatch.delenv('PREDPEP_PARTIAL_STATUS', raising=False)
    job_id, _d = _make_job(results_root, status='partial', final_status=AGG_FAILED)
    view = scheduler.public_status(job_id)
    assert view['status'] == 'Complete', \
        "emitting an unrecognised status would hang the DDN poll loop"
    assert view['partial'] is True
    assert view['failed_stage'] == 'score_aggregation'
    assert view['message']


def test_partial_reports_as_partial_once_enabled(results_root, monkeypatch):
    monkeypatch.setenv('PREDPEP_PARTIAL_STATUS', '1')
    job_id, _d = _make_job(results_root, status='partial', final_status=AGG_FAILED)
    view = scheduler.public_status(job_id)
    assert view['status'] == 'Partial'
    assert view['partial'] is True


def test_healthy_job_is_not_flagged_partial(results_root):
    job_id, _d = _make_job(results_root, status='complete')
    view = scheduler.public_status(job_id)
    assert view['status'] == 'Complete'
    assert view['partial'] is False
    assert view['failed_stage'] is None


# --- download availability ------------------------------------------------------

@pytest.mark.parametrize('status', ['complete', 'partial', 'stopped', 'failed'])
def test_download_offered_whenever_a_zip_exists(results_root, status):
    """The Jobs page showed Download '—' while a complete 523 MB zip sat on disk."""
    job_id, _d = _make_job(results_root, status=status)
    view = scheduler.public_status(job_id)
    assert view['download_url'] == '/download/%s/%s.zip' % (job_id, job_id)


def test_no_download_without_a_zip(results_root):
    job_id, _d = _make_job(results_root, zip_=False, status='failed')
    assert scheduler.public_status(job_id)['download_url'] is None


def test_artifacts_are_listed(results_root):
    """Lets a client salvage deterministically instead of guessing."""
    job_id, d = _make_job(results_root, final_status=AGG_FAILED)
    (d / 'SPILRH.all.txt').write_text('SPILRH1Q672F1_1.pdb\t-40.5\n')
    scheduler._finalize(job_id, scheduler._terminal_for_dead(job_id))
    meta = json.loads((d / 'job.json').read_text())
    assert 'SPILRH.all.txt' in meta['artifacts']


# --- job.json must not be left saying "running" ---------------------------------

def test_stale_running_job_with_dead_manager_is_finalised(results_root):
    """job.json still read "status": "running" 14 h after the job ended. Whatever
    stopped the scheduler from finalising it, a read of the job must self-heal."""
    job_id, d = _make_job(results_root, status='running', final_status=AGG_FAILED)
    view = scheduler.public_status(job_id)
    assert view['status'] != 'Running'
    meta = json.loads((d / 'job.json').read_text())
    assert meta['status'] == 'partial'
    assert meta['completed_at']


def test_running_job_with_live_manager_stays_running(results_root, monkeypatch):
    job_id, d = _make_job(results_root, zip_=False, status='running')
    monkeypatch.setattr(scheduler, '_manager_alive', lambda j: True)
    view = scheduler.public_status(job_id)
    assert view['status'] == 'Running'
    assert json.loads((d / 'job.json').read_text())['status'] == 'running'
