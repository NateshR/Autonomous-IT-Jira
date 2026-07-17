"""Secret redaction. Secrets found in a ticket body must never be echoed into a
comment or a log line (§6.5). This masks the obvious shapes before any text the
agent writes leaves the system.
"""

from __future__ import annotations

import re

_MASK = "[REDACTED-SECRET]"

# Bare secret shapes: no surrounding label, so the mask itself carries the signal.
_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),                 # api-key style
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                       # long hex tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),           # slack-style tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # aws access key id
]

# Labelled secrets: "password is X", "token=X", "my pwd -> X". The label is KEPT
# and only the value is masked. Masking the label too ("my password is hunter2"
# -> "my [REDACTED]") destroys the very fact that a credential was disclosed -
# which is the signal ESCALATE_INCIDENT depends on. The value never survives;
# the disclosure always does.
_LABELLED = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|credential)(s?)\b"
    r"(\s*(?:is|are|:|=|->)\s*)\S+")


def redact(text: str) -> str:
    """Mask secret VALUES while preserving the fact that a secret was present.

    Used on every path where agent-written text leaves the system (§6.5), and on
    the ticket body before it reaches the model - so a live credential is never
    sent to the LLM provider, while the model can still see that one was leaked
    and escalate.
    """
    out = _LABELLED.sub(rf"\1\2\3{_MASK}", text)
    for pat in _PATTERNS:
        out = pat.sub(_MASK, out)
    return out
