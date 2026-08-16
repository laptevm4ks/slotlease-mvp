from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import Settings
from .errors import ApplyPreconditionError, PlanError
from .models import (
    ClusterIdentity,
    SlotSnapshot,
    format_timestamp,
    parse_timestamp,
    utc_datetime,
)
from .policy import Evaluation

PLAN_SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_plan(
    evaluations: Iterable[Evaluation],
    cluster: ClusterIdentity,
    *,
    now: datetime,
    expires_in: timedelta,
    plan_id: str | None = None,
) -> dict[str, Any]:
    created_at = utc_datetime(now)
    if expires_in.total_seconds() <= 0:
        raise PlanError("plan expiry must be greater than zero")

    actions: list[dict[str, Any]] = []
    for evaluation in evaluations:
        if not evaluation.eligible_for_plan:
            continue
        snapshot = evaluation.snapshot
        policy = evaluation.policy
        assert policy is not None
        actions.append(
            {
                "action": "drop_replication_slot",
                "slot_name": snapshot.slot_name,
                "reasons": list(evaluation.reasons),
                "policy": policy.to_dict(),
                "observed": snapshot.report_dict(created_at),
                "snapshot_hash": sha256_json(snapshot.safety_dict()),
            }
        )

    body: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_id": plan_id or str(uuid.uuid4()),
        "created_at": format_timestamp(created_at),
        "expires_at": format_timestamp(created_at + expires_in),
        "cluster": cluster.to_dict(),
        "actions": sorted(actions, key=lambda action: action["slot_name"]),
    }
    return with_integrity(body)


def with_integrity(body: Mapping[str, Any]) -> dict[str, Any]:
    if "integrity" in body:
        raise PlanError("integrity is generated and must not be supplied in plan body")
    result = dict(body)
    result["integrity"] = {"algorithm": "sha256", "digest": sha256_json(result)}
    return result


def verify_integrity(plan: Mapping[str, Any]) -> None:
    integrity = plan.get("integrity")
    if not isinstance(integrity, Mapping) or integrity.get("algorithm") != "sha256":
        raise PlanError("plan has no supported integrity block")
    digest = integrity.get("digest")
    if not isinstance(digest, str):
        raise PlanError("plan integrity digest is missing")
    unsigned = {key: value for key, value in plan.items() if key != "integrity"}
    expected = sha256_json(unsigned)
    if not hmac.compare_digest(digest, expected):
        raise PlanError("plan integrity check failed; generate a new plan")


def validate_plan(plan: Mapping[str, Any], *, now: datetime) -> None:
    verify_integrity(plan)
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise PlanError("unsupported plan schema version")
    if not isinstance(plan.get("plan_id"), str) or not plan["plan_id"]:
        raise PlanError("plan_id is missing")
    if not isinstance(plan.get("cluster"), Mapping):
        raise PlanError("cluster identity is missing")
    try:
        ClusterIdentity.from_mapping(plan["cluster"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError("cluster identity is incomplete or invalid") from exc
    actions = plan.get("actions")
    if not isinstance(actions, list):
        raise PlanError("actions must be a JSON array")

    try:
        created_at = parse_timestamp(str(plan["created_at"]))
        expires_at = parse_timestamp(str(plan["expires_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError("plan timestamps are missing or invalid") from exc
    current_time = utc_datetime(now)
    if expires_at <= created_at:
        raise PlanError("plan expiry must be later than creation time")
    if current_time > expires_at:
        raise PlanError(f"plan expired at {format_timestamp(expires_at)}; generate a new plan")
    if created_at > current_time + timedelta(minutes=5):
        raise PlanError("plan creation time is unexpectedly in the future")

    seen: set[str] = set()
    for action in actions:
        if not isinstance(action, Mapping) or action.get("action") != "drop_replication_slot":
            raise PlanError("plan contains an unsupported action")
        slot_name = action.get("slot_name")
        if not isinstance(slot_name, str) or not slot_name:
            raise PlanError("plan action has no slot_name")
        if slot_name in seen:
            raise PlanError(f"plan contains duplicate action for slot {slot_name}")
        seen.add(slot_name)
        if not isinstance(action.get("snapshot_hash"), str):
            raise PlanError(f"plan action for {slot_name} has no snapshot_hash")


def load_plan(path: str | Path, *, now: datetime) -> dict[str, Any]:
    plan_path = Path(path)
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError(f"cannot read plan {plan_path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise PlanError(f"invalid JSON plan {plan_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError("plan root must be a JSON object")
    validate_plan(value, now=now)
    return value


def write_plan(path: str | Path, plan: Mapping[str, Any]) -> None:
    """Atomically replace a plan so apply never reads a half-written JSON file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def verify_confirmation(plan: Mapping[str, Any], confirmation: str) -> None:
    expected = str(plan["plan_id"])
    if not hmac.compare_digest(confirmation, expected):
        raise ApplyPreconditionError(
            "confirmation does not match plan_id; copy the exact Plan ID from the plan output"
        )


def verify_cluster(plan: Mapping[str, Any], current: ClusterIdentity) -> None:
    planned = ClusterIdentity.from_mapping(plan["cluster"])
    if planned != current:
        raise ApplyPreconditionError(
            "connected PostgreSQL cluster/database differs from the one recorded in the plan"
        )


def verify_policy(action: Mapping[str, Any], settings: Settings) -> None:
    slot_name = str(action["slot_name"])
    current = settings.policy_for(slot_name)
    if current is None:
        raise ApplyPreconditionError(f"slot {slot_name} is no longer managed by the config")
    if not current.allow_drop:
        raise ApplyPreconditionError(f"slot {slot_name} no longer has allow_drop: true")
    if action.get("policy") != current.to_dict():
        raise ApplyPreconditionError(
            f"policy for slot {slot_name} changed after planning; generate a new plan"
        )


def verify_snapshot(action: Mapping[str, Any], current: SlotSnapshot | None) -> None:
    slot_name = str(action["slot_name"])
    if current is None:
        raise ApplyPreconditionError(f"slot {slot_name} no longer exists")
    if current.slot_type != "logical":
        raise ApplyPreconditionError(f"slot {slot_name} is not logical")
    if current.active:
        raise ApplyPreconditionError(f"slot {slot_name} is active; refusing to drop")
    if current.temporary:
        raise ApplyPreconditionError(f"slot {slot_name} is temporary; refusing to drop")
    if current.synced:
        raise ApplyPreconditionError(f"slot {slot_name} is a synced standby slot; refusing to drop")
    if current.failover:
        raise ApplyPreconditionError(f"slot {slot_name} is failover-enabled; refusing to drop")

    current_hash = sha256_json(current.safety_dict())
    if not hmac.compare_digest(str(action["snapshot_hash"]), current_hash):
        raise ApplyPreconditionError(
            f"slot {slot_name} changed after planning; generate and approve a new plan"
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
