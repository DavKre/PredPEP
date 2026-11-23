# app/scheduler.py — in-process CPU-aware job scheduler + disk retention.
# One gevent greenlet in the single gunicorn worker owns all job lifecycle.
# job.json["status"] is the source of truth:
#   queued -> running -> complete|partial|stopped|failed.
#
# 'partial' means: the computation produced usable per-design results, but the final
# aggregated score table (tab2_final_scores.csv) is missing or incomplete. The manager
# records this in final_status.json before zipping; _finalize folds it into job.json.
#
# WIRE COMPATIBILITY: 'partial' is NOT emitted on the API until PREDPEP_PARTIAL_STATUS
# is enabled. The DDN client maps unrecognised /status values to *running* and polls
# without a wall-clock timeout, so emitting a new value before that client ships support
# would make it poll a finished job forever. Until then partial jobs report status
# "Complete" alongside partial=true + failed_stage, which every existing consumer
# ignores safely. See docs/incident-SPILRH_c70d2d4c.md.
import os
import json
import time
import glob
import re
import shutil
import signal
import subprocess
from datetime import datetime, timezone

# Read from the environment so this module and predPEP.py cannot disagree about
# where jobs live: a mismatch makes every job invisible to the web layer while
# the scheduler happily runs it. Defaults are the historical hardcoded paths, so
# behaviour is unchanged when the variables are unset. run_catSPEC.py has its own
# copy of BASE_RESULT_FOLDER and must be kept in step too -- it uses it to build
# pdb_relative_path, which /stream_final_pdb resolves against.
BASE_UPLOAD_FOLDER = os.environ.get('PREDPEP_UPLOAD_DIR', '/tmp/pepspec/uploads')
BASE_RESULT_FOLDER = os.environ.get('PREDPEP_RESULT_DIR', '/tmp/pepspec/results')


# How many cores stay free for everything that is not a pepspec job. Applies
# only when no cgroup quota is set -- see _default_budget below.
CORE_RESERVE = int(os.environ.get('PREDPEP_CORE_RESERVE', '4'))


def _cgroup_quota():
    """Cores this process may use under its cgroup, or None if unlimited.

    `docker run --cpus N` writes a bandwidth limit, not an affinity mask, so
    os.sched_getaffinity() cannot see it -- inside the container it still
    reports every core on the host. The limit has to be read from the cgroup.

    cgroup v2 stores "<quota> <period>" (or "max") in cpu.max; v1 splits it
    across cpu.cfs_quota_us and cpu.cfs_period_us, with -1 meaning unlimited.
    Both are tried, under this process's own cgroup first and the filesystem
    root second, because a container's cgroup namespace makes the root its own
    limit while a systemd service on bare metal has its limit one level down.
    """
    try:
        rel = open('/proc/self/cgroup').read().split('\n')[0].split(':')[-1]
    except (OSError, IndexError):
        rel = '/'
    roots = []
    for r in (os.path.join('/sys/fs/cgroup', rel.lstrip('/')), '/sys/fs/cgroup'):
        if r not in roots:
            roots.append(r)

    for root in roots:
        # cgroup v2
        try:
            quota, period = open(os.path.join(root, 'cpu.max')).read().split()
        except (OSError, ValueError):
            pass
        else:
            if quota != 'max':
                try:
                    cores = int(quota) // int(period)
                except (ValueError, ZeroDivisionError):
                    cores = 0
                if cores >= 1:
                    return cores
            continue  # an explicit "max" here means unlimited, not "look higher"
        # cgroup v1
        try:
            quota = int(open(os.path.join(root, 'cpu.cfs_quota_us')).read().strip())
            period = int(open(os.path.join(root, 'cpu.cfs_period_us')).read().strip())
        except (OSError, ValueError):
            continue
        if quota > 0 and period > 0 and quota // period >= 1:
            return quota // period
    return None


def _detect_cores():
    try:
        return len(os.sched_getaffinity(0))
    except Exception:
        return os.cpu_count() or 4


def _default_budget():
    """Cores predPEP will hand out when PREDPEP_CORE_BUDGET is unset.

    Two cases, deliberately different:

      * A cgroup quota IS an allocation somebody already made for this
        container. `--cpus 20` means twenty, and holding a reserve back from
        it would silently shrink an explicit decision. Use it as given.

      * No quota means predPEP is sharing the machine with the host and
        whatever else runs there, so CORE_RESERVE cores (4 by default) stay
        free. That keeps the dashboard, the healthcheck and the database
        answering while a job has every other core at 100%. On a host running
        an autoheal sidecar this is not cosmetic: a healthcheck that times out
        gets the container restarted, and a restart mid-job costs hours of
        pepspec.

    The quota must never be smaller than the budget. If it is, the scheduler
    admits work the kernel then throttles, and the job runs slower than one
    sized honestly -- with nothing in the dashboard to say why. Whoever sets
    `--cpus` is therefore setting the budget too, which is the point of
    deriving it here instead of carrying a second number in the environment.
    """
    quota = _cgroup_quota()
    if quota:
        return quota
    return max(2, _detect_cores() - CORE_RESERVE)


# An EMPTY value counts as unset. Wrappers pass empty strings through without
# meaning to -- compose writes PREDPEP_CORE_BUDGET: "${PREDPEP_CORE_BUDGET:-}"
# and a shell exports a blank variable just as readily -- and int('') raises at
# import, which would take the whole app down rather than fall back.
_budget_env = os.environ.get('PREDPEP_CORE_BUDGET', '').strip()
if _budget_env:
    try:
        CORE_BUDGET = int(_budget_env)
    except ValueError:
        print('[scheduler] PREDPEP_CORE_BUDGET=%r is not a number; '
              'deriving from the host instead' % _budget_env, flush=True)
        CORE_BUDGET = _default_budget()
else:
    CORE_BUDGET = _default_budget()
RETENTION_BYTES = int(os.environ.get('PREDPEP_RETENTION_BYTES', 50 * 1024 ** 3))
RETENTION_DAYS = int(os.environ.get('PREDPEP_RETENTION_DAYS', 180))

# A pause freezes the manager's process group. It does NOT return the job's cores
# to the node: the reservation stands and the frozen Rosetta keeps its memory, so
# a job left paused holds capacity it is not using. Hence a ceiling -- past it the
# scheduler resumes the job itself rather than letting a forgotten pause hold a
# machine. 0 disables the ceiling.
MAX_PAUSE_SECONDS = int(os.environ.get('PREDPEP_MAX_PAUSE_HOURS', 24)) * 3600

# Where an evicted job is kept after it leaves the results directory.
#
# Set PREDPEP_ARCHIVE_DIR to a mounted path when one is available. Until then
# the default is a local directory, NOT an empty value: an unset archive must
# not mean "delete without keeping a copy". If the archive cannot be written,
# the job is left exactly where it is -- see _archive_job.
ARCHIVE_DIR = (os.environ.get('PREDPEP_ARCHIVE_DIR') or '').strip() \
    or '/var/lib/spacepep-predpep/archive'

# What to carry into the archive. The zip is the complete record of a run; the
# rest are the small files the dashboard reads, so an archived job can still be
# listed and inspected without unpacking anything.
ARCHIVE_KEEP_SUFFIXES = ('.zip', '.all.txt', '.old.txt')
ARCHIVE_KEEP_NAMES = (
    'job.json', 'tab2_final_scores.csv', 'tab2_filtered_scores.csv',
    'filter_report.json', 'final_status.json', 'aggregation_report.json',
    'lineage.json', 'design_spec.json', 'all_model_scores.csv',
    'non_mc_pdbs.csv', 'iteration_metrics.json',
)

# Iteration cap, shown in the Jobs table as "N / MAX_ITERATIONS" and used to
# infer the finish reason (count >= cap -> limit reached, else early stop).
#
# Read from the environment rather than hardcoded in two files; run_iteMAN.py
# reads the same variable and inherits this process's environment, so the
# denominator shown in the Jobs table cannot disagree with the number of
# rounds the manager will actually run.
try:
    MAX_ITERATIONS = max(1, int(os.environ.get('PREDPEP_MAX_ITERATIONS', 6)))
except (TypeError, ValueError):
    MAX_ITERATIONS = 6

TERMINAL = ('complete', 'partial', 'stopped', 'failed')

# Written by run_iteMAN.py before zipping; also travels inside the result zip so the
# failure is diagnosable offline without parsing a traceback out of manager_stderr.log.
FINAL_STATUS_FILE = 'final_status.json'

_STATUS_DISPLAY = {'queued': 'Queued', 'running': 'Running', 'complete': 'Complete',
                   'partial': 'Partial', 'stopped': 'Stopped', 'failed': 'Failed'}

# Result files worth advertising to a client that needs to salvage a partial run.
_ARTIFACT_GLOBS = ('*.all.txt', 'tab2_final_scores.csv', 'non_mc_pdbs.csv',
                   'all_model_scores.csv', 'processed_peptide_scores.csv',
                   'aggregation_report.json', FINAL_STATUS_FILE)


def _emit_partial():
    """Whether the API may emit the 'partial' status value (see module header)."""
    return os.environ.get('PREDPEP_PARTIAL_STATUS', '0').lower() not in ('0', '', 'false', 'no')

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


def _read_final_status(job_id):
    """The manager's own verdict on the final stages, or {} if it never wrote one."""
    try:
        with open(os.path.join(_jdir(job_id), FINAL_STATUS_FILE)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _list_artifacts(job_id):
    """Names of salvageable result files, captured before _post_zip_cleanup removes
    them, so a client knows what the zip contains without downloading it."""
    d = _jdir(job_id)
    names = []
    for pattern in _ARTIFACT_GLOBS:
        for p in sorted(glob.glob(os.path.join(d, pattern))):
            names.append(os.path.basename(p))
    if glob.glob(os.path.join(d, '**', '*_B.fasta'), recursive=True):
        names.append('*_B.fasta')
    if glob.glob(os.path.join(d, '**', '*.spec'), recursive=True):
        names.append('*.spec')
    return names


def _finalize(job_id, status):
    """Mark a job terminal and record progress: stamp completed_at + iterations_done
    once, plus a finish_reason for completed jobs (limit vs early). MUST run before
    _post_zip_cleanup so the iteration dirs are still countable."""
    meta = _read_meta(job_id)
    meta['job_id'] = job_id
    meta['status'] = status
    meta.setdefault('completed_at', _now_iso())
    meta.pop('paused', None)                    # terminal jobs are never paused
    meta.pop('paused_at', None)
    if 'iterations_done' not in meta:
        meta['iterations_done'] = count_iterations(job_id)
    if status == 'complete':
        meta['finish_reason'] = 'limit' if meta.get('iterations_done', 0) >= MAX_ITERATIONS else 'early'
    # Carry the manager's failure reason into job.json so a consumer never has to dig
    # a traceback out of manager_stderr.log inside the result zip.
    final = _read_final_status(job_id)
    if final.get('failed_stage'):
        meta['failed_stage'] = final['failed_stage']
    if final.get('message'):
        meta['message'] = final['message']
    if 'artifacts' not in meta:
        meta['artifacts'] = _list_artifacts(job_id)
    try:
        with open(os.path.join(_jdir(job_id), 'job.json'), 'w') as f:
            json.dump(meta, f)
    except Exception as e:
        # Never silent: a swallowed write here leaves a finished job reading
        # "running" forever, which is exactly what happened to SPILRH_c70d2d4c.
        print('[scheduler] FAILED to finalize %s as %s: %s' % (job_id, status, e), flush=True)


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
    # SIGKILL is not blockable and reaches a SIGSTOPped group too, so cancelling a
    # paused job needs no SIGCONT first.
    return _signal_group(job_id, signal.SIGKILL)


def _signal_group(job_id, sig):
    """Send `sig` to the manager's whole process group, guarding against PID reuse.

    Safe for SIGSTOP/SIGCONT because nothing in the pipeline detaches: every
    subprocess.run and the one Popen in run_iteMAN.py inherit this group, so the
    freeze reaches pepspec, rosetta_scripts and FoldX too rather than leaving
    workers burning CPU under a UI that says "paused".
    """
    try:
        with open(os.path.join(_jdir(job_id), 'manager.pid')) as f:
            pid = int(f.read().strip())
        if _pid_is_manager(pid):
            os.killpg(os.getpgid(pid), sig)
            return True
    except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
        pass
    return False


def _set_paused(job_id, paused):
    meta = _read_meta(job_id)
    meta['job_id'] = job_id
    if paused:
        meta['paused'] = True
        meta['paused_at'] = _now_iso()
    else:
        meta.pop('paused', None)
        meta.pop('paused_at', None)
    try:
        with open(os.path.join(_jdir(job_id), 'job.json'), 'w') as f:
            json.dump(meta, f)
    except Exception:
        pass


def is_paused(job_id):
    return bool(_read_meta(job_id).get('paused'))


def pause_job(job_id):
    """SIGSTOP a running job's process group. Returns (ok, error).

    The flag goes in job.json, not in memory: a gunicorn worker restart must not
    lose track of a frozen process, which would otherwise sit stopped forever
    with nothing recording why.
    """
    if job_id in _queue:
        return False, 'Job is queued, not running; nothing to pause.'
    if not _manager_alive(job_id):
        return False, 'Job is not running.'
    if is_paused(job_id):
        return True, None                      # idempotent
    if not _signal_group(job_id, signal.SIGSTOP):
        return False, 'Could not signal the job.'
    _set_paused(job_id, True)
    return True, None


def resume_job(job_id):
    """SIGCONT a paused job's process group. Returns (ok, error)."""
    if not is_paused(job_id):
        return False, 'Job is not paused.'
    if not _manager_alive(job_id):
        _set_paused(job_id, False)             # died while frozen; reconcile handles it
        return False, 'Job is no longer running.'
    if not _signal_group(job_id, signal.SIGCONT):
        return False, 'Could not signal the job.'
    _set_paused(job_id, False)
    return True, None


def _auto_resume_overdue():
    """Resume anything paused longer than the ceiling.

    Driven from the scheduler tick rather than a threading.Timer, which would die
    with the worker that set it and leave the job frozen indefinitely.
    """
    if MAX_PAUSE_SECONDS <= 0:
        return
    for job_id in list(_running):
        meta = _read_meta(job_id)
        if not meta.get('paused'):
            continue
        try:
            started = datetime.fromisoformat(meta['paused_at']).timestamp()
        except Exception:
            started = 0
        if started and time.time() - started > MAX_PAUSE_SECONDS:
            ok, _ = resume_job(job_id)
            print('[scheduler] auto-resumed %s after %d h paused'
                  % (job_id, MAX_PAUSE_SECONDS // 3600), flush=True)


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
    """Terminal state for a job whose manager is no longer running.

    A zip is proof that the run finished, NOT that it succeeded: the manager kept
    zipping and exiting 0 after a fatal aggregation error. Its own verdict in
    final_status.json wins when present; jobs predating that file fall back to the
    old zip-existence rule.
    """
    d = _jdir(job_id)
    has_zip = os.path.exists(os.path.join(d, '%s.zip' % job_id))
    verdict = _read_final_status(job_id).get('status')
    if verdict in TERMINAL:
        return verdict if verdict != 'complete' or has_zip else 'failed'
    if has_zip:
        return 'complete'
    if os.path.exists(os.path.join(d, 'STOPPED')):
        return 'stopped'
    return 'failed'


# Files the dashboard reads AFTER a job finishes. The old cleanup kept only the
# zip and job.json, which deleted every one of these -- so a job that completed
# cleanly showed "Results not found yet" on the Results page and "unavailable" on
# all five analytics panels, while a job that ended PARTIAL displayed fine
# (cleanup only runs for 'complete'). The zip is still the archive; these are the
# few small files that keep the finished job viewable without unpacking it.
#
# Everything bulky still goes: the .pdbs model directories, the .spec files, the
# per-run logs. On a save_all_pdbs job that is tens of GB reclaimed and a few MB
# kept.
_KEEP_JOB_FILES = {
    'job.json',
    'tab2_final_scores.csv',      # Results table + most analytics
    'tab2_filtered_scores.csv',   # post-run filter subset
    'filter_report.json',         # what was filtered and why
    'all_model_scores.csv',       # analytics: correlation, PCA, logo
    'non_mc_pdbs.csv',            # fallback score source
    'final_status.json',          # the manager's own verdict
    'aggregation_report.json',    # run_catSPEC.py damage report
    'lineage.json',               # provenance chain
    'design_spec.json',           # what was submitted
    'iteration_metrics.json',     # live/iteration progress
    'tab3_ncaa_designs.csv',      # NCAA design pass: the Results table reads it
    'ncaa_design_report.json',    # NCAA design pass: palette, interface, failures
}
# Suffixes kept at job level: the FoldX master tables, whatever the evolved
# PDB_BASE happens to be called.
_KEEP_JOB_SUFFIXES = ('.all.txt', '.old.txt')
# Inside each iteration directory, keep only these small records. The directory
# itself is kept (empty of bulk) so /jobs/<id>/metrics can still find them.
_KEEP_ITER_FILES = {'flags_manifest.json', 'runs_status.json',
                    'foldx_status.json', 'iteration_metrics.json'}


def _post_zip_cleanup(job_id):
    """Reclaim the bulk of a finished job, keeping what the dashboard reads.

    The zip remains the complete archive. See _KEEP_JOB_FILES for why this is a
    selective sweep rather than "keep the zip and nothing else".
    """
    d = _jdir(job_id)
    keep = set(_KEEP_JOB_FILES) | {'%s.zip' % job_id}
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isdir(p):
                # An iteration directory: strip it to its small records rather
                # than removing it, so flags_manifest.json survives.
                if '_iter' in name:
                    _strip_iteration_dir(p)
                elif name == 'top10':
                    # The ten best models, a few MB. Kept because the Results
                    # page streams structures for the viewer, and the model PDBs
                    # it references inside the iteration dirs are being removed
                    # here. Without this, a finished job has a score table and
                    # no viewable structures at all.
                    continue
                elif name == 'ncaa_designs':
                    # The NCAA design pass. Stripped rather than removed,
                    # because tab3_ncaa_designs.csv points the Results viewer
                    # at ncaa_*.pdb inside it by relative path.
                    _strip_ncaa_dir(p)
                else:
                    # DELIBERATELY NOT rmtree, which is what this branch used
                    # to do. Deleting every directory it did not recognise
                    # destroyed the NCAA design pass -- designs, score files
                    # and table -- two minutes after it wrote them, leaving the
                    # job looking as though the pass had never run. A sweep
                    # whose default is to delete the unknown does that to
                    # every future addition, so the default is now to keep.
                    # The bulk is in the iteration dirs, still stripped above.
                    continue
                continue
            if name in keep or name.endswith(_KEEP_JOB_SUFFIXES):
                continue
            try:
                os.remove(p)
            except OSError:
                pass
    except Exception:
        pass
    shutil.rmtree(os.path.join(BASE_UPLOAD_FOLDER, job_id), ignore_errors=True)


# Kept anywhere under ncaa_designs/: the score files the deltas were computed
# from, the generated protocols, and the run logs. All small.
_KEEP_NCAA_SUFFIXES = ('.sc', '.xml', '.log', '.json')
# PDB prefixes that are OUTPUT. Anything else ending .pdb under ncaa_designs/
# is the copy of the input model rosetta_scripts was handed, which already
# exists in top10/ or an iteration dir -- three copies per model, ~830 KB each.
_KEEP_NCAA_PDB_PREFIXES = ('ncaa_', 'ctrl_', 'base_')


def _strip_ncaa_dir(path):
    """Keep every designed structure and every number; drop duplicated inputs.

    Deliberately conservative about what it removes. The control and baseline
    PDBs are kept even though no table references them, because they are the
    structures the reported deltas are differences BETWEEN, and this function
    exists because the previous sweep was too eager.
    """
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                keep = (name.endswith(_KEEP_NCAA_SUFFIXES)
                        or (name.endswith('.pdb')
                            and name.startswith(_KEEP_NCAA_PDB_PREFIXES)))
                if keep:
                    continue
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass
    except Exception:
        pass


def _strip_iteration_dir(path):
    """Delete everything in an iteration dir except its small JSON records."""
    try:
        for name in os.listdir(path):
            if name in _KEEP_ITER_FILES:
                continue
            p = os.path.join(path, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass
    except Exception:
        pass


def _tick():
    # 0) release any pause that has outstayed its ceiling
    try:
        _auto_resume_overdue()
    except Exception as e:
        print('[scheduler] auto-resume error: %s' % e, flush=True)
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


def _archive_job(job_id):
    """Copy a job's records into the archive and VERIFY them. True on success.

    Deletion is gated on this returning True. An archive step that silently
    fails and then deletes is worse than having no retention policy at all, so
    every file is size-checked after the copy and any shortfall aborts the whole
    archive -- the job then stays in the results directory and the next sweep
    tries again.

    The zip is copied rather than moved, so an interruption leaves the original
    intact; the original is removed by the caller only after this returns True.
    """
    src = _jdir(job_id)
    dest = os.path.join(ARCHIVE_DIR, job_id)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        print('[scheduler] archive unavailable (%s): %s — keeping %s'
              % (ARCHIVE_DIR, e, job_id), flush=True)
        return False

    wanted = []
    try:
        for name in os.listdir(src):
            p = os.path.join(src, name)
            if not os.path.isfile(p):
                continue
            if name in ARCHIVE_KEEP_NAMES or name.endswith(ARCHIVE_KEEP_SUFFIXES):
                wanted.append(name)
    except OSError as e:
        print('[scheduler] cannot read %s for archiving: %s' % (job_id, e), flush=True)
        return False

    if not wanted:
        # Nothing worth keeping is not the same as "archive succeeded". Refuse,
        # so a job whose records are already gone is not quietly deleted on the
        # strength of an empty archive.
        print('[scheduler] %s has no archivable records; keeping it' % job_id,
              flush=True)
        return False

    copied = []
    for name in wanted:
        s, d = os.path.join(src, name), os.path.join(dest, name)
        try:
            shutil.copy2(s, d)
            if os.path.getsize(d) != os.path.getsize(s):
                raise OSError('size mismatch after copy')
            copied.append(name)
        except OSError as e:
            print('[scheduler] archive of %s failed on %s: %s — keeping the job'
                  % (job_id, name, e), flush=True)
            return False

    # An index line per archived job, so the archive is readable without
    # listing directories and a future dashboard view can enumerate it.
    try:
        meta = _read_meta(job_id)
        with open(os.path.join(ARCHIVE_DIR, 'archive_index.jsonl'), 'a') as f:
            f.write(json.dumps({
                'job_id': job_id,
                'archived_at': time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()),
                'status': meta.get('status'),
                'completed_at': meta.get('completed_at'),
                'submitted_at': meta.get('submitted_at'),
                'protein_symbol': meta.get('protein_symbol'),
                'files': copied,
                'bytes': sum(os.path.getsize(os.path.join(dest, n)) for n in copied),
            }) + '\n')
    except (OSError, ValueError) as e:
        # The records are safely copied; a missing index line is cosmetic and
        # must not block the eviction.
        print('[scheduler] archived %s but could not write the index: %s'
              % (job_id, e), flush=True)

    print('[scheduler] archived %s -> %s (%d file(s))'
          % (job_id, dest, len(copied)), flush=True)
    return True


def _evict(job_id, reason):
    """Archive, verify, then remove the job from the results directory.

    Returns the bytes reclaimed, or 0 when the job was kept because the archive
    could not be completed.
    """
    d = _jdir(job_id)
    size = _du_bytes(d)
    if not _archive_job(job_id):
        return 0
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(os.path.join(BASE_UPLOAD_FOLDER, job_id), ignore_errors=True)
    print('[scheduler] evicted %s (%s), %.1f MB reclaimed'
          % (job_id, reason, size / (1024 ** 2)), flush=True)
    return size


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
    # Age-based eviction. Archived and verified before removal; a job whose
    # archive fails is left in place and retried on the next sweep.
    for r in rows:
        entry, st, mtime, sz = r
        if st in TERMINAL and (now - mtime) > age_limit and os.path.isdir(_jdir(entry)):
            freed = _evict(entry, 'older than %d days' % RETENTION_DAYS)
            if freed:
                r[3] = 0
                total -= sz

    # Size-based eviction, oldest terminal job first. Same gate.
    if total > RETENTION_BYTES:
        for entry, st, mtime, sz in sorted(
                [r for r in rows if r[1] in TERMINAL], key=lambda x: x[2]):
            if total <= RETENTION_BYTES:
                break
            if os.path.isdir(_jdir(entry)):
                freed = _evict(entry, 'over the %d GB cap'
                               % (RETENTION_BYTES // (1024 ** 3)))
                if freed:
                    total -= sz
                else:
                    # Cannot archive, so cannot evict. Stop rather than walking
                    # the whole list failing on each one; the cause (an
                    # unwritable archive) is the same for all of them.
                    print('[scheduler] size eviction halted: archive '
                          'unavailable, %.1f GB still in use'
                          % (total / (1024 ** 3)), flush=True)
                    break
    _disk_used = max(0, total)


def public_status(job_id):
    """The job's externally visible state — the single place status is shaped for the
    API and the UI.

    Self-heals a job left reading "running" whose manager is gone (SPILRH_c70d2d4c's
    job.json still said "running" 14 h after it ended), and applies the 'partial' wire
    gate described in the module header.
    """
    d = _jdir(job_id)
    raw = _read_meta(job_id).get('status')

    if raw is None:                                  # pre-status job: derive once
        raw = _terminal_for_dead(job_id)
        _set_status(job_id, raw)
    elif raw == 'running' and not _manager_alive(job_id):
        raw = _terminal_for_dead(job_id)
        _finalize(job_id, raw)

    meta = _read_meta(job_id)
    # Reason lives in job.json once finalised; fall back to the manager's own file so
    # the reason survives even if the merge into job.json never happened.
    final = _read_final_status(job_id)
    failed_stage = meta.get('failed_stage') or final.get('failed_stage')
    message = meta.get('message') or final.get('message')
    partial = (raw == 'partial')
    disp = _STATUS_DISPLAY.get(raw, 'Running')
    if partial and not _emit_partial():
        disp = 'Complete'                            # compatible wire value + flags below

    has_zip = os.path.exists(os.path.join(d, '%s.zip' % job_id))
    return {
        'status': disp,
        'partial': partial,
        # A paused job is still Running on the wire. New status STRINGS break the
        # dispatcher (see the module header on 'partial'); an added boolean does
        # not, and a consumer that ignores it simply sees a job taking longer.
        'paused': bool(meta.get('paused')) and raw == 'running',
        'failed_stage': failed_stage,
        'message': message,
        # Recorded at finalize time (before cleanup removes them); scanned live for
        # jobs the manager stamped terminal itself.
        'artifacts': meta.get('artifacts') or _list_artifacts(job_id),
        # Results stay retrievable for every terminal state that produced a zip —
        # a stopped or partial run must not strand the user's own results.
        'download_url': ('/download/%s/%s.zip' % (job_id, job_id)) if has_zip else None,
    }


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
