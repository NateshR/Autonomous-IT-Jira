"""Policy retrieval. Splits the 10 policy files into cited sections
(POL-NN §N.N) and returns the spans most relevant to a ticket.

Deliberately simple: keyword/token overlap over ten short documents. A heavier
vector store is not justified here and would be less inspectable. If the top
score is below a threshold we return nothing, which the pipeline treats as "no
grounding" and downgrades to DEFER (grounding is enforced structurally).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agent.models import PolicySpan

_SECTION_RE = re.compile(r"^(\d+\.\d+)\s+(.*)$")
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "be", "my", "i", "me", "you", "it", "this", "that", "with", "can", "how",
    "do", "does", "was", "were", "at", "by", "as", "if", "no", "not", "your",
}


def _norm(w: str) -> str:
    # light normalization: strip a trailing plural 's' (attachments -> attachment)
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _tokens(text: str) -> set[str]:
    return {_norm(w) for w in _WORD_RE.findall(text.lower())
            if w not in _STOP and len(w) > 1}


@dataclass
class Retriever:
    spans: list[PolicySpan]
    span_tokens: list[set[str]]

    @classmethod
    def from_dir(cls, policy_dir: str | Path) -> "Retriever":
        spans: list[PolicySpan] = []
        for path in sorted(Path(policy_dir).glob("POL-*.md")):
            policy_id = path.stem  # "POL-01"
            for line in path.read_text(encoding="utf-8").splitlines():
                m = _SECTION_RE.match(line.strip())
                if m:
                    spans.append(PolicySpan(policy_id=policy_id, section=m.group(1),
                                            text=m.group(2).strip()))
        return cls(spans=spans, span_tokens=[_tokens(s.text) for s in spans])

    def search(self, query: str, top_k: int = 4, min_score: float = 1.0) -> list[PolicySpan]:
        q = _tokens(query)
        scored: list[tuple[float, PolicySpan]] = []
        for span, toks in zip(self.spans, self.span_tokens):
            overlap = len(q & toks)
            if overlap >= min_score:
                scored.append((overlap, span))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [span for _, span in scored[:top_k]]

    def get(self, policy_id: str, section: str) -> PolicySpan | None:
        for s in self.spans:
            if s.policy_id == policy_id and s.section == section:
                return s
        return None
