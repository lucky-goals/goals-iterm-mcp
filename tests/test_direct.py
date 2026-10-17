"""Focused direct MCP contract tests; no real terminal writes."""
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from cc_iterm_mcp.backend_types import CaptureDTO, CaptureLineDTO
from cc_iterm_mcp.direct import ItermTools, build_server


def backend():
    target = {"session_id": "session-1", "session_epoch": "epoch", "broker_epoch": "broker"}
    result = AsyncMock()
    result.list_sessions.return_value = [{"target": target, "window_id": "window-1", "tab_id": "tab-1"}]
    result.capture.return_value = CaptureDTO(screen={"lines": [{"row": 0, "text": "界😀"}]})
    result.history.return_value = CaptureDTO(history_lines=(CaptureLineDTO(3, "history\tline"),))
    return result


class DirectTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_surface_and_read_only(self):
        tools = await build_server(ItermTools(backend())).list_tools()
        self.assertEqual({t.name for t in tools}, {"list_sessions", "read_screen", "read_history", "send_text", "send_keys", "create_window", "create_tab"})
        reads = await build_server(ItermTools(backend()), read_only=True).list_tools()
        self.assertEqual({t.name for t in reads}, {"list_sessions", "read_screen", "read_history"})

    async def test_exact_text_no_newline_added(self):
        fake = backend()
        service = ItermTools(fake)
        text = "a\t界😀\nb\n"
        result = await service.invoke(service.send, "session-1", text)
        self.assertTrue(result["api_acknowledged"])
        self.assertEqual(fake.send.await_args.args[1], text)
        fake.send.assert_awaited_once()

    async def test_missing_and_disallowed_targets_never_write(self):
        fake = backend()
        service = ItermTools(fake, session_ids=["session-1"])
        for name in ["active", "all", "", "other"]:
            with self.assertRaises(ToolError):
                await service.invoke(service.send, name, "x")
        fake.send.assert_not_awaited()
        with self.assertRaises(ToolError):
            await service.invoke(service.create, "window")
        fake.create.assert_not_awaited()

    async def test_write_failure_never_retries_or_leaks(self):
        fake = backend()
        fake.send.side_effect = RuntimeError("private terminal secret")
        service = ItermTools(fake)
        with self.assertRaisesRegex(ToolError, "delivery may be unknown") as caught:
            await service.invoke(service.send, "session-1", "x")
        self.assertNotIn("private", str(caught.exception))
        fake.send.assert_awaited_once()

    async def test_input_and_history_limits(self):
        fake = backend()
        service = ItermTools(fake)
        for text in ["", "界" * 6000]:
            with self.assertRaises(ToolError):
                await service.invoke(service.send, "session-1", text)
        for lines in [0, 2001, True]:
            with self.assertRaises(ToolError):
                await service.invoke(service.history, "session-1", lines)
        fake.send.assert_not_awaited()
        fake.history.assert_not_awaited()

    async def test_mcp_key_validation_and_read_projection(self):
        fake = backend()
        server = build_server(ItermTools(fake))
        await server.call_tool("send_keys", {"session_id": "session-1", "key": "enter"})
        self.assertEqual(fake.send.await_args.args[1], "\r")
        with self.assertRaises(ToolError):
            await server.call_tool("send_keys", {"session_id": "session-1", "key": "invented"})
        fake.send.assert_awaited_once()
        service = ItermTools(fake)
        self.assertEqual((await service.invoke(service.history, "session-1", 10))["lines"][0]["text"], "history\tline")

    async def test_all_supported_keys_send_exact_sequences(self):
        fake = backend()
        server = build_server(ItermTools(fake))
        expected = {
            "enter": "\r", "tab": "\t", "escape": "\x1b", "backspace": "\x7f",
            "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
            "ctrl_c": "\x03", "ctrl_d": "\x04", "ctrl_u": "\x15", "ctrl_l": "\x0c",
        }
        for key, sequence in expected.items():
            await server.call_tool("send_keys", {"session_id": "session-1", "key": key})
            self.assertEqual(fake.send.await_args.args[1], sequence)
        self.assertEqual(fake.send.await_count, len(expected))

    async def test_read_only_write_calls_are_rejected(self):
        fake = backend()
        server = build_server(ItermTools(fake), read_only=True)
        for name in ("send_text", "send_keys", "create_window", "create_tab"):
            with self.assertRaises(ToolError):
                await server.call_tool(name, {})
        fake.send.assert_not_awaited()
        fake.create.assert_not_awaited()

    async def test_creation_arguments_and_invalid_commands(self):
        fake = backend()
        fake.create.return_value = {"session_id": "new"}
        server = build_server(ItermTools(fake))
        await server.call_tool("create_window", {"command": "/bin/cat"})
        fake.create.assert_awaited_with("window", None, "/bin/cat")
        await server.call_tool("create_tab", {"window_id": "window-1"})
        fake.create.assert_awaited_with("tab", "window-1", None)
        for command in ("", "界" * 6000):
            with self.assertRaises(ToolError):
                await server.call_tool("create_window", {"command": command})
        self.assertEqual(fake.create.await_count, 2)

    async def test_concurrent_writes_are_serialized(self):
        fake = backend()
        entered = asyncio.Event()
        release = asyncio.Event()
        async def send(*args):
            entered.set()
            await release.wait()
        fake.send.side_effect = send
        service = ItermTools(fake)
        first = asyncio.create_task(service.invoke(service.send, "session-1", "a"))
        await entered.wait()
        second = asyncio.create_task(service.invoke(service.send, "session-1", "b"))
        await asyncio.sleep(0)
        self.assertEqual(fake.send.await_count, 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(fake.send.await_count, 2)

    async def test_real_stdio_handshake_and_sdk_stdout_isolation(self):
        code = '''
from tests.test_direct import backend
from cc_iterm_mcp.direct import ItermTools, build_server
fake = backend()
async def connect():
    print("SDK diagnostic must not enter protocol stdout")
fake.connect.side_effect = connect
build_server(ItermTools(fake)).run()
'''
        async with stdio_client(StdioServerParameters(command=sys.executable, args=["-c", code])) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                self.assertEqual(len((await client.list_tools()).tools), 7)
                result = await client.call_tool("read_screen", {"session_id": "session-1"})
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent["screen"]["lines"][0]["text"], "界😀")


if __name__ == "__main__":
    unittest.main()
