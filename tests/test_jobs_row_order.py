"""The /jobs row must be built after the status call, not before.

Observed on SPPAUP_dd66980b the moment it finished: the row read

    status: Complete · iteration: None · iterations_done: None

while job.json on disk already said iterations_done: 6. public_status() is not a
pure read -- for a job whose manager has just died it derives the terminal state
and finalises job.json -- so a caller that snapshots job.json first pairs the
fresh status with pre-finalize meta. It self-corrected on the next poll, which is
what made it easy to dismiss.

These tests pin the ordering rather than the symptom: they assert that the data
the row needs exists only after the call, so reading meta first is wrong by
construction.
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
    scheduler._queue.clear()
    scheduler._running.clear()
    return root


def _finished_but_unfinalised(root, job_id='SPROW_1'):
    """A job that has produced its zip and six iteration dirs, whose manager is
    gone, but whose job.json still says 'running' -- the state every job passes
    through between its last breath and the next scheduler tick."""
    d = root / job_id
    d.mkdir()
    (d / ('%s.zip' % job_id)).write_bytes(b'PK\x05\x06' + b'\0' * 18)
    for i in range(1, 7):
        (d / ('%s_iter%d_x' % (job_id, i))).mkdir()
    (d / 'job.json').write_text(json.dumps({
        'job_id': job_id, 'status': 'running',
        'submitted_at': '2026-11-23T13:00:00+00:00',
        'protein_symbol': 'ROWTEST', 'user_name': 'test.user', 'cpus': 4,
    }))
    (d / 'final_status.json').write_text(json.dumps({'status': 'complete'}))
    return job_id


def _read_meta(root, job_id):
    with open(os.path.join(str(root), job_id, 'job.json')) as f:
        return json.load(f)


def test_meta_lacks_the_count_before_the_status_call(results_root):
    """The precondition for the bug: reading first gets you nothing."""
    job_id = _finished_but_unfinalised(results_root)
    assert 'iterations_done' not in _read_meta(results_root, job_id)


def test_status_call_finalises_the_count(results_root):
    job_id = _finished_but_unfinalised(results_root)
    view = scheduler.public_status(job_id)
    assert view['status'] == 'Complete'
    assert _read_meta(results_root, job_id)['iterations_done'] == 6


def test_the_wrong_order_reproduces_the_blank_iteration_count(results_root):
    """What the Operations table used to do."""
    job_id = _finished_but_unfinalised(results_root)

    meta = _read_meta(results_root, job_id)            # read first  <-- the bug
    view = scheduler.public_status(job_id)
    meta['status'] = view['status']
    row_iteration = meta.get('iterations_done')

    assert meta['status'] == 'Complete'
    assert row_iteration is None, 'this is the defect being guarded against'


def test_the_fixed_order_gives_a_consistent_row(results_root):
    """What it does now: status and count come from the same generation."""
    job_id = _finished_but_unfinalised(results_root)

    view = scheduler.public_status(job_id)             # call first  <-- the fix
    meta = _read_meta(results_root, job_id)
    meta['status'] = view['status']
    row_iteration = meta.get('iterations_done')

    assert meta['status'] == 'Complete'
    assert row_iteration == 6


def test_completed_at_is_also_present_in_the_fixed_order(results_root):
    """Elapsed is computed from completed_at, so the same staleness showed there
    as a finished job whose elapsed time kept counting up."""
    job_id = _finished_but_unfinalised(results_root)
    scheduler.public_status(job_id)
    assert _read_meta(results_root, job_id).get('completed_at')
