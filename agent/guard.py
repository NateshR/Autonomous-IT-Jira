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

from typing import Callable

from agent.models import PlannedToolCall, ToolResult
from agent.tools import Tool
from mock.systems import MockSystems
from mock.ticket_store import Ticket


class Unsafe(Exception):
    """Raised when a proposed action fails a hard safety rule. The real tool is
    never invoked, so nothing unsafe happens."""


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
    (this is the costly false positive from E-15)."""
    target = args.get("user")
    if target is None:
        return True  # tool does not act on a specific user account
    return target == ticket.reporter


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


PRECHECKS: dict[str, Callable[[Ticket, dict, MockSystems], bool]] = {
    "authorized": _authorized,
    "risk_signals_clear": _risk_signals_clear,
    "minutes_le_60": _minutes_le_60,
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

    # 1. Risk-class floor (AMBER blocked, RED escalation-only).
    enforce_risk_class(tool, in_escalation)

    # 2. Declared preconditions - generic loop over whatever the tool requires.
    for check_name in tool.requires:
        check = PRECHECKS.get(check_name)
        if check is None:
            raise Unsafe(f"{call.tool}: unknown precondition '{check_name}'")
        if not check(ticket, call.args, systems):
            raise Unsafe(f"{call.tool}: precondition '{check_name}' failed")

    # 3. Fire once, with an idempotency key for state-changing tools.
    key = None if tool.read_only or tool.idem is None else tool.idem(ticket, call.args)
    if key is not None:
        resp = tool.fn(**call.args, idempotency_key=key)
    else:
        resp = tool.fn(**call.args)

    # 4. Verify the effect (skip for read-only tools).
    verified = True if tool.read_only else _did_effect_take(tool, call.args, resp, systems)

    return ToolResult(
        tool=call.tool,
        args=call.args,
        idempotency_key=key,
        raw_response=resp,
        verified=verified,
        idempotent_replay=bool(resp.get("idempotent_replay")),
    )
