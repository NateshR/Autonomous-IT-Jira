"""In-memory mock of Helix privileged systems: Okta, ServiceNow, IAM, SOC,
Directory, and the endpoint (Make-Me-Admin) and asset-management surfaces.

Design notes
------------
- Every state-changing endpoint accepts an ``idempotency_key``. A repeat key
  returns the *same* stored result and performs no second effect (see §7 of the
  brief). Using the key is the caller's responsibility; the store enforces it.
- Two deliberate failure modes are supported so the agent can prove it handles
  them (see §6.4 / §7):
    1. Silent no-op: an account flagged ``silent_noop_unlock`` returns
       ``{"status": "success"}`` from ``okta.unlock_account`` but stays locked.
       The guard must re-read state to catch this before claiming success.
    2. Step-2 failure: ``assetmgmt.create_case`` commits the case (step 1) then
       raises :class:`Step2Failure` during CMDB registration (step 2) for a
       flagged asset, leaving a half-done state the handler must roll back.
- Read-only helpers (``is_locked`` etc.) exist so the guard can verify effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class Step2Failure(Exception):
    """Raised by a multi-step endpoint when its second step fails.

    Carries the id of the partial record created by step 1 so the caller can
    roll it back.
    """

    def __init__(self, message: str, rollback_id: str) -> None:
        super().__init__(message)
        self.rollback_id = rollback_id


@dataclass
class Account:
    user: str
    locked: bool = False
    lock_epoch: int = 0          # bumps on each new lockout; part of the idem key
    mfa_enabled: bool = True
    active_sessions: int = 0
    # risk_signals flags
    compromise: bool = False
    mfa_fatigue: bool = False
    impossible_travel: bool = False
    # failure-mode hook
    silent_noop_unlock: bool = False


@dataclass
class DirectoryUser:
    user: str
    display_name: str
    manager: str | None = None
    is_privileged: bool = False
    active: bool = True


class MockSystems:
    """Single object holding all mock state. Instantiate once, seed, then pass
    to the tool registry. An in-memory dict behind a few methods is plenty."""

    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {}
        self.directory: dict[str, DirectoryUser] = {}
        self.approvals: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.cases: dict[str, dict] = {}
        self.admin_grants: dict[str, dict] = {}
        self.incidents: dict[str, dict] = {}
        self.pages: list[dict] = []
        self.reset_emails: list[dict] = []
        # idempotency ledger: key -> stored result
        self._idem: dict[str, Any] = {}
        self._counter = 0
        # assets whose CMDB registration (create_case step 2) is seeded to fail
        self._cmdb_fail_assets: set[str] = set()
        # injected clock for calendar-day idempotency keys (no Date.now in code)
        self.today: str = "2026-07-15"

    # ------------------------------------------------------------------ utils
    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter:04d}"

    def _idempotent(self, key: str | None, produce: Callable[[], Any]) -> Any:
        """Run ``produce`` once per key. Repeat keys return the stored result
        without re-running the effect."""
        if key is None:
            return produce()
        if key in self._idem:
            return {**self._idem[key], "idempotent_replay": True}
        result = produce()
        self._idem[key] = result
        return result

    # -------------------------------------------------------------- directory
    def lookup_user(self, user: str) -> dict:
        u = self.directory.get(user)
        if u is None:
            return {"found": False, "user": user}
        return {
            "found": True,
            "user": u.user,
            "display_name": u.display_name,
            "manager": u.manager,
            "is_privileged": u.is_privileged,
            "active": u.active,
        }

    def verify_manager(self, manager: str, subordinate: str) -> dict:
        sub = self.directory.get(subordinate)
        is_mgr = bool(sub and sub.manager == manager)
        return {"manager": manager, "subordinate": subordinate, "verified": is_mgr}

    # ------------------------------------------------------------------- okta
    def okta_risk_signals(self, user: str) -> dict:
        a = self.accounts.get(user)
        if a is None:
            return {"user": user, "clear": True, "flags": []}
        flags = [
            name
            for name, on in (
                ("compromise", a.compromise),
                ("mfa_fatigue", a.mfa_fatigue),
                ("impossible_travel", a.impossible_travel),
            )
            if on
        ]
        return {"user": user, "clear": not flags, "flags": flags}

    def okta_unlock_account(self, user: str, idempotency_key: str | None = None) -> dict:
        def produce() -> dict:
            a = self.accounts.get(user)
            if a is None:
                return {"status": "error", "reason": "no such account", "user": user}
            # Failure mode 1: return success but do NOT actually unlock.
            if a.silent_noop_unlock:
                return {"status": "success", "user": user, "note": "acknowledged"}
            a.locked = False
            return {"status": "success", "user": user}

        return self._idempotent(idempotency_key, produce)

    def okta_send_password_reset(self, user: str, idempotency_key: str | None = None) -> dict:
        def produce() -> dict:
            email = f"{user}@helix.example"
            self.reset_emails.append({"user": user, "email": email})
            return {"status": "sent", "user": user, "email": email}

        return self._idempotent(idempotency_key, produce)

    def okta_revoke_sessions(self, user: str, idempotency_key: str | None = None) -> dict:
        def produce() -> dict:
            a = self.accounts.get(user)
            if a is not None:
                a.active_sessions = 0
            return {"status": "revoked", "user": user}

        return self._idempotent(idempotency_key, produce)

    def okta_force_password_reset(self, user: str, idempotency_key: str | None = None) -> dict:
        def produce() -> dict:
            return {"status": "reset_forced", "user": user}

        return self._idempotent(idempotency_key, produce)

    # AMBER - must never be called inline; only reachable as a drafted action.
    def okta_disable_mfa(self, user: str, idempotency_key: str | None = None) -> dict:
        def produce() -> dict:
            a = self.accounts.get(user)
            if a is not None:
                a.mfa_enabled = False
            return {"status": "mfa_disabled", "user": user}

        return self._idempotent(idempotency_key, produce)

    # ------------------------------------------------------------- servicenow
    def servicenow_create_request(
        self, item: str, fields: dict, idempotency_key: str | None = None
    ) -> dict:
        def produce() -> dict:
            rid = self._next_id("REQ")
            self.requests[rid] = {"id": rid, "item": item, "fields": fields, "status": "filed"}
            return {"status": "filed", "request_id": rid, "item": item}

        return self._idempotent(idempotency_key, produce)

    # ---------------------------------------------------------------- endpoint
    def endpoint_grant_admin(
        self, user: str, minutes: int, idempotency_key: str | None = None
    ) -> dict:
        def produce() -> dict:
            # Argument-level guardrail: Make-Me-Admin is capped at 60 min (POL-04 §4.6).
            if minutes > 60:
                return {"status": "rejected", "reason": "minutes exceeds 60 cap", "user": user}
            gid = self._next_id("ADM")
            self.admin_grants[gid] = {"id": gid, "user": user, "minutes": minutes}
            return {"status": "granted", "grant_id": gid, "user": user, "minutes": minutes}

        return self._idempotent(idempotency_key, produce)

    # --------------------------------------------------------------- assetmgmt
    def assetmgmt_create_case(
        self, case_type: str, fields: dict, idempotency_key: str | None = None
    ) -> dict:
        def produce() -> dict:
            # Step 1: create the case (committed).
            cid = self._next_id("CASE")
            self.cases[cid] = {"id": cid, "case_type": case_type, "fields": fields, "status": "open"}
            # Step 2: CMDB registration. Failure mode 2 for a flagged asset.
            asset = fields.get("asset")
            if asset and asset in self._cmdb_fail_assets:
                raise Step2Failure(f"CMDB registration failed for {asset}", rollback_id=cid)
            self.cases[cid]["cmdb_registered"] = True
            return {"status": "open", "case_id": cid, "case_type": case_type}

        return self._idempotent(idempotency_key, produce)

    def delete_case(self, case_id: str) -> dict:
        """Rollback helper for a half-done create_case."""
        self.cases.pop(case_id, None)
        return {"status": "rolled_back", "case_id": case_id}

    # -------------------------------------------------------------------- iam
    def iam_create_approval(
        self, action: str, approvers: list[str], idempotency_key: str | None = None
    ) -> dict:
        def produce() -> dict:
            aid = self._next_id("APR")
            self.approvals[aid] = {
                "id": aid,
                "action": action,
                "approvers": approvers,
                "status": "PENDING",
            }
            return {"status": "PENDING", "approval_id": aid, "approvers": approvers}

        return self._idempotent(idempotency_key, produce)

    def iam_get_approval(self, approval_id: str) -> dict:
        rec = self.approvals.get(approval_id)
        if rec is None:
            return {"approval_id": approval_id, "status": "NONE"}
        return {"approval_id": approval_id, "status": rec["status"], "action": rec["action"]}

    # AMBER - must never be called inline.
    def iam_grant_access(
        self, user: str, system: str, role: str, idempotency_key: str | None = None
    ) -> dict:
        def produce() -> dict:
            return {"status": "granted", "user": user, "system": system, "role": role}

        return self._idempotent(idempotency_key, produce)

    # -------------------------------------------------------------------- soc
    def soc_open_incident(
        self, sev: str, summary: str, idempotency_key: str | None = None
    ) -> dict:
        def produce() -> dict:
            iid = self._next_id("INC")
            self.incidents[iid] = {"id": iid, "sev": sev, "summary": summary, "status": "open"}
            return {"status": "open", "incident_id": iid, "sev": sev}

        return self._idempotent(idempotency_key, produce)

    def soc_page_oncall(self, team: str, idempotency_key: str | None = None) -> dict:
        def produce() -> dict:
            self.pages.append({"team": team})
            return {"status": "paged", "team": team}

        return self._idempotent(idempotency_key, produce)

    # -------------------------------------------------------- read-only verify
    def is_locked(self, user: str) -> bool:
        a = self.accounts.get(user)
        return bool(a and a.locked)

    def mfa_enabled(self, user: str) -> bool:
        a = self.accounts.get(user)
        return bool(a and a.mfa_enabled)

    def request_exists(self, request_id: str) -> bool:
        return request_id in self.requests

    def grant_exists(self, grant_id: str) -> bool:
        return grant_id in self.admin_grants

    def case_registered(self, case_id: str) -> bool:
        c = self.cases.get(case_id)
        return bool(c and c.get("cmdb_registered"))

    def incident_exists(self, incident_id: str) -> bool:
        return incident_id in self.incidents
