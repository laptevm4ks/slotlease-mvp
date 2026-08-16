# SlotLease

**Safety-first lifecycle control for PostgreSQL logical replication slots.**

SlotLease inventories logical slots, measures the WAL each slot retains, evaluates
owner-defined TTL and byte budgets, and produces an auditable cleanup plan. A slot
is dropped only by a separate `apply` command with the exact generated plan ID.

> Project status: portfolio-grade MVP and local incident lab. The default workflow
> is intentionally conservative; it never auto-drops an unknown or active slot.

The detailed two-to-four-week implementation sequence is available in
[`ROADMAP.ru.md`](ROADMAP.ru.md).

## Why this exists

A logical replication slot is durable cluster state. If its consumer disappears,
PostgreSQL can keep WAL needed by that consumer long after the application that
created the slot is gone. The result is often discovered as a disk-space incident,
while the responsible pipeline, preview environment, or team is no longer obvious.

SlotLease turns that operational ambiguity into a small control loop:

1. **Scan** the cluster and explain the observed risk.
2. **Plan** only policy-managed, inactive slots explicitly marked `allow_drop`.
3. **Apply** a short-lived, integrity-protected plan after an exact confirmation.

PostgreSQL documents both the fields exposed by
[`pg_replication_slots`](https://www.postgresql.org/docs/18/view-pg-replication-slots.html)
and the disk-risk of unbounded slot retention in
[`max_slot_wal_keep_size`](https://www.postgresql.org/docs/18/runtime-config-replication.html).

## What the MVP demonstrates

- PostgreSQL 18 logical decoding, publications, LSN arithmetic, and WAL retention.
- SQL diagnostics across `pg_replication_slots`, `pg_stat_activity`, `pg_stat_wal`,
  `pg_settings`, and `pg_ls_waldir()`.
- A policy-driven Python CLI with human-readable and JSON output.
- A deliberately explicit `scan -> plan -> apply` safety boundary.
- Docker Compose failure injection with a stopped logical replication consumer.
- An isolated integration test that verifies a real slot is removed only after the
  generated plan ID is confirmed.

## Architecture

```mermaid
flowchart LR
    P["PostgreSQL 18"] -->|"read-only catalog queries"| S["slotlease scan"]
    C["YAML ownership and limits"] --> S
    S --> R["risk report"]
    S -->|"allow_drop + violated policy"| G["slotlease plan"]
    G --> J["integrity-protected JSON plan\ncluster + expiry + snapshot"]
    J -->|"exact --confirm PLAN_ID"| A["slotlease apply"]
    A -->|"revalidate safety snapshot"| P
    A --> U["audit-friendly result"]
```

The CLI connects directly to PostgreSQL. It has no daemon, queue, or control-plane
dependency in the MVP, which keeps the first version observable and easy to debug.

## Safety contract

| Condition | `scan` | `plan` | `apply` |
|---|---:|---:|---:|
| Slot is not declared in policy | `UNMANAGED` | excluded | impossible |
| `allow_drop: false` | reported | excluded | impossible |
| Slot is active | reported | excluded | rejected |
| Slot is temporary or synchronized | reported | excluded | rejected |
| TTL/WAL budget is healthy | reported | excluded | impossible |
| Plan expired, changed, or belongs to another cluster | n/a | n/a | rejected |
| Exact plan ID was not supplied | n/a | generated | rejected |

`apply` re-reads the slot before deletion. A plan is not permission to act on stale
state: cluster identity, plan integrity, expiry, slot type, activity, and the safety
snapshot must still match.

PostgreSQL's `REPLICATION` privilege is cluster-wide rather than a per-slot ACL.
The lab therefore proves least privilege relative to a superuser, but application
policy cannot contain stolen replication credentials. A production deployment
should use a read-only identity for `scan` and inject a short-lived replication
credential only into the approved `apply` job.

## Quick start

### Prerequisites

- Docker Engine with Docker Compose v2
- Python 3.11+ (the container image uses Python 3.12)
- Bash (Git Bash or WSL is sufficient on Windows)
- GNU Make is optional; every command is also shown without it

### 1. Install the CLI

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

PowerShell activation:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

### 2. Start the disposable PostgreSQL lab

```bash
docker compose up -d --wait postgres
# or: make up
```

The initialization scripts create:

- database `slotlease`, admin role `slotlease_admin`, and non-superuser
  `slotlease_agent` with only `REPLICATION` plus `pg_monitor`;
- table `public.demo_events`;
- publication `slotlease_publication`;
- inactive logical slot `slotlease_demo` using `pgoutput`.

The lab publishes PostgreSQL on `127.0.0.1:55432`. Its password is intentionally
local-only and must not be reused outside this repository.

### 3. Scan the initial slot

```bash
python -m slotlease \
  --config config.example.yaml \
  --dsn postgresql://slotlease_agent:slotlease_agent_local_only@127.0.0.1:55432/slotlease \
  scan --format table
```

Representative output:

```text
SLOT            TYPE     ACTIVE  INACTIVE  RETAINED WAL  STATUS   OWNER          PLAN  REASONS / BLOCKERS
slotlease_demo  logical  no      8s        0 B           HEALTHY  data-platform  -     -
```

For automation, request stable JSON instead of parsing the table:

```bash
python -m slotlease --config config.example.yaml scan --format json
# or: make scan-json
```

### 4. Reproduce the incident safely

The slot has no consumer by default. Generate incompressible row changes until it
retains approximately 64 MiB of WAL:

```bash
TARGET_WAL_MB=64 bash scripts/generate-wal.sh
# or: make wal TARGET_WAL_MB=64
```

The script refuses to run if the slot is active, stops at a built-in 96 MiB limit,
and the server has an independent `max_slot_wal_keep_size=128MB` guardrail. This is
a controlled simulation, not a real disk-exhaustion command.

Inspect PostgreSQL directly:

```bash
docker compose exec -T postgres \
  psql -U slotlease_admin -d slotlease \
  -f /opt/slotlease/sql/slot-health.sql
```

Run `scan` again. The policy should now explain that the retained-WAL budget is
violated rather than merely emitting an unexplained red status.

```text
SLOT            TYPE     ACTIVE  INACTIVE  RETAINED WAL  STATUS     OWNER          PLAN  REASONS / BLOCKERS
slotlease_demo  logical  no      3m14s     66.0 MiB      VIOLATION  data-platform  drop  retained_wal_exceeded
```

### 5. Generate and review a plan

```bash
mkdir -p .slotlease
python -m slotlease --config config.example.yaml \
  plan --output .slotlease/plan.json --expires-in 15m
```

The command prints the plan ID and the exact follow-up command. Review the file;
do not extract a slot name from table output and call `pg_drop_replication_slot`
directly.

```json
{
  "schema_version": 1,
  "plan_id": "<generated-plan-id>",
  "created_at": "<UTC timestamp>",
  "expires_at": "<UTC timestamp + 15 minutes>",
  "cluster": {
    "system_identifier": "<PostgreSQL system identifier>",
    "database": "slotlease"
  },
  "actions": [
    {
      "action": "drop_replication_slot",
      "slot_name": "slotlease_demo",
      "reasons": ["retained WAL exceeds 64 MiB"]
    }
  ],
  "integrity": {
    "algorithm": "sha256",
    "digest": "<digest>"
  }
}
```

### 6. Apply with an exact confirmation

```bash
PLAN_ID="$(python -c 'import json; print(json.load(open(".slotlease/plan.json"))["plan_id"])')"

python -m slotlease --config config.example.yaml \
  apply --plan .slotlease/plan.json --confirm "${PLAN_ID}"

# Equivalent Make target:
# make apply PLAN_ID="${PLAN_ID}"
```

Expected result:

```text
APPLIED drop_replication_slot slotlease_demo
Summary: 1 applied, 0 skipped, 0 failed
```

Bring the lab down and remove its disposable volume:

```bash
docker compose down --volumes --remove-orphans
# or: make down
```

## Policy file

`config.example.yaml` documents the complete MVP schema and points at the local
Compose port:

```yaml
version: 1

database:
  dsn: postgresql://slotlease_agent:slotlease_agent_local_only@127.0.0.1:55432/slotlease

plan:
  expires_in: 15m

defaults:
  inactive_ttl: 1h
  max_retained_wal: 512MiB

slots:
  slotlease_demo:
    owner: data-platform
    inactive_ttl: 5m
    max_retained_wal: 64MiB
    allow_drop: true
```

DSN precedence is `--dsn` argument, then `SLOTLEASE_DSN`, then
`database.dsn`. In a real environment, keep credentials out of Git and provide
the DSN through a secret manager or runtime environment variable.

Durations accept values such as `30s`, `15m`, `2h`, and `1h30m`. Byte budgets
accept `B`, `KB`, `MB`, `GB`, `KiB`, `MiB`, and `GiB` suffixes.

## CLI reference

```text
slotlease [--config PATH] [--dsn DSN] scan [--format table|json]
slotlease [--config PATH] [--dsn DSN] plan --output PATH [--expires-in 15m]
slotlease [--config PATH] [--dsn DSN] apply --plan PATH --confirm PLAN_ID
```

- `scan` is read-only and includes unmanaged slots.
- `plan` includes only managed violations with `allow_drop: true`.
- `apply` is the only command allowed to mutate PostgreSQL.

## SQL diagnostics

The standalone diagnostic at
`infra/postgres/diagnostics/slot-health.sql` answers four different questions:

1. How far is `restart_lsn` behind the current insert LSN?
2. How far is `confirmed_flush_lsn` behind the producer?
3. How many bytes remain before the configured slot cap can make the slot unsafe?
4. Which backend is actively consuming a slot?

The essential retained-WAL expression is:

```sql
SELECT
    slot_name,
    pg_wal_lsn_diff(pg_current_wal_insert_lsn(), restart_lsn)::bigint
        AS retained_wal_bytes
FROM pg_replication_slots
WHERE slot_type = 'logical';
```

Do not confuse this logical estimate with the physical size returned by
`pg_ls_waldir()`. Other requirements—checkpoints, archiving, recovery, and other
slots—also affect files present in `pg_wal`.

The included SQL prints an ETA based on the average WAL rate since
`pg_stat_wal.stats_reset`. That number is useful for the demo but deliberately
labelled as an estimate. Production alerting should calculate a rate from two
samples 30–60 seconds apart and use actual filesystem free bytes.

The second diagnostic performs a recent 10-second sample and accepts real free
filesystem bytes from the container:

```bash
FREE_BYTES="$(docker compose exec -T postgres sh -c \
  'df -B1 --output=avail "$PGDATA" | tail -1 | tr -d " "')"

docker compose exec -T postgres \
  psql -U slotlease_admin -d slotlease \
  -v filesystem_free_bytes="${FREE_BYTES}" \
  -v safety_reserve_bytes=67108864 \
  -f /opt/slotlease/sql/disk-eta.sql
```

The JSON digest is **tamper-evident, not an authentication signature**. It catches
accidental plan edits; a production approval service should sign plans with a key
that the plan author cannot access.

## Optional consumer

Start `pg_recvlogical` to prove that consumer feedback advances the slot:

```bash
docker compose --profile consumer up -d consumer
docker compose logs -f consumer
```

Stop it before running the failure generator again:

```bash
docker compose --profile consumer stop consumer
```

## Container image

Build the non-root, multi-stage image:

```bash
docker build --tag slotlease:dev .
# or: make docker-build
```

Run it on the Compose network with a read-only policy mount:

```bash
docker run --rm \
  --network slotlease-mvp_default \
  --mount type=bind,src="$(pwd)/config.example.yaml",dst=/config.yaml,readonly \
  --env SLOTLEASE_DSN=postgresql://slotlease_agent:slotlease_agent_local_only@postgres:5432/slotlease \
  slotlease:dev --config /config.yaml scan --format table
```

The image contains no policy or database credential. It runs as UID/GID `10001`
and defaults to `--help`, so starting the image without arguments is non-mutating.

## Tests

Fast tests:

```bash
python -m pytest -m "not integration" -q
# or: make test
```

Real lifecycle test:

```bash
python -m pytest tests/test_integration_lifecycle.py -m integration -vv
# or: make test-integration
```

The integration test:

1. allocates an available host port and unique Compose project name;
2. starts a fresh PostgreSQL volume;
3. creates or verifies the inactive logical slot and produces WAL;
4. invokes the public CLI through `scan -> plan -> apply`;
5. reads `plan_id` from JSON instead of scraping terminal text;
6. proves a wrong confirmation is rejected and leaves the slot intact;
7. verifies through SQL that the correctly confirmed slot was removed;
8. destroys only its uniquely named disposable volume.

Environment overrides such as `SLOTLEASE_TEST_COMPOSE_FILE`,
`SLOTLEASE_TEST_POSTGRES_SERVICE`, and `SLOTLEASE_TEST_SLOT` keep the test easy to
adapt if the lab naming changes.

## Repository layout

```text
slotlease-mvp/
├── compose.yaml
├── config.example.yaml
├── Dockerfile
├── Makefile
├── pyproject.toml
├── infra/postgres/
│   ├── init/
│   │   ├── 001-schema.sql
│   │   ├── 002-replication.sql
│   │   └── 003-agent-role.sql
│   ├── diagnostics/
│   │   ├── disk-eta.sql
│   │   └── slot-health.sql
│   └── workload/generate-batch.sql
├── scripts/generate-wal.sh
├── src/slotlease/
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── db.py
│   ├── errors.py
│   ├── models.py
│   ├── plan.py
│   ├── policy.py
│   └── units.py
└── tests/
    ├── test_policy_and_plan.py
    ├── test_cli_smoke.py
    └── test_integration_lifecycle.py
```

## Design decisions worth discussing in an interview

- **Why a file-based plan?** It creates a reviewable boundary between diagnosis
  and mutation, and can later become an approval artifact in CI or GitOps.
- **Why bind a plan to `system_identifier`?** Database names and hostnames are not
  unique enough to prevent a plan generated for one cluster reaching another.
- **Why hash the observed slot snapshot?** An inactive slot can become active or
  advance between planning and execution; stale evidence must not authorize action.
- **Why report unmanaged slots?** Operators still need visibility, while absence of
  ownership metadata must never imply permission to delete.
- **Why not use free disk as retained WAL?** Retained logical distance, physical WAL
  directory size, and filesystem capacity are related but different measurements.

## MVP boundaries and next steps

Current non-goals:

- automatic remediation or a long-running controller;
- physical-slot deletion;
- managed-service discovery and secret-manager integrations;
- Kubernetes reconciliation, RBAC, or centralized fleet history;
- claiming a lab ETA is an exact production outage prediction.

Natural next releases:

1. Prometheus metrics and structured audit events.
2. A lease registry backed by Git with ownership and expiry review.
3. Read-only fleet inventory across many PostgreSQL clusters.
4. Slack/PagerDuty notifications and approval integrations.
5. A Kubernetes Operator that preserves the same Plan -> Apply contract.

## Operational warning

Dropping a replication slot permanently removes the server-side position needed by
its consumer. The consumer may require a full resnapshot afterward. Use this MVP
only against disposable infrastructure until its policy, permissions, and recovery
procedure have been reviewed for your environment.
