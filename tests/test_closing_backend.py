"""Focused tests for the iTerm lifecycle backend seams."""

from __future__ import annotations

import os
import signal
import unittest

from goals_iterm_mcp.closing_backend import ClosingBackend
from goals_iterm_mcp.errors import DomainError
from goals_iterm_mcp.process_inspection import MacOSProcessProvider, ProcessRecord


def make_row(session_id: str = "s1", *, tab: str = "t1", window: str = "w1"):
    return {
        "session_id": session_id,
        "window_id": window,
        "tab_id": tab,
        "title": "Shell",
        "target": {
            "session_id": session_id,
            "session_epoch": f"epoch-{session_id}",
            "broker_epoch": "broker",
        },
    }


class Point:
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y


class Prompt:
    state = "EDITING"
    command_range = (Point(4, 2), Point(4, 2))


class NativeSession:
    def __init__(self, owner):
        self.owner = owner
        self.close_calls = []

    async def async_close(self, *, force=False):
        self.close_calls.append(force)
        self.owner.rows.clear()


class VariableSession(NativeSession):
    def __init__(self, owner, variables):
        super().__init__(owner)
        self.variables = dict(variables)
        self.reads = []

    async def async_get_variable(self, name):
        self.reads.append(name)
        return self.variables.get(name)


class FakeBackend:
    def __init__(self, rows):
        self.rows = list(rows)
        self.native = NativeSession(self)
        for item in self.rows:
            item["_session"] = self.native
        self.snapshot_calls = 0

    async def list_sessions(self):
        self.snapshot_calls += 1
        return [dict(item) for item in self.rows]


class ProcessEvidence:
    def __init__(self, *, identity=None, active_jobs=None, foreground=False):
        self.identity = identity or {"pid": 123, "start": "start-a"}
        self.active_jobs = [] if active_jobs is None else active_jobs
        self.foreground = foreground

    def __call__(self, row):
        return {
            "available": True,
            "identity": self.identity,
            "active_jobs": list(self.active_jobs),
            "stopped_jobs": [],
            "foreground": self.foreground,
        }


class SequenceProcessEvidence:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self, row):
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return dict(self.values[index])

    def interrupt_foreground(self, identity, row=None, probe=None):
        return {"sent": "SIGINT", "pid": identity["pid"]}

    def verify_interrupt(self, identity, row=None, probe=None):
        return {"verified": True, "complete": True}


class ClosingBackendTests(unittest.IsolatedAsyncioTestCase):
    def _backend(self, rows=None, process=None, prompt=None):
        backend = FakeBackend(rows or [make_row()])
        adapter = ClosingBackend(
            backend,
            process_provider=process or ProcessEvidence(),
            prompt_reader=prompt or (lambda row: Prompt()),
        )
        return backend, adapter

    async def test_snapshot_normalizes_title_and_probe_requires_empty_prompt(self):
        backend, adapter = self._backend()
        rows = await adapter.snapshot()
        self.assertEqual(rows[0]["name"], "Shell")
        probe = await adapter.probe(rows[0])
        self.assertEqual(probe["state"], "idle")
        self.assertEqual(probe["identity"]["pid"], 123)

    async def test_running_and_finished_prompts_fail_closed(self):
        backend, adapter = self._backend()

        class Running(Prompt):
            state = "RUNNING"

        probe = await adapter.probe((await adapter.snapshot())[0])
        self.assertEqual(probe["state"], "idle")

        adapter.prompt_reader = lambda row: Running()
        probe = await adapter.probe((await adapter.snapshot())[0])
        self.assertEqual(probe["state"], "busy")

        class Finished(Prompt):
            state = "FINISHED"

        adapter.prompt_reader = lambda row: Finished()
        probe = await adapter.probe((await adapter.snapshot())[0])
        self.assertEqual(probe["state"], "unknown")

    async def test_active_child_makes_editing_prompt_busy(self):
        _, adapter = self._backend(process=ProcessEvidence(active_jobs=[{"pid": 99}]))
        probe = await adapter.probe((await adapter.snapshot())[0])
        self.assertEqual(probe["state"], "busy")
        self.assertEqual(probe["reason"], "active_job_on_tty")

    async def test_close_forwards_native_force_once_and_verifies_disappearance(self):
        backend, adapter = self._backend()
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        await adapter.exit_session(row, probe, force=False)
        await adapter.close_target("session", "s1", [row], force=True)
        self.assertEqual(backend.native.close_calls, [True])
        self.assertEqual(backend.rows, [])

    async def test_changed_membership_refuses_before_native_write(self):
        backend, adapter = self._backend()
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        await adapter.exit_session(row, probe)
        changed = make_row("s2")
        changed["_session"] = backend.native
        backend.rows[:] = [changed]
        with self.assertRaises(DomainError) as ctx:
            await adapter.close_target("session", "s1", [row])
        self.assertEqual(ctx.exception.code, "TARGET_GONE")
        self.assertEqual(backend.native.close_calls, [])

    async def test_new_process_after_exit_is_refused(self):
        process = ProcessEvidence(identity={"pid": 123, "start": "start-a"})
        backend, adapter = self._backend(process=process)
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        await adapter.exit_session(row, probe)
        process.identity = {"pid": 456, "start": "start-b"}
        with self.assertRaises(DomainError) as ctx:
            await adapter.close_target("session", "s1", [row])
        self.assertEqual(ctx.exception.code, "PROCESS_IDENTITY_CHANGED")
        self.assertEqual(backend.native.close_calls, [])

    async def test_missing_prompt_integration_is_unknown(self):
        _, adapter = self._backend(prompt=lambda row: None)
        probe = await adapter.probe((await adapter.snapshot())[0])
        self.assertEqual(probe["state"], "unknown")
        self.assertEqual(probe["reason"], "prompt_unavailable")
        self.assertFalse(probe["prompt"]["available"])

    async def test_unknown_prompt_with_active_process_is_busy(self):
        _, adapter = self._backend(
            process=ProcessEvidence(active_jobs=[{"pid": 99}]),
            prompt=lambda row: None,
        )
        probe = await adapter.probe((await adapter.snapshot())[0])
        self.assertEqual(probe["state"], "busy")
        self.assertEqual(probe["reason"], "active_job_on_tty")
        self.assertEqual(probe["prompt"]["reason"], "prompt_unavailable")

    async def test_process_provider_none_is_explicitly_unavailable(self):
        _, adapter = self._backend(process=lambda row: None)
        probe = await adapter.probe((await adapter.snapshot())[0])
        self.assertEqual(probe["state"], "unknown")
        self.assertEqual(probe["reason"], "process_inspection_unavailable")
        diagnostic = adapter.diagnostic((await adapter.snapshot())[0], probe)
        self.assertIs(diagnostic["process"]["available"], False)
        self.assertEqual(diagnostic["process"]["reason"], "process_inspection_unavailable")

    async def test_diagnostic_projection_omits_resume_argv_and_raw_reasons(self):
        backend = FakeBackend([make_row()])

        class CliAdapter:
            def probe(self, row, process):
                return {
                    "state": "busy",
                    "reason": "secret command and environment",
                    "identity": {"pid": 200, "start": "cli-start"},
                    "identity_source": "claude_pid",
                    "activity_evidence_gap": "CLI provider exposes no current task lifecycle evidence",
                    "blocked_code": "SECRET / raw adapter detail",
                    "cli_sessions": [
                        {
                            "cli": "codex",
                            "chat_uuid": "12345678-1234-4234-8234-123456789abc",
                            "identity_source": "process_open_file",
                            "persistence_status": "saved",
                            "resume": {"argv": ["--secret-token"]},
                        }
                    ],
                }

        adapter = ClosingBackend(
            backend,
            cli_adapter=CliAdapter(),
            process_provider=ProcessEvidence(),
            prompt_reader=lambda row: Prompt(),
        )
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        diagnostic = adapter.diagnostic(row, probe)
        self.assertEqual(diagnostic["cli"]["activity"]["state"], "busy")
        self.assertEqual(diagnostic["cli"]["identity_source"], "claude_pid")
        self.assertEqual(
            diagnostic["cli"]["activity"]["evidence_gap"],
            "ACTIVITY_EVIDENCE_UNAVAILABLE",
        )
        self.assertEqual(diagnostic["cli"]["blocked_code"], "PROBE_UNAVAILABLE")
        self.assertEqual(diagnostic["close_readiness"]["status"], "blocked")
        self.assertFalse(diagnostic["close_readiness"]["ready"])
        self.assertNotIn("SECRET", str(diagnostic))
        self.assertNotIn("resume", str(diagnostic))
        self.assertNotIn("secret", str(diagnostic))

    async def test_probe_reads_process_binding_variables_from_exact_session(self):
        row = make_row()
        backend = FakeBackend([row])
        variable_session = VariableSession(
            backend,
            {"jobPid": "321", "tty": "ttys004", "jobName": "zsh", "path": "/tmp/work"},
        )
        row["_session"] = variable_session
        seen = []

        def provider(evidence_row):
            seen.append(evidence_row)
            return {
                "available": True,
                "identity": {"pid": 321, "start": "shell-start"},
                "active_jobs": [],
                "stopped_jobs": [],
                "foreground": False,
            }

        adapter = ClosingBackend(
            backend,
            process_provider=provider,
            prompt_reader=lambda row: Prompt(),
        )
        probe = await adapter.probe((await adapter.snapshot())[0])
        self.assertEqual(probe["state"], "idle")
        self.assertEqual(seen[0]["job_pid"], "321")
        self.assertEqual(seen[0]["tty"], "ttys004")
        self.assertEqual(variable_session.reads, ["jobPid", "tty", "jobName", "path"])

    async def test_force_unknown_empty_shell_can_exit_and_close(self):
        backend, adapter = self._backend(prompt=lambda row: None)
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        self.assertEqual(probe["state"], "unknown")
        result = await adapter.exit_session(row, probe, force=True)
        self.assertFalse(result["task_interrupted"])
        await adapter.close_target("session", "s1", [row], force=True)
        self.assertEqual(backend.native.close_calls, [True])

    async def test_verified_exit_that_auto_removes_session_is_already_closed(self):
        backend, adapter = self._backend()
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        await adapter.exit_session(row, probe)
        backend.rows.clear()
        await adapter.close_target("session", "s1", [row], force=False)
        self.assertEqual(backend.native.close_calls, [])

    async def test_new_sibling_job_after_interrupt_is_refused(self):
        backend = FakeBackend([make_row()])
        process = SequenceProcessEvidence(
            [
                {
                    "available": True,
                    "identity": {"pid": 200, "start": "job-start"},
                    "active_jobs": [{"pid": 200, "identity": {"pid": 200, "start": "job-start"}}],
                    "stopped_jobs": [],
                    "foreground": True,
                },
                {
                    "available": True,
                    "identity": {"pid": 100, "start": "shell-start"},
                    "active_jobs": [],
                    "stopped_jobs": [],
                    "foreground": False,
                },
                {
                    "available": True,
                    "identity": {"pid": 100, "start": "shell-start"},
                    "active_jobs": [{"pid": 201, "identity": {"pid": 201, "start": "sibling-start"}}],
                    "stopped_jobs": [],
                    "foreground": True,
                },
            ]
        )
        adapter = ClosingBackend(
            backend,
            process_provider=process,
            prompt_reader=lambda row: Prompt(),
        )
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        await adapter.exit_session(row, probe, force=True)
        with self.assertRaises(DomainError) as ctx:
            await adapter.close_target("session", "s1", [row], force=True)
        self.assertEqual(ctx.exception.code, "ACTIVITY_CHANGED")
        self.assertEqual(backend.native.close_calls, [])

    async def test_verified_interrupt_accepts_iTerm_auto_removed_session(self):
        backend = FakeBackend([make_row()])

        class AutoRemovingProcessEvidence:
            def __call__(self, row):
                return {
                    "available": True,
                    "identity": {"pid": 200, "start": "job-start"},
                    "active_jobs": [
                        {"pid": 200, "identity": {"pid": 200, "start": "job-start"}}
                    ],
                    "stopped_jobs": [],
                    "foreground": True,
                }

            def interrupt_foreground(self, identity, row=None, probe=None):
                backend.rows.clear()
                return {"sent": "SIGINT", "pid": identity["pid"]}

            def verify_interrupt(self, identity, row=None, probe=None):
                return {"verified": True, "complete": True}

        adapter = ClosingBackend(
            backend,
            process_provider=AutoRemovingProcessEvidence(),
            prompt_reader=lambda row: Prompt(),
        )
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        result = await adapter.exit_session(row, probe, force=True)
        self.assertTrue(result["task_interrupted"])
        self.assertEqual(backend.rows, [])

    async def test_cli_child_identity_overrides_shell_identity(self):
        backend = FakeBackend([make_row()])
        class CliProcessEvidence(ProcessEvidence):
            def verify_interrupt(self, identity, row=None, probe=None):
                return {"verified": True, "complete": True}

        process = CliProcessEvidence(
            identity={"pid": 100, "start": "shell-start"},
            active_jobs=[
                {
                    "pid": 200,
                    "identity": {"pid": 200, "start": "cli-start"},
                    "executable": "claude",
                }
            ],
            foreground=True,
        )
        seen = {}

        class CliAdapter:
            class Binding:
                process_start = "cli-start"

            def binding(self, session_id):
                return self.Binding()

            def probe(self, row, process):
                return {
                    "state": "busy",
                    "reason": "cli busy",
                    "identity": {"pid": 200, "process_start": "cli-start"},
                    "cli_sessions": [{"chat_uuid": "12345678-1234-4234-8234-123456789abc"}],
                }

            def exit_session(self, row, probe, force=False):
                seen["probe"] = probe
                return {"cli_exit_status": "exited", "verified": True}

        adapter = ClosingBackend(
            backend,
            cli_adapter=CliAdapter(),
            process_provider=process,
            prompt_reader=lambda row: Prompt(),
        )
        row = (await adapter.snapshot())[0]
        probe = await adapter.probe(row)
        self.assertEqual(probe["identity"]["pid"], 200)
        self.assertEqual(probe["identity"]["start"], "cli-start")
        await adapter.exit_session(row, probe, force=True)
        self.assertEqual(seen["probe"]["identity"]["pid"], 200)


class ProcessInspectionTests(unittest.TestCase):
    def test_ps_runner_uses_argv_and_redacts_command_arguments(self):
        calls = []

        class Completed:
            stdout = "  12  1  12  12 ttys001 S+ Mon Oct 20 10:00:00 2026 /bin/zsh --secret=value\n"

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return Completed()

        provider = MacOSProcessProvider(runner=runner, enabled=True, own_pid=900)
        record = provider.get(12)
        self.assertIsNotNone(record)
        self.assertEqual(record.executable, "zsh")
        self.assertNotIn("secret", record.as_dict())
        self.assertEqual(calls[0][0][0:2], ["ps", "-ww"])
        self.assertNotIn("--secret=value", calls[0][0])
        self.assertFalse(calls[0][1].get("shell", True))

    def test_foreground_leaf_job_is_seen_when_job_pid_is_not_shell(self):
        provider = MacOSProcessProvider(enabled=True, own_pid=900)
        provider.list_processes = lambda: [
            ProcessRecord(10, 1, 10, 20, "ttys001", "S", "shell", "zsh"),
            ProcessRecord(20, 10, 20, 20, "ttys001", "R", "job", "python"),
        ]
        evidence = provider.inspect({"job_pid": 20, "tty": "ttys001"})
        self.assertEqual(evidence["identity"]["pid"], 20)
        self.assertEqual([item["pid"] for item in evidence["active_jobs"]], [20])
        self.assertTrue(evidence["foreground"])

    def test_shell_job_pid_binds_foreground_child_for_interruption(self):
        provider = MacOSProcessProvider(enabled=True, own_pid=900)
        provider.list_processes = lambda: [
            ProcessRecord(10, 1, 10, 20, "ttys001", "S", "shell", "zsh"),
            ProcessRecord(20, 10, 20, 20, "ttys001", "R", "job", "python"),
        ]
        evidence = provider.inspect({"job_pid": 10, "tty": "ttys001"})
        self.assertEqual(evidence["identity"]["pid"], 20)
        self.assertTrue(evidence["foreground"])

    def test_detached_descendant_remains_in_scoped_job_census(self):
        provider = MacOSProcessProvider(enabled=True, own_pid=900)
        provider.list_processes = lambda: [
            ProcessRecord(10, 1, 10, 20, "ttys001", "S", "shell", "zsh"),
            ProcessRecord(20, 10, 20, 20, "ttys001", "R", "job", "python"),
            ProcessRecord(21, 20, 21, 21, None, "R", "child", "worker"),
        ]
        evidence = provider.inspect({"job_pid": 20, "tty": "ttys001"})
        self.assertEqual(
            {item["pid"] for item in evidence["active_jobs"]},
            {20, 21},
        )

    def test_stale_foreground_pid_does_not_count_idle_shell_as_job(self):
        provider = MacOSProcessProvider(enabled=True, own_pid=900)
        provider.list_processes = lambda: [
            ProcessRecord(10, 1, 10, 10, "ttys001", "S", "shell", "zsh"),
        ]
        evidence = provider.inspect({"job_pid": 20, "tty": "ttys001"})
        self.assertEqual(evidence["active_jobs"], [])
        self.assertIsNone(evidence["identity"])

    def test_interrupt_signals_exact_verified_foreground_group(self):
        signals = []
        provider = MacOSProcessProvider(
            enabled=True,
            own_pid=900,
            signaler=lambda pid, value: signals.append((pid, value)),
        )
        provider.list_processes = lambda: [
            ProcessRecord(900, 1, 900, 900, "ttys000", "S", "mcp", "python"),
            ProcessRecord(10, 1, 10, 20, "ttys001", "S", "shell", "zsh"),
            ProcessRecord(20, 10, 20, 20, "ttys001", "R", "job", "python"),
        ]
        result = provider.interrupt_foreground(
            {"pid": 20, "start": "job", "tty": "ttys001"},
            {"job_pid": 20, "tty": "ttys001"},
            {},
        )
        self.assertEqual(result["pid"], 20)
        self.assertEqual(signals, [(-20, signal.SIGINT)])

    def test_empty_process_table_does_not_prove_interrupt_completion(self):
        provider = MacOSProcessProvider(enabled=True, own_pid=900)
        provider.list_processes = lambda: []
        result = provider.verify_interrupt(
            {"pid": os.getpid(), "start": "unavailable"},
            {},
            {},
        )
        self.assertFalse(result["verified"])

    def test_surviving_observed_child_blocks_interrupt_completion(self):
        provider = MacOSProcessProvider(enabled=True, own_pid=900, verify_timeout=0.05)
        provider.list_processes = lambda: [
            ProcessRecord(21, 1, 21, 21, "none", "R", "child", "python"),
        ]
        result = provider.verify_interrupt(
            {"pid": 20, "start": "job", "tty": "ttys001"},
            {},
            {
                "process": {
                    "active_jobs": [
                        {"identity": {"pid": 20, "start": "job", "tty": "ttys001"}},
                        {"identity": {"pid": 21, "start": "child", "tty": "ttys001"}},
                    ],
                    "stopped_jobs": [],
                }
            },
        )
        self.assertFalse(result["verified"])


if __name__ == "__main__":
    unittest.main()
