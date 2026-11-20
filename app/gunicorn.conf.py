"""gunicorn configuration for predPEP.

TWO SETTINGS HERE ARE CORRECTNESS, NOT TUNING.

1. workers = 1.

   scheduler.py keeps the queue, the running set and the reserved-core count in
   module-level state inside the worker process. Two workers means two
   schedulers, each believing it owns the whole core budget: both admit jobs up
   to CORE_BUDGET, oversubscribing the machine, and /state answers from
   whichever one received the request. There is no shared store to fix that
   with -- multi-worker needs the queue moved out of process memory first.

2. worker_class = 'gevent'.

   scheduler.start_scheduler() runs its tick loop as a greenlet:

       gevent.spawn(_loop)      # and _loop ends with gevent.sleep(3)

   A greenlet only advances when something yields to the gevent hub. Under the
   'gthread' worker this file used to specify, nothing does -- so the app came
   up, answered /state with plausible JSON, and never ran a single job. It did
   not even log a problem after the first request, because start_scheduler sets
   its _started flag before the import and never retries.

   The gevent worker also monkey-patches the standard library, which is what
   lets the scheduler's blocking calls cooperate instead of stalling the hub.
"""

import os

bind = os.environ.get('PREDPEP_BIND', '0.0.0.0:8383')

workers = 1                     # see above -- do not raise
worker_class = 'gevent'         # see above -- the scheduler is a greenlet

# Concurrency comes from greenlets, not threads; `threads` is ignored by this
# worker class, so it is deliberately not set.
worker_connections = int(os.environ.get('PREDPEP_WORKER_CONNECTIONS', '200'))

# preload_app would import the app in the master and fork; with one worker it
# buys nothing, and it would start the scheduler greenlet in a process that
# does not survive a worker restart.
preload_app = False

# The analytics bundle is a couple of seconds of pandas on a large job, and
# /results_data streams score tables. The 30s default is too tight.
timeout = 300
graceful_timeout = 30

# A worker restart drops the in-memory queue, so never recycle on request
# count. The scheduler re-adopts running jobs on startup, but queued ones are
# requeued and in-flight work is briefly invisible.
max_requests = 0

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('PREDPEP_LOGLEVEL', 'info')
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'
