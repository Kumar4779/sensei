"""Slice 35: KimiClient contract tests against a stubbed HTTP layer (no network), and the gates.

Every response here is a canned reply shaped like Moonshot's documented chat-completions
response. Nothing in this file contacts api.moonshot.ai or reads the real Keychain.
"""

import io
import json
import socket
import subprocess
import urllib.error

import pytest

from sensei_core import credentials
from sensei_core import replay as replay_mod
from sensei_core.clients import KIMI_BASE_URL, KimiClient, KimiKeyMissing, KimiQuotaExhausted, KimiUnavailable
from sensei_core.config import load_models
from sensei_core.containment import check_egress
from sensei_core.egress import EgressLog
from sensei_core.executor import Executor
from sensei_core.router import FakeClient, Router
from sensei_core.runs import RunStore
from sensei_core.trace import TraceWriter, read_events

KEY = "sk-test-NEVER-LOGGED-7f3a9c1e"  # distinctive, so a leak anywhere is findable


def reply(content="Hello from Kimi."):
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": "kimi-k3",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content,
                                                 "reasoning_content": "thinking..."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}}


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Stub:
    """Stands in for urllib.request.urlopen: records each request and returns or raises."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return Response(json.dumps(outcome).encode() if isinstance(outcome, dict) else outcome)


def http_error(code, body=None):
    body = body if body is not None else {"error": {"message": "nope", "type": "invalid_authentication_error"}}
    return urllib.error.HTTPError(KIMI_BASE_URL + "/chat/completions", code, "error", {},
                                  io.BytesIO(json.dumps(body).encode()))


def client(*outcomes, key=KEY, **kw):
    stub = Stub(*outcomes)
    return KimiClient("kimi-k3", key=lambda: key, opener=stub, **kw), stub


# ---- the HTTP contract ------------------------------------------------------------------


def test_request_shape():
    c, stub = client(reply(), timeout=7.5)
    assert c.complete("What is 1+1?") == "Hello from Kimi."
    [(request, timeout)] = stub.calls
    assert request.full_url == "https://api.moonshot.ai/v1/chat/completions" and request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"model": "kimi-k3", "messages": [{"role": "user", "content": "What is 1+1?"}],
                                        "stream": False, "max_completion_tokens": 4096, "reasoning_effort": "low"}
    assert timeout == 7.5


def test_the_route_config_sets_the_model():
    route = load_models()["cheap_api"]
    assert route.provider == "kimi" and route.model == "kimi-k3"
    assert KimiClient.for_route(route).model == "kimi-k3"


def test_response_parsing_takes_the_message_content_only():
    c, _ = client(reply("The answer is 2."))
    assert c.complete("q") == "The answer is 2."  # reasoning_content is not part of the answer


@pytest.mark.parametrize("body", [b"not json", {"choices": []}, {"choices": [{"message": {}}]},
                                  {"choices": [{"message": {"content": ""}}]}, {"error": {"message": "x"}}])
def test_a_malformed_reply_is_a_route_failure(body):
    c, _ = client(body)
    with pytest.raises(KimiUnavailable) as err:
        c.complete("q")
    assert err.value.error_class == "bad_response" and not err.value.fail_closed


@pytest.mark.parametrize("code,error_class", [(401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited"),
                                              (500, "server_error"), (503, "server_error"), (504, "server_error"),
                                              (400, "bad_request")])
def test_http_errors_map_to_route_failure_classes(code, error_class):
    c, _ = client(http_error(code))
    with pytest.raises(KimiUnavailable) as err:
        c.complete("q")
    assert err.value.error_class == error_class and not err.value.fail_closed
    assert f"HTTP {code}" in str(err.value)


@pytest.mark.parametrize("exc", [socket.timeout("timed out"), TimeoutError(),
                                 urllib.error.URLError(socket.timeout("timed out"))])
def test_the_timeout_is_honoured(exc):
    c, stub = client(exc, timeout=3.0)
    with pytest.raises(KimiUnavailable, match=r"timed out after 3 s") as err:
        c.complete("q")
    assert err.value.error_class == "timeout" and stub.calls[0][1] == 3.0


def test_unreachable_is_a_route_failure():
    c, _ = client(urllib.error.URLError(ConnectionRefusedError()))
    with pytest.raises(KimiUnavailable) as err:
        c.complete("q")
    assert err.value.error_class == "unreachable"


def test_the_key_is_read_at_call_time_every_call():
    keys = iter([KEY, None])
    c = KimiClient("kimi-k3", key=lambda: next(keys), opener=Stub(reply()))
    assert c.complete("q") == "Hello from Kimi."
    with pytest.raises(KimiKeyMissing):  # removed from the Keychain since: the next call fails closed
        c.complete("q")
    assert not any(KEY in repr(v) for v in vars(c).values())  # never kept on the client


def test_key_absent_fails_closed_before_any_request():
    c, stub = client(reply(), key=None)
    with pytest.raises(KimiKeyMissing, match="sensei-kimi-api-key") as err:
        c.complete("q")
    assert err.value.fail_closed and err.value.error_class == "key_absent" and stub.calls == []


# ---- the credential broker --------------------------------------------------------------


def test_broker_reads_the_keychain_entry(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=KEY + "\n", stderr="")

    monkeypatch.setattr(credentials.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(credentials.subprocess, "run", run)
    assert credentials.keychain_secret(credentials.KIMI_KEY_SERVICE) == KEY
    assert seen == [["/usr/bin/security", "find-generic-password", "-a", "sensei", "-s", "sensei-kimi-api-key", "-w"]]


@pytest.mark.parametrize("outcome", ["missing", "empty", "timeout", "no-security"])
def test_broker_returns_none_when_there_is_no_key(monkeypatch, outcome):
    def run(argv, **kw):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 10)
        code, out = (44, "") if outcome == "missing" else (0, "\n")
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="could not be found")

    monkeypatch.setattr(credentials.shutil, "which", lambda name: None if outcome == "no-security" else "/x/security")
    monkeypatch.setattr(credentials.subprocess, "run", run)
    assert credentials.keychain_secret("sensei-kimi-api-key") is None


# ---- behind the gates -------------------------------------------------------------------


def executor(tmp_path, kimi):
    trace = TraceWriter(tmp_path / "traces" / "gateway.jsonl", run_id="r", session_id="s")
    clients = {"local_private": FakeClient("local"), "cheap_api": kimi, "frontier_api": FakeClient("frontier")}
    ex = Executor(Router(load_models(), trace=trace), clients, trace=trace, egress=EgressLog(tmp_path / "egress.jsonl"),
                  runs=RunStore(tmp_path / "runs"))
    return ex, trace, clients


def types(trace):
    return [e["event_type"] for e in read_events(trace.path)]


def test_key_absent_traces_route_failure_and_never_substitutes_a_route(tmp_path):
    kimi, stub = client(reply(), key=None)
    ex, trace, clients = executor(tmp_path, kimi)
    with pytest.raises(KimiKeyMissing):
        ex.run_once("plan my week", "API_OK", "standard")  # standard would escalate on an ordinary failure
    [failure] = [e for e in read_events(trace.path) if e["event_type"] == "route_failure"]
    assert failure["payload"] == {"route": "cheap_api", "model": "kimi-k3", "error_class": "key_absent",
                                  "fail_closed": True}
    assert "run_escalated" not in types(trace) and "run_retried" not in types(trace)
    assert clients["frontier_api"].prompts == [] and clients["local_private"].prompts == []
    assert stub.calls == [] and "run_failed" in types(trace)


@pytest.mark.parametrize("code", [401, 429, 500])
def test_http_failures_keep_the_existing_escalation(tmp_path, code):
    # standard: one failure on cheap_api escalates to frontier_api (Slice 23 rules, unchanged).
    kimi, _ = client(http_error(code), reply())
    ex, trace, clients = executor(tmp_path, kimi)
    result = ex.run_once("plan my week", "API_OK", "standard")
    assert result.route == "frontier_api" and result.escalated
    [failure] = [e["payload"] for e in read_events(trace.path) if e["event_type"] == "route_failure"]
    assert failure["route"] == "cheap_api" and failure["error_class"] == {401: "auth_failed", 429: "rate_limited",
                                                                         500: "server_error"}[code]
    assert "run_escalated" in types(trace)


def test_a_simple_task_retries_the_same_route_never_another(tmp_path):
    kimi, stub = client(http_error(503), reply("second time lucky"))
    ex, trace, clients = executor(tmp_path, kimi)
    result = ex.run_once("hi", "API_OK", "simple")
    assert result.route == "cheap_api" and result.response == "second time lucky" and len(stub.calls) == 2
    assert "run_retried" in types(trace) and clients["frontier_api"].prompts == []


# The real key shape (sk-...) is also scrubbed by the trace writer's redaction (Slice 13). The
# second key matches no redaction pattern, so this test proves the client itself never passes
# the key on, without leaning on that second layer.
@pytest.mark.parametrize("key", [KEY, "plainKimiKey0000NOREDACTIONPATTERN"])
def test_the_key_never_reaches_a_trace_an_egress_record_or_a_run_record(tmp_path, key):
    def echo():  # the server's error body echoes the key back: the client must not pass the body on
        return http_error(401, {"error": {"message": f"Invalid key {key}", "type": "invalid_authentication_error"}})

    kimi, _ = client(echo(), echo(), reply(), urllib.error.URLError(socket.timeout()), echo(), key=key)
    ex, trace, _ = executor(tmp_path, kimi)
    errors = []
    for difficulty in ("simple", "simple", "standard"):  # fail + fail, success, failures with escalation
        try:
            ex.run_once("say hi", "API_OK", difficulty)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e} {e.__cause__!r} {e.__context__!r}")
    kimi_missing, _ = client(reply(), key=None)
    ex2, _, _ = executor(tmp_path, kimi_missing)
    with pytest.raises(KimiKeyMissing) as err:
        ex2.run_once("say hi", "API_OK", "simple")
    errors.append(str(err.value))
    egress = (tmp_path / "egress.jsonl").read_text()
    assert '"destination_host": "api.moonshot.ai"' in egress and '"error_class": "KimiUnavailable"' in egress
    everything = [trace.path.read_text(), egress, *(p.read_text() for p in (tmp_path / "runs").glob("*.json")), *errors]
    assert all(key not in text for text in everything)
    assert all(e not in text for text in everything for e in ("[REDACTED]",)), "nothing needed scrubbing"
    assert {"route_failure", "run_retried", "run_escalated", "model_call"} <= set(types(trace))


# ---- Slice 36: a quota-exhausted 429 fails closed; a rate-limit 429 does not ----------------

QUOTA = {"error": {"message": "Your account is suspended due to insufficient balance",
                   "type": "exceeded_current_quota_error"}}
RATE_LIMIT = {"error": {"message": "rate limit reached for requests", "type": "rate_limit_reached_error"}}


def test_the_client_tells_the_two_429s_apart():
    c, _ = client(http_error(429, QUOTA))
    with pytest.raises(KimiQuotaExhausted) as quota:
        c.complete("q")
    assert quota.value.fail_closed and quota.value.error_class == "quota_exhausted"
    assert "exceeded_current_quota_error" in str(quota.value) and "insufficient balance" not in str(quota.value)
    for body in (RATE_LIMIT, {"error": {"message": "x"}}, {}):  # any other 429, typed or not, is a rate limit
        c, _ = client(http_error(429, body))
        with pytest.raises(KimiUnavailable) as other:
            c.complete("q")
        assert type(other.value) is KimiUnavailable and other.value.error_class == "rate_limited"
        assert not other.value.fail_closed


@pytest.mark.parametrize("difficulty", ["simple", "standard"])
def test_a_quota_429_makes_one_call_and_fails_the_run(tmp_path, difficulty):
    kimi, stub = client(http_error(429, QUOTA), reply())  # a second call would succeed: it must not happen
    ex, trace, clients = executor(tmp_path, kimi)
    with pytest.raises(KimiQuotaExhausted):
        ex.run_once("plan my week", "API_OK", difficulty)
    assert len(stub.calls) == 1
    assert clients["frontier_api"].prompts == [] and clients["local_private"].prompts == []
    [failure] = [e["payload"] for e in read_events(trace.path) if e["event_type"] == "route_failure"]
    assert failure == {"route": "cheap_api", "model": "kimi-k3", "error_class": "quota_exhausted", "fail_closed": True}
    t = types(trace)
    assert "run_retried" not in t and "run_escalated" not in t and "model_call" not in t
    assert t[t.index("route_failure") + 1] == "run_failed"
    [record] = (tmp_path / "runs").glob("*.json")
    assert json.loads(record.read_text())["status"] == "failed"


def test_a_rate_limit_429_still_retries_then_escalates(tmp_path):
    # simple: retried once on the same route; standard: escalated to frontier_api (Slice 23, unchanged)
    kimi, stub = client(http_error(429, RATE_LIMIT), reply("after the burst"))
    ex, trace, clients = executor(tmp_path, kimi)
    assert ex.run_once("hi", "API_OK", "simple").response == "after the burst" and len(stub.calls) == 2
    kimi2, stub2 = client(http_error(429, RATE_LIMIT), reply())
    ex2, trace2, clients2 = executor(tmp_path / "standard", kimi2)
    result = ex2.run_once("plan my week", "API_OK", "standard")
    assert result.route == "frontier_api" and result.escalated and len(stub2.calls) == 1
    assert clients2["frontier_api"].prompts == ["plan my week"]
    for tr, follow in ((trace, "run_retried"), (trace2, "run_escalated")):
        failures = [e["payload"] for e in read_events(tr.path) if e["event_type"] == "route_failure"]
        assert failures == [{"route": "cheap_api", "model": "kimi-k3", "error_class": "rate_limited"}]
        assert follow in types(tr)


def test_the_distinction_survives_replay(tmp_path):
    kimi, _ = client(http_error(429, QUOTA), http_error(429, RATE_LIMIT), reply(), http_error(429, RATE_LIMIT),
                     reply(), reply(), key=KEY)
    ex, trace, _ = executor(tmp_path, kimi)
    with pytest.raises(KimiQuotaExhausted):
        ex.run_once("plan my week", "API_OK", "standard")  # quota: stops
    ex.run_once("hi", "API_OK", "simple")  # rate limit: retried, completes
    ex.run_once("plan my month", "API_OK", "standard")  # rate limit: escalated, completes
    with pytest.raises(KimiKeyMissing):  # Slice 35's other fail-closed case
        executor(tmp_path, client(reply(), key=None)[0])[0].run_once("plan", "API_OK", "standard")
    result = replay_mod.replay(trace.path)
    assert result.matched, replay_mod.format_result(trace.path, result)
    assert result.runs == 4

    # Drop the recorded fail_closed fact: replay then derives an escalation the trace does not have.
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text(trace.path.read_text().replace(', "fail_closed": true', "", 1))
    assert tampered.read_text() != trace.path.read_text()
    bad = replay_mod.replay(tampered)
    report = replay_mod.format_result(tampered, bad)
    # Replay re-decides the escalation (a route_decision), but the trace recorded the stop.
    assert not bad.matched and "expected route_decision, recorded run_failed" in report


def test_the_probe_reports_cheap_api_as_a_real_client(tmp_path, monkeypatch):
    from sensei_core.gateway import local_setup

    monkeypatch.setenv("SENSEI_LOCAL_MODEL", "qwen3:8b")
    ex, _, _ = local_setup(tmp_path)  # builds clients only; no network, no Keychain read
    assert isinstance(ex.clients["cheap_api"], KimiClient)
    r = check_egress(ex.routes, ex.clients)
    assert "cheap_api (real client)" in r.evidence and "frontier_api (fake client)" in r.evidence
    assert r.status == "warn" and "LOGGED, NOT ENFORCED" in r.evidence  # still not enforced
