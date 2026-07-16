"""The six disposition handlers. Each produces exactly the artifact its
disposition requires (NOTES §4) and is the only place tool execution is driven -
always through the guard. AUTO_ACTION and ESCALATE are the only handlers that
mutate systems; PROPOSE can only route via iam.create_approval; the rest just
comment/transition.
"""

from __future__ import annotations

from agent.constants import Status
from agent.context import AgentContext
from agent.guard import PartialFailure, ToolInvocationError, Unsafe, guarded_execute
from agent.models import AuditRecord, Decision, PlannedToolCall, ToolResult
from agent.redaction import redact
from mock.systems import Step2Failure
from mock.ticket_store import Ticket

_ROLLED_BACK = ("Action partially failed and was rolled back; flagged for a human.")


def _base_record(ticket: Ticket, decision: Decision) -> AuditRecord:
    return AuditRecord(
        ticket_id=ticket.id,
        disposition=decision.disposition,
        citations=decision.citations,
        reasoning=redact(decision.reasoning),
    )


def _cites(decision: Decision) -> str:
    return ", ".join(c.cite() for c in decision.citations) or "policy"



def _run_chain(decision: Decision, ticket: Ticket, ctx: AgentContext,
               in_escalation: bool = False) -> list[ToolResult]:
    """Run the proposed tool calls through the guard, in order, verifying each.
    Shared by AUTO_ACTION and ESCALATE - the only two handlers that mutate state.
    Raises Unsafe / ToolInvocationError / Step2Failure / PartialFailure upward so
    each handler can apply its own policy for what to do about it."""
    completed: list[ToolResult] = []
    for call in decision.planned_tool_calls:
        r = guarded_execute(call, ticket, ctx.registry, ctx.systems,
                            in_escalation=in_escalation)
        if not r.verified:
            raise PartialFailure(f"{call.tool} did not verify", completed)
        completed.append(r)
    return completed


def _fail(rec: AuditRecord, ticket: Ticket, ctx: AgentContext, *, note: str,
          comment: str, status: str, outcome: str,
          results: list[ToolResult] | None = None) -> AuditRecord:
    """Record a non-success outcome consistently: keep what completed, note why,
    tell the requester, move the ticket, and never claim success."""
    rec.tool_results = results or []
    rec.notes.append(note)
    ctx.store.comment(ticket.id, comment)
    ctx.store.transition(ticket.id, status)
    rec.outcome = outcome
    return rec


# --------------------------------------------------------------- ANSWER_ONLY
def answer_only(ticket: Ticket, decision: Decision, ctx: AgentContext) -> AuditRecord:
    rec = _base_record(ticket, decision)
    ctx.store.comment(ticket.id, f"{redact(decision.reasoning)} (per {_cites(decision)})")
    ctx.store.transition(ticket.id, Status.CLOSED)
    rec.outcome = "closed"
    return rec


# ---------------------------------------------------------------- AUTO_ACTION
def auto_action(ticket: Ticket, decision: Decision, ctx: AgentContext) -> AuditRecord:
    rec = _base_record(ticket, decision)
    try:
        completed = _run_chain(decision, ticket, ctx)
    except (Unsafe, ToolInvocationError) as e:
        # The guard blocked or could not run a proposed action. The safe response
        # is to NOT act and route to a human - never force it through.
        rec.disposition = "DEFER_HUMAN"
        return _fail(rec, ticket, ctx, note=f"guard blocked: {e}",
                     comment="Could not complete this safely; routing to the Service Desk.",
                     status=Status.DEFERRED, outcome="deferred")
    except Step2Failure as e:
        ctx.systems.delete_case(e.rollback_id)   # undo the committed step-1 partial
        return _fail(rec, ticket, ctx,
                     note=f"multi-step failure, rolled back step 1 ({e.rollback_id}): {e}",
                     comment=_ROLLED_BACK, status=Status.DEFERRED, outcome="rolled_back")
    except PartialFailure as e:
        _rollback(e.completed, ctx)
        return _fail(rec, ticket, ctx, note=f"partial failure, rolled back/flagged: {e}",
                     comment=_ROLLED_BACK, status=Status.DEFERRED, outcome="rolled_back",
                     results=e.completed)

    rec.tool_results = completed
    ctx.store.comment(ticket.id, f"Done: {redact(decision.reasoning)} (per {_cites(decision)})")
    ctx.store.transition(ticket.id, Status.CLOSED)
    rec.outcome = "closed"
    return rec


# --------------------------------------------------------- PROPOSE_FOR_APPROVAL
def propose_for_approval(ticket: Ticket, decision: Decision, ctx: AgentContext) -> AuditRecord:
    rec = _base_record(ticket, decision)
    routed = None
    for call in decision.planned_tool_calls:
        try:
            r = guarded_execute(call, ticket, ctx.registry, ctx.systems)
        except (Unsafe, ToolInvocationError) as e:
            # Correct behavior: an AMBER grant proposed here is refused inline.
            rec.notes.append(f"refused inline (correct): {e}")
            continue
        rec.tool_results.append(r)
        if call.tool == "iam.create_approval":
            routed = r
    if routed is not None:
        aid = routed.raw_response.get("approval_id")
        ctx.store.comment(
            ticket.id,
            f"This is a privileged action and cannot be executed automatically. "
            f"Routed for approval ({aid}) per {_cites(decision)}. This ticket stays pending.",
        )
        ctx.store.transition(ticket.id, Status.WAITING_APPROVAL)
        rec.outcome = "pending"
    else:
        ctx.store.comment(ticket.id, "Privileged request could not be routed; escalating to a human.")
        ctx.store.transition(ticket.id, Status.DEFERRED)
        rec.outcome = "deferred"
    return rec


# ------------------------------------------------------------ ESCALATE_INCIDENT
def escalate_incident(ticket: Ticket, decision: Decision, ctx: AgentContext) -> AuditRecord:
    rec = _base_record(ticket, decision)
    try:
        completed = _run_chain(decision, ticket, ctx, in_escalation=True)
    except (Unsafe, PartialFailure, Step2Failure, ToolInvocationError) as e:
        _rollback(getattr(e, "completed", []), ctx)
        return _fail(rec, ticket, ctx, note=f"containment partial/blocked, flagged: {e}",
                     comment="Security incident raised; some containment steps failed "
                             "and were flagged for SOC.",
                     status=Status.ESCALATED, outcome="escalated")

    rec.tool_results = completed
    # POL-09 §9.2 containment instruction to the user; never close a RED ticket.
    ctx.store.comment(
        ticket.id,
        "This looks like a security incident. An incident has been opened and the on-call "
        "team paged; active sessions were revoked and a password reset forced. Do not approve "
        "any prompts. Per POL-09 §9.2, disconnect from the network (unplug Ethernet, disable "
        "Wi-Fi), do NOT power off, and await SOC instructions.",
    )
    ctx.store.transition(ticket.id, Status.ESCALATED)
    rec.outcome = "escalated"
    return rec


# ------------------------------------------------------------ ASK_CLARIFICATION
def ask_clarification(ticket: Ticket, decision: Decision, ctx: AgentContext) -> AuditRecord:
    rec = _base_record(ticket, decision)
    question = redact(decision.reasoning) or "Could you share more detail so we can help safely?"
    ctx.store.comment(ticket.id, question)
    ctx.store.transition(ticket.id, Status.WAITING_CUSTOMER)
    ctx.store.add_label(ticket.id, "needs-clarification")
    rec.outcome = "waiting"
    return rec


_QUEUE_RULES = [
    ("People Ops", ("hr", "vacation", "pto", "payroll", "benefits", "workday", "people ops")),
    ("Security", ("security", "phish", "inject", "malware", "breach", "compromise", "soc")),
    ("Data Governance", ("data owner", "dlp", "restricted", "confidential", "classification")),
    ("Network Security", ("vpn", "travel exception", "geo", "anyconnect")),
]


def _route_queue(reasoning: str) -> str:
    text = reasoning.lower()
    for queue, keywords in _QUEUE_RULES:
        if any(k in text for k in keywords):
            return queue
    return "Service Desk"


# ----------------------------------------------------------------- DEFER_HUMAN
def defer_human(ticket: Ticket, decision: Decision, ctx: AgentContext) -> AuditRecord:
    rec = _base_record(ticket, decision)
    queue = _route_queue(decision.reasoning)
    ctx.store.comment(ticket.id, f"Routing to {queue}: {redact(decision.reasoning)}")
    ctx.store.add_label(ticket.id, f"queue:{queue.lower().replace(' ', '-')}")
    ctx.store.transition(ticket.id, Status.DEFERRED)
    rec.outcome = f"deferred->{queue}"
    return rec


# ------------------------------------------------------------------- rollback
def _rollback(completed: list[ToolResult], ctx: AgentContext) -> None:
    """Undo what we can (currently: created asset cases) and leave the rest for a
    human. Best-effort and honest: we never claim a clean rollback we cannot do."""
    for r in completed:
        if r.tool == "assetmgmt.create_case":
            cid = r.raw_response.get("case_id")
            if cid:
                ctx.systems.delete_case(cid)


HANDLERS = {
    "ANSWER_ONLY": answer_only,
    "AUTO_ACTION": auto_action,
    "PROPOSE_FOR_APPROVAL": propose_for_approval,
    "ESCALATE_INCIDENT": escalate_incident,
    "ASK_CLARIFICATION": ask_clarification,
    "DEFER_HUMAN": defer_human,
}
