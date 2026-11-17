"""Lifecycle invariants for passive CLI identity evidence.

These tests use only injected ``ps``/``lsof`` output.  They never launch a
CLI, attach to iTerm, or read a user's real transcript.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import signal

from goals_iterm_mcp.cli_recovery import CliRecoveryAdapter, _dsh_zstd_executable


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

    def test_post_sigint_verification_failure_raises_exit_outcome_unknown(self):
        from goals_iterm_mcp.cli_recovery import ExitOutcomeUnknown

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
        probe["state"] = "busy"

        with mock.patch(
            "goals_iterm_mcp.cli_recovery._self_and_ancestors",
            return_value=frozenset(),
        ), mock.patch(
            "goals_iterm_mcp.cli_recovery._wait_process_scope_exit",
            return_value=False,
        ), mock.patch("goals_iterm_mcp.cli_recovery.os.kill"):
            with self.assertRaises(ExitOutcomeUnknown) as ctx:
                adapter.exit_session({"session_id": "s1"}, probe, force=True)

        self.assertEqual(ctx.exception.side_effect_status, "unknown")

    def test_pre_sigint_refusal_marks_side_effect_not_attempted(self):
        adapter = CliRecoveryAdapter(runner=ProcessEvidenceRunner(opened=""))
        probe = {
            "state": "busy",
            "identity": None,
            "cli_sessions": [{"cli": "claude", "chat_uuid": CHAT, "identity_status": "verified"}],
        }
        with mock.patch("goals_iterm_mcp.cli_recovery.os.kill") as kill:
            with self.assertRaises(RuntimeError) as ctx:
                adapter.exit_session({"session_id": "s1"}, probe, force=True)

        kill.assert_not_called()
        self.assertEqual(getattr(ctx.exception, "side_effect_status", None), "not_attempted")

    def test_dsh_open_lock_with_active_subagent_filters_to_root(self):
        root_id = f"session-{CHAT}"
        sub_id = f"session-{OTHER_CHAT}"
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = Path(tmp) / ".dsh" / "sessions" / "--work--"
            root_dir = sessions_root / root_id
            sub_dir = sessions_root / sub_id
            root_dir.mkdir(parents=True)
            sub_dir.mkdir(parents=True)
            root_lock = root_dir / "session.lock"
            sub_lock = sub_dir / "session.lock"
            root_lock.write_text("", encoding="utf-8")
            sub_lock.write_text("", encoding="utf-8")

            root_header = json.dumps({"type": "session", "version": 4, "id": root_id}) + "\n"
            sub_header = (
                json.dumps(
                    {
                        "type": "session",
                        "version": 4,
                        "id": sub_id,
                        "origin": "subagent",
                        "parentSession": root_id,
                    }
                )
                + "\n"
            )
            zstd_exe = _dsh_zstd_executable()
            if zstd_exe is not None:
                root_zstd = root_dir / "session.v4.jsonl.zstd"
                sub_zstd = sub_dir / "session.v4.jsonl.zstd"
                subprocess.run(
                    [zstd_exe, "-q", "-f", "-", "-o", str(root_zstd)],
                    input=(root_header + '{"type":"message","text":"do not leak"}\n').encode(),
                    check=True,
                )
                subprocess.run(
                    [zstd_exe, "-q", "-f", "-", "-o", str(sub_zstd)],
                    input=sub_header.encode(),
                    check=True,
                )
            else:
                (root_dir / "session.v4.jsonl").write_text(root_header, encoding="utf-8")
                (sub_dir / "session.v4.jsonl").write_text(sub_header, encoding="utf-8")

            class MultiOpenRunner(ProcessEvidenceRunner):
                def __call__(self, argv, **kwargs):
                    if "-Fn" in argv:
                        return f"p{PID}\nn{root_lock}\nn{sub_lock}\n".encode()
                    return super().__call__(argv, **kwargs)

            adapter = CliRecoveryAdapter(home=tmp, runner=MultiOpenRunner())
            probe = adapter.observe(
                "s1", cli="dsh", pid=PID, process_start=START
            ).as_probe()

        self.assertIsNone(probe.get("blocked_code"))
        self.assertEqual(len(probe["cli_sessions"]), 1)
        self.assertEqual(probe["cli_sessions"][0]["chat_uuid"], root_id)
        self.assertEqual(probe["cli_sessions"][0]["identity_status"], "verified")
        self.assertEqual(probe["cli_sessions"][0]["persistence_status"], "saved")

    def test_dsh_two_non_subagent_open_locks_stay_ambiguous(self):
        root_a = f"session-{CHAT}"
        root_b = f"session-{OTHER_CHAT}"
        with tempfile.TemporaryDirectory() as tmp:
            sessions_root = Path(tmp) / ".dsh" / "sessions" / "--work--"
            dir_a = sessions_root / root_a
            dir_b = sessions_root / root_b
            dir_a.mkdir(parents=True)
            dir_b.mkdir(parents=True)
            lock_a = dir_a / "session.lock"
            lock_b = dir_b / "session.lock"
            lock_a.write_text("", encoding="utf-8")
            lock_b.write_text("", encoding="utf-8")
            (dir_a / "session.v4.jsonl").write_text(
                json.dumps({"type": "session", "version": 4, "id": root_a}) + "\n",
                encoding="utf-8",
            )
            (dir_b / "session.v4.jsonl").write_text(
                json.dumps({"type": "session", "version": 4, "id": root_b}) + "\n",
                encoding="utf-8",
            )

            class MultiRootRunner(ProcessEvidenceRunner):
                def __call__(self, argv, **kwargs):
                    if "-Fn" in argv:
                        return f"p{PID}\nn{lock_a}\nn{lock_b}\n".encode()
                    return super().__call__(argv, **kwargs)

            probe = CliRecoveryAdapter(home=tmp, runner=MultiRootRunner()).observe(
                "s1", cli="dsh", pid=PID, process_start=START
            ).as_probe()

        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_AMBIGUOUS")
        self.assertEqual(probe["cli_sessions"], [])

    def test_dsh_open_lock_without_log_has_empty_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / ".dsh" / "sessions" / "_no-cwd" / CHAT
            session_dir.mkdir(parents=True)
            lock_path = session_dir / "session.lock"
            lock_path.write_text("", encoding="utf-8")

            probe = CliRecoveryAdapter(
                home=tmp, runner=ProcessEvidenceRunner(opened=str(lock_path))
            ).observe("s1", cli="dsh-tui", pid=PID, process_start=START).as_probe()

        self.assertIsNone(probe.get("blocked_code"))
        self.assertEqual(probe["cli_sessions"][0]["chat_uuid"], CHAT)
        self.assertEqual(probe["cli_sessions"][0]["persistence_status"], "empty")

    def test_dsh_tui_wrapper_chain_collapses_without_ambiguity(self):
        shell_pid = 100
        tui_pid = 101
        dsh_pid = 102
        tui_start = "Mon Oct 20 10:00:00 2026"
        dsh_start = "Mon Oct 20 10:00:01 2026"
        lock_path = f"/Users/example/.dsh/sessions/--work--/session-{CHAT}/session.lock"

        def runner(argv, **_kwargs):
            if "pid=,lstart=,state=" in argv:
                target_pid = int(argv[argv.index("-p") + 1])
                start = dsh_start if target_pid == dsh_pid else tui_start
                return f"{target_pid} {start} S".encode()
            if "command=" in argv:
                target_pid = int(argv[argv.index("-p") + 1])
                if target_pid == dsh_pid:
                    return b"node /opt/homebrew/bin/dsh --profile dsh-tui"
                return b"node /opt/homebrew/bin/dsh-tui"
            if "-Fn" in argv:
                target_pid = int(argv[argv.index("-p") + 1])
                if target_pid == dsh_pid:
                    return f"p{dsh_pid}\nn{lock_path}\n".encode()
                return b""
            return b""

        adapter = CliRecoveryAdapter(runner=runner)
        process = {
            "identity": {"pid": shell_pid, "start": "shell-start"},
            "active_jobs": [
                {
                    "pid": tui_pid,
                    "ppid": shell_pid,
                    "executable": "dsh-tui",
                    "identity": {"pid": tui_pid, "start": tui_start},
                },
                {
                    "pid": dsh_pid,
                    "ppid": tui_pid,
                    "executable": "dsh",
                    "identity": {"pid": dsh_pid, "start": dsh_start},
                },
            ],
        }
        with mock.patch("goals_iterm_mcp.cli_recovery._pid_alive", return_value=True):
            probe = adapter.probe({"session_id": "s1"}, process=process).as_probe()

        self.assertIsNone(probe.get("blocked_code"))
        self.assertEqual(probe["identity"]["pid"], dsh_pid)
        self.assertEqual(probe["identity"]["start"], dsh_start)
        self.assertEqual(probe["cli_sessions"][0]["chat_uuid"], f"session-{CHAT}")
        self.assertEqual(
            probe["cli_sessions"][0]["resume"],
            {"executable": "dsh", "argv": ["--profile", "dsh-tui", "--resume", f"session-{CHAT}"]},
        )

    def test_dsh_sibling_processes_remain_ambiguous(self):
        process = {
            "identity": {"pid": 100, "start": "shell-start"},
            "active_jobs": [
                {
                    "pid": 101,
                    "ppid": 100,
                    "executable": "dsh",
                    "identity": {"pid": 101, "start": "s-101"},
                },
                {
                    "pid": 102,
                    "ppid": 100,
                    "executable": "dsh",
                    "identity": {"pid": 102, "start": "s-102"},
                },
            ],
        }
        probe = CliRecoveryAdapter(runner=ProcessEvidenceRunner()).probe(
            {"session_id": "s1"}, process=process
        ).as_probe()

        self.assertEqual(probe["blocked_code"], "CLI_IDENTITY_AMBIGUOUS")
        self.assertEqual(probe["cli_sessions"], [])

    def test_dsh_exit_session_signals_verified_process(self):
        session_id = f"session-{CHAT}"
        lock_path = f"/Users/example/.dsh/sessions/--work--/{session_id}/session.lock"
        adapter = CliRecoveryAdapter(runner=ProcessEvidenceRunner(opened=lock_path))
        adapter.register(
            "s1",
            cli="dsh",
            pid=PID,
            process_start=START,
            chat_uuid=session_id,
        )
        probe = adapter.observe("s1", process_start=START).as_probe()
        probe["state"] = "busy"

        with (
            mock.patch("goals_iterm_mcp.cli_recovery._self_and_ancestors", return_value=frozenset()),
            mock.patch("goals_iterm_mcp.cli_recovery._wait_process_scope_exit", return_value=True),
            mock.patch("goals_iterm_mcp.cli_recovery._process_scope_exit_verified", return_value=True),
            mock.patch("goals_iterm_mcp.cli_recovery.os.kill") as kill,
        ):
            result = adapter.exit_session({"session_id": "s1"}, probe, force=True)

        self.assertEqual(result["cli_exit_status"], "exited")
        self.assertEqual(kill.call_args.args, (PID, signal.SIGINT))


if __name__ == "__main__":
    unittest.main()
