"""Runtime settings. Secrets come from the environment (.env is gitignored)."""

from __future__ import annotations

import os
from dataclasses import dataclass

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


@dataclass
class Settings:
    model: str = os.getenv("AGENT_MODEL", "claude-opus-4-8")
    # provider: "anthropic" | "stub". Default to anthropic only if a key exists.
    provider: str = os.getenv(
        "LLM_PROVIDER",
        "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "stub",
    )
    retrieval_top_k: int = 4
    # below this overlap score, retrieval is treated as "no grounding" -> DEFER
    retrieval_min_score: float = 1.0


SETTINGS = Settings()
