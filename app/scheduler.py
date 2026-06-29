# app/scheduler.py — in-process CPU-aware job scheduler + disk retention.
# One gevent greenlet in the single gunicorn worker owns all job lifecycle.
# job.json["status"] is the source of truth: queued -> running -> complete|stopped|failed.
import os
import json
import time
import glob
import re
import shutil
import signal
import subprocess
from datetime import datetime, timezone

BASE_UPLOAD_FOLDER = '/tmp/pepspec/uploads'
BASE_RESULT_FOLDER = '/tmp/pepspec/results'


def _detect_cores():
    try:
        return len(os.sched_getaffinity(0))
    except Exception:
        return os.cpu_count() or 4


CORE_BUDGET = int(os.environ.get('PREDPEP_CORE_BUDGET', _detect_cores()))
RETENTION_BYTES = int(os.environ.get('PREDPEP_RETENTION_BYTES', 50 * 1024 ** 3))
RETENTION_DAYS = int(os.environ.get('PREDPEP_RETENTION_DAYS', 180))

# Iteration cap, shown in the Jobs table as "N / MAX_ITERATIONS" and used to
# infer the finish reason (count >= cap -> limit reached, else early stop).
# MUST mirror MAX_ITERATIONS in pipeline/run_iteMAN.py — update both together.
MAX_ITERATIONS = 6

TERMINAL = ('complete', 'stopped', 'failed')

_queue = []        # job_ids awaiting cores (FIFO)
_running = {}       # job_id -> reserved cpus
_started = False
_disk_used = 0      # bytes, refreshed by the sweep
_last_sweep = 0.0


def _jdir(job_id):
    return os.path.join(BASE_RESULT_FOLDER, job_id)


def _cmd_path(job_id):
    return os.path.join(_jdir(job_id), 'manager.cmd.json')


def _read_meta(job_id):
    try:
        with open(os.path.join(_jdir(job_id), 'job.json')) as f:
            return json.load(f)
    except Exception:
        return {}


def _set_status(job_id, status):
    meta = _read_meta(job_id)
    meta['job_id'] = job_id
    meta['status'] = status
    try:
        with open(os.path.join(_jdir(job_id), 'job.json'), 'w') as f:
            json.dump(meta, f)
    except Exception:
        pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def count_iterations(job_id):
    """Highest iteration index reached, read from the {job_id}_iterN_* worker dirs
    the pipeline creates. Works while a job runs; call before _post_zip_cleanup to
    capture the final count (cleanup deletes those dirs). Returns 0 if none yet."""
    best = 0
    for p in glob.glob(os.path.join(_jdir(job_id), '%s_iter*' % job_id)):
        if not os.path.isdir(p):
            continue
        m = re.search(r'_iter(\d+)', os.path.basename(p))
        if m:
            best = max(best, int(m.group(1)))
    return best


def _finalize(job_id, status):
    """Mark a job terminal and record progress: stamp completed_at + iterations_done
    once, plus a finish_reason for completed jobs (limit vs early). MUST run before
    _post_zip_cleanup so the iteration dirs are still countable."""
    meta = _read_meta(job_id)
    meta['job_id'] = job_id
    meta['status'] = status
    meta.setdefault('completed_at', _now_iso())
    if 'iterations_done' not in meta:
        meta['iterations_done'] = count_iterations(job_id)
    if status == 'complete':
        meta['finish_reason'] = 'limit' if meta.get('iterations_done', 0) >= MAX_ITERATIONS else 'early'
    try:
        with open(os.path.join(_jdir(job_id), 'job.json'), 'w') as f:
            json.dump(meta, f)
    except Exception:
        pass


def _du_bytes(path):
    try:
        out = subprocess.check_output(['du', '-sb', path], stderr=subprocess.DEVNULL)
        return int(out.split()[0])
    except Exception:
        return 0


def _pid_is_manager(pid):
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            return b'run_iteMAN' in f.read()
    except Exception:
        return False


def _manager_alive(job_id):
    try:
        with open(os.path.join(_jdir(job_id), 'manager.pid')) as f:
            return _pid_is_manager(int(f.read().strip()))
    except Exception:
        return False


def kill_job(job_id):
    """SIGKILL the manager process group, guarding against PID reuse. Returns True if killed."""
    try:
        with open(os.path.join(_jdir(job_id), 'manager.pid')) as f:
            pid = int(f.read().strip())
        if _pid_is_manager(pid):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return True
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
        pass
    return False


def enqueue(job_id, manager_command, cpus):
    """Web layer calls this at submit (after writing job.json status=queued). Persist the
    command so the scheduler (or a post-restart reconcile) can launch it; append to FIFO.
    Returns the number of jobs ahead in the queue (0 = front)."""
    try:
        with open(_cmd_path(job_id), 'w') as f:
            json.dump({'cmd': manager_command, 'cpus': int(cpus)}, f)
    except Exception:
        pass
    if job_id not in _queue and job_id not in _running:
        _queue.append(job_id)
    return max(0, _queue.index(job_id)) if job_id in _queue else 0


def cancel(job_id):
    """Stop a job: dequeue if queued, kill if running, mark stopped."""
    if job_id in _queue:
        _queue.remove(job_id)
    kill_job(job_id)
    try:
        open(os.path.join(_jdir(job_id), 'STOPPED'), 'w').close()
    except Exception:
        pass
    _running.pop(job_id, None)
    _finalize(job_id, 'stopped')


def _launch(job_id):
    """Launch the manager for a queued job. Returns reserved cpus or None on failure."""
    try:
        with open(_cmd_path(job_id)) as f:
            spec = json.load(f)
        cmd, cpus = spec['cmd'], int(spec['cpus'])
    except Exception:
        _finalize(job_id, 'failed')
        return None
    d = _jdir(job_id)
    try:
        proc = subprocess.Popen(
            cmd, close_fds=True, start_new_session=True,
            stdout=open(os.path.join(d, '%s_manager_stdout.log' % job_id), 'w'),
            stderr=open(os.path.join(d, '%s_manager_stderr.log' % job_id), 'w'))
        with open(os.path.join(d, 'manager.pid'), 'w') as pf:
            pf.write(str(proc.pid))
        _set_status(job_id, 'running')
        return cpus
    except Exception:
        _finalize(job_id, 'failed')
        return None


def _terminal_for_dead(job_id):
    d = _jdir(job_id)
    if os.path.exists(os.path.join(d, '%s.zip' % job_id)):
        return 'complete'
    if os.path.exists(os.path.join(d, 'STOPPED')):
        return 'stopped'
    return 'failed'


def _post_zip_cleanup(job_id):
    """Keep only <job>.zip + job.json in the result dir; drop the uploads dir."""
    d = _jdir(job_id)
    keep = {'%s.zip' % job_id, 'job.json'}
    try:
        for name in os.listdir(d):
            if name in keep:
                continue
            p = os.path.join(d, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception:
        pass
    shutil.rmtree(os.path.join(BASE_UPLOAD_FOLDER, job_id), ignore_errors=True)


def _tick():
    # 1) completion detection — free cores for finished/dead managers
    for job_id, _cpus in list(_running.items()):
        if not _manager_alive(job_id):
            st = _terminal_for_dead(job_id)
            _finalize(job_id, st)
            if st == 'complete':
                _post_zip_cleanup(job_id)
            _running.pop(job_id, None)
    # 2) admission — strict FIFO
    reserved = sum(_running.values())
    while _queue:
        job_id = _queue[0]
        try:
            cpus = int(json.load(open(_cmd_path(job_id)))['cpus'])
        except Exception:
            _queue.pop(0)
            _finalize(job_id, 'failed')
            continue
        if reserved + cpus > CORE_BUDGET:
            break  # head doesn't fit; wait (no skip-ahead)
        _queue.pop(0)
        got = _launch(job_id)
        if got:
            _running[job_id] = got
            reserved += got


def _sweep():
    """Enforce age + size retention. Only terminal jobs are evicted."""
    global _disk_used
    rows = []
    try:
        entries = os.listdir(BASE_RESULT_FOLDER)
    except FileNotFoundError:
        _disk_used = 0
        return
    for entry in entries:
        d = _jdir(entry)
        if not os.path.isdir(d):
            continue
        st = _read_meta(entry).get('status')
        try:
            mtime = os.path.getmtime(d)
        except OSError:
            mtime = 0
        rows.append([entry, st, mtime, _du_bytes(d)])
    total = sum(r[3] for r in rows) + _du_bytes(BASE_UPLOAD_FOLDER)
    now = time.time()
    age_limit = RETENTION_DAYS * 86400
    for r in rows:
        entry, st, mtime, sz = r
        if st in TERMINAL and (now - mtime) > age_limit and os.path.isdir(_jdir(entry)):
            shutil.rmtree(_jdir(entry), ignore_errors=True)
            shutil.rmtree(os.path.join(BASE_UPLOAD_FOLDER, entry), ignore_errors=True)
            r[3] = 0
            total -= sz
    if total > RETENTION_BYTES:
        for entry, st, mtime, sz in sorted([r for r in rows if r[1] in TERMINAL], key=lambda x: x[2]):
            if total <= RETENTION_BYTES:
                break
            if os.path.isdir(_jdir(entry)):
                shutil.rmtree(_jdir(entry), ignore_errors=True)
                shutil.rmtree(os.path.join(BASE_UPLOAD_FOLDER, entry), ignore_errors=True)
                total -= sz
    _disk_used = max(0, total)


def get_state():
    reserved = sum(_running.values())
    return {
        'core_budget': CORE_BUDGET,
        'reserved_cores': reserved,
        'available_cores': max(0, CORE_BUDGET - reserved),
        'running': len(_running),
        'queued': len(_queue),
        'accepting': True,
        'disk': {
            'used_bytes': _disk_used,
            'cap_bytes': RETENTION_BYTES,
            'used_pct': round(100.0 * _disk_used / RETENTION_BYTES, 1) if RETENTION_BYTES else 0,
        },
    }


def _reconcile():
    """Rebuild scheduler state from the filesystem on startup."""
    try:
        entries = os.listdir(BASE_RESULT_FOLDER)
    except FileNotFoundError:
        return
    for entry in entries:
        d = _jdir(entry)
        if not os.path.isdir(d):
            continue
        meta = _read_meta(entry)
        st = meta.get('status')
        if st is None:                       # pre-Part-B job: derive once
            _set_status(entry, _terminal_for_dead(entry))
        elif st == 'queued':
            if os.path.exists(_cmd_path(entry)):
                _queue.append(entry)
            else:
                _finalize(entry, 'failed')
        elif st == 'running':
            if _manager_alive(entry):
                _running[entry] = int(meta.get('cpus', 2))
            else:                            # subprocess died with the container
                _finalize(entry, _terminal_for_dead(entry))
    _queue.sort(key=lambda j: _read_meta(j).get('submitted_at', ''))


def _loop():
    import gevent
    global _last_sweep
    try:
        _reconcile()
    except Exception as e:
        print('[scheduler] reconcile error: %s' % e, flush=True)
    while True:
        try:
            _tick()
            if time.time() - _last_sweep > 300:
                _sweep()
                _last_sweep = time.time()
        except Exception as e:
            print('[scheduler] tick error: %s' % e, flush=True)
        gevent.sleep(3)


def start_scheduler():
    """Idempotent; start the background loop in the current (worker) process."""
    global _started
    if _started:
        return
    _started = True
    import gevent
    gevent.spawn(_loop)
    print('[scheduler] started: core_budget=%d retention=%dGB/%dd'
          % (CORE_BUDGET, RETENTION_BYTES // (1024 ** 3), RETENTION_DAYS), flush=True)
