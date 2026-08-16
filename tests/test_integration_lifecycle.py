from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = os.getenv("SLOTLEASE_TEST_COMPOSE_FILE", "compose.yaml")
POSTGRES_SERVICE = os.getenv("SLOTLEASE_TEST_POSTGRES_SERVICE", "postgres")
POSTGRES_USER = os.getenv("SLOTLEASE_TEST_POSTGRES_USER", "slotlease_admin")
POSTGRES_DB = os.getenv("SLOTLEASE_TEST_POSTGRES_DB", "slotlease")
POSTGRES_PASSWORD = os.getenv(
    "SLOTLEASE_TEST_POSTGRES_PASSWORD", "slotlease_local_only"
)
AGENT_USER = os.getenv("SLOTLEASE_TEST_AGENT_USER", "slotlease_agent")
AGENT_PASSWORD = os.getenv(
    "SLOTLEASE_TEST_AGENT_PASSWORD", "slotlease_agent_local_only"
)
SLOT_NAME = os.getenv("SLOTLEASE_TEST_SLOT", "slotlease_demo")


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        rendered = " ".join(command)
        pytest.fail(
            f"Command failed ({result.returncode}): {rendered}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _docker_is_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    for command in (["docker", "info"], ["docker", "compose", "version"]):
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            return False
    return True


@dataclass(frozen=True)
class ComposeStack:
    project_name: str
    port: int
    env: dict[str, str]

    def compose(self, *args: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        return _run(
            [
                "docker",
                "compose",
                "-f",
                COMPOSE_FILE,
                "--project-name",
                self.project_name,
                *args,
            ],
            env=self.env,
            timeout=timeout,
        )

    def psql(self, sql: str) -> str:
        result = self.compose(
            "exec",
            "-T",
            POSTGRES_SERVICE,
            "psql",
            "--set",
            "ON_ERROR_STOP=1",
            "--username",
            POSTGRES_USER,
            "--dbname",
            POSTGRES_DB,
            "--tuples-only",
            "--no-align",
            "--command",
            sql,
        )
        return result.stdout.strip()

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
            f"@127.0.0.1:{self.port}/{POSTGRES_DB}"
        )

    @property
    def agent_dsn(self) -> str:
        return (
            f"postgresql://{AGENT_USER}:{AGENT_PASSWORD}"
            f"@127.0.0.1:{self.port}/{POSTGRES_DB}"
        )


@pytest.fixture(scope="module")
def compose_stack() -> ComposeStack:
    if not _docker_is_ready():
        pytest.skip("Docker Engine and Docker Compose v2 are required")

    compose_path = PROJECT_ROOT / COMPOSE_FILE
    if not compose_path.is_file():
        pytest.fail(f"Compose file not found: {compose_path}")

    port = _free_tcp_port()
    env = os.environ.copy()
    env["POSTGRES_PORT"] = str(port)
    stack = ComposeStack(
        project_name=f"slotlease_it_{uuid4().hex[:10]}", port=port, env=env
    )

    stack.compose("up", "--detach", "--wait", POSTGRES_SERVICE, timeout=120)
    try:
        yield stack
    finally:
        stack.compose("down", "--volumes", "--remove-orphans", timeout=90)


def _slotlease(config_path: Path, dsn: str, *args: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "slotlease",
        "--config",
        str(config_path),
        "--dsn",
        dsn,
        *args,
    ]


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(PROJECT_ROOT / "src"), env.get("PYTHONPATH")])
    )
    return env


@pytest.mark.integration
def test_scan_plan_apply_drops_only_the_confirmed_slot(
    compose_stack: ComposeStack, tmp_path: Path
) -> None:
    """Exercise the public CLI against an isolated, disposable PostgreSQL volume."""

    slot_exists = compose_stack.psql(
        "SELECT count(*) FROM pg_replication_slots "
        f"WHERE slot_name = '{SLOT_NAME}';"
    )
    if slot_exists == "0":
        compose_stack.psql(
            "SELECT slot_name FROM pg_create_logical_replication_slot"
            f"('{SLOT_NAME}', 'pgoutput');"
        )

    # A few MiB are enough to make the slot observable. We use a dedicated table
    # so the test does not depend on the shape of the demo workload schema.
    compose_stack.psql(
        "CREATE TABLE IF NOT EXISTS public.slotlease_it_wal ("
        "id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, payload bytea NOT NULL);"
        "INSERT INTO public.slotlease_it_wal(payload) "
        "SELECT gen_random_bytes(1024) FROM generate_series(1, 4000);"
    )
    time.sleep(1.1)

    config_path = tmp_path / "integration-policy.yaml"
    config_path.write_text(
        "\n".join(
            [
                "version: 1",
                "database:",
                f'  dsn: "{compose_stack.agent_dsn}"',
                "plan:",
                "  expires_in: 15m",
                "defaults:",
                "  inactive_ttl: 0s",
                "  max_retained_wal: 1B",
                "slots:",
                f"  {SLOT_NAME}:",
                "    owner: integration-test",
                "    inactive_ttl: 0s",
                "    max_retained_wal: 1B",
                "    allow_drop: true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cli_env = _cli_env()

    scan = _run(
        _slotlease(config_path, compose_stack.agent_dsn, "scan", "--format", "json"),
        env=cli_env,
    )
    json.loads(scan.stdout)
    assert SLOT_NAME in scan.stdout

    plan_path = tmp_path / "plan.json"
    _run(
        _slotlease(
            config_path,
            compose_stack.agent_dsn,
            "plan",
            "--output",
            str(plan_path),
            "--expires-in",
            "15m",
        ),
        env=cli_env,
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert len(plan["actions"]) == 1
    assert any(
        action["action"] == "drop_replication_slot"
        and action["slot_name"] == SLOT_NAME
        for action in plan["actions"]
    )

    denied = subprocess.run(
        _slotlease(
            config_path,
            compose_stack.agent_dsn,
            "apply",
            "--plan",
            str(plan_path),
            "--confirm",
            "wrong-plan-id",
        ),
        cwd=PROJECT_ROOT,
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert denied.returncode != 0
    assert compose_stack.psql(
        "SELECT count(*) FROM pg_replication_slots "
        f"WHERE slot_name = '{SLOT_NAME}';"
    ) == "1"

    _run(
        _slotlease(
            config_path,
            compose_stack.agent_dsn,
            "apply",
            "--plan",
            str(plan_path),
            "--confirm",
            plan["plan_id"],
        ),
        env=cli_env,
    )

    remaining = compose_stack.psql(
        "SELECT count(*) FROM pg_replication_slots "
        f"WHERE slot_name = '{SLOT_NAME}';"
    )
    assert remaining == "0"
