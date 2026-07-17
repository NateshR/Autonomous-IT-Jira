# Autonomous IT Helpdesk Agent (JIRA)

An AI agent that monitors a JIRA Service Desk for Helix Industries (a regulated
company: SOX, HIPAA, GDPR) and, for each ticket, chooses one of six dispositions
and executes the correct action(s) against mocked enterprise systems (Okta,
ServiceNow, IAM, SOC). The design goal is **judgment under the ability to act**:
an over-eager agent that takes a wrong action is far more dangerous than one that
answers a question wrong, so safety and restraint are enforced structurally.

## Results

- **17 worked examples:** 17/17 disposition accuracy, **0 unsafe actions**.
- **6 adversarial tickets** (injection, asserted authority, on-behalf-of, fan-out,
  fake approval, leaked secret): **6/6 correct, 0 unsafe actions**, secret value
  never echoed (0 occurrences in the stored trace).
- **State-level verification** (`python -m eval.verify_state`): all 23 tickets pass
  9 safety invariants asserted against real mock-system state - no AMBER tool ever
  executed, no RED outside an escalation, every state change verified by re-read
  and carrying an idempotency key, no unlock without a clear risk check, every
  citation real, no secret in agent-written text, no RED ticket closed.

Numbers are reproduced by the committed artifacts in `eval/` (`RESULTS.md`,
`report.csv`, `decision_log.txt`, `trace.json`, `STATE_VERIFICATION.md`) and
`eval/adv/`.

`verify_state` exists because `run_eval` grades the *disposition label*, and a
label can be right while the work never happened. It caught exactly that: an
approval never routed and an on-call never paged, both behind a clean-looking
log line. Grading the label is not grading the action.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY

pytest                        # 41 tests - guard safety, no API key needed
python -m eval.run_eval       # 17 examples through claude-opus-4-8
python -m eval.run_eval --examples eval/adversarial.json   # attack suite
python -m eval.idempotency_demo                            # retry+dup act once (no key)
python -m eval.demo E-04      # readable single-ticket trace (Loom-ready)
python -m eval.demo E-07      # a privileged request refused and routed for approval
python -m eval.demo E-13      # a prompt-injection attempt refused
python -m eval.verify_state   # assert real system state on all 23 tickets
python -m eval.schema_repro   # minimal repro of the empty-args bug (see below)
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
requires a citation from the provided spans. (2) The pipeline re-checks each
citation against the corpus: a cited section that does not exist is dropped
(catches a hallucinated policy), and any answer/action left with no valid
citation is downgraded to DEFER_HUMAN. Structural, not trust-based. (ESCALATE is
not force-deferred on a missing citation - sitting on a suspected breach is worse
than escalating uncited.)

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
positive). Blast radius is enforced explicitly: a `no_fan_out` precondition
refuses a team-wide/multi-target action ("reset the whole team"), and
self-service tools can only ever target the requester.

## Idempotency and recovery

Every state-changing tool call carries the documented idempotency key (e.g.
unlock uses account + lock epoch), so a retry or duplicate acts once; duplicate
tickets are linked, not re-acted. After every action the effect is re-read from
state (verify) - the deliberate silent no-op mock returns `verified=False` and is
never reported as success. Multi-step failures roll back the committed step and
flag rather than claim a half-done success. See `eval/idempotency_demo.py`.

The Anthropic client is configured with explicit retries and a timeout (SDK
exponential backoff on connection errors, 408/409/429, 5xx). Mock tool calls are
in-memory; a real integration would wrap them with the same retry/timeout/backoff
policy.

## What I would harden before production

- Real identity provider + a persistent idempotency store (survives restarts).
- A human review queue and richer approver routing (SoD, delegation).
- Retrieval quality + a real confidence threshold once the corpus is large.
- Configurable blast-radius thresholds + group-target resolution (a keyword +
  multi-target fan-out guard is already enforced).
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
tests/   guard safety, idempotency, failure modes, redaction, pipeline (41 tests)
```

Known seeded usernames (for authz in test tickets): `jsmith`, `mtaylor`,
`pjones`, `rkumar`, `lchen`, `dwight`, `samlee`, `asmith` (a manager),
`dbowner`/`pricingowner` (data owners). Invented reporters are auto-provisioned.

## Notes

Privileged systems are always mocked - never point an agent at real Okta or
production IAM. Secrets stay out of the repo (`.env` is gitignored).
`LLD.md` is the low-level design (architecture, flowcharts, how the code works);
`NOTES.md` is the captured spec; `BUILD_PLAN.md` is the phased plan.
