"""Audit worker passes: claim one job, run it, handle halts.

No broker and no threads: the standalone ``run_audit_worker``
management command calls :func:`run_claimed_job` in its own process,
so web-server restarts never kill in-flight audits. The atomic claim
guarantees a job runs at most once even if two worker processes race;
a job left RUNNING by a killed worker is reclaimed through its stale
lease on the next pass and resumed from durable stage results.
"""

from __future__ import annotations

import logging
import threading

from django.db import close_old_connections

from providers.base import ProviderError

from . import pipeline, services
from .models import AuditJob

logger = logging.getLogger(__name__)


def _close_worker_connection() -> None:
    """Release this pass's DB handle when it is safe to do so.

    The standalone command runs passes on its main thread and reuses
    one connection across polls (fresh queries every pass, no held
    transaction), so there is nothing to close there; closing the main
    thread's handle would also break test transactions. Only a
    non-main caller's handle is released here.
    """
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
        _close_worker_connection()
        return None
    if job is None:
        _close_worker_connection()
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
        _close_worker_connection()
    job.refresh_from_db()
    return job
