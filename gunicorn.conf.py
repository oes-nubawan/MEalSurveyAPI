"""
Gunicorn configuration — ensure APScheduler runs in ONLY ONE worker.

With multiple gunicorn workers (Render default = multi-worker),
each worker would start its own scheduler, causing duplicate sends.

Solution: Only the "primary" worker (nr=0) runs the scheduler.
All other workers have their scheduler jobs removed.

Usage: gunicorn -c gunicorn.conf.py app:app
"""

import os


def post_worker_init(worker):
    """Called after each worker initializes. Remove scheduler from non-primary workers."""
    if worker.nr != 0:
        try:
            from app import scheduler
            if scheduler.running:
                scheduler.shutdown(wait=False)
                print(f"[gunicorn] Worker {worker.nr}: Scheduler SHUTDOWN (only worker 0 runs it)")
        except Exception as e:
            print(f"[gunicorn] Worker {worker.nr}: Scheduler shutdown error: {e}")
