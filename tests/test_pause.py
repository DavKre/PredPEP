"""Pause/resume bookkeeping.

The signalling itself is proved against a live job (see docs/pause.md); what is
checked here is the state machine around it, which is where a pause goes wrong
quietly: a flag that does not survive a worker restart, a terminal job still
marked paused, or a pause that never ends.
"""
import json, os, sys, time, types
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))


@pytest.fixture
def sched(tmp_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, 'BASE_RESULT_FOLDER', str(tmp_path))
    scheduler._queue.clear()
    scheduler._running.clear()
    return scheduler


def _job(sched, jid='SPTEST_1', status='running', pid=None):
    d = os.path.join(sched.BASE_RESULT_FOLDER, jid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'job.json'), 'w') as f:
        json.dump({'job_id': jid, 'status': status}, f)
    if pid is not None:
        with open(os.path.join(d, 'manager.pid'), 'w') as f:
            f.write(str(pid))
    return jid


def test_pause_refuses_a_job_that_is_not_running(sched):
    jid = _job(sched, status='complete')
    ok, err = sched.pause_job(jid)
    assert ok is False and 'not running' in err


def test_pause_refuses_a_queued_job(sched):
    jid = _job(sched, status='queued')
    sched._queue.append(jid)
    ok, err = sched.pause_job(jid)
    assert ok is False and 'queued' in err


def test_pause_persists_to_job_json_not_memory(sched, monkeypatch):
    """A gunicorn worker restart must not lose track of a frozen process."""
    jid = _job(sched, pid=4242)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: True)
    monkeypatch.setattr(sched, '_signal_group', lambda j, s: True)

    ok, err = sched.pause_job(jid)
    assert ok and err is None

    on_disk = json.load(open(os.path.join(sched._jdir(jid), 'job.json')))
    assert on_disk['paused'] is True and 'paused_at' in on_disk
    assert sched.is_paused(jid) is True


def test_pause_is_idempotent(sched, monkeypatch):
    jid = _job(sched, pid=4242)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: True)
    sent = []
    monkeypatch.setattr(sched, '_signal_group', lambda j, s: sent.append(s) or True)
    sched.pause_job(jid)
    sched.pause_job(jid)
    assert len(sent) == 1, 'a second pause must not re-signal'


def test_resume_clears_the_flag(sched, monkeypatch):
    jid = _job(sched, pid=4242)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: True)
    monkeypatch.setattr(sched, '_signal_group', lambda j, s: True)
    sched.pause_job(jid)
    ok, err = sched.resume_job(jid)
    assert ok and err is None
    assert sched.is_paused(jid) is False
    assert 'paused_at' not in json.load(open(os.path.join(sched._jdir(jid), 'job.json')))


def test_resume_of_a_job_that_died_while_frozen_clears_the_flag(sched, monkeypatch):
    """Otherwise the job reconciles to failed while still reading paused."""
    jid = _job(sched, pid=4242)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: True)
    monkeypatch.setattr(sched, '_signal_group', lambda j, s: True)
    sched.pause_job(jid)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: False)
    ok, err = sched.resume_job(jid)
    assert ok is False and 'no longer running' in err
    assert sched.is_paused(jid) is False


def test_auto_resume_releases_an_overdue_pause(sched, monkeypatch):
    """A forgotten pause must not hold a node's cores for ever."""
    jid = _job(sched, pid=4242)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: True)
    monkeypatch.setattr(sched, '_signal_group', lambda j, s: True)
    monkeypatch.setattr(sched, 'MAX_PAUSE_SECONDS', 3600)
    sched._running[jid] = 2
    sched.pause_job(jid)

    meta = json.load(open(os.path.join(sched._jdir(jid), 'job.json')))
    meta['paused_at'] = '2020-01-01T00:00:00+00:00'       # long overdue
    json.dump(meta, open(os.path.join(sched._jdir(jid), 'job.json'), 'w'))

    sched._auto_resume_overdue()
    assert sched.is_paused(jid) is False


def test_auto_resume_leaves_a_fresh_pause_alone(sched, monkeypatch):
    jid = _job(sched, pid=4242)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: True)
    monkeypatch.setattr(sched, '_signal_group', lambda j, s: True)
    monkeypatch.setattr(sched, 'MAX_PAUSE_SECONDS', 3600)
    sched._running[jid] = 2
    sched.pause_job(jid)
    sched._auto_resume_overdue()
    assert sched.is_paused(jid) is True


def test_finalize_clears_a_stale_paused_flag(sched, monkeypatch):
    """A terminal job must never still read paused."""
    jid = _job(sched, pid=4242)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: True)
    monkeypatch.setattr(sched, '_signal_group', lambda j, s: True)
    sched.pause_job(jid)
    sched._finalize(jid, 'stopped')
    meta = json.load(open(os.path.join(sched._jdir(jid), 'job.json')))
    assert 'paused' not in meta and 'paused_at' not in meta


def test_paused_never_leaks_as_a_status_string(sched, monkeypatch):
    """The wire keeps the known vocabulary; paused is a separate boolean.

    A new status string is what broke the dispatcher on 'partial'; an added
    boolean is backward compatible.
    """
    jid = _job(sched, pid=4242)
    monkeypatch.setattr(sched, '_manager_alive', lambda j: True)
    monkeypatch.setattr(sched, '_signal_group', lambda j, s: True)
    sched.pause_job(jid)
    view = sched.public_status(jid)
    assert view['status'] == 'Running'
    assert view['paused'] is True
