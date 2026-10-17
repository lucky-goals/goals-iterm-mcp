"""Connection acquisition failures must release transport ownership."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from goals_iterm_mcp.backends import ItermBackend
from goals_iterm_mcp.errors import DomainError


class InitializationTests(unittest.IsolatedAsyncioTestCase):
    def connection(self, error):
        dispatcher = asyncio.create_task(asyncio.Event().wait())
        connection = SimpleNamespace(
            websocket=SimpleNamespace(close=AsyncMock()),
            async_get_app=AsyncMock(side_effect=error),
        )
        connection._Connection__dispatch_forever_future = dispatcher
        return connection, dispatcher

    async def test_lookup_failures_close_transport_and_allow_fresh_retry(self):
        for error in (RuntimeError('secret'), DomainError('BACKEND_UNAVAILABLE', 'failed')):
            with self.subTest(error=type(error).__name__):
                connection, dispatcher = self.connection(error)
                replacement = SimpleNamespace(async_get_app=AsyncMock(return_value=object()))
                factory = Mock(side_effect=[connection, replacement])
                backend = ItermBackend(broker_epoch='test', connection_factory=factory)
                with self.assertRaises(DomainError):
                    await backend.connect()
                self.assertTrue(dispatcher.cancelled())
                connection.websocket.close.assert_awaited_once()
                self.assertFalse(backend.connected)
                await backend.connect()
                self.assertTrue(backend.connected)
                self.assertEqual(factory.call_count, 2)
                await backend.close(timeout=.1)

    async def test_repeated_cancellation_waits_for_transport_cleanup(self):
        lookup_started, close_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def lookup():
            lookup_started.set()
            await asyncio.Event().wait()
        async def close():
            close_started.set()
            await release.wait()
        connection, dispatcher = self.connection(lookup)
        connection.websocket.close.side_effect = close
        backend = ItermBackend(broker_epoch='test', connection_factory=lambda: connection)
        task = asyncio.create_task(backend.connect())
        await lookup_started.wait()
        task.cancel()
        await close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(dispatcher.cancelled())
        connection.websocket.close.assert_awaited_once()
        self.assertFalse(backend.connected)

    async def test_factory_failure_does_not_prevent_retry(self):
        connection = SimpleNamespace(async_get_app=AsyncMock(return_value=object()))
        factory = Mock(side_effect=[RuntimeError('secret'), connection])
        backend = ItermBackend(broker_epoch='test', connection_factory=factory)
        with self.assertRaises(DomainError):
            await backend.connect()
        await backend.connect()
        self.assertTrue(backend.connected)
        await backend.close(timeout=.1)

    async def test_failed_close_blocks_reconnection(self):
        for synchronous in (False, True):
            connection, dispatcher = self.connection(RuntimeError('lookup'))
            mock_type = Mock if synchronous else AsyncMock
            connection.websocket.close = mock_type(side_effect=RuntimeError('close'))
            factory = Mock(return_value=connection)
            backend = ItermBackend(broker_epoch='test', connection_factory=factory)
            with self.assertRaises(DomainError):
                await backend.connect()
            with self.assertRaises(DomainError):
                await backend.connect()
            self.assertEqual(factory.call_count, 1)
            self.assertTrue(dispatcher.cancelled())

    async def test_missing_app_is_failure_and_closes_transport(self):
        connection, dispatcher = self.connection(None)
        connection.async_get_app.return_value = None
        backend = ItermBackend(broker_epoch='test', connection_factory=lambda: connection)
        with self.assertRaises(DomainError):
            await backend.connect()
        self.assertTrue(dispatcher.cancelled())
        connection.websocket.close.assert_awaited_once()

    async def test_slow_cleanup_is_bounded_and_blocks_retry(self):
        connection, dispatcher = self.connection(RuntimeError('lookup'))
        release = asyncio.Event()
        async def slow_close():
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        connection.websocket.close.side_effect = slow_close
        backend = ItermBackend(broker_epoch='test', connection_factory=lambda: connection)
        original_close = backend.close
        backend.close = lambda **kwargs: original_close(timeout=.01)
        try:
            with self.assertRaises(DomainError):
                await asyncio.wait_for(backend.connect(), timeout=.5)
            with self.assertRaises(DomainError):
                await backend.connect()
            self.assertTrue(dispatcher.cancelled())
        finally:
            release.set()
            await asyncio.sleep(0)

    async def test_self_cancelled_websocket_close_blocks_retry(self):
        connection, _ = self.connection(RuntimeError('lookup'))
        connection.websocket.close.side_effect = asyncio.CancelledError()
        factory = Mock(return_value=connection)
        backend = ItermBackend(broker_epoch='test', connection_factory=factory)
        with self.assertRaises(DomainError):
            await backend.connect()
        with self.assertRaises(DomainError):
            await backend.connect()
        self.assertEqual(factory.call_count, 1)
