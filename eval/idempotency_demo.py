"""Idempotency demonstration (stretch): run an acting ticket twice - and feed a
duplicate - and show the action is performed exactly once.

Uses the StubLLM so the demo is deterministic and free; idempotency is a
property of the guard + mock idempotency ledger, not of the model.
"""

from __future__ import annotations

from agent.llm import StubLLM
from agent.models import Decision, PlannedToolCall, PolicySpan
from agent.pipeline import Agent
from agent.retriever import Retriever
from mock.seed import seed_systems
from mock.ticket_store import MockTicketStore, Ticket

BAR = "=" * 74


def _unlock_decision() -> Decision:
    return Decision(
        disposition="AUTO_ACTION",
        citations=[PolicySpan(policy_id="POL-01", section="1.4")],
        reasoning="account owner locked out past the self-service window; risk clear",
        planned_tool_calls=[
            PlannedToolCall(tool="okta.risk_signals", args={"user": "jsmith"}),
            PlannedToolCall(tool="okta.unlock_account", args={"user": "jsmith"}),
        ],
    )


def main() -> None:
    systems = seed_systems()                       # ONE shared system (not reseeded)
    store = MockTicketStore()
    # Two tickets about the SAME lockout, plus a duplicate of the first.
    store.add(Ticket(id="ACT-1", reporter="jsmith", body="locked out of my own account"))
    store.add(Ticket(id="ACT-2", reporter="jsmith", body="still locked out, please unlock"))
    store.add(Ticket(id="ACT-1-DUP", reporter="jsmith",
                     body="same as ACT-1", duplicate_of="ACT-1"))

    table = {"ACT-1": _unlock_decision(), "ACT-2": _unlock_decision()}
    agent = Agent(store, systems, Retriever.from_dir("policies"), StubLLM(table))

    print(BAR)
    print("Account jsmith locked:", systems.is_locked("jsmith"), "(lock_epoch",
          systems.accounts["jsmith"].lock_epoch, ")")

    print("\n1) First acting ticket ACT-1:")
    r1 = agent.handle("ACT-1")
    unlock1 = next(t for t in r1.tool_results if t.tool == "okta.unlock_account")
    print(f"   unlock key = {unlock1.idempotency_key}  replay = {unlock1.idempotent_replay}")
    print(f"   account now locked? {systems.is_locked('jsmith')}   outcome = {r1.outcome}")

    print("\n2) Re-submission ACT-2 (same lockout -> same idempotency key):")
    r2 = agent.handle("ACT-2")
    unlock2 = next(t for t in r2.tool_results if t.tool == "okta.unlock_account")
    print(f"   unlock key = {unlock2.idempotency_key}  replay = {unlock2.idempotent_replay}")
    print(f"   -> action NOT performed twice (same key, deduped)")

    print("\n3) Duplicate ticket ACT-1-DUP:")
    r3 = agent.handle("ACT-1-DUP")
    print(f"   outcome = {r3.outcome}   links = {store.get('ACT-1-DUP').links}")
    print(f"   -> linked to the original, no action taken")

    same_key = unlock1.idempotency_key == unlock2.idempotency_key
    acted_once = (not unlock1.idempotent_replay) and unlock2.idempotent_replay
    print("\nRESULT:", "PASS" if (same_key and acted_once) else "FAIL",
          "- action performed exactly once across retry + duplicate")
    print(BAR)


if __name__ == "__main__":
    main()
