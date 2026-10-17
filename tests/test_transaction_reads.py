"""Pure-memory tests for the cancellation-safe transaction read supervisor."""

from __future__ import annotations

import asyncio
import math
import unittest

from cc_iterm_mcp.errors import DomainError
from cc_iterm_mcp.transaction_reads import TransactionReadSupervisor, _SerialGate


class FakeTransaction:
    def __init__(
        self,
        *,
        enter_release: asyncio.Event | None = None,
        exit_release: asyncio.Event | None = None,
        enter_error: BaseException | None = None,
        exit_error: BaseException | None = None,
        ignore_cancel: bool = False,
    ) -> None:
        self.enter_release = enter_release
        self.exit_release = exit_release
        self.enter_error = enter_error
        self.exit_error = exit_error
        self.ignore_cancel = ignore_cancel
        self.enter_started = asyncio.Event()
        self.entered = asyncio.Event()
        self.exit_started = asyncio.Event()
        self.enters = 0
        self.exits = 0
        self.events: list[str] = []

    async def __aenter__(self) -> "FakeTransaction":
        self.enters += 1
        self.events.append("enter")
        self.enter_started.set()
        if self.enter_release is not None:
            while not self.enter_release.is_set():
                try:
                    await self.enter_release.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancel:
                        raise
        if self.enter_error is not None:
            raise self.enter_error
        self.entered.set()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.exits += 1
        self.events.append("exit")
        self.exit_started.set()
        if self.exit_release is not None:
            while not self.exit_release.is_set():
                try:
                    await self.exit_release.wait()
                except asyncio.CancelledError:
                    if not self.ignore_cancel:
                        raise
        if self.exit_error is not None:
            raise self.exit_error


async def _yield(value: object) -> object:
    return value


class TransactionReadSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_body_domain_error_are_balanced(self) -> None:
        supervisor = TransactionReadSupervisor(timeout_seconds=0.5)
        transaction = FakeTransaction()
        aborts: list[bool] = []

        result = await supervisor.run(
            transaction,
            lambda raw: _yield(raw),
            lambda: aborts.append(True) or True,
        )
        self.assertIs(result, transaction)
        self.assertEqual(transaction.events, ["enter", "exit"])
        self.assertEqual(aborts, [])
        self.assertFalse(supervisor.poisoned)

        domain_error = DomainError("ITEM_TOO_LARGE", "domain detail")
        second = FakeTransaction()

        async def failing_body(_: object) -> object:
            raise domain_error

        with self.assertRaises(DomainError) as raised:
            await supervisor.run(second, failing_body, lambda: aborts.append(True) or True)
        self.assertIs(raised.exception, domain_error)
        self.assertEqual(second.exits, 1)
        self.assertEqual(aborts, [])
        self.assertFalse(supervisor.poisoned)

    async def test_cancellation_during_entry_still_sends_one_end(self) -> None:
        entered_release = asyncio.Event()
        transaction = FakeTransaction(enter_release=entered_release)
        supervisor = TransactionReadSupervisor(timeout_seconds=0.5)
        call = asyncio.create_task(
            supervisor.run(transaction, lambda _: _yield("read"), lambda: True)
        )
        await transaction.enter_started.wait()
        call.cancel()
        await asyncio.sleep(0)
        self.assertFalse(call.done())
        self.assertEqual(transaction.exits, 0)

        entered_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertEqual(transaction.enters, 1)
        self.assertEqual(transaction.exits, 1)
        self.assertEqual(supervisor.active_read_count, 0)
        self.assertFalse(supervisor.poisoned)

    async def test_repeated_cancellation_waits_for_exit_and_gate(self) -> None:
        exit_release = asyncio.Event()
        first = FakeTransaction(exit_release=exit_release)
        second = FakeTransaction()
        supervisor = TransactionReadSupervisor(timeout_seconds=0.5)
        first_call = asyncio.create_task(
            supervisor.run(first, lambda _: _yield("first"), lambda: True)
        )
        await first.exit_started.wait()
        first_call.cancel()
        first_call.cancel()
        await asyncio.sleep(0)
        second_call = asyncio.create_task(
            supervisor.run(second, lambda _: _yield("second"), lambda: True)
        )
        await asyncio.sleep(0.01)
        self.assertEqual(second.enters, 0)

        exit_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first_call
        self.assertEqual(await second_call, "second")
        self.assertEqual(first.exits, 1)

    async def test_entry_and_exit_failures_poison_and_abort_once(self) -> None:
        for transaction in (
            FakeTransaction(enter_error=RuntimeError("private-entry-detail")),
            FakeTransaction(exit_error=RuntimeError("private-exit-detail")),
        ):
            supervisor = TransactionReadSupervisor(timeout_seconds=0.5)
            aborts: list[int] = []
            with self.assertRaises(DomainError) as raised:
                await supervisor.run(
                    transaction,
                    lambda _: _yield("result"),
                    lambda: aborts.append(1) or True,
                )
            self.assertEqual(raised.exception.code, "BACKEND_UNAVAILABLE")
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(aborts, [1])
            self.assertTrue(supervisor.poisoned)

            unused = FakeTransaction()
            with self.assertRaises(DomainError) as unavailable:
                await supervisor.run(unused, lambda _: _yield("unused"), lambda: True)
            self.assertEqual(unavailable.exception.code, "BACKEND_UNAVAILABLE")
            self.assertEqual(unused.enters, 0)

    async def test_timeout_confirmed_abort_cancels_but_keeps_uncooperative_runner(self) -> None:
        release = asyncio.Event()
        cancelled = asyncio.Event()
        transaction = FakeTransaction(ignore_cancel=True)
        supervisor = TransactionReadSupervisor(timeout_seconds=0.02, cancel_grace_seconds=0.005)

        async def body(_: object) -> str:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            return "late"

        aborts: list[int] = []
        with self.assertRaises(DomainError) as raised:
            await supervisor.run(transaction, body, lambda: aborts.append(1) or True)
        self.assertEqual(raised.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(aborts, [1])
        self.assertTrue(cancelled.is_set())
        self.assertTrue(supervisor.poisoned)
        self.assertEqual(supervisor.active_read_count, 1)

        release.set()
        for _ in range(10):
            if supervisor.active_read_count == 0:
                break
            await asyncio.sleep(0)
        self.assertEqual(supervisor.active_read_count, 0)

    async def test_unconfirmed_abort_keeps_inflight_record_and_rejects_new_runs(self) -> None:
        release = asyncio.Event()
        transaction = FakeTransaction()
        supervisor = TransactionReadSupervisor(timeout_seconds=0.02, cancel_grace_seconds=0.005)

        async def body(_: object) -> str:
            await release.wait()
            return "late"

        call = asyncio.create_task(
            supervisor.run(transaction, body, lambda: False)
        )
        await transaction.entered.wait()
        with self.assertRaises(DomainError):
            await call
        self.assertTrue(supervisor.poisoned)
        self.assertEqual(supervisor.active_read_count, 1)

        unused = FakeTransaction()
        with self.assertRaises(DomainError):
            await supervisor.run(unused, lambda _: _yield("never"), lambda: True)
        self.assertEqual(unused.enters, 0)

        release.set()
        for _ in range(10):
            if supervisor.active_read_count == 0:
                break
            await asyncio.sleep(0)
        self.assertEqual(supervisor.active_read_count, 0)

    async def test_parallel_gate_and_cancelled_queue_never_enter(self) -> None:
        first_release = asyncio.Event()
        first = FakeTransaction()
        queued = FakeTransaction()
        supervisor = TransactionReadSupervisor(timeout_seconds=0.5)

        async def first_body(_: object) -> str:
            await first_release.wait()
            return "first"

        first_call = asyncio.create_task(supervisor.run(first, first_body, lambda: True))
        await first.entered.wait()
        queued_call = asyncio.create_task(
            supervisor.run(queued, lambda _: _yield("queued"), lambda: True)
        )
        await asyncio.sleep(0.01)
        queued_call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued_call
        self.assertEqual(queued.enters, 0)

        first_release.set()
        self.assertEqual(await first_call, "first")

    async def test_gate_handoff_token_is_released_when_waiter_is_cancelled_before_resume(self) -> None:
        gate = _SerialGate()
        first_token = await gate.acquire()
        handed_off = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)

        # release() resolves the waiter future synchronously.  Cancel before
        # the waiter task gets its next event-loop turn to exercise the
        # release/token handoff race.
        gate.release(first_token)
        handed_off.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await handed_off

        next_waiter = asyncio.create_task(gate.acquire())
        next_token = await next_waiter
        gate.release(next_token)

    async def test_close_wakes_caller_and_keeps_unfinished_runner_active(self) -> None:
        release = asyncio.Event()
        transaction = FakeTransaction(ignore_cancel=True)
        supervisor = TransactionReadSupervisor(timeout_seconds=0.5, cancel_grace_seconds=0.005)
        aborts: list[int] = []

        async def body(_: object) -> str:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return "late"

        call = asyncio.create_task(
            supervisor.run(transaction, body, lambda: aborts.append(1) or True)
        )
        await transaction.entered.wait()
        supervisor.close()
        with self.assertRaises(DomainError) as raised:
            await call
        self.assertEqual(raised.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(aborts, [1])
        self.assertEqual(supervisor.active_read_count, 1)
        self.assertTrue(supervisor.closed)

        release.set()
        for _ in range(10):
            if supervisor.active_read_count == 0:
                break
            await asyncio.sleep(0)
        self.assertEqual(supervisor.active_read_count, 0)

    async def test_budget_validation(self) -> None:
        with self.assertRaises(ValueError):
            TransactionReadSupervisor(timeout_seconds=0)
        with self.assertRaises(ValueError):
            TransactionReadSupervisor(timeout_seconds=math.inf)
        with self.assertRaises(ValueError):
            TransactionReadSupervisor(timeout_seconds=2.1)
        with self.assertRaises(ValueError):
            TransactionReadSupervisor(cancel_grace_seconds=0.2)

    async def test_cross_event_loop_use_is_rejected(self) -> None:
        supervisor = TransactionReadSupervisor()
        first = FakeTransaction()
        await supervisor.run(first, lambda _: _yield("first"), lambda: True)

        async def second_loop() -> None:
            with self.assertRaises(RuntimeError):
                await supervisor.run(FakeTransaction(), lambda _: _yield("second"), lambda: True)

        # asyncio.run creates a distinct loop after this IsolatedAsyncioTestCase
        # turn.  Running it in a worker thread keeps the test deterministic.
        await asyncio.to_thread(asyncio.run, second_loop())


if __name__ == "__main__":
    unittest.main()
