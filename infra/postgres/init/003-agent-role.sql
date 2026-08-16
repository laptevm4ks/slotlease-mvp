\set ON_ERROR_STOP on

-- The agent can inspect cluster statistics and manage replication slots, but it
-- is deliberately not a superuser and cannot modify application rows.
CREATE ROLE slotlease_agent
    WITH LOGIN REPLICATION PASSWORD 'slotlease_agent_local_only';

GRANT pg_monitor TO slotlease_agent;
GRANT CONNECT ON DATABASE slotlease TO slotlease_agent;
GRANT USAGE ON SCHEMA public TO slotlease_agent;
GRANT SELECT ON TABLE public.demo_events TO slotlease_agent;

