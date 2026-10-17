"""Stable, short names used to identify sessions in conversational tools."""

from __future__ import annotations

import secrets
import unicodedata
from collections.abc import Iterable

from .errors import DomainError


# Keep these names easy to say and type.  They are deliberately ASCII so that
# names stay legible in terminal tabs, shell prompts, and MCP responses.
NICKNAMES: tuple[str, ...] = (
    "acorn", "alder", "amber", "apex", "apple", "arrow", "aspire", "atlas",
    "badge", "basil", "beacon", "berry", "birch", "blaze", "bloom", "bolt",
    "brave", "brook", "cable", "cactus", "candle", "canary", "canyon", "caper",
    "cedar", "charm", "chase", "cider", "cirrus", "civic", "cliff", "cloud",
    "clover", "cobalt", "comet", "coral",
    "crane", "crest", "crisp", "crown", "cosmos", "daisy", "dawn", "delta",
    "dingo", "dragon", "dove", "drift",
    "eagle", "earth", "ember", "emerald", "fable", "falcon", "fern", "flare",
    "flint", "flora", "focus", "foxy", "frost", "gale", "garden", "gems",
    "glade", "gleam", "glint", "globe", "grain", "granite", "grape", "grove",
    "harbor", "haven", "hazel", "hearth", "hedge", "heron", "honey", "horizon",
    "indigo", "iris", "island", "ivory", "jade", "jasmine", "jolly", "juniper",
    "kestrel", "kindle", "kitten", "lagoon", "lark", "laser", "laurel", "leaf",
    "lemon", "lilac", "linen", "lotus", "lucid", "lumen", "maple", "marble",
    "marsh", "meadow", "meteor", "mint", "misty", "mocha", "moss", "nectar",
    "nova", "oasis", "ocean", "olive", "onyx", "orbit", "orchid", "otter",
    "panda", "paper", "pearl", "pebble", "pepper", "petal", "phoenix", "piano",
    "pilot", "pine", "pixel", "plume", "polar", "pond", "prairie", "prism",
    "quartz", "quest", "raven", "reef", "relic", "ripple", "river", "robin",
    "rocket", "rose", "sable", "sage", "sail", "scarlet", "scout", "shale",
    "shell", "shore", "signal", "silk", "slate", "solar", "sonic", "sparrow",
    "spark", "spice", "spirit", "spruce", "star", "stone", "storm", "summit",
    "sunny", "swift", "tango", "teal", "terra", "thistle", "thunder", "tiger",
    "timber", "topaz", "torch", "trail", "triton", "tulip", "umber", "velvet",
    "verve", "violet", "vista", "water", "whale", "willow", "wind", "winter",
    "wisp", "wolf", "wonder", "xenon", "yarrow", "zephyr", "zinc", "zinnia",
)

NICKNAME_VARIABLE = "user.goals_iterm_mcp_nickname"
MIN_NICKNAME_LENGTH = 4
MAX_NICKNAME_LENGTH = 7
MAX_EXPLICIT_NAME_LENGTH = 64


def _validate_catalogue() -> None:
    folded = [name.casefold() for name in NICKNAMES]
    if len(NICKNAMES) != 200:
        raise RuntimeError("the nickname catalogue must contain exactly 200 names")
    if len(set(folded)) != len(folded):
        raise RuntimeError("the nickname catalogue must contain distinct names")
    for name in NICKNAMES:
        if not MIN_NICKNAME_LENGTH <= len(name) <= MAX_NICKNAME_LENGTH:
            raise RuntimeError(f"invalid nickname length: {name!r}")
        if not name.isascii() or not name.isalpha() or name != name.lower():
            raise RuntimeError(f"invalid nickname characters: {name!r}")


_validate_catalogue()


def normalize_name(value: str) -> str:
    """Validate and normalize an explicit name before sending it to iTerm."""

    if not isinstance(value, str):
        raise DomainError("INVALID_ARGUMENT", "name must be a string")
    name = value.strip()
    if not name:
        raise DomainError("INVALID_ARGUMENT", "name must not be empty")
    if len(name) > MAX_EXPLICIT_NAME_LENGTH:
        raise DomainError(
            "INVALID_ARGUMENT",
            f"name must contain at most {MAX_EXPLICIT_NAME_LENGTH} characters",
        )
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise DomainError("INVALID_ARGUMENT", "name is not valid Unicode") from exc
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise DomainError("INVALID_ARGUMENT", "name contains a control character")
    return name


def name_key(value: str) -> str:
    """Return the case-insensitive key used for collision checks."""

    return normalize_name(value).casefold()


def _occupied_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return name_key(value)
    except DomainError:
        # Keep malformed foreign titles visible to collision checks when they
        # differ only by surrounding whitespace.  They are never emitted as a
        # generated nickname themselves.
        return value.strip().casefold() or None


def choose_nickname(occupied: Iterable[str]) -> str:
    """Choose one unused catalogue name without retrying a random write."""

    occupied_keys = set()
    for value in occupied:
        key = _occupied_key(value)
        if key is not None:
            occupied_keys.add(key)
    available = [name for name in NICKNAMES if name.casefold() not in occupied_keys]
    if not available:
        raise DomainError("NICKNAME_POOL_EXHAUSTED", "no unused session nickname remains")
    return secrets.choice(available)


def ensure_name_available(name: str, occupied: Iterable[str]) -> str:
    """Validate an explicit name and reject a case-insensitive collision."""

    normalized = normalize_name(name)
    key = normalized.casefold()
    if any(_occupied_key(value) == key for value in occupied):
        raise DomainError("NICKNAME_IN_USE", "session name is already in use")
    return normalized


__all__ = [
    "MAX_EXPLICIT_NAME_LENGTH",
    "MAX_NICKNAME_LENGTH",
    "MIN_NICKNAME_LENGTH",
    "NICKNAME_VARIABLE",
    "NICKNAMES",
    "choose_nickname",
    "ensure_name_available",
    "name_key",
    "normalize_name",
]
