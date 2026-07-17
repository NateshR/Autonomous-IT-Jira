"""End-to-end pipeline tests driven by a deterministic StubLLM.

These exercise every disposition handler and prove the whole path (ingest ->
retrieve -> decide -> guard+execute -> record) works, that unsafe actions never
fire, and that the guard catches a wrongly-proposed AUTO_ACTION.
"""

from agent.llm import StubLLM
from agent.models import Decision, PlannedToolCall, PolicySpan
from agent.pipeline import Agent
from agent.retriever import Retriever
from mock.seed import seed_systems
from mock.ticket_store import MockTicketStore, Ticket

POLICY_DIR = "policies"


def span(pid, sec):
    return PolicySpan(policy_id=pid, section=sec, text="")


def tc(tool, **args):
    return PlannedToolCall(tool=tool, args=args)


def build(tickets, table):
    store = MockTicketStore()
    for t in tickets:
        store.add(t)
    systems = seed_systems()
    agent = Agent(store, systems, Retriever.from_dir(POLICY_DIR), StubLLM(table))
    return store, systems, agent


def test_answer_only():
    t = Ticket(id="E-01", reporter="jsmith", body="how many failed attempts before locked out?")
    store, _, agent = build([t], {"E-01": Decision(
        disposition="ANSWER_ONLY", citations=[span("POL-01", "1.4")],
        reasoning="Accounts lock after 5 consecutive failed attempts")})
    rec = agent.handle("E-01")
    assert rec.disposition == "ANSWER_ONLY" and rec.outcome == "closed"
    assert store.get("E-01").status == "Closed" and rec.unsafe_action_count == 0
    assert not rec.tool_results  # no mutation


def test_auto_action_unlock():
    t = Ticket(id="E-04", reporter="jsmith", body="locked out 20 minutes")
    store, systems, agent = build([t], {"E-04": Decision(
        disposition="AUTO_ACTION", citations=[span("POL-01", "1.4")],
        reasoning="owner locked out, risk clear",
        planned_tool_calls=[tc("okta.risk_signals", user="jsmith"),
                            tc("okta.unlock_account", user="jsmith")])})
    rec = agent.handle("E-04")
    assert rec.outcome == "closed" and systems.is_locked("jsmith") is False
    assert rec.unsafe_action_count == 0


def test_propose_routes_and_refuses_amber_inline():
    t = Ticket(id="E-07", reporter="rkumar", body="grant me prod postgres admin, manager said ok")
    store, systems, agent = build([t], {"E-07": Decision(
        disposition="PROPOSE_FOR_APPROVAL", citations=[span("POL-10", "10.2")],
        reasoning="privileged access",
        planned_tool_calls=[
            tc("iam.create_approval", action="grant prod-postgres admin",
               approvers=["asmith", "dbowner"]),
            tc("iam.grant_access", user="rkumar", system="prod-postgres", role="admin"),
        ])})
    rec = agent.handle("E-07")
    assert rec.outcome == "pending"
    assert store.get("E-07").status == "Waiting for Approval"
    assert any("refused inline" in n for n in rec.notes)   # AMBER grant blocked
    assert rec.unsafe_action_count == 0                    # and not counted as executed


def test_escalate_incident_contains_and_stays_open():
    t = Ticket(id="E-10", reporter="pjones", body="6 okta pushes I didn't start")
    store, systems, agent = build([t], {"E-10": Decision(
        disposition="ESCALATE_INCIDENT",
        citations=[span("POL-01", "1.3"), span("POL-09", "9.2")],
        reasoning="MFA fatigue attack",
        planned_tool_calls=[
            tc("okta.risk_signals", user="pjones"),
            tc("soc.open_incident", sev="SEV-2", summary="MFA fatigue on pjones"),
            tc("soc.page_oncall", team="soc"),
            tc("okta.revoke_sessions", user="pjones"),
            tc("okta.force_password_reset", user="pjones"),
        ])})
    rec = agent.handle("E-10")
    assert rec.outcome == "escalated"
    assert store.get("E-10").status == "Escalated"      # never closed
    assert systems.accounts["pjones"].active_sessions == 0
    assert rec.unsafe_action_count == 0                 # RED allowed under escalation


def test_ask_clarification():
    t = Ticket(id="E-11", reporter="mtaylor", body="my laptop is broken")
    store, _, agent = build([t], {"E-11": Decision(
        disposition="ASK_CLARIFICATION", reasoning="What exactly is failing?")})
    rec = agent.handle("E-11")
    assert rec.outcome == "waiting" and "needs-clarification" in store.get("E-11").labels


def test_defer_out_of_scope_routes_to_named_queue():
    t = Ticket(id="E-12", reporter="mtaylor", body="how many vacation days do I have?")
    store, _, agent = build([t], {"E-12": Decision(
        disposition="DEFER_HUMAN", reasoning="This is an HR/PTO question, not IT")})
    rec = agent.handle("E-12")
    assert rec.outcome == "deferred->People Ops"
    assert "queue:people-ops" in store.get("E-12").labels


def test_ask_then_reply_reevaluates():
    # ASK_CLARIFICATION sets Waiting for Customer; when the requester replies,
    # re-handling must re-evaluate and act (§4: unlike DEFER, ASK stays with the
    # agent). The reply MUST arrive the way a real one does - as a comment on the
    # ticket, via store.reply(). An earlier version of this test appended the
    # answer to `body` instead, which passed while proving nothing: the decider
    # only ever read `body`, so the real path (comments) was never exercised.
    def reply_aware(system, user, tag):
        if "cracked" in user or "won't turn on" in user:
            return Decision(disposition="ANSWER_ONLY",
                            citations=[PolicySpan(policy_id="POL-08", section="8.4")],
                            reasoning="repairs are done at the IT Depot in Austin")
        return Decision(disposition="ASK_CLARIFICATION",
                        reasoning="What exactly is failing on the laptop?")

    t = Ticket(id="RP-1", reporter="mtaylor", body="My laptop is broken.")
    store, _, agent = build([t], reply_aware)
    r1 = agent.handle("RP-1")
    assert r1.disposition == "ASK_CLARIFICATION"
    assert store.get("RP-1").status == "Waiting for Customer"

    # the requester replies - as a COMMENT, which is how a reply really arrives:
    store.reply("RP-1", "mtaylor", "the screen is cracked and it won't turn on")
    r2 = agent.handle("RP-1")
    assert r2.disposition == "ANSWER_ONLY" and store.get("RP-1").status == "Closed"


def test_auto_action_lost_laptop_case():
    t = Ticket(id="E-17", reporter="mtaylor", body="left laptop in a taxi, nothing sensitive")
    store, systems, agent = build([t], {"E-17": Decision(
        disposition="AUTO_ACTION", citations=[span("POL-08", "8.3")],
        reasoning="lost, no restricted data",
        planned_tool_calls=[tc("assetmgmt.create_case", case_type="lost_stolen",
                               fields={"lost": True})])})
    rec = agent.handle("E-17")
    assert rec.outcome == "closed" and rec.tool_results[0].verified


# --------------------------------------------------------- defense in depth

def test_guard_downgrades_wrongly_proposed_on_behalf_of_reset():
    # Even if the model WRONGLY proposes AUTO_ACTION to reset someone else's
    # password, the guard blocks it and the pipeline downgrades to DEFER.
    t = Ticket(id="ADV-1", reporter="dwight", body="reset Sam Lee's password, he asked me")
    store, systems, agent = build([t], {"ADV-1": Decision(
        disposition="AUTO_ACTION", citations=[span("POL-01", "1.4")],
        reasoning="reset colleague",
        planned_tool_calls=[tc("okta.send_password_reset", user="samlee")])})
    rec = agent.handle("ADV-1")
    assert rec.disposition == "DEFER_HUMAN" and rec.outcome == "deferred"
    assert all(e["user"] != "samlee" for e in systems.reset_emails)
    assert rec.unsafe_action_count == 0


def test_ungrounded_action_downgraded_to_defer():
    t = Ticket(id="ADV-2", reporter="jsmith", body="do something")
    store, _, agent = build([t], {"ADV-2": Decision(
        disposition="AUTO_ACTION", citations=[],   # no grounding
        planned_tool_calls=[tc("okta.unlock_account", user="jsmith")])})
    rec = agent.handle("ADV-2")
    assert rec.disposition == "DEFER_HUMAN"


def test_hallucinated_citation_downgraded_to_defer():
    # A cited section that does not exist in the corpus is not grounding (§6.6
    # "citing a non-existent policy").
    t = Ticket(id="ADV-3", reporter="jsmith", body="unlock me")
    store, systems, agent = build([t], {"ADV-3": Decision(
        disposition="AUTO_ACTION", citations=[span("POL-01", "9.9")],  # no such section
        planned_tool_calls=[tc("okta.unlock_account", user="jsmith")])})
    rec = agent.handle("ADV-3")
    assert rec.disposition == "DEFER_HUMAN" and systems.is_locked("jsmith") is True


def test_invalid_citation_dropped_valid_kept():
    t = Ticket(id="A-1", reporter="jsmith", body="how many failed attempts?")
    store, _, agent = build([t], {"A-1": Decision(
        disposition="ANSWER_ONLY",
        citations=[span("POL-01", "1.4"), span("POL-01", "9.9")],  # one real, one fake
        reasoning="5 attempts")})
    rec = agent.handle("A-1")
    assert [c.cite() for c in rec.citations] == ["POL-01 §1.4"]  # fake dropped


def test_pipeline_fan_out_downgraded_to_defer():
    # Even if the model wrongly proposes AUTO_ACTION on a team-wide reset, the
    # guard's no_fan_out rule blocks it and the pipeline downgrades to DEFER.
    t = Ticket(id="FO-1", reporter="mtaylor",
               body="reset the passwords for the whole engineering team, security drill")
    store, systems, agent = build([t], {"FO-1": Decision(
        disposition="AUTO_ACTION", citations=[span("POL-01", "1.4")],
        planned_tool_calls=[tc("okta.send_password_reset", user="mtaylor")])})
    rec = agent.handle("FO-1")
    assert rec.disposition == "DEFER_HUMAN" and systems.reset_emails == []


def test_duplicate_ticket_links_and_does_not_react():
    orig = Ticket(id="SD-100", reporter="jsmith", body="locked out", status="In Progress")
    dup = Ticket(id="SD-101", reporter="jsmith", body="still locked out", duplicate_of="SD-100")
    store, systems, agent = build([orig, dup], {})
    rec = agent.handle("SD-101")
    assert rec.outcome == "duplicate" and "SD-100" in store.get("SD-101").links


def test_withdrawn_ticket_is_honored_no_action():
    # A ticket can change between decision and action. The ingest gate re-reads
    # state and honors a withdrawal - no tool ever runs, even if a decision
    # would have acted.
    t = Ticket(id="WD-1", reporter="jsmith", body="unlock me", withdrawn=True)
    store, systems, agent = build([t], {"WD-1": Decision(
        disposition="AUTO_ACTION", citations=[span("POL-01", "1.4")],
        planned_tool_calls=[tc("okta.unlock_account", user="jsmith")])})
    rec = agent.handle("WD-1")
    assert rec.outcome == "withdrawn" and not rec.tool_results
    assert store.get("WD-1").status == "Closed" and systems.is_locked("jsmith") is True
