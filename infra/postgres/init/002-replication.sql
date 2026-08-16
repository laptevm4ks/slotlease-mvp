\set ON_ERROR_STOP on

-- Publications describe which table changes pgoutput is allowed to send.
CREATE PUBLICATION slotlease_publication
    FOR TABLE public.demo_events;

-- This durable slot intentionally starts without a consumer. Its restart_lsn
-- therefore stays behind while demo_events receives writes.
SELECT slot_name, lsn
FROM pg_create_logical_replication_slot('slotlease_demo', 'pgoutput');
