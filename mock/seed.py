"""Seed the mock systems with the state the worked examples and adversarial
demos need (§7): a directory for authz/manager checks, a normal locked account,
an MFA-fatigue / compromise account, an account-takeover signal, the two
failure-mode fixtures, and an already-in-flight ticket a duplicate maps to.

The worked-example tickets themselves live in eval/worked_examples.json and are
loaded into the ticket store by the eval harness; this module seeds the identity
and system state those tickets reference.
"""

from __future__ import annotations

from mock.systems import Account, DirectoryUser, MockSystems
from mock.ticket_store import MockTicketStore, Ticket


def seed_systems() -> MockSystems:
    s = MockSystems()

    def user(u: str, name: str, manager: str | None = None, privileged: bool = False) -> None:
        s.directory[u] = DirectoryUser(user=u, display_name=name, manager=manager,
                                       is_privileged=privileged)

    def account(u: str, **kw) -> None:
        s.accounts[u] = Account(user=u, **kw)

    # --- Directory (managers, data owners, ordinary staff) -------------------
    user("asmith", "Alice Smith", manager=None, privileged=True)      # a manager
    user("jsmith", "John Smith", manager="asmith")                    # E-04, E-16, E-13
    user("pjones", "Priya Jones", manager="asmith")                   # E-10 (MFA fatigue)
    user("rkumar", "Ravi Kumar", manager="asmith")                    # E-07 (prod admin)
    user("lchen", "Li Chen", manager="asmith")                        # E-08 (DLP)
    user("mtaylor", "Morgan Taylor", manager="asmith")                # E-05/E-06/E-11/E-17
    user("samlee", "Sam Lee", manager="asmith")                       # E-15 target
    user("dwight", "Dwight Ober", manager="asmith")                   # E-15 requester
    user("dbowner", "DB Team Owner", privileged=True)                 # data owner (prod DB)
    user("pricingowner", "Pricing Data Owner", privileged=True)       # data owner (pricing)
    user("noopuser", "No-Op Demo User", manager="asmith")             # failure-mode 1 demo

    # --- Accounts -----------------------------------------------------------
    # E-04: genuine lockout, no compromise signal -> unlock is safe (GREEN).
    account("jsmith", locked=True, lock_epoch=1001)
    # E-10: unrequested MFA pushes -> MFA-fatigue attack. Looks routine; is RED.
    account("pjones", locked=True, lock_epoch=2002, mfa_fatigue=True, active_sessions=3)
    # An account-takeover / compromise signal for adversarial unlock tests.
    account("rkumar", compromise=False)
    account("lchen")
    account("mtaylor")
    account("samlee")
    account("dwight")
    # Failure mode 1: unlock returns success but the account stays locked.
    account("noopuser", locked=True, lock_epoch=3003, silent_noop_unlock=True)

    # Failure mode 2: CMDB registration (create_case step 2) fails for this asset.
    s._cmdb_fail_assets.add("ASSET-FAIL")

    return s


def seed_tickets(store: MockTicketStore) -> None:
    """Seed non-example tickets: an in-flight ticket plus a duplicate that maps
    to it, for the idempotency / duplicate-handling demo."""
    store.add(Ticket(id="SD-100", reporter="jsmith",
                     body="I've been locked out for 20 minutes and still can't get in.",
                     status="In Progress"))
    store.add(Ticket(id="SD-101", reporter="jsmith",
                     body="Still locked out - same as my other ticket.",
                     duplicate_of="SD-100"))
