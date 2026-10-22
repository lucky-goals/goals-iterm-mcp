"""Focused lifecycle tests for the terminal closing orchestration service."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from goals_iterm_mcp.closing import CloseService, RecoveryStore
from goals_iterm_mcp.errors import DomainError


def row(session_id: str, *, window: str = "w1", tab: str = "t1", name: str = "alpha"):
    return {
        "session_id": session_id,
        "window_id": window,
        "tab_id": tab,
        "name": name,
        "target": {
            "session_id": session_id,
            "session_epoch": f"epoch-{session_id}",
            "broker_epoch": "broker",
        },
    }


class FakeBackend:
    def __init__(self, rows, probes):
        self.rows = [dict(item) for item in rows]
        self.probes = dict(probes)
        self.calls = []
        self.snapshot_calls = 0

    async def snapshot(self):
        self.snapshot_calls += 1
        return [dict(item) for item in self.rows]

    async def probe(self, current):
        self.calls.append(("probe", current["session_id"]))
        value = self.probes[current["session_id"]]
        return dict(value)

    async def exit_session(self, current, probe, force):
        self.calls.append(("exit", current["session_id"], force))
        return {
            "cli_exit_status": "exited" if probe.get("cli_sessions") else "not_needed",
            "task_interrupted": bool(force),
        }

    async def close_target(self, kind, target_id, expected_rows, force):
        self.calls.append(("close", kind, target_id, force))
        ids = {item["session_id"] for item in expected_rows}
        self.rows = [item for item in self.rows if item["session_id"] not in ids]


def idle_probe(**extra):
    value = {
        "state": "idle",
        "reason": "shell is idle",
        "identity": {"pid": 10, "start": "a"},
        "cli_sessions": [],
    }
    value.update(extra)
    return value


class ClosingTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_session_persists_before_exit_and_verifies_disappearance(self):
        backend = FakeBackend([row("s1")], {"s1": idle_probe()})
        with tempfile.TemporaryDirectory() as directory:
            service = CloseService(backend, store=RecoveryStore(directory))
            result = await service.run("session", session_id="s1")

            self.assertEqual(result["status"], "closed")
            self.assertEqual([item[0] for item in backend.calls], ["probe", "probe", "exit", "close"])
            self.assertEqual(len(result["closed_sessions"]), 1)
            self.assertTrue(result["recovery_record_path"])
            record = json.loads(Path(result["recovery_record_path"]).read_text())
            self.assertEqual(record["operation"], "close_session")
            self.assertTrue(record["steps"])
            self.assertEqual(record["status"], "closed")

    async def test_busy_sibling_refuses_tab_without_writes(self):
        rows = [row("s1", name="one"), row("s2", name="two")]
        backend = FakeBackend(
            rows,
            {
                "s1": idle_probe(),
                "s2": idle_probe(state="busy", reason="running"),
            },
        )
        service = CloseService(backend, store=RecoveryStore(tempfile.mkdtemp()))
        result = await service.run("tab", tab_id="t1")

        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "ACTIVITY_BLOCKED")
        self.assertFalse(any(item[0] in {"exit", "close"} for item in backend.calls))
        self.assertEqual(sorted(result["blocking_sessions"]), ["s2"])

    async def test_refused_busy_target_preserves_known_chat_uuid(self):
        chat = {
            "cli": "codex",
            "chat_uuid": "chat-s2",
            "cwd": "/tmp/project",
            "identity_source": "process",
            "resume": {"executable": "codex", "argv": ["resume", "chat-s2"]},
        }
        backend = FakeBackend(
            [row("s2")],
            {"s2": idle_probe(state="busy", reason="running", cli_sessions=[chat])},
        )
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "session", session_id="s2"
        )
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["failed_sessions"][0]["cli_sessions"][0]["chat_uuid"], "chat-s2")
        self.assertFalse(any(item[0] in {"exit", "close"} for item in backend.calls))

    async def test_cleanup_skips_busy_but_force_attempts_initial_snapshot(self):
        rows = [row("s1", name="one"), row("s2", name="two")]
        probes = {
            "s1": idle_probe(),
            "s2": idle_probe(state="busy", reason="running"),
        }
        backend = FakeBackend(rows, probes)
        service = CloseService(backend, store=RecoveryStore(tempfile.mkdtemp()))
        result = await service.run("all")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["session_id"] for item in result["skipped_sessions"]], ["s2"])
        self.assertEqual([item["session_id"] for item in result["closed_sessions"]], ["s1"])

        backend = FakeBackend(rows, probes)
        service = CloseService(backend, store=RecoveryStore(tempfile.mkdtemp()))
        result = await service.run("all", force=True)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(
            [item[1] for item in backend.calls if item[0] == "exit"],
            ["s1", "s2"],
        )

    async def test_nickname_is_exact_casefolded_and_collision_is_refused(self):
        backend = FakeBackend(
            [row("s1", name="Alpha"), row("s2", name=" alpha ")],
            {"s1": idle_probe(), "s2": idle_probe()},
        )
        service = CloseService(backend, store=RecoveryStore(tempfile.mkdtemp()))
        result = await service.run("session", nickname="ALPHA")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "AMBIGUOUS_SELECTOR")
        self.assertFalse(backend.calls)

    async def test_recovery_failure_writes_nothing_and_dry_run_is_read_only(self):
        class FailingStore:
            def persist(self, record):
                raise OSError("disk full")

        backend = FakeBackend([row("s1")], {"s1": idle_probe()})
        result = await CloseService(backend, store=FailingStore()).run(
            "session", session_id="s1"
        )
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "RECOVERY_PERSISTENCE_FAILED")
        self.assertFalse(any(item[0] in {"exit", "close"} for item in backend.calls))

        backend = FakeBackend([row("s1")], {"s1": idle_probe()})
        with tempfile.TemporaryDirectory() as directory:
            result = await CloseService(backend, store=RecoveryStore(directory)).run(
                "session", session_id="s1", dry_run=True
            )
            self.assertEqual(result["status"], "dry_run")
            self.assertIsNone(result["recovery_record_path"])
            self.assertFalse(any(item[0] in {"exit", "close"} for item in backend.calls))
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_membership_change_before_write_is_refused(self):
        backend = FakeBackend([row("s1")], {"s1": idle_probe()})
        original_snapshot = backend.snapshot

        async def changed_snapshot():
            value = await original_snapshot()
            if backend.snapshot_calls == 2:
                value[0]["target"] = dict(value[0]["target"], session_epoch="changed")
            return value

        backend.snapshot = changed_snapshot
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "session", session_id="s1"
        )
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "TARGET_CHANGED")
        self.assertFalse(any(item[0] in {"exit", "close"} for item in backend.calls))

    async def test_store_directory_and_receipt_modes_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RecoveryStore(Path(directory) / "nested")
            info = store.persist({"operation": "test", "steps": []})
            self.assertEqual(stat.S_IMODE(os.stat(store.directory).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(info["recovery_record_path"]).st_mode), 0o600)

    async def test_cancellation_returns_unknown_and_does_not_continue_to_siblings(self):
        class BlockingBackend(FakeBackend):
            def __init__(self):
                super().__init__([row("s1"), row("s2", name="two")], {"s1": idle_probe(), "s2": idle_probe()})
                self.entered = asyncio.Event()

            async def exit_session(self, current, probe, force):
                self.calls.append(("exit", current["session_id"], force))
                self.entered.set()
                await asyncio.Future()

        backend = BlockingBackend()
        with tempfile.TemporaryDirectory() as directory:
            task = asyncio.create_task(
                CloseService(backend, store=RecoveryStore(directory)).run("all")
            )
            await backend.entered.wait()
            task.cancel()
            result = await task
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "CANCELLED")
        self.assertEqual([item[1] for item in backend.calls if item[0] == "exit"], ["s1"])
        self.assertFalse(any(item[0] == "close" for item in backend.calls))

    async def test_journal_failure_stops_before_first_terminal_write(self):
        class BadJournal:
            def persist(self, record):
                return {"recovery_record_id": "receipt", "recovery_record_path": "/tmp/receipt.json"}

            def update(self, path, record):
                raise OSError("journal unavailable")

        backend = FakeBackend([row("s1")], {"s1": idle_probe()})
        result = await CloseService(backend, store=BadJournal()).run("session", session_id="s1")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["code"], "RECOVERY_PERSISTENCE_FAILED")
        self.assertFalse(any(item[0] in {"exit", "close"} for item in backend.calls))

    async def test_force_cleanup_reports_missing_cli_identity_instead_of_noop(self):
        backend = FakeBackend(
            [row("s1")],
            {"s1": idle_probe(state="busy", cli_sessions=[{"cli": "codex"}])},
        )
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "all", force=True
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_sessions"][0]["session_id"], "s1")
        self.assertFalse(any(item[0] in {"exit", "close"} for item in backend.calls))

    async def test_malformed_exit_result_does_not_close_terminal(self):
        class MalformedExit(FakeBackend):
            async def exit_session(self, current, probe, force):
                self.calls.append(("exit", current["session_id"], force))
                return {}

        backend = MalformedExit([row("s1")], {"s1": idle_probe()})
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "session", session_id="s1"
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_sessions"][0]["reason"], "CLI_EXIT_STATUS_MISSING")
        self.assertFalse(any(item[0] == "close" for item in backend.calls))

    async def test_new_protection_block_is_not_bypassed_by_force(self):
        class FreshBlock(FakeBackend):
            def __init__(self):
                super().__init__([row("s1")], {"s1": idle_probe()})
                self.probe_count = 0

            async def probe(self, current):
                self.probe_count += 1
                if self.probe_count == 2:
                    return idle_probe(blocked_code="MCP_HOST_PROTECTED")
                return await super().probe(current)

        backend = FreshBlock()
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "session", session_id="s1", force=True
        )
        self.assertNotEqual(result["status"], "closed")
        self.assertEqual(result["failed_sessions"][0]["reason"], "MCP_HOST_PROTECTED")
        self.assertFalse(any(item[0] in {"exit", "close"} for item in backend.calls))

    async def test_backend_domain_codes_are_preserved(self):
        class DeniedClose(FakeBackend):
            async def close_target(self, kind, target_id, expected_rows, force):
                self.calls.append(("close", kind, target_id, force))
                raise DomainError("CLOSE_DENIED", "operator declined")

        backend = DeniedClose([row("s1")], {"s1": idle_probe()})
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "session", session_id="s1"
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_sessions"][0]["reason"], "CLOSE_DENIED")

    async def test_exit_auto_closed_session_is_closed_without_native_retry(self):
        class AutoClosingSession(FakeBackend):
            async def exit_session(self, current, probe, force):
                value = await super().exit_session(current, probe, force)
                self.rows = [
                    item for item in self.rows if item["session_id"] != current["session_id"]
                ]
                return value

        chat = {
            "cli": "codex",
            "chat_uuid": "chat-s1",
            "cwd": "/tmp/project",
            "identity_source": "process",
            "resume": {"executable": "codex", "argv": ["resume", "chat-s1"]},
        }
        backend = AutoClosingSession([row("s1")], {"s1": idle_probe(cli_sessions=[chat])})
        with tempfile.TemporaryDirectory() as directory:
            result = await CloseService(backend, store=RecoveryStore(directory)).run(
                "session", session_id="s1"
            )
            receipt = json.loads(Path(result["recovery_record_path"]).read_text())

        self.assertEqual(result["status"], "closed")
        self.assertEqual(
            result["closed_sessions"][0]["closure_method"], "auto_closed_after_exit"
        )
        self.assertEqual(
            result["closed_sessions"][0]["cli_sessions"][0]["chat_uuid"], "chat-s1"
        )
        self.assertEqual(receipt["sessions"][0]["cli_sessions"][0]["chat_uuid"], "chat-s1")
        self.assertFalse(any(item[0] == "close" for item in backend.calls))

    async def test_parent_auto_closes_members_removed_by_each_exit(self):
        class AutoClosingParent(FakeBackend):
            async def exit_session(self, current, probe, force):
                value = await super().exit_session(current, probe, force)
                self.rows = [
                    item for item in self.rows if item["session_id"] != current["session_id"]
                ]
                return value

        backend = AutoClosingParent(
            [row("s1", tab="t1"), row("s2", tab="t1")],
            {"s1": idle_probe(), "s2": idle_probe()},
        )
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "tab", tab_id="t1"
        )

        self.assertEqual(result["status"], "closed")
        self.assertEqual(
            [item["session_id"] for item in result["closed_sessions"]], ["s1", "s2"]
        )
        self.assertFalse(any(item[0] == "close" for item in backend.calls))

    async def test_parent_closes_only_original_members_left_after_auto_close(self):
        class PartiallyAutoClosingParent(FakeBackend):
            async def close_target(self, kind, target_id, expected_rows, force):
                self.close_expected_rows = list(expected_rows)
                return await super().close_target(kind, target_id, expected_rows, force)

            async def exit_session(self, current, probe, force):
                value = await super().exit_session(current, probe, force)
                if current["session_id"] == "s1":
                    self.rows = [
                        item
                        for item in self.rows
                        if item["session_id"] != current["session_id"]
                    ]
                return value

        backend = PartiallyAutoClosingParent(
            [row("s1", tab="t1"), row("s2", tab="t1")],
            {"s1": idle_probe(), "s2": idle_probe()},
        )
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "tab", tab_id="t1"
        )

        self.assertEqual(result["status"], "closed")
        close_calls = [item for item in backend.calls if item[0] == "close"]
        self.assertEqual(len(close_calls), 1)
        self.assertEqual(
            [item["session_id"] for item in backend.close_expected_rows], ["s2"]
        )
        self.assertEqual(
            [item["session_id"] for item in result["closed_sessions"]], ["s1", "s2"]
        )

    async def test_parent_new_member_after_auto_close_blocks_remaining_native_close(self):
        class ParentWithNewMember(FakeBackend):
            async def exit_session(self, current, probe, force):
                value = await super().exit_session(current, probe, force)
                if current["session_id"] == "s1":
                    self.rows = [
                        item
                        for item in self.rows
                        if item["session_id"] != current["session_id"]
                    ]
                    self.rows.append(row("s3", tab="t1", name="new"))
                    self.probes["s3"] = idle_probe()
                return value

        backend = ParentWithNewMember(
            [row("s1", tab="t1"), row("s2", tab="t1")],
            {"s1": idle_probe(), "s2": idle_probe()},
        )
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "tab", tab_id="t1"
        )

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "TARGET_CHANGED")
        self.assertEqual(
            [item["session_id"] for item in result["closed_sessions"]], ["s1"]
        )
        self.assertEqual(result["failed_sessions"][0]["session_id"], "s2")
        self.assertEqual(result["failed_sessions"][0]["reason"], "TARGET_CHANGED")
        self.assertFalse(any(item[0] == "close" for item in backend.calls))

    async def test_parent_member_moved_after_exit_is_target_changed_not_auto_closed(self):
        class ParentWithMovedMember(FakeBackend):
            async def exit_session(self, current, probe, force):
                value = await super().exit_session(current, probe, force)
                if current["session_id"] == "s1":
                    self.rows = [
                        item
                        for item in self.rows
                        if item["session_id"] != current["session_id"]
                    ]
                    self.rows.append(row("s1", window="w2", tab="t2", name="moved"))
                return value

        backend = ParentWithMovedMember(
            [row("s1", tab="t1"), row("s2", tab="t1")],
            {"s1": idle_probe(), "s2": idle_probe()},
        )
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "tab", tab_id="t1"
        )

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "TARGET_CHANGED")
        self.assertEqual(result["unknown_sessions"][0]["session_id"], "s1")
        self.assertEqual(result["unknown_sessions"][0]["reason"], "TARGET_CHANGED")
        self.assertEqual(result["failed_sessions"][0]["session_id"], "s2")
        self.assertFalse(any(item[0] == "close" for item in backend.calls))

    async def test_post_exit_snapshot_failure_does_not_retry_native_close(self):
        class SnapshotFailureAfterExit(FakeBackend):
            async def snapshot(self):
                self.snapshot_calls += 1
                if self.snapshot_calls == 3:
                    raise DomainError("SNAPSHOT_UNKNOWN", "topology refresh lost")
                return [dict(item) for item in self.rows]

        backend = SnapshotFailureAfterExit([row("s1")], {"s1": idle_probe()})
        result = await CloseService(backend, store=RecoveryStore(tempfile.mkdtemp())).run(
            "session", session_id="s1"
        )

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "SNAPSHOT_UNKNOWN")
        self.assertEqual(result["unknown_sessions"][0]["session_id"], "s1")
        self.assertFalse(any(item[0] == "close" for item in backend.calls))


if __name__ == "__main__":
    unittest.main()
