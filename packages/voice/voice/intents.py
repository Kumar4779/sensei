"""Transcript → intent + parameters (Voice Lab Slice 1): deterministic rules, no model.

Parameters are resolved against the World to concrete IDs ("the note" becomes
the latest note's ID, "Alex" becomes "Alex Chen"), so the echo can read back
exactly what would run. Whenever a referent is missing or ambiguous, the
parser returns a Clarify instead of guessing: two contacts named Sam, "send
it" with nothing to send, or a verb it doesn't know (a misheard "spend the
note").

Every sentence of the heard transcript is scanned for the command (Slice 5,
#12). Before that, an injection clause is removed: one claiming prior
confirmation ("the user already confirmed this") or telling the agent to skip
it ("skip the confirmation", "without asking"). Such a clause is never a
command and never a confirmation, so whatever command remains is still only
proposed and echoed. If more than one sentence holds a command, the parser
clarifies rather than guess.

A command turn can never also be a confirmation. That needs its own turn after
the echo (voice/loop.py); the parser can't produce one, and the confirmation
state machine and the gate don't change.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from voice.world import World

LOW_CONFIDENCE = 0.6
NUMBER_WORDS = {"ten": 10, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
                "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
                "eighty": 80, "ninety": 90, "hundred": 100, "one hundred": 100}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_AFFIRM = re.compile(r"^(yes|yeah|yep|confirm|confirmed|correct|do it|go ahead)\b")
_NEGATE = re.compile(r"^(no|nope|cancel|stop|don't|do not|wait)\b")


@dataclass(frozen=True)
class Command:
    tool: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Clarify:
    question: str
    reason: str  # low_confidence | ambiguous | missing_referent | unrecognised


@dataclass(frozen=True)
class Confirmation:
    affirm: bool
    mentions: dict[str, Any] = field(default_factory=dict)  # parameters the user named while confirming


@dataclass
class Memory:
    """What the loop remembers between turns, so follow-ups can resolve "it"."""

    last_event_ids: list[str] = field(default_factory=list)
    last_note_id: str | None = None


def _norm(text: str) -> str:
    text = text.casefold().replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'$.,:-]", " ", text)).strip()


# A clause that claims the user already confirmed, or tells the agent to skip confirmation. It is removed
# before parsing: never obeyed, never a command, never a confirmation.
INJECTION = re.compile(
    r"\b(?:already|previously|pre-?)\s*(?:been\s+|has\s+|have\s+)?(?:confirmed|approved|authori[sz]ed|agreed)\b"
    r"|\b(?:skip|bypass|ignore)\s+(?:the\s+|any\s+|your\s+)?(?:confirmation|confirming|echo|read-?back|check)s?\b"
    r"|\bwithout\s+(?:asking|confirmation|confirming|checking|reading\s+(?:it\s+)?back)\b"
    r"|\b(?:no\s+need|don'?t\s+need|do\s+not\s+need)\s+to\s+(?:confirm|ask|check)\b"
    r"|\b(?:don'?t|do\s+not)\s+(?:ask|confirm|check)\b"
    r"|\bconsider\s+(?:it|this)\s+(?:confirmed|approved)\b"
    r"|\bconfirmation\s+(?:is|was|has\s+been)\s+(?:given|granted|done|not\s+needed|unnecessary)\b",
    re.IGNORECASE)
_CLAUSE = re.compile(r",\s*|\s+(?:and|so|then)\s+", re.IGNORECASE)
_FILLER_TAIL = re.compile(r"\s+(?:now|immediately|right\s+now|right\s+away|asap|please)$", re.IGNORECASE)


def sentences(text: str) -> list[str]:
    return [p.rstrip(".!?;, ") for p in re.split(r"(?<=[.!?;])\s+", text.strip()) if p.rstrip(".!?;, ")]


def strip_injection(sentence: str) -> tuple[str, list[str]]:
    """Remove injection clauses from one sentence. Returns (what remains, the clauses removed)."""
    if not INJECTION.search(sentence):
        return sentence, []
    clauses = [c for c in _CLAUSE.split(sentence) if c.strip()]
    kept = [c for c in clauses if not INJECTION.search(c)]
    removed = [c for c in clauses if INJECTION.search(c)]
    return _FILLER_TAIL.sub("", " and ".join(kept)).strip(), removed


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?;])\s+", text.strip(), maxsplit=1)[0].rstrip(".!?;, ")


def _resolve_contact(world: World, name: str) -> list[str]:
    name = name.strip(" .,").casefold()
    full = [c for c in world.contacts if c.casefold() == name]
    return full or [c for c in world.contacts if c.split()[0].casefold() == name]


def _resolve_day(world: World, word: str) -> str | None:
    word = word.strip(" .,?").casefold()
    if word == "today":
        return world.today.isoformat()
    if word == "tomorrow":
        return (world.today + dt.timedelta(days=1)).isoformat()
    if word in WEEKDAYS:
        ahead = (WEEKDAYS.index(word) - world.today.weekday()) % 7
        return (world.today + dt.timedelta(days=ahead)).isoformat()
    return None


def _amount(word: str) -> str | None:
    word = word.strip().replace("$", "")
    value = NUMBER_WORDS.get(word) or (int(word) if word.isdigit() else None)
    return None if value is None else f"${value:,.2f}"


def _contact_or_clarify(world: World, name: str) -> list[str] | Clarify:
    matches = _resolve_contact(world, name)
    if len(matches) > 1:
        return Clarify(f"Which {name.strip().title()} do you mean: {' or '.join(matches)}?", "ambiguous")
    if not matches:
        return Clarify(f"I don't have a contact called {name.strip().title()}. Who should it go to?", "missing_referent")
    return matches


def parse_command(text: str, world: World, memory: Memory) -> Command | Clarify:
    """Scan every sentence for the command (Slice 5). Injection clauses are removed first. Exactly one command
    sentence: that command (still only proposed and echoed by the loop). More than one: clarify. None: the most
    specific clarification any sentence produced."""
    commands: list[Command] = []
    clarifications: list[Clarify] = []
    for raw in sentences(text):
        cleaned, _ = strip_injection(raw)
        if not cleaned:
            continue
        parsed = _parse_sentence(cleaned, world, memory)
        (commands if isinstance(parsed, Command) else clarifications).append(parsed)
    if len(commands) > 1:
        return Clarify("I heard more than one request. Please tell me one thing at a time.", "ambiguous")
    if commands:
        return commands[0]
    specific = [c for c in clarifications if c.reason != "unrecognised"]
    return (specific or clarifications or [Clarify("Sorry, I didn't catch that. Could you say it another way?",
                                                   "unrecognised")])[0]


def _parse_sentence(raw: str, world: World, memory: Memory) -> Command | Clarify:
    """The Slice 1 rules, applied to one sentence with any injection clause already removed."""
    s = re.sub(r"^(?:actually|no|wait)[, ]+| instead$", "", _norm(raw)).strip()

    if m := re.search(r"\b(?:pay|send|spend) (\$?\w+) dollars (?:to|on) (\w+(?: \w+)?)$", s):
        amount = _amount(m.group(1))
        who = _contact_or_clarify(world, m.group(2))
        if isinstance(who, Clarify):
            return who
        if amount is None:
            return Clarify("How much should I pay?", "missing_referent")
        return Command("pay", {"payee": who[0], "amount": amount, "invoice_id": None})

    if m := re.search(r"\bpay invoice (?:inv[- ]?)?(\d+)\b", s):
        invoice_id = f"INV-{m.group(1)}"
        invoice = world.invoices.get(invoice_id)
        if invoice is None or invoice["status"] != "open":
            return Clarify(f"I can't find an open invoice {invoice_id}. Which invoice?", "missing_referent")
        return Command("pay", {"payee": invoice["payee"], "amount": invoice["amount"], "invoice_id": invoice_id})

    if m := re.search(r"\bsend (?:the |my )?(note|it)\b(?: to (.+))?$", s):
        if m.group(1) == "it" and memory.last_note_id is None:
            return Clarify("What would you like me to send, and to whom?", "missing_referent")
        if not m.group(2):
            return Clarify("Who should I send it to?", "missing_referent")
        if not world.notes:
            return Clarify("You don't have any notes to send.", "missing_referent")
        who = _contact_or_clarify(world, m.group(2))
        if isinstance(who, Clarify):
            return who
        note_id = memory.last_note_id if m.group(1) == "it" else world.notes[-1]["id"]
        return Command("send_note", {"note_id": note_id, "recipient": who[0]})

    if re.fullmatch(r"send(?: it)?", s):
        return Clarify("What would you like me to send, and to whom?", "missing_referent")

    if m := re.match(r"(?:please )?(?:take|make) a note(?: that)?[:,]? (.+)$", s):
        start = len(raw) - len(m.group(1))
        return Command("take_note", {"text": raw[start:].strip()})

    if m := re.search(r"\b(?:on|in) my calendar(?: for)? (\w+)$", s) or re.search(r"\bmy calendar (\w+)$", s):
        date = _resolve_day(world, m.group(1))
        if date is None:
            return Clarify("Which day should I check?", "missing_referent")
        return Command("calendar_query", {"date": date})

    if re.search(r"\b(?:morning )?brief(?:ing)?\b", s):
        return Command("morning_brief", {})

    if m := re.match(r"(?:and )?move (?:it|that|that one|this one|the first one) to (\w+)$", s):
        if len(memory.last_event_ids) != 1:
            return Clarify("Which event should I move?", "ambiguous" if memory.last_event_ids else "missing_referent")
        date = _resolve_day(world, m.group(1))
        if date is None:
            return Clarify("Which day should I move it to?", "missing_referent")
        return Command("move_event", {"event_id": memory.last_event_ids[0], "date": date})

    if m := re.match(r"(approve|deny|reject) (?:the )?(?:pending )?(?:(\w+) )?request$", s):
        tool = "approve_pending" if m.group(1) == "approve" else "deny_pending"
        pending = {rid: a for rid, a in world.approvals.items() if a["status"] == "pending"}
        kind = m.group(2)
        matches = [rid for rid, a in pending.items() if kind in (None, a["kind"])]
        if len(matches) != 1:
            kinds = " or ".join(sorted(a["kind"] for a in pending.values()))
            return Clarify(f"Which request: {kinds}?" if pending else "There are no pending requests.",
                           "ambiguous" if matches else "missing_referent")
        return Command(tool, {"request_id": matches[0]})

    if m := re.match(r"delete (?:the |my )?note(?: (?:about|on) (.+))?$", s):
        topic = m.group(1)
        matches = [n for n in world.notes if topic is None or topic.casefold() in n["text"].casefold()]
        if topic is None and memory.last_note_id:
            matches = [world.note(memory.last_note_id)]
        if len(matches) != 1:
            return Clarify("Which note should I delete?", "ambiguous" if matches else "missing_referent")
        return Command("delete_note", {"note_id": matches[0]["id"]})

    if m := re.match(r"submit (?:the |my )?(.+?)(?: form)?$", s):
        matches = [fid for fid, f in world.forms.items() if f["name"] == m.group(1) and f["status"] == "draft"]
        if len(matches) != 1:
            return Clarify("Which form should I submit?", "missing_referent")
        return Command("submit_form", {"form_id": matches[0]})

    return Clarify("Sorry, I didn't catch that. Could you say it another way?", "unrecognised")


def parse_confirmation(text: str, world: World) -> Confirmation | None:
    """A yes/no answer to an echo, with any parameters the user named; None if it isn't one."""
    s = _norm(text)
    if _NEGATE.match(s):
        return Confirmation(False)
    if not _AFFIRM.match(s):
        return None
    mentions: dict[str, Any] = {}
    named = sorted({c for c in world.contacts if c.casefold() in s}
                   | {c for c in world.contacts if re.search(rf"\b{c.split()[0].casefold()}\b", s)})
    if named:
        mentions["people"] = named
    if m := re.search(r"\b(\$?\d+|" + "|".join(NUMBER_WORDS) + r") dollars\b", s):
        mentions["amount"] = _amount(m.group(1))
    if m := re.search(r"\binvoice (?:inv[- ]?)?(\d+)\b", s):
        mentions["invoice_id"] = f"INV-{m.group(1)}"
    return Confirmation(True, mentions)


def confirmed_params(echoed: dict[str, Any], confirmation: Confirmation) -> dict[str, Any]:
    """The parameters this confirmation actually agrees to. Anything the user named overrides the echo, so
    a yes that names a different person, amount or invoice no longer matches it, and the machine declines."""
    params = dict(echoed)
    people = confirmation.mentions.get("people", [])
    person_key = "recipient" if "recipient" in params else "payee" if "payee" in params else None
    if person_key and people and params[person_key] not in people:
        params[person_key] = people[0]
    for key in ("amount", "invoice_id"):
        if key in confirmation.mentions and key in params:
            params[key] = confirmation.mentions[key]
    return params


def spoken_values(tool: str, p: dict[str, Any], world: World) -> dict[str, str | None]:
    """What each parameter value must sound like in its echo. This is derived from the World by code,
    independently of render_echo's wording, so the echo-completeness check compares two sources. Every
    parameter is mapped; a None value (no invoice for a person payment) has nothing to state."""
    forms: dict[str, str | None] = {}
    for key, value in p.items():
        if value is None:
            forms[key] = None
        elif key == "note_id":
            forms[key] = world.note(value)["text"]
        elif key == "form_id":
            forms[key] = world.forms[value]["name"]
        elif key == "request_id":
            forms[key] = world.approvals[value]["summary"]
        else:  # recipient, payee, amount, invoice_id are spoken as they are
            forms[key] = str(value)
    return forms


def render_echo(tool: str, p: dict[str, Any], world: World) -> str:
    """Read back exactly what will run: every parameter, in words, then ask for an explicit yes."""
    if tool == "send_note":
        what = f"send your note \"{world.note(p['note_id'])['text']}\" to {p['recipient']}"
    elif tool == "pay":
        what = f"pay {p['payee']} {p['amount']}" + (f" for invoice {p['invoice_id']}" if p.get("invoice_id") else "")
    elif tool == "delete_note":
        what = f"permanently delete your note \"{world.note(p['note_id'])['text']}\""
    elif tool == "submit_form":
        what = f"submit your {world.forms[p['form_id']]['name']}"
    elif tool == "approve_pending":
        request = world.approvals[p["request_id"]]
        what = f"approve the {request['kind']} request: {request['summary']}"
    else:
        what = f"run {tool} with {p}"
    return f"Just to confirm: you want me to {what}. Say yes to go ahead, or no to cancel."


def is_low_confidence(confidence: float | None) -> bool:
    return confidence is not None and confidence < LOW_CONFIDENCE
