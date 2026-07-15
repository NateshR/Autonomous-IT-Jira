"""Tool registry: the single place each tool's safety contract lives.

Each ``Tool`` declares:
  - ``risk``     : GREEN / GREEN* / AMBER / RED  (the floor the guard enforces)
  - ``requires`` : names of preconditions the guard must pass before firing
  - ``idem``     : idempotency-key recipe (per NOTES §3), or None for read-only
  - ``verify``   : re-reads state after the call to confirm the effect actually
                   happened (catches the silent no-op failure mode); None means
                   "trust a non-error status"
  - ``fn``       : the bound mock endpoint

Onboarding an 11th tool = add one row here (and, only if it needs a genuinely
new kind of check, one function in guard.PRECHECKS). The guard's control flow
never changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from mock.systems import MockSystems
from mock.ticket_store import Ticket

RiskClass = str  # "GREEN" | "GREEN*" | "AMBER" | "RED"

# A precondition/idempotency/verify callable signature helper (documentation only).
IdemFn = Callable[[Ticket, dict], str]


@dataclass
class Tool:
    name: str
    risk: RiskClass
    fn: Callable[..., dict]
    requires: list[str] = field(default_factory=list)
    idem: IdemFn | None = None
    verify: Callable[[dict, dict, MockSystems], bool] | None = None
    read_only: bool = False


# --------------------------------------------------------------- idem recipes
# Recipes mirror the "Idempotency key" column of the tool catalog (NOTES §3).

def _unlock_key(s: MockSystems) -> IdemFn:
    def key(t: Ticket, a: dict) -> str:               # account + lock epoch
        acct = s.accounts.get(a["user"])
        epoch = acct.lock_epoch if acct else 0
        return f"{a['user']}:{epoch}"
    return key


def _reset_key(s: MockSystems) -> IdemFn:
    def key(t: Ticket, a: dict) -> str:               # user + calendar day
        return f"{a['user']}:{s.today}"
    return key


def _revoke_key(t: Ticket, a: dict) -> str:           # user + incident
    return f"{a['user']}:{a.get('incident', t.id)}"


def _request_key(t: Ticket, a: dict) -> str:          # user + item + day (day via ticket)
    return f"{t.reporter}:{a.get('item')}:{t.id}"


def _admin_key(t: Ticket, a: dict) -> str:            # user + session (ticket as session)
    return f"{a['user']}:{t.id}"


def _case_key(t: Ticket, a: dict) -> str:             # asset + type
    fields = a.get("fields", {})
    return f"{fields.get('asset', t.id)}:{a.get('case_type')}"


def _approval_key(t: Ticket, a: dict) -> str:         # request hash
    return f"apr:{hash((a.get('action'), tuple(a.get('approvers', []))))}"


def _incident_key(t: Ticket, a: dict) -> str:         # ticket id
    return f"{t.id}"


# ------------------------------------------------------------------- verifies

def _v_unlocked(a: dict, resp: dict, s: MockSystems) -> bool:
    return not s.is_locked(a["user"])


def _v_reset_sent(a: dict, resp: dict, s: MockSystems) -> bool:
    return any(e["user"] == a["user"] for e in s.reset_emails)


def _v_sessions_revoked(a: dict, resp: dict, s: MockSystems) -> bool:
    acct = s.accounts.get(a["user"])
    return bool(acct and acct.active_sessions == 0)


def _v_request_filed(a: dict, resp: dict, s: MockSystems) -> bool:
    return s.request_exists(resp.get("request_id", ""))


def _v_admin_granted(a: dict, resp: dict, s: MockSystems) -> bool:
    return resp.get("status") == "granted" and s.grant_exists(resp.get("grant_id", ""))


def _v_case_registered(a: dict, resp: dict, s: MockSystems) -> bool:
    return s.case_registered(resp.get("case_id", ""))


def _v_approval_routed(a: dict, resp: dict, s: MockSystems) -> bool:
    return resp.get("status") == "PENDING" and bool(resp.get("approval_id"))


def _v_incident_open(a: dict, resp: dict, s: MockSystems) -> bool:
    return s.incident_exists(resp.get("incident_id", ""))


# --------------------------------------------------------------- the registry

def build_tool_registry(s: MockSystems) -> dict[str, Tool]:
    """Bind the catalog to a concrete mock-systems instance."""
    return {
        # --- read-only (identity / risk / approval status) ------------------
        "directory.lookup_user": Tool(
            "directory.lookup_user", "GREEN", read_only=True,
            fn=lambda user, **_: s.lookup_user(user)),
        "directory.verify_manager": Tool(
            "directory.verify_manager", "GREEN", read_only=True,
            fn=lambda manager, subordinate, **_: s.verify_manager(manager, subordinate)),
        "okta.risk_signals": Tool(
            "okta.risk_signals", "GREEN", read_only=True,
            fn=lambda user, **_: s.okta_risk_signals(user)),
        "iam.get_approval": Tool(
            "iam.get_approval", "GREEN", read_only=True,
            fn=lambda approval_id, **_: s.iam_get_approval(approval_id)),

        # --- GREEN actions --------------------------------------------------
        "okta.unlock_account": Tool(
            "okta.unlock_account", "GREEN*",
            requires=["authorized", "risk_signals_clear"],
            idem=_unlock_key(s), verify=_v_unlocked,
            fn=s.okta_unlock_account),
        "okta.send_password_reset": Tool(
            "okta.send_password_reset", "GREEN",
            requires=["authorized"],
            idem=_reset_key(s), verify=_v_reset_sent,
            fn=s.okta_send_password_reset),
        "okta.revoke_sessions": Tool(
            "okta.revoke_sessions", "GREEN",
            idem=_revoke_key, verify=_v_sessions_revoked,
            fn=s.okta_revoke_sessions),
        "okta.force_password_reset": Tool(
            "okta.force_password_reset", "GREEN",
            idem=_revoke_key,   # user + incident, same recipe
            fn=s.okta_force_password_reset),
        "servicenow.create_request": Tool(
            "servicenow.create_request", "GREEN",
            idem=_request_key, verify=_v_request_filed,
            fn=s.servicenow_create_request),
        "endpoint.grant_admin": Tool(
            "endpoint.grant_admin", "GREEN",
            requires=["authorized", "minutes_le_60"],
            idem=_admin_key, verify=_v_admin_granted,
            fn=s.endpoint_grant_admin),
        "assetmgmt.create_case": Tool(
            "assetmgmt.create_case", "GREEN",
            idem=_case_key, verify=_v_case_registered,
            fn=s.assetmgmt_create_case),
        "iam.create_approval": Tool(
            "iam.create_approval", "GREEN",
            idem=_approval_key, verify=_v_approval_routed,
            fn=s.iam_create_approval),

        # --- AMBER (never inline; only draftable into create_approval) ------
        "iam.grant_access": Tool(
            "iam.grant_access", "AMBER", fn=s.iam_grant_access),
        "okta.disable_mfa": Tool(
            "okta.disable_mfa", "AMBER", fn=s.okta_disable_mfa),

        # --- RED (escalation only) ------------------------------------------
        "soc.open_incident": Tool(
            "soc.open_incident", "RED",
            idem=_incident_key, verify=_v_incident_open,
            fn=s.soc_open_incident),
        "soc.page_oncall": Tool(
            "soc.page_oncall", "RED",
            idem=_incident_key,
            fn=s.soc_page_oncall),
    }
