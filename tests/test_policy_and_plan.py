from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from slotlease.errors import ApplyPreconditionError, PlanError
from slotlease.models import ClusterIdentity, SlotPolicy, SlotSnapshot
from slotlease.plan import build_plan, validate_plan, verify_confirmation, verify_snapshot
from slotlease.policy import evaluate_slot


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def snapshot(**changes: object) -> SlotSnapshot:
    base = SlotSnapshot(
        slot_name="slotlease_demo",
        plugin="pgoutput",
        slot_type="logical",
        database="slotlease",
        temporary=False,
        active=False,
        active_pid=None,
        xmin=None,
        catalog_xmin="740",
        restart_lsn="0/1000000",
        confirmed_flush_lsn="0/1000000",
        inactive_since=NOW - timedelta(minutes=10),
        retained_wal_bytes=80 * 1024 * 1024,
        conflicting=False,
        invalidation_reason=None,
        failover=False,
        synced=False,
    )
    return replace(base, **changes)


def policy(**changes: object) -> SlotPolicy:
    base = SlotPolicy(
        owner="data-platform",
        inactive_ttl=timedelta(minutes=5),
        max_retained_wal=64 * 1024 * 1024,
        allow_drop=True,
    )
    return replace(base, **changes)


def test_active_slot_is_reported_but_never_planned() -> None:
    evaluation = evaluate_slot(snapshot(active=True, active_pid=42), policy(), NOW)

    assert "slot_active" in evaluation.blockers
    assert evaluation.eligible_for_plan is False


def test_plan_is_short_lived_cluster_bound_and_tamper_evident() -> None:
    evaluation = evaluate_slot(snapshot(), policy(), NOW)
    plan = build_plan(
        [evaluation],
        ClusterIdentity(system_identifier="123456", database="slotlease"),
        now=NOW,
        expires_in=timedelta(minutes=15),
        plan_id="review-me",
    )

    assert [action["slot_name"] for action in plan["actions"]] == ["slotlease_demo"]
    validate_plan(plan, now=NOW + timedelta(minutes=1))
    verify_confirmation(plan, "review-me")

    changed = {**plan, "expires_at": "2099-01-01T00:00:00Z"}
    with pytest.raises(PlanError, match="integrity"):
        validate_plan(changed, now=NOW)


def test_apply_rejects_wrong_confirmation_and_changed_slot() -> None:
    evaluation = evaluate_slot(snapshot(), policy(), NOW)
    plan = build_plan(
        [evaluation],
        ClusterIdentity(system_identifier="123456", database="slotlease"),
        now=NOW,
        expires_in=timedelta(minutes=15),
        plan_id="approved-id",
    )
    action = plan["actions"][0]

    with pytest.raises(ApplyPreconditionError, match="confirmation"):
        verify_confirmation(plan, "wrong-id")
    with pytest.raises(ApplyPreconditionError, match="changed after planning"):
        verify_snapshot(action, snapshot(confirmed_flush_lsn="0/2000000"))
    with pytest.raises(ApplyPreconditionError, match="active"):
        verify_snapshot(action, snapshot(active=True, active_pid=99))
