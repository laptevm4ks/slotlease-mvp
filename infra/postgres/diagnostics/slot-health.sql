\set ON_ERROR_STOP on
\pset pager off

\echo '--- server guardrails ---'
SELECT name, setting, unit
FROM pg_settings
WHERE name IN (
    'wal_level',
    'max_replication_slots',
    'max_wal_senders',
    'max_slot_wal_keep_size',
    'idle_replication_slot_timeout',
    'max_wal_size',
    'checkpoint_timeout'
)
ORDER BY name;

\echo '--- logical slot health ---'
WITH wal_rate AS (
    SELECT
        wal_bytes,
        stats_reset,
        wal_bytes / NULLIF(EXTRACT(epoch FROM clock_timestamp() - stats_reset), 0)
            AS avg_bytes_per_second
    FROM pg_stat_wal
),
slot_health AS (
    SELECT
        s.*,
        GREATEST(
            pg_wal_lsn_diff(pg_current_wal_insert_lsn(), s.restart_lsn),
            0
        )::bigint AS retained_wal_bytes,
        GREATEST(
            pg_wal_lsn_diff(pg_current_wal_insert_lsn(), s.confirmed_flush_lsn),
            0
        )::bigint AS consumer_lag_bytes
    FROM pg_replication_slots AS s
    WHERE s.slot_type = 'logical'
)
SELECT
    sh.slot_name,
    sh.plugin,
    sh.database,
    sh.active,
    sh.active_pid,
    sh.inactive_since,
    CASE
        WHEN sh.inactive_since IS NULL THEN NULL
        ELSE clock_timestamp() - sh.inactive_since
    END AS inactive_for,
    sh.restart_lsn,
    sh.confirmed_flush_lsn,
    pg_size_pretty(sh.retained_wal_bytes) AS retained_wal,
    pg_size_pretty(sh.consumer_lag_bytes) AS consumer_lag,
    sh.wal_status,
    pg_size_pretty(sh.safe_wal_size) AS safe_wal_remaining,
    sh.invalidation_reason,
    pg_size_pretty(round(wr.avg_bytes_per_second)::bigint) AS avg_wal_per_second,
    CASE
        WHEN sh.safe_wal_size IS NULL OR wr.avg_bytes_per_second <= 0 THEN NULL
        ELSE (sh.safe_wal_size / wr.avg_bytes_per_second)::double precision
            * interval '1 second'
    END AS eta_to_slot_loss_at_avg_rate
FROM slot_health AS sh
CROSS JOIN wal_rate AS wr
ORDER BY sh.slot_name;

\echo '--- physical size of pg_wal (not retained bytes) ---'
SELECT
    count(*) AS wal_files,
    pg_size_pretty(COALESCE(sum(size), 0)::bigint) AS pg_wal_directory_size
FROM pg_ls_waldir();

\echo '--- active replication processes ---'
SELECT
    s.slot_name,
    a.pid,
    a.usename,
    a.application_name,
    a.client_addr,
    a.backend_type,
    a.state,
    a.wait_event_type,
    a.wait_event,
    clock_timestamp() - a.backend_start AS connected_for
FROM pg_replication_slots AS s
JOIN pg_stat_activity AS a ON a.pid = s.active_pid
ORDER BY s.slot_name;

\echo '--- filesystem capacity (run through psql inside the container) ---'
\! df -h "$PGDATA"

-- avg_wal_per_second is an average since pg_stat_wal.stats_reset. For an alert
-- or production ETA, take two samples 30-60 seconds apart and use their delta.
