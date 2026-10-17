"""Small primitives for bounded asynchronous shutdown cleanup.

The shutdown path deliberately does not use :func:`asyncio.wait_for`.  A
``wait_for`` timeout cancels the wrapped coroutine and then waits for that
coroutine to acknowledge cancellation, which makes an uncooperative cleanup
operation able to extend process shutdown indefinitely.  This module only
uses :func:`asyncio.wait`; when the deadline is reached it returns the still
pending tasks to the owner.  Owners may request cancellation for tasks that
are known to be cleanup-only, but they must not await that cancellation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import math
from collections.abc import Awaitable, Iterable
from typing import Any


TaskLike = asyncio.Future[Any] | Awaitable[Any]


def _consume_task_exception(task: asyncio.Future[Any]) -> None:
    """Retrieve a completed task exception without changing its outcome.

    A detached cleanup task must not produce a ``Task exception was never
    retrieved`` warning after the loop moves on.  ``CancelledError`` is also
    retrieved here, while preserving cancellation as the task's actual
    result.  This callback intentionally never raises into the event loop.
    """

    if not task.done():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


@dataclass(frozen=True, slots=True)
class BoundedWaitResult:
    """The outcome of one bounded wait.

    ``pending`` means that the operation was still running when the deadline
    was reached.  It remains a live task reference so its eventual completion
    can still be observed by the owning component, but its presence is never
    interpreted as successful cleanup.  ``done`` includes cancelled and
    failed tasks; all of their exceptions have been consumed by a callback.
    """

    done: frozenset[asyncio.Future[Any]]
    pending: frozenset[asyncio.Future[Any]]
    deadline: float | None = None

    @property
    def timed_out(self) -> bool:
        return bool(self.pending)

    @property
    def detached(self) -> bool:
        """Whether any cleanup was detached at the deadline."""

        return bool(self.pending)

    @property
    def complete(self) -> bool:
        return not self.pending

    def merge(self, *others: "BoundedWaitResult") -> "BoundedWaitResult":
        """Return one aggregate result while preserving detached tasks."""

        done = set(self.done)
        pending = set(self.pending)
        deadline = self.deadline
        for other in others:
            done.update(other.done)
            pending.update(other.pending)
            if deadline is None:
                deadline = other.deadline
        # A task cannot be both done and pending, but filtering here also
        # makes aggregation robust when a detached task finishes between two
        # cleanup phases.
        done.difference_update(pending)
        return BoundedWaitResult(frozenset(done), frozenset(pending), deadline)


def validate_timeout(value: float) -> float:
    """Validate and normalize a public shutdown timeout."""

    if isinstance(value, bool):
        raise ValueError("timeout must be a finite non-negative number")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout must be a finite non-negative number") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("timeout must be a finite non-negative number")
    return normalized


def _as_future(value: TaskLike) -> asyncio.Future[Any]:
    if isinstance(value, asyncio.Future):
        return value
    if not inspect.isawaitable(value):
        raise TypeError("bounded wait expects awaitables")
    # ensure_future accepts coroutine objects and third-party awaitables while
    # preserving an existing Task/Future supplied by a caller.
    return asyncio.ensure_future(value)


async def wait_bounded(
    awaitables: Iterable[TaskLike],
    timeout: float | None = None,
    *,
    deadline: float | None = None,
    cancel_pending: bool = False,
) -> BoundedWaitResult:
    """Wait for cleanup tasks until one monotonic deadline.

    ``timeout`` is relative to the current event-loop clock.  ``deadline``
    may be supplied when several cleanup groups share one shutdown budget;
    when both are supplied the earlier instant wins.  The returned ``pending``
    set is never awaited after the deadline.  ``cancel_pending=True`` only
    requests cancellation from those pending cleanup tasks and returns them
    immediately; it is deliberately opt-in so callers cannot accidentally
    cancel live domain work.
    """

    if timeout is None and deadline is None:
        raise ValueError("bounded wait requires timeout or deadline")
    loop = asyncio.get_running_loop()
    absolute: float | None = None
    if timeout is not None:
        absolute = loop.time() + validate_timeout(timeout)
    if deadline is not None:
        if isinstance(deadline, bool):
            raise ValueError("deadline must be a finite non-negative number")
        try:
            deadline_value = float(deadline)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("deadline must be a finite non-negative number") from exc
        if not math.isfinite(deadline_value) or deadline_value < 0:
            raise ValueError("deadline must be a finite non-negative number")
        absolute = deadline_value if absolute is None else min(absolute, deadline_value)

    tasks = {_as_future(value) for value in awaitables}
    for task in tasks:
        # Install the callback before waiting.  If the task was already done,
        # consume immediately too; add_done_callback schedules asynchronously.
        task.add_done_callback(_consume_task_exception)
        if task.done():
            _consume_task_exception(task)
    if not tasks:
        return BoundedWaitResult(frozenset(), frozenset(), absolute)

    remaining = max(0.0, absolute - loop.time()) if absolute is not None else 0.0
    done, pending = await asyncio.wait(tasks, timeout=remaining)
    # asyncio.wait may return a task in ``pending`` just as it finishes.  Keep
    # the set returned by asyncio as the evidence at the deadline; this is the
    # conservative result needed by callers deciding whether to detach.
    if cancel_pending:
        for task in pending:
            if not task.done():
                task.cancel()
    return BoundedWaitResult(frozenset(done), frozenset(pending), absolute)


def merge_wait_results(*results: BoundedWaitResult) -> BoundedWaitResult:
    """Aggregate results from sequential cleanup phases."""

    if not results:
        return BoundedWaitResult(frozenset(), frozenset())
    aggregate = results[0]
    return aggregate.merge(*results[1:])


__all__ = [
    "BoundedWaitResult",
    "TaskLike",
    "merge_wait_results",
    "validate_timeout",
    "wait_bounded",
]
