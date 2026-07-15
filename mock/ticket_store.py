"""Ticket workflow surface (jira.*) behind an adapter.

The agent talks to a ``TicketStore``; the concrete backend is swappable. We ship
an in-memory ``MockTicketStore`` for the graded build and a documented
``JiraCloudStore`` stub showing where a real free JIRA Cloud project would plug
in. Only the privileged systems are a hard requirement to mock (§7); JIRA "may"
be real (§1.2), so the adapter keeps that a config choice, not a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Ticket:
    id: str
    reporter: str                 # established by login on a real Service Desk
    body: str
    status: str = "Open"
    labels: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    # test/seed hooks
    duplicate_of: str | None = None
    withdrawn: bool = False


class TicketStore(Protocol):
    def get(self, ticket_id: str) -> Ticket: ...
    def comment(self, ticket_id: str, text: str) -> None: ...
    def transition(self, ticket_id: str, status: str) -> None: ...
    def add_label(self, ticket_id: str, label: str) -> None: ...
    def link_issues(self, ticket_id: str, other_id: str) -> None: ...


class MockTicketStore:
    """In-memory ticket surface. Seeded with the worked-example tickets."""

    def __init__(self) -> None:
        self.tickets: dict[str, Ticket] = {}

    def add(self, ticket: Ticket) -> None:
        self.tickets[ticket.id] = ticket

    def get(self, ticket_id: str) -> Ticket:
        return self.tickets[ticket_id]

    def comment(self, ticket_id: str, text: str) -> None:
        self.tickets[ticket_id].comments.append(text)

    def transition(self, ticket_id: str, status: str) -> None:
        self.tickets[ticket_id].status = status

    def add_label(self, ticket_id: str, label: str) -> None:
        labels = self.tickets[ticket_id].labels
        if label not in labels:
            labels.append(label)

    def link_issues(self, ticket_id: str, other_id: str) -> None:
        links = self.tickets[ticket_id].links
        if other_id not in links:
            links.append(other_id)


class JiraCloudStore:
    """Stub: where a real free JIRA Cloud project would plug in.

    Left unwired on purpose - the privileged systems must stay mocked, and JIRA
    being real is optional. To enable, implement these against the JIRA REST API
    (GET issue, add comment, transition, add label, link issues) using an API
    token from the environment, and select it via config. The rest of the agent
    is unchanged because it only depends on the TicketStore protocol.
    """

    def get(self, ticket_id: str) -> Ticket:  # pragma: no cover - not wired
        raise NotImplementedError("Wire to JIRA REST /issue/{id} to enable real JIRA.")

    def comment(self, ticket_id: str, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def transition(self, ticket_id: str, status: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def add_label(self, ticket_id: str, label: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def link_issues(self, ticket_id: str, other_id: str) -> None:  # pragma: no cover
        raise NotImplementedError
