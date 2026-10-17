"""Cancellation and ownership tests for the iTerm history transaction boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from goals_iterm_mcp.backends import ItermBackend
from goals_iterm_mcp.errors import DomainError
from goals_iterm_mcp.transaction_reads import TransactionReadSupervisor


class _Transport:
    def __init__(self, websocket: "_WebSocket") -> None:
        self.websocket = websocket
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True
        self.websocket._closing = True
        self.websocket.closed = True

    def is_closing(self) -> bool:
        return self.websocket._closing


class _WebSocket:
    def __init__(self) -> None:
        self._closing = False
        self.closed = False
        self.transport = _Transport(self)

    async def close(self) -> None:
        self._closing = True
        self.closed = True


class _Dispatcher:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def __await__(self):
        async def done():
            return None

        return done().__await__()


class _Connection:
    def __init__(self) -> None:
        self.websocket = _WebSocket()
        self.dispatcher = _Dispatcher()
        setattr(self, "_Connection__dispatch_forever_future", self.dispatcher)


class _State:
    def __init__(self) -> None:
        self.entry_started = asyncio.Event()
        self.entry_release = asyncio.Event()
        self.exit_started = asyncio.Event()
        self.body_started = asyncio.Event()
        self.body_release = asyncio.Event()
        self.exit_release = asyncio.Event()
        self.transaction_created = asyncio.Event()
        self.transaction_count = 0
        self.block_body = False
        self.block_entry = False
        self.block_exit = False
        self.exit_calls = 0
        self.exit_cancellations = 0


class _Transaction:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.state.transaction_count += 1
        self.state.transaction_created.set()

    async def __aenter__(self):
        self.state.entry_started.set()
        if self.state.block_entry:
            await self.state.entry_release.wait()
        return self

    async def __aexit__(self, *_exc_info):
        self.state.exit_calls += 1
        self.state.exit_started.set()
        if self.state.block_exit:
            try:
                await self.state.exit_release.wait()
            except asyncio.CancelledError:
                self.state.exit_cancellations += 1
                raise


class _Session:
    session_id = "history-session"
    name = "history-session"

    def __init__(self, state: _State, *, session_id: str | None = None) -> None:
        self.state = state
        if session_id is not None:
            self.session_id = session_id
            self.name = session_id
        self.line_info_calls = 0

    async def async_get_line_info(self, *_args):
        self.line_info_calls += 1
        self.state.body_started.set()
        if self.state.block_body:
            await self.state.body_release.wait()
        return SimpleNamespace(overflow=0, scrollback_buffer_height=0)


class _SDK:
    __version__ = "2.30"

    def __init__(self, state: _State) -> None:
        self.Transaction = lambda _connection: _Transaction(state)
        self.rpc = SimpleNamespace()


class HistoryTransactionTests(unittest.IsolatedAsyncioTestCase):
    def _backend(self, state: _State):
        connection = _Connection()
        session = _Session(state)
        app = SimpleNamespace(windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[session])])])
        backend = ItermBackend(broker_epoch="broker", connection=connection, app=app)
        backend._iterm2 = _SDK(state)
        backend._transaction_supervisor = TransactionReadSupervisor(
            timeout_seconds=0.05,
            cancel_grace_seconds=0.01,
        )
        return backend, connection, session

    async def _target(self, backend: ItermBackend):
        return (await backend.list_sessions())[0]["target"]

    async def test_cancel_after_entry_started_poison_closes_owned_connection(self):
        state = _State()
        state.block_entry = True
        backend, connection, _session = self._backend(state)
        target = await self._target(backend)
        task = asyncio.create_task(backend.history(target, 1))
        await asyncio.wait_for(state.entry_started.wait(), timeout=1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(connection.websocket.transport.aborted)
        self.assertTrue(connection.dispatcher.cancelled)
        self.assertTrue(backend._transaction_poisoned)
        self.assertIsNone(backend._connection)
        with self.assertRaises(DomainError):
            await backend.history(target, 1)
        with self.assertRaises(DomainError):
            await backend.capture(target)
        await backend.close()

    async def test_repeated_cancel_during_exit_has_one_exit_and_poison(self):
        state = _State()
        state.block_exit = True
        backend, connection, _session = self._backend(state)
        target = await self._target(backend)
        task = asyncio.create_task(backend.history(target, 1))
        await asyncio.wait_for(state.exit_started.wait(), timeout=1)

        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(state.exit_calls, 1)
        self.assertGreaterEqual(state.exit_cancellations, 1)
        self.assertTrue(connection.websocket.transport.aborted)
        self.assertTrue(backend._transaction_poisoned)
        await backend.close()

    async def test_timeout_aborts_captured_connection_and_not_replacement(self):
        state = _State()
        state.block_body = True
        backend, connection, _session = self._backend(state)
        target = await self._target(backend)
        task = asyncio.create_task(backend.history(target, 1))
        await asyncio.wait_for(state.body_started.wait(), timeout=1)

        with self.assertRaises(DomainError):
            await task

        replacement = _Connection()
        backend._connection = replacement
        self.assertTrue(connection.websocket.transport.aborted)
        self.assertFalse(replacement.websocket.transport.aborted)
        self.assertTrue(backend._transaction_poisoned)
        await backend.close()

    async def test_abort_callback_refuses_when_backend_pointer_is_replaced(self):
        state = _State()
        backend, captured, _session = self._backend(state)
        replacement = _Connection()
        callback = backend._history_abort_owned(captured)
        backend._connection = replacement

        self.assertTrue(callback())
        self.assertTrue(backend._transaction_poisoned)
        self.assertTrue(captured.websocket.transport.aborted)
        self.assertFalse(replacement.websocket.transport.aborted)
        await backend.close()

    async def test_queued_gate_revalidates_replaced_session_before_reading_it(self):
        state = _State()
        state.block_body = True
        backend, _connection, first_session = self._backend(state)
        second_session = _Session(state, session_id="history-session-2")
        sessions = backend._app.windows[0].tabs[0].sessions
        sessions.append(second_session)
        descriptors = await backend.list_sessions()
        first_target = descriptors[0]["target"]
        second_target = descriptors[1]["target"]

        first_task = asyncio.create_task(backend.history(first_target, 1))
        await asyncio.wait_for(state.body_started.wait(), timeout=1)
        state.transaction_created.clear()

        second_task = asyncio.create_task(backend.history(second_target, 1))
        await asyncio.wait_for(state.transaction_created.wait(), timeout=1)
        replacement = _Session(state, session_id="history-session-2")
        sessions[1] = replacement
        state.body_release.set()

        await first_task
        with self.assertRaises(DomainError) as raised:
            await second_task
        self.assertEqual(raised.exception.code, "STALE_TARGET")
        self.assertEqual(first_session.line_info_calls, 1)
        self.assertEqual(second_session.line_info_calls, 0)
        self.assertEqual(replacement.line_info_calls, 0)
        await backend.close()

    async def test_body_wait_revalidates_same_id_replacement_before_decode(self):
        state = _State()
        state.block_body = True
        backend, _connection, captured_session = self._backend(state)
        target = await self._target(backend)
        task = asyncio.create_task(backend.history(target, 1))
        await asyncio.wait_for(state.body_started.wait(), timeout=1)

        replacement = _Session(state)
        backend._app.windows[0].tabs[0].sessions[0] = replacement
        state.body_release.set()

        with self.assertRaises(DomainError) as raised:
            await task
        self.assertEqual(raised.exception.code, "STALE_TARGET")
        self.assertEqual(captured_session.line_info_calls, 1)
        self.assertEqual(replacement.line_info_calls, 0)
        await backend.close()


if __name__ == "__main__":
    unittest.main()
