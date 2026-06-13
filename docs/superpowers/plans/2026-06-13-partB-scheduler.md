# Part B — In-process Scheduler, Retention & Capacity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a CPU-aware in-process job scheduler (FIFO queue capped at a core budget), a `/state` capacity endpoint, disk retention (≤50 GB / ≤6 mo) + post-zip cleanup, a Jobs-tab disk bar + Queued/Running/Failed statuses, and the boot-wedge fix (`--preload`).

**Architecture:** A new `app/scheduler.py` owns job lifecycle (queue → admission → completion → retention) as one gevent greenlet in the single gunicorn worker; `job.json.status` is the source of truth. `app/predPEP.py` becomes the thin web layer (submit enqueues; `/state`, `/jobs`, `/status` read scheduler state; stop routes through the scheduler). `app/gunicorn.conf.py` adds `preload_app` + starts the scheduler.

**Tech Stack:** Flask/gunicorn(gevent), Python stdlib + gevent, `du`, vanilla JS, Docker.

---

### Task 0: Branch + rollback tag
- [ ] **Step 1**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git checkout -b feature/partB-scheduler
docker tag predpep:local predpep:preB
git status -sb | head -1
```
Expected: `## feature/partB-scheduler`.

---

### Task 1: Create the scheduler module
**Files:** Create `app/scheduler.py`

- [ ] **Step 1: write `app/scheduler.py`**
```python
# app/scheduler.py — in-process CPU-aware job scheduler + disk retention.
# One gevent greenlet in the single gunicorn worker owns all job lifecycle.
# job.json["status"] is the source of truth: queued -> running -> complete|stopped|failed.
import os
import json
import time
import shutil
import signal
import subprocess

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
    _set_status(job_id, 'stopped')


def _launch(job_id):
    """Launch the manager for a queued job. Returns reserved cpus or None on failure."""
    try:
        with open(_cmd_path(job_id)) as f:
            spec = json.load(f)
        cmd, cpus = spec['cmd'], int(spec['cpus'])
    except Exception:
        _set_status(job_id, 'failed')
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
        _set_status(job_id, 'failed')
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
            _set_status(job_id, st)
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
            _set_status(job_id, 'failed')
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
                _set_status(entry, 'failed')
        elif st == 'running':
            if _manager_alive(entry):
                _running[entry] = int(meta.get('cpus', 2))
            else:                            # subprocess died with the container
                _set_status(entry, _terminal_for_dead(entry))
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
```

- [ ] **Step 2: syntax check + commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
python3 -c "import ast; ast.parse(open('app/scheduler.py').read()); print('scheduler.py parses OK')"
git add app/scheduler.py
git commit -q -m "feat(scheduler): in-process CPU-aware queue + completion + retention module"
```
Expected: `scheduler.py parses OK`.

---

### Task 2: Wire the scheduler into `app/predPEP.py`

- [ ] **Step 1: import the module + lazy start**

After the existing `from flask import ...` import line, add:
```python
import scheduler
```
Then, right after `predPEP = Flask(__name__)` (the app object), add:
```python

@predPEP.before_request
def _ensure_scheduler():
    scheduler.start_scheduler()  # idempotent; starts in the worker on first request
```

- [ ] **Step 2: clamp `cpus` to the core budget**

Change:
```python
        cpus = max(2, min(32, int(request.form.get('cpus', '8'))))
```
to:
```python
        cpus = max(2, min(32, scheduler.CORE_BUDGET, int(request.form.get('cpus', '8'))))
```

- [ ] **Step 3: submit → enqueue (replace the direct Popen)**

Replace the metadata-write-through-return block. Change:
```python
    # Persist submission metadata for the Jobs list (survives on the volume)
    try:
        with open(os.path.join(master_result_folder, 'job.json'), 'w') as jf:
            json.dump({
                'job_id': job_folder_name,
                'submitted_at': datetime.now(timezone.utc).isoformat(),
                'protein_symbol': protein_symbol,
                'user_name': user_name,
                'cpus': int(cpus),
                'pdb_filename': new_filename,
                'peptide_length': count_peptide_residues(filepath),
            }, jf)
    except Exception as e:
        predPEP.logger.warning(f"[submit] could not write job.json: {e}")

    # 4. LAUNCH ASYNCHRONOUS ITERATIVE MANAGER
    try:
        if not os.path.exists(MANAGER_SCRIPT_PATH) or not os.access(MANAGER_SCRIPT_PATH, os.X_OK):
            return jsonify({'success': False, 'error': 'Manager script not found/executable.'})

        # MODIFIED: Last argument passed as job_folder_name instead of new_pdb_base
        # GEÄNDERT: Letztes Argument als job_folder_name anstelle von new_pdb_base übergeben
        manager_command = [
            PYTHON_EXECUTABLE, MANAGER_SCRIPT_PATH,
            filepath, master_result_folder, cpus, 
            job_folder_name, master_result_folder, job_folder_name
        ]

        proc = subprocess.Popen(
            manager_command, close_fds=True, start_new_session=True,
            stdout=open(os.path.join(master_result_folder, f'{job_folder_name}_manager_stdout.log'), 'w'),
            stderr=open(os.path.join(master_result_folder, f'{job_folder_name}_manager_stderr.log'), 'w')
        )
        try:
            with open(os.path.join(master_result_folder, 'manager.pid'), 'w') as pf:
                pf.write(str(proc.pid))
        except Exception as e:
            predPEP.logger.warning(f"[submit] could not write manager.pid: {e}")

        return jsonify({
            'success': True,
            'message': f'Job submitted for {new_pdb_base} (ID: {job_folder_name}).',
            'job_id': job_folder_name
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
```
to:
```python
    if not os.path.exists(MANAGER_SCRIPT_PATH) or not os.access(MANAGER_SCRIPT_PATH, os.X_OK):
        return jsonify({'success': False, 'error': 'Manager script not found/executable.'})

    manager_command = [
        PYTHON_EXECUTABLE, MANAGER_SCRIPT_PATH,
        filepath, master_result_folder, cpus,
        job_folder_name, master_result_folder, job_folder_name
    ]

    # Persist submission metadata (status=queued); the scheduler launches the manager
    # when cores are free and owns the status field from here on.
    try:
        with open(os.path.join(master_result_folder, 'job.json'), 'w') as jf:
            json.dump({
                'job_id': job_folder_name,
                'submitted_at': datetime.now(timezone.utc).isoformat(),
                'protein_symbol': protein_symbol,
                'user_name': user_name,
                'cpus': int(cpus),
                'pdb_filename': new_filename,
                'peptide_length': count_peptide_residues(filepath),
                'status': 'queued',
            }, jf)
    except Exception as e:
        predPEP.logger.warning(f"[submit] could not write job.json: {e}")

    try:
        ahead = scheduler.enqueue(job_folder_name, manager_command, int(cpus))
    except Exception as e:
        return jsonify({'success': False, 'error': f'Failed to queue job: {e}'})

    state = scheduler.get_state()
    placed = 'running' if ahead == 0 and state['available_cores'] >= int(cpus) else 'queued'
    return jsonify({
        'success': True,
        'job_id': job_folder_name,
        'status': placed,
        'queue_position': ahead,
        'message': (f'Job {job_folder_name} running.' if placed == 'running'
                    else f'Job {job_folder_name} queued ({ahead} ahead).'),
    })
```

- [ ] **Step 4: status mapping helper + `/jobs` + `/status` + `/state`**

Add a helper just above the `list_jobs` route:
```python
_STATUS_DISPLAY = {'queued': 'Queued', 'running': 'Running', 'complete': 'Complete',
                   'stopped': 'Stopped', 'failed': 'Failed'}

def _display_status(jdir, entry):
    """Return (display_status, download_url) from job.json status (scheduler-owned),
    falling back to filesystem markers for any job without a status field."""
    raw = None
    try:
        with open(os.path.join(jdir, 'job.json')) as f:
            raw = json.load(f).get('status')
    except Exception:
        raw = None
    if raw is None:
        if os.path.exists(os.path.join(jdir, f"{entry}.zip")):
            raw = 'complete'
        elif os.path.exists(os.path.join(jdir, 'STOPPED')):
            raw = 'stopped'
        else:
            raw = 'running'
    disp = _STATUS_DISPLAY.get(raw, 'Running')
    dl = f"/download/{entry}/{entry}.zip" if (raw == 'complete' and os.path.exists(os.path.join(jdir, f"{entry}.zip"))) else None
    return disp, dl
```
In `list_jobs`, replace the status/download block:
```python
            meta.setdefault('job_id', entry)
            if os.path.exists(os.path.join(jdir, f"{entry}.zip")):
                meta['status'] = 'Complete'
                meta['download_url'] = f"/download/{entry}/{entry}.zip"
            elif os.path.exists(os.path.join(jdir, 'STOPPED')):
                meta['status'] = 'Stopped'
                meta['download_url'] = None
            else:
                meta['status'] = 'Processing'
                meta['download_url'] = None
```
with:
```python
            meta.setdefault('job_id', entry)
            meta['status'], meta['download_url'] = _display_status(jdir, entry)
```
Replace the body of `check_status`'s else-branch. Change:
```python
        if os.path.exists(os.path.join(master_result_dir, 'STOPPED')):
            return jsonify({'status': 'Stopped', 'message': 'Job was stopped.'})
        if os.path.exists(master_result_dir):
            return jsonify({'status': 'Processing', 'message': 'Job is running iterations...'})
        return jsonify({'status': 'Pending/Failed', 'message': 'Job failed to start.'})
```
to:
```python
        if os.path.exists(master_result_dir):
            disp, dl = _display_status(master_result_dir, master_pdb_base)
            return jsonify({'status': disp, 'download_url': dl,
                            'message': f'Job {disp.lower()}.'})
        return jsonify({'status': 'Pending/Failed', 'message': 'Job failed to start.'})
```
(The leading `if os.path.exists(output_zip_path): return Complete` at the top of `check_status` stays as-is.)

Add a `/state` route immediately before the `download_file` route:
```python
@predPEP.route('/state', methods=['GET'])
def node_state():
    """Capacity + disk for DDN dispatch and the UI bar."""
    return jsonify(scheduler.get_state())


```

- [ ] **Step 5: route stop/delete through the scheduler**

Change `stop_job`'s kill+marker body. Replace:
```python
    killed = _kill_job(jdir)
    try:
        open(os.path.join(jdir, 'STOPPED'), 'w').close()
    except Exception:
        pass
    return jsonify({'success': True, 'stopped': job_id, 'killed': killed})
```
with:
```python
    scheduler.cancel(job_id)
    return jsonify({'success': True, 'stopped': job_id})
```
In `delete_job`, change the kill call:
```python
    _kill_job(os.path.join(BASE_RESULT_FOLDER, job_id))
```
to:
```python
    scheduler.cancel(job_id)
```
Delete the now-unused `_kill_job` function (the whole `def _kill_job(jdir): ... return False` block) and remove the now-unused `import signal` line (scheduler owns killing). Keep `import os, shutil, subprocess, json` (still used elsewhere).

- [ ] **Step 6: verify + commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
python3 -c "import ast; ast.parse(open('app/predPEP.py').read()); print('OK')"
grep -nE "import scheduler|/state|scheduler.enqueue|scheduler.cancel|_display_status|before_request" app/predPEP.py | head
test -z "$(grep -n 'def _kill_job' app/predPEP.py)" && echo "_kill_job removed"
git add app/predPEP.py
git commit -q -m "feat(api): submit enqueues to scheduler; /state; status from scheduler; stop via scheduler"
```
Expected: `OK`; references present; `_kill_job removed`.

---

### Task 3: gunicorn config (preload + start scheduler)
**Files:** Create `app/gunicorn.conf.py`

- [ ] **Step 1: write `app/gunicorn.conf.py`**
```python
# Auto-loaded by gunicorn from the WORKDIR (/opt/sp-predPEP).
# preload_app imports the Flask app in the master BEFORE the gevent worker forks,
# which avoids the intermittent fork-time worker wedge. The scheduler is started
# per-worker (post_fork) so it runs in the worker, not the preload master.
preload_app = True

def post_worker_init(worker):
    try:
        import predPEP  # noqa: F401
        predPEP.scheduler.start_scheduler()
    except Exception as e:
        worker.log.warning("scheduler start failed in post_worker_init: %s" % e)
```

- [ ] **Step 2: confirm it lands in the image WORKDIR**

`COPY app/ /opt/sp-predPEP/` already ships everything in `app/`, so `gunicorn.conf.py` reaches
`/opt/sp-predPEP/gunicorn.conf.py` (the CWD gunicorn auto-discovers). No Dockerfile/CMD change
needed — the CMD's explicit flags merge over the config file.
```bash
cd /home/david/DATA/OFFLINE/predpep_local
python3 -c "import ast; ast.parse(open('app/gunicorn.conf.py').read()); print('OK')"
git add app/gunicorn.conf.py
git commit -q -m "build: gunicorn preload_app + start scheduler per worker (boot-wedge fix)"
```

---

### Task 4: Runtime config — env passthrough + optional memory cap
**Files:** `scripts/run.sh`, `scripts/run-dev.sh`, `README.md`

- [ ] **Step 1: `scripts/run.sh` — pass scheduler envs + optional --memory**

Immediately before the `docker run -d \` line, add:
```bash
ENV_ARGS=()
for v in PREDPEP_CORE_BUDGET PREDPEP_RETENTION_BYTES PREDPEP_RETENTION_DAYS; do
  [ -n "${!v:-}" ] && ENV_ARGS+=( -e "$v=${!v}" )
done
[ -n "${PREDPEP_MEMORY:-}" ] && ENV_ARGS+=( --memory "${PREDPEP_MEMORY}" )
```
and change:
```bash
docker run -d \
  --name "${CONTAINER}" \
  -v predpep_data:/tmp/pepspec \
```
to:
```bash
docker run -d \
  --name "${CONTAINER}" \
  "${ENV_ARGS[@]}" \
  -v predpep_data:/tmp/pepspec \
```

- [ ] **Step 2: `scripts/run-dev.sh` — same**

Add the identical `ENV_ARGS=(...)` block before its `docker run -d \`, and insert
`  "${ENV_ARGS[@]}" \` right after `--name "${CONTAINER}" \`.

- [ ] **Step 3: `README.md` — document tuning + autoheal**

In `## Web UI` (or a new `## Tuning` block after Daily use), add:
```markdown
## Tuning & reliability (env vars on `scripts/run.sh`)

- `PREDPEP_CORE_BUDGET` — max CPU cores the node will commit across running jobs (default: the machine's core count). Web-submitted jobs reserve their CPU count and queue when the budget is full.
- `PREDPEP_RETENTION_BYTES` / `PREDPEP_RETENTION_DAYS` — job-storage caps (default 50 GB / 180 days). Oldest finished jobs are evicted first; completed jobs keep only their result `.zip`.
- `PREDPEP_MEMORY` — optional Docker memory cap (e.g. `PREDPEP_MEMORY=32g`), passed to `--memory`.
- **Auto-heal:** the gunicorn worker preloads the app to avoid the fork-time boot wedge; for an unattended fleet, also run a restarter that reacts to `health=unhealthy` (e.g. the `willfarrell/autoheal` sidecar, or have DDN `docker restart` a node whose `/health` fails).
```

- [ ] **Step 4: commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
bash -n scripts/run.sh && bash -n scripts/run-dev.sh && echo "scripts OK"
git add scripts/run.sh scripts/run-dev.sh README.md
git commit -q -m "deploy: pass scheduler env config + optional --memory; document tuning/autoheal"
```
Expected: `scripts OK`.

---

### Task 5: UI — disk bar + Queued/Running/Failed statuses + submit placement
**Files:** `app/static/tab7_jobs.js`, `app/templates/index.html`, `app/static/tab1_submission.js`

- [ ] **Step 1: `tab7_jobs.js` — fetch /state + render the disk bar, and status colors**

Replace the whole `window.loadJobs = async function () { ... };` with:
```javascript
window.loadJobs = async function () {
    const tbody = document.getElementById('jobsTableBody');
    if (!tbody) return;
    try {
        const [jr, sr] = await Promise.all([fetch('/jobs'), fetch('/state')]);
        const data = await jr.json();
        const state = await sr.json().catch(() => null);
        renderDiskBar(state);
        if (!data.success) { tbody.innerHTML = `<tr><td colspan="8">Error: ${data.error || 'failed'}</td></tr>`; return; }
        if (!data.jobs.length) { tbody.innerHTML = `<tr><td colspan="8">No jobs yet.</td></tr>`; return; }
        const esc = s => String(s ?? '—').replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
        const cls = st => ({ Complete: 'status-complete', Running: 'status-processing', Queued: 'status-queued',
                             Stopped: 'status-stopped', Failed: 'status-stopped' }[st] || 'status-processing');
        tbody.innerHTML = data.jobs.map(j => {
            const date = j.submitted_at ? new Date(j.submitted_at).toLocaleString() : '—';
            const dl = j.download_url ? `<a href="${j.download_url}">Download</a>` : '—';
            const stoppable = (j.status === 'Running' || j.status === 'Queued');
            return `<tr>
                <td>${date}</td><td>${esc(j.protein_symbol)}</td><td>${esc(j.user_name)}</td>
                <td>${esc(j.cpus)}</td><td>${esc(j.peptide_length)}</td>
                <td class="${cls(j.status)}">${esc(j.status)}</td><td>${dl}</td>
                <td>${stoppable ? `<button class="job-stop" data-id="${encodeURIComponent(j.job_id)}">Stop</button> ` : ''}<button class="job-delete" data-id="${encodeURIComponent(j.job_id)}">Delete</button></td>
            </tr>`;
        }).join('');
        tbody.querySelectorAll('.job-stop').forEach(b =>
            b.addEventListener('click', () => window.stopJob(decodeURIComponent(b.dataset.id))));
        tbody.querySelectorAll('.job-delete').forEach(b =>
            b.addEventListener('click', () => window.deleteJob(decodeURIComponent(b.dataset.id))));
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="8">Error loading jobs: ${e}</td></tr>`;
    }
};

function renderDiskBar(state) {
    const bar = document.getElementById('diskBar');
    const lbl = document.getElementById('diskLabel');
    if (!bar || !state || !state.disk) return;
    const gb = b => (b / (1024 ** 3)).toFixed(1);
    const pct = Math.min(100, state.disk.used_pct || 0);
    bar.style.width = pct + '%';
    bar.style.background = pct > 90 ? '#c5221f' : (pct > 70 ? '#b06000' : '#137333');
    lbl.textContent = `Disk: ${gb(state.disk.used_bytes)} / ${gb(state.disk.cap_bytes)} GB (${pct}%)  ·  Cores: ${state.reserved_cores}/${state.core_budget} used, ${state.queued} queued`;
}
```

- [ ] **Step 2: `index.html` — disk bar markup + status CSS**

Just inside `<div id="tab7-view" class="tab-content hidden">`, after the `<h2>Jobs</h2>` line, add:
```html
            <div style="margin:0.5rem 0;">
                <div id="diskLabel" style="font-size:0.85em;color:#555;">Disk: …</div>
                <div style="background:#eee;border-radius:4px;height:10px;overflow:hidden;">
                    <div id="diskBar" style="height:10px;width:0%;background:#137333;transition:width .3s;"></div>
                </div>
            </div>
```
In the `<style>` block, after the `.status-processing` rule add:
```css
        .status-queued { color: #555; }
        .status-stopped { color: #c5221f; font-weight: bold; }
```

- [ ] **Step 3: `tab1_submission.js` — show queued/running placement on submit**

Find where the upload response is handled (after `await fetch('/upload'...)` resolves and `data.job_id` is used to start polling). Set the status message from the new response fields. The submit handler already reads `data` from the upload; add right where it begins polling (before/at `window.statusInterval = setInterval(...)`):
```javascript
            document.getElementById('message').textContent = data.message
                || (data.status === 'queued' ? 'Job queued.' : 'Job running…');
```
(The exact insertion point is the success branch of the submit handler around line 175–185; place it just before the `setInterval(() => window.pollStatus(jobId), 5000)` call.)

- [ ] **Step 4: `index.js` `pollStatus` — keep polling on Queued/Running, stop on terminal**

Change:
```javascript
        } else if (data.status === 'Stopped' || data.status === 'Pending/Failed') {
            clearInterval(window.statusInterval);
            document.getElementById('loading').style.display = 'none';
        }
```
to:
```javascript
        } else if (['Stopped', 'Failed', 'Pending/Failed'].includes(data.status)) {
            clearInterval(window.statusInterval);
            document.getElementById('loading').style.display = 'none';
        }
```
(`Queued`/`Running` fall through → keep polling, which is correct.)

- [ ] **Step 5: verify + commit**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
for f in tab7_jobs.js index.js tab1_submission.js; do node --check app/static/$f && echo "$f OK"; done
git add app/static/tab7_jobs.js app/static/index.js app/static/tab1_submission.js app/templates/index.html
git commit -q -m "feat(ui): disk usage bar, Queued/Running/Failed statuses, submit placement message"
```

---

### Task 6: Build + smoke (queue, /state, retention, restart)
**Files:** none — `predpep_smoke2` (6365), throwaway volume, `PREDPEP_CORE_BUDGET=4`

- [ ] **Step 1: Build**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
./scripts/build.sh 2>&1 | tee build.log | tail -n 6
```
Expected: image written; `OK: Rosetta prune kept…`; no ERROR.

- [ ] **Step 2: Launch with a small budget (gentle wait + restart fallback)**
```bash
docker rm -f predpep_smoke2 2>/dev/null; docker volume rm predpep_data_smoke 2>/dev/null
docker run -d --name predpep_smoke2 -e PREDPEP_CORE_BUDGET=4 -v predpep_data_smoke:/tmp/pepspec -p 6365:6363 predpep:local >/dev/null
sleep 12
curl -fsS --max-time 6 http://localhost:6365/health >/dev/null 2>&1 || { docker restart predpep_smoke2 >/dev/null; sleep 12; }
echo "state: $(curl -fsS --max-time 6 http://localhost:6365/state)"
```
Expected: `/state` shows `core_budget:4, reserved_cores:0, queued:0`.

- [ ] **Step 3: Queue admission — 2 jobs of cpus=4 (only one fits)**
```bash
sub(){ curl -fsS --max-time 15 -F protein_symbol=$1 -F user_name=t -F cpus=4 -F file1=@examples/quicktest.pdb http://localhost:6365/upload; }
A=$(sub AAA); echo "A: $A"
B=$(sub BBB); echo "B: $B"
sleep 5
echo "state: $(curl -fsS --max-time 6 http://localhost:6365/state)"
echo "statuses: $(curl -fsS --max-time 6 http://localhost:6365/jobs | python3 -c "import sys,json;print([(j['protein_symbol'],j['status']) for j in json.load(sys.stdin)['jobs']])")"
```
Expected: A `status:"running"`, B `status:"queued","queue_position":1`; `/state` → `reserved_cores:4, available_cores:0, running:1, queued:1`; statuses show one `Running`, one `Queued`.

- [ ] **Step 4: Stop the running job → queued one starts**
```bash
AID=$(echo "$A" | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -fsS --max-time 8 -X POST http://localhost:6365/jobs/$AID/stop >/dev/null
sleep 6
echo "state: $(curl -fsS --max-time 6 http://localhost:6365/state)"
echo "statuses: $(curl -fsS --max-time 6 http://localhost:6365/jobs | python3 -c "import sys,json;print([(j['protein_symbol'],j['status']) for j in json.load(sys.stdin)['jobs']])")"
```
Expected: A `Stopped`, B transitioned `Queued→Running`; `/state` reserved back to 4, queued 0.

- [ ] **Step 5: Retention — fake complete jobs evicted by a tiny cap**
```bash
docker exec predpep_smoke2 bash -lc '
for n in 1 2 3; do d=/tmp/pepspec/results/FAKE_$n; mkdir -p $d; printf "{\"job_id\":\"FAKE_%s\",\"submitted_at\":\"2026-01-0%s\",\"status\":\"complete\"}" $n $n > $d/job.json; head -c 5000000 /dev/zero > $d/FAKE_$n.zip; sleep 1; done'
# force a tiny cap by restarting with PREDPEP_RETENTION_BYTES small (sweep runs on start)
docker rm -f predpep_smoke2 >/dev/null
docker run -d --name predpep_smoke2 -e PREDPEP_CORE_BUDGET=4 -e PREDPEP_RETENTION_BYTES=8000000 -v predpep_data_smoke:/tmp/pepspec -p 6365:6363 predpep:local >/dev/null
sleep 12
curl -fsS --max-time 6 http://localhost:6365/health >/dev/null 2>&1 || { docker restart predpep_smoke2 >/dev/null; sleep 12; }
sleep 5   # let the startup sweep run
echo "remaining FAKE jobs (expect oldest evicted, <= cap): $(docker exec predpep_smoke2 bash -lc 'ls -d /tmp/pepspec/results/FAKE_* 2>/dev/null | wc -l')"
echo "state.disk: $(curl -fsS --max-time 6 http://localhost:6365/state | python3 -c "import sys,json;print(json.load(sys.stdin)['disk'])")"
```
Expected: fewer than 3 FAKE jobs remain (oldest evicted to get under the 8 MB cap); `state.disk.used_bytes` ≤ cap-ish.

- [ ] **Step 6: Restart reconcile — running→failed, queued re-queues**
(Already exercised by the restarts above: any job left `running` when the container was replaced shows `Failed` afterward, and `/state` rebuilt. Confirm no job is stuck:)
```bash
echo "post-restart statuses: $(curl -fsS --max-time 6 http://localhost:6365/jobs | python3 -c "import sys,json;print([(j['job_id'],j['status']) for j in json.load(sys.stdin)['jobs']])")"
docker rm -f predpep_smoke2 >/dev/null; docker volume rm predpep_data_smoke >/dev/null; echo cleaned
```
Expected: no job stuck in a non-terminal state from before the restart (interrupted ones read `Failed`/`Complete`/`Stopped`).

---

### Task 7: Merge + cut over
- [ ] **Step 1: Merge**
```bash
cd /home/david/DATA/OFFLINE/predpep_local
git checkout main && git merge --ff-only feature/partB-scheduler && git branch -d feature/partB-scheduler
test -z "$(git status --porcelain --untracked-files=all)" && echo CLEAN || git status --short
```

- [ ] **Step 2: Cut over (preserve volume, gentle wait)**
```bash
docker rm -f predpep_app >/dev/null; ./scripts/run.sh >/dev/null
sleep 12
curl -fsS --max-time 6 http://localhost:6363/health >/dev/null 2>&1 || { docker restart predpep_app >/dev/null; sleep 12; }
echo "health: $(curl -fsS --max-time 6 http://localhost:6363/health)  docker:$(docker inspect predpep_app --format '{{.State.Health.Status}}')"
echo "state: $(curl -fsS --max-time 6 http://localhost:6363/state)"
echo "jobs: $(curl -fsS --max-time 6 http://localhost:6363/jobs | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d["jobs"]),"jobs")')"
docker images predpep --format '{{.Repository}}:{{.Tag}}' | grep -E 'local|preB' | tr '\n' ' '; echo
```
Expected: healthy; `/state` returns the capacity JSON (core_budget = host cores); existing jobs still listed (the pre-Part-B `SPFGFD` reconciled to `complete`); `predpep:local` + `predpep:preB` present.

> **Rollback:** `docker rm -f predpep_app && docker tag predpep:preB predpep:local && ./scripts/run.sh` (volume retained).

---

## Self-Review
**Spec coverage:** scheduler module/loop/admission/completion (Task1) ✓; submit→enqueue + cpus clamp + `/state` + status from scheduler + stop/delete via scheduler + pid-reuse guard (Task2 + scheduler.kill_job) ✓; preload + per-worker scheduler start (Task3) ✓; env config + optional --memory + autoheal doc (Task4) ✓; disk bar + Queued/Running/Failed + submit placement + poll-on-Queued/Running (Task5) ✓; queue/`/state`/retention/restart verification (Task6) ✓; cutover + preB rollback (Task7) ✓; post-zip cleanup (scheduler `_post_zip_cleanup`) ✓; reconcile orphans→failed (scheduler `_reconcile`) ✓.
**Placeholder scan:** Task5.3 references "around line 175–185" of `tab1_submission.js` (its exact submit-success lines weren't pre-read) but specifies the precise inserted code + anchor (just before the `setInterval(...pollStatus...)`); concrete at execution. No other placeholders.
**Consistency:** `scheduler.enqueue/cancel/kill_job/get_state/start_scheduler/CORE_BUDGET` are defined in Task1 and called exactly so in Task2/3; `job.json.status` lowercase canonical (scheduler) → `_STATUS_DISPLAY` title-case (API) → `cls()` classes (UI) line up; `manager.cmd.json`/`manager.pid`/`STOPPED`/`<job>.zip` filenames consistent; `predpep:preB` rollback + `feature/partB-scheduler` branch consistent.
