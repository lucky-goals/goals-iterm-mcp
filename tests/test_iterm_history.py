"""Focused tests for bounded iTerm2 2.30 history reads."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

try:
    import iterm2
except ImportError:  # pragma: no cover - the optional SDK is test-only
    iterm2 = None

from cc_iterm_mcp.backend_types import CaptureDTO, CaptureLineDTO
from cc_iterm_mcp.backends import ItermBackend, _bound_capture, _capture_size
from cc_iterm_mcp.errors import DomainError


@unittest.skipIf(iterm2 is None, "optional iterm2 SDK is not installed")
class ItermHistoryTests(unittest.IsolatedAsyncioTestCase):
    class Transport:
        def __init__(self, websocket):
            self.websocket = websocket
            self.aborted = False

        def abort(self):
            self.aborted = True
            self.websocket._closing = True
            self.websocket.closed = True

        def is_closing(self):
            return self.websocket._closing

    class WebSocket:
        def __init__(self):
            self._closing = False
            self.closed = False
            self.transport = ItermHistoryTests.Transport(self)

        async def close(self):
            self._closing = True
            self.closed = True

    class Connection:
        def __init__(self):
            self.websocket = ItermHistoryTests.WebSocket()

    class Transaction:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

    class Session:
        session_id = "history-session"

        def __init__(self, *, overflow=100, scrollback=10, first_visible=999):
            self.info = SimpleNamespace(
                mutable_area_height=30,
                scrollback_buffer_height=scrollback,
                overflow=overflow,
                first_visible_line_number=first_visible,
            )
            self.contents_calls = []

        async def async_get_line_info(self):
            return self.info

        async def async_get_contents(self, *args):
            self.contents_calls.append(args)
            raise AssertionError("pinned iTerm2 2.30 history must use raw RPC")

    def _backend(self, session, rpc, *, max_capture_bytes=1 * 1024 * 1024):
        app = SimpleNamespace(
            windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[session])])]
        )
        connection = self.Connection()
        backend = ItermBackend(
            broker_epoch="broker",
            connection=connection,
            app=app,
            max_capture_bytes=max_capture_bytes,
        )
        backend._iterm2 = SimpleNamespace(
            __version__="2.30",
            rpc=SimpleNamespace(async_get_screen_contents=rpc),
            api_pb2=iterm2.api_pb2,
            Transaction=self.Transaction,
        )
        return backend

    @staticmethod
    def _response(start, end, *, bad_range=False, billion_run=False):
        buffer = iterm2.api_pb2.GetBufferResponse(
            status=iterm2.api_pb2.GetBufferResponse.Status.Value("OK")
        )
        returned_start = start - 1 if bad_range else start
        returned_end = end - 1 if bad_range else end
        buffer.windowed_coord_range.coord_range.start.x = 0
        buffer.windowed_coord_range.coord_range.start.y = returned_start
        buffer.windowed_coord_range.coord_range.end.x = 0
        buffer.windowed_coord_range.coord_range.end.y = returned_end
        for row in range(start, end):
            line = buffer.contents.add(text=f"row-{row}", continuation=1)
            if billion_run:
                line.code_points_per_cell.add(num_code_points=1, repeats=1_000_000_000)
        return SimpleNamespace(get_buffer_response=buffer)

    async def test_retained_history_range_excludes_grid_and_uses_raw_rpc(self):
        calls = []

        async def rpc(connection, session_id, windowed_range, style):
            calls.append((connection, session_id, windowed_range, style))
            return self._response(windowed_range.start.y, windowed_range.end.y)

        session = self.Session()
        backend = self._backend(session, rpc)
        target = (await backend.list_sessions())[0]["target"]
        capture = await backend.history(target, 5)

        self.assertEqual((calls[0][2].start.y, calls[0][2].end.y), (105, 110))
        self.assertEqual(calls[0][3], True)
        self.assertEqual(
            [line.position for line in capture.history_lines], [105, 106, 107, 108, 109]
        )
        self.assertFalse(capture.history_lines[0].wrapped)
        self.assertEqual(
            capture.history_source_bounds, {"start": "100", "end_exclusive": "110"}
        )
        self.assertFalse(capture.truncated)
        self.assertEqual(session.contents_calls, [])

    async def test_zero_retained_history_does_not_issue_contents_rpc(self):
        async def rpc(*args):
            raise AssertionError("zero history must not issue a contents RPC")

        session = self.Session(scrollback=0)
        backend = self._backend(session, rpc)
        target = (await backend.list_sessions())[0]["target"]
        capture = await backend.history(target, 5)

        self.assertEqual(capture.history_lines, ())
        self.assertEqual(
            capture.history_source_bounds, {"start": "100", "end_exclusive": "100"}
        )

    async def test_mismatched_returned_range_is_unavailable(self):
        async def rpc(connection, session_id, windowed_range, style):
            return self._response(
                windowed_range.start.y, windowed_range.end.y, bad_range=True
            )

        backend = self._backend(self.Session(), rpc)
        target = (await backend.list_sessions())[0]["target"]
        with self.assertRaises(DomainError) as error:
            await backend.history(target, 5)
        self.assertEqual(error.exception.code, "BACKEND_UNAVAILABLE")

    async def test_raw_history_does_not_expand_pathological_sdk_runs(self):
        async def rpc(connection, session_id, windowed_range, style):
            return self._response(
                windowed_range.start.y, windowed_range.end.y, billion_run=True
            )

        backend = self._backend(self.Session(), rpc)
        target = (await backend.list_sessions())[0]["target"]
        capture = await backend.history(target, 1)
        self.assertEqual(capture.history_lines[0].text, "row-109")

    async def test_unknown_continuation_does_not_fabricate_wrapping(self):
        async def rpc(connection, session_id, windowed_range, style):
            response = self._response(windowed_range.start.y, windowed_range.end.y)
            response.get_buffer_response = SimpleNamespace(
                status=0,
                windowed_coord_range=response.get_buffer_response.windowed_coord_range,
                contents=[SimpleNamespace(text="unknown", continuation=99)],
            )
            return response

        backend = self._backend(self.Session(scrollback=1), rpc)
        target = (await backend.list_sessions())[0]["target"]
        capture = await backend.history(target, 1)
        self.assertIsNone(capture.history_lines[0].wrapped)

    async def test_nonascii_history_metadata_counts_toward_byte_bound(self):
        async def rpc(connection, session_id, windowed_range, style):
            response = self._response(windowed_range.start.y, windowed_range.end.y)
            response.get_buffer_response.contents[0].text = "é"
            response.get_buffer_response.contents[0].continuation = 2
            return response

        backend = self._backend(self.Session(scrollback=1), rpc, max_capture_bytes=6)
        target = (await backend.list_sessions())[0]["target"]
        with self.assertRaises(DomainError) as error:
            await backend.history(target, 1)
        self.assertEqual(error.exception.code, "ITEM_TOO_LARGE")


class HistoryBoundsTests(unittest.TestCase):
    TARGET = {"session_id": "s", "session_epoch": "se", "broker_epoch": "be"}

    def test_truncation_counts_json_escaping_without_splitting_unicode(self):
        text = "界😀\n\t\x00" * 100
        dto = CaptureDTO(screen={"complete": True, "lines": [{"row": 0, "text": text}]})
        bounded = _bound_capture(dto, 400)
        self.assertLessEqual(_capture_size(bounded), 400)
        self.assertTrue(bounded.truncated)
        self.assertFalse(bounded.screen["complete"])
        self.assertTrue(text.startswith(bounded.screen["lines"][0]["text"]))
        bounded.screen["lines"][0]["text"].encode("utf-8", errors="strict")

    def test_oversized_fixed_metadata_is_rejected(self):
        with self.assertRaises(DomainError) as caught:
            _bound_capture(CaptureDTO(context={"value": "x" * 1000}), 400)
        self.assertEqual(caught.exception.code, "ITEM_TOO_LARGE")

    def test_bounding_capture_preserves_private_history_bounds(self):
        dto = CaptureDTO(
            history_lines=(CaptureLineDTO(100, "x" * 100),),
            history_source_bounds={"start": "100", "end_exclusive": "101"},
        )
        bounded = _bound_capture(dto, 512)
        self.assertEqual(
            bounded.history_source_bounds, {"start": "100", "end_exclusive": "101"}
        )
        self.assertEqual(bounded.history_lines[0].position, 100)

    def test_history_bounds_are_closed_and_uint64_bounded(self):
        with self.assertRaises(ValueError):
            CaptureDTO(
                history_source_bounds={"start": "0", "end_exclusive": "1", "extra": "2"}
            )
        with self.assertRaises(ValueError):
            CaptureDTO(
                history_source_bounds={
                    "start": "0",
                    "end_exclusive": "18446744073709551616",
                }
            )


if __name__ == "__main__":
    unittest.main()
