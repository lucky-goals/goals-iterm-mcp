"""Passive chat identity and activity evidence for supported coding CLIs.

This module is deliberately smaller than a CLI process manager.  A terminal
close operation needs two different kinds of evidence:

* the iTerm session and the process that is running in it must already be
  bound to one another; and
* an interrupted coding CLI must have a chat identifier that belongs to that
  process.

The first item is supplied by the closing backend.  ``CliRecoveryAdapter``
keeps an optional, exact iTerm-session binding and adds the few passive
discovery paths that are safe enough to use without writing to a running
terminal.  It never sends slash commands, reads terminal contents, chooses a
session by cwd/mtime, or returns a saved-session list as proof of the current
process.

The adapter is intentionally conservative.  Claude Code, Codex, and Kiro do
not expose a reliable, process-bound foreground chat id through their
installed CLIs.  An explicit startup UUID is retained for reporting but is
blocked unless a current process-opened transcript/rollout agrees with it.
Cursor Agent has a passive process-specific log path on the installed
versions; its conversation id is accepted only when it is found in a log file
opened by the supplied PID, and a conflicting startup UUID is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import time
from typing import Any, Literal
import uuid


SUPPORTED_CLIS = frozenset({"claude", "codex", "cursor", "kiro"})
Activity = Literal["idle", "busy", "waiting", "unknown", "exited"]

# Cursor's process log is not a transcript API.  Keep reads bounded and retain
# only the conversationId from a classification-init line.  In particular,
# do not parse arbitrary JSON log fields, which may contain prompts or tokens.
DEFAULT_CURSOR_LOG_BYTES = 256 * 1024
DEFAULT_CURSOR_LOG_LINES = 4096
DEFAULT_KIRO_OUTPUT_BYTES = 512 * 1024
DEFAULT_KIRO_RECORDS = 2048
DEFAULT_PROCESS_ARG_BYTES = 64 * 1024

_UUID_RE = re.compile(
    rb"\"conversationId\"\s*:\s*\"([0-9a-fA-F]{8}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{12})\""
)
_GENERIC_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_UUID_BYTES_RE = re.compile(
    rb"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def normalize_cli(value: Any) -> str | None:
    """Map executable/context names to the four supported CLI names."""

    if not isinstance(value, str):
        return None
    name = Path(value).name.casefold()
    if name in {"claude", "claude.exe"}:
        return "claude"
    if name in {"codex", "codex.exe", "codex-cli"}:
        return "codex"
    if name in {"cursor", "cursor.exe", "cursor-agent", "cursor-agent.exe", "agent"}:
        return "cursor"
    if name in {"kiro", "kiro.exe", "kiro-cli", "kiro-cli.exe"}:
        return "kiro"
    return None


def _valid_uuid(value: Any) -> str | None:
    if not isinstance(value, str) or not _GENERIC_ID_RE.fullmatch(value):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


def _safe_cwd(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    return value


def _process_identity(pid: int | None, process_start: str | None) -> dict[str, Any] | None:
    if pid is None or type(pid) is not int or pid <= 0:
        return None
    result: dict[str, Any] = {"pid": pid}
    if isinstance(process_start, str) and process_start:
        result["process_start"] = process_start
        # ClosingBackend's stable identity normalizer accepts the shared
        # start/start_time spellings.  Keep process_start as an explicit
        # provenance alias while exposing both compatibility keys.
        result["start"] = process_start
        result["start_time"] = process_start
    return result


def _with_process_identity(
    observation: "Observation", pid: int, process_start: str | None
) -> "Observation":
    identity = dict(observation.identity or {})
    identity.update(_process_identity(pid, process_start) or {})
    return replace(observation, identity=identity)


def _require_process_start(
    observation: "Observation", process_start: str | None
) -> "Observation":
    if observation.cli_sessions and (
        not isinstance(process_start, str) or not process_start
    ):
        return replace(
            observation,
            reason="chat UUID found but current process start token is unavailable",
            blocked_code=observation.blocked_code or "PROCESS_IDENTITY_MISSING",
        )
    return observation


@dataclass(frozen=True, slots=True)
class ChatEvidence:
    """A chat id that can be written to a recovery receipt.

    ``identity_source`` describes how the id was tied to the current process;
    it is not a claim that the CLI has durable history.  ``persistence_status``
    is deliberately an opaque, small value supplied by an adapter.
    """

    cli: str
    chat_uuid: str
    cwd: str | None = None
    identity_source: str = "registered_process"
    persistence_status: str | None = None
    resume_executable: str | None = None
    resume_argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        cli = normalize_cli(self.cli)
        if cli is None:
            raise ValueError("unsupported CLI")
        chat_uuid = _valid_uuid(self.chat_uuid)
        if chat_uuid is None:
            raise ValueError("chat_uuid must be a UUID")
        object.__setattr__(self, "cli", cli)
        object.__setattr__(self, "chat_uuid", chat_uuid)
        if self.cwd is not None and _safe_cwd(self.cwd) is None:
            raise ValueError("cwd must be a non-empty path")
        if not isinstance(self.identity_source, str) or not self.identity_source:
            raise ValueError("identity_source must be non-empty")
        if self.persistence_status is not None and not isinstance(
            self.persistence_status, str
        ):
            raise ValueError("persistence_status must be a string or null")
        if self.resume_executable is None:
            object.__setattr__(self, "resume_executable", self.cli)
        if not isinstance(self.resume_executable, str) or not self.resume_executable:
            raise ValueError("resume_executable must be non-empty")
        if not isinstance(self.resume_argv, tuple) or not all(
            isinstance(item, str) for item in self.resume_argv
        ):
            raise ValueError("resume_argv must contain strings")

    def as_record(self) -> dict[str, Any]:
        """Return the metadata shape consumed by the closing service."""

        return {
            "cli": self.cli,
            "chat_uuid": self.chat_uuid,
            "cwd": self.cwd,
            "identity_source": self.identity_source,
            "persistence_status": self.persistence_status,
            "resume": {
                "executable": self.resume_executable,
                "argv": list(self.resume_argv),
            },
        }


@dataclass(frozen=True, slots=True)
class ProcessBinding:
    """An exact process/chat binding registered by the caller.

    The key is the iTerm session id held by the closing backend.  A binding is
    never looked up by cwd, title, or the newest file in a directory.
    """

    session_id: str
    cli: str
    pid: int
    chat: ChatEvidence | None = None
    cwd: str | None = None
    process_start: str | None = None
    activity: Activity = "unknown"
    activity_reason: str = "no passive lifecycle signal"

    def __post_init__(self) -> None:
        cli = normalize_cli(self.cli)
        if cli is None:
            raise ValueError("unsupported CLI")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be non-empty")
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("pid must be a positive integer")
        if self.cwd is not None and _safe_cwd(self.cwd) is None:
            raise ValueError("cwd must be a non-empty path")
        if self.process_start is not None and (
            not isinstance(self.process_start, str) or not self.process_start
        ):
            raise ValueError("process_start must be a non-empty opaque token")
        if self.chat is not None and self.process_start is None:
            raise ValueError("chat binding requires a stable process_start")
        if self.activity not in {"idle", "busy", "waiting", "unknown", "exited"}:
            raise ValueError("invalid activity state")
        object.__setattr__(self, "cli", cli)


@dataclass(frozen=True, slots=True)
class Observation:
    """Passive evidence returned by :meth:`CliRecoveryAdapter.observe`."""

    state: Activity
    reason: str
    identity: Mapping[str, Any] | None
    cli_sessions: tuple[ChatEvidence, ...] = ()
    blocked_code: str | None = None
    source: str | None = None

    def as_probe(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state,
            "reason": self.reason,
            "identity": dict(self.identity) if self.identity is not None else None,
            "cli_sessions": [item.as_record() for item in self.cli_sessions],
        }
        if self.blocked_code:
            result["blocked_code"] = self.blocked_code
        if self.source:
            result["identity_source"] = self.source
        return result


class _Runner:
    """Tiny subprocess seam used by Kiro listing and Cursor lsof lookup."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        timeout: float = 1.5,
    ) -> bytes:
        raise NotImplementedError


def _default_runner(
    argv: Sequence[str], *, cwd: str | None = None, timeout: float = 1.5
) -> bytes:
    """Run a fixed argv without a shell and return bounded stdout only."""

    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    return completed.stdout[:DEFAULT_KIRO_OUTPUT_BYTES]


def _pid_alive(pid: int) -> bool:
    """Return liveness without reading command lines or process environments."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A process can be alive while this user cannot signal it.  Keep the
        # result conservative; callers must not treat this as an exit proof.
        return True
    except OSError:
        return False
    return True


def _process_start_and_state(
    pid: int, runner: Callable[..., bytes]
) -> tuple[str | None, str | None]:
    """Read only ``pid/lstart/state`` for one PID, never command arguments."""

    executable = shutil.which("ps") or "/bin/ps"
    try:
        raw = runner(
            (executable, "-p", str(pid), "-o", "pid=,lstart=,state="),
            timeout=1.0,
        )[:4096]
    except Exception:
        return None, None
    try:
        line = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        return None, None
    parts = line.split()
    if len(parts) < 7:
        return None, None
    try:
        if int(parts[0]) != pid:
            return None, None
    except ValueError:
        return None, None
    # macOS lstart is five tokens after the PID; the final token is state.
    return " ".join(parts[1:6]), parts[6]


def _self_and_ancestors(runner: Callable[..., bytes]) -> frozenset[int]:
    """Return local process ancestors without consulting command lines."""

    result: set[int] = set()
    current = os.getpid()
    while current > 0 and current not in result:
        result.add(current)
        try:
            executable = shutil.which("ps") or "/bin/ps"
            raw = runner((executable, "-p", str(current), "-o", "ppid="), timeout=1.0)
            parent = int(raw.decode("ascii", errors="strict").strip().split()[0])
        except (OSError, TypeError, ValueError, UnicodeError):
            break
        current = parent if parent != current else 0
    return frozenset(result)


def _wait_process_exit(
    pid: int,
    expected_start: str,
    runner: Callable[..., bytes],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        start, state = _process_start_and_state(pid, runner)
        if start is None:
            # ``ps`` can race process reaping.  A direct liveness check is
            # only a fallback; it never turns a changed PID into success.
            if not _pid_alive(pid):
                return True
        elif start != expected_start:
            return True
        elif state and state[0].upper() in {"Z", "X"}:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _process_exit_verified(
    pid: int, expected_start: str, runner: Callable[..., bytes]
) -> bool:
    """Distinguish a gone process from a changed/reused PID after a wait."""

    start, state = _process_start_and_state(pid, runner)
    if start is None:
        return not _pid_alive(pid)
    if start != expected_start:
        return False
    return bool(state and state[0].upper() in {"Z", "X"})


def _stable_process_pair(value: Any) -> tuple[int, str] | None:
    """Extract one stable ``(pid, start)`` pair from allowlisted process data."""

    if not isinstance(value, Mapping):
        return None
    nested = value.get("identity")
    candidate = nested if isinstance(nested, Mapping) else value
    raw_pid = candidate.get("pid")
    if isinstance(raw_pid, bool):
        return None
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    raw_start = candidate.get(
        "start_time",
        candidate.get(
            "start",
            candidate.get("starttime", candidate.get("startTime", candidate.get("process_start"))),
        ),
    )
    if raw_start in (None, "") or not isinstance(raw_start, (str, int)):
        return None
    start = str(raw_start)
    return (pid, start) if start else None


def _probe_process_scope(
    probe: Mapping[str, Any],
) -> tuple[tuple[int, str], ...] | None:
    """Return the pre-signal process identity set carried by a probe.

    The process provider's ``process.processes`` list and its active/stopped
    lists are combined (duplicates are removed).  A present but malformed
    entry makes the scope unavailable rather than allowing a main-PID-only
    exit that could strand a reparented child.
    """

    main = _stable_process_pair(probe.get("identity"))
    if main is None:
        return None
    process = probe.get("process")
    if not isinstance(process, Mapping):
        return (main,)
    entries: list[Any] = []
    for key in ("process_identities", "processes", "active_jobs", "stopped_jobs"):
        value = process.get(key)
        if value is None:
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            entries.extend(value)
        else:
            return None
    pairs: list[tuple[int, str]] = [main]
    for entry in entries:
        pair = _stable_process_pair(entry)
        if pair is None:
            return None
        if pair not in pairs:
            pairs.append(pair)
    return tuple(pairs)


def _process_scope_current(
    scope: Sequence[tuple[int, str]], runner: Callable[..., bytes]
) -> bool:
    """Reject PID reuse or missing evidence before delivering SIGINT."""

    for pair in scope:
        start, state = _process_start_and_state(pair[0], runner)
        if start is None:
            # A process that disappeared before the signal is harmless; an
            # alive PID with unavailable identity is unsafe to classify.
            if _pid_alive(pair[0]):
                return False
            continue
        if start != pair[1]:
            return False
        if state and state[0].upper() in {"Z", "X"}:
            continue
    return True


def _process_scope_exit_verified(
    scope: Sequence[tuple[int, str]], runner: Callable[..., bytes]
) -> bool:
    return all(_process_exit_verified(pid, start, runner) for pid, start in scope)


def _wait_process_scope_exit(
    scope: Sequence[tuple[int, str]],
    runner: Callable[..., bytes],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _process_scope_exit_verified(scope, runner):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _opened_paths(pid: int, runner: Callable[..., bytes]) -> tuple[Path, ...]:
    """Return paths currently opened by exactly ``pid``.

    ``lsof`` is queried in field mode and only name fields are consumed.  No
    command-line or environment field is requested.  Callers still filter
    the result to their CLI's known storage roots before using a path.
    """

    if type(pid) is not int or pid <= 0:
        return ()
    executable = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        output = runner((executable, "-a", "-p", str(pid), "-Fn"), timeout=1.0)
    except Exception:
        return ()
    paths: list[Path] = []
    for raw in output.splitlines():
        if not raw.startswith(b"n"):
            continue
        try:
            candidate = Path(os.fsdecode(raw[1:]))
        except (OSError, ValueError):
            continue
        paths.append(candidate)
    return tuple(dict.fromkeys(paths))


def _cursor_log_paths(pid: int, runner: Callable[..., bytes]) -> tuple[Path, ...]:
    """Find Cursor log files currently opened by exactly ``pid``."""

    return tuple(
        candidate
        for candidate in _opened_paths(pid, runner)
        if candidate.name.startswith("session-")
        and candidate.name.endswith(".log")
        # Cursor uses a temporary directory for these logs.  Reject arbitrary
        # user files even if they happen to have the same basename.
        and "cursor-agent-logs-" in str(candidate.parent)
    )


def _process_command_uuid(
    pid: int,
    cli: str,
    runner: Callable[..., bytes],
) -> str | None:
    """Extract one explicit UUID flag from a process command line.

    The command line is read into a bounded local buffer solely to match the
    documented UUID-bearing flags.  The raw command is never returned,
    logged, or included in an error.  A missing or ambiguous flag returns
    ``None`` so the caller can try a process-opened metadata path.
    """

    executable = shutil.which("ps") or "/bin/ps"
    try:
        raw = runner(
            (executable, "-p", str(pid), "-o", "command="),
            timeout=1.0,
        )[:DEFAULT_PROCESS_ARG_BYTES]
    except Exception:
        return None
    normalized = normalize_cli(cli)
    if normalized == "claude":
        flags = (b"--session-id", b"--resume")
    elif normalized == "codex":
        flags = (b"resume", b"--session-id")
    elif normalized == "cursor":
        flags = (b"--resume",)
    elif normalized == "kiro":
        flags = (b"--resume-id", b"--resume")
    else:
        return None

    matches: list[str] = []
    for flag in flags:
        # A UUID may be supplied as ``--flag UUID`` or ``--flag=UUID``.  The
        # token boundaries avoid matching a UUID embedded in a credential.
        pattern = re.compile(
            rb"(?:^|[\x00\t\n\r ])" + re.escape(flag) + rb"(?:=|[\t ]+)" + _UUID_BYTES_RE.pattern
        )
        for match in pattern.finditer(raw):
            try:
                candidate = match.group(1).decode("ascii")
            except UnicodeDecodeError:
                continue
            value = _valid_uuid(candidate)
            if value is not None and value not in matches:
                matches.append(value)
    return matches[0] if len(matches) == 1 else None


def _opened_uuid_paths(
    pid: int,
    cli: str,
    runner: Callable[..., bytes],
) -> tuple[str, ...]:
    """Extract UUIDs from process-opened CLI metadata paths only."""

    normalized = normalize_cli(cli)
    if normalized not in {"claude", "codex"}:
        return ()
    values: list[str] = []
    for candidate in _opened_paths(pid, runner):
        if normalized == "claude":
            # Claude transcript filenames are UUID.jsonl beneath
            # ~/.claude/projects.  We use the open path as the process
            # binding and never read the JSONL body.
            if ".claude" not in candidate.parts or candidate.suffix != ".jsonl":
                continue
            if not any(part == "projects" for part in candidate.parts):
                continue
            value = _valid_uuid(candidate.stem)
        else:
            # Codex rollout files are named rollout-<uuid>.jsonl.zst beneath
            # ~/.codex/sessions.  A live process may have no rollout yet.
            if ".codex" not in candidate.parts or "sessions" not in candidate.parts:
                continue
            if not candidate.name.startswith("rollout-"):
                continue
            stem = candidate.name.removeprefix("rollout-")
            if stem.endswith(".jsonl.zst"):
                stem = stem[: -len(".jsonl.zst")]
            elif stem.endswith(".jsonl"):
                stem = stem[: -len(".jsonl")]
            else:
                continue
            value = _valid_uuid(stem)
        if value is not None and value not in values:
            values.append(value)
    return tuple(values)


def _resume_spec(cli: str, chat_uuid: str) -> tuple[str, tuple[str, ...]]:
    return {
        "claude": ("claude", ("--resume", chat_uuid)),
        "codex": ("codex", ("resume", chat_uuid)),
        "cursor": ("cursor-agent", ("--resume", chat_uuid)),
        "kiro": ("kiro-cli", ("chat", "--resume-id", chat_uuid)),
    }[cli]


def _passive_process_chat(
    *,
    cli: str,
    pid: int,
    cwd: str | None,
    runner: Callable[..., bytes],
) -> tuple[ChatEvidence | None, str | None]:
    """Find one UUID from current process metadata.

    An argv UUID identifies how the process started, but interactive CLIs can
    switch conversations in the same process.  A process-opened transcript or
    rollout is stronger current-session evidence.  argv-only therefore returns
    a chat record with ``CLI_IDENTITY_STARTUP_ONLY`` so callers can report the
    startup UUID without permitting interruption.
    """

    normalized = normalize_cli(cli)
    if normalized is None:
        return None, None
    candidates: list[tuple[str, str]] = []
    explicit = _process_command_uuid(pid, normalized, runner)
    opened = _opened_uuid_paths(pid, normalized, runner)
    if explicit is not None:
        candidates.append((explicit, "process_argv"))
    candidates.extend((value, "process_open_file") for value in opened)
    unique = {value for value, _source in candidates}
    if len(unique) > 1:
        return None, "CLI_IDENTITY_AMBIGUOUS"
    if not unique:
        return None, None
    value = next(iter(unique))
    # A matching open file upgrades argv evidence to current-session evidence.
    source = "process_open_file" if opened else "process_argv_startup"
    executable, argv = _resume_spec(normalized, value)
    persistence = "saved" if opened else "unknown"
    blocked_code = None if opened else "CLI_IDENTITY_STARTUP_ONLY"
    return (
        ChatEvidence(
            normalized,
            value,
            cwd=cwd,
            identity_source=source,
            persistence_status=persistence,
            resume_executable=executable,
            resume_argv=argv,
        ),
        blocked_code,
    )


def _conversation_id_from_cursor_log(
    path: Path,
    *,
    max_bytes: int = DEFAULT_CURSOR_LOG_BYTES,
    max_lines: int = DEFAULT_CURSOR_LOG_LINES,
) -> str | None:
    """Extract only a UUID from Cursor's classification-init event.

    The scan is bounded and line-oriented.  Nonmatching lines are discarded
    immediately; no transcript or arbitrary JSON value is returned to callers.
    """

    if max_bytes <= 0 or max_lines <= 0:
        return None
    values = _conversation_ids_from_cursor_log(
        path, max_bytes=max_bytes, max_lines=max_lines
    )
    return values[0] if len(values) == 1 else None


def _conversation_ids_from_cursor_log(
    path: Path,
    *,
    max_bytes: int = DEFAULT_CURSOR_LOG_BYTES,
    max_lines: int = DEFAULT_CURSOR_LOG_LINES,
) -> tuple[str, ...]:
    """Return all distinct classification-init UUIDs in one Cursor log."""

    if max_bytes <= 0 or max_lines <= 0:
        return ()
    values: list[str] = []
    try:
        with path.open("rb") as stream:
            consumed = 0
            for _ in range(max_lines):
                line = stream.readline(min(16 * 1024, max_bytes - consumed + 1))
                if not line:
                    break
                consumed += len(line)
                if consumed > max_bytes:
                    break
                if b"conversationClassification.init" not in line:
                    continue
                match = _UUID_RE.search(line)
                if match is None:
                    continue
                try:
                    candidate = match.group(1).decode("ascii")
                except UnicodeDecodeError:
                    continue
                value = _valid_uuid(candidate)
                if value is not None and value not in values:
                    values.append(value)
    except (OSError, ValueError):
        return ()
    return tuple(values)


def _cursor_meta(uuid_value: str, cwd: str | None, home: Path) -> tuple[str | None, str | None]:
    """Read only whitelisted Cursor metadata for an already found UUID."""

    roots = [home / ".cursor" / "acp-sessions"]
    old_root = home / ".cursor" / "chats"
    try:
        old_parents = tuple(path for path in old_root.iterdir() if path.is_dir())
    except OSError:
        old_parents = ()
    roots.extend(old_parent for old_parent in old_parents)

    for root in roots:
        path = root / uuid_value / "meta.json"
        try:
            if path.is_symlink() or not path.is_file():
                continue
            # Cursor metadata is small; refusing oversized files prevents a
            # future format change from turning this into transcript access.
            if path.stat().st_size > 64 * 1024:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(raw, Mapping):
            continue
        meta_cwd = _safe_cwd(raw.get("cwd"))
        if cwd is not None and meta_cwd is not None:
            try:
                if Path(meta_cwd).resolve() != Path(cwd).resolve():
                    continue
            except OSError:
                if meta_cwd != cwd:
                    continue
        has_conversation = raw.get("hasConversation")
        if has_conversation is True:
            persistence = "saved"
        elif has_conversation is False:
            persistence = "empty"
        else:
            persistence = "unknown"
        return meta_cwd or cwd, persistence
    return cwd, None


def _cursor_observation(
    *,
    pid: int,
    cwd: str | None,
    home: Path,
    runner: Callable[..., bytes],
    supplied_chat: ChatEvidence | None = None,
    expected_chat_uuid: str | None = None,
    activity: Activity = "unknown",
    activity_reason: str | None = None,
) -> Observation:
    if not _pid_alive(pid):
        return Observation(
            "exited", "the bound Cursor process is no longer alive", {"pid": pid}, (), source="pid"
        )

    chat = supplied_chat
    source = "registered_process" if chat is not None else None
    log_path: Path | None = None
    found_values: list[tuple[str, Path]] = []
    for candidate in _cursor_log_paths(pid, runner):
        for found in _conversation_ids_from_cursor_log(candidate):
            if all(found != value for value, _ in found_values):
                found_values.append((found, candidate))
    if len(found_values) > 1:
        return Observation(
            activity,
            "Cursor process log contains multiple conversation UUIDs",
            {"pid": pid},
            (),
            blocked_code="CLI_IDENTITY_AMBIGUOUS",
            source="cursor_pid_log",
        )
    if found_values:
        found, log_path = found_values[0]
        if expected_chat_uuid is not None and found != expected_chat_uuid:
            return Observation(
                activity,
                "Cursor startup UUID conflicts with the current process log UUID",
                {"pid": pid},
                (),
                blocked_code="CLI_IDENTITY_AMBIGUOUS",
                source="cursor_pid_log",
            )
        if chat is None:
            meta_cwd, persistence = _cursor_meta(found, cwd, home)
            chat = ChatEvidence(
                "cursor",
                found,
                cwd=meta_cwd,
                identity_source="cursor_pid_log",
                persistence_status=persistence,
                resume_executable="cursor-agent",
                resume_argv=("--resume", found),
            )
            source = "cursor_pid_log"

    if chat is None:
        return Observation(
            activity,
            activity_reason or "Cursor process is recognized but no process-bound chat UUID was found",
            {"pid": pid},
            (),
            blocked_code="CLI_IDENTITY_MISSING",
            source="cursor_pid",
        )
    reason = activity_reason or "Cursor process identity is bound; task activity is not passively exposed"
    if log_path is not None:
        try:
            stat = log_path.stat()
            reason = f"{reason}; process log metadata mtime={stat.st_mtime_ns} size={stat.st_size}"
        except OSError:
            pass
    return Observation(activity, reason, {"pid": pid}, (chat,), source=source)


def _parse_kiro_sessions(raw: bytes, *, cwd: str | None) -> tuple[dict[str, Any], ...]:
    """Parse only id/status/cwd fields from Kiro's JSON session listing."""

    if not raw or len(raw) > DEFAULT_KIRO_OUTPUT_BYTES:
        return ()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return ()
    records: list[Any]
    if isinstance(value, list):
        records = value
    elif isinstance(value, Mapping):
        candidate = value.get("sessions")
        if isinstance(candidate, list):
            records = candidate
        else:
            records = [value]
    else:
        return ()
    result: list[dict[str, Any]] = []
    for item in records[:DEFAULT_KIRO_RECORDS]:
        if not isinstance(item, Mapping):
            continue
        chat_id = _valid_uuid(
            item.get("session_id") or item.get("sessionId") or item.get("id") or item.get("uuid")
        )
        if chat_id is None:
            continue
        item_cwd = _safe_cwd(item.get("cwd") or item.get("working_directory"))
        if cwd is not None and item_cwd is not None:
            try:
                if Path(cwd).resolve() != Path(item_cwd).resolve():
                    continue
            except OSError:
                if cwd != item_cwd:
                    continue
        status = item.get("status")
        if not isinstance(status, str):
            status = None
        result.append({"chat_uuid": chat_id, "cwd": item_cwd or cwd, "status": status})
    return tuple(result)


def _kiro_observation(
    *,
    pid: int,
    cwd: str | None,
    runner: Callable[..., bytes],
    executable: str = "kiro-cli",
    supplied_chat: ChatEvidence | None = None,
    activity: Activity = "unknown",
    activity_reason: str | None = None,
) -> Observation:
    if not _pid_alive(pid):
        return Observation("exited", "the bound Kiro process is no longer alive", {"pid": pid}, (), source="pid")
    if supplied_chat is not None:
        return Observation(
            activity,
            activity_reason or "Kiro process identity is registered; task activity is not passively exposed",
            {"pid": pid},
            (supplied_chat,),
            source="registered_process",
        )
    # This command is read-only, but its result is only a candidate list.  We
    # deliberately do not bind a candidate to the PID without a registration.
    try:
        output = runner(
            (executable, "chat", "--list-sessions", "--format", "json"),
            cwd=cwd,
            timeout=1.5,
        )
    except Exception:
        output = b""
    candidates = _parse_kiro_sessions(output, cwd=cwd)
    busy = [item for item in candidates if item.get("status") == "in_progress"]
    reason = activity_reason or "Kiro exposes saved-session status but no process-bound foreground UUID"
    if busy:
        reason = f"{reason}; {len(busy)} saved session(s) report in_progress"
    return Observation(
        activity,
        reason,
        {"pid": pid},
        (),
        blocked_code="CLI_IDENTITY_MISSING",
        source="kiro_session_list" if candidates else "kiro_pid",
    )


class CliRecoveryAdapter:
    """Exact-session registry plus conservative passive CLI observations."""

    def __init__(
        self,
        *,
        home: str | os.PathLike[str] | None = None,
        runner: Callable[..., bytes] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.home = Path(home).expanduser() if home is not None else Path.home()
        self.runner = runner or _default_runner
        self.monotonic = monotonic or time.monotonic
        self._bindings: dict[str, ProcessBinding] = {}

    def register(
        self,
        session_id: str,
        *,
        cli: str,
        pid: int,
        chat_uuid: str | None = None,
        cwd: str | None = None,
        process_start: str | None = None,
        activity: Activity = "unknown",
        activity_reason: str = "registered process has no task activity signal",
        persistence_status: str | None = None,
        identity_source: str = "registered_process",
    ) -> ProcessBinding:
        """Register exact launch/resume evidence for one iTerm session.

        Callers should invoke this when they launch/resume a CLI or when a
        trusted integration returns its chat id.  It is safe to call again
        with a new process; the old binding is replaced atomically in memory.
        """

        normalized = normalize_cli(cli)
        if normalized is None:
            raise ValueError("unsupported CLI")
        chat: ChatEvidence | None = None
        if chat_uuid is not None:
            resume = {
                "claude": ("claude", ("--resume", chat_uuid)),
                "codex": ("codex", ("resume", chat_uuid)),
                "cursor": ("cursor-agent", ("--resume", chat_uuid)),
                "kiro": ("kiro-cli", ("chat", "--resume-id", chat_uuid)),
            }[normalized]
            chat = ChatEvidence(
                normalized,
                chat_uuid,
                cwd=cwd,
                identity_source=identity_source,
                persistence_status=persistence_status,
                resume_executable=resume[0],
                resume_argv=resume[1],
            )
        binding = ProcessBinding(
            session_id=session_id,
            cli=normalized,
            pid=pid,
            chat=chat,
            cwd=cwd,
            process_start=process_start,
            activity=activity,
            activity_reason=activity_reason,
        )
        self._bindings[session_id] = binding
        return binding

    def register_chat(
        self,
        session_id: str,
        *,
        chat_uuid: str,
        persistence_status: str | None = None,
        identity_source: str = "registered_process",
    ) -> ProcessBinding:
        """Add an id to an existing exact process binding."""

        binding = self._bindings.get(session_id)
        if binding is None:
            raise KeyError(session_id)
        normalized = normalize_cli(binding.cli)
        assert normalized is not None
        resume = {
            "claude": ("claude", ("--resume", chat_uuid)),
            "codex": ("codex", ("resume", chat_uuid)),
            "cursor": ("cursor-agent", ("--resume", chat_uuid)),
            "kiro": ("kiro-cli", ("chat", "--resume-id", chat_uuid)),
        }[normalized]
        chat = ChatEvidence(
            normalized,
            chat_uuid,
            cwd=binding.cwd,
            identity_source=identity_source,
            persistence_status=persistence_status,
            resume_executable=resume[0],
            resume_argv=resume[1],
        )
        replacement = replace(binding, chat=chat)
        self._bindings[session_id] = replacement
        return replacement

    def unregister(self, session_id: str) -> None:
        self._bindings.pop(session_id, None)

    def binding(self, session_id: str) -> ProcessBinding | None:
        return self._bindings.get(session_id)

    @staticmethod
    def _context_value(row: Mapping[str, Any], name: str) -> Any:
        context = row.get("context")
        if not isinstance(context, Mapping):
            return None
        value = context.get(name)
        if isinstance(value, Mapping):
            return value.get("value")
        return value

    @staticmethod
    def _process_candidates(process: Any) -> list[tuple[str, int, str | None]]:
        """Reduce process-provider facts to executable/pid/start triples."""

        if not isinstance(process, Mapping):
            return []
        values: list[Any] = []
        for key in ("active_jobs", "stopped_jobs"):
            candidate = process.get(key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                values.extend(candidate)
        values.append(process)
        result: list[tuple[str, int, str | None]] = []
        for value in values:
            if not isinstance(value, Mapping):
                continue
            executable = value.get("executable") or value.get("program")
            cli = normalize_cli(executable)
            if cli is None:
                continue
            identity = value.get("identity")
            identity = identity if isinstance(identity, Mapping) else value
            raw_pid = identity.get("pid", value.get("pid")) if isinstance(identity, Mapping) else None
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            start = identity.get("start_time", identity.get("start")) if isinstance(identity, Mapping) else None
            start = str(start) if isinstance(start, (str, int)) and str(start) else None
            candidate = (cli, pid, start)
            if candidate not in result:
                result.append(candidate)
        return result

    def probe(self, row: Mapping[str, Any], process: Any = None) -> Observation:
        """Adapt closing-backend process facts to a passive CLI observation.

        The process provider may report the shell plus active descendants.  A
        supported CLI is selected only when exactly one executable in that
        process set matches; this avoids signalling a sibling or a shared
        daemon.  The iTerm row remains the exact registry key.
        """

        session_id = row.get("session_id") if isinstance(row, Mapping) else None
        if not isinstance(session_id, str) or not session_id:
            return Observation("unknown", "session identity is unavailable", None, (), "PROCESS_IDENTITY_MISSING", "row")
        candidates = self._process_candidates(process)
        context_program = self._context_value(row, "program")
        context_cli = normalize_cli(context_program)
        context_cwd = _safe_cwd(self._context_value(row, "cwd"))
        if not candidates and context_cli is not None:
            process_map = process if isinstance(process, Mapping) else {}
            identity = process_map.get("identity") if isinstance(process_map, Mapping) else None
            identity = identity if isinstance(identity, Mapping) else process_map
            raw_pid = identity.get("pid") if isinstance(identity, Mapping) else None
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                pid = 0
            start = identity.get("start_time", identity.get("start")) if isinstance(identity, Mapping) else None
            start = str(start) if isinstance(start, (str, int)) and str(start) else None
            if pid > 0:
                candidates = [(context_cli, pid, start)]
        if len(candidates) > 1:
            return Observation(
                "unknown",
                "multiple supported CLI processes share the target process tree",
                _process_identity(candidates[0][1], candidates[0][2]),
                (),
                "CLI_IDENTITY_AMBIGUOUS",
                "process_tree",
            )
        if not candidates:
            # The caller may still have supplied an exact binding with no
            # process-provider child record; observe() will use that binding.
            return self.observe(session_id, cli=context_cli, cwd=context_cwd)
        cli, pid, process_start = candidates[0]
        return self.observe(
            session_id,
            cli=cli,
            pid=pid,
            cwd=context_cwd,
            process_start=process_start,
        )

    def _current_identity(self, pid: int, expected_start: str) -> bool:
        current_start, state = _process_start_and_state(pid, self.runner)
        return current_start == expected_start and not (
            state and state[0].upper() in {"Z", "X"}
        )

    def exit_session(
        self,
        row: Mapping[str, Any],
        probe: Mapping[str, Any],
        force: bool = False,
    ) -> dict[str, Any]:
        """Gracefully stop exactly the CLI PID proved by ``probe``.

        The sequence is bounded ``SIGINT`` then ``SIGTERM``.  It never uses
        SIGKILL, process groups, shell commands, or a PID without a matching
        start token.  Any process identities carried by the pre-signal probe
        are checked too, so an observed child that survives or is reused
        yields an exception and the caller retains the terminal.
        """

        cli_sessions = probe.get("cli_sessions") if isinstance(probe, Mapping) else None
        if not isinstance(cli_sessions, Sequence) or isinstance(cli_sessions, (str, bytes)) or not cli_sessions:
            raise RuntimeError("CLI identity is required before exit")
        identity = probe.get("identity") if isinstance(probe, Mapping) else None
        if not isinstance(identity, Mapping):
            raise RuntimeError("stable process identity is required before exit")
        try:
            pid = int(identity.get("pid"))
        except (TypeError, ValueError):
            raise RuntimeError("stable process identity is required before exit") from None
        expected_start = identity.get("start_time", identity.get("start"))
        if pid <= 0 or not isinstance(expected_start, str) or not expected_start:
            raise RuntimeError("stable process identity is required before exit")
        if pid in _self_and_ancestors(self.runner):
            raise RuntimeError("target process is an MCP process ancestor")
        if not self._current_identity(pid, expected_start):
            raise RuntimeError("target process identity changed before CLI exit")
        scope = _probe_process_scope(probe)
        if scope is None:
            raise RuntimeError("pre-signal process identity scope is unavailable")
        if not _process_scope_current(scope, self.runner):
            raise RuntimeError("a scoped CLI process identity changed before exit")
        verified_processes = [
            {"pid": scoped_pid, "start": scoped_start, "start_time": scoped_start}
            for scoped_pid, scoped_start in scope
        ]
        state = str(probe.get("state") or "unknown").casefold()
        if not force and state != "idle":
            raise RuntimeError("CLI activity is not idle")

        def exit_result() -> dict[str, Any]:
            return {
                "cli_exit_status": "exited",
                "task_interrupted": bool(force and state != "idle"),
                "verified": True,
                "verified_processes": verified_processes,
            }
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            if _process_scope_exit_verified(scope, self.runner):
                return exit_result()
            raise RuntimeError("CLI descendants remained after SIGINT") from None
        except (PermissionError, OSError) as exc:
            raise RuntimeError("SIGINT could not be delivered to the CLI process") from exc
        if _wait_process_scope_exit(scope, self.runner, 1.5):
            if _process_scope_exit_verified(scope, self.runner):
                return exit_result()
            raise RuntimeError("CLI process identity changed during graceful shutdown")
        if not self._current_identity(pid, expected_start):
            if _process_scope_exit_verified(scope, self.runner):
                return exit_result()
            if _process_exit_verified(pid, expected_start, self.runner):
                raise RuntimeError("CLI descendant remained after SIGINT")
            raise RuntimeError("CLI process identity changed during graceful shutdown")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            if _process_scope_exit_verified(scope, self.runner):
                return exit_result()
            raise RuntimeError("CLI descendants remained after SIGTERM") from None
        except (PermissionError, OSError) as exc:
            raise RuntimeError("SIGTERM could not be delivered to the CLI process") from exc
        if not _wait_process_scope_exit(scope, self.runner, 1.5):
            raise RuntimeError("CLI process or descendant did not exit within the bounded graceful shutdown")
        if not _process_scope_exit_verified(scope, self.runner):
            raise RuntimeError("CLI process identity changed during graceful shutdown")
        return exit_result()

    def observe(
        self,
        session_id: str,
        *,
        cli: str | None = None,
        pid: int | None = None,
        cwd: str | None = None,
        process_start: str | None = None,
        recognized_cli: bool = False,
        activity: Activity | None = None,
        activity_reason: str | None = None,
    ) -> Observation:
        """Observe one current process without terminal input.

        ``session_id`` is mandatory and is the only registry key.  If caller
        supplied PID/CLI values disagree with a registered binding, the
        observation is blocked instead of trying another process.  Set
        ``recognized_cli`` when the caller has identified an agent executable
        that this adapter does not support; ordinary shell/job programs may
        leave it false and remain an ordinary observation.
        """

        binding = self._bindings.get(session_id)
        normalized_cli = normalize_cli(cli) if cli is not None else None
        if binding is not None:
            if (pid is not None and pid != binding.pid) or (
                normalized_cli is not None and normalized_cli != binding.cli
            ) or (
                binding.process_start is not None
                and process_start != binding.process_start
            ):
                return Observation(
                    "unknown",
                    "registered process binding changed or lacks its start token",
                    {"pid": binding.pid, "process_start": binding.process_start},
                    (),
                    blocked_code="PROCESS_IDENTITY_CHANGED",
                    source="registry",
                )
            cli = binding.cli
            pid = binding.pid
            cwd = cwd or binding.cwd
            process_start = process_start or binding.process_start
            supplied_chat = binding.chat
            bound_activity = binding.activity
            bound_reason = binding.activity_reason
        else:
            supplied_chat = None
            bound_activity = "unknown"
            bound_reason = "recognized CLI has no registered process binding"
        if normalized_cli is None:
            normalized_cli = normalize_cli(cli)
        if normalized_cli not in SUPPORTED_CLIS:
            # A missing CLI program is a normal shell/job observation.  The
            # closing backend may add process identity separately.  If the
            # caller has positively identified an agent executable that this
            # adapter does not support, keep it blocked instead of silently
            # treating it as an ordinary shell.
            if pid is not None and not _pid_alive(pid):
                return Observation("exited", "bound process is no longer alive", _process_identity(pid, process_start), ())
            if recognized_cli:
                return Observation(
                    "unknown",
                    "an identified coding CLI is outside the supported adapter set",
                    _process_identity(pid, process_start),
                    (),
                    blocked_code="CLI_UNSUPPORTED",
                    source="unsupported_cli",
                )
            return Observation(
                "unknown",
                "no supported coding CLI was identified",
                _process_identity(pid, process_start),
                (),
                source="unrecognized",
            )
        if pid is None or type(pid) is not int or pid <= 0:
            return Observation(
                "unknown",
                "recognized CLI has no current process id",
                None,
                (),
                blocked_code="PROCESS_IDENTITY_MISSING",
                source="cli_context",
            )
        selected_activity = activity if activity is not None else bound_activity
        selected_reason = activity_reason or bound_reason
        registered_chat = supplied_chat
        passive_chat, passive_error = _passive_process_chat(
            cli=normalized_cli,
            pid=pid,
            cwd=cwd,
            runner=self.runner,
        )
        if supplied_chat is not None:
            if passive_error == "CLI_IDENTITY_AMBIGUOUS" or (
                passive_chat is not None
                and passive_chat.chat_uuid != supplied_chat.chat_uuid
            ):
                return _require_process_start(
                    _with_process_identity(
                        Observation(
                            selected_activity,
                            "current process metadata conflicts with the registered chat UUID",
                            _process_identity(pid, process_start),
                            (supplied_chat,),
                            blocked_code="CLI_IDENTITY_AMBIGUOUS",
                            source="process_metadata",
                        ),
                        pid,
                        process_start,
                    ),
                    process_start,
                )
        elif passive_chat is not None:
            supplied_chat = passive_chat
        if normalized_cli in {"claude", "codex", "kiro"} and passive_error is not None:
            return _require_process_start(
                _with_process_identity(
                    Observation(
                        selected_activity,
                        (
                            "multiple process-bound chat UUIDs were observed"
                            if passive_error == "CLI_IDENTITY_AMBIGUOUS"
                            else "argv identifies only the CLI startup session; current session evidence is unavailable"
                        ),
                        _process_identity(pid, process_start),
                        (supplied_chat,) if supplied_chat is not None else (),
                        blocked_code=passive_error,
                        source="process_metadata",
                    ),
                    pid,
                    process_start,
                ),
                process_start,
            )
        if normalized_cli == "cursor":
            current = _cursor_observation(
                pid=pid,
                cwd=cwd,
                home=self.home,
                runner=self.runner,
                supplied_chat=registered_chat,
                expected_chat_uuid=passive_chat.chat_uuid if passive_chat is not None else None,
                activity=selected_activity,
                activity_reason=selected_reason,
            )
            if current.cli_sessions:
                return _require_process_start(
                    _with_process_identity(current, pid, process_start), process_start
                )
            if (
                passive_chat is not None
                and passive_error == "CLI_IDENTITY_STARTUP_ONLY"
                and current.state != "exited"
                and current.blocked_code in {None, "CLI_IDENTITY_MISSING"}
            ):
                current = Observation(
                    selected_activity,
                    "argv identifies only the Cursor startup session; current log evidence is unavailable",
                    _process_identity(pid, process_start),
                    (passive_chat,),
                    blocked_code=passive_error,
                    source="process_argv_startup",
                )
            elif passive_error is not None and current.blocked_code is None:
                current = replace(current, blocked_code=passive_error)
            return _require_process_start(
                _with_process_identity(current, pid, process_start), process_start
            )
        if normalized_cli == "kiro":
            return _require_process_start(
                _with_process_identity(
                    _kiro_observation(
                        pid=pid,
                        cwd=cwd,
                        runner=self.runner,
                        supplied_chat=supplied_chat,
                        activity=selected_activity,
                        activity_reason=selected_reason,
                    ),
                    pid,
                    process_start,
                ),
                process_start,
            )
        identity = _process_identity(pid, process_start)
        if not _pid_alive(pid):
            return Observation("exited", "the bound CLI process is no longer alive", identity, (), source="pid")
        if supplied_chat is None:
            return Observation(
                selected_activity,
                selected_reason or f"{normalized_cli} has no passive foreground chat-id API",
                identity,
                (),
                blocked_code="CLI_IDENTITY_MISSING",
                source=f"{normalized_cli}_pid",
            )
        return _require_process_start(
            Observation(
                selected_activity,
                selected_reason,
                identity,
                (supplied_chat,),
                source="registered_process",
            ),
            process_start,
        )


__all__ = [
    "Activity",
    "ChatEvidence",
    "CliRecoveryAdapter",
    "Observation",
    "ProcessBinding",
    "SUPPORTED_CLIS",
    "normalize_cli",
]
