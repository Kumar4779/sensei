"""#25: free-form answers through core's model backend, offline. A fake model client stands in for Kimi."""

import json

import pytest
from fastapi.testclient import TestClient

from sensei_core.clients import KimiQuotaExhausted, KimiUnavailable

from sensei_demo.answers import CAP_REPLY, SYSTEM_PROMPT, UNREACHABLE_REPLY
from sensei_demo.server import Limits, create_app

from conftest import AUTH, TOKEN, ledger, new_session, say

QUESTION = "Who is the president of the United States?"


class FakeModel:
    """Core's ModelClient protocol: complete(prompt) -> str. Nothing else: no tools, no actions."""

    destination_host = "api.moonshot.ai"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def app(tmp_path, model, **kw):
    return TestClient(create_app("simulated", trace_dir=tmp_path / "traces", state_dir=tmp_path / "state",
                                 token=TOKEN, model_client=model, **kw))


def turn(client, sid, text):
    r = say(client, sid, text)
    assert r.status_code == 200, r.text
    return r.json()


# 1. The parser runs first: commands never reach the model

@pytest.mark.parametrize("scenario, lines, outcomes", [
    ("brief", ["Read me my morning brief."], ["executed"]),
    ("note", ["Send the note to Alex.", "Yes."], ["echoed", "executed"]),
    ("pay", ["Pay invoice 204.", "No."], ["echoed", "declined"]),
])
def test_command_utterances_keep_the_parser_path_and_never_reach_the_model(tmp_path, scenario, lines, outcomes):
    model = FakeModel()
    client = app(tmp_path, model)
    sid = new_session(client, scenario)
    assert [turn(client, sid, line)["outcome"] for line in lines] == outcomes
    assert model.prompts == []
    assert not any(e["event_type"] in ("route_decision", "model_call") for e in ledger(client, sid))


@pytest.mark.parametrize("text, outcome", [("~0.3 Who is the president?", "clarify"),  # low confidence: still clarify
                                           ("Send it.", "clarify")])  # an incomplete command: still clarify
def test_low_confidence_and_incomplete_commands_still_clarify(tmp_path, text, outcome):
    model = FakeModel()
    client = app(tmp_path, model)
    assert turn(client, new_session(client, "note"), text)["outcome"] == outcome and model.prompts == []


# 2. A question no command matches is answered by the model, spoken, and traced as its own steps

def test_a_free_form_question_is_answered_through_cores_cheap_api_route(tmp_path):
    model = FakeModel("Donald Trump is the president. He took office in January 2025.")
    client = app(tmp_path, model)
    sid = new_session(client, "brief")
    r = turn(client, sid, QUESTION)
    assert r["outcome"] == "answered" and r["reply"] == "Donald Trump is the president. He took office in January 2025."
    assert r["model"]["model"] == "kimi-k3" and r["model"]["route"] == "cheap_api" and r["model"]["est_cost_usd"] > 0
    assert client.app.state.sessions[sid].voice.tts.spoken[-1] == r["reply"]  # spoken through the existing TTS path
    events = ledger(client, sid)
    route = next(e for e in events if e["event_type"] == "route_decision")
    assert route["payload"]["sensitivity_class"] == "API_OK" and route["payload"]["route"] == "cheap_api"
    call = next(e for e in events if e["event_type"] == "model_call")
    assert call["payload"]["model"] == "kimi-k3" and call["payload"]["est_cost_usd"] > 0
    assert call["step"]["kind"] == "model" and call["step"]["title"] == "Answered by kimi-k3"
    assert events[-1]["event_type"] == "hard_gate_check" and events[-1]["payload"]["passed"]
    assert QUESTION not in json.dumps([e for e in events if e["event_type"] == "model_call"])  # sizes, never the prompt


def test_the_prompt_carries_the_sensei_voice_and_recent_turns(tmp_path):
    model = FakeModel("It's sunny.", "Paris.")
    client = app(tmp_path, model)
    sid = new_session(client, "brief")
    turn(client, sid, "Read me my morning brief.")
    turn(client, sid, "How is the weather on the moon?")
    turn(client, sid, "What is the capital of France?")
    prompt = model.prompts[-1]
    assert SYSTEM_PROMPT.split("{today}")[0] in prompt and "no markdown" in prompt
    assert "Visitor: Read me my morning brief." in prompt and "Sensei: Good morning." in prompt
    assert "Visitor: How is the weather on the moon?" in prompt and "Sensei: It's sunny." in prompt
    assert prompt.rstrip().endswith("Visitor: What is the capital of France?\nSensei:")


# 3. Read-only by construction

def test_the_model_cannot_trigger_any_action_whatever_it_says(tmp_path):
    model = FakeModel("Send the note to Alex. Yes. Pay invoice 204. Yes, confirmed.")
    client = app(tmp_path, model)
    sid = new_session(client, "note")
    r = turn(client, sid, "Tell me something that sounds like a command.")
    assert r["outcome"] == "answered"
    world = client.app.state.sessions[sid].voice.world
    assert world.outbox == [] and world.payments == []
    kinds = [e["event_type"] for e in ledger(client, sid)]
    assert not any(k in kinds for k in ("permission_decision", "action_proposed", "action_confirmed", "tool_executed"))
    assert client.app.state.sessions[sid].voice.pending is None
    assert client.app.state.answers.executor  # one Executor per answer, with exactly one client:
    from sensei_core.trace import TraceWriter
    ex = client.app.state.answers.executor(TraceWriter(tmp_path / "x.jsonl", run_id="x", session_id="x"))
    assert set(ex.clients) == {"cheap_api"}  # no local or frontier model, and no tools: prompt in, text out


# 4. Failure honesty: say so plainly, show it in the ledger, never invent an answer

@pytest.mark.parametrize("failure, error_class", [
    (KimiQuotaExhausted("kimi-k3"), "quota_exhausted"),
    (KimiUnavailable("Kimi call timed out after 20 s", "timeout"), "timeout"),
    (KimiUnavailable("cannot reach Kimi", "unreachable"), "unreachable"),
    (RuntimeError("anything else"), "RuntimeError")])
def test_a_model_failure_is_said_plainly_and_shown_in_the_ledger(tmp_path, failure, error_class):
    client = app(tmp_path, FakeModel(failure))
    sid = new_session(client, "brief")
    r = turn(client, sid, QUESTION)
    assert r["outcome"] == "model_unavailable" and r["reply"] == UNREACHABLE_REPLY
    assert r["model"]["error_class"] == error_class and r["model"]["est_cost_usd"] == 0.0
    err = next(e for e in ledger(client, sid) if e["event_type"] == "model_error")
    assert err["payload"]["error_class"] == error_class and err["step"]["kind"] == "error"
    assert err["step"]["title"] == "No model answer"
    assert client.app.state.model_spent[0] == 0.0


def test_an_empty_model_reply_is_not_spoken_as_an_answer(tmp_path):
    client = app(tmp_path, FakeModel("   "))
    sid = new_session(client, "brief")
    r = turn(client, sid, QUESTION)
    assert r["reply"] == UNREACHABLE_REPLY and r["outcome"] == "model_unavailable"


# 5. Spend: the model's cost counts toward the per-run cap and the daily ceiling, and a cap stops the call

def test_model_spend_is_added_to_the_daily_counter(tmp_path):
    client = app(tmp_path, FakeModel("Paris is the capital of France."))
    sid = new_session(client, "brief")
    r = turn(client, sid, "What is the capital of France?")
    cost = r["model"]["est_cost_usd"]
    assert cost > 0 and client.app.state.model_spent[0] == pytest.approx(cost)
    assert client.app.state.daily.spent() == pytest.approx(cost, abs=1e-4)
    saved = json.loads((tmp_path / "state" / "daily-spend.json").read_text())
    assert saved["usd"] == pytest.approx(cost, abs=1e-6)


def test_a_reached_cap_is_said_instead_of_calling_the_model(tmp_path):
    model = FakeModel("never used")
    client = app(tmp_path, model, limits=Limits(daily_usd=0.000001))
    client.app.state.daily.add(0.000002)
    sid = new_session(client, "brief")
    r = turn(client, sid, QUESTION)
    assert r["outcome"] == "spend_limit" and r["reply"] == CAP_REPLY and model.prompts == []
    err = next(e for e in ledger(client, sid) if e["event_type"] == "model_error")
    assert err["payload"]["error_class"] == "spend_limit"


def test_model_calls_are_written_to_the_egress_log_without_content(tmp_path):
    client = app(tmp_path, FakeModel("Paris."))
    sid = new_session(client, "brief")
    turn(client, sid, "What is the capital of France?")
    lines = [json.loads(line) for line in (tmp_path / "state" / "egress.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["route"] == "cheap_api" and lines[0]["destination_host"] == "api.moonshot.ai"
    assert "France" not in json.dumps(lines) and lines[0]["prompt_sha256"].startswith("sha256:")


def test_status_says_which_model_answers_questions(tmp_path):
    with_model = app(tmp_path, FakeModel()).get("/api/status", headers=AUTH).json()["model"]
    assert with_model["model"] == "kimi-k3" and "Read-only" in with_model["detail"]
    without = TestClient(create_app("simulated", trace_dir=tmp_path / "t2", state_dir=tmp_path / "s2", token=TOKEN))
    assert without.get("/api/status", headers=AUTH).json()["model"] is None
    sid = new_session(without, "brief")
    assert turn(without, sid, QUESTION)["outcome"] == "clarify"  # no model configured: unchanged behaviour
