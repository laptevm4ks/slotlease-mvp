# SlotLease MVP: практический roadmap на 2–4 недели

Цель MVP: обнаружить заброшенный logical replication slot, объяснить риск в
байтах/времени и удалить слот только через двухфазный процесс `Plan → Apply`.
Все команды ниже выполняются из корня репозитория.

## Этап 1. Локальный PostgreSQL-стенд и управляемая WAL-авария (дни 1–3)

### Результат этапа

- PostgreSQL 18 запускается в Docker с `wal_level=logical`.
- Init SQL создаёт таблицу, publication и неактивный logical slot.
- Генератор создаёт около 64 MiB удерживаемого WAL и останавливается раньше
  опасного размера.

Ключевые параметры уже находятся в `compose.yaml`:

```yaml
services:
  postgres:
    image: postgres:18-bookworm
    command:
      - postgres
      - -c
      - wal_level=logical
      - -c
      - max_replication_slots=10
      - -c
      - max_wal_senders=10
      - -c
      - max_slot_wal_keep_size=128MB
    ports:
      - "${POSTGRES_PORT:-55432}:5432"
    volumes:
      - postgres_data:/var/lib/postgresql
      - ./infra/postgres/init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U \"$${POSTGRES_USER}\" -d \"$${POSTGRES_DB}\""]
```

Почему так:

- `wal_level=logical` обязателен для logical decoding.
- `max_slot_wal_keep_size=128MB` ограничивает лабораторный ущерб на стороне БД.
- healthcheck нужен, потому что порядок запуска контейнеров не означает готовность
  PostgreSQL принимать соединения.
- Для PostgreSQL 18 volume монтируется в `/var/lib/postgresql`, а не в старый
  leaf-каталог.

Init SQL:

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE public.demo_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    payload bytea NOT NULL
);

CREATE PUBLICATION slotlease_publication FOR TABLE public.demo_events;

SELECT slot_name, lsn
FROM pg_create_logical_replication_slot('slotlease_demo', 'pgoutput');
```

Отдельный init-файл создаёт не superuser для CLI:

```sql
CREATE ROLE slotlease_agent
    WITH LOGIN REPLICATION PASSWORD 'slotlease_agent_local_only';
GRANT pg_monitor TO slotlease_agent;
GRANT CONNECT ON DATABASE slotlease TO slotlease_agent;
```

`REPLICATION` нужен для drop slot, а `pg_monitor` — для системной диагностики.
Генератор тестовой нагрузки по-прежнему работает через локального admin.

`gen_random_bytes()` выбран специально: одинаковые строки могут хорошо сжиматься
TOAST и давать плохо предсказуемый объём WAL.
Сам batch лежит отдельно в `infra/postgres/workload/generate-batch.sql`, а Bash
отвечает только за циклы, guardrails и измерение результата.

Запуск:

```bash
docker compose up -d --wait postgres
docker compose ps
```

Сначала можно показать исправного consumer:

```bash
docker compose --profile consumer up -d consumer
docker compose logs -f consumer
docker compose --profile consumer stop consumer
```

После остановки consumer слот перестаёт продвигать `restart_lsn`. Создаём
контролируемый инцидент:

```bash
TARGET_WAL_MB=64 bash scripts/generate-wal.sh
```

Скрипт отказывается работать с активным слотом, имеет hard stop 96 MiB, а сервер
имеет отдельный cap 128 MiB. Реально заполнять volume на 100% для MVP не нужно:
это опасно и хуже воспроизводится.

Важно: init SQL выполняется только на пустом volume. Для чистого повторения стенда:

```bash
docker compose down --volumes --remove-orphans
docker compose up -d --wait postgres
```

### Definition of Done

`pg_replication_slots.active = false`, после генератора retained WAL больше 64 MiB,
а Docker host сохраняет свободное место.

## Этап 2. SQL-диагностика и ETA (дни 4–5)

### Результат этапа

Один отчёт отвечает на четыре вопроса: кто владеет слотом, активен ли consumer,
сколько WAL удерживается и сколько времени осталось при текущей скорости записи.

Основной запрос:

```sql
SELECT
    slot_name,
    active,
    active_pid,
    inactive_since,
    restart_lsn,
    confirmed_flush_lsn,
    pg_wal_lsn_diff(
        pg_current_wal_insert_lsn(), restart_lsn
    )::bigint AS retained_wal_bytes,
    pg_wal_lsn_diff(
        pg_current_wal_insert_lsn(), confirmed_flush_lsn
    )::bigint AS consumer_lag_bytes,
    wal_status,
    safe_wal_size,
    invalidation_reason
FROM pg_replication_slots
WHERE slot_type = 'logical'
ORDER BY slot_name;
```

- `restart_lsn` показывает старейшую позицию, которая всё ещё нужна слоту.
- `confirmed_flush_lsn` показывает, что consumer уже подтвердил.
- Это логическая LSN-дистанция, а не физический размер каталога `pg_wal`.

Связь с процессом consumer:

```sql
SELECT
    s.slot_name,
    a.pid,
    a.usename,
    a.application_name,
    a.client_addr,
    a.state,
    a.wait_event_type,
    a.wait_event,
    clock_timestamp() - a.backend_start AS connected_for
FROM pg_replication_slots AS s
JOIN pg_stat_activity AS a ON a.pid = s.active_pid;
```

Недавнюю WAL-скорость лучше считать по двум sample, а не по среднему с момента
перезапуска статистики:

```sql
CREATE TEMP TABLE wal_sample AS
SELECT clock_timestamp() AS sampled_at, wal_bytes
FROM pg_stat_wal;

SELECT pg_sleep(10);

SELECT
    (w.wal_bytes - s.wal_bytes)
      / NULLIF(EXTRACT(epoch FROM clock_timestamp() - s.sampled_at), 0)
      AS recent_wal_bytes_per_second
FROM pg_stat_wal AS w
CROSS JOIN wal_sample AS s;
```

Формулы:

```text
ETA до потери slot = safe_wal_size / recent_wal_bytes_per_second
ETA до заполнения FS = (filesystem_free_bytes - safety_reserve) / recent_wal_bytes_per_second
```

PostgreSQL надёжно знает `safe_wal_size`, но не универсальный free space volume.
Поэтому filesystem bytes берём с уровня ОС:

```bash
docker compose exec -T postgres sh -c 'df -B1 "$PGDATA"'
```

Готовый `disk-eta.sql` объединяет free bytes с 10-секундным delta-sample:

```bash
FREE_BYTES="$(docker compose exec -T postgres sh -c \
  'df -B1 --output=avail "$PGDATA" | tail -1 | tr -d " "')"
docker compose exec -T postgres \
  psql -U slotlease_admin -d slotlease \
  -v filesystem_free_bytes="${FREE_BYTES}" \
  -v safety_reserve_bytes=67108864 \
  -f /opt/slotlease/sql/disk-eta.sql
```

Готовый объединённый отчёт:

```bash
docker compose exec -T postgres \
  psql -U slotlease_admin -d slotlease \
  -f /opt/slotlease/sql/slot-health.sql
```

### Definition of Done

Вы можете устно объяснить разницу между retained WAL, consumer lag, `safe_wal_size`,
физическим размером `pg_wal` и свободным местом filesystem.

## Этап 3. Python CLI и безопасный Plan → Apply (дни 6–10)

### Результат этапа

```text
src/slotlease/
├── cli.py       # argparse и команды scan/plan/apply
├── config.py    # YAML, DSN precedence, валидация
├── db.py        # только parameterized SQL
├── models.py    # immutable snapshots/policies
├── policy.py    # pure evaluation без I/O
├── plan.py      # JSON plan, expiry, digest, revalidation
└── units.py      # 15m, 64MiB
```

Политика является явным allow-list:

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

Логика eligibility:

```python
violation = (
    inactive_for >= policy.inactive_ttl
    or retained_wal_bytes >= policy.max_retained_wal
)

eligible = all([
    policy.allow_drop,
    violation,
    slot.slot_type == "logical",
    not slot.active,
    not slot.temporary,
    not slot.synced,
    not slot.failover,
])
```

Последовательность команд:

```bash
python -m slotlease --config config.example.yaml scan --format table

python -m slotlease --config config.example.yaml \
  plan --output .slotlease/plan.json --expires-in 15m

python -m slotlease --config config.example.yaml \
  apply --plan .slotlease/plan.json --confirm '<PLAN_ID>'
```

Почему две команды, а не `--delete`:

1. `scan` всегда read-only и показывает даже unmanaged slots.
2. `plan` фиксирует `system_identifier`, database, policy, expiry и snapshot LSN.
3. Оператор читает JSON и явно копирует случайный `plan_id`.
4. `apply` проверяет digest, cluster identity, неизменность policy и дважды читает
   slot перед `pg_drop_replication_slot()`.
5. PostgreSQL дополнительно сам отклоняет удаление слота, если consumer успел
   подключиться.

Удаление вызывается параметризованно:

```python
conn.execute("SELECT pg_drop_replication_slot(%s)", (slot_name,))
```

SHA-256 digest здесь защищает от случайного редактирования, но не является
криптографической подписью доверенного approver. Для production-вектора добавьте
HMAC/KMS signature или approval service.

У PostgreSQL нет per-slot ACL: роль с `REPLICATION` технически может удалить любой
slot. В следующей версии разделите read-only identity для `scan` и краткоживущий
credential, выдаваемый только approved `apply` job.

### Definition of Done

Неверный plan ID, истёкший plan, другая БД, активировавшийся consumer, изменившийся
LSN или снятый `allow_drop` приводят к отказу до удаления.

## Этап 4. Docker-образ и тесты (дни 11–14)

### Результат этапа

Сборка non-root multi-stage image:

```bash
docker build -t slotlease:dev .

docker run --rm \
  --network slotlease-mvp_default \
  --mount type=bind,src="$(pwd)/config.example.yaml",dst=/config.yaml,readonly \
  -e SLOTLEASE_DSN=postgresql://slotlease_agent:slotlease_agent_local_only@postgres:5432/slotlease \
  slotlease:dev --config /config.yaml scan --format table
```

Быстрые тесты проверяют parser, policy и защиту plan:

```bash
python -m pip install -e ".[dev]"
python -m pytest -m "not integration" -q
```

Интеграционный тест поднимает уникальный Compose project, создаёт WAL, вызывает
публичный CLI, сначала проверяет отказ с неправильным ID, потом подтверждённое
удаление и в `finally` уничтожает только свой disposable volume:

```bash
python -m pytest tests/test_integration_lifecycle.py -m integration -vv
```

Критические assertions:

```python
assert denied.returncode != 0
assert slot_count() == "1"

apply(plan_id=plan["plan_id"])
assert slot_count() == "0"
```

### Definition of Done

Unit suite зелёный; integration suite проходит два раза подряд; образ работает без
root, не содержит policy/DSN и без аргументов выводит help вместо mutation.

## Этап 5. GitHub-портфолио и следующий релиз (дни 15–18)

### Результат этапа

README должен вести читателя в таком порядке:

1. Одно предложение о проблеме и safety promise.
2. Схема `PostgreSQL → Scan → Plan JSON → Apply`.
3. Safety matrix: unmanaged/active/temporary/synced/expired.
4. Quick start, который занимает не больше пяти минут.
5. Реальный CLI-output до и после WAL incident.
6. Объяснение LSN и честные ограничения ETA.
7. Команды unit/integration tests.
8. Design decisions для собеседования.
9. Non-goals и roadmap, чтобы MVP не выглядел недоделанным монолитом.

Минимальный demo-сценарий для GIF/видео:

```text
scan: HEALTHY
stop consumer + generate WAL
scan: VIOLATION retained_wal_exceeded
plan: 1 action, expires in 15m
apply with wrong ID: rejected
apply with correct ID: slot removed
SQL: count(*) = 0
```

Что вынести в GitHub Actions после MVP:

```yaml
- run: python -m pip install -e ".[dev]"
- run: python -m pytest -m "not integration" -q
- run: python -m pytest -m integration -vv
```

Следующий технически сильный vector: Prometheus metrics + append-only audit events,
затем fleet inventory, Git-based ownership registry и Kubernetes Operator. Не
начинайте с Operator: сначала докажите корректность slot lifecycle на одной БД.

### Definition of Done

Новый разработчик воспроизводит incident по README без устных подсказок; в резюме
проект можно описать одной измеримой фразой: «реализовал safety-first lifecycle
manager logical replication slots с LSN diagnostics, short-lived plans и реальным
Compose integration test».
