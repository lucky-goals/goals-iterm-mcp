"""Project absolute iTerm buffer coordinates onto returned screen rows."""

from __future__ import annotations


from dataclasses import dataclass


from collections.abc import Mapping


from typing import Any, Final


_MISSING: Final[object] = object()


_NO_DEFAULT: Final[object] = object()


class WireDecodeError(ValueError):
    """A malformed raw screen or line that cannot be safely decoded."""


@dataclass(frozen=True, slots=True)
class WirePoint:
    """Minimal point shape compatible with iTerm2's ``Point`` object."""

    x: int
    y: int


def _proto_field_present(value: Any, name: str) -> bool | None:
    """Return protobuf field presence when the object exposes it.

    Protobuf message attributes return a default nested message even when an
    optional message field was not set.  Presence matters for coordinate
    ranges: a default ``(0, 0)`` range is not evidence that a response was
    returned for the visible screen.  Duck-typed fixtures and the public SDK
    wrappers do not expose ``HasField``; ``None`` means that presence is
    unavailable and structural validation must decide instead.
    """

    has_field = getattr(value, "HasField", None)
    if not callable(has_field):
        return None
    try:
        return bool(has_field(name))
    except Exception:
        return None


def _field_alias(value: Any, names: tuple[str, ...], default: Any = _NO_DEFAULT) -> Any:
    """Read the first available spelling from a protobuf-like object."""

    for name in names:
        result = _field(value, name, _MISSING)
        if result is not _MISSING:
            return result
    if default is not _NO_DEFAULT:
        return default
    raise WireDecodeError(f"missing field {'/'.join(names)}")


def _point(value: Any) -> WirePoint | None:
    """Read a non-negative point from raw protobuf or SDK-shaped data."""

    if value is _MISSING or value is None:
        return None
    x = _field_alias(value, ("x", "column"), _MISSING)
    y = _field_alias(value, ("y", "row"), _MISSING)
    if (
        isinstance(x, bool)
        or not isinstance(x, int)
        or isinstance(y, bool)
        or not isinstance(y, int)
        or x < 0
        or y < 0
    ):
        return None
    return WirePoint(x=x, y=y)


def _window_bounds(
    window: Any,
) -> tuple[WirePoint, WirePoint, tuple[int, int] | None] | None:
    """Read the absolute coordinate bounds of a returned window.

    The raw protobuf spells the nested range ``coord_range`` while the SDK
    wrapper spells it ``coordRange`` and also exposes ``start``/``end``
    convenience properties.  Both forms describe absolute buffer coordinates
    and therefore provide the origin needed to project a cursor into the
    returned row slice.
    """

    if window is _MISSING or window is None:
        return None

    direct_start = _field(window, "start", _MISSING)
    direct_end = _field(window, "end", _MISSING)
    if direct_start is not _MISSING or direct_end is not _MISSING:
        start = _point(direct_start)
        end = _point(direct_end)
        raw_columns = _field(window, "columnRange", _MISSING)
    else:
        coord_range = _field_alias(window, ("coord_range", "coordRange"), _MISSING)
        if coord_range is _MISSING or coord_range is None:
            return None
        present = _proto_field_present(window, "coord_range")
        if present is False:
            return None
        start = _point(_field(coord_range, "start", _MISSING))
        end = _point(_field(coord_range, "end", _MISSING))

        # The raw response's column window is separate from coord_range.  A
        # non-empty column window cannot prove a full visible row, so retain
        # its location and length for the strict projection check below.
        raw_columns = _field(window, "columns", _MISSING)

    if start is None or end is None:
        return None
    column_range: tuple[int, int] | None = None
    if raw_columns is not _MISSING:
        if raw_columns is None:
            return None
        location = _field(raw_columns, "location", _MISSING)
        length = _field(raw_columns, "length", _MISSING)
        if (
            isinstance(location, bool)
            or not isinstance(location, int)
            or isinstance(length, bool)
            or not isinstance(length, int)
            or location < 0
            or length < 0
        ):
            return None
        column_range = (location, length)

    # CoordRange uses a half-open end coordinate.  The visible-screen
    # projection is intentionally narrower than generic range decoding: only
    # a complete, unwindowed set of rows can establish a row-zero origin.
    if end.y <= start.y:
        return None
    return start, end, column_range


def screen_cursor_coord(
    cursor: Any,
    windowed_coord_range: Any,
    *,
    row_count: int | None = None,
) -> WirePoint | None:
    """Project iTerm2's absolute cursor into a returned screen slice.

    iTerm2's ``Coord.y`` is an absolute, stable buffer line number.  The
    visible-screen DTO, however, numbers the returned rows from zero.  This
    helper subtracts the returned window's absolute origin and returns
    ``None`` whenever the response cannot prove the projection.  Callers that
    need absolute history coordinates must continue using the raw coordinates
    and returned range directly.
    """

    point = _point(cursor)
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count <= 0:
        return None
    bounds = _window_bounds(windowed_coord_range)
    if point is None or bounds is None:
        return None
    start, end, column_range = bounds
    if start.x != 0 or end.x != 0 or end.y - start.y != row_count:
        return None
    if column_range is not None and column_range != (0, 0):
        return None
    if point.y < start.y or point.y >= end.y:
        return None
    return WirePoint(x=point.x, y=point.y - start.y)


def _field(value: Any, name: str, default: Any = _NO_DEFAULT) -> Any:
    """Read a protobuf-like field without coercing malformed values."""

    if isinstance(value, Mapping):
        if name in value:
            return value[name]
        if default is not _NO_DEFAULT:
            return default
        raise WireDecodeError(f"missing field {name}")
    try:
        return getattr(value, name)
    except AttributeError as exc:
        if default is not _NO_DEFAULT:
            return default
        raise WireDecodeError(f"missing field {name}") from exc
    except Exception as exc:
        raise WireDecodeError(f"missing field {name}") from exc
