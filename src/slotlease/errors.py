from __future__ import annotations


class SlotLeaseError(Exception):
    """Base class for expected, user-facing SlotLease errors."""


class ConfigError(SlotLeaseError):
    """Raised when the YAML configuration is missing or unsafe."""


class PlanError(SlotLeaseError):
    """Raised when a plan is malformed, expired, or has failed integrity checks."""


class ApplyPreconditionError(SlotLeaseError):
    """Raised when reality no longer matches the approved plan."""

