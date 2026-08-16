\set ON_ERROR_STOP on
\pset pager off

\if :{?filesystem_free_bytes}
\else
  \echo 'ERROR: pass -v filesystem_free_bytes=<bytes>'
\endif

\if :{?safety_reserve_bytes}
\else
  \set safety_reserve_bytes 67108864
\endif

\if :{?sample_seconds}
\else
  \set sample_seconds 10
\endif

CREATE TEMP TABLE slotlease_wal_sample AS
SELECT clock_timestamp() AS sampled_at, wal_bytes
FROM pg_stat_wal;

SELECT pg_sleep(:'sample_seconds'::double precision);

WITH current_sample AS (
    SELECT clock_timestamp() AS sampled_at, wal_bytes
    FROM pg_stat_wal
),
wal_rate AS (
    SELECT
        (current_sample.wal_bytes - first_sample.wal_bytes)
        / NULLIF(
            EXTRACT(epoch FROM current_sample.sampled_at - first_sample.sampled_at),
            0
        ) AS bytes_per_second
    FROM current_sample
    CROSS JOIN slotlease_wal_sample AS first_sample
),
inputs AS (
    SELECT
        :'filesystem_free_bytes'::bigint AS free_bytes,
        :'safety_reserve_bytes'::bigint AS reserve_bytes
)
SELECT
    pg_size_pretty(inputs.free_bytes) AS filesystem_free,
    pg_size_pretty(inputs.reserve_bytes) AS safety_reserve,
    pg_size_pretty(round(wal_rate.bytes_per_second)::bigint) AS recent_wal_per_second,
    CASE
        WHEN wal_rate.bytes_per_second <= 0 THEN NULL
        ELSE GREATEST(inputs.free_bytes - inputs.reserve_bytes, 0)
             / wal_rate.bytes_per_second * interval '1 second'
    END AS estimated_time_to_reserve
FROM inputs
CROSS JOIN wal_rate;
