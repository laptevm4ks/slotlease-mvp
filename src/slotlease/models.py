from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return utc_datetime(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utc_datetime(parsed)


@dataclass(frozen=True)
class ClusterIdentity:
    system_identifier: str
    database: str

    def to_dict(self) -> dict[str, str]:
        return {
            "system_identifier": self.system_identifier,
            "database": self.database,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClusterIdentity":
        return cls(
            system_identifier=str(value["system_identifier"]),
            database=str(value["database"]),
        )


@dataclass(frozen=True)
class SlotSnapshot:
    slot_name: str
    plugin: str | None
    slot_type: str
    database: str | None
    temporary: bool
    active: bool
    active_pid: int | None
    xmin: str | None
    catalog_xmin: str | None
    restart_lsn: str | None
    confirmed_flush_lsn: str | None
    inactive_since: datetime | None
    retained_wal_bytes: int
    conflicting: bool | None
    invalidation_reason: str | None
    failover: bool
    synced: bool

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "SlotSnapshot":
        inactive_since = row.get("inactive_since")
        if inactive_since is not None:
            inactive_since = utc_datetime(inactive_since)
        return cls(
            slot_name=str(row["slot_name"]),
            plugin=_optional_text(row.get("plugin")),
            slot_type=str(row["slot_type"]),
            database=_optional_text(row.get("database")),
            temporary=bool(row["temporary"]),
            active=bool(row["active"]),
            active_pid=int(row["active_pid"]) if row.get("active_pid") is not None else None,
            xmin=_optional_text(row.get("xmin")),
            catalog_xmin=_optional_text(row.get("catalog_xmin")),
            restart_lsn=_optional_text(row.get("restart_lsn")),
            confirmed_flush_lsn=_optional_text(row.get("confirmed_flush_lsn")),
            inactive_since=inactive_since,
            retained_wal_bytes=max(int(row.get("retained_wal_bytes") or 0), 0),
            conflicting=_optional_bool(row.get("conflicting")),
            invalidation_reason=_optional_text(row.get("invalidation_reason")),
            failover=bool(row.get("failover", False)),
            synced=bool(row.get("synced", False)),
        )

    def inactive_for(self, now: datetime) -> timedelta | None:
        if self.active or self.inactive_since is None:
            return None
        return max(utc_datetime(now) - self.inactive_since, timedelta(0))

    def safety_dict(self) -> dict[str, Any]:
        """Stable fields that must not change between plan and apply.

        ``retained_wal_bytes`` is intentionally excluded: it normally grows while
        an abandoned slot is inactive. The LSN positions are included, so any
        consumer progress invalidates the approval.
        """

        return {
            "slot_name": self.slot_name,
            "plugin": self.plugin,
            "slot_type": self.slot_type,
            "database": self.database,
            "temporary": self.temporary,
            "active": self.active,
            "active_pid": self.active_pid,
            "xmin": self.xmin,
            "catalog_xmin": self.catalog_xmin,
            "restart_lsn": self.restart_lsn,
            "confirmed_flush_lsn": self.confirmed_flush_lsn,
            "inactive_since": format_timestamp(self.inactive_since) if self.inactive_since else None,
            "conflicting": self.conflicting,
            "invalidation_reason": self.invalidation_reason,
            "failover": self.failover,
            "synced": self.synced,
        }

    def report_dict(self, now: datetime) -> dict[str, Any]:
        inactive_for = self.inactive_for(now)
        return {
            **self.safety_dict(),
            "retained_wal_bytes": self.retained_wal_bytes,
            "inactive_for_seconds": (
                int(inactive_for.total_seconds()) if inactive_for is not None else None
            ),
        }


@dataclass(frozen=True)
class SlotPolicy:
    owner: str
    inactive_ttl: timedelta
    max_retained_wal: int
    allow_drop: bool = False

    def to_dict(self) -> dict[str, Any]:
        ttl_seconds = self.inactive_ttl.total_seconds()
        return {
            "owner": self.owner,
            "inactive_ttl_seconds": (
                int(ttl_seconds) if ttl_seconds.is_integer() else ttl_seconds
            ),
            "max_retained_wal_bytes": self.max_retained_wal,
            "allow_drop": self.allow_drop,
        }


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_bool(value: Any) -> bool | None:
    return bool(value) if value is not None else None

