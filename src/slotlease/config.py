from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import yaml

from .errors import ConfigError
from .models import SlotPolicy
from .units import parse_bytes, parse_duration

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SLOT_NAME = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Settings:
    dsn: str | None
    plan_expires_in: timedelta
    slots: Mapping[str, SlotPolicy]

    def policy_for(self, slot_name: str) -> SlotPolicy | None:
        return self.slots.get(slot_name)


def load_config(path: str | Path, *, environ: Mapping[str, str] | None = None) -> Settings:
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {config_path}: {exc.strerror or exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc
    return parse_config(data, environ=environ)


def parse_config(
    data: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    root = _mapping(data, "configuration")
    _reject_unknown(root, {"version", "database", "plan", "defaults", "slots"}, "configuration")

    version = root.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported config version {version!r}; expected 1")

    database = _mapping(root.get("database", {}), "database")
    _reject_unknown(database, {"dsn"}, "database")
    raw_dsn = database.get("dsn")
    dsn = _expand_env(raw_dsn, env, "database.dsn") if raw_dsn is not None else None
    if dsn is not None and (not isinstance(dsn, str) or not dsn.strip()):
        raise ConfigError("database.dsn must be a non-empty string")

    plan = _mapping(root.get("plan", {}), "plan")
    _reject_unknown(plan, {"expires_in"}, "plan")
    plan_expires_in = parse_duration(plan.get("expires_in", "15m"), field="plan.expires_in")
    if plan_expires_in.total_seconds() <= 0:
        raise ConfigError("plan.expires_in must be greater than zero")

    defaults = _mapping(root.get("defaults", {}), "defaults")
    _reject_unknown(defaults, {"inactive_ttl", "max_retained_wal"}, "defaults")

    slots_data = _mapping(root.get("slots", {}), "slots")
    slots: dict[str, SlotPolicy] = {}
    for slot_name, raw_slot in slots_data.items():
        if not isinstance(slot_name, str) or not _SLOT_NAME.fullmatch(slot_name):
            raise ConfigError(
                f"slot name {slot_name!r} is invalid; use lowercase letters, digits, and underscores"
            )
        item = _mapping(raw_slot, f"slots.{slot_name}")
        _reject_unknown(
            item,
            {"owner", "inactive_ttl", "max_retained_wal", "allow_drop"},
            f"slots.{slot_name}",
        )
        owner = item.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            raise ConfigError(f"slots.{slot_name}.owner must be a non-empty string")

        ttl_value = item.get("inactive_ttl", defaults.get("inactive_ttl"))
        wal_value = item.get("max_retained_wal", defaults.get("max_retained_wal"))
        if ttl_value is None:
            raise ConfigError(f"slots.{slot_name}.inactive_ttl has no value or default")
        if wal_value is None:
            raise ConfigError(f"slots.{slot_name}.max_retained_wal has no value or default")

        allow_drop = item.get("allow_drop", False)
        if not isinstance(allow_drop, bool):
            raise ConfigError(f"slots.{slot_name}.allow_drop must be true or false")
        slots[slot_name] = SlotPolicy(
            owner=owner.strip(),
            inactive_ttl=parse_duration(ttl_value, field=f"slots.{slot_name}.inactive_ttl"),
            max_retained_wal=parse_bytes(
                wal_value, field=f"slots.{slot_name}.max_retained_wal"
            ),
            allow_drop=allow_drop,
        )

    return Settings(dsn=dsn.strip() if dsn else None, plan_expires_in=plan_expires_in, slots=slots)


def resolve_dsn(settings: Settings, cli_dsn: str | None, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    dsn = cli_dsn or env.get("SLOTLEASE_DSN") or settings.dsn
    if not dsn:
        raise ConfigError(
            "database DSN is required; pass --dsn, set SLOTLEASE_DSN, or configure database.dsn"
        )
    return dsn


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{field} must be a YAML mapping")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ConfigError(f"{field} has unknown keys: {', '.join(unknown)}")


def _expand_env(value: Any, environ: Mapping[str, str], field: str) -> Any:
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in environ:
            raise ConfigError(f"{field} references unset environment variable {name}")
        return environ[name]

    return _ENV_REFERENCE.sub(replace, value)

