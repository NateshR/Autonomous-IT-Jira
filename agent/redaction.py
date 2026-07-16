"""Secret redaction. Secrets found in a ticket body must never be echoed into a
comment or a log line (§6.5). This masks the obvious shapes before any text the
agent writes leaves the system.
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),                 # api-key style
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                       # long hex tokens
    # "password is X", "password: X", "token=X", "my pwd -> X"
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|credential)s?\b"
               r"\s*(?:is|are|:|=|->)\s*\S+"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),           # slack-style tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # aws access key id
]

_MASK = "[REDACTED]"


def redact(text: str) -> str:
    out = text
    for pat in _PATTERNS:
        out = pat.sub(_MASK, out)
    return out
