"""Safe lifecycle adapter for iTerm sessions, tabs, and windows.

``CloseService`` owns selection, receipts, and the public result shape.  This
module owns the boundary where observations become terminal writes.  It keeps
that boundary deliberately small:

* prompt state and process evidence are read before an exit/close;
* process and CLI adapters are injected, so this module never guesses a CLI
  from a title or sends an exploratory command;
* target membership and epoch identities are checked again immediately before
  the one native close call; and
* the native acknowledgement is followed by a fresh topology read.

The iTerm SDK is imported lazily.  Unit tests and read-only catalogue users
therefore do not need an installed iTerm module.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .cli_recovery import CliRecoveryAdapter
from .errors import DomainError
from .process_inspection import MacOSProcessProvider, ProcessIdentity, ProcessRecord


_MISSING = object()
_KINDS = {"session", "tab", "window"}
_STATES = {"idle", "busy", "unknown", "exited"}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _attr(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        try:
            return getattr(value, name)
        except (AttributeError, TypeError):
            continue
        except Exception:
            continue
    return default


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if value is None:
        return None
    # A few test doubles and DTOs expose only public attributes.  Do not call
    # arbitrary conversion methods because lifecycle inputs are untrusted.
    try:
        values = vars(value)
    except TypeError:
        return None
    return values if isinstance(values, Mapping) else None


def _name_from_row(row: Mapping[str, Any], session_id: str) -> str:
    for key in ("name", "nickname", "title", "session_name", "context_summary"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return f"session-{session_id[:8]}"


def _session_id(row: Mapping[str, Any]) -> str | None:
    target = _mapping(row.get("target"))
    value = target.get("session_id") if target else row.get("session_id")
    if isinstance(value, str) and value:
        return value
    return None


def _id_value(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value else None


def _target_map(row: Mapping[str, Any]) -> dict[str, Any] | None:
    target = _mapping(row.get("target"))
    return dict(target) if target is not None else None


def _coord_tuple(value: Any) -> tuple[Any, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        x, y = value.get("x"), value.get("y")
    else:
        x, y = _attr(value, "x"), _attr(value, "y")
    if x is None or y is None:
        return None
    return x, y


def _command_range_empty(command_range: Any) -> bool | None:
    """Return whether an iTerm ``CoordRange`` has equal start/end points."""

    if command_range is None:
        return None
    if isinstance(command_range, Sequence) and not isinstance(
        command_range, (str, bytes, bytearray)
    ):
        if len(command_range) != 2:
            return None
        start, end = command_range
    else:
        start = _attr(command_range, "start")
        end = _attr(command_range, "end")
    if start is None or end is None:
        return None
    first = _coord_tuple(start)
    second = _coord_tuple(end)
    if first is None or second is None:
        return None
    return first == second


def _state_name(value: Any) -> str:
    if value is None:
        return "unknown"
    name = _attr(value, "name", default=None)
    if isinstance(name, str):
        value = name
    elif isinstance(value, str):
        value = value
    elif isinstance(value, int) and not isinstance(value, bool):
        value = {0: "editing", 1: "running", 2: "finished"}.get(value, "unknown")
    else:
        value = str(value)
    value = value.rsplit(".", 1)[-1].strip().lower()
    return value if value in {"editing", "running", "finished", "unknown"} else "unknown"


def _stable_identity(value: Any) -> dict[str, Any] | None:
    """Normalize process identity without accepting a PID-only identity."""

    if isinstance(value, ProcessIdentity):
        value = value.as_dict()
    elif isinstance(value, ProcessRecord):
        value = value.identity.as_dict()
    value_map = _mapping(value)
    if value_map is None:
        return None
    raw_pid = value_map.get("pid")
    if isinstance(raw_pid, bool):
        return None
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    start = value_map.get(
        "start_time",
        value_map.get(
            "start",
            value_map.get(
                "starttime",
                value_map.get("startTime", value_map.get("process_start")),
            ),
        ),
    )
    if start is None or not isinstance(start, (str, int)) or str(start) == "":
        return None
    result: dict[str, Any] = {
        "pid": pid,
        "start": str(start),
        "start_time": str(start),
        "starttime": str(start),
        "process_start": str(start),
    }
    tty = value_map.get("tty")
    if isinstance(tty, str) and tty:
        result["tty"] = tty
    return result


def _process_item(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, ProcessRecord):
        return value.as_dict()
    result = _mapping(value)
    return result


def _process_pid(value: Any) -> int | None:
    identity = _stable_identity(value)
    if identity is not None:
        return int(identity["pid"])
    item = _process_item(value)
    raw = item.get("pid") if item else None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _record_active_jobs(process_info: Mapping[str, Any]) -> tuple[list[Any], list[Any], bool]:
    """Extract active/stopped child evidence and whether it was explicit."""

    explicit = False
    active = process_info.get("active_jobs", _MISSING)
    stopped = process_info.get("stopped_jobs", _MISSING)
    if active is not _MISSING or stopped is not _MISSING:
        explicit = True
    active_values = list(active) if isinstance(active, Iterable) and not isinstance(active, (str, bytes, Mapping)) else []
    stopped_values = list(stopped) if isinstance(stopped, Iterable) and not isinstance(stopped, (str, bytes, Mapping)) else []
    jobs = process_info.get("jobs", _MISSING)
    if jobs is not _MISSING:
        explicit = True
        if isinstance(jobs, Iterable) and not isinstance(jobs, (str, bytes, Mapping)):
            for item in jobs:
                state = str(_attr(item, "state", default="")).upper()
                if state.startswith(("T", "W")):
                    stopped_values.append(item)
                elif not state.startswith(("Z", "X")):
                    active_values.append(item)
    return active_values, stopped_values, explicit


def _stable_process_scope(probe: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return stable identities for the observed process tree and jobs.

    ``job_identities`` deliberately excludes the shell/root identity.  A
    shell survives a normal foreground interrupt, while every active/stopped
    job observed before that interrupt must independently disappear before a
    native close is permitted.
    """

    process = _process_item(probe.get("process"))
    if process is None:
        return [], []
    all_items: list[Any] = []
    job_items: list[Any] = []
    for key in ("active_jobs", "stopped_jobs"):
        value = process.get(key)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
            values = list(value)
            all_items.extend(values)
            job_items.extend(values)
    value = process.get("processes")
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        all_items.extend(value)
    root = process.get("identity")
    if root is not None:
        all_items.append(root)

    def normalize(items: Iterable[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[int, str, str | None]] = set()
        for item in items:
            item_map = _mapping(item)
            candidate = item_map.get("identity") if item_map is not None else item
            identity = _stable_identity(candidate)
            if identity is None:
                continue
            key = (int(identity["pid"]), str(identity["start"]), identity.get("tty"))
            if key in seen:
                continue
            seen.add(key)
            result.append(identity)
        return result

    return normalize(all_items), normalize(job_items)


def _error_from_exception(prefix: str, exc: BaseException) -> DomainError:
    text = str(exc).strip()
    upper = text.upper()
    if "USER_DECLINED" in upper or "PERMISSION" in upper or "DENIED" in upper:
        code = f"{prefix}_DENIED"
    elif "TIMEOUT" in upper or "DISCONNECT" in upper or "CONNECTION" in upper:
        code = f"{prefix}_UNKNOWN"
    elif "NOT_FOUND" in upper or "GONE" in upper:
        code = f"{prefix}_GONE"
    else:
        code = f"{prefix}_FAILED"
    return DomainError(code, text or code)


async def _invoke_compatible(
    method: Callable[..., Any], variants: Sequence[tuple[tuple[Any, ...], dict[str, Any]]]
) -> Any:
    """Invoke one of a small, explicitly enumerated adapter signatures."""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        signature = None
    selected: tuple[tuple[Any, ...], dict[str, Any]] | None = None
    if signature is not None:
        for args, kwargs in variants:
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                continue
            selected = (args, kwargs)
            break
    if selected is None:
        selected = variants[0]
    return await _maybe_await(method(*selected[0], **selected[1]))


class ClosingBackend:
    """Lifecycle operations over an existing connected iTerm backend.

    ``cli_adapter``, ``prompt_reader`` and ``process_provider`` are optional
    seams.  They may be synchronous or asynchronous.  The adapter never
    falls back to terminal keystrokes or name-based process matching.
    """

    def __init__(
        self,
        backend: Any,
        *,
        allowed: Iterable[str] | None = None,
        store: Any | None = None,
        cli_adapter: Any | None = None,
        prompt_reader: Callable[..., Any] | None = None,
        process_provider: Any | None = None,
        own_pid: int | None = None,
    ) -> None:
        self.backend = backend
        self.allowed = (
            None
            if allowed is None
            else frozenset(value for value in allowed if isinstance(value, str))
        )
        self.store = store
        # Keep a passive adapter by default so recognized CLIs can contribute
        # process-bound UUID evidence and force-close can use the bounded
        # signal path.  It never sends terminal input or probes with slash
        # commands; callers may still inject a stricter adapter for tests.
        self.cli_adapter = cli_adapter if cli_adapter is not None else CliRecoveryAdapter()
        self.prompt_reader = prompt_reader
        if process_provider is None and sys.platform == "darwin":
            process_provider = MacOSProcessProvider(own_pid=own_pid)
        self.process_provider = process_provider
        self._last_rows: list[dict[str, Any]] = []
        self._raw_by_session: dict[str, Any] = {}
        self._session_objects: dict[str, Any] = {}
        self._last_probes: dict[str, dict[str, Any]] = {}
        # A close is only allowed after the corresponding exit/idle proof has
        # been recorded.  The proof is refreshed again in close_target so a
        # new process or CLI cannot enter during the gap between exit and the
        # native close call.
        self._exit_proofs: dict[str, dict[str, Any]] = {}

    def is_allowed(self, row: Mapping[str, Any]) -> bool:
        session_id = _session_id(row)
        return self.allowed is None or session_id in self.allowed

    @staticmethod
    def _normalize_row(raw: Any) -> dict[str, Any]:
        item = _mapping(raw)
        if item is None:
            raise DomainError("BACKEND_UNAVAILABLE", "iTerm session row is malformed")
        result = {
            key: value
            for key, value in item.items()
            if isinstance(key, str) and not key.startswith("_")
        }
        session_id = _session_id(item)
        if session_id is None:
            raise DomainError("BACKEND_UNAVAILABLE", "iTerm session row has no session ID")
        target = _target_map(item)
        if target is not None:
            target_session_id = target.get("session_id")
            if target_session_id != session_id:
                raise DomainError("BACKEND_UNAVAILABLE", "iTerm target identity is malformed")
            result["target"] = target
        result["session_id"] = session_id
        for key in ("window_id", "tab_id"):
            value = result.get(key)
            if value is not None and not isinstance(value, str):
                raise DomainError("BACKEND_UNAVAILABLE", f"iTerm {key} is malformed")
        result["name"] = _name_from_row(item, session_id)
        return result

    async def snapshot(self) -> list[dict[str, Any]]:
        reader = getattr(self.backend, "list_sessions", None)
        if reader is None:
            reader = getattr(self.backend, "snapshot", None)
        if reader is None:
            raise DomainError("BACKEND_UNAVAILABLE", "backend has no session snapshot")
        try:
            raw_rows = await _maybe_await(reader())
        except DomainError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_from_exception("SNAPSHOT", exc) from exc
        if raw_rows is None or isinstance(raw_rows, (str, bytes, Mapping)):
            raise DomainError("BACKEND_UNAVAILABLE", "backend session snapshot is malformed")
        rows: list[dict[str, Any]] = []
        raw_by_session: dict[str, Any] = {}
        for raw in raw_rows:
            normalized = self._normalize_row(raw)
            sid = normalized["session_id"]
            if sid in raw_by_session:
                raise DomainError("BACKEND_UNAVAILABLE", "duplicate session IDs in snapshot")
            rows.append(normalized)
            raw_by_session[sid] = raw
            object_value = _attr(raw, "_session", "session", "session_object", default=None)
            if object_value is not None:
                self._session_objects[sid] = object_value
        self._last_rows = rows
        self._raw_by_session = raw_by_session
        return [dict(row) for row in rows]

    async def _resolve_session(self, session_id: str) -> Any | None:
        cached = self._session_objects.get(session_id)
        if cached is not None:
            return cached
        app = self._app()
        getter = getattr(app, "get_session_by_id", None) if app is not None else None
        if getter is not None:
            try:
                value = await _maybe_await(getter(session_id))
            except Exception:
                value = None
            if value is not None:
                self._session_objects[session_id] = value
                return value
        records = getattr(self.backend, "_session_records", None)
        if records is not None:
            try:
                for session, _display in await _maybe_await(records()):
                    if _attr(session, "session_id", "sessionId", "id") == session_id:
                        self._session_objects[session_id] = session
                        return session
            except Exception:
                pass
        return None

    def _app(self) -> Any | None:
        return _attr(self.backend, "_app", "app", default=None)

    async def _read_prompt(self, row: Mapping[str, Any], session: Any) -> tuple[Any, str | None]:
        sid = _session_id(row)
        if sid is None:
            return None, "session_id_missing"
        if self.prompt_reader is not None:
            variants = [
                ((row, session), {}),
                ((row,), {}),
                ((session, sid), {}),
                ((sid,), {}),
                ((), {}),
            ]
            try:
                return await _invoke_compatible(self.prompt_reader, variants), None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return None, f"prompt_reader_failed:{type(exc).__name__}"
        sdk = getattr(self.backend, "_iterm2", None)
        if sdk is None:
            try:
                sdk = importlib.import_module("iterm2")
            except Exception:
                return None, "shell_integration_unavailable"
        getter = getattr(sdk, "async_get_last_prompt", None)
        connection = _attr(self.backend, "_connection", "connection", default=None)
        if getter is None or connection is None:
            return None, "shell_integration_unavailable"
        try:
            return await _maybe_await(getter(connection, sid)), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, f"prompt_unavailable:{type(exc).__name__}"

    async def _read_process(self, row: Mapping[str, Any], session: Any) -> tuple[Any, str | None]:
        provider = self.process_provider
        if provider is None:
            return None, "process_inspection_unavailable"
        method = getattr(provider, "inspect_session", None)
        if method is None:
            method = getattr(provider, "inspect", None)
        if method is None and callable(provider):
            method = provider
        if method is None:
            return None, "process_inspection_unavailable"
        variants = [((row, session), {}), ((row,), {}), ((session,), {}), ((), {})]
        try:
            return await _invoke_compatible(method, variants), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, f"process_inspection_failed:{type(exc).__name__}"

    async def _augment_process_row(self, row: Mapping[str, Any], session: Any) -> dict[str, Any]:
        """Read only shell-integration variables needed for process binding.

        The normal catalogue intentionally exposes no PID/TTY fields.  Fetch
        these variables at probe time from the exact Session object instead of
        inferring them from a title, cwd or process name.
        """

        result = dict(row)
        getter = getattr(session, "async_get_variable", None)
        if getter is None:
            return result
        values: dict[str, Any] = {}
        for name in ("jobPid", "tty", "jobName", "path"):
            try:
                value = await _maybe_await(getter(name))
            except asyncio.CancelledError:
                raise
            except Exception:
                value = None
            if value not in (None, ""):
                values[name] = value
        if values:
            context = dict(_mapping(result.get("context")) or {})
            aliases = {"jobPid": "job_pid", "jobName": "program", "path": "cwd"}
            for name, value in values.items():
                context[aliases.get(name, name)] = {"value": value}
            result["context"] = context
            if "jobPid" in values:
                result["job_pid"] = values["jobPid"]
            if "tty" in values:
                result["tty"] = values["tty"]
        return result

    async def _read_cli(self, row: Mapping[str, Any], session: Any, process: Any) -> tuple[Any, str | None]:
        adapter = self.cli_adapter
        if adapter is None:
            return None, None
        method = None
        method_name = None
        for name in ("probe", "observe", "identify", "inspect", "classify"):
            candidate = getattr(adapter, name, None)
            if candidate is not None:
                method = candidate
                method_name = name
                break
        if method is None:
            return None, "cli_adapter_unavailable"
        sid = _session_id(row)
        process_map = _process_item(process)
        process_identity = _stable_identity(
            process_map.get("identity") if process_map is not None else process
        )
        process_pid = _process_pid(process_identity)
        process_start = process_identity.get("start") if process_identity is not None else None
        context = _mapping(row.get("context"))
        program = _attr(context.get("program") if context else None, "value", default=None)
        cwd = _attr(context.get("cwd") if context else None, "value", default=None)
        variants = [
            (
                (sid,),
                {
                    "pid": process_pid,
                    "process_start": process_start,
                    "cli": program,
                    "cwd": cwd,
                },
            ),
            ((sid,), {}),
            ((row, process), {}),
            ((row, process, session), {}),
            ((row,), {}),
            ((session, row), {}),
        ]
        if method_name != "observe":
            variants = variants[2:] + variants[:2]
        try:
            value = await _invoke_compatible(method, variants)
            as_probe = getattr(value, "as_probe", None)
            if callable(as_probe):
                value = as_probe()
            return value, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, f"cli_probe_failed:{type(exc).__name__}"

    @staticmethod
    def _cli_sessions(cli_value: Any, row: Mapping[str, Any]) -> list[Any]:
        value = _mapping(cli_value)
        if value is not None:
            value = value.get("cli_sessions", value.get("sessions", value.get("cli")))
        if value is None:
            # A CLI adapter is the source of truth.  A row-level value is only
            # accepted when a caller explicitly injected it for a test or a
            # persisted catalogue adapter.
            value = row.get("cli_sessions")
        if value is None:
            return []
        if isinstance(value, Mapping):
            return [dict(value)]
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            return list(value)
        return []

    async def probe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self._normalize_row(row)
        sid = normalized["session_id"]
        session = await self._resolve_session(sid)
        if session is None and self._last_rows and not any(
            item.get("session_id") == sid for item in self._last_rows
        ):
            return {
                "state": "exited",
                "reason": "session_not_in_snapshot",
                "identity": None,
                "cli_sessions": [],
            }
        if session is None:
            # Test doubles can provide prompt/process evidence without an SDK
            # Session object; retain that path while still failing closed when
            # no evidence is available.
            session = normalized
        prompt, prompt_error = await self._read_prompt(normalized, session)
        evidence_row = await self._augment_process_row(normalized, session)
        process, process_error = await self._read_process(evidence_row, session)
        cli, cli_error = await self._read_cli(evidence_row, session, process)

        process_map = _process_item(process)
        identity = _stable_identity(
            process_map.get("identity") if process_map is not None else process
        )
        if identity is None and process_map is not None:
            identity = _stable_identity(process_map)
        active_jobs: list[Any] = []
        stopped_jobs: list[Any] = []
        jobs_explicit = False
        protected = False
        foreground: bool | None = None
        if process_map is not None:
            active_jobs, stopped_jobs, jobs_explicit = _record_active_jobs(process_map)
            protected = bool(process_map.get("protected"))
            pid = _process_pid(identity)
            protected_pids = process_map.get("protected_pids")
            if isinstance(protected_pids, Iterable) and not isinstance(protected_pids, (str, bytes, Mapping)):
                protected = protected or pid in {
                    int(value)
                    for value in protected_pids
                    if str(value).isdigit()
                }
            foreground_value = process_map.get("foreground")
            if isinstance(foreground_value, bool):
                foreground = foreground_value

        cli_sessions = self._cli_sessions(cli, normalized)
        state = _state_name(_attr(prompt, "state", "prompt_state", default=None))
        reason = "prompt_unavailable"
        blocked_code: str | None = None
        binding = None
        binding_getter = getattr(self.cli_adapter, "binding", None)
        if callable(binding_getter):
            try:
                binding = binding_getter(sid)
            except Exception:
                binding = None
        bound_start = _attr(binding, "process_start", default=None)
        if protected:
            state = "unknown"
            reason = "target_process_is_mcp_ancestor"
            blocked_code = "SELF_PROTECTION"
        elif cli_error:
            state = "unknown"
            reason = cli_error
            blocked_code = "CLI_IDENTITY_UNKNOWN"
        elif state == "running":
            state = "busy"
            reason = "prompt_command_running"
        elif state == "finished":
            state = "unknown"
            reason = "prompt_command_finished_without_new_prompt"
        elif state == "editing":
            command_empty = _command_range_empty(_attr(prompt, "command_range", default=None))
            if command_empty is not True:
                state = "unknown"
                reason = "prompt_command_range_unavailable"
            elif process_error:
                state = "unknown"
                reason = process_error
            elif not jobs_explicit:
                state = "unknown"
                reason = "active_job_evidence_unavailable"
            elif stopped_jobs:
                state = "busy"
                reason = "stopped_job_on_tty"
            elif active_jobs:
                state = "busy"
                reason = "active_job_on_tty"
            elif identity is None:
                state = "unknown"
                reason = "stable_process_identity_unavailable"
            else:
                state = "idle"
                reason = "shell_prompt_empty_and_tty_idle"
        else:
            state = "unknown"
            reason = prompt_error or "prompt_state_unknown"

        cli_map = _mapping(cli)
        # A process provider often reports the shell as the root identity and
        # the coding CLI as one active descendant.  The CLI adapter's
        # process-bound identity is the one that may be interrupted.
        cli_identity = _stable_identity(cli_map.get("identity")) if cli_map is not None else None
        if cli_sessions and cli_identity is not None:
            identity = cli_identity
        # Compare a registered CLI binding with the process-bound CLI
        # identity when one was observed.  Comparing it with the shell root
        # would falsely reject a valid child CLI whose PID differs from the
        # shell's ``jobPid``.
        live_identity = cli_identity or identity
        live_start = live_identity.get("start") if live_identity is not None else None
        if bound_start is not None and live_start is not None and str(bound_start) != str(live_start):
            state = "unknown"
            reason = "registered CLI process start changed"
            blocked_code = "PROCESS_IDENTITY_CHANGED"
        if cli_map is not None and isinstance(cli_map.get("blocked_code"), str):
            blocked_code = blocked_code or cli_map["blocked_code"]
            if state != "busy":
                state = "unknown"
                reason = str(cli_map.get("reason") or cli_map["blocked_code"])
        if cli_sessions and identity is None:
            # Chat UUID and process identity are different pieces of evidence.
            # Keep the session record for reporting, but block interruption
            # until the current process itself has a stable identity.
            blocked_code = blocked_code or "CLI_IDENTITY_UNKNOWN"
            if state != "busy":
                state = "unknown"
                reason = "cli_process_identity_unavailable"
        result: dict[str, Any] = {
            "state": state if state in _STATES else "unknown",
            "reason": reason,
            "identity": identity,
            "cli_sessions": cli_sessions,
        }
        if process_map is not None:
            result["process"] = dict(process_map)
        if foreground is not None:
            result["foreground"] = foreground
        if blocked_code:
            result["blocked_code"] = blocked_code
        self._last_probes[sid] = dict(result)
        return result

    def _remember_exit_proof(
        self,
        row: Mapping[str, Any],
        probe: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        prior_probe: Mapping[str, Any] | None = None,
    ) -> None:
        sid = _session_id(row)
        if sid is None:
            return
        scope, jobs = _stable_process_scope(probe)
        if prior_probe is not None:
            prior_scope, prior_jobs = _stable_process_scope(prior_probe)
            scope = prior_scope + [item for item in scope if item not in prior_scope]
            jobs = prior_jobs + [item for item in jobs if item not in prior_jobs]
        self._exit_proofs[sid] = {
            "identity": _stable_identity(probe.get("identity")),
            "cli_sessions": [dict(item) for item in (probe.get("cli_sessions") or ()) if isinstance(item, Mapping)],
            "process_scope": scope,
            "job_identities": jobs,
            "result": dict(result),
        }

    async def _verify_interrupt(self, row: Mapping[str, Any], probe: Mapping[str, Any]) -> bool:
        provider = self.process_provider
        if provider is None:
            return False
        method = None
        method_name = None
        for name in ("verify_interrupt", "verify_completion", "verify"):
            candidate = getattr(provider, name, None)
            if candidate is not None:
                method = candidate
                method_name = name
                break
        if method is not None:
            identity = _stable_identity(probe.get("identity"))
            # ``verify_interrupt`` is the process-provider spelling and takes
            # the stable identity first.  Some injected adapters use the
            # generic ``(row, probe)`` shape even with that name, so inspect
            # the first parameter name before selecting the call shape.
            first_name = ""
            try:
                parameters = tuple(inspect.signature(method).parameters.values())
                if parameters:
                    first_name = parameters[0].name.casefold()
            except (TypeError, ValueError):
                pass
            identity_first = method_name == "verify_interrupt" and first_name in {
                "identity",
                "process_identity",
                "processid",
                "process_id",
            }
            if identity_first:
                variants = [
                    ((identity, row, probe), {}),
                    ((identity, row), {}),
                    ((identity,), {}),
                ]
            else:
                variants = [
                    ((row, probe), {}),
                    ((row,), {}),
                    ((probe,), {}),
                ]
            value = await _invoke_compatible(
                method,
                variants,
            )
            if isinstance(value, Mapping):
                if "complete" in value:
                    return bool(value["complete"])
                if "verified" in value:
                    return bool(value["verified"])
                active, stopped, explicit = _record_active_jobs(value)
                return explicit and not active and not stopped
            return bool(value)
        inspect_method = getattr(provider, "inspect", None)
        if inspect_method is not None:
            value = await _invoke_compatible(inspect_method, [((row,), {}), ((), {})])
            value_map = _process_item(value)
            if value_map is None:
                return False
            active, stopped, explicit = _record_active_jobs(value_map)
            return explicit and not active and not stopped
        return False

    async def exit_session(
        self, row: Mapping[str, Any], probe: Mapping[str, Any], force: bool = False
    ) -> dict[str, Any]:
        normalized = self._normalize_row(row)
        cached_probe = self._last_probes.get(normalized["session_id"])
        if cached_probe is not None and "process" not in probe and "process" in cached_probe:
            merged_probe = dict(cached_probe)
            merged_probe.update(probe)
            probe = merged_probe
        state = str(probe.get("state", "unknown")).lower()
        cli_sessions = list(probe.get("cli_sessions") or ())
        if state == "exited":
            raise DomainError("SESSION_GONE", "session already exited")
        if probe.get("blocked_code") == "SELF_PROTECTION":
            raise DomainError("SELF_PROTECTION", "target is an ancestor of the MCP process")
        if not force and state != "idle":
            code = "ACTIVITY_UNKNOWN" if state == "unknown" else "ACTIVITY_BLOCKED"
            raise DomainError(code, str(probe.get("reason") or code))
        if state == "unknown" and not force and not cli_sessions:
            raise DomainError("ACTIVITY_UNKNOWN", str(probe.get("reason") or "activity is unknown"))

        if cli_sessions:
            adapter = self.cli_adapter
            if adapter is None:
                raise DomainError("CLI_RECOVERY_UNAVAILABLE", "recognized CLI has no exit adapter")
            for item in cli_sessions:
                item_map = _mapping(item)
                if item_map is None:
                    raise DomainError("CLI_IDENTITY_UNKNOWN", "CLI identity is malformed")
                chat_uuid = item_map.get("chat_uuid", item_map.get("uuid", item_map.get("id")))
                if not isinstance(chat_uuid, str) or not chat_uuid.strip():
                    raise DomainError("CLI_IDENTITY_UNKNOWN", "chat UUID is required before CLI exit")
            method = None
            for name in ("exit_session", "exit", "shutdown"):
                candidate = getattr(adapter, name, None)
                if candidate is not None:
                    method = candidate
                    break
            if method is None:
                raise DomainError("CLI_RECOVERY_UNAVAILABLE", "CLI adapter cannot exit a session")
            try:
                value = await _invoke_compatible(
                    method,
                    [
                        ((normalized, probe, force), {}),
                        ((normalized, probe), {"force": force}),
                        ((normalized,), {"force": force}),
                        ((probe,), {"force": force}),
                    ],
                )
            except DomainError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _error_from_exception("CLI_EXIT", exc) from exc
            result: dict[str, Any] = dict(value) if isinstance(value, Mapping) else {}
            status = result.get("cli_exit_status")
            if status is None and result.get("exited") is True:
                status = "exited"
            if status != "exited":
                raise DomainError("CLI_EXIT_FAILED", str(result.get("reason") or "CLI exit was not verified"))
            if result.get("verified") is False:
                raise DomainError("CLI_EXIT_UNVERIFIED", "CLI adapter did not verify exit")
            process_info = _process_item(probe.get("process"))
            if process_info is not None:
                active_jobs, stopped_jobs, _ = _record_active_jobs(process_info)
                if active_jobs or stopped_jobs:
                    _scope, job_identities = _stable_process_scope(probe)
                    if len(job_identities) < len(active_jobs) + len(stopped_jobs):
                        raise DomainError(
                            "PROCESS_IDENTITY_UNKNOWN",
                            "every observed CLI child needs a stable PID/start identity",
                        )
                    has_verifier = any(
                        callable(getattr(self.process_provider, name, None))
                        for name in ("verify_interrupt", "verify_completion", "verify")
                    )
                    if not has_verifier:
                        raise DomainError(
                            "CLI_EXIT_UNVERIFIED",
                            "CLI exit did not provide a verifier for observed child jobs",
                        )
                    try:
                        process_verified = await self._verify_interrupt(normalized, probe)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        raise _error_from_exception("CLI_EXIT", exc) from exc
                    if not process_verified:
                        raise DomainError(
                            "CLI_EXIT_UNVERIFIED",
                            "an observed CLI child process remains after exit",
                        )
            result["cli_exit_status"] = "exited"
            result["task_interrupted"] = bool(result.get("task_interrupted", force))
            result["force"] = bool(force)
            self._remember_exit_proof(normalized, probe, result)
            return result

        if state == "idle":
            result = {"cli_exit_status": "not_needed", "task_interrupted": False, "force": bool(force)}
            self._remember_exit_proof(normalized, probe, result)
            return result
        process = _process_item(probe.get("process"))
        if process is None:
            raise DomainError("INTERRUPT_UNSUPPORTED", "no process evidence for forced interruption")
        identity = _stable_identity(probe.get("identity"))
        if identity is None:
            raise DomainError("PROCESS_IDENTITY_UNKNOWN", "forced interruption needs a stable process identity")
        if process.get("protected"):
            raise DomainError("SELF_PROTECTION", "target process is protected")
        active, stopped, explicit = _record_active_jobs(process)
        if not explicit:
            raise DomainError("ACTIVE_JOB_UNKNOWN", "foreground job evidence is unavailable")
        if active or stopped:
            _scope, job_identities = _stable_process_scope(probe)
            if len(job_identities) < len(active) + len(stopped):
                raise DomainError(
                    "PROCESS_IDENTITY_UNKNOWN",
                    "every observed foreground job needs a stable PID/start identity",
                )
        if not active and not stopped:
            # ``force`` bypasses an unavailable prompt state when the process
            # census itself proves that no job owns the TTY.  This is the
            # ordinary shell case; unknown process/CLI identity still blocks.
            if state == "unknown" and force and identity is not None:
                result = {"cli_exit_status": "not_needed", "task_interrupted": False, "force": True}
                self._remember_exit_proof(normalized, probe, result)
                return result
            raise DomainError("ACTIVE_JOB_UNKNOWN", "foreground job evidence is unavailable")
        if probe.get("foreground") is not True and process.get("foreground") is not True:
            raise DomainError("FOREGROUND_REQUIRED", "forced interruption requires the targeted foreground job")
        provider = self.process_provider
        interrupt = getattr(provider, "interrupt_foreground", None)
        if interrupt is None:
            interrupt = getattr(provider, "interrupt", None)
        if interrupt is None:
            raise DomainError("INTERRUPT_UNSUPPORTED", "no targeted interruption adapter is installed")
        try:
            value = await _invoke_compatible(
                interrupt,
                [
                    ((identity, normalized, probe), {}),
                    ((identity, probe), {}),
                    ((identity,), {}),
                ],
            )
        except DomainError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_from_exception("INTERRUPT", exc) from exc
        if isinstance(value, Mapping) and value.get("verified") is False:
            raise DomainError("INTERRUPT_UNVERIFIED", "targeted interruption was not verified")
        try:
            verified = await self._verify_interrupt(normalized, probe)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_from_exception("INTERRUPT", exc) from exc
        if not verified:
            raise DomainError("INTERRUPT_UNVERIFIED", "targeted interruption did not reach completion")
        # Refresh topology before re-probing.  iTerm can remove a session
        # automatically as soon as its last process exits; probing the stale
        # cached Session in that interval produces false ``unknown`` evidence.
        # A verified process-tree exit plus exact disappearance is already a
        # complete close proof, and close_target will independently accept
        # that already-removed target.
        try:
            refreshed_rows = await self.snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_from_exception("INTERRUPT", exc) from exc
        refreshed = next(
            (
                candidate
                for candidate in refreshed_rows
                if candidate.get("session_id") == normalized["session_id"]
            ),
            None,
        )
        if refreshed is None:
            result = {
                "cli_exit_status": "not_needed",
                "task_interrupted": True,
                "force": True,
            }
            self._remember_exit_proof(normalized, probe, result)
            return result
        for key in ("window_id", "tab_id", "target"):
            if refreshed.get(key) != normalized.get(key):
                raise DomainError("TARGET_CHANGED", "session identity changed after interruption")

        # Re-probe after the targeted process has disappeared.  The first
        # proof referred to the interrupted foreground PID; an idle shell
        # may now have a different PID/start token.  Recording the fresh
        # shell evidence lets the final close check reject a newly-started
        # job while accepting the expected post-interrupt shell transition.
        try:
            post_probe = await self.probe(refreshed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_from_exception("INTERRUPT", exc) from exc
        if post_probe.get("blocked_code"):
            raise DomainError(str(post_probe["blocked_code"]), "post-interrupt process evidence is blocked")
        post_process = _process_item(post_probe.get("process")) or {}
        post_active, post_stopped, post_explicit = _record_active_jobs(post_process)
        post_state = str(post_probe.get("state") or "unknown").casefold()
        if post_state == "busy" or (post_state == "unknown" and not (post_explicit and not post_active and not post_stopped)):
            raise DomainError("INTERRUPT_UNVERIFIED", "post-interrupt activity remains unknown or busy")
        result = {
            "cli_exit_status": "not_needed",
            "task_interrupted": True,
            "force": True,
        }
        self._remember_exit_proof(normalized, post_probe, result, prior_probe=probe)
        return result

    async def _refresh_app(self) -> None:
        app = self._app()
        method = getattr(app, "async_refresh", None) if app is not None else None
        if method is not None:
            try:
                await _maybe_await(method())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _error_from_exception("CLOSE_REFRESH", exc) from exc

    async def _revalidate_exit_proofs(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Ensure the process/CLI observed before close is still the target.

        A parent target can sit open while an exited CLI is replaced by a new
        command.  Fresh topology alone cannot detect that replacement, so this
        check is intentionally process aware and runs before the native close.
        """

        for row in rows:
            sid = _session_id(row)
            if sid is None:
                raise DomainError("TARGET_CHANGED", "session identity disappeared before close")
            proof = self._exit_proofs.get(sid)
            fresh = await self.probe(row)
            if fresh.get("blocked_code"):
                raise DomainError(str(fresh["blocked_code"]), "fresh close preflight is blocked")
            if fresh.get("state") != "idle":
                proof_result = (proof or {}).get("result", {})
                process = _process_item(fresh.get("process"))
                active, stopped, explicit = _record_active_jobs(process or {})
                identity_ok = _stable_identity(fresh.get("identity")) == (proof or {}).get("identity")
                force_idle_unknown = (
                    fresh.get("state") == "unknown"
                    and proof_result.get("force") is True
                    and explicit
                    and not active
                    and not stopped
                    and identity_ok
                )
                if not force_idle_unknown:
                    raise DomainError("ACTIVITY_CHANGED", "session activity changed before native close")
            fresh_chats = {
                item.get("chat_uuid")
                for item in (fresh.get("cli_sessions") or ())
                if isinstance(item, Mapping)
            }
            if fresh_chats:
                # A CLI that appears after the exit proof is a new target and
                # must never be closed implicitly.
                proof_chats = {
                    item.get("chat_uuid")
                    for item in (proof or {}).get("cli_sessions", ())
                    if isinstance(item, Mapping)
                }
                if not proof_chats or fresh_chats != proof_chats:
                    raise DomainError("TARGET_CHANGED", "a new CLI appeared before native close")
            if proof is not None:
                expected_identity = proof.get("identity")
                fresh_identity = _stable_identity(fresh.get("identity"))
                if expected_identity is not None and fresh_identity is None:
                    # A CLI process may legitimately disappear after a
                    # verified exit; an empty CLI list is the evidence that it
                    # did not get replaced by another agent.
                    if not proof.get("cli_sessions"):
                        raise DomainError("PROCESS_IDENTITY_CHANGED", "target process identity disappeared")
                elif expected_identity is not None and fresh_identity != expected_identity:
                    if not proof.get("cli_sessions"):
                        raise DomainError("PROCESS_IDENTITY_CHANGED", "target process identity changed")

    async def _resolve_native_target(self, kind: str, target_id: str, rows: Sequence[Mapping[str, Any]]) -> Any:
        app = self._app()
        if kind == "session":
            session = await self._resolve_session(target_id)
            if session is not None:
                return session
        elif app is not None:
            names = {
                "tab": ("get_tab_by_id",),
                "window": ("get_window_by_id",),
            }[kind]
            for name in names:
                getter = getattr(app, name, None)
                if getter is not None:
                    try:
                        value = await _maybe_await(getter(target_id))
                    except Exception:
                        value = None
                    if value is not None:
                        return value
        getter = getattr(self.backend, f"get_{kind}_by_id", None)
        if getter is not None:
            value = await _maybe_await(getter(target_id))
            if value is not None:
                return value
        # The session object can be carried by a test double row; accept it
        # only after exact target membership has been validated by the caller.
        if kind == "session":
            for row in rows:
                if row.get("session_id") == target_id:
                    raw = self._raw_by_session.get(target_id)
                    value = _attr(raw, "_session", "session", "session_object", default=None)
                    if value is not None:
                        return value
        raise DomainError("TARGET_GONE", f"{kind} target is unavailable")

    @staticmethod
    def _target_membership(kind: str, row: Mapping[str, Any], target_id: str) -> bool:
        if kind == "session":
            return row.get("session_id") == target_id
        return row.get(f"{kind}_id") == target_id

    async def close_target(
        self,
        kind: str,
        target_id: str,
        expected_rows: Sequence[Mapping[str, Any]],
        force: bool = False,
    ) -> None:
        if kind not in _KINDS or not isinstance(target_id, str) or not target_id:
            raise DomainError("INVALID_ARGUMENT", "close target kind or ID is invalid")
        if not isinstance(expected_rows, Sequence) or isinstance(expected_rows, (str, bytes)):
            raise DomainError("INVALID_ARGUMENT", "expected_rows must be a sequence")
        expected = [self._normalize_row(row) for row in expected_rows]
        if not expected:
            raise DomainError("TARGET_CHANGED", "close target has no expected sessions")
        expected_ids = [row["session_id"] for row in expected]
        if len(set(expected_ids)) != len(expected_ids):
            raise DomainError("TARGET_CHANGED", "close target contains duplicate sessions")
        if any(not self._target_membership(kind, row, target_id) for row in expected):
            raise DomainError("TARGET_CHANGED", "expected session is outside the target")
        if self.allowed is not None and any(row["session_id"] not in self.allowed for row in expected):
            raise DomainError("ACCESS_DENIED", "close target contains an unauthorized session")
        if kind == "session" and (len(expected) != 1 or expected[0]["session_id"] != target_id):
            raise DomainError("TARGET_CHANGED", "session target membership is invalid")

        previous_ids = {row.get("session_id") for row in self._last_rows}
        current = await self.snapshot()
        current_target = [row for row in current if self._target_membership(kind, row, target_id)]
        if not current_target:
            # A verified force exit can make iTerm remove a session
            # automatically (for example, a shell whose last foreground job
            # terminated).  If every expected member has an exit proof and is
            # now absent, the requested target is already closed.  A new
            # member would appear in ``current_target`` and still take the
            # normal membership-change refusal path below.
            proofs_complete = all(
                isinstance(self._exit_proofs.get(sid), Mapping)
                and self._exit_proofs[sid].get("result", {}).get("cli_exit_status")
                in {"not_needed", "exited"}
                for sid in expected_ids
            )
            current_ids = {row.get("session_id") for row in current}
            expected_ids_set = set(expected_ids)
            new_ids = current_ids - previous_ids
            if (
                proofs_complete
                and not (expected_ids_set & current_ids)
                and not new_ids
            ):
                return
            raise DomainError("TARGET_GONE", f"{kind} target is no longer present")
        if self.allowed is not None and any(row["session_id"] not in self.allowed for row in current_target):
            raise DomainError("ACCESS_DENIED", "close target contains an unauthorized session")
        current_by_id = {row["session_id"]: row for row in current_target}
        if set(current_by_id) != set(expected_ids):
            raise DomainError("TARGET_CHANGED", "target membership changed before close")
        expected_by_id = {row["session_id"]: row for row in expected}
        for sid, current_row in current_by_id.items():
            expected_row = expected_by_id[sid]
            for key in ("window_id", "tab_id", "target"):
                if current_row.get(key) != expected_row.get(key):
                    raise DomainError("TARGET_CHANGED", "target identity changed before close")

        # Re-read prompt/process/CLI evidence after every exit and immediately
        # before the one native close.  This closes the race where a new job
        # or CLI starts in the same iTerm session after the initial exit.
        await self._revalidate_exit_proofs(current_target)
        native = await self._resolve_native_target(kind, target_id, current_target)
        close_method = getattr(native, "async_close", None)
        if close_method is None:
            raise DomainError("UNSUPPORTED_CAPABILITY", f"{kind} close is unavailable")
        try:
            # This is the sole native write.  In particular, do not wrap it in
            # an iTerm transaction: close/exit UI operations are not safely
            # transactional and an acknowledgement still needs verification.
            await _maybe_await(close_method(force=bool(force)))
        except DomainError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error_from_exception("CLOSE", exc) from exc

        await self._refresh_app()
        remaining = await self.snapshot()
        if any(self._target_membership(kind, row, target_id) for row in remaining):
            raise DomainError("CLOSE_UNVERIFIED", f"{kind} target remains after close acknowledgement")
        app = self._app()
        if app is not None:
            getter_name = {"session": "get_session_by_id", "tab": "get_tab_by_id", "window": "get_window_by_id"}[kind]
            getter = getattr(app, getter_name, None)
            if getter is not None:
                try:
                    value = await _maybe_await(getter(target_id))
                except Exception:
                    value = None
                if value is not None:
                    raise DomainError("CLOSE_UNVERIFIED", f"{kind} object remains after refresh")


__all__ = ["ClosingBackend"]
