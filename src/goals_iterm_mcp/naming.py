"""Stable, short names used to identify sessions in conversational tools."""

from __future__ import annotations

import secrets
import unicodedata
from collections.abc import Iterable

from .errors import DomainError


# Keep these names memorable, easy to say, and easy to type (200 distinct
# lowercase ASCII words, strictly 4 to 6 characters each).
NICKNAMES: tuple[str, ...] = (
    "acorn", "alpha", "amber", "apex", "apple", "arrow", "aspen", "atlas",
    "azure", "badge", "bamboo", "basil", "beacon", "berry", "birch", "blaze",
    "bloom", "bolt", "breeze", "brook", "cabin", "cactus", "candle", "canyon",
    "cedar", "charm", "cider", "cliff", "cloud", "clover", "cobalt", "comet",
    "coral", "cosmos", "crane", "crest", "crisp", "crown", "daisy", "dawn",
    "delta", "dingo", "dove", "dragon", "dream", "drift", "dune", "eagle",
    "earth", "echo", "ember", "falcon", "fern", "flame", "flare", "flash",
    "flint", "flora", "focus", "forge", "frost", "galaxy", "gale", "garden",
    "gecko", "glade", "gleam", "globe", "grain", "grape", "grove", "halo",
    "harbor", "haven", "hawk", "hazel", "helix", "heron", "honey", "husky",
    "igloo", "indigo", "iris", "island", "ivory", "jade", "jazz", "jewel",
    "kayak", "kite", "kitten", "kiwi", "koala", "lagoon", "lake", "lark",
    "laser", "laurel", "lava", "leaf", "lemon", "lilac", "lime", "lion",
    "lotus", "lucky", "lumen", "lunar", "lynx", "magic", "mango", "maple",
    "marsh", "meadow", "meteor", "mint", "misty", "mocha", "moose", "moss",
    "music", "nectar", "neon", "nexus", "noble", "north", "nova", "oasis",
    "ocean", "olive", "omega", "onyx", "opera", "orbit", "orchid", "otter",
    "panda", "peach", "pearl", "pebble", "pepper", "petal", "piano", "pilot",
    "pine", "pixel", "planet", "plum", "polar", "pond", "poppy", "prism",
    "quartz", "quest", "radar", "raven", "reef", "river", "robin", "rocket",
    "rose", "ruby", "safari", "sage", "sail", "scout", "shore", "silver",
    "solar", "sonic", "spark", "spruce", "star", "stone", "storm", "summit",
    "sunny", "swift", "tango", "teal", "terra", "tiger", "timber", "topaz",
    "torch", "trail", "tulip", "velvet", "violet", "vista", "whale", "willow",
    "winter", "wolf", "wonder", "xenon", "zebra", "zenith", "zephyr", "zinc",
)

NICKNAME_VARIABLE = "user.goals_iterm_mcp_nickname"
MIN_NICKNAME_LENGTH = 4
MAX_NICKNAME_LENGTH = 6
MAX_EXPLICIT_NAME_LENGTH = 64
RESERVED_SELECTORS = frozenset({"active", "all", "current", "focused", "*"})


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
    if name.casefold() in RESERVED_SELECTORS:
        raise DomainError("INVALID_ARGUMENT", "name must not be a reserved selector")
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
