"""Audit outputs. The rich AuditRecord per ticket is projected into three
deliverables: a one-line decision log, a CSV eval report, and a full structured
JSON trace (stretch). All three derive from the same records - no duplication.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from agent.models import AuditRecord, fmt_tool_call


def report_row(example: dict, rec: AuditRecord) -> dict:
    acceptable = example.get("acceptable", [example.get("expected")])
    match = rec.disposition in acceptable
    return {
        "id": rec.ticket_id,
        "expected": example.get("expected", ""),
        "predicted": rec.disposition,
        "match": "Y" if match else "N",
        "tool_calls": " ; ".join(fmt_tool_call(t) for t in rec.tool_results) or "-",
        "citations": ",".join(c.cite() for c in rec.citations) or "-",
        "outcome": rec.outcome,
        "unsafe_actions": rec.unsafe_action_count,
        "reason": (rec.reasoning or "; ".join(rec.notes))[:160],
    }


def write_report_csv(rows: list[dict], path: str | Path) -> None:
    cols = ["id", "expected", "predicted", "match", "tool_calls",
            "citations", "outcome", "unsafe_actions", "reason"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def write_trace_json(records: list[AuditRecord], path: str | Path) -> None:
    payload = [r.model_dump() for r in records]
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def print_decision_log(records: list[AuditRecord]) -> None:
    print("DECISION LOG")
    for r in records:
        print("  " + r.log_line())
