\set ON_ERROR_STOP on

-- pgcrypto gives the workload generator incompressible random bytes. Repeated
-- strings are a poor WAL demo because TOAST can compress them aggressively.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE public.demo_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    producer text NOT NULL DEFAULT 'slotlease-wal-generator',
    payload bytea NOT NULL
);

CREATE INDEX demo_events_created_at_idx
    ON public.demo_events (created_at);

COMMENT ON TABLE public.demo_events IS
    'Disposable workload table used to generate WAL retained by slotlease_demo.';
