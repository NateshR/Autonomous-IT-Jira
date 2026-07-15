# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A take-home assignment (Leena.ai Forward Deployed Engineer): an AI agent that monitors a JIRA Service Desk for a fictional regulated company (Helix Industries) and, per ticket, chooses one of six dispositions and executes the correct action(s) against MOCK enterprise systems (Okta, ServiceNow, IAM, SOC). It is graded primarily on action safety and restraint, not answer quality.

**`NOTES.md` is the full spec and the source of truth.** Read it before doing any design or implementation work. It contains the 10-policy knowledge base (condensed), the tool catalog with risk classes and idempotency keys, the six dispositions, all 17 worked examples, the edge cases, the mock requirements, the deliverables, and the evaluation rubric.

## Current status

Pre-implementation. The repo currently contains only `NOTES.md` (analysis/spec). No code, no git repo, no build tooling, and no chosen stack yet. When scaffolding begins, add the real build/test/run commands to this file, replacing this section.

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

## Intended architecture

A five-stage pipeline, identical for every ticket:

```
INGEST (re-read ticket, detect dupes/withdrawals)
  -> RETRIEVE (search 10 policies -> cited spans)
  -> DECIDE (LLM -> structured disposition)
  -> GUARD + EXECUTE (deterministic safety inspector; only place tools fire)
  -> RECORD (jira comment + citation, decision log, close/leave pending)
```

Each of the six dispositions maps to its own handler that produces exactly its required artifact (see NOTES.md §4). AUTO_ACTION is the only handler that executes tools, so all safety checks concentrate there. PROPOSE_FOR_APPROVAL has no code path to the AMBER tool - it can only draft into `iam.create_approval`.

Planned module split (subject to change when the stack is chosen):
- agent: pipeline loop, retriever, decider (LLM), guard, executor, disposition handlers, audit
- mock: in-memory fake systems, the two deliberate failure modes, seed data
- policies: POL-01 .. POL-10 knowledge base
- eval: the 17 worked examples + the CSV report (predicted disposition + tool calls + citation + unsafe-action count, which must be 0)

The decision log, eval report, and structured audit trace are the same data at three levels of detail - emit one rich structured audit record per ticket and derive all three from it.

## Mock requirements (from NOTES.md §7)

Keep the mock small (in-memory dicts). It must implement: idempotency keys; two failure modes (a silent no-op that returns success without effect, and a multi-step action whose second step fails); and seed data (a directory for authz, a compromised account for the MFA-fatigue case, an in-flight ticket a duplicate can map to). Mock the AMBER tools + `iam.get_approval` specifically so the guardrail can prove the agent refuses to grant without an approved record. Never wire the agent to real Okta or production IAM.

## Conventions

- No em dashes in any written output (prose, docs, comments). Use a hyphen, or a spaced hyphen for a parenthetical break.
- Keep secrets out of the repo.
