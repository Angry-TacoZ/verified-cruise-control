"""In-process audit worker: claim one job, run it, handle halts.

No broker: the queue endpoint spawns a daemon thread per request, and the
atomic claim guarantees a job runs at most once even if threads race.
Threads are daemonic so backend shutdown never hangs; a job left RUNNING
by a restart is reclaimed through the stale lease on the next claim.
"""

from __future__ import annotations

import logging
import threading
import uuid

from django.db import close_old_connections

from providers.base import ProviderError

from . import pipeline, services
from .models import AuditJob

logger = logging.getLogger(__name__)


def _close_thread_connection() -> None:
    """Release this thread's DB handle. Spawned worker threads only: the
    main thread's connection belongs to its caller (test transaction,
    request handler, management command) and must not be closed here."""
    if threading.current_thread() is not threading.main_thread():
        close_old_connections()


def run_claimed_job(
    worker_id: str, muse_adapter=None, sam_adapter=None
) -> AuditJob | None:
    """Claim one job and run the full pipeline. Returns the settled job,
    or None when no job was claimable. Expected halts (budget pause,
    cancel, provider failure) are already recorded with exact state; only
    unexpected crashes fail the job here."""
    try:
        job = services.claim_job(worker_id)
    except Exception:
        logger.exception("audit worker %s failed to claim", worker_id)
        _close_thread_connection()
        return None
    if job is None:
        _close_thread_connection()
        return None
    try:
        pipeline.run_stages(job.pk, worker_id, muse_adapter, sam_adapter)
    except (
        services.BudgetExceededError,
        services.JobNotRunningError,
        ProviderError,
    ) as exc:
        logger.info("audit job %s halted: %s", job.pk, exc)
    except Exception as exc:
        logger.exception("audit job %s crashed", job.pk)
        services.fail_job(job.pk, worker_id, "worker_crash", str(exc)[:500])
    finally:
        _close_thread_connection()
    job.refresh_from_db()
    return job


def spawn_worker(muse_adapter=None, sam_adapter=None) -> str:
    """Run one claim-and-run pass in a daemon thread. Returns worker_id."""
    worker_id = f"w-{uuid.uuid4().hex[:12]}"
    thread = threading.Thread(
        target=run_claimed_job,
        args=(worker_id, muse_adapter, sam_adapter),
        daemon=True,
        name=f"audit-{worker_id}",
    )
    thread.start()
    return worker_id
