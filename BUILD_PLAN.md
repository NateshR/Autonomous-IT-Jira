# Build Plan - Autonomous IT Helpdesk Agent

Companion to [NOTES.md](./NOTES.md) (the spec) and [CLAUDE.md](./CLAUDE.md) (the invariants). This is the implementation plan and the skeleton of the final README. Scope: core + all stretch items (maximal).

---

## 0. Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best LLM/agent ecosystem; reviewers expect it |
| LLM | Provider-agnostic interface, **default Anthropic Claude** (`claude-opus-4-8`, adaptive thinking) | Strongest structured-output + tool-use; clean abstraction reads well |
| Structured decision | `client.messages.parse()` with a Pydantic schema | Validation at the tool-call layer; the model retries on mismatch, no hand-parsing |
| JIRA surface | Mock behind a `TicketStore` adapter | Fast, zero-auth; adapter lets real JIRA Cloud slot in later. Brief permits mock (§1.2/§7 say "may") |
| Privileged systems | Always mocked (Okta/ServiceNow/IAM/SOC) | Hard requirement (§7) |
| Retrieval | Local embedding or section-index over 10 short policies (no heavy vector DB) | 10 docs do not justify a vector store; keep it inspectable |
| Secrets | `.env` (gitignored), `ANTHROPIC_API_KEY` from env | Keep secrets out of the repo |

---

## 1. Repository layout

```
autonomous-it-jira/
  agent/
    __init__.py
    pipeline.py        # the 5-stage loop (orchestrator)
    retriever.py       # policy search + citation
    decider.py         # LLM decision (provider-agnostic)
    llm.py             # LLMClient interface + Anthropic + stub impls
    guard.py           # deterministic risk-gating executor (the safety core)
    handlers.py        # one handler per disposition
    tools.py           # tool registry: name -> {risk class, idempotency recipe, fn}
    audit.py           # structured audit record -> decision log + eval report
    models.py          # Pydantic: Decision, ToolCall, AuditRecord, etc.
    redaction.py       # secret detection + redaction
    config.py          # settings, model id, thresholds
  mock/
    systems.py         # in-memory Okta/ServiceNow/IAM/SOC/Directory
    ticket_store.py    # TicketStore adapter (mock JIRA) + real-JIRA stub
    failures.py        # the two deliberate failure modes
    seed.py            # seed data (compromised acct, directory, in-flight ticket)
  policies/
    POL-01.md ... POL-10.md
  eval/
    worked_examples.json   # the 17 tickets (+ our own adversarial cases)
    run_eval.py            # runs all tickets -> report.csv + confusion matrix
    report.csv             # generated
  tests/
    test_guard.py          # unit tests for the safety gates (most important)
    test_idempotency.py
    test_failure_modes.py
  README.md
  requirements.txt
  .env.example
  .gitignore
```

---

## 2. Data models (agent/models.py)

The whole system passes typed objects. Core schemas:

```python
Disposition = Literal[
    "ANSWER_ONLY", "AUTO_ACTION", "PROPOSE_FOR_APPROVAL",
    "ESCALATE_INCIDENT", "ASK_CLARIFICATION", "DEFER_HUMAN",
]

class PolicySpan(BaseModel):
    policy_id: str        # "POL-01"
    section: str          # "1.4"
    text: str             # the quoted span

class PlannedToolCall(BaseModel):
    tool: str             # "okta.unlock_account"
    args: dict

class Decision(BaseModel):          # what the LLM returns (parsed)
    disposition: Disposition
    citations: list[PolicySpan]     # empty -> forces DEFER downstream
    planned_tool_calls: list[PlannedToolCall]
    reasoning: str

class ToolResult(BaseModel):
    tool: str
    args: dict
    idempotency_key: str | None
    raw_response: dict
    verified: bool                  # post-action state re-read result

class AuditRecord(BaseModel):       # one per ticket; source of log + report + trace
    ticket_id: str
    disposition: Disposition
    citations: list[PolicySpan]
    tool_results: list[ToolResult]
    reasoning: str
    unsafe_action_count: int        # must be 0
    outcome: str                    # closed / pending / escalated / waiting / deferred
```

`AuditRecord` is the single rich object; the decision log (one line), eval report (CSV row), and structured audit trace (full JSON) are all projections of it. Build it from day one -> deliverables 3, 4, and stretch 7 come almost free.

---

## 3. Component responsibilities

### pipeline.py (Stage orchestrator)
```
handle_ticket(ticket_id):
  ticket = store.get(ticket_id)          # re-read fresh (catch withdrawal/dupe)
  if is_duplicate(ticket): link + stop
  spans = retriever.search(ticket.body)
  decision = decider.decide(ticket, spans)   # LLM proposes
  record = HANDLERS[decision.disposition](ticket, decision, spans)
  audit.write(record)
  return record
```

### retriever.py
Splits the 10 policy files into `POL-NN §N.N` sections, retrieves the top-k relevant spans for a ticket. If top score is below a confidence threshold, return empty -> the decider/guard treats that as "cannot ground" -> DEFER. Grounding is enforced structurally: a disposition that needs a citation cannot proceed without one.

### llm.py (provider-agnostic)
```python
class LLMClient(Protocol):
    def decide(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel: ...

class AnthropicLLM:   # default
    # client.messages.parse(model="claude-opus-4-8",
    #   thinking={"type":"adaptive"}, output_format=schema, ...)

class StubLLM:        # deterministic, for tests/CI without a key
```
Default = Anthropic. `StubLLM` lets the guard and pipeline be tested with zero API cost.

### decider.py
Builds the system prompt (the 7-step reasoning heuristic from NOTES.md §9 + the risk-class rules + "only cite from provided spans"), passes ticket + retrieved spans, returns a validated `Decision`. The LLM only proposes; it never executes.

### tools.py (the tool registry)
Each tool is registered with its risk class and idempotency recipe from NOTES.md §3:
```python
TOOLS = {
  "okta.unlock_account": Tool(risk="GREEN*", idem=lambda t,a: f"{a['user']}:{lock_epoch(a['user'])}",
                              preflight=["risk_signals_clear"], affects_user=True, fn=mock.okta.unlock),
  "iam.grant_access":    Tool(risk="AMBER", fn=mock.iam.grant_access),   # never callable inline
  "soc.open_incident":   Tool(risk="RED",  fn=mock.soc.open_incident),
  ...
}
```

### guard.py (THE SAFETY CORE - deterministic)
Every real action passes through `guarded_execute`. This is where the grade is won:
```python
def guarded_execute(call, ticket, in_escalation=False):
    tool = TOOLS[call.tool]
    if tool.risk == "AMBER":
        raise Unsafe("AMBER cannot run inline")            # structurally blocked
    if tool.risk.startswith("RED") and not in_escalation:
        raise Unsafe("RED only during escalation")
    if tool.affects_user and not authorized(ticket.reporter, call.args):
        raise Unsafe("authorization not established")       # self vs on-behalf-of
    if call.tool == "okta.unlock_account":
        if not okta.risk_signals(call.args["user"]).clear:  # context promotes GREEN->RED
            raise Unsafe("compromise signal; escalate")
    key = tool.idem(ticket, call.args)
    resp = tool.fn(**call.args, idempotency_key=key)
    verified = verify_effect(call)                          # re-read state (silent no-op)
    return ToolResult(..., verified=verified)
```
- AMBER tools are only reachable as a *draft inside* `iam.create_approval` - never as a live call.
- `authorized()` uses `directory.lookup_user` / `verify_manager`; it ignores authority claimed in the ticket body.
- `verify_effect()` re-reads system state; if the mock silently no-op'd, `verified=False` and the handler must not report success.
- Any `Unsafe` raised increments nothing on the "did an unsafe action" counter, because the tool never fired - that is the point.

### handlers.py (one per disposition, produces the exact artifact)
- `answer_only`: comment with citation, close. No mutation.
- `auto_action`: for each planned GREEN call -> `guarded_execute` -> if any not `verified`, roll back / flag; else comment (what was done + citation) + close.
- `propose_for_approval`: draft action + approvers -> `iam.create_approval` (GREEN routing) -> comment + citation -> leave pending. Never executes the AMBER action.
- `escalate_incident`: `soc.open_incident` + `page_oncall` -> allowed GREEN containment (`revoke_sessions`/`force_password_reset`) -> POL-09 instruction to user -> never close. Redact secrets.
- `ask_clarification`: one question -> transition "Waiting for Customer" -> label.
- `defer_human`: comment reason -> route to queue. No answer, no action.

### redaction.py
Regex/entropy detection for secrets (tokens, passwords, keys) in ticket bodies; replaces with `[REDACTED]` before anything is echoed into a comment or logged.

---

## 4. Mock layer (mock/)

Keep it small (a few dozen lines per system). In-memory dicts.

- **systems.py**: `okta`, `servicenow`, `iam`, `soc`, `directory` with the endpoints the examples use, plus AMBER (`grant_access`, `disable_mfa`) and `iam.get_approval` so the guardrail can prove refusal-without-approval.
- **Idempotency**: each state-changing endpoint records `idempotency_key -> result`; a repeat key returns the stored result (no second effect).
- **failures.py**: two deliberate modes ->
  1. `silent_noop`: a designated account's `unlock` returns `{"status":"success"}` but leaves `locked=True`.
  2. `step2_fail`: a two-step action (e.g. create_case then link) where step 2 raises, so the handler must roll back / flag.
- **seed.py**: directory with managers; a normal locked account (E-04); a `mfa_fatigue`-flagged account (E-10); a lost/stolen-eligible asset; an in-flight ticket a duplicate maps to.
- **ticket_store.py**: `TicketStore` interface (`get/comment/transition/add_label/link_issues`) with a `MockTicketStore` and a `JiraCloudStore` stub (documented, not wired).

---

## 5. Build order (phased milestones)

**Phase A - Skeleton + mocks + models**
Repo scaffold, Pydantic models, mock systems with idempotency + seed data + the two failure modes, `MockTicketStore`. Deliverable: mocks importable, seed state loads.

**Phase B - The guard (do this before the LLM)**
`tools.py` registry + `guard.py` + `authorized()` + `verify_effect()`. Unit tests: AMBER refused inline, RED refused outside escalation, unlock refused when `mfa_fatigue`, unauthorized on-behalf-of refused, idempotent double-call acts once, silent no-op detected. This is the graded core; harden it first with `StubLLM`.

**Phase C - Retrieval + decider + handlers**
Policy sectioning + retrieval; `AnthropicLLM` with `messages.parse`; system prompt; six handlers; wire the pipeline end to end.

**Phase D - Audit + eval**
`AuditRecord` -> decision log (one line/ticket), `report.csv`, structured JSON trace. `eval/run_eval.py` over the 17 examples: predicted disposition + tool calls + citation + `unsafe_action_count` (assert total == 0).

**Phase E - Stretch**
Confusion matrix + per-disposition precision/recall; idempotency demo script (run an acting ticket twice, show one effect); a batch of ~10 adversarial tickets (injection, fake authority, on-behalf-of, fan-out "reset the whole team", "already approved" claim, secret-in-body) with results table.

**Phase F - README + Loom**
README (<=2 pages): architecture, prompt strategy, grounding enforcement, act-vs-instruct line, production hardening, Deployment judgment (healthcare vs fintech, onboarding policy/tool #11). Record 5-min Loom: one AUTO_ACTION end to end (E-04 unlock) + one refused-and-routed privileged request (E-07 prod admin).

---

## 6. How the plan maps to the rubric (NOTES.md §11)

| Rubric dimension | Where it is earned |
|---|---|
| Resolution correctness | decider + handlers + eval over 17 + unseen |
| Action safety / restraint (heaviest) | guard.py: structural AMBER/RED blocks, risk-signal preflight, authz gate |
| Grounding & citation | retriever + citation-carrying `Decision`; empty citations -> DEFER |
| Idempotency & recovery | idem keys in mocks + guard; verify_effect; rollback on step-2 fail |
| Engineering quality | tool abstraction, secrets in env, retry/timeout via SDK, AuditRecord log, README |
| FDE thinking | README Deployment judgment + tool/policy #11 onboarding notes |

---

## 7. Open items to confirm before Phase C

- Retrieval method: simple TF/keyword section match vs local embeddings. Default: start keyword-simple, upgrade to embeddings only if recall is weak on the 17.
- Whether to add a "confidence threshold" number for below-threshold DEFER, or keep it as "no citation found -> DEFER". Default: the latter, plus a low-similarity guard.
