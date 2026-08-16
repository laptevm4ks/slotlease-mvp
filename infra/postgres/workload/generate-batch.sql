\set ON_ERROR_STOP on

-- Values are supplied by scripts/generate-wal.sh after strict integer/range
-- validation. Keeping the workload in SQL makes it easy to inspect with psql.
INSERT INTO public.demo_events (payload)
SELECT gen_random_bytes(:payload_bytes)
FROM generate_series(1, :batch_rows);

