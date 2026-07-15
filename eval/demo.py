"""Readable single-ticket trace - for the Loom walkthrough and quick manual
checks. Runs one ticket through the real pipeline and prints, step by step, the
retrieved policy, the decision, each guarded tool call with its verify result,
and the resulting JIRA state.

Usage:
    python -m eval.demo E-04                       # a worked example by id
    python -m eval.demo E-13                        # the injection refusal
    python -m eval.demo --examples eval/adversarial.json ADV-ONBEHALF
    python -m eval.demo --reporter jsmith --body "I'm locked out of my own account"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.config import SETTINGS
from agent.llm import build_llm
from agent.pipeline import Agent
from agent.retriever import Retriever
from mock.seed import seed_systems
from mock.ticket_store import MockTicketStore, Ticket

BAR = "=" * 74


def _load(examples_path: str, ticket_id: str) -> dict | None:
    for ex in json.loads(Path(examples_path).read_text(encoding="utf-8")):
        if ex["id"] == ticket_id:
            return ex
    return None


def run(ticket: Ticket, provider: str, model: str, expected: str | None):
    systems = seed_systems()
    store = MockTicketStore()
    store.add(ticket)
    retriever = Retriever.from_dir("policies")
    agent = Agent(store, systems, retriever, build_llm(provider, model))

    print(BAR)
    print(f"TICKET {ticket.id}  (reporter: {ticket.reporter})")
    print(f'  "{ticket.body}"')

    relevant = retriever.search(ticket.body, top_k=3)
    print("\nRETRIEVE (top policy spans):")
    for s in relevant:
        print(f"  {s.cite():<12} {s.text[:64]}")

    rec = agent.handle(ticket.id)

    print("\nDECIDE (LLM proposes):")
    print(f"  disposition : {rec.disposition}")
    print(f"  citations   : {', '.join(c.cite() for c in rec.citations) or '-'}")
    print(f"  reasoning   : {rec.reasoning[:200]}")

    print("\nGUARD + EXECUTE (only place tools fire):")
    if rec.tool_results:
        for t in rec.tool_results:
            flag = "VERIFIED" if t.verified else "UNVERIFIED"
            replay = " (idempotent replay)" if t.idempotent_replay else ""
            print(f"  {t.tool}({_args(t.args)}) -> {t.raw_response.get('status', 'ok')} "
                  f"[{flag}]{replay}")
    else:
        print("  (no system-mutating tools executed)")
    for n in rec.notes:
        print(f"  note: {n}")

    final = store.get(ticket.id)
    print("\nRECORD (JIRA state):")
    print(f"  status  : {final.status}")
    if final.labels:
        print(f"  labels  : {', '.join(final.labels)}")
    if final.links:
        print(f"  links   : {', '.join(final.links)}")
    if final.comments:
        print(f"  comment : {final.comments[-1][:200]}")

    print(f"\nOUTCOME: {rec.outcome}    UNSAFE ACTIONS: {rec.unsafe_action_count}")
    if expected:
        ok = "MATCH" if rec.disposition == expected else "differs"
        print(f"EXPECTED: {expected}  ({ok})")
    print(BAR)


def _args(a: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in a.items())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("ticket_id", nargs="?", help="id in the examples file")
    ap.add_argument("--examples", default="eval/worked_examples.json")
    ap.add_argument("--reporter")
    ap.add_argument("--body")
    ap.add_argument("--provider", default=SETTINGS.provider)
    ap.add_argument("--model", default=SETTINGS.model)
    args = ap.parse_args()

    if args.body:
        t = Ticket(id=args.ticket_id or "DEMO-1", reporter=args.reporter or "jsmith",
                   body=args.body)
        run(t, args.provider, args.model, None)
    else:
        ex = _load(args.examples, args.ticket_id)
        if ex is None:
            raise SystemExit(f"ticket {args.ticket_id} not found in {args.examples}")
        t = Ticket(id=ex["id"], reporter=ex["reporter"], body=ex["body"])
        run(t, args.provider, args.model, ex.get("expected"))
