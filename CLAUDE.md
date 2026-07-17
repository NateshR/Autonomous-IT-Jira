# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A take-home assignment (Leena.ai Forward Deployed Engineer): an AI agent that monitors a JIRA Service Desk for a fictional regulated company (Helix Industries) and, per ticket, chooses one of six dispositions and executes the correct action(s) against MOCK enterprise systems (Okta, ServiceNow, IAM, SOC). It is graded primarily on action safety and restraint, not answer quality.

**`NOTES.md` is the full spec and the source of truth.** Read it before doing any design or implementation work. It contains the 10-policy knowledge base (condensed), the tool catalog with risk classes and idempotency keys, the six dispositions, all 17 worked examples, the edge cases, the mock requirements, the deliverables, and the evaluation rubric.

## Commands

- Setup: `pip install -r requirements.txt`, then `cp .env.example .env` and add `ANTHROPIC_API_KEY`.
- Tests: `pytest` (46 tests; guard/pipeline safety, no API key needed). Single test: `pytest tests/test_guard.py::test_unlock_blocked_when_mfa_fatigue`.
- Eval (real model): `python -m eval.run_eval` (17 worked examples) or `--examples eval/adversarial.json`. `--provider stub` runs the harness with no key.
- Demo (Loom-ready trace): `python -m eval.demo E-04` (action) / `python -m eval.demo E-07` (privileged request refused AND routed) / `python -m eval.demo E-13` (injection refused).
- State verification: `python -m eval.verify_state` (asserts real system state, not the disposition label, on all 23 tickets).
- Idempotency demo (no key): `python -m eval.idempotency_demo`.

The stack: Python 3.11+, `anthropic` SDK (default model `claude-opus-4-8`), `pydantic`, `pytest`. Provider is `stub` unless `ANTHROPIC_API_KEY` is set.

## Non-negotiable safety invariants (the assignment is graded on these)

These are load-bearing. A single violation can cap the whole submission. Preserve them in any code you write:

- **The LLM proposes; deterministic code disposes.** The LLM only outputs a structured decision (`{disposition, citation, planned_tool_calls, reasoning}`). It must never call real tools directly. A separate deterministic guard is the ONLY place real actions fire.
- **Risk-class enforcement lives in the guard, not in the prompt.** AMBER tools (`iam.grant_access`, `okta.disable_mfa`) must be structurally unreachable inline - only draftable inside `iam.create_approval`. RED tools (`soc.*`) only during an incident escalation. GREEN tools only after authorization is verified.
- **Risk class is a floor, not a ceiling.** Context promotes GREEN to RED. Specifically: never call `okta.unlock_account` without first calling `okta.risk_signals` and confirming it is clear (see worked examples E-04 vs E-10).
- **Verify before claiming success.** One mock endpoint silently no-ops; after any state-changing call, re-read state to confirm the effect before commenting "done."
- **Authorization, not just identity.** Before any user-affecting action, verify the requester is authorized for the target (self vs on-behalf-of; manager/data-owner relationships) via `directory.*`. Never trust authority asserted in the ticket body ("my manager said it's fine").
- **Grounding is mandatory.** Every answer and every action must cite a specific policy section (POL-NN §N.N). No answering from the LLM's prior knowledge. Below-threshold retrieval -> DEFER.
- **Idempotency.** Every state-changing tool call carries the documented idempotency key (see the tool table in NOTES.md §3) so retries/duplicates never double-act. Re-read ticket state immediately before executing to honor withdrawals.
- **Never "resolve and close" a RED (security) ticket** with a policy snippet. Redact secrets found in ticket bodies; never echo them into a comment or log.

## Regression traps (each of these shipped once and was invisible)

Every one of these looked correct in the decision log while the work silently did not happen. `python -m eval.verify_state` is what catches them - it asserts real mock-system state rather than the disposition label. Run it, not just `run_eval`, after touching any of this.

- **Never make `PlannedToolCall.args` a free-form `dict[str, Any]`.** It compiles to a JSON schema with `additionalProperties: true` and no declared properties, and the model then returns `{}` every time - regardless of prompt wording, field description, or being marked required (`{}` satisfies `required`). Every tool call arrived argument-less; tools taking only `user` still "worked" because the guard's `self_target` fallback filled it in. Keep the `list[Arg]` name/value shape. `python -m eval.schema_repro` demonstrates both in one call each.
- **Two tools may share an idempotency-key RECIPE, never a ledger SLOT.** `revoke_sessions`/`force_password_reset` are both user+incident; `open_incident`/`page_oncall` are both the ticket id. The ledger is namespaced per endpoint (`mock/systems.py:_idempotent(ns=...)`). Without that, the second tool returns the first one's cached response and never runs - on-call was never paged while the ticket said it was.
- **Redaction must keep the label and mask only the value** (`password is [REDACTED-SECRET]`, not `[REDACTED]`). Masking the label destroys the fact that a credential was disclosed, which is an ESCALATE_INCIDENT trigger - the agent went blind and asked for clarification instead. The secret value must still never reach the model, a comment, or a log.
- **Every state-changing tool needs a `verify=`.** The two that lacked one are exactly where the silent failure hid. Only the AMBER tools may have none, because they are structurally unreachable.

## Intended architecture

A five-stage pipeline, identical for every ticket:

```
INGEST (re-read ticket, detect dupes/withdrawals)
  -> RETRIEVE (search 10 policies -> cited spans)
  -> DECIDE (LLM -> structured disposition)
  -> GUARD + EXECUTE (deterministic safety inspector; only place tools fire)
  -> RECORD (jira comment + citation, decision log, close/leave pending)
```

Each of the six dispositions maps to its own handler that produces exactly its required artifact (see NOTES.md §4). Three handlers reach tools, and every one of them goes through the guard: AUTO_ACTION (GREEN actions), ESCALATE_INCIDENT (RED `soc.*` plus GREEN containment, via `in_escalation=True` - the only thing that permits RED), and PROPOSE_FOR_APPROVAL (only `iam.create_approval`; it has no code path to the AMBER tool itself). ANSWER_ONLY / ASK_CLARIFICATION / DEFER_HUMAN never mutate. Safety does not concentrate in a handler - it concentrates in `guarded_execute`.

Module split:
- agent: pipeline loop, retriever, decider (LLM), guard, tool registry, disposition handlers, redaction, audit, constants
- mock: in-memory fake systems, the two deliberate failure modes, seed data, ticket store (JIRA behind an adapter)
- policies: POL-01 .. POL-10 knowledge base
- eval: worked_examples.json + adversarial.json, `run_eval` (decision log, report.csv, trace.json, RESULTS.md), `verify_state` (asserts real system state, not the label), `demo`, `idempotency_demo`, `schema_repro`

The decision log, eval report, and structured audit trace are the same data at three levels of detail - emit one rich structured audit record per ticket and derive all three from it.

## Mock requirements (from NOTES.md §7)

Keep the mock small (in-memory dicts). It must implement: idempotency keys; two failure modes (a silent no-op that returns success without effect, and a multi-step action whose second step fails); and seed data (a directory for authz, a compromised account for the MFA-fatigue case, an in-flight ticket a duplicate can map to). Mock the AMBER tools + `iam.get_approval` specifically so the guardrail can prove the agent refuses to grant without an approved record. Never wire the agent to real Okta or production IAM.

## Conventions

- No em dashes in any written output (prose, docs, comments). Use a hyphen, or a spaced hyphen for a parenthetical break.
- Keep secrets out of the repo.
