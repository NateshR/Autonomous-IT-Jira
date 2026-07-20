# Autonomous IT Helpdesk Agent (JIRA)

An AI agent that monitors a JIRA Service Desk for Helix Industries (a regulated
company: SOX, HIPAA, GDPR) and, per ticket, chooses one of six dispositions and
executes the correct action(s) against mocked enterprise systems (Okta,
ServiceNow, IAM, SOC). The design goal is **judgment under the ability to act**:
an over-eager agent that takes a wrong action is far more dangerous than one that
answers a question wrong, so restraint is enforced structurally, not by prompting.

**[5-minute walkthrough](https://drive.google.com/file/d/1OwnA7A7xtBs6Ltx09-hVxSb8SlkChtWN/view?usp=sharing)** - one action executed end-to-end (E-04,
self-service unlock) and one privileged request refused and routed (E-07, prod
DBA admin -> `iam.create_approval`).

## Results

- **Worked examples: 17/17**, **0 unsafe actions**.
- **Adversarial suite: 6/6**, **0 unsafe actions** (injection, asserted
  authority, on-behalf-of, fan-out, fake approval, leaked secret).
- **State verification: 23/23 tickets**, 0 failures across 9 safety invariants
  asserted against real mock-system state.
- 46 tests, no API key needed.

Reproduced by the committed artifacts in `eval/` and `eval/adv/` (`RESULTS.md`,
`report.csv`, `decision_log.txt`, `trace.json`, `STATE_VERIFICATION.md`).

`verify_state` exists because `run_eval` grades the *disposition label*, and a
label can be right while the work never happened - see the bug below.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # add your ANTHROPIC_API_KEY

pytest                        # 46 tests - guard safety, no API key needed
python -m eval.run_eval       # 17 worked examples
python -m eval.run_eval --examples eval/adversarial.json --out eval/adv
python -m eval.verify_state   # assert real system state on all 23 tickets
python -m eval.idempotency_demo   # retry + duplicate act once (no key)
python -m eval.demo E-04      # one action end-to-end
python -m eval.demo E-07      # a privileged request refused AND routed
python -m eval.schema_repro   # the empty-args bug, in two API calls
```

Try your own tickets: a JSON file shaped like `eval/worked_examples.json`, then
`python -m eval.run_eval --examples FILE`. Invented reporters are
auto-provisioned in the mock directory.

## Architecture

```
INGEST -> RETRIEVE -> DECIDE (LLM) -> GUARD + EXECUTE -> RECORD
```

The one load-bearing idea: **the LLM proposes; deterministic code disposes.** The
model returns a schema-validated `Decision` (disposition, citations, planned tool
calls, reasoning) and never touches a tool. A separate deterministic **guard**
(`agent/guard.py`) is the only place a real action fires, and it re-checks every
rule against real system state first. A fooled or forgetful model therefore
cannot cause an unsafe action.

Each tool's whole safety contract is one row in `agent/tools.py`: risk class,
preconditions, idempotency recipe, verify function. Adding a rule is one entry in
`guard.PRECHECKS`; the guard's control flow never changes. `LLD.md` has the
component map, flowcharts and a full worked trace.

## Prompt strategy

A single schema-constrained call returns a `Decision`. The system prompt encodes
the six dispositions, the risk classes, a restraint-first reasoning order (rule
out reasons NOT to act before reasons to act), and the rule to cite only from the
spans provided. The tool catalog is rendered from the registry, so the model's
menu and the guard's enforcement come from one object. Because the corpus is only
~60 short lines, **all of it** is passed every ticket with ranked spans first,
which removes retrieval recall as a failure mode. That trick dies above a few
hundred spans; at scale this needs hybrid search plus a reranker.

## How grounding is enforced

Two layers. The prompt forbids answering from prior knowledge and requires a
citation. Then the pipeline **validates every citation against the corpus**: a
section that does not exist is dropped, and an answer or action left with no
valid citation is downgraded to DEFER_HUMAN. Structural, not trust-based.
ESCALATE is exempt - sitting on a suspected breach is worse than escalating
uncited.

## The act-vs-instruct line, and why

The agent **acts** only on GREEN tools that are grounded, authorized, verified
and reversible, and only on the requester's own account. Privileged or
irreversible work is **routed** (AMBER -> `iam.create_approval`) and
security-sensitive tickets are **escalated** (RED -> SOC), never resolved.
Enforced in code:

- **AMBER** (`iam.grant_access`, `okta.disable_mfa`) is structurally unreachable
  inline - there is no branch, flag or argument that reaches it. A handler can
  only draft it into an approval.
- **RED** (`soc.*`) runs only during an escalation.
- **GREEN** runs only after its declared preconditions pass. `okta.unlock_account`
  requires `risk_signals_clear`, so the unlock that is right for a genuine lockout
  (E-04) is refused when it is an MFA-fatigue attack (E-10). **The class is a
  floor, not a ceiling.**

Authorization, not identity: a user-affecting action requires the target to be the
requester **and** that identity to be present and active in the directory - because
`user == reporter` is true for a terminated employee too (POL-10 §10.4). Authority
asserted in a ticket is never trusted, and a claimed approval id is looked up via
`iam.get_approval` rather than believed. Blast radius is explicit: `no_fan_out`
refuses team-wide or multi-target requests, and a filed request or case cannot
name a third party as its subject.

## Idempotency and recovery

Every state-changing call carries the documented idempotency key (unlock uses
account + lock epoch), so a retry or duplicate acts once; duplicate tickets are
linked, not re-acted. The ledger is namespaced per endpoint - two tools may share
a key *recipe* but never a ledger *slot*, or the second silently returns the
first's cached response and never runs. After every action the effect is re-read
from state, so the deliberate silent-no-op mock yields `verified=False` and is
never reported as success. Multi-step failures roll back the committed step and
flag rather than claim a half-done success. See `eval/idempotency_demo.py`.

The Anthropic client sets explicit retries and a timeout (SDK exponential backoff
on connection errors, 408/409/429, 5xx). A real tool integration would wrap the
mock calls in the same policy.

## The bug that made a green eval lie

Every tool call the model proposed arrived with **no arguments**, because a
free-form `dict[str, Any]` compiles to a schema with no declared properties and
the model returns `{}` every time - whatever the prompt says. It was invisible
because the eval graded the disposition **label**: E-07 scored a match while
never routing an approval, E-09 while never opening an incident. The fix was
small; the lesson was that **grading the label is not grading the action**, which
is why `eval/verify_state.py` exists. `python -m eval.schema_repro` reproduces the
cause in two API calls; `LLD.md` §13 has the full write-up.

## What I would harden before production

- Real identity provider; persistent idempotency store (survives restarts).
- Approver routing resolved from the directory (SoD, delegation) and validated -
  today `iam.create_approval` files whatever approvers it is handed.
- Corroboration before RED: an escalation currently opens an incident on the
  model's disposition alone.
- Hybrid retrieval + reranker + a real confidence threshold once the corpus grows.
- Prompt caching (static system prompt + corpus) for cost at ticket volume.
- Stronger secret detection (entropy + context), per-tenant audit retention, full
  observability on the trace, per-ticket rate limits.

## Deployment judgment

**Onboarding policy #11 or tool #11.** A new policy is a file dropped into
`policies/` - grounding is retrieval-driven, so no code change for pure knowledge.
A new tool is one row in `agent/tools.py`; only a genuinely new *kind* of
precondition adds one function to `guard.PRECHECKS`. The guard never changes.

**Healthcare vs fintech.** Same architecture, different corpus plus risk-class and
approver tuning. Healthcare (HIPAA/PHI) tightens data-handling and geography rules
and pushes more PHI-exposure tickets to RED. Fintech (SOX/PCI) tightens privileged
access and change control - more AMBER routing, segregation of duties on
approvals. For a new customer you swap the policy files, retune each tool's risk
class and preconditions to their control framework, and wire their approver
directory; the decision engine and guard are unchanged.

## Repo layout

```
agent/    pipeline, decider, guard, tools, handlers, retriever, llm, redaction, audit, models
mock/     systems (Okta/ServiceNow/IAM/SOC/Directory), ticket_store adapter, seed, failure modes
policies/ POL-01..10 - the only authorized knowledge source
eval/     worked_examples.json, adversarial.json, run_eval, verify_state, demo,
          idempotency_demo, schema_repro + committed artifacts
tests/    guard safety, idempotency, failure modes, redaction, pipeline (46 tests)
```

## Notes

Privileged systems are always mocked - never point an agent at real Okta or
production IAM. Secrets stay out of the repo (`.env` is gitignored). `LLD.md` is
the low-level design (component map, flowcharts, worked trace); `NOTES.md` is the
captured spec; `BUILD_PLAN.md` the phased plan.
