"""Standalone audit worker: ``python backend/manage.py run_audit_worker``.

Runs in its own process so web-server restarts never kill in-flight
audits. Each pass atomically claims one queued (or stale) job and runs
the full pipeline; stages heartbeat, so cancel wins promptly and a
killed worker's job is reclaimed through its stale lease on the next
pass. No broker, no threads: one process, one job at a time.

The first SIGINT/SIGTERM finishes the current job, then stops; a
second interrupt abandons the in-flight job immediately. Either way a
killed job stays RUNNING with a dead lease, and the next worker pass
reclaims it and resumes from durable stage results. Exactly-once
billing is NOT claimed: an external call that finished server-side
microseconds before a crash may go unsettled, and a resumed stage
never re-fires a call whose observations already persisted.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import uuid

from django.core.management.base import BaseCommand

from audits import worker

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Poll the audit queue and run claimed jobs until stopped."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Claim and run at most one job, then exit.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=2.0,
            help="Seconds to sleep when the queue is empty (default 2).",
        )
        parser.add_argument(
            "--worker-id",
            default="",
            help="Stable worker id (default: generated per process).",
        )

    def handle(self, *args, **options):
        worker_id = options["worker_id"] or f"w-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        stop = threading.Event()
        signals_seen = 0

        def _request_stop(signum, frame):
            nonlocal signals_seen
            signals_seen += 1
            logger.info("audit worker %s received signal %s", worker_id, signum)
            if signals_seen > 1:
                # Second interrupt: abandon the in-flight job now. It
                # stays RUNNING with a dead lease and is reclaimed (and
                # resumed from durable results) by the next worker pass.
                raise KeyboardInterrupt
            stop.set()

        previous: dict = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, _request_stop)
            except (OSError, ValueError, RuntimeError):
                # Platform without this signal, or no main-thread handler.
                continue

        self.stdout.write(f"audit worker {worker_id} started")
        try:
            if options["once"]:
                self._run_pass(worker_id)
                return
            poll = max(float(options["poll_interval"]), 0.1)
            while not stop.is_set():
                if self._run_pass(worker_id) is None:
                    stop.wait(poll)
        except KeyboardInterrupt:
            # Second interrupt, or a platform where the handler above
            # could not be installed: the in-flight job, if any, keeps
            # its RUNNING state and is reclaimed via its stale lease.
            logger.info("audit worker %s interrupted", worker_id)
        finally:
            for sig, handler in previous.items():
                try:
                    signal.signal(sig, handler)
                except (OSError, ValueError, RuntimeError):
                    continue
        self.stdout.write(f"audit worker {worker_id} stopped")

    def _run_pass(self, worker_id: str):
        job = worker.run_claimed_job(worker_id)
        if job is None:
            logger.debug("audit worker %s idle: no claimable job", worker_id)
        else:
            logger.info(
                "audit worker %s settled job %s as %s",
                worker_id,
                job.pk,
                job.status,
            )
        return job
