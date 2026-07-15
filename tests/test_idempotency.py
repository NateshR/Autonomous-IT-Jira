"""Idempotency / duplicate handling at the guard boundary.

A retried or duplicated acting ticket must not perform the action twice. The
documented idempotency key makes the second call a no-op replay.
"""

from agent.guard import guarded_execute
from agent.models import PlannedToolCall
from agent.tools import build_tool_registry
from mock.seed import seed_systems
from mock.ticket_store import Ticket


def test_duplicate_request_files_once():
    s = seed_systems()
    reg = build_tool_registry(s)
    ticket = Ticket(id="E-05", reporter="mtaylor", body="I need Figma")
    c = PlannedToolCall(tool="servicenow.create_request",
                        args={"item": "software", "fields": {"name": "Figma"}})
    r1 = guarded_execute(c, ticket, reg, s)
    r2 = guarded_execute(c, ticket, reg, s)
    assert r1.idempotency_key == r2.idempotency_key
    assert r2.idempotent_replay is True
    assert len(s.requests) == 1  # only one request actually filed


def test_new_lockout_gets_new_key():
    # Same account, a genuinely new lockout later -> new epoch -> new key ->
    # the unlock is correctly allowed again (not blocked as a duplicate).
    s = seed_systems()
    reg = build_tool_registry(s)
    ticket = Ticket(id="E-04", reporter="jsmith", body="locked out")
    c = PlannedToolCall(tool="okta.unlock_account", args={"user": "jsmith"})
    r1 = guarded_execute(c, ticket, reg, s)
    # simulate a fresh lockout next time
    s.accounts["jsmith"].locked = True
    s.accounts["jsmith"].lock_epoch = 1002
    r2 = guarded_execute(c, ticket, reg, s)
    assert r1.idempotency_key != r2.idempotency_key
    assert r2.idempotent_replay is False and s.is_locked("jsmith") is False
