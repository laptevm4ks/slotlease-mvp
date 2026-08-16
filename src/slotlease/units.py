from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ConfigError

_DURATION_TOKEN = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h|d|w)", re.IGNORECASE)
_DURATION_FACTORS = {
    "ms": Decimal("0.001"),
    "s": Decimal(1),
    "m": Decimal(60),
    "h": Decimal(3600),
    "d": Decimal(86400),
    "w": Decimal(604800),
}

_BYTE_VALUE = re.compile(
    r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$",
    re.IGNORECASE,
)
_BYTE_FACTORS = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "KIB": 1 << 10,
    "MIB": 1 << 20,
    "GIB": 1 << 30,
    "TIB": 1 << 40,
}


def parse_duration(value: Any, *, field: str = "duration") -> timedelta:
    """Parse values such as ``30s``, ``15m`` or ``1h30m``.

    Unit suffixes are mandatory in YAML strings to prevent a millisecond/second
    misunderstanding. Numeric values are accepted as seconds for programmatic use.
    """

    if isinstance(value, timedelta):
        duration = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        duration = timedelta(seconds=float(value))
    elif isinstance(value, str):
        compact = re.sub(r"\s+", "", value)
        if not compact:
            raise ConfigError(f"{field} must not be empty")

        total_seconds = Decimal(0)
        position = 0
        for match in _DURATION_TOKEN.finditer(compact):
            if match.start() != position:
                raise ConfigError(f"{field} has invalid duration {value!r}")
            try:
                amount = Decimal(match.group("value"))
            except InvalidOperation as exc:  # pragma: no cover - guarded by regex
                raise ConfigError(f"{field} has invalid duration {value!r}") from exc
            total_seconds += amount * _DURATION_FACTORS[match.group("unit").lower()]
            position = match.end()
        if position != len(compact) or position == 0:
            raise ConfigError(f"{field} has invalid duration {value!r}")
        duration = timedelta(seconds=float(total_seconds))
    else:
        raise ConfigError(f"{field} must be a duration such as '15m' or '1h30m'")

    if duration.total_seconds() < 0:
        raise ConfigError(f"{field} must be zero or greater")
    return duration


def parse_bytes(value: Any, *, field: str = "bytes") -> int:
    """Parse byte budgets such as ``1B``, ``64MiB`` or ``2GB``."""

    if isinstance(value, int) and not isinstance(value, bool):
        result = value
    elif isinstance(value, str):
        match = _BYTE_VALUE.fullmatch(value.strip())
        if not match:
            raise ConfigError(f"{field} must be a byte size such as '64MiB'")
        amount = Decimal(match.group("value"))
        total = amount * _BYTE_FACTORS[match.group("unit").upper()]
        if total != total.to_integral_value():
            raise ConfigError(f"{field} must resolve to a whole number of bytes")
        result = int(total)
    else:
        raise ConfigError(f"{field} must be an integer or a size such as '64MiB'")

    if result < 0:
        raise ConfigError(f"{field} must be zero or greater")
    return result


def human_bytes(value: int) -> str:
    amount = float(max(value, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def human_duration(value: timedelta | None) -> str:
    if value is None:
        return "-"
    seconds = max(int(value.total_seconds()), 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return "".join(parts[:2])

