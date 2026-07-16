"""The five-stage pipeline: ingest -> retrieve -> decide -> guard+execute (via
handler) -> record. This is the orchestrator; the safety logic lives in the
guard and the artifacts in the handlers.
"""

from __future__ import annotations

from agent import decider
from agent.config import SETTINGS
from agent.constants import Status
from agent.context import AgentContext
from agent.llm import LLMClient
from agent.models import AuditRecord, Decision
from agent.handlers import HANDLERS
from agent.retriever import Retriever
from agent.tools import build_tool_registry
from mock.systems import MockSystems
from mock.ticket_store import Ticket, TicketStore

# Dispositions that assert an answer or take an action must be grounded.
_REQUIRE_CITATION = {"ANSWER_ONLY", "AUTO_ACTION", "PROPOSE_FOR_APPROVAL"}


class Agent:
    def __init__(self, store: TicketStore, systems: MockSystems, retriever: Retriever,
                 llm: LLMClient, top_k: int | None = None,
                 min_score: float | None = None) -> None:
        self.ctx = AgentContext(store=store, systems=systems,
                                registry=build_tool_registry(systems))
        self.retriever = retriever
        self.llm = llm
        self.top_k = SETTINGS.retrieval_top_k if top_k is None else top_k
        self.min_score = SETTINGS.retrieval_min_score if min_score is None else min_score

    # -------------------------------------------------------------- one ticket
    def handle(self, ticket_id: str) -> AuditRecord:
        # Stage 1: ingest - re-read fresh state to catch duplicates/withdrawals.
        ticket = self.ctx.store.get(ticket_id)

        dup = self._duplicate_or_withdrawn(ticket)
        if dup is not None:
            return dup

        # Stage 2: retrieve a ranking hint; the full corpus is always supplied.
        relevant = self.retriever.search(ticket.body, self.top_k, self.min_score)

        # Stage 3: decide (LLM proposes).
        decision = decider.decide(self.llm, ticket, relevant, self.retriever.spans,
                                  self.ctx.registry)
        decision = self._enforce_grounding(decision)

        # Stage 4: dispatch to the disposition handler (guard executes inside).
        record = HANDLERS[decision.disposition](ticket, decision, self.ctx)

        # Stage 5: tally unsafe actions (should always be 0 - the guard ensures it).
        record.unsafe_action_count = self._count_unsafe(record)
        return record

    # ------------------------------------------------------------- ingest gate
    def _duplicate_or_withdrawn(self, ticket: Ticket) -> AuditRecord | None:
        if ticket.withdrawn:
            self.ctx.store.comment(ticket.id, "Ticket withdrawn by requester; taking no action.")
            self.ctx.store.transition(ticket.id, Status.CLOSED)
            return AuditRecord(ticket_id=ticket.id, disposition="DEFER_HUMAN",
                               outcome="withdrawn",
                               reasoning="honored withdrawal; no action taken")
        if ticket.duplicate_of:
            self.ctx.store.link_issues(ticket.id, ticket.duplicate_of)
            self.ctx.store.comment(ticket.id, f"Duplicate of {ticket.duplicate_of}; linked, no action taken.")
            return AuditRecord(ticket_id=ticket.id, disposition="DEFER_HUMAN",
                               outcome="duplicate",
                               reasoning=f"linked duplicate of {ticket.duplicate_of}; not re-acted")
        return None

    # ------------------------------------------------------- grounding gate
    def _enforce_grounding(self, decision: Decision) -> Decision:
        # A citation only counts if the cited section actually exists in the
        # corpus. This closes the "citing a non-existent policy" case (§6.6):
        # a hallucinated section is dropped, and an answer/action with no valid
        # citation left is downgraded to DEFER.
        valid = [c for c in decision.citations
                 if self.retriever.get(c.policy_id, c.section) is not None]
        if decision.disposition in _REQUIRE_CITATION and not valid:
            return Decision(
                disposition="DEFER_HUMAN",
                reasoning=(f"no valid policy grounding for the proposed "
                           f"{decision.disposition} (cited section not found); "
                           f"routing to a human"),
            )
        if len(valid) != len(decision.citations):
            return decision.model_copy(update={"citations": valid})
        return decision

    # ------------------------------------------------------- safety accounting
    def _count_unsafe(self, record: AuditRecord) -> int:
        unsafe = 0
        for r in record.tool_results:
            tool = self.ctx.registry.get(r.tool)
            if tool is None:
                continue
            if tool.risk == "AMBER":
                unsafe += 1
            elif tool.risk == "RED" and record.disposition != "ESCALATE_INCIDENT":
                unsafe += 1
        return unsafe
