"""The deterministic safety inspector.

The LLM only proposes; ``guarded_execute`` is the ONLY place a real action
fires, and it re-checks every hard rule against real system state before it
does. A fooled or forgetful model cannot cause an unsafe action, because:

  - AMBER tools are structurally unreachable inline (they can only be *drafted*
    into iam.create_approval by a handler).
  - RED tools only run during an incident escalation.
  - Every GREEN tool must pass the preconditions its registry row declares
    (authorization, risk-signal-clear, argument caps, ...) - enforced by a
    generic loop over ``tool.requires``, not hardcoded per tool.
  - After firing, the effect is re-read from state (verify) so a silent no-op
    is never reported as success.

Preconditions live in PRECHECKS as small functions that query the mock systems.
Onboarding a new precondition = add one entry; the guard loop is unchanged.
"""

from __future__ import annotations

import re
from typing import Callable

from agent.models import PlannedToolCall, ToolResult
from agent.tools import Tool
from mock.systems import MockSystems, Step2Failure
from mock.ticket_store import Ticket


class Unsafe(Exception):
    """Raised when a proposed action fails a hard safety rule. The real tool is
    never invoked, so nothing unsafe happens."""


class ToolInvocationError(Exception):
    """The model proposed a tool with arguments the tool cannot accept. Not a
    safety violation - the tool did not run - but the handler must route to a
    human rather than crash or claim success."""


# The model may phrase an argument slightly differently than our signatures.
# Normalize the common synonyms so a semantically-correct call still executes;
# anything genuinely malformed falls through to ToolInvocationError.
_ARG_ALIASES = {
    "username": "user", "user_name": "user", "account": "user",
    "account_id": "user", "accountid": "user", "login": "user",
    "user_id": "user", "userid": "user", "target": "user", "target_user": "user",
    "min": "minutes", "duration": "minutes", "duration_minutes": "minutes",
    "approver": "approvers", "sev_level": "sev", "severity": "sev",
}


# Args whose tool signature wants a list. The model emits these comma-separated
# (see models.Arg), so split them back into a list here.
_LIST_ARGS = ("approvers",)


def _normalize_args(args: dict) -> dict:
    out: dict = {}
    for k, v in args.items():
        out[_ARG_ALIASES.get(k, k)] = v
    if "minutes" in out:
        try:
            out["minutes"] = int(out["minutes"])
        except (TypeError, ValueError):
            pass
    for k in _LIST_ARGS:
        v = out.get(k)
        if isinstance(v, str):
            out[k] = [p.strip() for p in v.split(",") if p.strip()]
    return out


class PartialFailure(Exception):
    """A multi-step action fired step 1 but a later step failed or did not
    verify. Carries the results completed so far so the handler can roll back."""

    def __init__(self, message: str, completed: list[ToolResult]) -> None:
        super().__init__(message)
        self.completed = completed


# ----------------------------------------------------------------- PRECHECKS
# name -> (ticket, args, systems) -> bool. True = precondition satisfied.

def _authorized(ticket: Ticket, args: dict, s: MockSystems) -> bool:
    """The requester must be acting on their own account. Authority asserted in
    the ticket body is never trusted; on-behalf-of without proof fails here
    (this is the costly false positive from E-15). A missing target fails closed
    - this precondition is only attached to user-affecting tools."""
    return args.get("user") == ticket.reporter


def _risk_signals_clear(ticket: Ticket, args: dict, s: MockSystems) -> bool:
    """Okta must report no compromise / MFA-fatigue / impossible-travel. This is
    what promotes a GREEN unlock to a RED escalation in context (E-04 vs E-10)."""
    user = args.get("user")
    if user is None:
        return True
    return s.okta_risk_signals(user)["clear"]


def _minutes_le_60(ticket: Ticket, args: dict, s: MockSystems) -> bool:
    """Make-Me-Admin is capped at 60 minutes (POL-04 §4.6)."""
    return int(args.get("minutes", 0)) <= 60


_FANOUT_RE = re.compile(
    r"\b(everyone|everybody|team[-\s]wide|all of (us|them))\b"
    r"|\b(all|every|each|entire|whole)\s+(\w+\s+){0,2}"
    r"(users?|staff|employees?|accounts?|team|people|members?|colleagues?)\b",
    re.I,
)


def _no_fan_out(ticket: Ticket, args: dict, s: MockSystems) -> bool:
    """Blast radius (§6.1): a request that fans out to many users must be routed,
    never auto-fired. Explicitly refuse when the args name multiple targets or
    the ticket asks for a group/team-wide action."""
    for k in ("users", "targets", "accounts", "members"):
        v = args.get(k)
        if isinstance(v, (list, tuple, set)) and len(v) > 1:
            return False
    return _FANOUT_RE.search(ticket.body or "") is None


PRECHECKS: dict[str, Callable[[Ticket, dict, MockSystems], bool]] = {
    "authorized": _authorized,
    "risk_signals_clear": _risk_signals_clear,
    "minutes_le_60": _minutes_le_60,
    "no_fan_out": _no_fan_out,
}


# ------------------------------------------------------------------ the guard

def enforce_risk_class(tool: Tool, in_escalation: bool) -> None:
    if tool.risk == "AMBER":
        raise Unsafe(f"{tool.name}: AMBER tools must be routed via iam.create_approval, "
                     f"never executed inline")
    if tool.risk == "RED" and not in_escalation:
        raise Unsafe(f"{tool.name}: RED tools may only run during an incident escalation")


def _did_effect_take(tool: Tool, args: dict, resp: dict, s: MockSystems) -> bool:
    """Re-read state to confirm the action took. Catches the silent no-op."""
    if tool.verify is not None:
        return tool.verify(args, resp, s)
    # No queryable state to re-read: trust only a non-error status.
    return resp.get("status") not in {"error", "rejected"}


def guarded_execute(
    call: PlannedToolCall,
    ticket: Ticket,
    registry: dict[str, Tool],
    systems: MockSystems,
    in_escalation: bool = False,
) -> ToolResult:
    """Run one proposed tool call through every hard gate, then fire it once and
    verify. Raises Unsafe (nothing executed) or Step2Failure-derived errors."""
    if call.tool not in registry:
        raise Unsafe(f"{call.tool}: unknown tool")
    tool = registry[call.tool]
    args = _normalize_args(call.arg_dict())

    # Self-service tools act on the requester's own account. If the model omits
    # the target, default it to the reporter - this can only ever target the
    # requester themselves, so it never enables an on-behalf-of action.
    if tool.self_target and not args.get("user"):
        args["user"] = ticket.reporter

    # 1. Risk-class floor (AMBER blocked, RED escalation-only).
    enforce_risk_class(tool, in_escalation)

    # 2. Declared preconditions - generic loop over whatever the tool requires.
    for check_name in tool.requires:
        check = PRECHECKS.get(check_name)
        if check is None:
            raise Unsafe(f"{call.tool}: unknown precondition '{check_name}'")
        if not check(ticket, args, systems):
            raise Unsafe(f"{call.tool}: precondition '{check_name}' failed")

    # 3. Fire once (idempotency key for state-changing tools), then 4. verify.
    #    Any arg-shape problem in the key recipe, the call, or the verify is a
    #    controlled ToolInvocationError - the handler routes to a human rather
    #    than crashing or claiming success. Step2Failure must still propagate.
    try:
        key = None if tool.read_only or tool.idem is None else tool.idem(ticket, args)
        if key is not None:
            resp = tool.fn(**args, idempotency_key=key)
        else:
            resp = tool.fn(**args)
        verified = True if tool.read_only else _did_effect_take(tool, args, resp, systems)
    except Step2Failure:
        raise
    except (TypeError, KeyError, AttributeError) as e:
        raise ToolInvocationError(f"{call.tool}: bad arguments {sorted(args)}: {e}")

    return ToolResult(
        tool=call.tool,
        args=args,
        idempotency_key=key,
        raw_response=resp,
        verified=verified,
        idempotent_replay=bool(resp.get("idempotent_replay")),
    )
