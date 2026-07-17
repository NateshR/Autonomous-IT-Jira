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
    # If True and the model omits `user`, the guard defaults it to the ticket
    # reporter. Safe: these tools act on the requester's own account, so the
    # target can only ever be the requester (never someone else).
    self_target: bool = False
    # Display-only: used to render the tool catalog into the decider prompt, so
    # the model's menu is generated from this registry rather than hand-copied
    # (one source of truth - onboarding tool #11 really is one row).
    signature: str = ""     # e.g. "user, minutes"
    hint: str = ""          # e.g. "minutes <= 60"


# --------------------------------------------------------------- idem recipes
# Recipes mirror the "Idempotency key" column of the tool catalog (NOTES §3).

def _unlock_key(s: MockSystems) -> IdemFn:
    def key(t: Ticket, a: dict) -> str:               # account + lock epoch
        acct = s.accounts.get(a.get("user"))
        epoch = acct.lock_epoch if acct else 0
        return f"{a.get('user')}:{epoch}"
    return key


def _reset_key(s: MockSystems) -> IdemFn:
    def key(t: Ticket, a: dict) -> str:               # user + calendar day
        return f"{a.get('user')}:{s.today}"
    return key


def _revoke_key(t: Ticket, a: dict) -> str:           # user + incident
    return f"{a.get('user')}:{a.get('incident', t.id)}"


def _request_key(s: MockSystems) -> IdemFn:           # user + item + day
    def key(t: Ticket, a: dict) -> str:
        return f"{t.reporter}:{a.get('item')}:{s.today}"
    return key


def _admin_key(t: Ticket, a: dict) -> str:            # user + session (ticket as session)
    return f"{a.get('user')}:{t.id}"


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


def _v_paged(a: dict, resp: dict, s: MockSystems) -> bool:
    return s.was_paged(a["team"])


def _v_reset_forced(a: dict, resp: dict, s: MockSystems) -> bool:
    return s.reset_was_forced(a["user"])


# --------------------------------------------------------------- the registry

def build_tool_registry(s: MockSystems) -> dict[str, Tool]:
    """Bind the catalog to a concrete mock-systems instance."""
    return {
        # --- read-only (identity / risk / approval status) ------------------
        # Tolerant of missing/aliased args so a read check never crashes a run.
        "directory.lookup_user": Tool(
            "directory.lookup_user", "GREEN", read_only=True, self_target=True,
            signature="user", hint="read",
            fn=lambda **kw: s.lookup_user(kw.get("user"))),
        "directory.verify_manager": Tool(
            "directory.verify_manager", "GREEN", read_only=True,
            signature="manager, subordinate", hint="read - verifies a claimed manager",
            fn=lambda **kw: s.verify_manager(kw.get("manager"), kw.get("subordinate"))),
        "okta.risk_signals": Tool(
            "okta.risk_signals", "GREEN", read_only=True, self_target=True,
            signature="user", hint="read - CHECK BEFORE ANY UNLOCK",
            fn=lambda **kw: s.okta_risk_signals(kw.get("user"))),
        "iam.get_approval": Tool(
            "iam.get_approval", "GREEN", read_only=True,
            signature="approval_id", hint="read - rebuts an 'already approved' claim",
            fn=lambda **kw: s.iam_get_approval(kw.get("approval_id"))),

        # --- GREEN actions --------------------------------------------------
        "okta.unlock_account": Tool(
            "okta.unlock_account", "GREEN*",
            signature="user", hint="only if risk signals are clear",
            requires=["authorized", "risk_signals_clear", "no_fan_out"], self_target=True,
            idem=_unlock_key(s), verify=_v_unlocked,
            fn=s.okta_unlock_account),
        "okta.send_password_reset": Tool(
            "okta.send_password_reset", "GREEN",
            signature="user", hint="verified owner only",
            requires=["authorized", "no_fan_out"], self_target=True,
            idem=_reset_key(s), verify=_v_reset_sent,
            fn=s.okta_send_password_reset),
        "okta.revoke_sessions": Tool(
            "okta.revoke_sessions", "GREEN", self_target=True, requires=["no_fan_out"],
            signature="user", hint="containment",
            idem=_revoke_key, verify=_v_sessions_revoked,
            fn=s.okta_revoke_sessions),
        "okta.force_password_reset": Tool(
            "okta.force_password_reset", "GREEN", self_target=True, requires=["no_fan_out"],
            signature="user", hint="containment",
            idem=_revoke_key,   # user + incident (same recipe as revoke_sessions;
                                # the ledger namespaces per endpoint so they never collide)
            verify=_v_reset_forced,
            fn=s.okta_force_password_reset),
        "servicenow.create_request": Tool(
            "servicenow.create_request", "GREEN",
            signature="item, fields", hint="files the request, does not grant it",
            idem=_request_key(s), verify=_v_request_filed,
            fn=s.servicenow_create_request),
        "endpoint.grant_admin": Tool(
            "endpoint.grant_admin", "GREEN",
            signature="user, minutes", hint="minutes <= 60",
            requires=["authorized", "minutes_le_60", "no_fan_out"], self_target=True,
            idem=_admin_key, verify=_v_admin_granted,
            fn=s.endpoint_grant_admin),
        "assetmgmt.create_case": Tool(
            "assetmgmt.create_case", "GREEN",
            signature="case_type, fields", hint="lost/stolen or offboarding case",
            idem=_case_key, verify=_v_case_registered,
            fn=s.assetmgmt_create_case),
        "iam.create_approval": Tool(
            "iam.create_approval", "GREEN",
            signature="action, approvers", hint="routing is GREEN; the granting is not",
            idem=_approval_key, verify=_v_approval_routed,
            fn=s.iam_create_approval),

        # --- AMBER (never inline; only draftable into create_approval) ------
        "iam.grant_access": Tool(
            "iam.grant_access", "AMBER", signature="user, system, role",
            hint="NEVER call inline - draft it into iam.create_approval",
            fn=s.iam_grant_access),
        "okta.disable_mfa": Tool(
            "okta.disable_mfa", "AMBER", signature="user",
            hint="NEVER call inline - draft it into iam.create_approval",
            fn=s.okta_disable_mfa),

        # --- RED (escalation only) ------------------------------------------
        "soc.open_incident": Tool(
            "soc.open_incident", "RED",
            signature="sev, summary", hint="escalation only",
            idem=_incident_key, verify=_v_incident_open,
            fn=s.soc_open_incident),
        "soc.page_oncall": Tool(
            "soc.page_oncall", "RED",
            signature="team", hint="escalation only",
            idem=_incident_key, verify=_v_paged,
            fn=s.soc_page_oncall),
    }
