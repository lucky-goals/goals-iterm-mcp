"""iTerm Python API backend for direct MCP operations."""

from __future__ import annotations
import asyncio
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
import importlib
import inspect
import json
import uuid
from typing import Any
from .backend_types import MAX_HISTORY_COORDINATE, CaptureDTO, CaptureLineDTO, Target
from .errors import DomainError
from .shutdown import (
    BoundedWaitResult,
    merge_wait_results,
    validate_timeout,
    wait_bounded,
)
from .screen_coordinates import screen_cursor_coord


MAX_CAPTURE_BYTES = 1 * 1024 * 1024


MAX_HISTORY_LINES = 2_000


MAX_INPUT_BYTES = 16 * 1024


MAX_SESSION_RECORDS = 256


MAX_TOPOLOGY_DEPTH = 128


MAX_TOPOLOGY_NODES = 4096


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _as_target(value: Any) -> dict[str, str]:
    """Return the three-field target identity used by every backend call."""

    if is_dataclass(value):
        value = {item.name: getattr(value, item.name) for item in fields(value)}
    elif not isinstance(value, Mapping) and hasattr(value, "to_dict"):
        value = value.to_dict()
    if isinstance(value, str):
        raise DomainError(
            "INVALID_ARGUMENT",
            "target must include session, session, and broker epochs",
        )
    if not isinstance(value, Mapping):
        raise DomainError("INVALID_ARGUMENT", "target must be a mapping")
    result: dict[str, str] = {}
    for name in ("session_id", "session_epoch", "broker_epoch"):
        item = value.get(name)
        if not isinstance(item, str) or not item:
            raise DomainError(
                "INVALID_ARGUMENT", f"target.{name} must be a non-empty string"
            )
        try:
            item.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise DomainError(
                "INVALID_ARGUMENT", f"target.{name} is not valid Unicode"
            ) from exc
        result[name] = item
    return result


def _text_bytes(value: str) -> int:
    if not isinstance(value, str):
        raise DomainError("INVALID_ARGUMENT", "terminal text must be a string")
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise DomainError(
            "INVALID_ARGUMENT", "terminal text is not valid Unicode"
        ) from exc


SessionDescriptor = dict[str, Any]


def _unknown_context() -> dict[str, Any]:
    """Context shape consumed by ``ObservationEngine``.

    Unknown is explicit.  An empty string or a title-derived process name
    would incorrectly turn missing information into an authorization hint.
    """

    return {
        name: {
            "value": None,
            "source": "unavailable",
            "quality": "unknown",
            "observed_at": None,
        }
        for name in ("program", "cwd", "job_pid", "tty", "shell_command")
    }


def _context_value(
    value: Any, *, source: str = "iterm_variable", observed_at: str | None = None
) -> dict[str, Any]:
    if value is None or value == "":
        return {
            "value": None,
            "source": "unavailable",
            "quality": "unknown",
            "observed_at": None,
        }
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return {
                "value": None,
                "source": "unavailable",
                "quality": "unknown",
                "observed_at": None,
            }
    if not isinstance(value, (str, int, bool)):
        value = str(value)
    return {
        "value": str(value),
        "source": source,
        "quality": "observed",
        "observed_at": observed_at,
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _invoke(method: Any, *args: Any, **kwargs: Any) -> Any:
    """Call an API/mocked API method while accepting sync test doubles."""

    result = method(*args, **kwargs)
    return await _maybe_await(result)


def _safe_attr(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            try:
                return getattr(value, name)
            except Exception:
                continue
    return default


def _session_id(session: Any) -> str | None:
    value = _safe_attr(session, "session_id", "sessionId", "id")
    return value if isinstance(value, str) and value else None


def _strict_session_id(session: Any) -> str:
    """Validate an opaque session ID before it can affect lifecycle state."""

    value = _safe_attr(session, "session_id", "sessionId", "id", default=_MISSING)
    if (
        value is _MISSING
        or not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or "\x00" in value
    ):
        raise _TopologyScanError("topology contains a malformed session ID")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _TopologyScanError("topology contains a malformed session ID") from exc
    return value


_MISSING = object()


class _TopologyScanError(RuntimeError):
    """The SDK did not provide a complete, uniquely identifiable topology."""


class _TopologyLimitError(RuntimeError):
    """The topology exceeded the bounded work/catalogue budget."""


@dataclass(frozen=True, slots=True)
class _SessionDisplay:
    """Display-only location discovered from the iTerm object hierarchy."""

    window_id: str | None = None
    tab_id: str | None = None
    display_path: str | None = None
    is_focused: bool | None = None


def _display_id(value: Any) -> str | None:
    """Keep hierarchy identifiers opaque and nullable at the public boundary."""

    return value if isinstance(value, str) and value else None


def _strict_display_id(value: Any, label: str) -> str | None:
    """Validate an optional hierarchy ID instead of silently dropping it."""

    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _TopologyScanError(f"topology identifier {label} is malformed")
    return value


def _split_path_label(path: Sequence[int]) -> str:
    """Render a nested splitter path without using it as a session address."""

    if not path:
        return "split"
    return "split/" + "/".join(str(index) for index in path)


def _display_path(
    *,
    window_id: str | None,
    tab_id: str | None,
    window_index: int,
    tab_index: int,
    category: str,
    path: Sequence[int] = (),
) -> str:
    """Build a human-facing hierarchy path with index fallbacks.

    The fallback labels make a layout with an unavailable opaque ID useful for
    display while keeping ``window_id``/``tab_id`` themselves correctly null.
    The path is never parsed by target validation or used for writes.
    """

    window_label = window_id or f"window[{window_index}]"
    tab_label = tab_id or f"tab[{tab_index}]"
    parts = [window_label, tab_label]
    if category == "pane":
        parts.append(_split_path_label(path))
    else:
        parts.extend((category, str(path[0] if path else 0)))
    return "/".join(parts)


def _focus_state(
    app: Any,
    window: Any,
    tab: Any,
    session_id: str,
    window_id: str | None,
    tab_id: str | None,
) -> bool | None:
    """Derive focus only from explicit app/window/tab state."""

    app_active = _safe_attr(app, "app_active", default=_MISSING)
    if app_active is False:
        return False
    if app_active is not True:
        return None

    current_window_id = _display_id(
        _safe_attr(
            app, "current_terminal_window_id", "current_window_id", default=_MISSING
        )
    )
    if current_window_id is None:
        current_window = _safe_attr(app, "current_window", default=_MISSING)
        current_window_id = _display_id(
            _safe_attr(current_window, "window_id", "id", default=_MISSING)
        )
    selected_tab_id = _display_id(
        _safe_attr(window, "selected_tab_id", default=_MISSING)
    )
    active_session_id = _display_id(
        _safe_attr(tab, "active_session_id", default=_MISSING)
    )
    if (
        current_window_id is None
        or window_id is None
        or selected_tab_id is None
        or tab_id is None
        or active_session_id is None
    ):
        return None
    return (
        current_window_id == window_id
        and selected_tab_id == tab_id
        and active_session_id == session_id
    )


def _strict_attr(value: Any, *names: str) -> Any:
    """Read an attribute without hiding a malformed SDK/test-double value."""

    if value is _MISSING or value is None:
        return _MISSING
    for name in names:
        if isinstance(value, Mapping):
            if name in value:
                return value[name]
            continue
        try:
            return getattr(value, name)
        except AttributeError:
            continue
        except Exception as exc:
            raise _TopologyScanError(f"cannot read topology attribute {name}") from exc
    return _MISSING


def _strict_items(
    value: Any,
    label: str,
    *,
    optional: bool = False,
    work_count: list[int] | None = None,
) -> list[Any]:
    """Materialize a required collection, rejecting partial/malformed data."""

    if value is _MISSING:
        if optional:
            return []
        raise _TopologyScanError(f"topology collection {label} is unavailable")
    if value is None or isinstance(value, (str, bytes, Mapping)):
        raise _TopologyScanError(f"topology collection {label} is malformed")
    items: list[Any] = []
    try:
        for item in value:
            if len(items) >= MAX_TOPOLOGY_NODES:
                raise _TopologyLimitError(
                    f"topology collection {label} exceeds its work budget"
                )
            if work_count is not None:
                work_count[0] += 1
                if work_count[0] > MAX_TOPOLOGY_NODES:
                    raise _TopologyLimitError("topology exceeds its work budget")
            items.append(item)
    except _TopologyLimitError:
        raise
    except (TypeError, ValueError) as exc:
        raise _TopologyScanError(f"topology collection {label} is malformed") from exc
    return items


def _strict_splitter_sessions(
    node: Any,
    path: tuple[int, ...] = (),
    *,
    seen_nodes: set[int] | None = None,
    node_count: list[int] | None = None,
    reserved: bool = False,
) -> Iterable[tuple[Any, tuple[int, ...]]]:
    """Walk a splitter tree while proving that every leaf has a session ID."""

    if len(path) > MAX_TOPOLOGY_DEPTH:
        raise _TopologyLimitError("topology splitter depth exceeds its work budget")
    if seen_nodes is None:
        seen_nodes = set()
    if node_count is None:
        node_count = [0]
    if not reserved:
        node_count[0] += 1
        if node_count[0] > MAX_TOPOLOGY_NODES:
            raise _TopologyLimitError("topology splitter exceeds its work budget")
    object_key = id(node)
    if object_key in seen_nodes:
        raise _TopologyScanError("topology splitter contains a cycle or duplicate node")
    seen_nodes.add(object_key)

    raw_id = _strict_attr(node, "session_id", "sessionId", "id")
    session_id = _strict_session_id(node) if raw_id is not _MISSING else None
    if session_id is not None:
        yield node, path or (0,)
        return
    children = _strict_attr(node, "children")
    if children is _MISSING:
        raise _TopologyScanError(
            "topology contains an unidentified session or malformed splitter"
        )
    # Child references reserve their shared work budget while the iterable is
    # materialized.  Recursive visits use ``reserved=True`` so a wide nested
    # splitter cannot charge each child a second time after retaining it.
    for index, child in enumerate(
        _strict_items(children, "splitter.children", work_count=node_count)
    ):
        yield from _strict_splitter_sessions(
            child,
            path + (index,),
            seen_nodes=seen_nodes,
            node_count=node_count,
            reserved=True,
        )


def _strict_hierarchy_records(app: Any) -> Iterable[tuple[Any, _SessionDisplay]]:
    """Yield a complete topology or raise before lifecycle state is changed.

    The official hierarchy is authoritative when ``App.windows`` exists.  A
    flat ``App.sessions`` collection is retained as a compatibility fallback
    and is also accepted as a second view of the same objects.  Repeated
    references to one object are harmless; two distinct objects claiming one
    UUID are an ambiguous/duplicate topology and therefore fail closed.
    """

    seen_ids: dict[str, tuple[int, str]] = {}
    seen_objects: set[int] = set()
    seen_nodes: set[int] = set()
    node_count = [0]
    direct_ids: set[str] = set()

    def emit(
        session: Any, display: _SessionDisplay, *, source: str
    ) -> Iterable[tuple[Any, _SessionDisplay]]:
        session_id = _strict_session_id(session)
        if session_id is None:
            raise _TopologyScanError("topology contains a session without an ID")
        object_key = id(session)
        previous = seen_ids.get(session_id)
        if previous is not None:
            previous_object, previous_source = previous
            if (
                source == "direct"
                and previous_source == "hierarchy"
                and previous_object == object_key
            ):
                return
            if previous_object != object_key or previous_source == source:
                raise _TopologyScanError("topology contains duplicate session IDs")
            return
        if len(seen_ids) >= MAX_SESSION_RECORDS:
            raise _TopologyLimitError("topology catalogue exceeds its bounded limit")
        seen_ids[session_id] = (object_key, source)
        if object_key in seen_objects:
            return
        seen_objects.add(object_key)
        yield session, display

    windows_value = _strict_attr(app, "windows")
    direct_value = _strict_attr(app, "sessions")
    hierarchy_present = windows_value is not _MISSING
    if not hierarchy_present:
        # A flat collection is a supported test-double/older-wrapper shape,
        # but without either source there is no evidence that the app is
        # really an empty topology.
        if direct_value is _MISSING:
            raise _TopologyScanError("iTerm topology is unavailable")
        direct = _strict_items(direct_value, "app.sessions", work_count=node_count)
        windows: list[Any] = []
    else:
        windows = _strict_items(windows_value, "app.windows", work_count=node_count)
        direct = _strict_items(
            direct_value, "app.sessions", optional=True, work_count=node_count
        )

    for window_index, window in enumerate(windows):
        window_id = _strict_display_id(
            _strict_attr(window, "window_id", "id"), "window_id"
        )
        tabs_value = _strict_attr(window, "tabs")
        tabs = _strict_items(tabs_value, "window.tabs", work_count=node_count)
        for tab_index, tab in enumerate(tabs):
            tab_id = _strict_display_id(_strict_attr(tab, "tab_id", "id"), "tab_id")
            root = _strict_attr(tab, "root")
            if root is _MISSING:
                visible = _strict_attr(tab, "sessions")
                visible_items = _strict_items(
                    visible, "tab.sessions", work_count=node_count
                )
                root_records = [
                    (session, (index,)) for index, session in enumerate(visible_items)
                ]
            elif root is None:
                raise _TopologyScanError("topology tab root is malformed")
            else:
                root_records = list(
                    _strict_splitter_sessions(
                        root, seen_nodes=seen_nodes, node_count=node_count
                    )
                )
                # Older wrappers expose visible sessions beside a root that
                # is not itself iterable.  Use them only when the root is an
                # explicitly empty tree; never silently fill a malformed one.
                if not root_records:
                    visible = _strict_attr(tab, "sessions")
                    if visible is not _MISSING:
                        root_records = [
                            (session, (index,))
                            for index, session in enumerate(
                                _strict_items(
                                    visible, "tab.sessions", work_count=node_count
                                )
                            )
                        ]
            for session, path in root_records:
                focused = _focus_state(
                    app, window, tab, _session_id(session) or "", window_id, tab_id
                )
                display = _SessionDisplay(
                    window_id=window_id,
                    tab_id=tab_id,
                    display_path=_display_path(
                        window_id=window_id,
                        tab_id=tab_id,
                        window_index=window_index,
                        tab_index=tab_index,
                        category="pane",
                        path=path,
                    ),
                    is_focused=focused,
                )
                yield from emit(session, display, source="hierarchy")

            for category, property_name in (
                ("minimized", "minimized_sessions"),
                ("floating", "floating_sessions"),
            ):
                extras_value = _strict_attr(tab, property_name)
                if extras_value is _MISSING:
                    continue
                for index, session in enumerate(
                    _strict_items(
                        extras_value, f"tab.{property_name}", work_count=node_count
                    )
                ):
                    focused = _focus_state(
                        app, window, tab, _session_id(session) or "", window_id, tab_id
                    )
                    display = _SessionDisplay(
                        window_id=window_id,
                        tab_id=tab_id,
                        display_path=_display_path(
                            window_id=window_id,
                            tab_id=tab_id,
                            window_index=window_index,
                            tab_index=tab_index,
                            category=category,
                            path=(index,),
                        ),
                        is_focused=focused,
                    )
                    yield from emit(session, display, source="hierarchy")

    for index, session in enumerate(direct):
        session_id = _strict_session_id(session)
        if session_id in direct_ids:
            raise _TopologyScanError("topology contains duplicate session IDs")
        direct_ids.add(session_id)
        if hierarchy_present:
            hierarchy_record = seen_ids.get(session_id)
            if (
                hierarchy_record is None
                or hierarchy_record[0] != id(session)
                or hierarchy_record[1] != "hierarchy"
            ):
                raise _TopologyScanError(
                    "flat topology disagrees with the window hierarchy"
                )
        window_id = _strict_display_id(_strict_attr(session, "window_id"), "window_id")
        tab_id = _strict_display_id(_strict_attr(session, "tab_id"), "tab_id")
        tab = _strict_attr(session, "tab")
        window = _strict_attr(tab, "window")
        tab_id = tab_id or _strict_display_id(
            _strict_attr(tab, "tab_id", "id"), "tab_id"
        )
        window_id = window_id or _strict_display_id(
            _strict_attr(window, "window_id", "id"), "window_id"
        )
        path = None
        if window_id is not None or tab_id is not None:
            path = _display_path(
                window_id=window_id,
                tab_id=tab_id,
                window_index=0,
                tab_index=index,
                category="pane",
                path=(index,),
            )
        focused = _strict_attr(session, "is_focused", "focused", "is_active")
        display = _SessionDisplay(
            window_id=window_id,
            tab_id=tab_id,
            display_path=path,
            is_focused=focused if isinstance(focused, bool) else None,
        )
        yield from emit(session, display, source="direct")


def _line_text(value: Any) -> tuple[str, bool | None]:
    if isinstance(value, str):
        return value, None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="strict"), None
        except UnicodeDecodeError as exc:
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm returned non-UTF-8 terminal data"
            ) from exc
    text = _safe_attr(value, "text", "string", "contents", "value", default="")
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm returned non-UTF-8 terminal data"
            ) from exc
    if not isinstance(text, str):
        text = str(text)
    wrapped = _safe_attr(value, "wrapped", "hard_eol", default=None)
    if isinstance(wrapped, bool):
        # iTerm's hard_eol is the inverse of a soft/wrapped line.
        if hasattr(value, "hard_eol") and not hasattr(value, "wrapped"):
            wrapped = not wrapped
    else:
        wrapped = None
    return text, wrapped


def _line_values(contents: Any) -> list[Any]:
    if contents is None:
        return []
    if isinstance(contents, (str, bytes)):
        return (
            contents.decode("utf-8").splitlines()
            if isinstance(contents, bytes)
            else contents.splitlines()
        )
    if isinstance(contents, Mapping):
        for key in ("lines", "contents", "text"):
            if key in contents:
                value = contents[key]
                if isinstance(value, str):
                    return value.splitlines()
                if isinstance(value, Iterable):
                    return list(value)
        return []
    lines = _safe_attr(contents, "lines", "contents", default=None)
    if lines is not None and not isinstance(lines, (str, bytes)):
        try:
            return list(lines)
        except TypeError:
            pass
    if isinstance(lines, str):
        return lines.splitlines()
    if isinstance(contents, Iterable):
        try:
            return list(contents)
        except TypeError:
            pass
    return []


def _screen_lines(screen: Any) -> tuple[list[dict[str, Any]], int, int, Any]:
    """Convert an iTerm ScreenContents-like object to schema line objects."""

    values: list[Any]
    number_of_lines = _safe_attr(screen, "number_of_lines", "rows", default=None)
    number_of_columns = _safe_attr(screen, "number_of_columns", "columns", default=None)
    cursor = _safe_attr(screen, "cursor_coord", "cursor", default=None)
    line_getter = getattr(screen, "line", None) or getattr(screen, "line_at", None)
    if callable(line_getter) and isinstance(number_of_lines, int):
        # Official ScreenContents uses line(index), not line_at(index).
        # A failed row read is unavailable data, never a fabricated empty row.
        values = [line_getter(index) for index in range(max(0, number_of_lines))]
    else:
        values = _line_values(screen)
        if number_of_lines is None:
            number_of_lines = len(values)
    if not isinstance(number_of_lines, int) or number_of_lines < 0:
        number_of_lines = len(values)
    if not isinstance(number_of_columns, int) or number_of_columns < 0:
        number_of_columns = max(
            (len(_line_text(value)[0]) for value in values), default=0
        )
    lines: list[dict[str, Any]] = []
    for index, value in enumerate(values[:number_of_lines]):
        text, wrapped = _line_text(value)
        row: dict[str, Any] = {"row": index, "text": text}
        if wrapped is not None:
            row["wrapped"] = wrapped
        lines.append(row)
    # The SDK exposes the raw protobuf cursor, whose y coordinate is absolute
    # buffer numbering.  Project it only at the visible-screen DTO boundary;
    # history readers retain their absolute source positions separately.
    windowed_coord_range = _safe_attr(screen, "windowed_coord_range", default=None)
    cursor = screen_cursor_coord(
        cursor, windowed_coord_range, row_count=number_of_lines
    )
    return lines, number_of_lines, number_of_columns, cursor


def _cursor_mapping(cursor: Any) -> dict[str, int] | None:
    if cursor is None:
        return None
    row = _safe_attr(cursor, "row", "y", default=None)
    column = _safe_attr(cursor, "column", "x", default=None)
    if isinstance(row, int) and isinstance(column, int) and row >= 0 and column >= 0:
        return {"row": row, "column": column}
    return None


def _capture_size(dto: CaptureDTO) -> int:
    return len(
        json.dumps(asdict(dto), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def _bound_capture(dto: CaptureDTO, max_bytes: int = MAX_CAPTURE_BYTES) -> CaptureDTO:
    if _capture_size(dto) <= max_bytes:
        return dto
    screen = dict(dto.screen)
    original = screen.pop("lines", [])
    screen["lines"] = []
    if "complete" in screen:
        screen["complete"] = False
    bounded = replace(dto, screen=screen, history_lines=(), truncated=True)
    if _capture_size(bounded) > max_bytes:
        raise DomainError("ITEM_TOO_LARGE", "capture metadata exceeds limit")
    # Only truncate text at Unicode scalar boundaries. Measure escaped JSON,
    # so control characters and per-row metadata count against the limit too.
    for is_history, rows in ((False, original), (True, dto.history_lines)):
        for row in rows:
            text = row.text if is_history else row["text"]

            def candidate(prefix):
                if is_history:
                    return replace(
                        bounded,
                        history_lines=(
                            *bounded.history_lines,
                            replace(row, text=prefix),
                        ),
                    )
                return replace(
                    bounded,
                    screen={
                        **bounded.screen,
                        "lines": [*bounded.screen["lines"], {**row, "text": prefix}],
                    },
                )

            full = candidate(text)
            if _capture_size(full) <= max_bytes:
                bounded = full
                continue
            if _capture_size(candidate("")) > max_bytes:
                return bounded
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if _capture_size(candidate(text[:middle])) <= max_bytes:
                    low = middle
                else:
                    high = middle - 1
            return candidate(text[:low])
    return bounded


class ItermBackend:
    """iTerm Python API connection, topology, rendered reads and input."""

    def __init__(
        self,
        *,
        broker_epoch: str,
        connection: Any | None = None,
        app: Any | None = None,
        connection_factory: Any | None = None,
        max_capture_bytes: int = MAX_CAPTURE_BYTES,
    ) -> None:
        if not isinstance(broker_epoch, str) or not broker_epoch:
            raise ValueError("broker_epoch must be a non-empty string")
        if (
            not isinstance(max_capture_bytes, int)
            or isinstance(max_capture_bytes, bool)
            or not 1 <= max_capture_bytes <= MAX_CAPTURE_BYTES
        ):
            raise ValueError("max_capture_bytes must be between 1 and 1MiB")
        self.broker_epoch = broker_epoch
        self._connection = connection
        self._app = app
        self._connection_factory = connection_factory
        self._iterm2: Any | None = None
        self._session_epochs: dict[str, str] = {}
        self._max_capture_bytes = max_capture_bytes
        self._transaction_supervisor: Any | None = None
        self._transaction_poisoned = False

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._app is not None

    async def connect(self) -> "ItermBackend":
        """Open the iTerm API connection and resolve the current app once."""

        if self._transaction_poisoned or (
            self._transaction_supervisor is not None
            and self._transaction_supervisor.closed
        ):
            raise DomainError(
                "BACKEND_UNAVAILABLE",
                "iTerm2 transaction state requires owner restart",
            )
        if self.connected:
            return self
        try:
            if self._connection is None:
                if self._connection_factory is not None:
                    self._connection = await _maybe_await(self._connection_factory())
                else:
                    # Deliberately lazy: importing this module must not make
                    # iTerm2 a dependency for fixture or schema tests.
                    self._iterm2 = importlib.import_module("iterm2")
                    connection_type = getattr(self._iterm2, "Connection", None)
                    if connection_type is None:
                        raise RuntimeError("iterm2.Connection is unavailable")
                    creator = getattr(connection_type, "async_create", None)
                    if creator is None:
                        raise RuntimeError(
                            "iterm2.Connection.async_create is unavailable"
                        )
                    self._connection = await _maybe_await(creator())
            if self._iterm2 is None and self._connection_factory is None:
                self._iterm2 = importlib.import_module("iterm2")
            if self._app is None:
                getter = (
                    getattr(self._iterm2, "async_get_app", None)
                    if self._iterm2 is not None
                    else None
                )
                if getter is not None:
                    self._app = await _maybe_await(getter(self._connection))
                else:
                    getter = getattr(self._connection, "async_get_app", None)
                    if getter is None:
                        raise RuntimeError("iTerm app lookup is unavailable")
                    self._app = await _maybe_await(getter())
            if self._app is None:
                raise RuntimeError("iTerm app lookup returned no app")
        except BaseException as exc:
            # Keep ownership until close has acquired the connection. Shield
            # bounded cleanup even when the caller cancels more than once.
            cleanup = asyncio.create_task(self.close(timeout=2.0))
            cancelled = isinstance(exc, asyncio.CancelledError)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            try:
                result = cleanup.result()
                if result is not None and (
                    result.pending
                    or any(not task.cancelled() and task.exception() is not None
                           for task in result.done)
                ):
                    self._transaction_poisoned = True
            except BaseException:
                self._transaction_poisoned = True
            if cancelled:
                raise asyncio.CancelledError from exc
            if isinstance(exc, (DomainError, KeyboardInterrupt, SystemExit)):
                raise
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm2 connection is unavailable"
            ) from exc
        return self

    async def close(self, timeout: float | None = None) -> BoundedWaitResult | None:
        """Close only this backend's API connection, never a terminal session.

        iterm2 2.30 has no public Connection.close. Cancel its owned dispatch
        task before closing the websocket, avoiding its traceback-on-close
        path. This pinned compatibility access needs review on SDK upgrades.
        """
        bounded_timeout = validate_timeout(timeout) if timeout is not None else None
        supervisor = self._transaction_supervisor
        if supervisor is not None:
            supervisor.close()

        connection = self._connection
        self._connection, self._app = None, None
        if connection is None:
            return
        dispatcher = getattr(connection, "_Connection__dispatch_forever_future", None)
        websocket = getattr(connection, "websocket", None)
        if timeout is None:
            if dispatcher is not None:
                dispatcher.cancel()
                try:
                    await dispatcher
                except asyncio.CancelledError:
                    pass
            if websocket is not None:
                await websocket.close()
            return None

        assert bounded_timeout is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + bounded_timeout
        phases: list[BoundedWaitResult] = []
        try:
            cleanup_tasks: list[Any] = []
            websocket_task = None
            if dispatcher is not None:
                # Dispatch cancellation is a request only. A foreign SDK
                # future may swallow CancelledError; detaching it after the
                # deadline is still safe because the connection/app ownership
                # was retired synchronously above.
                with suppress(Exception):
                    dispatcher.cancel()
                cleanup_tasks.append(dispatcher)
            if websocket is not None:
                try:
                    close_result = websocket.close()
                except Exception:
                    self._transaction_poisoned = True
                    close_result = None
                if inspect.isawaitable(close_result):
                    websocket_task = asyncio.ensure_future(close_result)
                    cleanup_tasks.append(websocket_task)
            if cleanup_tasks:
                phases.append(
                    await wait_bounded(
                        cleanup_tasks,
                        deadline=deadline,
                        cancel_pending=True,
                    )
                )
            if websocket_task is not None and websocket_task.done():
                if websocket_task.cancelled() or websocket_task.exception() is not None:
                    self._transaction_poisoned = True
            return (
                merge_wait_results(*phases)
                if phases
                else BoundedWaitResult(frozenset(), frozenset(), deadline)
            )
        finally:
            # No connection object remains owned by this backend, even if an
            # SDK dispatcher or websocket refuses cancellation after timeout.
            self._connection, self._app = None, None

    def _require_connected(self) -> None:
        if self._transaction_poisoned or (
            self._transaction_supervisor is not None
            and self._transaction_supervisor.closed
        ):
            raise DomainError(
                "BACKEND_UNAVAILABLE",
                "iTerm2 transaction state requires owner restart",
            )
        if not self.connected:
            raise DomainError("BACKEND_UNAVAILABLE", "iTerm2 backend is not connected")

    async def _sessions(self) -> list[Any]:
        records = await self._session_records()
        self._commit_epoch_catalog(records)
        return [session for session, _ in records]

    async def _session_records(self) -> list[tuple[Any, _SessionDisplay]]:
        self._require_connected()
        try:
            records = list(_strict_hierarchy_records(self._app))
            if len(records) > MAX_SESSION_RECORDS:
                raise DomainError(
                    "RESOURCE_LIMIT",
                    "iTerm session catalogue exceeds its bounded limit",
                )
            return records
        except DomainError:
            raise
        except _TopologyLimitError as exc:
            raise DomainError(
                "RESOURCE_LIMIT", "iTerm session topology exceeds its bounded limit"
            ) from exc
        except _TopologyScanError as exc:
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm session topology is incomplete"
            ) from exc
        except Exception as exc:
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm session enumeration failed"
            ) from exc

    def _commit_epoch_catalog(
        self, records: Sequence[tuple[Any, _SessionDisplay]]
    ) -> None:
        """Atomically retain epochs for exactly one valid current catalogue."""

        session_ids: list[str] = []
        for session, _ in records:
            session_id = _session_id(session)
            if session_id is None:
                # This should already be rejected by the strict scanner.  Keep
                # the guard here so a future caller cannot accidentally turn a
                # partial catalogue into an epoch-pruning operation.
                raise DomainError(
                    "BACKEND_UNAVAILABLE",
                    "iTerm session topology contains an unknown ID",
                )
            session_ids.append(session_id)
        if len(session_ids) > MAX_SESSION_RECORDS:
            raise DomainError(
                "RESOURCE_LIMIT", "iTerm session catalogue exceeds its bounded limit"
            )
        if len(set(session_ids)) != len(session_ids):
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm session topology contains duplicate IDs"
            )

        # Build the replacement independently.  If anything above fails, the
        # old map remains untouched and no absent session is pruned.
        next_epochs = {
            session_id: self._session_epochs[session_id]
            for session_id in session_ids
            if session_id in self._session_epochs
        }
        for session_id in session_ids:
            if session_id not in next_epochs:
                next_epochs[session_id] = uuid.uuid4().hex
        self._session_epochs = next_epochs

    async def _session_for(self, target: Target) -> tuple[dict[str, str], Any]:
        target_map = _as_target(target)
        if target_map["broker_epoch"] != self.broker_epoch:
            raise DomainError("STALE_TARGET", "target belongs to another broker epoch")
        sessions = await self._sessions()
        for session in sessions:
            if _session_id(session) == target_map["session_id"]:
                expected = self._session_epochs.get(target_map["session_id"])
                if expected != target_map["session_epoch"]:
                    raise DomainError("STALE_TARGET", "target session epoch is stale")
                return target_map, session
        raise DomainError("SESSION_GONE", "target session is no longer available")

    async def _variable(self, session: Any, name: str) -> Any:
        method = getattr(session, "async_get_variable", None)
        if method is None:
            return None
        try:
            return await _invoke(method, name)
        except Exception:
            # A missing shell integration variable is ordinary unknown data;
            # it is not a reason to fabricate a value from a title.
            return None

    async def _context(self, session: Any, observed_at: str) -> dict[str, Any]:
        job_name = await self._variable(session, "jobName")
        if job_name is None:
            job_name = await self._variable(session, "deepestJob")
        context = _unknown_context()
        context["program"] = _context_value(job_name, observed_at=observed_at)
        context["cwd"] = _context_value(
            await self._variable(session, "path"), observed_at=observed_at
        )
        context["job_pid"] = _context_value(
            await self._variable(session, "jobPid"), observed_at=observed_at
        )
        context["tty"] = _context_value(
            await self._variable(session, "tty"), observed_at=observed_at
        )
        context["shell_command"] = _context_value(
            await self._variable(session, "lastCommand"),
            source="shell_integration",
            observed_at=observed_at,
        )
        return context

    async def list_sessions(self) -> list[SessionDescriptor]:
        # Take a complete topology snapshot first.  Metadata reads can await
        # shell integration, so nothing in the epoch catalogue may be changed
        # until the same topology has been observed again below.
        records = await self._session_records()
        observed_at = _now()
        pending: dict[str, dict[str, Any]] = {}
        for session, display in records:
            session_id = _session_id(session)
            if session_id is None:
                # The strict scanner rejects this.  Retain the guard so a
                # future scanner cannot turn an unidentified object into an
                # apparent absence.
                raise DomainError(
                    "BACKEND_UNAVAILABLE",
                    "iTerm session topology contains an unknown ID",
                )
            title = _safe_attr(session, "name", "session_name", "title", default=None)
            window_id = display.window_id
            tab_id = display.tab_id
            focused = display.is_focused
            if focused is None:
                session_focus = _safe_attr(
                    session, "is_focused", "focused", "is_active", default=None
                )
                focused = session_focus if isinstance(session_focus, bool) else None
            try:
                context = await self._context(session, observed_at)
            except DomainError:
                raise
            except Exception as exc:
                raise DomainError(
                    "BACKEND_UNAVAILABLE", "iTerm session metadata is unavailable"
                ) from exc
            if not isinstance(context, Mapping):
                raise DomainError(
                    "BACKEND_UNAVAILABLE", "iTerm session metadata is malformed"
                )
            program_item = context.get("program")
            cwd_item = context.get("cwd")
            if not isinstance(program_item, Mapping) or not isinstance(
                cwd_item, Mapping
            ):
                raise DomainError(
                    "BACKEND_UNAVAILABLE", "iTerm session metadata is malformed"
                )
            program = program_item.get("value")
            cwd = cwd_item.get("value")
            context_summary = (
                " / ".join(str(value) for value in (program, cwd) if value) or None
            )
            if title is not None and not isinstance(title, str):
                raise DomainError(
                    "BACKEND_UNAVAILABLE", "iTerm session title is malformed"
                )
            if any(
                value is not None and not isinstance(value, str)
                for value in (window_id, tab_id, display.display_path)
            ):
                raise DomainError(
                    "BACKEND_UNAVAILABLE", "iTerm session location is malformed"
                )
            pending[session_id] = {
                "session": session,
                "context_summary": context_summary,
                "title": title,
            }

        # Re-read topology after all metadata awaits.  A changed set or a
        # replaced live object would make the pending metadata a partial,
        # mixed-time catalogue.  Return a retryable backend error and leave
        # the previous epoch map untouched in that case.
        current_records = await self._session_records()
        initial_by_id = {_session_id(session): session for session, _ in records}
        current_by_id = {
            _session_id(session): session for session, _ in current_records
        }
        if set(initial_by_id) != set(current_by_id) or any(
            initial_by_id[session_id] is not current_by_id[session_id]
            for session_id in initial_by_id
        ):
            raise DomainError(
                "BACKEND_UNAVAILABLE",
                "iTerm session topology changed during metadata read",
            )

        final_rows: list[tuple[Any, _SessionDisplay, str | None, bool | None]] = []
        for session, display in current_records:
            session_id = _session_id(session)
            if session_id is None or session_id not in pending:
                raise DomainError(
                    "BACKEND_UNAVAILABLE",
                    "iTerm session topology changed during metadata read",
                )
            title = _safe_attr(
                session,
                "name",
                "session_name",
                "title",
                default=pending[session_id]["title"],
            )
            if title is not None and not isinstance(title, str):
                raise DomainError(
                    "BACKEND_UNAVAILABLE", "iTerm session title is malformed"
                )
            if any(
                value is not None and not isinstance(value, str)
                for value in (display.window_id, display.tab_id, display.display_path)
            ):
                raise DomainError(
                    "BACKEND_UNAVAILABLE", "iTerm session location is malformed"
                )
            focused = display.is_focused
            if focused is None:
                session_focus = _safe_attr(
                    session, "is_focused", "focused", "is_active", default=None
                )
                focused = session_focus if isinstance(session_focus, bool) else None
            final_rows.append((session, display, title, focused))

        # All metadata and final-topology validation is complete before this
        # atomic commit.  Epoch generation therefore cannot accidentally bind
        # a target to a topology that was only partially observed.
        self._commit_epoch_catalog(current_records)
        result: list[SessionDescriptor] = []
        for session, display, title, focused in final_rows:
            session_id = _session_id(session)
            result.append(
                SessionDescriptor(
                    target={
                        "session_id": session_id,
                        "session_epoch": self._session_epochs[session_id],
                        "broker_epoch": self.broker_epoch,
                    },
                    title=title,
                    window_id=display.window_id,
                    tab_id=display.tab_id,
                    display_path=display.display_path,
                    is_focused=focused if isinstance(focused, bool) else None,
                    context_summary=pending[session_id]["context_summary"],
                    observed_at=observed_at,
                )
            )
        return result

    async def capture(self, target: Target) -> CaptureDTO:
        target_map, session = await self._session_for(target)
        try:
            # SDK 2.30 slices Python strings using producer UTF-16 offsets.
            # Use the same official RPC once and retain its cell mapping for
            # a strict decoder. Read public text directly from the same proto:
            # SDK LineContents would expand unvalidated repeats first.
            sdk = self._iterm2
            rpc = getattr(getattr(sdk, "rpc", None), "async_get_screen_contents", None)
            geometry = getattr(session, "grid_size", None)
            grid_columns = getattr(geometry, "width", None)
            if getattr(sdk, "__version__", None) == "2.30" and callable(rpc):
                if (
                    not isinstance(grid_columns, int)
                    or isinstance(grid_columns, bool)
                    or grid_columns <= 0
                ):
                    raise RuntimeError("screen grid width is unavailable")
                response = await _invoke(
                    rpc, self._connection, target_map["session_id"], None, True
                )
                raw_buffer = response.get_buffer_response
                if raw_buffer.status != sdk.api_pb2.GetBufferResponse.Status.Value(
                    "OK"
                ):
                    raise RuntimeError("screen capture API rejected request")
                rows, columns = len(raw_buffer.contents), grid_columns
                cursor = screen_cursor_coord(
                    raw_buffer.cursor, raw_buffer.windowed_coord_range, row_count=rows
                )
                lines = []
                for index, raw_line in enumerate(raw_buffer.contents):
                    if not isinstance(raw_line.text, str):
                        raise RuntimeError("screen capture text is unavailable")
                    line = {"row": index, "text": raw_line.text}
                    if not isinstance(
                        raw_line.continuation, bool
                    ) and raw_line.continuation in {1, 2}:
                        line["wrapped"] = raw_line.continuation == 2
                    lines.append(line)
            else:
                getter = getattr(session, "async_get_screen_contents", None)
                if getter is None:
                    raise RuntimeError("screen capture API is unavailable")
                screen = await _invoke(getter)
                lines, rows, columns, cursor = _screen_lines(screen)
                if isinstance(grid_columns, int) and grid_columns > 0:
                    columns = grid_columns
            finished = _now()
            dto = CaptureDTO(
                screen={
                    "rows": rows,
                    "columns": columns,
                    "lines": lines,
                    "cursor": _cursor_mapping(cursor),
                    "buffer": "unknown",
                    "wrapped": None,
                    "complete": True,
                    "unavailable_fields": ["buffer", "wrapped"],
                },
                context=await self._context(session, finished),
                target=target_map,
                captured_at=finished,
            )
            return _bound_capture(dto, self._max_capture_bytes)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm screen capture failed"
            ) from exc

    def _history_supervisor(self) -> Any:
        if self._transaction_supervisor is None:
            try:
                from .transaction_reads import TransactionReadSupervisor

                self._transaction_supervisor = TransactionReadSupervisor(
                    timeout_seconds=2.0,
                    cancel_grace_seconds=0.1,
                )
            except DomainError:
                raise
            except Exception:
                raise DomainError(
                    "BACKEND_UNAVAILABLE",
                    "iTerm2 transaction supervisor is unavailable",
                ) from None
        return self._transaction_supervisor

    @staticmethod
    def _history_flag(value: Any, name: str) -> bool | None:
        try:
            flag = getattr(value, name)
            if callable(flag):
                flag = flag()
        except Exception:
            return None
        return flag if type(flag) is bool else None

    def _history_abort_owned(self, connection: Any) -> Any:
        """Prepare a synchronous abort callback for one captured connection."""

        websocket = getattr(connection, "websocket", _MISSING)
        transport = getattr(websocket, "transport", _MISSING)
        abort = getattr(transport, "abort", _MISSING)
        if websocket is _MISSING or transport is _MISSING or not callable(abort):
            self._transaction_poisoned = True
            raise DomainError(
                "BACKEND_UNAVAILABLE",
                "iTerm2 transaction transport cannot be bounded",
            )
        try:
            transport_closing = getattr(transport, "is_closing")
            websocket_closed = getattr(websocket, "closed")
        except Exception:
            transport_closing = websocket_closed = _MISSING
        if (
            transport_closing is _MISSING
            or not callable(transport_closing)
            or (not callable(websocket_closed) and type(websocket_closed) is not bool)
        ):
            self._transaction_poisoned = True
            raise DomainError(
                "BACKEND_UNAVAILABLE",
                "iTerm2 transaction transport cannot be bounded",
            )

        def abort_owned() -> bool:
            # Poison first.  A failed or indeterminate SDK transaction cannot
            # be made safe by reconnecting this Python process.
            self._transaction_poisoned = True
            if self._connection is connection:
                self._connection, self._app = None, None
            dispatcher = getattr(
                connection, "_Connection__dispatch_forever_future", None
            )
            if dispatcher is not None:
                try:
                    dispatcher.cancel()
                except Exception:
                    pass
            try:
                result = abort()
            except Exception:
                return False
            if inspect.isawaitable(result):
                return False
            return (
                self._history_flag(transport, "is_closing") is True
                or self._history_flag(websocket, "closed") is True
            )

        return abort_owned

    async def _history_transaction(
        self,
        *,
        connection: Any | None = None,
        sdk: Any | None = None,
    ):
        if connection is None:
            connection = self._connection
        if sdk is None:
            sdk = self._iterm2
        transaction_type = (
            getattr(sdk, "Transaction", None) if sdk is not None else None
        )
        if transaction_type is None:
            transaction_type = getattr(connection, "Transaction", None)
        if transaction_type is None:
            transaction_factory = getattr(connection, "transaction", None)
            if transaction_factory is not None:
                return await _maybe_await(transaction_factory())
        if transaction_type is None:
            raise DomainError(
                "UNSUPPORTED_CAPABILITY", "iTerm history transactions are unavailable"
            )
        return transaction_type(connection)

    def _assert_history_stamp(
        self,
        target_map: Mapping[str, str],
        connection: Any,
        app: Any,
        session: Any,
    ) -> None:
        if (
            self._transaction_poisoned
            or self._connection is not connection
            or self._app is not app
            or session is None
        ):
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm history transaction is unavailable"
            )
        if _session_id(session) != target_map.get("session_id"):
            raise DomainError(
                "STALE_TARGET", "target session object changed during history capture"
            )

    async def _revalidate_history_stamp(
        self,
        target_map: Mapping[str, str],
        connection: Any,
        app: Any,
        session: Any,
    ) -> None:
        self._assert_history_stamp(target_map, connection, app, session)
        try:
            checked_target, checked_session = await self._session_for(target_map)
        except DomainError as exc:
            if exc.code in {
                "INVALID_ARGUMENT",
                "SESSION_GONE",
                "STALE_TARGET",
                "RESOURCE_LIMIT",
            }:
                raise
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm history transaction is unavailable"
            ) from None
        except Exception:
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm history transaction is unavailable"
            ) from None
        self._assert_history_stamp(target_map, connection, app, session)
        if checked_target != dict(target_map):
            raise DomainError(
                "STALE_TARGET", "target identity changed during history capture"
            )
        if checked_session is not session:
            raise DomainError(
                "STALE_TARGET", "target session object changed during history capture"
            )

    async def _history_info(self, session: Any, transaction: Any) -> Any:
        method = getattr(session, "async_get_line_info", None)
        if method is not None:
            try:
                return await _invoke(method, transaction)
            except TypeError:
                return await _invoke(method)
        method = getattr(transaction, "async_get_line_info", None)
        if method is not None:
            return await _invoke(method, session)
        raise RuntimeError("iTerm line info API is unavailable")

    def _history_windowed_range(
        self, first: int, count: int, *, sdk: Any = None
    ) -> Any:
        """Build the SDK range without asking the SDK to expand line data."""

        if sdk is None:
            sdk = self._iterm2
        windowed_type = getattr(sdk, "WindowedCoordRange", None)
        coord_type = getattr(sdk, "CoordRange", None)
        point_type = getattr(sdk, "Point", None)
        if windowed_type is None or coord_type is None or point_type is None:
            # Injected test doubles often expose only rpc/api_pb2.  Resolve
            # the official value types lazily while retaining the import-free
            # construction path for fixture backends.
            try:
                sdk = importlib.import_module("iterm2")
            except Exception as exc:
                raise RuntimeError("iTerm coordinate range API is unavailable") from exc
            windowed_type = getattr(sdk, "WindowedCoordRange", None)
            coord_type = getattr(sdk, "CoordRange", None)
            point_type = getattr(sdk, "Point", None)
        if windowed_type is None or coord_type is None or point_type is None:
            raise RuntimeError("iTerm coordinate range API is unavailable")
        return windowed_type(
            coord_type(point_type(0, first), point_type(0, first + count))
        )

    @staticmethod
    def _history_response_range(raw_buffer: Any) -> tuple[int, int, int, int] | None:
        """Read the raw response coordinates without constructing SDK rows."""

        windowed = _safe_attr(raw_buffer, "windowed_coord_range", default=_MISSING)
        if windowed is _MISSING or windowed is None:
            return None
        coord_range = _safe_attr(
            windowed, "coord_range", "coordRange", default=_MISSING
        )
        if coord_range is _MISSING or coord_range is None:
            return None
        start = _safe_attr(coord_range, "start", default=_MISSING)
        end = _safe_attr(coord_range, "end", default=_MISSING)
        if start is _MISSING or end is _MISSING:
            return None
        start_x = _safe_attr(start, "x", default=None)
        start_y = _safe_attr(start, "y", default=None)
        end_x = _safe_attr(end, "x", default=None)
        end_y = _safe_attr(end, "y", default=None)
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (start_x, start_y, end_x, end_y)
        ):
            return None
        return start_x, start_y, end_x, end_y

    async def _history_contents(
        self,
        session: Any,
        transaction: Any,
        info: Any,
        first: int,
        count: int,
        *,
        connection: Any = None,
        sdk: Any = None,
    ) -> Any:
        """Fetch history rows, using raw RPC for pinned iTerm2 2.30."""

        if connection is None:
            connection = self._connection
        if sdk is None:
            sdk = self._iterm2
        rpc = getattr(getattr(sdk, "rpc", None), "async_get_screen_contents", None)
        if getattr(sdk, "__version__", None) == "2.30" and callable(rpc):
            windowed = self._history_windowed_range(first, count, sdk=sdk)
            response = await _invoke(
                rpc, connection, session.session_id, windowed, True
            )
            raw_buffer = _safe_attr(response, "get_buffer_response", default=_MISSING)
            if raw_buffer is _MISSING or raw_buffer is None:
                raise RuntimeError("iTerm history response is unavailable")
            api_pb2 = getattr(sdk, "api_pb2", None)
            get_buffer = getattr(api_pb2, "GetBufferResponse", None)
            status_type = getattr(get_buffer, "Status", None)
            status_value = getattr(status_type, "Value", None)
            ok_status = status_value("OK") if callable(status_value) else 0
            status = _safe_attr(raw_buffer, "status", default=None)
            if status != ok_status:
                raise RuntimeError("iTerm history API rejected request")
            returned = self._history_response_range(raw_buffer)
            if returned != (0, first, 0, first + count):
                raise RuntimeError(
                    "iTerm history response range does not match request"
                )
            contents = _safe_attr(raw_buffer, "contents", default=_MISSING)
            if (
                contents is _MISSING
                or contents is None
                or isinstance(contents, (str, bytes, Mapping))
            ):
                raise RuntimeError("iTerm history contents are unavailable")
            try:
                returned_count = len(contents)
            except (TypeError, AttributeError) as exc:
                raise RuntimeError("iTerm history contents are unavailable") from exc
            if returned_count != count:
                raise RuntimeError(
                    "iTerm history response row count does not match request"
                )
            return raw_buffer

        method = getattr(session, "async_get_contents", None)
        if method is not None:
            attempts = ((transaction, first, count), (first, count), (transaction,), ())
            for args in attempts:
                try:
                    return await _invoke(method, *args)
                except TypeError:
                    continue
        method = getattr(transaction, "async_get_contents", None)
        if method is not None:
            for args in ((session, first, count), (session, info), (session,)):
                try:
                    return await _invoke(method, *args)
                except TypeError:
                    continue
        raise RuntimeError("iTerm history contents API is unavailable")

    @staticmethod
    def _raw_history_lines(
        raw_buffer: Any, first: int, count: int, max_bytes: int
    ) -> tuple[CaptureLineDTO, ...]:
        """Decode exact, bounded raw row text without SDK line wrappers."""

        contents = _safe_attr(raw_buffer, "contents", default=_MISSING)
        if contents is _MISSING or contents is None:
            raise RuntimeError("iTerm history contents are unavailable")
        try:
            returned_count = len(contents)
        except (TypeError, AttributeError) as exc:
            raise RuntimeError("iTerm history contents are unavailable") from exc
        if returned_count != count:
            raise RuntimeError(
                "iTerm history response row count does not match request"
            )

        raw_rows: list[tuple[str, bool | None]] = []
        lower_bound_bytes = 0
        for offset in range(count):
            raw_line = contents[offset]
            text = _safe_attr(raw_line, "text", default=_MISSING)
            if not isinstance(text, str):
                raise RuntimeError("iTerm history text is unavailable")
            continuation = _safe_attr(raw_line, "continuation", default=None)
            if (
                isinstance(continuation, int)
                and not isinstance(continuation, bool)
                and continuation in {1, 2}
            ):
                wrapped = continuation == 2
            else:
                wrapped = None
            raw_rows.append((text, wrapped))
            lower_bound_bytes += len(text)
            if lower_bound_bytes > max_bytes:
                raise DomainError(
                    "ITEM_TOO_LARGE",
                    "history capture exceeds its bounded memory budget",
                )

        encoded_bytes = 0
        for text, _wrapped in raw_rows:
            encoded_bytes += _text_bytes(text)
            if encoded_bytes > max_bytes:
                raise DomainError(
                    "ITEM_TOO_LARGE",
                    "history capture exceeds its bounded memory budget",
                )

        return tuple(
            CaptureLineDTO(position=first + offset, text=text, wrapped=wrapped)
            for offset, (text, wrapped) in enumerate(raw_rows)
        )

    async def history(self, target: Target, last_lines: int) -> CaptureDTO:
        if (
            isinstance(last_lines, bool)
            or not isinstance(last_lines, int)
            or not 1 <= last_lines <= MAX_HISTORY_LINES
        ):
            raise DomainError(
                "INVALID_ARGUMENT", "last_lines must be between 1 and 2000"
            )
        target_map, session = await self._session_for(target)
        connection = self._connection
        app = self._app
        sdk = self._iterm2
        try:
            self._assert_history_stamp(target_map, connection, app, session)
            abort_owned = self._history_abort_owned(connection)
            transaction = await self._history_transaction(
                connection=connection, sdk=sdk
            )
            self._assert_history_stamp(target_map, connection, app, session)
            supervisor = self._history_supervisor()

            async def body(raw_transaction: Any) -> tuple[int, int, int, int, Any]:
                # The transaction gate may have queued this read behind a
                # different target.  Re-check the complete local topology
                # after the runner actually owns the gate, before using the
                # captured SDK session object.
                await self._revalidate_history_stamp(
                    target_map, connection, app, session
                )
                info = await self._history_info(session, raw_transaction)
                self._assert_history_stamp(target_map, connection, app, session)
                overflow = _safe_attr(info, "overflow", default=_MISSING)
                scrollback_height = _safe_attr(
                    info, "scrollback_buffer_height", default=_MISSING
                )
                if (
                    isinstance(overflow, bool)
                    or not isinstance(overflow, int)
                    or overflow < 0
                    or overflow > MAX_HISTORY_COORDINATE
                    or isinstance(scrollback_height, bool)
                    or not isinstance(scrollback_height, int)
                    or scrollback_height < 0
                    or scrollback_height > MAX_HISTORY_COORDINATE
                ):
                    raise RuntimeError("iTerm history line info is malformed")
                first = overflow
                end_exclusive = first + scrollback_height
                if end_exclusive > MAX_HISTORY_COORDINATE:
                    raise RuntimeError("iTerm history line range is malformed")
                selected_first = max(first, end_exclusive - last_lines)
                count = end_exclusive - selected_first
                if count < 0 or count > last_lines:
                    raise RuntimeError("iTerm history line range is malformed")
                if count == 0:
                    contents = None
                else:
                    self._assert_history_stamp(target_map, connection, app, session)
                    contents = await self._history_contents(
                        session,
                        raw_transaction,
                        info,
                        selected_first,
                        count,
                        connection=connection,
                        sdk=sdk,
                    )
                    self._assert_history_stamp(target_map, connection, app, session)
                return first, end_exclusive, selected_first, count, contents

            (
                first,
                end_exclusive,
                selected_first,
                count,
                contents,
            ) = await supervisor.run(
                transaction,
                body,
                abort_owned,
            )
            # Validate again after the SDK transaction has ended and before
            # decoding its result.  A same-ID replacement can happen while
            # the body is awaiting a line-info or contents response; the
            # captured object must never be turned into a DTO in that case.
            await self._revalidate_history_stamp(target_map, connection, app, session)
            if contents is None:
                lines: tuple[CaptureLineDTO, ...] = ()
            elif getattr(sdk, "__version__", None) == "2.30" and callable(
                getattr(getattr(sdk, "rpc", None), "async_get_screen_contents", None)
            ):
                lines = self._raw_history_lines(
                    contents, selected_first, count, self._max_capture_bytes
                )
            else:
                values = _line_values(contents)
                if len(values) > count:
                    values = values[:count]
                rows: list[tuple[int, str, bool | None]] = []
                lower_bound_bytes = 0
                for offset, value in enumerate(values):
                    text, wrapped = _line_text(value)
                    position = _safe_attr(
                        value,
                        "position",
                        "line",
                        "index",
                        default=selected_first + offset,
                    )
                    if (
                        isinstance(position, bool)
                        or not isinstance(position, int)
                        or position < 0
                    ):
                        position = selected_first + offset
                    rows.append((position, text, wrapped))
                    lower_bound_bytes += len(text)
                    if lower_bound_bytes > self._max_capture_bytes:
                        raise DomainError(
                            "ITEM_TOO_LARGE",
                            "history capture exceeds its bounded memory budget",
                        )
                encoded_bytes = 0
                for _, text, _wrapped in rows:
                    encoded_bytes += _text_bytes(text)
                    if encoded_bytes > self._max_capture_bytes:
                        raise DomainError(
                            "ITEM_TOO_LARGE",
                            "history capture exceeds its bounded memory budget",
                        )
                lines = tuple(
                    CaptureLineDTO(position=position, text=text, wrapped=wrapped)
                    for position, text, wrapped in rows
                )
            finished = _now()
            dto = CaptureDTO(
                screen={},
                context={},
                history_lines=tuple(lines),
                history_source_bounds={
                    "start": str(first),
                    "end_exclusive": str(end_exclusive),
                },
                target=target_map,
                captured_at=finished,
                truncated=False,
                # Overflow identifies the lower bound of retained history. It
                # is expected scrollback state, not evidence of a stream gap.
            )
            if _capture_size(dto) > self._max_capture_bytes:
                raise DomainError(
                    "ITEM_TOO_LARGE",
                    "history capture exceeds its bounded memory budget",
                )
            return dto
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm history capture failed"
            ) from exc

    async def send(self, target: Target, text: str) -> None:
        if _text_bytes(text) > MAX_INPUT_BYTES:
            raise DomainError("INPUT_TOO_LARGE", "terminal text exceeds 16KiB")
        target_map, session = await self._session_for(target)
        del target_map  # Target validation above is the important side effect.
        method = getattr(session, "async_send_text", None)
        if method is None:
            raise DomainError(
                "UNSUPPORTED_CAPABILITY", "iTerm text input is unavailable"
            )
        try:
            # Never use async_inject: it represents program output, not user
            # input.  Explicitly suppress iTerm broadcast to other sessions.
            await _invoke(method, text, suppress_broadcast=True)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(
                "BACKEND_UNAVAILABLE", "iTerm input delivery failed"
            ) from exc
