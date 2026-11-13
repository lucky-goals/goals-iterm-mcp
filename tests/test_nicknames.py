"""Targeted tests for conversational iTerm session names."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import iterm2
from mcp.server.fastmcp.exceptions import ToolError

from goals_iterm_mcp.backends import ItermBackend
from goals_iterm_mcp.direct import DirectItermBackend, ItermTools
from goals_iterm_mcp.errors import DomainError
from goals_iterm_mcp.naming import (
    MAX_NICKNAME_LENGTH,
    MIN_NICKNAME_LENGTH,
    NICKNAME_VARIABLE,
    NICKNAMES,
)


def row(session_id: str, name: str | None = None) -> dict:
    target = {
        "session_id": session_id,
        "session_epoch": f"epoch-{session_id}",
        "broker_epoch": "broker",
    }
    result = {"target": target}
    if name is not None:
        result.update(name=name, title=name)
    return result


class NamedBackend:
    def __init__(self, rows: list[dict], *, fail_rename: bool = False):
        self.rows = rows
        self.connect = AsyncMock()
        self.close = AsyncMock()
        self.fail_rename = fail_rename
        self.rename_calls: list[tuple[str, str]] = []
        self.create_calls: list[tuple[str, str | None, str | None, str | None]] = []

    async def list_sessions(self):
        # Return fresh row dictionaries, while retaining the live target data.
        return [dict(item) for item in self.rows]

    async def rename(self, target, name):
        self.rename_calls.append((target["session_id"], name))
        if self.fail_rename:
            raise RuntimeError("simulated write failure")
        for item in self.rows:
            if item["target"]["session_id"] == target["session_id"]:
                item["name"] = name
                item["title"] = name
                return
        raise RuntimeError("missing target")

    async def create(self, kind, target_id=None, command=None, name=None):
        self.create_calls.append((kind, target_id, command, name))
        return {"session_id": "new", "window_id": "window", "tab_id": "tab"}


class NicknameServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_catalogue_contains_200_distinct_four_to_six_letter_names(self):
        self.assertEqual(len(NICKNAMES), 200)
        self.assertEqual(MIN_NICKNAME_LENGTH, 4)
        self.assertEqual(MAX_NICKNAME_LENGTH, 6)
        self.assertEqual(len({item.casefold() for item in NICKNAMES}), 200)
        for item in NICKNAMES:
            self.assertGreaterEqual(len(item), 4)
            self.assertLessEqual(len(item), 6)
            self.assertTrue(item.isascii() and item.isalpha() and item == item.lower())

    async def test_explicit_duplicate_checks_sessions_outside_allowlist(self):
        backend = NamedBackend([row("owned", "home"), row("other", "amber")])
        service = ItermTools(backend, session_ids=["owned"])

        with self.assertRaisesRegex(ToolError, "NICKNAME_IN_USE"):
            await service.invoke(service.rename_session, "owned", "AMBER")
        self.assertEqual(backend.rename_calls, [])

    async def test_catalogue_only_reads_existing_names(self):
        backend = NamedBackend([row("session")])
        before = [dict(item) for item in backend.rows]
        service = ItermTools(backend)

        listed = await service.invoke(service.catalogue)

        self.assertEqual(listed[0]["session_id"], "session")
        self.assertEqual(backend.rows, before)
        self.assertEqual(backend.rename_calls, [])

    async def test_random_rename_is_serialized_and_unique(self):
        backend = NamedBackend([row("session", "home"), row("other", "work")])
        service = ItermTools(backend)

        first, second = await asyncio.gather(
            service.invoke(service.rename_session, "other"),
            service.invoke(service.rename_session, "session"),
        )

        self.assertNotEqual(first["name"], second["name"])
        self.assertEqual(len(backend.rename_calls), 2)
        self.assertEqual({first["name"], second["name"]} & set(NICKNAMES), {first["name"], second["name"]})

    async def test_create_checks_pool_before_backend_write(self):
        backend = NamedBackend([row(f"session-{index}", name) for index, name in enumerate(NICKNAMES)])
        service = ItermTools(backend)

        with self.assertRaisesRegex(ToolError, "NICKNAME_POOL_EXHAUSTED"):
            await service.invoke(service.create, "window")
        self.assertEqual(backend.create_calls, [])

    async def test_create_passes_generated_name_to_named_backend(self):
        backend = NamedBackend([row("external", "acorn")])
        service = ItermTools(backend)

        result = await service.invoke(service.create, "window", None, "/bin/cat")

        self.assertEqual(len(backend.create_calls), 1)
        self.assertEqual(backend.create_calls[0][:3], ("window", None, "/bin/cat"))
        self.assertEqual(backend.create_calls[0][3], result["name"])
        self.assertNotEqual(result["name"], "acorn")

    async def test_create_accepts_explicit_name_and_rejects_collisions_or_reserved(self):
        backend = NamedBackend([row("external", "amber")])
        service = ItermTools(backend)

        created = await service.invoke(
            service.create, "window", None, None, True, " cedar "
        )
        self.assertEqual(created["name"], "cedar")
        self.assertEqual(backend.create_calls, [("window", None, None, "cedar")])

        for duplicate_or_bad, expected in (
            ("AMBER", "NICKNAME_IN_USE"),
            ("   ", "INVALID_ARGUMENT"),
            ("active", "INVALID_ARGUMENT"),
            ("all", "INVALID_ARGUMENT"),
        ):
            with self.assertRaisesRegex(ToolError, expected):
                await service.invoke(
                    service.create, "window", None, None, True, duplicate_or_bad
                )
        self.assertEqual(len(backend.create_calls), 1)

    async def test_rename_failure_is_reported_without_retry(self):
        backend = NamedBackend([row("session", "home")], fail_rename=True)
        service = ItermTools(backend)

        with self.assertRaisesRegex(ToolError, "ITERM_API_ERROR"):
            await service.invoke(service.rename_session, "session", "ember")
        self.assertEqual(backend.rename_calls, [("session", "ember")])

    async def test_name_is_visible_after_service_restart(self):
        backend = NamedBackend([row("session", "home")])
        first_service = ItermTools(backend)

        renamed = await first_service.invoke(first_service.rename_session, "session", "ember")
        second_service = ItermTools(backend)
        listed = await second_service.invoke(second_service.catalogue)

        self.assertEqual(renamed["name"], "ember")
        self.assertEqual(listed[0]["name"], "ember")


class DirectBackendNicknameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            session_id="created",
            async_set_variable=AsyncMock(),
            async_set_name=AsyncMock(),
            async_set_profile_properties=AsyncMock(),
            async_get_variable=AsyncMock(return_value="ember"),
            async_activate=AsyncMock(),
        )
        self.tab = SimpleNamespace(tab_id="tab", current_session=self.session)
        self.window = SimpleNamespace(window_id="window", current_tab=self.tab)
        self.window.async_create_tab = AsyncMock(return_value=self.tab)
        self.window_factory = SimpleNamespace(async_create=AsyncMock(return_value=self.window))
        self.app = SimpleNamespace(
            terminal_windows=[self.window], get_session_by_id=lambda _session_id: self.session,
            async_refresh=AsyncMock(),
            async_activate=AsyncMock(),
        )
        self.backend = DirectItermBackend(
            broker_epoch="broker", connection=object(), app=self.app
        )
        self.backend._iterm2 = SimpleNamespace(
            LocalWriteOnlyProfile=iterm2.LocalWriteOnlyProfile,
            Profile=iterm2.Profile,
            TitleComponents=iterm2.TitleComponents,
            Window=self.window_factory,
        )

    async def test_creation_uses_profile_name_before_persisting_variable(self):
        result = await self.backend.create("window", command="/bin/cat", name="ember")

        profile = self.window_factory.async_create.await_args.kwargs["profile_customizations"]
        self.assertEqual(result["name"], "ember")
        self.assertTrue(result["activated"])
        self.assertEqual(profile.values["Name"], '"ember"')
        self.assertEqual(profile.values["Allow Title Setting"], "false")
        self.assertEqual(profile.values["Title Components"], "1")
        self.assertEqual(profile.values["Command"], '"/bin/cat"')
        self.session.async_set_name.assert_awaited_once_with("ember")
        self.session.async_set_variable.assert_awaited_once_with(NICKNAME_VARIABLE, "ember")
        self.session.async_activate.assert_awaited_once_with(
            select_tab=True, order_window_front=True
        )
        self.app.async_activate.assert_awaited_once_with(
            raise_all_windows=False, ignoring_other_apps=True
        )

    async def test_creation_can_skip_foreground_activation_and_activate_existing_session(self):
        result = await self.backend.create(
            "tab", target_id="window", name="ember", activate=False
        )
        self.assertFalse(result["activated"])
        self.session.async_activate.assert_not_awaited()
        self.app.async_activate.assert_not_awaited()

        target = {
            "session_id": "created",
            "session_epoch": "epoch",
            "broker_epoch": "broker",
        }
        with patch.object(
            ItermBackend,
            "_session_for",
            new=AsyncMock(return_value=(target, self.session)),
        ):
            await self.backend.activate(target)
        self.session.async_activate.assert_awaited_once_with(
            select_tab=True, order_window_front=True
        )
        self.app.async_activate.assert_awaited_once_with(
            raise_all_windows=False, ignoring_other_apps=True
        )

    async def test_rename_profile_write_then_variable_failure_is_not_retried(self):
        self.session.async_set_variable.side_effect = RuntimeError("variable failed")

        with self.assertRaises(DomainError) as caught:
            await self.backend._apply_name(self.session, "ember")

        self.assertEqual(caught.exception.code, "NAME_PERSISTENCE_FAILED")
        self.session.async_set_profile_properties.assert_awaited_once()
        self.session.async_set_variable.assert_awaited_once_with(NICKNAME_VARIABLE, "ember")

    async def test_catalogue_prefers_persisted_variable_without_writing(self):
        target = {
            "session_id": "created",
            "session_epoch": "epoch",
            "broker_epoch": "broker",
        }
        base_rows = [{"target": target, "title": "Shell", "context_summary": None}]
        with patch.object(ItermBackend, "list_sessions", new=AsyncMock(return_value=base_rows)):
            rows = await self.backend.list_sessions()

        self.assertEqual(rows[0]["name"], "ember")
        self.session.async_get_variable.assert_awaited_once_with(NICKNAME_VARIABLE)
        self.session.async_set_profile_properties.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
