"""Rendered terminal read results shared by the backend and MCP tools."""

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, TypeAlias

Target: TypeAlias = Mapping[str, str]
MAX_HISTORY_COORDINATE = (1 << 64) - 1


@dataclass(frozen=True, slots=True)
class CaptureLineDTO:
    position: int
    text: str
    wrapped: bool | None = None

    def __post_init__(self):
        if type(self.position) is not int or self.position < 0:
            raise ValueError("position must be a non-negative integer")
        self.text.encode("utf-8", errors="strict")
        if self.wrapped is not None and type(self.wrapped) is not bool:
            raise TypeError("wrapped must be boolean or null")


@dataclass(frozen=True, slots=True)
class CaptureDTO:
    screen: Mapping[str, Any] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    history_lines: Sequence[CaptureLineDTO] = field(default_factory=tuple)
    history_source_bounds: Mapping[str, Any] | None = None
    target: Target | None = None
    captured_at: str | None = None
    truncated: bool = False

    def __post_init__(self):
        if not isinstance(self.screen, Mapping) or not isinstance(
            self.context, Mapping
        ):
            raise TypeError("screen and context must be mappings")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be boolean")
        bounds = self.history_source_bounds
        if bounds is not None:
            if not isinstance(bounds, Mapping) or set(bounds) != {
                "start",
                "end_exclusive",
            }:
                raise ValueError("history bounds must contain start and end_exclusive")
            for value in bounds.values():
                if type(value) is int:
                    parsed = value
                elif (
                    isinstance(value, str)
                    and value.isascii()
                    and value.isdecimal()
                    and len(value) <= 20
                ):
                    parsed = int(value)
                else:
                    raise ValueError("history bounds must be decimal integers")
                if not 0 <= parsed <= MAX_HISTORY_COORDINATE:
                    raise ValueError("history bounds exceed uint64")
            if int(bounds["end_exclusive"]) < int(bounds["start"]):
                raise ValueError("history bounds are reversed")
