"""Fail-closed orchestration for closing iTerm sessions, tabs, and windows.

The backend deliberately owns all iTerm and process-specific operations.  This
module owns the lifecycle policy around those operations: immutable target
selection, activity preflight, recovery receipts, identity revalidation, and
independent post-close verification.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
import secrets
import stat
import uuid
from typing import Any

from .errors import DomainError


DEFAULT_RECOVERY_DIR = "~/.local/state/goals-iterm-mcp/recovery"
SNAPSHOT_TIMEOUT = 5.0
PROBE_TIMEOUT = 5.0
EXIT_TIMEOUT = 15.0
CLOSE_TIMEOUT = 15.0
VERIFY_TIMEOUT = 5.0
TOTAL_TIMEOUT = 120.0


class _ClosingError(Exception):
    """Internal control-flow error carrying a safe public code."""

    def __init__(self, code: str, *, unknown: bool = False):
        self.code = code
        self.unknown = unknown
        super().__init__(code)


class _Cancelled(_ClosingError):
    def __init__(self):
        super().__init__("CANCELLED", unknown=True)


class _TimedOut(_ClosingError):
    def __init__(self):
        super().__init__("TIMEOUT", unknown=True)


class _BackendUnavailable(_ClosingError):
    def __init__(self):
        super().__init__("BACKEND_UNAVAILABLE", unknown=True)


class _TargetChanged(_ClosingError):
    def __init__(self):
        super().__init__("TARGET_CHANGED")


class _ExitFailed(_ClosingError):
    def __init__(self, code: str = "CLI_EXIT_FAILED", *, unknown: bool = False):
        super().__init__(code, unknown=unknown)


class _CloseFailed(_ClosingError):
    def __init__(self, code: str = "CLOSE_FAILED", *, unknown: bool = False):
        super().__init__(code, unknown=unknown)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _copy_json(value: Any) -> Any:
    """Return a JSON-safe copy without preserving arbitrary backend fields."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json(item) for item in value]
    return str(value)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _awaitable(value: Any) -> Awaitable[Any] | None:
    return value if inspect.isawaitable(value) else None


async def _call_with_timeout(method: Any, *args: Any, timeout: float) -> Any:
    """Call a backend method once, accepting synchronous test doubles."""

    if not callable(method):
        raise _BackendUnavailable()
    try:
        value = method(*args)
        awaitable = _awaitable(value)
        if awaitable is None:
            return value
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise _TimedOut() from exc
        except asyncio.CancelledError as exc:
            raise _Cancelled() from exc
    except _ClosingError:
        raise
    except DomainError as exc:
        code = exc.code if isinstance(exc.code, str) and exc.code else "BACKEND_UNAVAILABLE"
        unknown = (
            code in {"BACKEND_UNAVAILABLE", "TIMEOUT", "DISCONNECTED", "CANCELLED"}
            or code.endswith("_UNKNOWN")
            or "TIMEOUT" in code
            or "DISCONNECT" in code
        )
        raise _ClosingError(code, unknown=unknown) from exc
    except asyncio.CancelledError as exc:
        raise _Cancelled() from exc
    except Exception as exc:
        raise _BackendUnavailable() from exc


def _safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _nested_target(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    target = raw.get("target")
    return target if isinstance(target, Mapping) else {}


def _normalize_row(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _BackendUnavailable()
    target = _nested_target(raw)
    session_id = _safe_id(raw.get("session_id")) or _safe_id(target.get("session_id"))
    if session_id is None:
        raise _BackendUnavailable()
    window_id = _safe_id(raw.get("window_id")) or _safe_id(target.get("window_id"))
    tab_id = _safe_id(raw.get("tab_id")) or _safe_id(target.get("tab_id"))
    name = raw.get("name")
    if not isinstance(name, str):
        for key in ("nickname", "title"):
            if isinstance(raw.get(key), str):
                name = raw[key]
                break
    if not isinstance(name, str):
        name = None
    identity = dict(target)
    identity.setdefault("session_id", session_id)
    for key in ("session_epoch", "broker_epoch"):
        if key not in identity and isinstance(raw.get(key), str):
            identity[key] = raw[key]
    return {
        "session_id": session_id,
        "window_id": window_id,
        "tab_id": tab_id,
        "name": name,
        "target": identity,
        "_raw": dict(raw),
    }


def _row_key(row: Mapping[str, Any]) -> tuple[str | None, str | None, str | None, str]:
    return (
        _safe_id(row.get("session_id")),
        _safe_id(row.get("window_id")),
        _safe_id(row.get("tab_id")),
        _canonical(row.get("target", {})),
    )


def _same_identity(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    return _canonical(left) == _canonical(right)


def _normalized_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value.casefold() if value else None


def _normalize_resume(raw: Mapping[str, Any], cli: str | None) -> dict[str, Any]:
    nested = raw.get("resume")
    nested = nested if isinstance(nested, Mapping) else {}
    executable = (
        nested.get("executable")
        or raw.get("resume_executable")
        or raw.get("executable")
        or cli
    )
    if not isinstance(executable, str) or not executable:
        executable = cli or ""
    argv = nested.get("argv")
    if argv is None:
        argv = raw.get("resume_argv")
    if argv is None:
        argv = raw.get("argv")
    if not isinstance(argv, (list, tuple)):
        argv = []
    argv = [item for item in argv if isinstance(item, str)]
    return {"executable": executable, "argv": argv}


def _normalize_chat(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"cli": raw}
    if not isinstance(raw, Mapping):
        raise _ClosingError("CLI_IDENTITY_MISSING")
    cli = raw.get("cli") or raw.get("name") or raw.get("program")
    chat_uuid = (
        raw.get("chat_uuid")
        or raw.get("chat_id")
        or raw.get("uuid")
        or raw.get("conversation_id")
    )
    if not isinstance(cli, str) or not cli or not isinstance(chat_uuid, str) or not chat_uuid:
        raise _ClosingError("CLI_IDENTITY_MISSING")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        cwd = str(cwd)
    source = raw.get("identity_source") or raw.get("source")
    if source is not None and not isinstance(source, str):
        source = str(source)
    persistence = raw.get("persistence_status") or raw.get("persistence")
    if isinstance(persistence, Mapping):
        persistence = _copy_json(persistence)
    elif persistence is not None:
        persistence = str(persistence)
    return {
        "cli": cli,
        "chat_uuid": chat_uuid,
        "cwd": cwd,
        "identity_source": source,
        "persistence_status": persistence,
        "resume": _normalize_resume(raw, cli),
    }


def _normalize_probe(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _BackendUnavailable()
    state = raw.get("state")
    state = state.casefold() if isinstance(state, str) else "unknown"
    if state not in {"idle", "busy", "waiting", "unknown", "exited"}:
        state = "unknown"
    reason = raw.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)
    cli_raw = raw.get("cli_sessions", [])
    if cli_raw is None:
        cli_raw = []
    if not isinstance(cli_raw, (list, tuple)):
        raise _ClosingError("CLI_IDENTITY_MISSING")
    chats = [_normalize_chat(item) for item in cli_raw]
    return {
        "state": state,
        "reason": reason,
        "identity": _copy_json(raw.get("identity")),
        "cli_sessions": chats,
        "blocked_code": raw.get("blocked_code") if isinstance(raw.get("blocked_code"), str) else None,
        "_raw": dict(raw),
    }


def _session_metadata(row: Mapping[str, Any], probe: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "session_id": row.get("session_id"),
        "window_id": row.get("window_id"),
        "tab_id": row.get("tab_id"),
        "nickname": row.get("name"),
        "name": row.get("name"),
        "target": _copy_json(row.get("target", {})),
        "cli_sessions": _copy_json((probe or {}).get("cli_sessions", [])),
        "state": (probe or {}).get("state"),
        "reason": (probe or {}).get("reason"),
        "interruption": "not_attempted",
        "exit": "not_attempted",
        "closure_status": "pending",
    }
    return result


def _merge_chat_records(current: list[dict[str, Any]], raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, (list, tuple)):
        return current
    updates: dict[str, dict[str, Any]] = {}
    for item in raw:
        try:
            chat = _normalize_chat(item)
        except _ClosingError:
            continue
        updates[chat["chat_uuid"]] = chat
    result = []
    for item in current:
        update = updates.get(item.get("chat_uuid"))
        if update is None:
            result.append(item)
            continue
        merged = dict(item)
        if update.get("cwd") is not None:
            merged["cwd"] = update["cwd"]
        if update.get("identity_source") is not None:
            merged["identity_source"] = update["identity_source"]
        if update.get("persistence_status") is not None:
            merged["persistence_status"] = update["persistence_status"]
        merged["resume"] = update["resume"]
        result.append(merged)
    return result


class RecoveryStore:
    """Private, durable, metadata-only JSON recovery receipts.

    ``persist`` writes one unique receipt atomically and returns a mapping with
    the stable record ID and path.  ``update`` replaces an existing receipt
    atomically for the per-step journal.  Both operations reject symlinks at
    the receipt directory/file boundary.
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None):
        value = directory
        if value is None:
            value = os.environ.get("GOALS_ITERM_MCP_RECOVERY_DIR") or DEFAULT_RECOVERY_DIR
        self.directory = Path(value).expanduser()

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise OSError("recovery path may not be a symlink")

    def _ensure_directory(self) -> None:
        self._reject_symlink(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._reject_symlink(self.directory)
        if not self.directory.is_dir():
            raise OSError("recovery path is not a directory")
        os.chmod(self.directory, 0o700)

    @staticmethod
    def _atomic_json(path: Path, record: Mapping[str, Any], *, replace: bool) -> None:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        RecoveryStore._reject_symlink(path.parent)
        if replace:
            RecoveryStore._reject_symlink(path)
        elif os.path.lexists(path):
            raise FileExistsError(str(path))
        token = secrets.token_hex(12)
        temporary = path.parent / f".{path.name}.{token}.tmp"
        fd = -1
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temporary, flags | nofollow, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            if replace:
                RecoveryStore._reject_symlink(path)
            elif os.path.lexists(path):
                raise FileExistsError(str(path))
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                dir_fd = -1
            if dir_fd >= 0:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def persist(self, record: Mapping[str, Any]) -> dict[str, str]:
        self._ensure_directory()
        record_id = record.get("recovery_record_id")
        if not isinstance(record_id, str) or not record_id:
            record_id = uuid.uuid4().hex
        path = self.directory / f"{record_id}.json"
        data = dict(record)
        data["recovery_record_id"] = record_id
        self._atomic_json(path, data, replace=False)
        return {"recovery_record_id": record_id, "recovery_record_path": str(path)}

    def update(self, path: str | os.PathLike[str], record: Mapping[str, Any]) -> dict[str, str]:
        self._ensure_directory()
        target = Path(path)
        if target.parent != self.directory:
            raise OSError("recovery receipt path escaped recovery directory")
        self._atomic_json(target, dict(record), replace=True)
        record_id = record.get("recovery_record_id")
        return {
            "recovery_record_id": record_id if isinstance(record_id, str) else target.stem,
            "recovery_record_path": str(target),
        }

    # ``write``/``save`` keep small custom adapters ergonomic without changing
    # the CloseService contract.  The service always prefers ``persist``.
    write = persist
    save = persist


@dataclass
class _Plan:
    kind: str
    target: dict[str, Any]
    rows: list[dict[str, Any]]
    probes: dict[str, dict[str, Any]]
    skipped: list[dict[str, Any]]
    prefailed: list[dict[str, Any]]
    code: str | None = None
    blocking: list[str] | None = None


class CloseService:
    """Plan and execute one immutable close operation against a backend."""

    def __init__(
        self,
        backend: Any,
        allowed: Iterable[str] | None = None,
        store: Any = None,
    ):
        self.backend = backend
        if allowed is None:
            self.allowed: frozenset[str] | None = None
        elif isinstance(allowed, str):
            self.allowed = frozenset({allowed})
        else:
            self.allowed = frozenset(item for item in allowed if isinstance(item, str))
        if isinstance(store, (str, os.PathLike)):
            self.store = RecoveryStore(store)
        else:
            self.store = store if store is not None else RecoveryStore()
        self._record: dict[str, Any] | None = None
        self._record_path: str | None = None
        self._journal_failed = False
        self._cancelled = False
        self._post_exit_scope_changed = False

    @staticmethod
    def _operation(kind: str) -> str:
        return {
            "session": "close_session",
            "tab": "close_tab",
            "window": "close_window",
            "all": "clean_all_sessions",
        }.get(kind, "close_session")

    @staticmethod
    def _base_result(kind: str, force: bool, dry_run: bool, target: Any = None) -> dict[str, Any]:
        return {
            "operation": CloseService._operation(kind),
            "status": "unknown",
            "force": bool(force),
            "dry_run": bool(dry_run),
            "target": target,
            "recovery_record_id": None,
            "recovery_record_path": None,
            "closed_sessions": [],
            "skipped_sessions": [],
            "failed_sessions": [],
            "unknown_sessions": [],
            "code": None,
            "blocking_sessions": [],
        }

    @staticmethod
    def _row_result(row: Mapping[str, Any], probe: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return _session_metadata(row, probe)

    def _in_scope(self, row: Mapping[str, Any]) -> bool:
        return self.allowed is None or row.get("session_id") in self.allowed

    @staticmethod
    def _find_rows(rows: Sequence[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
        return [row for row in rows if row.get("session_id") == session_id]

    def _selector_rows(
        self,
        kind: str,
        rows: list[dict[str, Any]],
        *,
        session_id: str | None,
        nickname: str | None,
        tab_id: str | None,
        window_id: str | None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
        selectors = {
            "session_id": session_id,
            "nickname": nickname,
            "tab_id": tab_id,
            "window_id": window_id,
        }
        if kind == "all":
            if any(value is not None for value in selectors.values()):
                raise _ClosingError("INVALID_SELECTOR")
            selected = [row for row in rows if self._in_scope(row)]
            return selected, {"kind": "all", "session_ids": [row["session_id"] for row in selected]}, None
        valid_keys = {"session_id", "nickname"} if kind == "session" else (
            {"tab_id", "session_id", "nickname"} if kind == "tab" else {"window_id", "session_id", "nickname"}
        )
        provided = [key for key, value in selectors.items() if key in valid_keys and value is not None]
        if len(provided) != 1:
            raise _ClosingError("INVALID_SELECTOR")
        key = provided[0]
        value = selectors[key]
        if not isinstance(value, str) or not value or (key == "nickname" and not value.strip()):
            raise _ClosingError("INVALID_SELECTOR")
        if key in {"session_id", "tab_id", "window_id"} and value.casefold() in {
            "active",
            "all",
            "current",
            "focused",
            "*",
        }:
            raise _ClosingError("INVALID_SELECTOR")
        if key == "session_id":
            matches = self._find_rows(rows, value)
        elif key == "nickname":
            wanted = _normalized_name(value)
            matches = [row for row in rows if _normalized_name(row.get("name")) == wanted]
        elif key == "tab_id":
            matches = [row for row in rows if row.get("tab_id") == value]
        else:
            matches = [row for row in rows if row.get("window_id") == value]
        if key == "nickname" and len(matches) > 1:
            raise _ClosingError("AMBIGUOUS_SELECTOR")
        # A tab/window ID naturally appears on every session in that parent;
        # only an explicit session ID must identify one row here.
        if key == "session_id" and len(matches) > 1:
            raise _ClosingError("AMBIGUOUS_SELECTOR")
        if key == "tab_id" and len({row.get("window_id") for row in matches}) > 1:
            raise _ClosingError("AMBIGUOUS_SELECTOR")
        if not matches:
            raise _ClosingError("SELECTOR_NOT_FOUND")
        anchor = matches[0]
        if kind == "session":
            selected = matches
        elif kind == "tab":
            parent = anchor.get("tab_id")
            if not parent:
                raise _ClosingError("TARGET_ID_UNAVAILABLE")
            selected = [row for row in rows if row.get("tab_id") == parent]
        else:
            parent = anchor.get("window_id")
            if not parent:
                raise _ClosingError("TARGET_ID_UNAVAILABLE")
            selected = [row for row in rows if row.get("window_id") == parent]
        target_id = anchor.get("session_id") if kind == "session" else anchor.get(f"{kind}_id")
        target = {
            "kind": kind,
            "session_id": anchor.get("session_id") if kind == "session" else None,
            "tab_id": anchor.get("tab_id") if kind == "tab" else None,
            "window_id": anchor.get("window_id") if kind == "window" else None,
            "session_ids": [row["session_id"] for row in selected],
        }
        return selected, target, target_id

    async def _snapshot(self) -> list[dict[str, Any]]:
        raw = await _call_with_timeout(getattr(self.backend, "snapshot", None), timeout=SNAPSHOT_TIMEOUT)
        if not isinstance(raw, (list, tuple)):
            raise _BackendUnavailable()
        rows = [_normalize_row(item) for item in raw]
        ids = [row["session_id"] for row in rows]
        if len(set(ids)) != len(ids):
            raise _BackendUnavailable()
        return rows

    async def _probe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        value = await _call_with_timeout(getattr(self.backend, "probe", None), row, timeout=PROBE_TIMEOUT)
        return _normalize_probe(value)

    async def _preflight(
        self,
        kind: str,
        rows: list[dict[str, Any]],
        target: dict[str, Any],
        *,
        force: bool,
    ) -> _Plan:
        probes: dict[str, dict[str, Any]] = {}
        skipped: list[dict[str, Any]] = []
        prefailed: list[dict[str, Any]] = []
        blocking: list[str] = []
        probe_failure: str | None = None
        for current in rows:
            sid = current["session_id"]
            try:
                probe = await self._probe(current)
            except _ClosingError as exc:
                if isinstance(exc, _Cancelled):
                    raise
                probe_failure = probe_failure or exc.code
                result = self._row_result(current)
                result.update({"state": "unknown", "reason": exc.code, "closure_status": "retained"})
                if kind == "all" and not force:
                    skipped.append(result)
                else:
                    prefailed.append(result)
                continue
            probes[sid] = probe
            result = self._row_result(current, probe)
            state = probe["state"]
            if probe.get("blocked_code"):
                result.update({"closure_status": "retained", "skip_code": probe["blocked_code"]})
                if kind == "all" and not force:
                    skipped.append(result)
                else:
                    prefailed.append(result)
                continue
            if kind == "all" and not force and state != "idle":
                result.update({"closure_status": "skipped", "skip_code": probe.get("blocked_code") or "ACTIVITY_BLOCKED"})
                skipped.append(result)
                continue
            if state == "exited":
                result.update({"closure_status": "retained", "skip_code": "SESSION_GONE"})
                if kind == "all":
                    skipped.append(result)
                else:
                    prefailed.append(result)
                continue
            if state != "idle" and not force:
                blocking.append(sid)
            # A force operation bypasses activity state, but not missing CLI
            # identity or a missing stable process identity.
            if probe.get("cli_sessions") and not probe.get("identity"):
                result.update({"closure_status": "retained", "skip_code": "PROCESS_IDENTITY_MISSING"})
                if kind == "all" and force:
                    prefailed.append(result)
                elif kind == "all":
                    skipped.append(result)
                else:
                    prefailed.append(result)
                continue
        if kind != "all" and blocking:
            return _Plan(kind, target, rows, probes, [], [], "ACTIVITY_BLOCKED", blocking)
        if kind != "all" and prefailed:
            code = next((item.get("skip_code") for item in prefailed if item.get("skip_code")), None)
            return _Plan(kind, target, rows, probes, [], prefailed, code or probe_failure or "PREFLIGHT_FAILED", blocking)
        if kind == "all" and not force:
            candidates = [row for row in rows if row["session_id"] in probes and row["session_id"] not in {item.get("session_id") for item in skipped}]
        elif kind == "all" and force:
            candidates = [row for row in rows if row["session_id"] in probes and row["session_id"] not in {item.get("session_id") for item in prefailed}]
        else:
            candidates = list(rows)
        # Force cleanup attempts every authorized session.  A failed probe is
        # retained in ``prefailed``; no new session can enter this plan.
        return _Plan(kind, target, candidates, probes, skipped, prefailed, None, blocking)

    def _base_receipt(self, plan: _Plan, force: bool) -> dict[str, Any]:
        return {
            "recovery_record_id": uuid.uuid4().hex,
            "operation": self._operation(plan.kind),
            "force": bool(force),
            "target": _copy_json(plan.target),
            "sessions": [
                {
                    "session_id": row.get("session_id"),
                    "window_id": row.get("window_id"),
                    "tab_id": row.get("tab_id"),
                    "nickname": row.get("name"),
                    "target": _copy_json(row.get("target", {})),
                    "cli_sessions": _copy_json(plan.probes.get(row["session_id"], {}).get("cli_sessions", [])),
                    "interruption": "not_attempted",
                    "exit": "not_attempted",
                    "closure_status": "pending",
                }
                for row in plan.rows
            ],
            "steps": [],
            "status": "planned",
        }

    async def _persist_receipt(self, receipt: dict[str, Any]) -> tuple[str | None, str | None]:
        method = getattr(self.store, "persist", None) or getattr(self.store, "write", None) or getattr(self.store, "save", None)
        if not callable(method):
            raise OSError("recovery store is unavailable")
        try:
            value = method(receipt)
            if inspect.isawaitable(value):
                value = await value
        except asyncio.CancelledError as exc:
            raise _Cancelled() from exc
        except Exception as exc:
            raise _ClosingError("RECOVERY_PERSISTENCE_FAILED") from exc
        record_id = receipt.get("recovery_record_id")
        path: str | None = None
        if isinstance(value, Mapping):
            candidate_id = value.get("recovery_record_id")
            candidate_path = value.get("recovery_record_path")
            if isinstance(candidate_id, str) and candidate_id:
                record_id = candidate_id
            if isinstance(candidate_path, str) and candidate_path:
                path = candidate_path
        elif isinstance(value, (tuple, list)) and len(value) >= 2:
            if isinstance(value[0], str) and value[0]:
                record_id = value[0]
            if isinstance(value[1], str) and value[1]:
                path = value[1]
        elif isinstance(value, str) and value:
            path = value
        receipt["recovery_record_id"] = record_id
        return record_id if isinstance(record_id, str) else None, path

    async def _journal(self, step: Mapping[str, Any], *, status: str | None = None) -> None:
        if self._record is None:
            return
        if self._journal_failed:
            return
        self._record.setdefault("steps", []).append(_copy_json(step))
        if status is not None:
            self._record["status"] = status
        if not self._record_path:
            return
        method = getattr(self.store, "update", None)
        if not callable(method):
            return
        try:
            value = method(self._record_path, self._record)
            if inspect.isawaitable(value):
                await value
        except asyncio.CancelledError:
            self._journal_failed = True
            self._cancelled = True
        except Exception:
            self._journal_failed = True

    @staticmethod
    def _lookup_row(rows: Sequence[dict[str, Any]], sid: str) -> dict[str, Any] | None:
        for row in rows:
            if row.get("session_id") == sid:
                return row
        return None

    async def _revalidate(
        self,
        row: dict[str, Any],
        expected_rows: Sequence[dict[str, Any]],
        probe: Mapping[str, Any],
        *,
        kind: str,
        force: bool,
        completed_rows: Sequence[dict[str, Any]] = (),
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        current_rows = await self._snapshot()
        expected_keys = {_row_key(item) for item in expected_rows}
        if kind in {"tab", "window"}:
            parent_key = "tab_id" if kind == "tab" else "window_id"
            parent = row.get(parent_key)
            current_parent = [item for item in current_rows if item.get(parent_key) == parent]
            if any(not self._in_scope(item) for item in current_parent):
                raise _TargetChanged()
            current_by_sid = {item["session_id"]: item for item in current_parent}
            completed_ids = {item["session_id"] for item in completed_rows}
            # A successful exit may cause iTerm to remove a session before
            # the next sibling write.  Such a disappearance is expected, but
            # every not-yet-exited member must still match the immutable
            # initial target and no new member may enter the parent.
            if any(_row_key(item) not in expected_keys for item in current_parent):
                raise _TargetChanged()
            current_global_by_sid = {item["session_id"]: item for item in current_rows}
            for expected in expected_rows:
                sid = expected["session_id"]
                observed = current_by_sid.get(sid)
                if sid in completed_ids:
                    global_observed = current_global_by_sid.get(sid)
                    if global_observed is not None and observed is None:
                        # The completed session still exists, but moved out
                        # of the original parent.  It was not auto-closed.
                        raise _TargetChanged()
                    if observed is not None and _row_key(observed) != _row_key(expected):
                        raise _TargetChanged()
                    continue
                if observed is None or _row_key(observed) != _row_key(expected):
                    raise _TargetChanged()
            current = current_by_sid.get(row["session_id"])
            if current is None or _row_key(current) != _row_key(row):
                raise _TargetChanged()
            row = current
        else:
            current = self._lookup_row(current_rows, row["session_id"])
            if current is None or _row_key(current) != _row_key(row):
                raise _TargetChanged()
            row = current
        fresh_probe = await self._probe(row)
        if fresh_probe.get("blocked_code"):
            # ``force`` only bypasses activity state.  A newly observed
            # protection/identity block still prevents the write.
            raise _ClosingError(fresh_probe["blocked_code"])
        if not _same_identity(probe.get("identity"), fresh_probe.get("identity")):
            raise _TargetChanged()
        initial_chats = {item.get("chat_uuid") for item in probe.get("cli_sessions", [])}
        fresh_chats = {item.get("chat_uuid") for item in fresh_probe.get("cli_sessions", [])}
        if initial_chats != fresh_chats:
            raise _TargetChanged()
        if not force and fresh_probe.get("state") != "idle":
            raise _ClosingError("ACTIVITY_BLOCKED")
        return row, fresh_probe

    @staticmethod
    def _exit_status(value: Any, has_cli: bool) -> tuple[str, str, list[dict[str, Any]]]:
        if not isinstance(value, Mapping):
            raise _ExitFailed("CLI_EXIT_FAILED", unknown=True)
        if "cli_exit_status" not in value:
            raise _ExitFailed("CLI_EXIT_STATUS_MISSING")
        if "task_interrupted" not in value or not isinstance(value.get("task_interrupted"), bool):
            raise _ExitFailed("TASK_INTERRUPTION_STATUS_MISSING")
        status = value.get("cli_exit_status")
        expected = "exited" if has_cli else "not_needed"
        if status not in {"not_needed", "exited", "failed", "unknown"}:
            raise _ExitFailed("CLI_EXIT_FAILED", unknown=True)
        if status != expected and not (not has_cli and status == "exited"):
            raise _ExitFailed("CLI_EXIT_FAILED", unknown=status == "unknown")
        interruption = value.get("task_interrupted")
        if interruption is True:
            interruption_status = "interrupted"
        elif interruption is False:
            interruption_status = "not_interrupted"
        else:
            interruption_status = "not_needed" if not has_cli else "unknown"
        updates = value.get("chat_persistence")
        if updates is None:
            updates = value.get("chat_sessions")
        return status, interruption_status, updates if isinstance(updates, list) else []

    async def _execute_one(
        self,
        row: dict[str, Any],
        probe: dict[str, Any],
        plan: _Plan,
        *,
        force: bool,
        session_results: dict[str, dict[str, Any]],
    ) -> tuple[str, _ClosingError | None]:
        sid = row["session_id"]
        result = session_results[sid]
        try:
            completed_rows = [
                candidate
                for candidate in plan.rows
                if candidate["session_id"] != sid
                and session_results[candidate["session_id"]].get("exit") in {"exited", "not_needed"}
            ]
            checked_row, checked_probe = await self._revalidate(
                row,
                plan.rows if plan.kind in {"tab", "window"} else [row],
                probe,
                kind=plan.kind,
                force=force,
                completed_rows=completed_rows,
            )
            value = await _call_with_timeout(
                getattr(self.backend, "exit_session", None), checked_row, checked_probe, force, timeout=EXIT_TIMEOUT
            )
            status, interruption, updates = self._exit_status(value, bool(checked_probe.get("cli_sessions")))
            result["interruption"] = interruption
            result["exit"] = status
            result["cli_sessions"] = _merge_chat_records(result.get("cli_sessions", []), updates)
            await self._journal({"step": "exit_session", "session_id": sid, "status": status})
            return "exited", None
        except _ClosingError as exc:
            if isinstance(exc, _Cancelled):
                self._cancelled = True
            result["closure_status"] = "unknown" if exc.unknown else "failed"
            result["exit"] = "unknown" if exc.unknown else "failed"
            result["reason"] = exc.code
            await self._journal({"step": "exit_session", "session_id": sid, "status": exc.code})
            return ("unknown" if exc.unknown else "failed"), exc

    async def _verify_absent(self, expected_rows: Sequence[dict[str, Any]], *, kind: str) -> None:
        current_rows = await _call_with_timeout(getattr(self.backend, "snapshot", None), timeout=VERIFY_TIMEOUT)
        if not isinstance(current_rows, (list, tuple)):
            raise _BackendUnavailable()
        normalized = [_normalize_row(item) for item in current_rows]
        expected_ids = {item["session_id"] for item in expected_rows}
        if any(item.get("session_id") in expected_ids for item in normalized):
            raise _CloseFailed("CLOSE_UNVERIFIED")

    async def _execute_plan(self, plan: _Plan, result: dict[str, Any], *, force: bool) -> dict[str, Any]:
        session_results: dict[str, dict[str, Any]] = {}
        for row in plan.rows:
            session_results[row["session_id"]] = self._row_result(row, plan.probes.get(row["session_id"]))
        for item in plan.prefailed:
            item.setdefault("closure_status", "failed")
            result["failed_sessions"].append(item)
        if not plan.rows:
            if result["failed_sessions"] or result["unknown_sessions"]:
                result["status"] = "partial" if result["failed_sessions"] else "unknown"
                result["code"] = "PREFLIGHT_FAILED" if result["failed_sessions"] else "BACKEND_UNAVAILABLE"
            else:
                result["status"] = "noop"
            return result
        for row in plan.rows:
            status, _error = await self._execute_one(
                row,
                plan.probes[row["session_id"]],
                plan,
                force=force,
                session_results=session_results,
            )
            if status != "exited":
                item = session_results[row["session_id"]]
                if status == "unknown":
                    result["unknown_sessions"].append(item)
                else:
                    result["failed_sessions"].append(item)
            if self._journal_failed and status == "exited":
                item = session_results[row["session_id"]]
                item["closure_status"] = "unknown"
                item["reason"] = "RECOVERY_PERSISTENCE_FAILED"
                if item not in result["unknown_sessions"]:
                    result["unknown_sessions"].append(item)
            if self._cancelled or self._journal_failed:
                break
        exited = [row for row in plan.rows if session_results[row["session_id"]]["exit"] in {"exited", "not_needed"}]
        auto_closed: set[str] = set()
        changed_after_exit: set[str] = set()
        remaining_parent_rows: list[dict[str, Any]] = []
        if exited and not self._cancelled and not self._journal_failed:
            try:
                post_exit_rows = await self._snapshot()
                if plan.kind in {"tab", "window"}:
                    parent_key = f"{plan.kind}_id"
                    parent_id = plan.target.get(parent_key)
                    current_parent = [item for item in post_exit_rows if item.get(parent_key) == parent_id]
                    expected_keys = {_row_key(item) for item in plan.rows}
                    current_keys = {_row_key(item) for item in current_parent}
                    if any(key not in expected_keys for key in current_keys):
                        self._post_exit_scope_changed = True
                    current_by_sid = {item["session_id"]: item for item in current_parent}
                    current_global_by_sid = {item["session_id"]: item for item in post_exit_rows}
                    for current in exited:
                        sid = current["session_id"]
                        observed = current_by_sid.get(sid)
                        if observed is None:
                            # A session absent from the original parent is
                            # auto-closed only when it is absent globally.
                            # A live row under another parent is target drift.
                            if sid in current_global_by_sid:
                                changed_after_exit.add(sid)
                                self._post_exit_scope_changed = True
                            else:
                                auto_closed.add(sid)
                        elif _row_key(observed) != _row_key(current):
                            changed_after_exit.add(sid)
                            self._post_exit_scope_changed = True
                        else:
                            remaining_parent_rows.append(observed)
                else:
                    for current in exited:
                        sid = current["session_id"]
                        observed = self._lookup_row(post_exit_rows, sid)
                        if observed is None:
                            auto_closed.add(sid)
                        elif _row_key(observed) != _row_key(current):
                            changed_after_exit.add(sid)
                            self._post_exit_scope_changed = True
            except _ClosingError as exc:
                # A lost post-exit snapshot leaves the side effect outcome
                # unknown.  Do not retry it or infer disappearance.
                self._post_exit_scope_changed = True
                result["code"] = exc.code
        # Preserve the immutable plan order in structured results; set
        # membership above is only used for classification.
        for row in plan.rows:
            sid = row["session_id"]
            if sid not in auto_closed:
                continue
            item = session_results[sid]
            item["closure_status"] = "closed"
            item["closure_method"] = "auto_closed_after_exit"
            await self._journal({"step": "verify", "session_id": sid, "status": "auto_closed_after_exit"})
            if item not in result["closed_sessions"]:
                result["closed_sessions"].append(item)
        for row in plan.rows:
            sid = row["session_id"]
            if sid not in changed_after_exit:
                continue
            item = session_results[sid]
            item["closure_status"] = "unknown"
            item["reason"] = "TARGET_CHANGED"
            if item not in result["unknown_sessions"]:
                result["unknown_sessions"].append(item)
        # A post-exit topology change (or a failed refresh) invalidates any
        # successful exit that we could not classify as an observed
        # auto-closure.  Preserve those side effects as unknown and never
        # fall through to a native close based on stale membership.
        if self._post_exit_scope_changed:
            for row in exited:
                sid = row["session_id"]
                if sid in auto_closed or sid in changed_after_exit:
                    continue
                item = session_results[sid]
                item["closure_status"] = "unknown"
                item["reason"] = result.get("code") or "TARGET_CHANGED"
                if item not in result["unknown_sessions"] and item not in result["failed_sessions"] and item not in result["closed_sessions"]:
                    result["unknown_sessions"].append(item)
        # A parent close is valid only when every affected session completed
        # its CLI exit.  For cleanup each session is its own target.
        close_groups: list[tuple[str, str, list[dict[str, Any]]]] = []
        if not self._cancelled and not self._journal_failed and plan.kind in {"tab", "window"}:
            if len(exited) == len(plan.rows) and not self._post_exit_scope_changed and remaining_parent_rows:
                target_id = plan.target.get(f"{plan.kind}_id")
                if isinstance(target_id, str):
                    close_groups.append((plan.kind, target_id, list(remaining_parent_rows)))
        elif not self._cancelled and not self._journal_failed and not self._post_exit_scope_changed:
            close_groups.extend(
                ("session", row["session_id"], [row])
                for row in exited
                if row["session_id"] not in auto_closed
                and row["session_id"] not in changed_after_exit
            )
        for close_kind, target_id, expected_rows in close_groups:
            try:
                # Recheck the exact parent membership immediately before the
                # native close.  This is separate from the pre-exit check.
                current_rows = await self._snapshot()
                if close_kind in {"tab", "window"}:
                    parent = expected_rows[0].get(f"{close_kind}_id")
                    current_parent = [item for item in current_rows if item.get(f"{close_kind}_id") == parent]
                    if {_row_key(item) for item in current_parent} != {_row_key(item) for item in expected_rows} or any(not self._in_scope(item) for item in current_parent):
                        raise _TargetChanged()
                else:
                    current = self._lookup_row(current_rows, expected_rows[0]["session_id"])
                    if current is None or _row_key(current) != _row_key(expected_rows[0]):
                        raise _TargetChanged()
                await _call_with_timeout(
                    getattr(self.backend, "close_target", None),
                    close_kind,
                    target_id,
                    expected_rows,
                    force,
                    timeout=CLOSE_TIMEOUT,
                )
                await self._journal({"step": "close_target", "kind": close_kind, "target_id": target_id, "status": "acknowledged"})
                await self._verify_absent(expected_rows, kind=close_kind)
                await self._journal({"step": "verify", "kind": close_kind, "target_id": target_id, "status": "closed"})
                for expected in expected_rows:
                    item = session_results[expected["session_id"]]
                    item["closure_status"] = "closed"
                    result["closed_sessions"].append(item)
            except _ClosingError as exc:
                if isinstance(exc, _Cancelled):
                    self._cancelled = True
                for expected in expected_rows:
                    item = session_results[expected["session_id"]]
                    item["closure_status"] = "unknown" if exc.unknown else "failed"
                    item["reason"] = exc.code
                    if item not in result["unknown_sessions"] and item not in result["failed_sessions"] and item not in result["closed_sessions"]:
                        (result["unknown_sessions"] if exc.unknown else result["failed_sessions"]).append(item)
                await self._journal({"step": "close_target", "kind": close_kind, "target_id": target_id, "status": exc.code})
                if self._cancelled or self._journal_failed:
                    break
        # Parent close failure due one sibling exit must retain every sibling.
        if plan.kind in {"tab", "window"} and len(exited) != len(plan.rows):
            for row in plan.rows:
                item = session_results[row["session_id"]]
                if item not in result["unknown_sessions"] and item not in result["failed_sessions"] and item not in result["closed_sessions"]:
                    item["closure_status"] = "failed"
                    item["reason"] = "PARENT_NOT_CLOSED"
                    result["failed_sessions"].append(item)
        if self._cancelled:
            for item in session_results.values():
                if item.get("closure_status") != "closed" and item not in result["unknown_sessions"] and item not in result["failed_sessions"]:
                    item["closure_status"] = "unknown"
                    item["reason"] = "CANCELLED"
                    result["unknown_sessions"].append(item)
            result["status"] = "unknown"
            result["code"] = "CANCELLED"
        elif self._journal_failed:
            result["code"] = "RECOVERY_PERSISTENCE_FAILED"
            result["status"] = "unknown"
        elif self._post_exit_scope_changed:
            result["status"] = "unknown"
            result["code"] = result.get("code") or "TARGET_CHANGED"
        elif (
            not result["closed_sessions"]
            and not result["unknown_sessions"]
            and result["failed_sessions"]
            and all(item.get("reason") in {"TARGET_CHANGED", "ACTIVITY_BLOCKED"} for item in result["failed_sessions"])
        ):
            # A fresh pre-write check can reject a plan after the receipt was
            # safely persisted.  No terminal write happened, so this remains
            # a refusal rather than a partial lifecycle operation.
            result["status"] = "refused"
            result["code"] = result["failed_sessions"][0].get("reason")
        elif result["unknown_sessions"]:
            result["status"] = "unknown" if not result["closed_sessions"] else "partial"
        elif result["failed_sessions"] or result["skipped_sessions"]:
            result["status"] = "partial" if result["closed_sessions"] else "partial"
        elif result["closed_sessions"]:
            result["status"] = "closed"
        else:
            result["status"] = "noop"
        final_status = result["status"]
        await self._journal({"step": "complete", "status": final_status}, status=final_status)
        if self._cancelled:
            result["status"] = "unknown"
            result["code"] = "CANCELLED"
        elif self._journal_failed:
            result["status"] = "unknown"
            result["code"] = "RECOVERY_PERSISTENCE_FAILED"
        return result

    async def _run_inner(
        self,
        kind: str,
        *,
        session_id: str | None = None,
        nickname: str | None = None,
        tab_id: str | None = None,
        window_id: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._record = None
        self._record_path = None
        self._journal_failed = False
        self._cancelled = False
        self._post_exit_scope_changed = False
        result = self._base_result(kind, force, dry_run)
        if kind not in {"session", "tab", "window", "all"}:
            result.update({"status": "refused", "code": "INVALID_KIND"})
            return result
        try:
            rows = await self._snapshot()
            selected, target, _target_id = self._selector_rows(
                kind,
                rows,
                session_id=session_id,
                nickname=nickname,
                tab_id=tab_id,
                window_id=window_id,
            )
            result["target"] = target
            if kind != "all" and any(not self._in_scope(row) for row in selected):
                result.update({"status": "refused", "code": "OUT_OF_SCOPE"})
                return result
            plan = await self._preflight(kind, selected, target, force=bool(force))
            result["skipped_sessions"] = list(plan.skipped)
            if plan.blocking:
                if dry_run:
                    for sid in plan.blocking:
                        row = self._lookup_row(selected, sid)
                        probe = plan.probes.get(sid)
                        if row is not None:
                            detail = self._row_result(row, probe)
                            detail.update({"closure_status": "retained", "reason": "ACTIVITY_BLOCKED"})
                            result["failed_sessions"].append(detail)
                    result.update({"status": "dry_run", "code": plan.code or "ACTIVITY_BLOCKED", "blocking_sessions": list(plan.blocking)})
                    result["would_close_sessions"] = []
                    result["would_skip_sessions"] = list(result["failed_sessions"])
                    return result
                for sid in plan.blocking:
                    row = self._lookup_row(selected, sid)
                    probe = plan.probes.get(sid)
                    if row is None:
                        continue
                    detail = self._row_result(row, probe)
                    detail.update({"closure_status": "retained", "reason": "ACTIVITY_BLOCKED"})
                    result["failed_sessions"].append(detail)
                result.update({"status": "refused", "code": plan.code or "ACTIVITY_BLOCKED", "blocking_sessions": list(plan.blocking)})
                result["skipped_sessions"] = []
                return result
            if plan.prefailed and kind != "all":
                result["failed_sessions"] = list(plan.prefailed)
                if dry_run:
                    result.update({"status": "dry_run", "code": plan.code or "PREFLIGHT_FAILED", "blocking_sessions": [item.get("session_id") for item in plan.prefailed]})
                    result["would_close_sessions"] = []
                    result["would_skip_sessions"] = list(plan.prefailed)
                    return result
                result.update({"status": "refused", "code": plan.code or "PREFLIGHT_FAILED", "blocking_sessions": [item.get("session_id") for item in plan.prefailed]})
                result["skipped_sessions"] = []
                return result
            if dry_run:
                result["status"] = "dry_run"
                result["would_close_sessions"] = [self._row_result(row, plan.probes.get(row["session_id"])) for row in plan.rows]
                result["would_skip_sessions"] = list(plan.skipped) + list(plan.prefailed)
                return result
            if not plan.rows:
                if plan.prefailed:
                    result["failed_sessions"] = list(plan.prefailed)
                    result["status"] = "partial"
                    result["code"] = plan.code or "PREFLIGHT_FAILED"
                    return result
                if result["failed_sessions"] or result["unknown_sessions"]:
                    result["status"] = "partial" if result["failed_sessions"] else "unknown"
                    result["code"] = "PREFLIGHT_FAILED" if result["failed_sessions"] else "BACKEND_UNAVAILABLE"
                else:
                    result["status"] = "noop"
                return result
            receipt = self._base_receipt(plan, bool(force))
            record_id, record_path = await self._persist_receipt(receipt)
            self._record = receipt
            self._record_path = record_path
            result["recovery_record_id"] = record_id
            result["recovery_record_path"] = record_path
            await self._journal({"step": "persist_receipt", "status": "persisted"}, status="executing")
            if self._journal_failed:
                # The receipt exists but its first journal update did not
                # persist.  Stop before any terminal input/native close.
                result["status"] = "refused"
                result["code"] = "RECOVERY_PERSISTENCE_FAILED"
                return result
            if self._cancelled:
                result["status"] = "unknown"
                result["code"] = "CANCELLED"
                return result
            return await self._execute_plan(plan, result, force=bool(force))
        except _ClosingError as exc:
            result["status"] = "unknown" if exc.unknown else "refused"
            result["code"] = exc.code
            if isinstance(exc, _Cancelled) and self._record is not None:
                await self._journal({"step": "cancelled", "status": "CANCELLED"}, status="unknown")
            return result
        except asyncio.CancelledError:
            result["status"] = "unknown"
            result["code"] = "CANCELLED"
            if self._record is not None:
                await self._journal({"step": "cancelled", "status": "CANCELLED"}, status="unknown")
            return result
        except Exception:
            result["status"] = "unknown"
            result["code"] = "BACKEND_UNAVAILABLE"
            return result

    async def run(
        self,
        kind: str,
        *,
        session_id: str | None = None,
        nickname: str | None = None,
        tab_id: str | None = None,
        window_id: str | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Run one close operation with a bounded overall orchestration time."""

        try:
            return await asyncio.wait_for(
                self._run_inner(
                    kind,
                    session_id=session_id,
                    nickname=nickname,
                    tab_id=tab_id,
                    window_id=window_id,
                    force=force,
                    dry_run=dry_run,
                ),
                timeout=TOTAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            result = self._base_result(kind, force, dry_run)
            result.update({"status": "unknown", "code": "TIMEOUT"})
            return result


__all__ = ["CloseService", "RecoveryStore"]
