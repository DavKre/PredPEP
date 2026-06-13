# Auto-loaded by gunicorn from the WORKDIR (/opt/sp-predPEP).
# preload_app imports the Flask app in the master BEFORE the gevent worker forks,
# which avoids the intermittent fork-time worker wedge. The scheduler is started
# per-worker (post_worker_init) so it runs in the worker, not the preload master.
preload_app = True


def post_worker_init(worker):
    try:
        import predPEP  # noqa: F401
        predPEP.scheduler.start_scheduler()
    except Exception as e:
        worker.log.warning("scheduler start failed in post_worker_init: %s" % e)
