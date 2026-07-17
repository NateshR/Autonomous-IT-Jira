"""Typed objects passed through the pipeline.

The LLM returns a validated ``Decision`` (it only *proposes*). Everything that
actually executes produces a ``ToolResult``. One ``AuditRecord`` per ticket is
the single source the decision log, eval report, and structured trace derive
from.
"""

from __future__ import annotations

import json

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Disposition = Literal[
    "ANSWER_ONLY",
    "AUTO_ACTION",
    "PROPOSE_FOR_APPROVAL",
    "ESCALATE_INCIDENT",
    "ASK_CLARIFICATION",
    "DEFER_HUMAN",
]


class PolicySpan(BaseModel):
    policy_id: str = Field(description="e.g. POL-01")
    section: str = Field(description="e.g. 1.4")
    text: str = Field(default="", description="the quoted policy text relied on")

    def cite(self) -> str:
        return f"{self.policy_id} §{self.section}"


class Arg(BaseModel):
    """One tool argument as a name/value pair.

    Why a pair and not a plain dict: a free-form ``dict[str, Any]`` compiles to a
    JSON schema with ``additionalProperties: true`` and NO declared properties,
    and the model then returns ``{}`` every time - regardless of prompt wording,
    field description, or being marked required (``{}`` satisfies ``required``).
    A list of typed pairs gives the decoder real structure to fill, and it does.
    """

    name: str = Field(description="exact parameter name from the tool catalog "
                                  "signature, e.g. 'minutes'")
    value: str = Field(description="the value, as a string. For a list, use "
                                   "comma-separated values (e.g. "
                                   "'manager,data-owner'). For a nested fields "
                                   "object, use compact JSON (e.g. "
                                   '\'{"asset":"LT-4471"}\').')


class PlannedToolCall(BaseModel):
    tool: str = Field(description="tool name from the catalog, e.g. okta.unlock_account")
    args: list[Arg] = Field(
        description="Every argument the tool's signature names. Never empty for a "
                    "tool that takes parameters.")

    @field_validator("args", mode="before")
    @classmethod
    def _accept_dict(cls, v: Any) -> Any:
        """Let internal callers (tests, the stub LLM) construct with a plain dict
        while the model still sees the list-of-pairs schema."""
        if isinstance(v, dict):
            return [{"name": k, "value": _to_str(val)} for k, val in v.items()]
        return v

    def arg_dict(self) -> dict[str, Any]:
        """The args as a dict, with values decoded back to real types."""
        return {a.name: _decode(a.value) for a in self.args}


def _to_str(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def _decode(v: str) -> Any:
    """Turn the string form back into a real type: JSON objects/arrays are parsed,
    everything else stays a string (the guard coerces ints where it needs them)."""
    s = v.strip()
    if s[:1] in ("{", "["):
        try:
            return json.loads(s)
        except ValueError:
            return v
    return v


class Decision(BaseModel):
    """What the decider (LLM) returns. Proposal only - never trusted for safety."""

    disposition: Disposition
    citations: list[PolicySpan] = Field(default_factory=list)
    planned_tool_calls: list[PlannedToolCall] = Field(default_factory=list)
    reasoning: str = ""


class ToolResult(BaseModel):
    tool: str
    args: dict[str, Any]
    idempotency_key: str | None = None
    raw_response: dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    idempotent_replay: bool = False


class AuditRecord(BaseModel):
    ticket_id: str
    disposition: Disposition
    citations: list[PolicySpan] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    reasoning: str = ""
    outcome: str = ""              # closed | pending | escalated | waiting | deferred | rolled_back
    unsafe_action_count: int = 0   # MUST be 0
    notes: list[str] = Field(default_factory=list)

    def log_line(self) -> str:
        cites = ",".join(c.cite() for c in self.citations) or "-"
        tools = " ; ".join(fmt_tool_call(t) for t in self.tool_results) or "-"
        return (f"{self.ticket_id} | {self.disposition} | cites={cites} | "
                f"tools=[{tools}] | outcome={self.outcome} | unsafe={self.unsafe_action_count}")


def fmt_args(args: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in args.items())


def fmt_tool_call(t: ToolResult) -> str:
    """One tool call with its arguments (brief §1.2 - the log records the tool
    call AND its arguments), plus the verify result."""
    flag = "ok" if t.verified else "UNVERIFIED"
    if t.idempotent_replay:
        flag += ",replay"
    return f"{t.tool}({fmt_args(t.args)})[{flag}]"
