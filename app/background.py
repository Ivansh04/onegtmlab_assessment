import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


def enqueue(
    tasks: set[asyncio.Task[Any]],
    operation: Callable[..., Awaitable[Any]],
    *args: Any,
) -> None:
    task = asyncio.create_task(operation(*args))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    

    def report_failure(done: asyncio.Task[Any]) -> None:
        if not done.cancelled() and done.exception() is not None:
            logger.error(
                "Background enrichment failed: %s",
                done.exception(),
                exc_info=(
                    type(done.exception()),
                    done.exception(),
                    done.exception().__traceback__,
                ),
            )

    task.add_done_callback(report_failure)
