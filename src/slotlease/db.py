from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .models import ClusterIdentity, SlotSnapshot

_SLOT_COLUMNS = """
    slot_name::text AS slot_name,
    plugin::text AS plugin,
    slot_type,
    database::text AS database,
    temporary,
    active,
    active_pid,
    xmin::text AS xmin,
    catalog_xmin::text AS catalog_xmin,
    restart_lsn::text AS restart_lsn,
    confirmed_flush_lsn::text AS confirmed_flush_lsn,
    inactive_since,
    COALESCE(
        GREATEST(pg_wal_lsn_diff(pg_current_wal_insert_lsn(), restart_lsn), 0),
        0
    )::bigint AS retained_wal_bytes,
    conflicting,
    invalidation_reason,
    failover,
    synced
"""


@contextmanager
def connection(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """Open a short-lived autocommit connection with an identifiable app name."""

    with psycopg.connect(
        dsn,
        autocommit=True,
        application_name="slotlease",
        row_factory=dict_row,
    ) as conn:
        yield conn


def fetch_cluster_identity(conn: psycopg.Connection[Any]) -> ClusterIdentity:
    row = conn.execute(
        """
        SELECT system_identifier::text AS system_identifier,
               current_database() AS database
        FROM pg_control_system()
        """
    ).fetchone()
    if row is None:  # pragma: no cover - PostgreSQL always returns one row
        raise RuntimeError("pg_control_system() returned no row")
    return ClusterIdentity.from_mapping(row)


def fetch_slots(conn: psycopg.Connection[Any]) -> list[SlotSnapshot]:
    rows = conn.execute(
        f"SELECT {_SLOT_COLUMNS} FROM pg_replication_slots ORDER BY slot_name"
    ).fetchall()
    return [SlotSnapshot.from_mapping(row) for row in rows]


def fetch_slot(conn: psycopg.Connection[Any], slot_name: str) -> SlotSnapshot | None:
    row = conn.execute(
        f"SELECT {_SLOT_COLUMNS} FROM pg_replication_slots WHERE slot_name = %s",
        (slot_name,),
    ).fetchone()
    return SlotSnapshot.from_mapping(row) if row is not None else None


def drop_replication_slot(conn: psycopg.Connection[Any], slot_name: str) -> None:
    # A bound parameter is important here: slot names are data, never SQL text.
    conn.execute("SELECT pg_drop_replication_slot(%s)", (slot_name,)).fetchone()
