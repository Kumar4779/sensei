"""The confirmation-echo state machine on its own: proposed → echoed → confirmed → executed."""

import pytest

from sensei_core.trace import TraceWriter, read_events

from voice.confirm import ConfirmationMachine, State, UnverifiedAction

PARAMS = {"payee": "Acme Paper", "amount": "$1,250.00", "invoice_id": "INV-204"}


@pytest.fixture
def machine(trace_path):
    return ConfirmationMachine(TraceWriter(trace_path, run_id="m", session_id="m"))


def _echoed(machine):
    action = machine.propose("pay", dict(PARAMS))
    machine.echo(action.action_id, lambda tool, p: f"pay {p['payee']} {p['amount']}")
    return action


def test_happy_path_walks_every_state_once(machine, trace_path):
    action = _echoed(machine)
    assert action.state is State.ECHOED and action.echo_text == "pay Acme Paper $1,250.00"
    machine.confirm(action.action_id, dict(PARAMS))
    assert action.state is State.CONFIRMED and action.receipt
    machine.redeem(action.receipt, "pay", dict(PARAMS))
    assert action.state is State.EXECUTED and action.receipt is None
    kinds = [e["event_type"] for e in read_events(trace_path)]
    assert kinds == ["action_proposed", "action_echoed", "action_confirmed"]


def test_nothing_executes_from_proposed(machine):
    action = machine.propose("pay", dict(PARAMS))
    assert action.receipt is None
    with pytest.raises(UnverifiedAction):
        machine.confirm(action.action_id, dict(PARAMS))  # no echo yet
    with pytest.raises(UnverifiedAction):
        machine.redeem(None, "pay", dict(PARAMS))
    assert machine.run_failed and action.state is State.PROPOSED


def test_the_echo_freezes_the_exact_parameters(machine):
    params = dict(PARAMS)
    action = machine.propose("pay", params)
    params["amount"] = "$9,999.00"  # the caller's dict changing later changes nothing
    machine.echo(action.action_id, lambda tool, p: p["amount"])
    assert action.echo_text == "$1,250.00" and action.echoed_params == PARAMS
    with pytest.raises(UnverifiedAction):
        machine.echo(action.action_id, lambda tool, p: "again")  # an echo happens once


@pytest.mark.parametrize("change", [{"amount": "$1,250.01"}, {"payee": "Acme Paper Inc"}, {"invoice_id": "INV-240"},
                                    {"extra": True}])
def test_a_confirmation_must_match_the_echo_exactly(machine, change):
    action = _echoed(machine)
    machine.confirm(action.action_id, {**PARAMS, **change})
    assert action.state is State.DECLINED and action.receipt is None
    assert not machine.run_failed  # a mismatched yes is a decline, not a violation


def test_a_second_confirmation_is_a_no_op(machine, trace_path):
    action = _echoed(machine)
    machine.confirm(action.action_id, dict(PARAMS))
    receipt = action.receipt
    machine.confirm(action.action_id, dict(PARAMS))
    assert action.receipt == receipt  # no second receipt
    machine.redeem(receipt, "pay", dict(PARAMS))
    machine.confirm(action.action_id, dict(PARAMS))
    assert action.state is State.EXECUTED
    with pytest.raises(UnverifiedAction):
        machine.redeem(receipt, "pay", dict(PARAMS))
    ignored = [e for e in read_events(trace_path) if e["event_type"] == "confirmation_ignored"]
    assert len(ignored) == 2


def test_a_receipt_cannot_run_a_different_tool(machine):
    action = _echoed(machine)
    machine.confirm(action.action_id, dict(PARAMS))
    with pytest.raises(UnverifiedAction):
        machine.redeem(action.receipt, "send_note", dict(PARAMS))
    assert machine.run_failed


def test_declined_actions_cannot_be_confirmed_or_run(machine):
    action = _echoed(machine)
    machine.decline(action.action_id, "user said no")
    with pytest.raises(UnverifiedAction):
        machine.confirm(action.action_id, dict(PARAMS))
    assert action.state is State.DECLINED
