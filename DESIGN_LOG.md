# Design Log

A phase-by-phase record of *what* we built and *why*, in plain language. This is
the "defend the system in review" companion to the code. NOTES.md is the spec,
BUILD_PLAN.md is the plan, CLAUDE.md is the invariants; this file explains the
reasoning behind the implementation so any decision can be justified out loud.

Read order for a reviewer: this file top to bottom. Each phase says what exists,
why it is shaped that way, and the questions it is designed to answer.

---

## The one idea the whole system is built on

**The LLM proposes; deterministic code disposes.**

The model never touches a real tool. It reads a ticket plus retrieved policy
spans and returns a structured `Decision` (which of six dispositions, which
tools it wants to call, which policy it cites, its reasoning). A separate,
dumb, un-foolable layer - the guard - is the only place a real action fires,
and it re-checks every hard safety rule against real system state before it
does.

Why this matters: the assignment's heaviest-weighted axis is action safety, and
they test it adversarially by reading the code and inventing tickets that try to
trigger unauthorized actions. If safety lived in the prompt, a clever ticket
could talk the model out of it. Because safety lives in deterministic code, a
fooled or forgetful model still cannot cause an unsafe action. Everything below
serves that split.

---

## Phase A - knowledge base, mocks, seed data

Files: `policies/POL-01..10.md`, `mock/systems.py`, `mock/ticket_store.py`,
`mock/seed.py`, plus scaffolding (`.gitignore`, `.env.example`,
`requirements.txt`).

### What exists
- **The 10 policies** as individual markdown files. These are the only
  authorized source of truth; every answer and action must cite one.
- **`MockSystems`** - one in-memory object holding all privileged-system state
  (Okta, ServiceNow, IAM, SOC, Directory, endpoint/Make-Me-Admin, asset
  management). State is plain dicts. Every state-changing method takes an
  `idempotency_key` and every method that mutates a user has a read-only
  counterpart so the guard can verify effects (`is_locked`, `mfa_enabled`,
  `request_exists`, `case_registered`, `incident_exists`, ...).
- **`TicketStore`** - the JIRA surface behind an adapter. `MockTicketStore` is
  the in-memory backend; `JiraCloudStore` is a documented stub showing where a
  real free JIRA Cloud project plugs in.
- **`seed.py`** - the fixtures the worked examples and adversarial demos need.

### Why it is shaped this way

**Why mock everything, and why is JIRA behind an adapter?**
The brief hard-requires mocking the privileged systems (never point an agent at
real Okta or prod IAM). It only *permits* real JIRA ("may back the ticket
surface"). So we mock privileged systems outright, and put JIRA behind a
`TicketStore` protocol so mock-now / real-JIRA-later is a config swap, not a
rewrite. That adapter also reads as clean engineering (tool abstraction is an
explicit rubric item).

**Why one `idempotency` ledger inside the mock?**
"Idempotent if you pass the documented key" is the environment's contract. We
model it literally: `_idempotent(key, produce)` runs the effect once per key and
returns the stored result (flagged `idempotent_replay`) on any repeat. This is
what makes a retried or duplicated ticket safe - the second call is a no-op.

**Why read-only verify helpers?**
One mock endpoint silently no-ops (returns success without effect). The only way
to catch that is to re-read state after acting. So every mutation has a matching
query the guard can call to confirm the change really happened.

### The two deliberate failure modes (built here, handled later)
The brief asks us to simulate two failures and show the agent handles them:
1. **Silent no-op.** The seeded account `noopuser` is flagged
   `silent_noop_unlock`: `okta_unlock_account` returns `{"status":"success"}`
   but leaves `locked=True`. The guard's verify step catches the lie.
2. **Step-2 failure.** `assetmgmt_create_case` is two steps: create the case
   (committed), then register it in the CMDB. For the seeded asset `ASSET-FAIL`
   step 2 raises `Step2Failure`, carrying the partial case id. The tool leaves
   the half-done state on purpose so a handler must roll back or flag rather
   than report success. We expose `delete_case` as the rollback.

We deliberately put the failure *state* in the mock but the *handling* in the
guard/handlers - that mirrors reality (the environment misbehaves; the agent
copes) and keeps the demo controllable via seed flags.

### Seed identities (so authz has something to check)
A small directory with managers and data owners, plus accounts wired to the
worked examples: `jsmith` genuinely locked (E-04, safe unlock), `pjones`
flagged `mfa_fatigue` (E-10, the disguised attack), `dwight` requesting a reset
for `samlee` he has no authority over (E-15), `noopuser`/`ASSET-FAIL` for the
failure modes, and an in-flight ticket `SD-100` with a duplicate `SD-101` for
the idempotency demo.

---

## Phase B - the deterministic guard (the graded core)

Files: `agent/models.py`, `agent/tools.py`, `agent/guard.py`, and
`tests/test_guard.py` / `test_idempotency.py` / `test_failure_modes.py`
(16 tests, all passing).

### What exists
- **`models.py`** - the typed objects: `Decision` (what the LLM returns),
  `ToolResult` (what one execution produced, including `verified`), and
  `AuditRecord` (one per ticket; the log line, CSV row, and JSON trace all
  derive from it).
- **`tools.py`** - the tool registry. Each `Tool` row carries its risk class,
  declared preconditions, idempotency recipe, and verify function.
- **`guard.py`** - `guarded_execute`, the only place a real action fires, plus
  the `PRECHECKS` table and the risk-class gate.

### The tool registry - a tool's whole safety contract in one row

Every tool is one row that declares four things as data:

```python
"okta.unlock_account": Tool(
    risk="GREEN*",
    requires=["authorized", "risk_signals_clear"],   # preconditions (names)
    idem=_unlock_key(s),                              # account + lock epoch
    verify=_v_unlocked,                               # re-read: is it unlocked?
    fn=s.okta_unlock_account),
```

Why declarative and not hardcoded `if` branches in the guard:
- A tool's safety rules are visible in one place a reviewer can read at a glance.
- Onboarding an 11th tool is one row (plus one function only if it needs a
  genuinely new *kind* of check). The guard's control flow never changes. This
  is exactly the "onboard tool #11" story the rubric asks about.
- The guard stays tiny and auditable because it contains no per-tool logic.

The four risk classes are enforced as a floor:
- **GREEN** - may run once its preconditions pass.
- **GREEN\*** (only `okta.unlock_account`) - GREEN, but its
  `risk_signals_clear` precondition can promote it to a refusal in context. The
  asterisk is a reminder that the class is a floor, not a ceiling.
- **AMBER** (`iam.grant_access`, `okta.disable_mfa`) - never runs inline. Only a
  handler may *draft* it into `iam.create_approval`.
- **RED** (`soc.*`) - only during an incident escalation.

### How arbitrary chains of tool calls are handled

There is no fixed "chain" hardcoded anywhere. A chain is two separate concerns,
handled in two places:

1. **Preconditions that must hold before a tool fires** (safety). These are the
   per-tool `requires=[...]`, enforced by a generic loop in the guard. The
   "check risk signals before unlocking" rule from the brief is just one entry
   (`risk_signals_clear`) among many, not a special case. Add N such rules by
   adding N registry entries.
2. **Multi-step actions a disposition performs** (workflow). These are the
   ordered `planned_tool_calls` the LLM proposes; a handler walks the list, runs
   each through the same guard, verifies after each step, and rolls back or
   flags on partial failure. Chain length does not matter - the handler just
   iterates.

Crucially, the LLM's proposed order is never trusted for safety. Even if the
model forgets to propose the risk check, the guard's `risk_signals_clear`
precondition blocks the unlock anyway.

### PRECHECKS - preconditions as data

`PRECHECKS` maps a precondition name to a small function that queries real
state and returns True (allowed) or False (blocked):

```python
PRECHECKS = {
    "authorized":         _authorized,          # target user == ticket.reporter
    "risk_signals_clear": _risk_signals_clear,  # okta.risk_signals(user).clear
    "minutes_le_60":      _minutes_le_60,        # Make-Me-Admin cap (POL-04 §4.6)
}
```

The guard runs whatever a tool declared:

```python
for check_name in tool.requires:
    if not PRECHECKS[check_name](ticket, call.args, systems):
        raise Unsafe(f"{call.tool}: precondition '{check_name}' failed")
```

- **`_authorized`** enforces "acting on self." Authority claimed in the ticket
  body is never trusted. On-behalf-of without proof fails here - this is the
  guard catching E-15, the costly false positive, even if the model slipped.
- **`_risk_signals_clear`** is the E-04-vs-E-10 discriminator: it asks Okta
  directly, so a routine-looking ticket that is actually an MFA-fatigue attack
  is blocked from the unlock and pushed toward escalation.
- **`_minutes_le_60`** is an argument-level guardrail: the tool is GREEN but the
  *arguments* can make it invalid.

### The execution path in `guarded_execute`

1. Look up the tool; unknown tool -> `Unsafe`.
2. `enforce_risk_class` - AMBER blocked, RED blocked outside escalation.
3. Loop the declared preconditions; any failure -> `Unsafe` (nothing fired).
4. Fire once, passing the idempotency key for state-changing tools.
5. Verify the effect by re-reading state (skip for read-only tools). A silent
   no-op comes back `verified=False`.
6. Return a `ToolResult` (raw response, key, verified, replay flag).

Any `Unsafe` is raised *before* the tool runs, so a blocked attempt is not an
unsafe action - it is a correctly prevented one. That is why the unsafe-action
count stays 0: the guard is the thing that keeps it there.

### Idempotency recipes

Each recipe mirrors the "Idempotency key" column of the catalog. The important
one to be able to explain: `okta.unlock_account` uses **account + lock epoch**
(`f"{user}:{lock_epoch}"`). Gluing the two together gives the right behavior in
both cases - the same lockout retried is deduped (same key), but a genuinely new
lockout later gets a fresh epoch and is correctly allowed again. `test_new_
lockout_gets_new_key` proves exactly this.

### Why the guard was built before the LLM

The safety core is what is graded hardest and probed adversarially, so we
hardened it first and tested it in isolation. The tests construct proposed tool
calls directly (standing in for a fooled model) and assert the guard refuses -
no API key or LLM needed. That is why Phases A and B run at zero API cost, and
why the safety guarantee does not depend on model behavior at all.

### What the 16 tests prove
- AMBER `grant_access` and `disable_mfa` refused inline (E-07 privilege, E-13
  injection) - and MFA stays on.
- RED `open_incident` refused outside escalation, allowed inside it.
- Unlock allowed for owner + clear risk (E-04); refused on `mfa_fatigue` (E-10)
  with the account left locked.
- On-behalf-of reset refused (E-15); self reset allowed (E-16).
- Make-Me-Admin allowed at 30 min, refused at 120.
- Double unlock / duplicate request act once; new lockout gets a new key.
- Silent no-op unlock returns `verified=False`.
- Step-2 case failure surfaces a rollback id and leaves a recoverable partial.

---

## Phase C - retrieval, decider, handlers, pipeline

Files: `agent/config.py`, `agent/llm.py`, `agent/retriever.py`,
`agent/decider.py`, `agent/redaction.py`, `agent/context.py`,
`agent/handlers.py`, `agent/pipeline.py`, and `tests/test_pipeline.py`
(26 tests total, all passing).

### What exists
- **`llm.py`** - the provider-agnostic seam. `LLMClient` is a one-method
  protocol (`decide(system, user, tag) -> Decision`). `AnthropicLLM` is the real
  decider; `StubLLM` is a deterministic test double backed by a `{tag: Decision}`
  table. `build_llm(provider, model)` picks one. The default provider is
  `anthropic` only if a key is present, else `stub`, so nothing breaks without a
  key.
- **`retriever.py`** - splits the 10 policy files into cited sections
  (`POL-NN §N.N`) and ranks them against a ticket by token overlap.
- **`decider.py`** - the system prompt (six dispositions, risk-class rules,
  restraint-first order, cite-only-from-provided-spans) plus the user-prompt
  builder, and one `decide()` call.
- **`redaction.py`** - masks secret shapes before any agent-written text leaves.
- **`handlers.py`** - the six disposition handlers.
- **`pipeline.py`** - the five-stage orchestrator (`Agent.handle`).

### Why the SDK, not Managed Agents (the earlier question, in code)
`AnthropicLLM.decide` uses `messages.parse` with `output_format=Decision`, so the
model is *forced* to return a schema-valid Decision and nothing else. That is the
whole propose/dispose seam made concrete: the model hands back structured data,
our code decides what runs. Managed Agents would have hosted the loop and tool
execution on Anthropic's side, hiding the graded safety layer.

### Retrieval: why we pass the full corpus, not just top-k
Keyword retrieval over 10 short policies is lossy - it missed the phishing ->
POL-09 link and the "40 MB attachment" case (short tokens like "mb", plurals
like "attachments"). Rather than tune a retriever, we exploit the fact that the
corpus is tiny (~60 one-line sections): the decider is handed the **entire**
policy corpus every ticket, with the ranked-relevant spans highlighted first as
a hint. This removes retrieval recall as a failure point - the model always has
everything it needs to cite - while the ranking hint still helps it focus. Light
token normalization (strip plural 's', keep 2-char tokens) improves the hint.
For a large real corpus this is where embeddings would go; at this scale they add
cost and opacity for no benefit, and "onboard policy #11" stays "drop a file in."

### Grounding is enforced twice
1. The prompt forbids answering/acting from prior knowledge and requires a
   citation from the provided spans.
2. The pipeline re-checks: if a disposition that asserts an answer or action
   (`ANSWER_ONLY` / `AUTO_ACTION` / `PROPOSE_FOR_APPROVAL`) comes back with no
   citation, it is downgraded to `DEFER_HUMAN`. Structural, not trust-based.
   (`ESCALATE_INCIDENT` is deliberately not force-deferred on a missing citation:
   sitting on a suspected breach is worse than escalating uncited. Documented
   judgment call.)

### The handlers - each produces exactly its artifact
- `answer_only`: cited comment, close. No mutation.
- `auto_action`: run the proposed GREEN chain through the guard, verify each; on
  `Unsafe` downgrade to DEFER (never force it through); on partial/step-2 failure
  roll back what we can and flag; on success, cited "done" comment + close.
- `propose_for_approval`: run the chain; a proposed AMBER grant is refused inline
  by the guard and noted as correct; `iam.create_approval` routes it; leave
  pending. Never executes the privileged change.
- `escalate_incident`: run the chain with `in_escalation=True` (so RED tools are
  permitted), give the POL-09 §9.2 containment instruction, never close.
- `ask_clarification`: one question, "Waiting for Customer", label.
- `defer_human`: reason + route. No action.

### The pipeline glue
`Agent.handle` runs the five stages, plus two gates: an **ingest gate** that
honors withdrawals and links duplicates without re-acting (idempotency at the
ticket level), and the **grounding gate** above. Finally it computes
`unsafe_action_count` defensively from the executed tool results (any AMBER
executed, or RED outside escalation) - which is always 0 because the guard
prevents those from ever firing.

### Why a StubLLM
The whole pipeline and all six handlers are tested deterministically with no API
key and no cost. The stub stands in for the model's proposal; the tests assert
the handlers and guard behave correctly regardless of what was proposed -
including the adversarial case where the stub *wrongly* proposes an AUTO_ACTION to
reset someone else's password, and the guard downgrades it to DEFER with no reset
sent. The real graded eval (Phase D) swaps in `AnthropicLLM` with no code change.

### What the Phase C tests add (10 more, 26 total)
All six dispositions end to end; propose routes while refusing the AMBER grant
inline; escalate contains and stays open; the guard downgrades a wrongly-proposed
on-behalf-of reset; an ungrounded action is downgraded to DEFER; a duplicate
ticket links without re-acting. Every record reports `unsafe_action_count == 0`.

---

## Anticipated review questions (and the honest answers)

**"Where is safety enforced - prompt or code?"** Code. `agent/guard.py`. The
prompt guides the model's proposal; the guard independently re-verifies every
hard rule against real state and is the only path to execution.

**"What stops the model disabling MFA if a ticket tricks it?"** `disable_mfa` is
AMBER. `enforce_risk_class` raises before it can run. The real function is never
reached. `test_amber_disable_mfa_blocked_even_if_llm_asks` demonstrates it.

**"How do you tell E-04 from E-10 - they read the same?"** We do not rely on the
model reading them differently. The guard calls `okta.risk_signals` and acts on
the fact: clear -> unlock allowed, `mfa_fatigue` -> unlock blocked.

**"How would you add an 11th tool?"** One row in the registry with its risk
class, `requires`, idempotency recipe, and verify. Only if it needs a brand-new
kind of precondition do you add one function to `PRECHECKS`. No guard changes.

**"How do you handle a chain of five tool calls, not just two?"** The handler
walks the proposed list, running each through the same guard, verifying after
each, and rolling back or flagging on partial failure. Length is irrelevant;
preconditions are enforced per tool regardless of order.

**"How do you avoid claiming success you did not achieve?"** Verify-after-fire.
Every mutation is re-read from state; a silent no-op returns `verified=False`
and the handler must not report success.

**"Why not use Anthropic Managed Agents?"** That would host the agent loop and
tool execution inside Anthropic's platform, hiding the safety-critical gating
that this assignment grades. We use the plain SDK so the model only returns a
structured decision and our own inspectable code decides what executes.
