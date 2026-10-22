"""Focused passive CLI identity tests; no live terminal input is used."""

from __future__ import annotations

import json
import signal
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from goals_iterm_mcp.cli_recovery import CliRecoveryAdapter


PID = __import__("os").getpid()
CHAT = "12345678-1234-4234-8234-1234567890ab"
OTHER_CHAT = "87654321-4321-4321-8123-ba0987654321"


class CliRecoveryTests(unittest.TestCase):
    def test_registered_chat_has_cli_specific_resume_argv(self):
        adapter = CliRecoveryAdapter()
        adapter.register(
            "s1", cli="codex", pid=PID, process_start="start-1", chat_uuid=CHAT
        )

        probe = adapter.observe("s1", process_start="start-1").as_probe()

        self.assertEqual(probe["cli_sessions"][0]["cli"], "codex")
        self.assertEqual(
            probe["cli_sessions"][0]["resume"],
            {"executable": "codex", "argv": ["resume", CHAT]},
        )

    def test_unregistered_claude_is_blocked(self):
        adapter = CliRecoveryAdapter()

        probe = adapter.observe("s1", cli="claude", pid=PID).as_probe()

        self.assertEqual(probe["cli_sessions"], [])
        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_MISSING")

    def test_explicit_resume_uuid_is_read_from_current_process_only(self):
        def runner(argv, **_kwargs):
            if "command=" in argv:
                return f"claude --resume {CHAT}".encode()
            return b""

        adapter = CliRecoveryAdapter(runner=runner)
        probe = adapter.observe(
            "s1", cli="claude", pid=PID, process_start="start-1"
        ).as_probe()

        self.assertEqual(probe["cli_sessions"][0]["chat_uuid"], CHAT)
        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_STARTUP_ONLY")

    def test_passive_uuid_without_process_start_cannot_be_interrupted(self):
        def runner(argv, **_kwargs):
            if "command=" in argv:
                return f"claude --resume {CHAT}".encode()
            return b""

        adapter = CliRecoveryAdapter(runner=runner)
        probe = adapter.observe("s1", cli="claude", pid=PID).as_probe()

        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_STARTUP_ONLY")

    def test_argv_uuid_conflicting_with_open_claude_transcript_is_blocked(self):
        transcript = "/Users/example/.claude/projects/work/" + OTHER_CHAT + ".jsonl"

        def runner(argv, **_kwargs):
            if "command=" in argv:
                return f"claude --resume {CHAT}".encode()
            if "-Fn" in argv:
                return f"p{PID}\nn{transcript}\n".encode()
            return b""

        adapter = CliRecoveryAdapter(runner=runner)
        probe = adapter.observe(
            "s1", cli="claude", pid=PID, process_start="start-1"
        ).as_probe()

        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_AMBIGUOUS")
        self.assertEqual(probe["cli_sessions"], [])

    def test_opened_codex_rollout_is_process_bound(self):
        rollout = "/Users/example/.codex/sessions/2026/10/22/rollout-" + CHAT + ".jsonl"

        def runner(argv, **_kwargs):
            if "-Fn" in argv:
                return f"p{PID}\nn{rollout}\n".encode()
            return b""

        adapter = CliRecoveryAdapter(runner=runner)
        probe = adapter.observe(
            "s1", cli="codex", pid=PID, process_start="start-1"
        ).as_probe()

        self.assertEqual(probe["cli_sessions"][0]["chat_uuid"], CHAT)
        self.assertEqual(
            probe["cli_sessions"][0]["identity_source"], "process_open_file"
        )

    def test_cursor_argv_and_current_log_must_agree(self):
        with tempfile.TemporaryDirectory(prefix="cursor-agent-logs-") as log_dir:
            log_path = Path(log_dir) / "session-test.log"
            log_path.write_bytes(
                b'{"event":"conversationClassification.init",'
                b'"conversationId":"' + OTHER_CHAT.encode("ascii") + b'"}\n'
            )

            def runner(argv, **_kwargs):
                if "command=" in argv:
                    return f"cursor-agent --resume {CHAT}".encode()
                return f"p{PID}\nn{log_path}\n".encode()

            adapter = CliRecoveryAdapter(runner=runner)
            probe = adapter.observe(
                "s1", cli="cursor-agent", pid=PID, process_start="start-1"
            ).as_probe()

        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_AMBIGUOUS")
        self.assertEqual(probe["cli_sessions"], [])

    def test_cursor_matching_current_log_upgrades_startup_argv_evidence(self):
        with tempfile.TemporaryDirectory(prefix="cursor-agent-logs-") as log_dir:
            log_path = Path(log_dir) / "session-test.log"
            log_path.write_bytes(
                b'{"event":"conversationClassification.init",'
                b'"conversationId":"' + CHAT.encode("ascii") + b'"}\n'
            )

            def runner(argv, **_kwargs):
                if "command=" in argv:
                    return f"cursor-agent --resume {CHAT}".encode()
                return f"p{PID}\nn{log_path}\n".encode()

            adapter = CliRecoveryAdapter(runner=runner)
            probe = adapter.observe(
                "s1", cli="cursor-agent", pid=PID, process_start="start-1"
            ).as_probe()

        self.assertNotIn("blocked_code", probe)
        self.assertEqual(
            probe["cli_sessions"][0]["identity_source"], "cursor_pid_log"
        )

    def test_cursor_startup_argv_without_current_log_is_reported_but_blocked(self):
        def runner(argv, **_kwargs):
            if "command=" in argv:
                return f"cursor-agent --resume {CHAT}".encode()
            return b""

        adapter = CliRecoveryAdapter(runner=runner)
        probe = adapter.observe(
            "s1", cli="cursor-agent", pid=PID, process_start="start-1"
        ).as_probe()

        self.assertEqual(probe["cli_sessions"][0]["chat_uuid"], CHAT)
        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_STARTUP_ONLY")

    def test_registered_chat_requires_matching_process_start(self):
        adapter = CliRecoveryAdapter()
        adapter.register(
            "s1", cli="claude", pid=PID, process_start="start-1", chat_uuid=CHAT
        )

        probe = adapter.observe("s1", process_start="start-2").as_probe()

        self.assertEqual(probe["blocked_code"], "PROCESS_IDENTITY_CHANGED")

    def test_unknown_coding_cli_can_be_explicitly_blocked(self):
        adapter = CliRecoveryAdapter()

        probe = adapter.observe(
            "s1", cli="new-agent", pid=PID, recognized_cli=True
        ).as_probe()

        self.assertEqual(probe["blocked_code"], "CLI_UNSUPPORTED")

    def test_cursor_id_must_come_from_the_supplied_pid_log(self):
        with tempfile.TemporaryDirectory(prefix="cursor-agent-logs-") as log_dir:
            with tempfile.TemporaryDirectory() as home_dir:
                log_path = Path(log_dir) / "session-test.log"
                log_path.write_bytes(
                    b'{"event":"conversationClassification.init",'
                    b'"conversationId":"' + CHAT.encode("ascii") + b'"}\n'
                )
                meta_dir = Path(home_dir) / ".cursor" / "acp-sessions" / CHAT
                meta_dir.mkdir(parents=True)
                (meta_dir / "meta.json").write_text(
                    json.dumps({"schemaVersion": 1, "cwd": "/tmp/work", "title": "safe"}),
                    encoding="utf-8",
                )

                def runner(argv, **_kwargs):
                    self.assertEqual(argv[1:3], ("-a", "-p"))
                    return f"p{PID}\nn{log_path}\n".encode()

                adapter = CliRecoveryAdapter(home=home_dir, runner=runner)
                probe = adapter.observe(
                    "s1", cli="cursor-agent", pid=PID, cwd="/tmp/work", process_start="start-1"
                ).as_probe()

        self.assertEqual(probe["cli_sessions"][0]["chat_uuid"], CHAT)
        self.assertEqual(probe["cli_sessions"][0]["identity_source"], "cursor_pid_log")

    def test_kiro_listing_is_not_identity_binding(self):
        def runner(_argv, **_kwargs):
            return json.dumps(
                [{"session_id": str(uuid.uuid4()), "status": "in_progress"}]
            ).encode()

        adapter = CliRecoveryAdapter(runner=runner)
        probe = adapter.observe("s1", cli="kiro-cli", pid=PID).as_probe()

        self.assertEqual(probe["cli_sessions"], [])
        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_MISSING")

    def test_exit_verifies_observed_process_scope(self):
        main_pid = 12345
        child_pid = 12346
        main_start = "Mon Oct 20 10:00:00 2026"
        child_start = "Tue Oct 21 10:00:00 2026"
        signals = []

        def runner(argv, **_kwargs):
            if "ppid=" in argv:
                return b"0"
            if "pid=,lstart=,state=" in argv:
                pid = int(argv[argv.index("-p") + 1])
                state = "Z" if signals else "R"
                start = main_start if pid == main_pid else child_start
                return f"{pid} {start} {state}".encode()
            return b""

        def fake_kill(pid, value):
            signals.append((pid, value))

        adapter = CliRecoveryAdapter(runner=runner)
        probe = {
            "state": "busy",
            "identity": {"pid": main_pid, "start": main_start},
            "cli_sessions": [{"chat_uuid": CHAT}],
            "process": {
                "processes": [
                    {"identity": {"pid": main_pid, "start": main_start}},
                    {"identity": {"pid": child_pid, "start": child_start}},
                ]
            },
        }

        with mock.patch("goals_iterm_mcp.cli_recovery.os.kill", side_effect=fake_kill):
            result = adapter.exit_session({"session_id": "s1"}, probe, force=True)

        self.assertEqual(signals, [(main_pid, signal.SIGINT)])
        self.assertEqual(
            result["verified_processes"],
            [
                {"pid": main_pid, "start": main_start, "start_time": main_start},
                {"pid": child_pid, "start": child_start, "start_time": child_start},
            ],
        )

    def test_exit_refuses_when_observed_child_survives_main_cli(self):
        main_pid = 12345
        child_pid = 12346
        main_start = "Mon Oct 20 10:00:00 2026"
        child_start = "Tue Oct 21 10:00:00 2026"
        signals = []

        def runner(argv, **_kwargs):
            if "ppid=" in argv:
                return b"0"
            if "pid=,lstart=,state=" in argv:
                pid = int(argv[argv.index("-p") + 1])
                state = "Z" if pid == main_pid and signals else "R"
                start = main_start if pid == main_pid else child_start
                return f"{pid} {start} {state}".encode()
            return b""

        def fake_kill(pid, value):
            signals.append((pid, value))

        adapter = CliRecoveryAdapter(runner=runner)
        probe = {
            "state": "busy",
            "identity": {"pid": main_pid, "start": main_start},
            "cli_sessions": [{"chat_uuid": CHAT}],
            "process": {
                "processes": [
                    {"identity": {"pid": main_pid, "start": main_start}},
                    {"identity": {"pid": child_pid, "start": child_start}},
                ]
            },
        }

        with (
            mock.patch("goals_iterm_mcp.cli_recovery.os.kill", side_effect=fake_kill),
            mock.patch(
                "goals_iterm_mcp.cli_recovery._wait_process_scope_exit",
                return_value=False,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "CLI descendant remained"):
                adapter.exit_session({"session_id": "s1"}, probe, force=True)

        self.assertEqual(signals, [(main_pid, signal.SIGINT)])


if __name__ == "__main__":
    unittest.main()
