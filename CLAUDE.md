# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A take-home assignment (Leena.ai Forward Deployed Engineer): an AI agent that monitors a JIRA Service Desk for a fictional regulated company (Helix Industries) and, per ticket, chooses one of six dispositions and executes the correct action(s) against MOCK enterprise systems (Okta, ServiceNow, IAM, SOC). It is graded primarily on action safety and restraint, not answer quality.

## Where things are documented

Read the right file rather than duplicating it here.

| File | What it holds |
|---|---|
| `NOTES.md` | **the spec, and the source of truth** - policies, tool catalog with risk classes and idempotency keys, the six dispositions, all 17 worked examples, edge cases, mock requirements, deliverables, rubric. Read before any design work. |
| `LLD.md` | how the code actually works - component map, data models, pipeline and guard flowcharts, the six handlers, a worked E-04 trace, idempotency/verification, §12 edge-case-to-test map, §13 the regression write-ups |
| `README.md` | the submission summary: architecture, prompt strategy, grounding, act-vs-instruct, what to harden, deployment judgment |
| `BUILD_PLAN.md` | the phased plan |

## Commands

- Setup: `pip install -r requirements.txt`, then `cp .env.example .env` and add `ANTHROPIC_API_KEY`.
- Tests: `pytest` (46 tests; guard/pipeline safety, no API key needed). Single test: `pytest tests/test_guard.py::test_unlock_blocked_when_mfa_fatigue`.
- Eval (real model): `python -m eval.run_eval` (17 worked examples) or `--examples eval/adversarial.json`. `--provider stub` runs the harness with no key.
- Demo (Loom-ready trace): `python -m eval.demo E-04` (action) / `python -m eval.demo E-07` (privileged request refused AND routed) / `python -m eval.demo E-13` (injection refused).
- State verification: `python -m eval.verify_state` (asserts real system state, not the disposition label, on all 23 tickets).
- Idempotency demo (no key): `python -m eval.idempotency_demo`.
- Schema repro: `python -m eval.schema_repro` (why `args` must stay `list[Arg]` - see traps below).

The stack: Python 3.11+, `anthropic` SDK (default model `claude-opus-4-8`), `pydantic`, `pytest`. Provider is `stub` unless `ANTHROPIC_API_KEY` is set.

## Non-negotiable safety invariants (the assignment is graded on these)

These are load-bearing. A single violation can cap the whole submission. Preserve them in any code you write:

- **Safety lives in `guarded_execute`, never in a handler.** Three handlers reach tools (AUTO_ACTION, ESCALATE_INCIDENT, PROPOSE_FOR_APPROVAL) and all three go through the guard. A new safety check belongs in `guard.PRECHECKS` plus a `requires` entry - never as an `if` inside a handler, or it protects one path and not the others.
- **The LLM proposes; deterministic code disposes.** The LLM only outputs a structured decision (`{disposition, citation, planned_tool_calls, reasoning}`). It must never call real tools directly. A separate deterministic guard is the ONLY place real actions fire.
- **Risk-class enforcement lives in the guard, not in the prompt.** AMBER tools (`iam.grant_access`, `okta.disable_mfa`) are structurally unreachable inline - only draftable inside `iam.create_approval`. RED tools (`soc.*`) run only during an incident escalation. GREEN tools run only after the preconditions their registry row declares.
- **Not every GREEN tool takes `authorized`, and that is deliberate.** It gates tools that change a specific person's access (unlock, password reset, grant_admin). Containment tools (`revoke_sessions`, `force_password_reset`) omit it on purpose - they reduce access during an incident, and requiring target==reporter would stop SOC containing a compromised account. `create_request`/`create_case` take no top-level `user` at all, so they use `fields_target_self` instead. Match the gate to the tool; do not blanket-apply `authorized`.
- **Risk class is a floor, not a ceiling.** Context promotes GREEN to RED. Specifically: never call `okta.unlock_account` without first calling `okta.risk_signals` and confirming it is clear (see worked examples E-04 vs E-10).
- **Verify before claiming success.** One mock endpoint silently no-ops; after any state-changing call, re-read state to confirm the effect before commenting "done."
- **Authorization, not just identity.** Before any user-affecting action, verify the requester is authorized for the target (self vs on-behalf-of; manager/data-owner relationships) via `directory.*`. Never trust authority asserted in the ticket body ("my manager said it's fine").
- **Grounding is mandatory.** Every answer and every action must cite a specific policy section (POL-NN §N.N), and no answering from the LLM's prior knowledge. Enforced by validating each citation against the corpus: a section that does not exist is dropped, and an answer or action left with no valid citation is downgraded to DEFER. (The brief frames this as below-threshold retrieval -> DEFER; the retrieval threshold gates nothing here because the full corpus is passed every ticket, so citation validation is the mechanism that does the work.)
- **Idempotency.** Every state-changing tool call carries the documented idempotency key (see the tool table in NOTES.md §3) so retries/duplicates never double-act.
- **Ingest re-reads the ticket by id before anything else** (`pipeline.handle`), and a withdrawal or duplicate returns immediately - before retrieve, decide, or any tool. Note the honest limit: decision and execution happen inside one call here, so there is no window between them; the brief's concern is a queued or deferred system, where the re-read would have to move to just before execution.
- **Never "resolve and close" a RED (security) ticket** with a policy snippet. Redact secrets found in ticket bodies; never echo them into a comment or log.

## Regression traps

Four rules that each shipped broken once, invisibly - the decision log looked correct while the work never happened. Why, and what it cost, is in `LLD.md` §13. Do not undo them:

- **`PlannedToolCall.args` stays `list[Arg]`.** Never a free-form `dict[str, Any]`.
- **Two tools may share an idempotency-key recipe, never a ledger slot.** The ledger is namespaced per endpoint (`mock/systems.py::_idempotent(ns=...)`).
- **Redaction keeps the label, masks only the value** (`password is [REDACTED-SECRET]`).
- **Every state-changing tool declares a `verify=`.** Only the AMBER tools may omit it, being structurally unreachable.

After touching any of these run `python -m eval.verify_state`, not just `run_eval` - it asserts real system state rather than the disposition label, which is what catches this whole class of bug.

## Mock

Requirements are `NOTES.md` §7; the implementation is `mock/`. Keep it small (in-memory dicts) - the assignment grades the agent, not the mock. Two rules that are not negotiable: the two deliberate failure modes stay (silent no-op, step-2 failure), and **the agent is never wired to real Okta or production IAM**.

## Conventions

- No em dashes in any written output (prose, docs, comments). Use a hyphen, or a spaced hyphen for a parenthetical break.
- Keep secrets out of the repo.
