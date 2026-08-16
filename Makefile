PYTHON ?= python3
COMPOSE ?= docker compose
CONFIG ?= config.example.yaml
PLAN ?= .slotlease/plan.json
POSTGRES_PORT ?= 55432
DSN ?= postgresql://slotlease_agent:slotlease_agent_local_only@127.0.0.1:$(POSTGRES_PORT)/slotlease

TARGET_WAL_MB ?= 64
BATCH_ROWS ?= 5000
PAYLOAD_BYTES ?= 1024
HARD_STOP_WAL_MB ?= 96

SLOTLEASE = $(PYTHON) -m slotlease --config $(CONFIG) --dsn "$(DSN)"

.PHONY: install up down logs wal scan scan-json plan apply test test-integration docker-build

install:
	$(PYTHON) -m pip install -e ".[dev]"

up:
	POSTGRES_PORT=$(POSTGRES_PORT) $(COMPOSE) up -d --wait postgres

down:
	POSTGRES_PORT=$(POSTGRES_PORT) $(COMPOSE) down --volumes --remove-orphans

logs:
	POSTGRES_PORT=$(POSTGRES_PORT) $(COMPOSE) logs -f postgres

wal:
	TARGET_WAL_MB=$(TARGET_WAL_MB) \
	BATCH_ROWS=$(BATCH_ROWS) \
	PAYLOAD_BYTES=$(PAYLOAD_BYTES) \
	HARD_STOP_WAL_MB=$(HARD_STOP_WAL_MB) \
	bash scripts/generate-wal.sh

scan:
	$(SLOTLEASE) scan --format table

scan-json:
	$(SLOTLEASE) scan --format json

plan:
	mkdir -p "$(dir $(PLAN))"
	$(SLOTLEASE) plan --output "$(PLAN)" --expires-in 15m

# Apply intentionally requires the caller to copy PLAN_ID from the generated
# JSON. A plain `make apply` can therefore never delete a slot by accident.
apply:
	@test -n "$(PLAN_ID)" || (echo "PLAN_ID is required: make apply PLAN_ID=<id>" >&2; exit 2)
	$(SLOTLEASE) apply --plan "$(PLAN)" --confirm "$(PLAN_ID)"

test:
	$(PYTHON) -m pytest -m "not integration" -q

test-integration:
	$(PYTHON) -m pytest tests/test_integration_lifecycle.py -m integration -vv

docker-build:
	docker build --tag slotlease:dev .
