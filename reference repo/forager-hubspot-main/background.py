"""
background.py
-------------
A tiny in-process work queue + daemon worker thread so HubSpot webhooks can be ACKed
immediately while the slow enrichment pipeline (Forager + LLM scoring + contact
creation) runs in the background.

Why: without this, the webhook request runs the whole pipeline synchronously. The
LLM logo step alone (web search) can take tens of seconds and blow past gunicorn's
worker timeout — HubSpot then sees no 2xx, assumes delivery failed, and RETRIES,
re-spending Forager credits. Returning 200 right away removes that retry loop.

Scope / limits: the queue is in-memory and per-process. If the process restarts
(deploy, crash) any not-yet-processed items are lost — that company simply won't be
enriched and can be re-triggered manually via /enrich/company. For the current volume
this is the right trade-off; a durable queue (Redis/RQ) is the upgrade path.
"""

import logging
import queue
import threading

logger = logging.getLogger(__name__)

_queue: "queue.Queue" = queue.Queue()
_started = False
_lock = threading.Lock()


def enqueue(func, *args, **kwargs) -> None:
    """Schedule func(*args, **kwargs) to run on the background worker."""
    _queue.put((func, args, kwargs))


def queue_size() -> int:
    return _queue.qsize()


def _run() -> None:
    while True:
        func, args, kwargs = _queue.get()
        try:
            func(*args, **kwargs)
        except Exception:  # noqa: BLE001 - a failed task must never kill the worker
            logger.exception("Background task %s failed", getattr(func, "__name__", func))
        finally:
            _queue.task_done()


def start_worker() -> None:
    """Start the single daemon worker thread (idempotent, one per process)."""
    global _started
    with _lock:
        if _started:
            return
        threading.Thread(target=_run, name="enrichment-worker", daemon=True).start()
        _started = True
        logger.info("Background enrichment worker started")
