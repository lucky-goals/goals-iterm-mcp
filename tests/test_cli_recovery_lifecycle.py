"""Lifecycle invariants for passive CLI identity evidence.

These tests use only injected ``ps``/``lsof`` output.  They never launch a
CLI, attach to iTerm, or read a user's real transcript.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import signal

from goals_iterm_mcp.cli_recovery import CliRecoveryAdapter


PID = os.getpid()
START = "Mon Oct 20 10:00:00 2026"
CHAT = "12345678-1234-4234-8234-1234567890ab"
OTHER_CHAT = "87654321-4321-4321-8123-ba0987654321"


class ProcessEvidenceRunner:
    """Bounded fake for the allowlisted process metadata queries."""

    def __init__(
        self,
        *,
        opened: str | None = None,
        start: str = START,
        executable: str | None = None,
    ):
        self.opened = opened
        self.start = start
        self.executable = executable

    def __call__(self, argv, **_kwargs):
        if "pid=,lstart=,state=" in argv:
            return f"{PID} {self.start} R".encode()
        if "comm=" in argv:
            return (self.executable or "").encode()
        if "command=" in argv:
            return b""
        if "-Fn" in argv:
            if self.opened is None:
                return b""
            return f"p{PID}\nn{self.opened}\n".encode()
        return b""


class CliRecoveryLifecycleTests(unittest.TestCase):
    def test_registered_pid_uuid_is_only_a_candidate_until_current_artifact(self):
        adapter = CliRecoveryAdapter(runner=ProcessEvidenceRunner())
        adapter.register(
            "s1",
            cli="claude",
            pid=PID,
            process_start=START,
            chat_uuid=CHAT,
        )

        probe = adapter.observe("s1", process_start=START).as_probe()

        self.assertEqual(probe["cli_sessions"][0]["identity_status"], "unverified")
        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_UNVERIFIED")
        with self.assertRaisesRegex(RuntimeError, "identity evidence is blocked"):
            adapter.exit_session({"session_id": "s1"}, probe, force=True)

    def test_registered_pid_start_reuse_is_reported_even_when_uuid_is_supplied(self):
        runner = ProcessEvidenceRunner(start="Tue Oct 21 10:00:00 2026")
        adapter = CliRecoveryAdapter(runner=runner)
        adapter.register(
            "s1",
            cli="claude",
            pid=PID,
            process_start=START,
            chat_uuid=CHAT,
        )

        probe = adapter.observe("s1", process_start=START).as_probe()

        self.assertEqual(probe["blocked_code"], "PROCESS_IDENTITY_CHANGED")
        self.assertIn("same process instance", probe["evidence_gap"])

    def test_same_pid_exec_to_another_supported_cli_is_not_reused(self):
        runner = ProcessEvidenceRunner(executable="codex")
        adapter = CliRecoveryAdapter(runner=runner)
        adapter.register(
            "s1",
            cli="claude",
            pid=PID,
            process_start=START,
            chat_uuid=CHAT,
        )

        probe = adapter.observe("s1", process_start=START).as_probe()

        self.assertEqual(probe["blocked_code"], "PROCESS_IDENTITY_CHANGED")

    def test_opened_chat_proof_is_not_cached_after_artifact_disappears(self):
        transcript = "/Users/example/.claude/projects/work/" + CHAT + ".jsonl"
        runner = ProcessEvidenceRunner(opened=transcript)
        adapter = CliRecoveryAdapter(runner=runner)
        adapter.register(
            "s1",
            cli="claude",
            pid=PID,
            process_start=START,
            chat_uuid=CHAT,
        )

        first = adapter.observe("s1", process_start=START).as_probe()
        self.assertNotIn("blocked_code", first)
        self.assertEqual(first["cli_sessions"][0]["identity_status"], "verified")

        runner.opened = None
        second = adapter.observe("s1", process_start=START).as_probe()

        self.assertEqual(second["blocked_code"], "CLI_IDENTITY_UNVERIFIED")
        self.assertEqual(second["cli_sessions"][0]["identity_status"], "unverified")

    def test_current_artifact_switch_conflicts_with_registered_chat(self):
        other = "/Users/example/.claude/projects/work/" + OTHER_CHAT + ".jsonl"
        adapter = CliRecoveryAdapter(runner=ProcessEvidenceRunner(opened=other))
        adapter.register(
            "s1",
            cli="claude",
            pid=PID,
            process_start=START,
            chat_uuid=CHAT,
        )

        probe = adapter.observe("s1", process_start=START).as_probe()

        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_AMBIGUOUS")
        self.assertIn("disagrees", probe["evidence_gap"])

    def test_activity_claim_never_becomes_idle_without_live_event(self):
        transcript = "/Users/example/.claude/projects/work/" + CHAT + ".jsonl"
        adapter = CliRecoveryAdapter(
            runner=ProcessEvidenceRunner(opened=transcript)
        )
        adapter.register(
            "s1",
            cli="claude",
            pid=PID,
            process_start=START,
            chat_uuid=CHAT,
            activity="idle",
        )

        probe = adapter.observe("s1", process_start=START).as_probe()

        self.assertEqual(probe["state"], "unknown")
        self.assertEqual(probe["blocked_code"], "ACTIVITY_EVIDENCE_UNVERIFIED")
        self.assertIn("prompt or lifecycle event", probe["activity_evidence_gap"])

    def test_cursor_current_log_upgrades_registration_without_stale_cache(self):
        with tempfile.TemporaryDirectory(prefix="cursor-agent-logs-") as log_dir:
            log_path = Path(log_dir) / "session-test.log"
            log_path.write_bytes(
                b'{"event":"conversationClassification.init",'
                b'"conversationId":"' + CHAT.encode("ascii") + b'"}\n'
            )
            adapter = CliRecoveryAdapter(
                runner=ProcessEvidenceRunner(opened=str(log_path))
            )
            adapter.register(
                "s1",
                cli="cursor-agent",
                pid=PID,
                process_start=START,
                chat_uuid=CHAT,
            )

            probe = adapter.observe("s1", process_start=START).as_probe()

        self.assertNotIn("blocked_code", probe)
        self.assertEqual(probe["cli_sessions"][0]["identity_status"], "verified")
        self.assertEqual(probe["cli_sessions"][0]["identity_source"], "cursor_pid_log")

    def test_cursor_log_switch_conflicts_with_registered_uuid(self):
        with tempfile.TemporaryDirectory(prefix="cursor-agent-logs-") as log_dir:
            log_path = Path(log_dir) / "session-test.log"
            log_path.write_bytes(
                b'{"event":"conversationClassification.init",'
                b'"conversationId":"' + OTHER_CHAT.encode("ascii") + b'"}\n'
            )
            adapter = CliRecoveryAdapter(
                runner=ProcessEvidenceRunner(opened=str(log_path))
            )
            adapter.register(
                "s1",
                cli="cursor-agent",
                pid=PID,
                process_start=START,
                chat_uuid=CHAT,
            )

            probe = adapter.observe("s1", process_start=START).as_probe()

        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_AMBIGUOUS")
        self.assertEqual(probe["cli_sessions"], [])

    def test_exit_rechecks_current_artifact_when_normalized_probe_drops_status(self):
        transcript = "/Users/example/.claude/projects/work/" + CHAT + ".jsonl"
        adapter = CliRecoveryAdapter(
            runner=ProcessEvidenceRunner(opened=transcript)
        )
        adapter.register(
            "s1",
            cli="claude",
            pid=PID,
            process_start=START,
            chat_uuid=CHAT,
        )
        probe = adapter.observe("s1", process_start=START).as_probe()
        # ``closing._normalize_probe`` keeps the UUID but may drop this
        # adapter-only evidence quality field.
        probe["cli_sessions"] = [
            {key: value for key, value in item.items() if key != "identity_status"}
            for item in probe["cli_sessions"]
        ]
        probe["state"] = "busy"

        with mock.patch(
            "goals_iterm_mcp.cli_recovery._self_and_ancestors",
            return_value=frozenset(),
        ), mock.patch(
            "goals_iterm_mcp.cli_recovery._wait_process_scope_exit",
            return_value=True,
        ), mock.patch(
            "goals_iterm_mcp.cli_recovery._process_scope_exit_verified",
            return_value=True,
        ), mock.patch("goals_iterm_mcp.cli_recovery.os.kill") as kill:
            result = adapter.exit_session({"session_id": "s1"}, probe, force=True)

        self.assertEqual(result["cli_exit_status"], "exited")
        self.assertEqual(kill.call_args.args[1], signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
