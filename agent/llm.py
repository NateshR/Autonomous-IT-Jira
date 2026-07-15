"""Provider-agnostic LLM layer.

The decider depends only on the ``LLMClient`` protocol - one method that takes a
system prompt, a user prompt, and the target schema, and returns a validated
instance of that schema. Anthropic is the default; a deterministic stub lets the
whole pipeline be tested with no API key and no cost.
"""

from __future__ import annotations

from typing import Callable, Protocol

from agent.models import Decision


class LLMClient(Protocol):
    def decide(self, system: str, user: str, tag: str | None = None) -> Decision:
        """Return a validated Decision. ``tag`` is an optional routing hint
        (e.g. ticket id) used only by the stub; real providers ignore it."""
        ...


class AnthropicLLM:
    """Real decider. Uses the Messages API with schema-constrained output so the
    model is forced to return a valid Decision (it retries on mismatch)."""

    def __init__(self, model: str) -> None:
        import anthropic  # imported lazily so the stub path needs no dependency

        self._client = anthropic.Anthropic()
        self._model = model

    def decide(self, system: str, user: str, tag: str | None = None) -> Decision:
        # messages.parse validates the response against the Decision schema and
        # returns the parsed object. Adaptive thinking helps the judgment calls.
        resp = self._client.messages.parse(
            model=self._model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=Decision,
        )
        parsed = resp.parsed_output
        if parsed is None:  # refusal or non-conforming output -> fail safe
            return Decision(disposition="DEFER_HUMAN", reasoning="model returned no parseable decision")
        return parsed


class StubLLM:
    """Deterministic test double. Backed by a {tag: Decision} table (or a
    callable) so tests can drive the pipeline and handlers without a model.
    Not the graded decider - the real eval uses AnthropicLLM."""

    def __init__(self, table: dict[str, Decision] | Callable[[str, str, str | None], Decision] | None = None) -> None:
        self._table = table or {}

    def decide(self, system: str, user: str, tag: str | None = None) -> Decision:
        if callable(self._table):
            return self._table(system, user, tag)
        if tag is not None and tag in self._table:
            return self._table[tag]
        return Decision(disposition="DEFER_HUMAN", reasoning="stub: no canned decision")


def build_llm(provider: str, model: str) -> LLMClient:
    if provider == "anthropic":
        return AnthropicLLM(model)
    return StubLLM()
