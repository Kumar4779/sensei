"""The three guided scenarios (Demo Slice 1). Each primes a fresh session on the voice World's synthetic fixtures.

The cards tell the visitor what to say and what to watch for in the ledger.
The data is demo data: the World's defaults (contacts, the note, invoices),
never a real mailbox, calendar or bank.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from voice.world import World

DEMO_DATA_LABEL = ("Demo data: the voice World's synthetic fixtures (contacts, notes, invoices, a calendar). "
                   "No real mail, calendar or payments are touched.")


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    kind: str  # what the gate does: read-only | echo then confirm | echo then decline
    say: tuple[str, ...]  # what the visitor says, turn by turn
    watch: str  # what the ledger should show

    def to_dict(self) -> dict:
        return asdict(self)


SCENARIOS = {s.id: s for s in (
    Scenario("brief", "Morning brief", "read-only",
             ("Read me my morning brief.",),
             "The gate allows a read: no echo, no approval, executed straight away."),
    Scenario("note", "Send a note to Alex", "echo, then confirm",
             ("Send the note to Alex.", "Yes."),
             "Sending is irreversible: proposed, echoed back to you, approved by your yes, executed, verified."),
    # Live, the demo's Deepgram has smart_format on, so a spoken "invoice two oh four" arrives as "invoice 204"
    # (unformatted, it is "two zero four", which the parser does not read yet: #16).
    Scenario("pay", "Pay an invoice", "echo, then decline",
             ("Pay invoice 204.", "No."),
             "Say no at the echo. The ledger shows the payment proposed and echoed, then declined: "
             "nothing executes, and no payment is made."),
)}


def world_for(scenario_id: str) -> World:
    """A fresh World for the scenario: the same synthetic fixtures every time."""
    if scenario_id not in SCENARIOS:
        raise KeyError(scenario_id)
    return World()
