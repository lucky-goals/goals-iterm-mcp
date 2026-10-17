"""Cancellation-safe, bounded supervisors for SDK read transactions.

The iTerm2 SDK transaction context manager is a useful abstraction for the
normal case, but cancelling the task which owns it can leave the server-side
transaction open.  :class:`TransactionReadSupervisor` therefore owns the
task which enters, uses, and exits a transaction.  A caller may be cancelled
while waiting for that task, but never cancels it directly.

This module deliberately knows nothing about iTerm2.  The caller supplies a
transaction context manager, an async body, and a synchronous callback which
can close the already-captured transport.  The callback is the only escape
hatch used after a timeout or an SDK context-manager failure.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import math
from typing import Any, Awaitable, Callable, Deque, Generic, TypeVar

from .errors import DomainError


T = TypeVar("T")


_DEFAULT_TIMEOUT_SECONDS = 2.0
_DEFAULT_CANCEL_GRACE_SECONDS = 0.1


def _backend_unavailable() -> DomainError:
    """Return the intentionally non-descriptive supervisor failure."""

    return DomainError("BACKEND_UNAVAILABLE")


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve a supervisor-owned task result so no warning is emitted."""

    try:
        task.result()
    except BaseException:
        # The owner has already converted task failures into state.  This
        # callback exists only to retrieve the result from tasks which are
        # allowed to finish after their caller has returned.
        pass


class _GateToken:
    __slots__ = ("_gate",)

    def __init__(self, gate: "_SerialGate") -> None:
        self._gate = gate


class _SerialGate:
    """A one-slot gate whose waiters can be failed without unlocking it."""

    __slots__ = ("_held", "_owner", "_waiters")

    def __init__(self) -> None:
        self._held = False
        self._owner: _GateToken | None = None
        self._waiters: Deque[asyncio.Future[_GateToken]] = deque()

    async def acquire(self) -> _GateToken:
        if not self._held:
            self._held = True
            token = _GateToken(self)
            self._owner = token
            return token

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[_GateToken] = loop.create_future()
        self._waiters.append(waiter)
        try:
            return await waiter
        except BaseException:
            # A cancelled queue waiter must never become the owner later.
            if waiter.done() and not waiter.cancelled():
                try:
                    token = waiter.result()
                except BaseException:
                    pass
                else:
                    # release() may have handed us the token immediately
                    # before cancellation reached this private gate task.
                    # Reclaim it here so a cancelled caller cannot strand the
                    # one-slot gate.
                    self.release(token)
            if not waiter.done():
                waiter.cancel()
            try:
                self._waiters.remove(waiter)
            except ValueError:
                # It may already have been handed a token by release().
                pass
            raise

    def release(self, token: _GateToken) -> None:
        if not self._held or self._owner is not token:
            return

        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.cancelled():
                continue
            if waiter.done():
                # A completed waiter can only be a failed waiter.  Its
                # exception is retrieved by the waiting run task.
                continue
            next_token = _GateToken(self)
            self._owner = next_token
            waiter.set_result(next_token)
            return

        self._owner = None
        self._held = False

    def fail_waiters(self) -> None:
        """Wake every queued caller while leaving the held gate untouched."""

        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.done():
                continue
            waiter.set_exception(_backend_unavailable())


@dataclass(slots=True)
class _RunnerOutcome(Generic[T]):
    status: str
    value: T | None = None
    error: BaseException | None = None


@dataclass(slots=True)
class _RunRecord(Generic[T]):
    transaction: Any
    body: Callable[[Any], Awaitable[T]]
    abort_owned: Callable[[], bool]
    token: _GateToken
    wakeup: asyncio.Future[None]
    runner_task: asyncio.Task[_RunnerOutcome[T]]
    outcome: _RunnerOutcome[T] | None = None
    abort_called: bool = False
    abort_confirmed: bool = False
    force_cancel: bool = False
    close_requested: bool = False
    timeout_requested: bool = False
    shutdown_task: asyncio.Task[bool] | None = None
    caller_cancelled: bool = False


class TransactionReadSupervisor(Generic[T]):
    """Serialize and bound SDK read transactions.

    ``run`` owns the transaction runner task.  A caller cancellation is
    remembered and is delivered only after the runner has either completed or
    the bounded timeout/transport-abort path has finished.  ``abort_owned``
    must be a trusted synchronous callback for the exact connection captured
    by the caller; a non-``True`` return value is treated as an unconfirmed
    abort.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        cancel_grace_seconds: float = _DEFAULT_CANCEL_GRACE_SECONDS,
    ) -> None:
        self._timeout_seconds = self._validate_budget(
            timeout_seconds, _DEFAULT_TIMEOUT_SECONDS, "timeout_seconds"
        )
        self._cancel_grace_seconds = self._validate_budget(
            cancel_grace_seconds, _DEFAULT_CANCEL_GRACE_SECONDS, "cancel_grace_seconds"
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate = _SerialGate()
        self._active: _RunRecord[Any] | None = None
        self._closed = False
        self._poisoned = False

    @staticmethod
    def _validate_budget(value: float, maximum: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a positive finite number")
        value = float(value)
        if not math.isfinite(value) or value <= 0 or value > maximum:
            raise ValueError(f"{name} must be a positive finite number no greater than {maximum}")
        return value

    @property
    def active_read_count(self) -> int:
        """Number of SDK runner transactions which have not actually ended."""

        return int(self._active is not None)

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError("transaction supervisor requires a running event loop") from exc
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("transaction supervisor cannot be used across event loops")
        return loop

    def _set_wakeup(self, record: _RunRecord[Any]) -> None:
        if not record.wakeup.done():
            record.wakeup.set_result(None)

    def _mark_poisoned(self) -> None:
        self._poisoned = True
        self._gate.fail_waiters()

    @staticmethod
    def _invoke_abort(record: _RunRecord[Any]) -> bool:
        if record.abort_called:
            return record.abort_confirmed
        record.abort_called = True
        try:
            result = record.abort_owned()
        except BaseException:
            result = False
        record.abort_confirmed = result is True
        return record.abort_confirmed

    def _start_shutdown(self, record: _RunRecord[Any]) -> asyncio.Task[bool] | None:
        if not record.abort_confirmed:
            return None
        task = record.shutdown_task
        if task is not None:
            return task
        record.force_cancel = True
        task = asyncio.create_task(self._cancel_runner_with_grace(record))
        record.shutdown_task = task
        task.add_done_callback(_consume_task_result)
        return task

    async def _cancel_runner_with_grace(self, record: _RunRecord[Any]) -> bool:
        runner = record.runner_task
        if not runner.done():
            # This is the only place in the module which cancels an SDK
            # runner, and it is reached only after abort_owned returned True.
            runner.cancel()

        deadline = asyncio.get_running_loop().time() + self._cancel_grace_seconds
        while not runner.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({runner}, timeout=remaining)
            except asyncio.CancelledError:
                # This task is supervisor-owned.  A caller's cancellation
                # must not cancel its bounded cleanup operation.
                continue
        return runner.done()

    async def _await_shutdown(self, record: _RunRecord[Any]) -> bool:
        task = self._start_shutdown(record)
        if task is None:
            return False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # The outer run records this cancellation separately; keep
                # waiting for the fixed grace budget here.
                record.caller_cancelled = True
                continue
        return task.result()

    async def _run_sdk(
        self, record: _RunRecord[T]
    ) -> _RunnerOutcome[T]:
        """Execute entry, body, and exit without leaking raw exceptions."""

        try:
            try:
                # iTerm2 2.30's Transaction.__aenter__ returns None.  The
                # transaction object itself is the raw transaction API used
                # by the read body; entry is only the begin/ACK handshake.
                await record.transaction.__aenter__()
            except asyncio.CancelledError as exc:
                if record.force_cancel:
                    return _RunnerOutcome("cancelled", error=exc)
                return _RunnerOutcome("entry_error", error=exc)
            except BaseException as exc:
                return _RunnerOutcome("entry_error", error=exc)

            body_error: BaseException | None = None
            body_value: T | None = None
            body_traceback = None
            try:
                body_value = await record.body(record.transaction)
            except asyncio.CancelledError as exc:
                if record.force_cancel:
                    return _RunnerOutcome("cancelled", error=exc)
                body_error = exc
                body_traceback = exc.__traceback__
            except BaseException as exc:
                body_error = exc
                body_traceback = exc.__traceback__

            try:
                await record.transaction.__aexit__(
                    type(body_error) if body_error is not None else None,
                    body_error,
                    body_traceback,
                )
            except asyncio.CancelledError as exc:
                if record.force_cancel:
                    return _RunnerOutcome("cancelled", error=exc)
                return _RunnerOutcome("exit_error", error=exc)
            except BaseException as exc:
                return _RunnerOutcome("exit_error", error=exc)

            if body_error is not None:
                return _RunnerOutcome("body_error", error=body_error)
            return _RunnerOutcome("success", value=body_value)
        except asyncio.CancelledError as exc:
            # Covers cancellation at an await introduced by an unusual
            # context-manager implementation.  It is safe to classify as a
            # runner cancellation because force_cancel is set before every
            # supervisor-owned cancellation.
            return _RunnerOutcome("cancelled", error=exc)
        except BaseException as exc:
            # Keep the task result retrievable even for an unexpected SDK
            # implementation failure.
            return _RunnerOutcome("fatal", error=exc)

    def _runner_done(self, record: _RunRecord[Any], task: asyncio.Task[Any]) -> None:
        try:
            try:
                outcome = task.result()
            except BaseException as exc:
                outcome = _RunnerOutcome("fatal", error=exc)
            if not isinstance(outcome, _RunnerOutcome):
                outcome = _RunnerOutcome("fatal", error=RuntimeError("invalid transaction runner result"))
            record.outcome = outcome

            if outcome.status in {"entry_error", "exit_error", "fatal"}:
                self._mark_poisoned()
                self._invoke_abort(record)
            elif outcome.status == "cancelled" and not record.close_requested:
                # A runner cancellation not initiated by close/timeout is an
                # indeterminate SDK transaction as well.
                if not record.timeout_requested:
                    self._mark_poisoned()
                    self._invoke_abort(record)
        except BaseException:
            # A done callback must never become an event-loop exception.  If
            # state handling itself fails, leave the supervisor poisoned and
            # let the active call report the generic backend failure.
            self._mark_poisoned()
            self._invoke_abort(record)
            if record.outcome is None:
                record.outcome = _RunnerOutcome("fatal")
        finally:
            self._set_wakeup(record)
            if self._active is record:
                self._active = None
            self._gate.release(record.token)

    async def _acquire_gate(self) -> _GateToken:
        # A failed/closed supervisor rejects before even creating a queue
        # waiter, which makes poison and close immediate for new calls.
        if self._closed or self._poisoned:
            raise _backend_unavailable()
        return await self._gate.acquire()

    async def run(
        self,
        transaction: Any,
        body: Callable[[Any], Awaitable[T]],
        abort_owned: Callable[[], bool],
    ) -> T:
        """Run one transaction body with bounded cancellation cleanup."""

        loop = self._check_loop()
        if not callable(body) or not callable(abort_owned):
            raise TypeError("body and abort_owned must be callable")
        if self._closed or self._poisoned:
            raise _backend_unavailable()

        # The gate acquisition is intentionally separate from the SDK runner:
        # cancelling a queued caller cancels only this private waiter and
        # therefore never starts SDK entry.
        gate_task = asyncio.create_task(self._acquire_gate())
        try:
            try:
                token = await asyncio.shield(gate_task)
            except asyncio.CancelledError:
                gate_task.cancel()
                try:
                    token = await asyncio.shield(gate_task)
                except BaseException:
                    pass
                else:
                    # Cancellation can race with a successful acquire.  If
                    # the private gate task already owns a token, release it
                    # before returning the caller's cancellation.
                    self._gate.release(token)
                raise
        except BaseException:
            # Retrieve any gate-task failure.  The awaited path normally did
            # this already; this is defensive for a cancellation race.
            if not gate_task.done():
                gate_task.cancel()
            if gate_task.done():
                try:
                    gate_task.result()
                except BaseException:
                    pass
            raise

        # Poison/close can race with the queued task being handed the token.
        # Do not enter a transaction in that case.
        if self._closed or self._poisoned:
            self._gate.release(token)
            raise _backend_unavailable()

        wakeup: asyncio.Future[None] = loop.create_future()
        # The record must exist before create_task so a synchronous close on
        # the same loop can see and abort it at the next scheduling point.
        placeholder: asyncio.Task[_RunnerOutcome[T]] | None = None
        record = _RunRecord(
            transaction=transaction,
            body=body,
            abort_owned=abort_owned,
            token=token,
            wakeup=wakeup,
            runner_task=None,  # type: ignore[arg-type]
        )
        self._active = record
        placeholder = asyncio.create_task(self._run_sdk(record))
        record.runner_task = placeholder
        placeholder.add_done_callback(lambda task: self._runner_done(record, task))

        caller_cancelled = False
        deadline = loop.time() + self._timeout_seconds

        try:
            while not placeholder.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    # The runner may have completed between the deadline
                    # calculation and this branch; let its outcome win.
                    if placeholder.done():
                        break
                    record.timeout_requested = True
                    self._mark_poisoned()
                    confirmed = self._invoke_abort(record)
                    if confirmed:
                        await self._await_shutdown(record)
                    if caller_cancelled or record.caller_cancelled:
                        raise asyncio.CancelledError
                    raise _backend_unavailable()

                try:
                    done, _ = await asyncio.wait(
                        {placeholder, wakeup},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    # Never propagate the caller cancellation through the
                    # SDK task.  Re-enter the wait loop and remember it.
                    caller_cancelled = True
                    record.caller_cancelled = True
                    continue

                if placeholder in done:
                    break
                if wakeup in done:
                    # close() sets close_requested and calls abort_owned;
                    # callback-level entry/exit failures use the same wakeup
                    # only after the runner is already complete.
                    if record.close_requested:
                        if record.abort_confirmed:
                            await self._await_shutdown(record)
                        if caller_cancelled or record.caller_cancelled:
                            raise asyncio.CancelledError
                        raise _backend_unavailable()
                    # A spurious wakeup is harmless; recompute the deadline.
        except asyncio.CancelledError:
            # This branch can only be reached by an explicit cancellation
            # raised after cleanup.  Preserve Python's cancellation contract.
            raise

        outcome = record.outcome
        if outcome is None:
            # The done callback runs in the same loop turn, but a zero-delay
            # callback race can leave it pending for one scheduling point.
            await asyncio.sleep(0)
            outcome = record.outcome
        if outcome is None:
            self._mark_poisoned()
            self._invoke_abort(record)
            if caller_cancelled or record.caller_cancelled:
                raise asyncio.CancelledError
            raise _backend_unavailable()

        # close() is a terminal supervisor operation.  Even if the runner
        # happened to finish successfully in the same loop turn as close,
        # the close caller must not observe that result as a read result.
        if record.close_requested:
            if caller_cancelled or record.caller_cancelled:
                raise asyncio.CancelledError
            raise _backend_unavailable()

        if caller_cancelled or record.caller_cancelled:
            raise asyncio.CancelledError
        if outcome.status == "success":
            return outcome.value  # type: ignore[return-value]
        if outcome.status == "body_error":
            error = outcome.error
            if isinstance(error, DomainError):
                raise error
            raise _backend_unavailable()
        # Entry, exit, fatal, and runner-cancellation errors never expose raw
        # SDK/body exception text.
        raise _backend_unavailable()

    def close(self) -> None:
        """Permanently stop new runs and request bounded active cleanup.

        ``close`` is synchronous by design.  It invokes the trusted abort
        callback at most once and wakes the active ``run`` caller.  It never
        invokes ``__aexit__`` or treats close as an SDK end acknowledgement.
        """

        if self._loop is not None:
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not None and current_loop is not self._loop:
                raise RuntimeError("transaction supervisor cannot be closed from another event loop")

        if self._closed:
            return
        self._closed = True
        self._gate.fail_waiters()
        record = self._active
        if record is None:
            return

        record.close_requested = True
        confirmed = self._invoke_abort(record)
        self._set_wakeup(record)

        # A normal close call is synchronous, so schedule the bounded runner
        # cancellation in the supervisor's own loop.  The active run, if it
        # still exists, awaits the same task; if its caller disappeared, this
        # fallback keeps cleanup progressing without pretending the SDK end
        # handshake completed.
        if confirmed and self._loop is not None:
            def schedule() -> None:
                self._start_shutdown(record)

            try:
                self._loop.call_soon_threadsafe(schedule)
            except RuntimeError:
                # The loop is already closed; the native runner's actual
                # lifetime remains represented by the active record.
                pass


__all__ = ["TransactionReadSupervisor"]
