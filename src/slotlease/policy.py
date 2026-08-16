from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import SlotPolicy, SlotSnapshot, utc_datetime

TTL_EXCEEDED = "inactive_ttl_exceeded"
WAL_BUDGET_EXCEEDED = "retained_wal_exceeded"


@dataclass(frozen=True)
class Evaluation:
    snapshot: SlotSnapshot
    policy: SlotPolicy | None
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def managed(self) -> bool:
        return self.policy is not None

    @property
    def eligible_for_plan(self) -> bool:
        return bool(
            self.policy
            and self.policy.allow_drop
            and self.reasons
            and not self.blockers
        )

    @property
    def status(self) -> str:
        if not self.managed:
            return "UNMANAGED"
        if not self.reasons:
            return "HEALTHY"
        return "VIOLATION"

    def to_report_dict(self, now: datetime) -> dict[str, Any]:
        return {
            **self.snapshot.report_dict(now),
            "owner": self.policy.owner if self.policy else None,
            "status": self.status,
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "allow_drop": self.policy.allow_drop if self.policy else False,
            "eligible_for_plan": self.eligible_for_plan,
            "policy": self.policy.to_dict() if self.policy else None,
        }


def evaluate_slot(
    snapshot: SlotSnapshot,
    policy: SlotPolicy | None,
    now: datetime,
) -> Evaluation:
    """Evaluate one snapshot without side effects, making policy easy to unit test."""

    utc_datetime(now)
    if policy is None:
        return Evaluation(snapshot, None, (), ())

    reasons: list[str] = []
    inactive_for = snapshot.inactive_for(now)
    if inactive_for is not None and inactive_for >= policy.inactive_ttl:
        reasons.append(TTL_EXCEEDED)
    if snapshot.retained_wal_bytes >= policy.max_retained_wal:
        reasons.append(WAL_BUDGET_EXCEEDED)

    blockers: list[str] = []
    if snapshot.slot_type != "logical":
        blockers.append("not_logical")
    if snapshot.active:
        blockers.append("slot_active")
    if snapshot.temporary:
        blockers.append("temporary_slot")
    if snapshot.synced:
        blockers.append("synced_standby_slot")
    if snapshot.failover:
        blockers.append("failover_slot")

    return Evaluation(snapshot, policy, tuple(reasons), tuple(blockers))


def evaluate_slots(
    snapshots: list[SlotSnapshot],
    policies: dict[str, SlotPolicy] | Any,
    now: datetime,
) -> list[Evaluation]:
    return [evaluate_slot(snapshot, policies.get(snapshot.slot_name), now) for snapshot in snapshots]

