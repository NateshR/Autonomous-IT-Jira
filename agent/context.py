"""Shared execution context passed to handlers - the mock systems, the ticket
store, and the bound tool registry."""

from __future__ import annotations

from dataclasses import dataclass

from agent.tools import Tool
from mock.systems import MockSystems
from mock.ticket_store import TicketStore


@dataclass
class AgentContext:
    store: TicketStore
    systems: MockSystems
    registry: dict[str, Tool]
