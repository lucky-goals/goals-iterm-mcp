"""Focused backend and adapter tests; no iTerm2 connection is opened."""

from __future__ import annotations

import unittest

from cc_iterm_mcp.backends import ItermBackend
from cc_iterm_mcp.errors import DomainError


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_hierarchy_maps_split_minimized_and_floating_sessions(self):
        class Session:
            def __init__(self, identifier, title):
                self.session_id = identifier
                self.name = title

        class Splitter:
            def __init__(self, children):
                self.children = children

        class Tab:
            def __init__(self, root, minimized_sessions=(), floating_sessions=()):
                self.root = root
                self.minimized_sessions = list(minimized_sessions)
                self.floating_sessions = list(floating_sessions)

        class Window:
            def __init__(self, window_id, tabs):
                self.window_id = window_id
                self.tabs = list(tabs)

        first = Session("s-first", "first")
        nested = Session("s-nested", "nested")
        minimized = Session("s-min", "minimized")
        floating = Session("s-float", "floating")
        root = Splitter([first, Splitter([nested])])
        tab = Tab(root, minimized_sessions=[minimized], floating_sessions=[floating])
        # Keep a flat collection too: hierarchy metadata must win when both
        # shapes are exposed, rather than taking the old early-return path.
        app = type(
            "App",
            (),
            {
                "windows": [Window("window-a", [tab])],
                "sessions": [first, nested, minimized, floating],
            },
        )()
        backend = ItermBackend(broker_epoch="be", connection=object(), app=app)

        descriptors = await backend.list_sessions()
        by_id = {item["target"]["session_id"]: item for item in descriptors}
        self.assertEqual(by_id["s-first"]["window_id"], "window-a")
        self.assertEqual(by_id["s-first"]["tab_id"], None)
        # The fake deliberately omits tab_id to exercise the display fallback.
        self.assertEqual(by_id["s-first"]["display_path"], "window-a/tab[0]/split/0")
        self.assertEqual(by_id["s-nested"]["display_path"], "window-a/tab[0]/split/1/0")
        self.assertEqual(by_id["s-min"]["display_path"], "window-a/tab[0]/minimized/0")
        self.assertEqual(by_id["s-float"]["display_path"], "window-a/tab[0]/floating/0")
        self.assertIsNone(by_id["s-first"]["is_focused"])

    async def test_real_hierarchy_reorder_and_window_move_keep_target_epoch(self):
        class Session:
            def __init__(self, identifier):
                self.session_id = identifier
                self.name = identifier

        class Splitter:
            def __init__(self, children):
                self.children = children

        class Tab:
            def __init__(self, tab_id, root):
                self.tab_id = tab_id
                self.root = root

        class Window:
            def __init__(self, window_id, tabs):
                self.window_id = window_id
                self.tabs = list(tabs)

        target_session = Session("s-moved")
        other = Session("s-other")
        root = Splitter([target_session, other])
        tab = Tab("tab-a", root)
        window = Window("window-a", [tab])
        app = type("App", (), {"windows": [window]})()
        backend = ItermBackend(broker_epoch="be", connection=object(), app=app)

        first = next(
            item
            for item in await backend.list_sessions()
            if item["target"]["session_id"] == "s-moved"
        )
        root.children[:] = [other, target_session]
        window.window_id = "window-b"
        tab.tab_id = "tab-b"
        second = next(
            item
            for item in await backend.list_sessions()
            if item["target"]["session_id"] == "s-moved"
        )

        self.assertEqual(first["target"], second["target"])
        self.assertNotEqual(first["display_path"], second["display_path"])
        self.assertEqual(second["window_id"], "window-b")
        self.assertEqual(second["tab_id"], "tab-b")

    async def test_official_screen_line_accessor_and_cell_geometry(self):
        class Line:
            def __init__(self, text):
                self.string = text

        class Screen:
            number_of_lines = 2
            cursor_coord = type("Point", (), {"x": 2, "y": 1})()

            def line(self, index):
                return Line(["中文\x00", "> "][index])

        class Session:
            session_id = "s"
            grid_size = type("Size", (), {"width": 80, "height": 2})()

            async def async_get_screen_contents(self):
                return Screen()

        app = type(
            "App",
            (),
            {
                "windows": [
                    type(
                        "Window",
                        (),
                        {"tabs": [type("Tab", (), {"sessions": [Session()]})()]},
                    )()
                ]
            },
        )()
        backend = ItermBackend(broker_epoch="be", connection=object(), app=app)
        target = (await backend.list_sessions())[0]["target"]
        capture = await backend.capture(target)
        self.assertEqual(
            [line["text"] for line in capture.screen["lines"]], ["中文\x00", "> "]
        )
        self.assertEqual(capture.screen["columns"], 80)

    async def test_generic_sdk_capture_projects_absolute_cursor_through_window_range(
        self,
    ):
        class Line:
            def __init__(self, text):
                self.string = text

        class Screen:
            number_of_lines = 2
            number_of_columns = 80
            cursor_coord = type("Point", (), {"x": 2, "y": 11})()
            windowed_coord_range = {
                "coord_range": {
                    "start": {"x": 0, "y": 10},
                    "end": {"x": 0, "y": 12},
                }
            }

            def line(self, index):
                return Line(["top", "> "][index])

        class Session:
            session_id = "s"
            grid_size = type("Size", (), {"width": 80, "height": 2})()

            async def async_get_screen_contents(self):
                return Screen()

        app = type(
            "App",
            (),
            {
                "windows": [
                    type(
                        "Window",
                        (),
                        {"tabs": [type("Tab", (), {"sessions": [Session()]})()]},
                    )()
                ]
            },
        )()
        backend = ItermBackend(broker_epoch="be", connection=object(), app=app)
        target = (await backend.list_sessions())[0]["target"]
        capture = await backend.capture(target)
        self.assertEqual(capture.screen["cursor"], {"row": 1, "column": 2})

    async def test_pinned_rpc_preserves_unicode_text_in_one_request(self):
        try:
            import iterm2
        except ImportError:
            self.skipTest("optional iterm2 SDK is not installed")
        from types import SimpleNamespace

        buffer = iterm2.api_pb2.GetBufferResponse(status=0)
        line = buffer.contents.add(text="😀A", continuation=1)
        for count in (2, 0, 1):
            line.code_points_per_cell.add(num_code_points=count, repeats=1)
        line.style.add(repeats=3)
        # Reproduce the pinned SDK bug with a tiny synthetic protobuf, without
        # claiming a real terminal emoji sample was observed.
        self.assertEqual(iterm2.LineContents(line).string_at(0), "😀A")
        buffer.cursor.x, buffer.cursor.y = 3, 0
        calls = []

        async def rpc(connection, session_id, coord_range, styles):
            calls.append((session_id, coord_range, styles))
            return SimpleNamespace(get_buffer_response=buffer)

        class Session:
            session_id = "s"
            grid_size = SimpleNamespace(width=8, height=1)

            async def async_get_screen_contents(self):
                raise AssertionError("must not issue a second screen request")

        app = SimpleNamespace(
            windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[Session()])])]
        )
        backend = ItermBackend(broker_epoch="be", connection=object(), app=app)
        backend._iterm2 = SimpleNamespace(
            __version__="2.30",
            rpc=SimpleNamespace(async_get_screen_contents=rpc),
            api_pb2=iterm2.api_pb2,
        )
        target = (await backend.list_sessions())[0]["target"]
        capture = await backend.capture(target)
        self.assertEqual(capture.screen["lines"][0]["text"], "😀A")
        self.assertEqual(calls, [("s", None, True)])

    async def test_unused_cell_geometry_does_not_expand_or_lose_public_text(self):
        try:
            import iterm2
        except ImportError:
            self.skipTest("optional iterm2 SDK is not installed")
        from types import SimpleNamespace

        buffer = iterm2.api_pb2.GetBufferResponse(status=0)
        line = buffer.contents.add(text="😀A", continuation=1)
        # A pathological run must never reach SDK LineContents expansion.
        line.code_points_per_cell.add(num_code_points=1, repeats=1_000_000_000)
        line.style.add(repeats=3)

        async def rpc(*args):
            return SimpleNamespace(get_buffer_response=buffer)

        session = SimpleNamespace(
            session_id="s", grid_size=SimpleNamespace(width=8, height=1)
        )
        app = SimpleNamespace(
            windows=[SimpleNamespace(tabs=[SimpleNamespace(sessions=[session])])]
        )
        backend = ItermBackend(broker_epoch="be", connection=object(), app=app)
        backend._iterm2 = SimpleNamespace(
            __version__="2.30",
            rpc=SimpleNamespace(async_get_screen_contents=rpc),
            # Any SDK wrapper/LineContents expansion would fail this test.
            api_pb2=iterm2.api_pb2,
        )
        target = (await backend.list_sessions())[0]["target"]
        capture = await backend.capture(target)
        self.assertEqual(capture.screen["lines"][0]["text"], "😀A")

    async def test_real_hierarchy_without_window_id_uses_index_display_fallback(self):
        class Session:
            session_id = "s-fallback"

        class Splitter:
            children = [Session()]

        class Tab:
            root = Splitter()

        class Window:
            tabs = [Tab()]

        app = type("App", (), {"windows": [Window()]})()
        backend = ItermBackend(broker_epoch="be", connection=object(), app=app)

        descriptor = (await backend.list_sessions())[0]
        self.assertIsNone(descriptor["window_id"])
        self.assertIsNone(descriptor["tab_id"])
        self.assertEqual(descriptor["display_path"], "window[0]/tab[0]/split/0")
        self.assertIsNone(descriptor["is_focused"])

    async def test_real_backend_is_explicit_and_uses_suppress_broadcast(self):
        class Session:
            session_id = "s1"

            def __init__(self):
                self.calls = []

            async def async_send_text(self, text, *, suppress_broadcast=False):
                self.calls.append((text, suppress_broadcast))

        class Tab:
            sessions = []

        session = Session()
        Tab.sessions = [session]

        class Window:
            tabs = [Tab()]

        class App:
            windows = [Window()]

        backend = ItermBackend(broker_epoch="be", connection=object(), app=App())
        descriptors = await backend.list_sessions()
        await backend.send(descriptors[0]["target"], "hello")
        self.assertEqual(session.calls, [("hello", True)])
        self.assertNotIn("async_inject", session.__class__.__dict__)

    async def test_unconnected_real_backend_does_not_auto_import_or_connect(self):
        backend = ItermBackend(broker_epoch="be")
        with self.assertRaises(DomainError) as result:
            await backend.list_sessions()
        self.assertEqual(result.exception.code, "BACKEND_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
