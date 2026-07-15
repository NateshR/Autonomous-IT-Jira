# Autonomous IT Helpdesk Agent (JIRA)

An AI agent that monitors a JIRA Service Desk for Helix Industries (a regulated
company: SOX, HIPAA, GDPR) and, for each ticket, chooses one of six dispositions
and executes the correct action(s) against mocked enterprise systems (Okta,
ServiceNow, IAM, SOC). The design goal is **judgment under the ability to act**:
an over-eager agent that takes a wrong action is far more dangerous than one that
answers a question wrong, so safety and restraint are enforced structurally.

## Results

- **17 worked examples:** 14/17 disposition accuracy, **0 unsafe actions**. All 3
  misses are conservative DEFER on legitimate GREEN self-service (the safe
  direction; a missed AUTO_ACTION is far cheaper than a false-positive action).
- **6 adversarial tickets** (injection, asserted authority, on-behalf-of, fan-out,
  fake approval, leaked secret): **6/6 correct, 0 unsafe actions**, secret fully
  redacted (0 occurrences in the stored trace).

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY

pytest                        # 26 tests - guard safety, no API key needed
python -m eval.run_eval       # 17 examples through claude-opus-4-8
python -m eval.run_eval --examples eval/adversarial.json   # attack suite
python -m eval.idempotency_demo                            # retry+dup act once (no key)
python -m eval.demo E-04      # readable single-ticket trace (Loom-ready)
python -m eval.demo E-13      # a privileged request refused and routed
```

Try your own tickets: put them in a JSON file (same shape as
`eval/worked_examples.json`) and run `python -m eval.run_eval --examples FILE`.
Reporter names you invent are auto-provisioned in the mock directory.

## Architecture

Five stages, identical for every ticket:

```
INGEST -> RETRIEVE -> DECIDE (LLM) -> GUARD + EXECUTE -> RECORD
```

The one load-bearing idea: **the LLM proposes; deterministic code disposes.** The
model reads the ticket plus retrieved policy and returns a schema-validated
`Decision` (disposition, citations, planned tool calls, reasoning). It never
touches a tool. A separate, deterministic **guard** is the only place a real
action fires, and it re-checks every hard rule against real system state first.
A fooled or forgetful model therefore cannot cause an unsafe action.

- `agent/decider.py` - builds the grounded prompt, gets a `Decision` via
  `messages.parse` (schema-constrained output).
- `agent/guard.py` - `guarded_execute`: risk-class gate + declarative
  preconditions + idempotency key + post-action verify. The safety core.
- `agent/tools.py` - the tool registry: each tool's risk class, preconditions,
  idempotency recipe, and verify function in one row.
- `agent/handlers.py` - one handler per disposition; each produces exactly its
  required artifact and drives execution through the guard.
- `agent/pipeline.py` - the orchestrator plus the ingest and grounding gates.
- `mock/` - in-memory Okta/ServiceNow/IAM/SOC/Directory + a `TicketStore` adapter
  (mock JIRA now, real JIRA Cloud a config swap). Includes idempotency keys and
  two deliberate failure modes.

## Prompt strategy

A single schema-constrained call returns a `Decision`. The system prompt encodes
the six dispositions, the risk classes, a restraint-first reasoning order (rule
out reasons NOT to act before reasons to act), and the hard rule to cite only
from the policy spans provided. Adaptive thinking is on for the judgment calls.
Because there are only ~60 short policy lines, the **entire policy corpus** is
passed on every ticket with the ranked-relevant spans highlighted first, which
removes retrieval recall as a failure point.

## How grounding is enforced

Two layers. (1) The prompt forbids answering or acting from prior knowledge and
requires a citation from the provided spans. (2) The pipeline re-checks: any
disposition that asserts an answer or action (ANSWER_ONLY / AUTO_ACTION /
PROPOSE_FOR_APPROVAL) with no citation is downgraded to DEFER_HUMAN. Structural,
not trust-based. (ESCALATE is not force-deferred on a missing citation - sitting
on a suspected breach is worse than escalating uncited.)

## The act-vs-instruct line, and why

The agent **acts** only on GREEN tools that are grounded, authorized, verified,
and reversible - and only on the requester's own account. Everything
privileged/irreversible is **routed** (AMBER -> `iam.create_approval`) and
security-sensitive tickets are **escalated** (RED -> SOC), never resolved. The
guard enforces the line in code, not prose:

- **AMBER** (`iam.grant_access`, `okta.disable_mfa`) is structurally unreachable
  inline - a handler can only draft it into an approval.
- **RED** (`soc.*`) runs only during an escalation.
- **GREEN** runs only after its declared preconditions pass. Notably
  `okta.unlock_account` requires `risk_signals_clear`, so the same unlock that is
  right for a genuine lockout (E-04) is refused when it is an MFA-fatigue attack
  (E-10). The class is a floor, not a ceiling.

Authorization, not just identity: a user-affecting action requires the target to
be the requester. Authority asserted in a ticket ("my manager said it's fine") is
never trusted. On-behalf-of without proof fails closed (E-15, the costly false
positive).

## Idempotency and recovery

Every state-changing tool call carries the documented idempotency key (e.g.
unlock uses account + lock epoch), so a retry or duplicate acts once; duplicate
tickets are linked, not re-acted. After every action the effect is re-read from
state (verify) - the deliberate silent no-op mock returns `verified=False` and is
never reported as success. Multi-step failures roll back the committed step and
flag rather than claim a half-done success. See `eval/idempotency_demo.py`.

## What I would harden before production

- Real identity provider + a persistent idempotency store (survives restarts).
- A human review queue and richer approver routing (SoD, delegation).
- Retrieval quality + a real confidence threshold once the corpus is large.
- Blast-radius caps enforced in code (recognize and refuse fan-out requests).
- Stronger secret detection (entropy + context) and per-tenant audit retention.
- Full observability on the audit trace; per-ticket rate limits and backoff.

## Deployment judgment

**Onboarding policy #11 or tool #11.** A new policy is a file dropped into
`policies/` - grounding is retrieval-driven, so no code change for pure
knowledge. A new tool is one row in `agent/tools.py` (risk class, preconditions,
idempotency recipe, verify); only a genuinely new *kind* of precondition adds one
function to `guard.PRECHECKS`. The guard's control flow never changes.

**Healthcare vs fintech.** Same architecture, different policy corpus plus
risk-class and approver tuning. Healthcare (HIPAA/PHI) tightens data-handling and
geography rules and pushes more PHI-exposure tickets to RED escalation. Fintech
(SOX/PCI) tightens privileged access and change control - more AMBER routing and
segregation-of-duties on approvals. For a new customer you swap the policy files,
adjust each tool's risk class/preconditions to their control framework, and wire
their approver directory; the decision engine and guard are unchanged.

## Repo layout

```
agent/   pipeline, decider, guard, tools, handlers, retriever, llm, redaction, audit, models
mock/    systems (Okta/ServiceNow/IAM/SOC/Directory), ticket_store adapter, seed, failure modes
policies/ POL-01..10  (the only authorized knowledge source)
eval/    worked_examples.json, adversarial.json, run_eval, demo, idempotency_demo
tests/   guard safety, idempotency, failure modes, full pipeline (26 tests)
```

Known seeded usernames (for authz in test tickets): `jsmith`, `mtaylor`,
`pjones`, `rkumar`, `lchen`, `dwight`, `samlee`, `asmith` (a manager),
`dbowner`/`pricingowner` (data owners). Invented reporters are auto-provisioned.

## Notes

Privileged systems are always mocked - never point an agent at real Okta or
production IAM. Secrets stay out of the repo (`.env` is gitignored). NOTES.md is
the captured spec; BUILD_PLAN.md is the phased plan.
