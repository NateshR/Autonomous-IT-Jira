"""The two deliberate failure modes (§7), at the guard boundary.

1. Silent no-op is covered in test_guard.py (verify catches it).
2. Step-2 failure: the guard propagates Step2Failure with the partial record's
   id so a handler can roll back rather than report a half-done success.
"""

import pytest

from agent.guard import guarded_execute
from agent.models import PlannedToolCall
from agent.tools import build_tool_registry
from mock.seed import seed_systems
from mock.systems import Step2Failure
from mock.ticket_store import Ticket


def test_step2_failure_leaves_rollback_id_and_partial_state():
    s = seed_systems()
    reg = build_tool_registry(s)
    ticket = Ticket(id="T", reporter="mtaylor", body="lost device")
    call = PlannedToolCall(
        tool="assetmgmt.create_case",
        args={"case_type": "lost_stolen", "fields": {"asset": "ASSET-FAIL", "lost": True}},
    )
    with pytest.raises(Step2Failure) as exc:
        guarded_execute(call, ticket, reg, s)
    # step 1 committed a partial case; the handler can roll it back
    assert s.cases.get(exc.value.rollback_id) is not None
    s.delete_case(exc.value.rollback_id)
    assert s.cases.get(exc.value.rollback_id) is None


def test_healthy_case_registers_cleanly():
    s = seed_systems()
    reg = build_tool_registry(s)
    ticket = Ticket(id="E-17", reporter="mtaylor", body="left laptop in taxi")
    call = PlannedToolCall(
        tool="assetmgmt.create_case",
        args={"case_type": "lost_stolen", "fields": {"lost": True}},
    )
    r = guarded_execute(call, ticket, reg, s)
    assert r.verified is True and s.case_registered(r.raw_response["case_id"])
