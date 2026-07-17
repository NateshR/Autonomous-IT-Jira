"""Minimal repro of the empty-args bug, kept because it is the evidence.

THE BUG
-------
Every tool call the model proposed arrived with NO arguments. Tools taking only
`user` appeared to work - the guard's self_target fallback filled it from the
ticket reporter - so the happy path looked fine. Tools taking more (open_incident,
create_approval, create_request, create_case, grant_admin) raised on invocation
and got downgraded to DEFER.

WHY IT WAS INVISIBLE
--------------------
The eval grades the disposition LABEL. E-07 still said PROPOSE_FOR_APPROVAL and
scored a match - while never routing an approval. E-09 said ESCALATE_INCIDENT and
scored a match - while never opening an incident or paging anyone, and then
commenting "the on-call team was paged". The decision log was clean. The work
never happened. That is why eval/verify_state.py now asserts system state instead.

THE CAUSE
---------
`args: dict[str, Any]` compiles to a JSON schema with `additionalProperties: true`
and NO declared properties. The model returns `{}` every time - regardless of
prompt wording, field description, or being marked required (`{}` satisfies
`required`; required means the key is present, not that it has content).

A list of typed name/value pairs gives the decoder real structure, and it fills.

Run this to see both, side by side, in one call each:
    python -m eval.schema_repro
"""

from __future__ import annotations

from typing import Any

from anthropic import Anthropic
from pydantic import BaseModel, Field

from agent.config import SETTINGS


class BrokenCall(BaseModel):
    """What we shipped: a free-form object. Always comes back empty."""

    tool: str
    args: dict[str, Any] = Field(
        description="e.g. {'user': 'jsmith', 'minutes': 30}")


class Arg(BaseModel):
    name: str = Field(description="exact parameter name, e.g. 'minutes'")
    value: str = Field(description="the value as a string")


class FixedCall(BaseModel):
    """The fix: typed pairs. The decoder has something to fill."""

    tool: str
    args: list[Arg]


PROMPT = ("Catalog: endpoint.grant_admin(user, minutes)\n"
          "Task: grant jsmith local admin for 30 minutes.\n"
          "Propose the call.")


def main() -> None:
    client = Anthropic()   # reads ANTHROPIC_API_KEY; agent.config loads .env
    for label, model_cls in (("dict[str, Any]  (what broke)", BrokenCall),
                             ("list[Arg]       (the fix)", FixedCall)):
        r = client.messages.parse(model=SETTINGS.model, max_tokens=512,
                                  messages=[{"role": "user", "content": PROMPT}],
                                  output_format=model_cls)
        print(f"  {label} -> {r.parsed_output!r}")
    print("\nSame prompt, same model, same task. Only the schema shape differs.")


if __name__ == "__main__":
    main()
