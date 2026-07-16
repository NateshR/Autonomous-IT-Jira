"""Shared constants. Single source of truth for values used across modules."""

from __future__ import annotations

# Where the policy knowledge base lives (used by every entry point).
POLICY_DIR = "policies"


class Status:
    """JIRA ticket statuses the agent transitions to. Constants, not magic
    strings, so a typo is an AttributeError rather than a silently-new status."""

    OPEN = "Open"
    CLOSED = "Closed"
    DEFERRED = "Deferred"
    ESCALATED = "Escalated"
    WAITING_CUSTOMER = "Waiting for Customer"
    WAITING_APPROVAL = "Waiting for Approval"
