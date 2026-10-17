"""Focused coverage for bounded transport cleanup.

The gates in these tests model SDK and stream handlers that swallow task
cancellation.  Every gate is released explicitly before the test returns so
the isolated test loop has no leaked stubborn tasks.
"""

from __future__ import annotations

import asyncio
import time
import unittest

from cc_iterm_mcp.backends import ItermBackend
from cc_iterm_mcp.shutdown import wait_bounded


async def _stubborn(gate: asyncio.Event) -> None:
    try:
        await gate.wait()
    except asyncio.CancelledError:
        # A real SDK/handler may ignore cancellation while it owns an
        # external transport. The owner must detach it without waiting here.
        await gate.wait()


class _Websocket:
    def __init__(self, gate: asyncio.Event):
        self.gate = gate
        self.started = False

    async def close(self):
        self.started = True
        await _stubborn(self.gate)


class _Connection:
    def __init__(self, dispatcher, websocket):
        self._Connection__dispatch_forever_future = dispatcher
        self.websocket = websocket


class BoundedTransportCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_helper_returns_pending_without_waiting_for_cancellation(self):
        gate = asyncio.Event()
        task = asyncio.create_task(_stubborn(gate))
        started = time.monotonic()
        result = await wait_bounded((task,), timeout=0.02, cancel_pending=True)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.25)
        self.assertIn(task, result.pending)
        self.assertTrue(result.detached)
        gate.set()
        await asyncio.gather(task, return_exceptions=True)

    async def test_iterm_bounded_close_detaches_dispatcher_and_websocket(self):
        dispatcher_gate = asyncio.Event()
        websocket_gate = asyncio.Event()
        dispatcher = asyncio.create_task(_stubborn(dispatcher_gate))
        websocket = _Websocket(websocket_gate)
        backend = ItermBackend(
            broker_epoch="epoch",
            connection=_Connection(dispatcher, websocket),
            app=object(),
        )
        await asyncio.sleep(0)
        result = await backend.close(timeout=0.02)
        self.assertIsNotNone(result)
        self.assertIn(dispatcher, result.pending)
        self.assertTrue(websocket.started)
        self.assertIsNone(backend._connection)
        dispatcher_gate.set()
        websocket_gate.set()
        await asyncio.gather(dispatcher, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
