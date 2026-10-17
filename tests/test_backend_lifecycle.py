"""Session epoch lifecycle tests for the strict iTerm topology catalogue."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cc_iterm_mcp.backends import ItermBackend
from cc_iterm_mcp.errors import DomainError


class _Session:
    def __init__(self, session_id: str, title: str | None = None):
        self.session_id = session_id
        self.name = title or session_id


class _Splitter:
    def __init__(self, *children):
        self.children = list(children)


class _Tab:
    def __init__(self, tab_id: str, session):
        self.tab_id = tab_id
        self.root = _Splitter(session)


class _Window:
    def __init__(self, window_id: str, tab):
        self.window_id = window_id
        self.tabs = [tab]


class _App:
    def __init__(self, session):
        self.tab = _Tab("tab-a", session)
        self.window = _Window("window-a", self.tab)
        self.windows = [self.window]


def _backend(app) -> ItermBackend:
    return ItermBackend(broker_epoch="broker", connection=object(), app=app)


def _target(rows, session_id: str):
    return next(row["target"] for row in rows if row["target"]["session_id"] == session_id)


class BackendLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_rename_move_and_focus_changes_preserve_epoch(self):
        session = _Session("session-a", "before")
        app = _App(session)
        first = _backend(app)

        rows = await first.list_sessions()
        original = _target(rows, "session-a")

        session.name = "after"
        app.window.window_id = "window-b"
        app.tab.tab_id = "tab-b"
        second = _target(await first.list_sessions(), "session-a")

        self.assertEqual(second["session_epoch"], original["session_epoch"])
        self.assertEqual(second["session_id"], original["session_id"])

    async def test_removal_prunes_and_same_uuid_reappearance_gets_new_epoch(self):
        session = _Session("session-a")
        app = _App(session)
        backend = _backend(app)
        original = _target(await backend.list_sessions(), "session-a")

        app.windows = []
        self.assertEqual(await backend.list_sessions(), [])
        self.assertNotIn("session-a", backend._session_epochs)

        replacement = _Session("session-a")
        app.tab = _Tab("tab-c", replacement)
        app.window = _Window("window-c", app.tab)
        app.windows = [app.window]
        replacement_target = _target(await backend.list_sessions(), "session-a")
        self.assertNotEqual(replacement_target["session_epoch"], original["session_epoch"])

        with self.assertRaises(DomainError) as error:
            await backend.send(original, "hello")
        self.assertEqual(error.exception.code, "STALE_TARGET")

    async def test_topology_change_while_metadata_awaits_does_not_commit(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class DelayedSession(_Session):
            delay = False

            async def async_get_variable(self, name):
                if self.delay and name == "jobName":
                    started.set()
                    await release.wait()
                return None

        session = DelayedSession("session-a")
        app = _App(session)
        backend = _backend(app)
        original = _target(await backend.list_sessions(), "session-a")

        session.delay = True
        listing = asyncio.create_task(backend.list_sessions())
        await asyncio.wait_for(started.wait(), timeout=1)
        app.windows = [_Window("window-b", _Tab("tab-b", _Session("session-b")))]
        release.set()

        with self.assertRaises(DomainError) as error:
            await listing
        self.assertEqual(error.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(backend._session_epochs, {"session-a": original["session_epoch"]})

    async def test_map_is_unchanged_for_unidentified_duplicate_or_failed_scans(self):
        first = _Session("session-a")
        app = _App(first)
        backend = _backend(app)
        original = _target(await backend.list_sessions(), "session-a")
        before = dict(backend._session_epochs)

        app.tab.root = _Splitter(first, first)
        with self.assertRaises(DomainError) as duplicate:
            await backend.list_sessions()
        self.assertEqual(duplicate.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(backend._session_epochs, before)

        app.tab.root = _Splitter(SimpleNamespace())
        with self.assertRaises(DomainError) as malformed:
            await backend.list_sessions()
        self.assertEqual(malformed.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(backend._session_epochs, before)

        cycle = _Splitter()
        cycle.children = [cycle]
        app.tab.root = cycle
        with self.assertRaises(DomainError) as cyclic:
            await backend.list_sessions()
        self.assertEqual(cyclic.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(backend._session_epochs, before)

        class BrokenApp:
            @property
            def windows(self):
                raise RuntimeError("enumeration failed")

        backend._app = BrokenApp()
        with self.assertRaises(DomainError) as failed:
            await backend.list_sessions()
        self.assertEqual(failed.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(backend._session_epochs, before)
        self.assertEqual(original["session_id"], "session-a")

    async def test_flat_catalogue_extra_id_is_inconsistent_with_hierarchy(self):
        session = _Session("session-a")
        app = _App(session)
        backend = _backend(app)
        original = _target(await backend.list_sessions(), "session-a")
        app.sessions = [session, _Session("session-extra")]

        with self.assertRaises(DomainError) as error:
            await backend.list_sessions()
        self.assertEqual(error.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(backend._session_epochs, {"session-a": original["session_epoch"]})

    async def test_topology_work_budget_covers_window_and_tab_nodes(self):
        session = _Session("session-a")
        app = _App(session)
        backend = _backend(app)
        original = _target(await backend.list_sessions(), "session-a")

        # One window, one tab, and one splitter already consume the tiny
        # patched budget; walking its leaf must fail before any epoch commit.
        with patch("cc_iterm_mcp.backends.MAX_TOPOLOGY_NODES", 3):
            with self.assertRaises(DomainError) as error:
                await backend.list_sessions()
        self.assertEqual(error.exception.code, "RESOURCE_LIMIT")
        self.assertEqual(backend._session_epochs, {"session-a": original["session_epoch"]})

    async def test_nested_wide_splitters_stop_generators_at_shared_budget(self):
        session = _Session("session-a")
        app = _App(session)
        backend = _backend(app)
        original = _target(await backend.list_sessions(), "session-a")

        class CountedChildren:
            def __init__(self, values):
                self.values = values
                self.yielded = 0

            def __iter__(self):
                for value in self.values:
                    self.yielded += 1
                    yield value

        inner = _Splitter()
        inner_children = CountedChildren([_Session(f"inner-{index}") for index in range(100)])
        inner.children = inner_children
        outer = _Splitter()
        outer_children = CountedChildren([inner, *(_Session(f"outer-{index}") for index in range(100))])
        outer.children = outer_children
        app.windows = [
            _Window("window-wide", SimpleNamespace(tab_id="tab-wide", root=outer)),
        ]

        with patch("cc_iterm_mcp.backends.MAX_TOPOLOGY_NODES", 8):
            with self.assertRaises(DomainError) as error:
                await backend.list_sessions()
        self.assertEqual(error.exception.code, "RESOURCE_LIMIT")
        self.assertLessEqual(outer_children.yielded, 9)
        self.assertLessEqual(inner_children.yielded, 9)
        self.assertEqual(backend._session_epochs, {"session-a": original["session_epoch"]})

    async def test_catalogue_limit_fails_without_pruning(self):
        session = _Session("session-a")
        app = _App(session)
        backend = _backend(app)
        original = _target(await backend.list_sessions(), "session-a")
        sessions = [_Session(f"session-{index}") for index in range(257)]
        app.windows = [_Window("window-many", _Tab("tab-many", _Splitter(*sessions)))]

        with self.assertRaises(DomainError) as error:
            await backend.list_sessions()
        self.assertEqual(error.exception.code, "RESOURCE_LIMIT")
        self.assertEqual(backend._session_epochs, {"session-a": original["session_epoch"]})

    async def test_epoch_catalogue_stays_bounded_across_many_churn_cycles(self):
        session = _Session("session-0")
        app = _App(session)
        backend = _backend(app)
        await backend.list_sessions()

        for index in range(300):
            current = _Session(f"session-{index + 1}")
            app.windows = [_Window("window-churn", _Tab("tab-churn", current))]
            await backend.list_sessions()

        self.assertEqual(len(backend._session_epochs), 1)
        self.assertEqual(set(backend._session_epochs), {"session-300"})


if __name__ == "__main__":
    unittest.main()
