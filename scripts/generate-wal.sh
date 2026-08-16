#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly COMPOSE_FILE="${PROJECT_DIR}/compose.yaml"

readonly DB_USER="${POSTGRES_USER:-slotlease_admin}"
readonly DB_NAME="${POSTGRES_DB:-slotlease}"
readonly SLOT_NAME="${SLOT_NAME:-slotlease_demo}"
readonly TARGET_WAL_MB="${TARGET_WAL_MB:-64}"
readonly HARD_STOP_WAL_MB="${HARD_STOP_WAL_MB:-96}"
readonly BATCH_ROWS="${BATCH_ROWS:-2500}"
readonly PAYLOAD_BYTES="${PAYLOAD_BYTES:-1024}"
readonly MAX_BATCHES="${MAX_BATCHES:-200}"
readonly ABSOLUTE_HARD_STOP_WAL_MB=96

compose=(docker compose --file "${COMPOSE_FILE}" --project-directory "${PROJECT_DIR}")

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

is_uint() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

for value_name in TARGET_WAL_MB HARD_STOP_WAL_MB BATCH_ROWS PAYLOAD_BYTES MAX_BATCHES; do
    value="${!value_name}"
    is_uint "${value}" || die "${value_name} must be a non-negative integer, got: ${value}"
done

[[ "${SLOT_NAME}" =~ ^[a-z0-9_]+$ ]] || die "SLOT_NAME contains unsafe characters"
(( TARGET_WAL_MB > 0 )) || die "TARGET_WAL_MB must be greater than zero"
(( HARD_STOP_WAL_MB > 0 )) || die "HARD_STOP_WAL_MB must be greater than zero"
(( TARGET_WAL_MB <= HARD_STOP_WAL_MB )) || die "TARGET_WAL_MB must not exceed HARD_STOP_WAL_MB"
(( HARD_STOP_WAL_MB <= ABSOLUTE_HARD_STOP_WAL_MB )) || \
    die "HARD_STOP_WAL_MB cannot exceed the built-in ${ABSOLUTE_HARD_STOP_WAL_MB} MiB safety limit"
(( BATCH_ROWS >= 1 && BATCH_ROWS <= 10000 )) || die "BATCH_ROWS must be between 1 and 10000"
# pgcrypto deliberately limits gen_random_bytes() calls to 1024 bytes.
(( PAYLOAD_BYTES >= 32 && PAYLOAD_BYTES <= 1024 )) || die "PAYLOAD_BYTES must be between 32 and 1024"
(( MAX_BATCHES >= 1 && MAX_BATCHES <= 1000 )) || die "MAX_BATCHES must be between 1 and 1000"

command -v docker >/dev/null 2>&1 || die "docker is not installed or is not on PATH"
"${compose[@]}" version >/dev/null 2>&1 || die "Docker Compose v2 is unavailable"

psql_exec() {
    "${compose[@]}" exec -T postgres \
        psql -X -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${DB_NAME}" "$@"
}

psql_exec -Atqc 'SELECT 1' >/dev/null || \
    die "postgres is not ready; run: docker compose -f \"${COMPOSE_FILE}\" up -d --wait postgres"

slot_state="$(psql_exec -Atqc \
    "SELECT active::text FROM pg_replication_slots WHERE slot_name = '${SLOT_NAME}'")"
[[ -n "${slot_state}" ]] || die "replication slot ${SLOT_NAME} does not exist"
[[ "${slot_state}" == "false" || "${slot_state}" == "f" ]] || \
    die "slot ${SLOT_NAME} is active; stop the consumer before generating retained WAL"

readonly BYTES_PER_MIB=1048576
readonly TARGET_BYTES=$((TARGET_WAL_MB * BYTES_PER_MIB))
readonly HARD_STOP_BYTES=$((HARD_STOP_WAL_MB * BYTES_PER_MIB))

printf 'Generating WAL for inactive slot %s\n' "${SLOT_NAME}"
printf 'Target: %s MiB; generator hard stop: %s MiB; server slot cap: 128 MiB\n' \
    "${TARGET_WAL_MB}" "${HARD_STOP_WAL_MB}"

for ((batch = 1; batch <= MAX_BATCHES; batch++)); do
    status_row="$(psql_exec -AtF '|' -c "
        SELECT
            COALESCE(pg_wal_lsn_diff(pg_current_wal_insert_lsn(), restart_lsn), 0)::bigint,
            COALESCE(safe_wal_size, -1),
            COALESCE(wal_status, 'unknown'),
            active::text
        FROM pg_replication_slots
        WHERE slot_name = '${SLOT_NAME}';
    ")"

    [[ -n "${status_row}" ]] || die "slot ${SLOT_NAME} disappeared during the run"
    IFS='|' read -r retained_bytes safe_wal_bytes wal_status active <<<"${status_row}"

    [[ "${active}" == "false" || "${active}" == "f" ]] || \
        die "slot became active during the run; refusing to produce misleading results"
    [[ "${wal_status}" != "lost" ]] || die "slot is already lost and must be recreated"

    if (( retained_bytes >= HARD_STOP_BYTES )); then
        die "generator hard stop reached at $((retained_bytes / BYTES_PER_MIB)) MiB"
    fi
    if (( retained_bytes >= TARGET_BYTES )); then
        printf 'Target reached before batch %d: %d MiB retained.\n' \
            "${batch}" "$((retained_bytes / BYTES_PER_MIB))"
        exit 0
    fi

    psql_exec -q \
        -v batch_rows="${BATCH_ROWS}" \
        -v payload_bytes="${PAYLOAD_BYTES}" \
        -f /opt/slotlease/workload/generate-batch.sql

    status_row="$(psql_exec -AtF '|' -c "
        SELECT
            COALESCE(pg_wal_lsn_diff(pg_current_wal_insert_lsn(), restart_lsn), 0)::bigint,
            COALESCE(safe_wal_size, -1),
            COALESCE(wal_status, 'unknown'),
            active::text
        FROM pg_replication_slots
        WHERE slot_name = '${SLOT_NAME}';
    ")"
    [[ -n "${status_row}" ]] || die "slot ${SLOT_NAME} disappeared during the run"
    IFS='|' read -r retained_bytes safe_wal_bytes wal_status active <<<"${status_row}"
    [[ "${active}" == "false" || "${active}" == "f" ]] || \
        die "slot became active during the run; refusing to produce misleading results"
    [[ "${wal_status}" != "lost" ]] || die "slot became lost during the run"

    printf 'batch=%03d retained=%4d MiB target=%d MiB wal_status=%s safe_remaining=%d MiB\n' \
        "${batch}" \
        "$((retained_bytes / BYTES_PER_MIB))" \
        "${TARGET_WAL_MB}" \
        "${wal_status}" \
        "$((safe_wal_bytes < 0 ? -1 : safe_wal_bytes / BYTES_PER_MIB))"

    if (( retained_bytes >= HARD_STOP_BYTES )); then
        die "generator hard stop reached at $((retained_bytes / BYTES_PER_MIB)) MiB"
    fi
    if (( retained_bytes >= TARGET_BYTES )); then
        printf '\nDone. Inspect the incident with:\n'
        printf '  docker compose -f "%s" exec postgres psql -U %s -d %s -f /opt/slotlease/sql/slot-health.sql\n' \
            "${COMPOSE_FILE}" "${DB_USER}" "${DB_NAME}"
        printf '\nStart the optional consumer to advance the slot:\n'
        printf '  docker compose -f "%s" --profile consumer up -d consumer\n' "${COMPOSE_FILE}"
        exit 0
    fi
done

die "MAX_BATCHES=${MAX_BATCHES} reached before the WAL target; increase BATCH_ROWS, not the safety limit"
