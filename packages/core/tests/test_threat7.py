"""Slice 29: Threat 7 completion — persistent kill switch, spend caps and alerts, loop detection."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sensei_core.brief import build_brief, load_morning_fixture
from sensei_core.config import DEFAULT_MODELS_PATH, ConfigError, SpendCaps, load_budgets, load_models
from sensei_core.executor import BudgetExceeded, Executor, LoopDetected, SpendCapReached
from sensei_core.killswitch import KillSwitch, KillSwitchEngaged
from sensei_core.router import FakeClient, Router
from sensei_core.trace import TraceWriter, read_events

FIXTURE = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001" / "fixture"
# 4000 chars = 1000 estimated input tokens = $0.001 on cheap_api (est_cost_per_1k_tokens 0.001).
PROMPT = "x" * 4000
CALL_USD = 0.001 + 3 / 1000 * 0.001  # input plus the 3-token "cheap answer" reply


@pytest.fixture
def trace(tmp_path):
    return TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")


def clients():
    return {"local_private": FakeClient("local answer"), "cheap_api": FakeClient("cheap answer"),
            "frontier_api": FakeClient("frontier answer")}


def executor(trace, budgets=None, kill_switch=None, client_map=None):
    c = client_map or clients()
    return Executor(Router(load_models(), trace=trace), c, trace=trace, budgets=budgets, kill_switch=kill_switch), c


def events(trace, event_type):
    return [e for e in read_events(trace.path) if e["event_type"] == event_type]


def prompt(i):
    return PROMPT[:-len(str(i))] + str(i)  # distinct prompts, same length and cost


# ---- 1. the kill switch persists ------------------------------------------------------


def test_kill_switch_is_a_file_and_fails_closed(tmp_path, trace):
    switch = KillSwitch(tmp_path / "killed.json", trace)
    assert switch.engaged() is False
    switch.engage()
    assert json.loads((tmp_path / "killed.json").read_text())["killed"] is True and switch.engaged() is True
    switch.disengage()
    assert not (tmp_path / "killed.json").exists() and switch.engaged() is False
    for bad in ("{not json", '{"killed": false}', "[]", ""):
        (tmp_path / "killed.json").write_text(bad)
        assert switch.engaged() is True, bad  # corrupt or unexpected: treated as killed
    (tmp_path / "killed.json").unlink()
    (tmp_path / "killed.json").mkdir()  # unreadable as a file
    assert switch.engaged() is True
    reasons = [e["payload"]["reason"] for e in events(trace, "kill_state_unreadable")]
    assert reasons == ["invalid JSON", "unexpected content", "unexpected content", "invalid JSON",
                       "IsADirectoryError"]


def test_executor_refuses_every_call_while_killed_local_routes_included(tmp_path, trace):
    switch = KillSwitch(tmp_path / "killed.json", trace)
    ex, c = executor(trace, kill_switch=switch)
    switch.engage()
    for sensitivity in ("LOCAL_ONLY", "API_OK"):
        with pytest.raises(KillSwitchEngaged):
            ex.run_once("hello", sensitivity, "simple")
    assert all(not client.prompts for client in c.values())  # nothing was sent
    assert len(events(trace, "run_refused")) == 2 and not events(trace, "route_failure")


# ---- 2. spend caps refuse before sending ------------------------------------------------


def test_budgets_config_defaults_to_no_cap_and_rejects_typos(tmp_path):
    assert load_budgets() == SpendCaps(None, None)  # the shipped models.toml sets no cap
    shipped = DEFAULT_MODELS_PATH.read_text()
    for extra, ok in (("daily_cap_usd = 1.5\nmonthly_cap_usd = 20", True), ("daily_cap = 1.5", False),
                      ("daily_cap_usd = -1", False), ("daily_cap_usd = true", False)):
        path = tmp_path / f"models-{len(extra)}.toml"
        path.write_text(shipped + extra + "\n")
        if ok:
            assert load_budgets(path) == SpendCaps(1.5, 20.0)
        else:
            with pytest.raises(ConfigError):
                load_budgets(path)


def test_caps_need_a_trace_to_count_against():
    with pytest.raises(ValueError, match="need a trace"):
        Executor(Router(load_models()), clients(), budgets=SpendCaps(daily_cap_usd=1.0))


def test_daily_cap_refuses_before_the_call_is_sent(trace):
    ex, c = executor(trace, budgets=SpendCaps(daily_cap_usd=0.0025))
    ex.run_once(prompt(1), "API_OK", "simple")
    ex.run_once(prompt(2), "API_OK", "simple")
    with pytest.raises(SpendCapReached) as err:
        ex.run_once(prompt(3), "API_OK", "simple")
    assert isinstance(err.value, BudgetExceeded) and err.value.cap == "daily"
    assert len(c["cheap_api"].prompts) == 2  # the FakeClient never saw the third prompt
    [ev] = events(trace, "spend_cap_reached")
    assert ev["payload"]["cap"] == "daily" and ev["payload"]["cap_usd"] == 0.0025
    assert ev["payload"]["spent_est_cost_usd"] == pytest.approx(2 * CALL_USD)
    assert len(events(trace, "model_call")) == 2


def test_monthly_cap_counts_this_month_only(trace):
    now = datetime.now(timezone.utc)
    last_month = now.replace(day=1) - timedelta(days=1)
    for when, usd in ((last_month, 50.0), (now, 0.001)):  # earlier spend from the ledger
        record = {"timestamp": when.isoformat(), "event_type": "model_call",
                  "payload": {"route": "cheap_api", "est_cost_usd": usd}}
        with trace.path.open("a") as f:
            f.write(json.dumps(record) + "\n")
    ex, c = executor(trace, budgets=SpendCaps(monthly_cap_usd=0.0015))
    with pytest.raises(SpendCapReached) as err:
        ex.run_once(prompt(1), "API_OK", "simple")
    assert err.value.cap == "monthly" and err.value.period == now.date().isoformat()[:7]
    assert err.value.accumulated_cost_usd == pytest.approx(0.001)  # last month's $50 not counted
    assert c["cheap_api"].prompts == []


def test_local_routes_are_exempt_from_cost_caps(trace):
    ex, c = executor(trace, budgets=SpendCaps(daily_cap_usd=0.0, monthly_cap_usd=0.0))
    assert ex.run_once(prompt(1), "LOCAL_ONLY", "simple").route == "local_private"
    with pytest.raises(SpendCapReached):
        ex.run_once(prompt(2), "API_OK", "simple")
    assert len(c["local_private"].prompts) == 1 and c["cheap_api"].prompts == []


def test_no_caps_is_the_old_behaviour(trace):
    ex, c = executor(trace)
    for i in range(5):
        ex.run_once(prompt(i), "API_OK", "simple")
    assert len(c["cheap_api"].prompts) == 5 and not events(trace, "spend_alert")


# ---- 3. spend alerts fire once per threshold per day ------------------------------------


def test_alerts_fire_once_at_50_and_80_percent(trace):
    ex, c = executor(trace, budgets=SpendCaps(daily_cap_usd=0.0045))
    percents = []
    for i in range(4):  # spend after each call: 22%, 45%, 67%, 89%
        ex.run_once(prompt(i), "API_OK", "simple")
        percents.append([e["payload"]["percent"] for e in events(trace, "spend_alert")])
    assert percents == [[], [], [50], [50, 80]]
    # A new executor over the same trace (a restart) does not re-send today's alerts.
    ex2, _ = executor(trace, budgets=SpendCaps(daily_cap_usd=0.0045))
    with pytest.raises(SpendCapReached):
        ex2.run_once(prompt(9), "API_OK", "simple")
    assert [e["payload"]["percent"] for e in events(trace, "spend_alert")] == [50, 80]
    assert events(trace, "spend_alert")[0]["payload"]["day_utc"] == datetime.now(timezone.utc).date().isoformat()


def test_one_call_past_both_thresholds_fires_both_once(trace):
    ex, _ = executor(trace, budgets=SpendCaps(daily_cap_usd=0.00125))
    ex.run_once(prompt(1), "API_OK", "simple")  # 80.2% in one call
    assert [e["payload"]["percent"] for e in events(trace, "spend_alert")] == [50, 80]


# ---- 4. loop detection ------------------------------------------------------------------


def test_a_stuck_run_halts_on_the_third_identical_call(trace):
    ex, c = executor(trace)
    with ex.run_scope():
        ex.run_once("same prompt", "API_OK", "simple")
        ex.run_once("same prompt", "API_OK", "simple")
        with pytest.raises(LoopDetected) as err:
            ex.run_once("same prompt", "API_OK", "simple")
    assert err.value.successful_calls == 2 and err.value.route == "cheap_api"
    assert len(c["cheap_api"].prompts) == 2  # the third was never sent
    [ev] = events(trace, "loop_detected")
    assert ev["payload"]["prompt_sha256"].startswith("sha256:") and "same prompt" not in json.dumps(ev)
    assert not events(trace, "route_failure")  # a halt, not a retried failure


def test_separate_runs_and_different_prompts_do_not_count(trace):
    ex, c = executor(trace)
    for _ in range(4):  # each run_once outside a scope is its own run
        ex.run_once("same prompt", "API_OK", "simple")
    with ex.run_scope():
        for i in range(4):
            ex.run_once(f"prompt {i}", "API_OK", "simple")
    assert len(c["cheap_api"].prompts) == 8 and not events(trace, "loop_detected")


class Flaky:
    def __init__(self, failures):
        self.failures, self.prompts = failures, []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            raise ConnectionError("simulated outage")
        return "recovered"


def test_retries_of_a_failed_call_are_exempt(trace):
    flaky = Flaky(failures=3)
    ex, _ = executor(trace, client_map={"local_private": flaky})
    with ex.run_scope():  # 3 failed attempts then a success, then one more success: 5 identical asks
        assert ex.run_once("same prompt", "LOCAL_ONLY", "simple", max_retries=3).response == "recovered"
        ex.run_once("same prompt", "LOCAL_ONLY", "simple")
    assert len(flaky.prompts) == 5 and not events(trace, "loop_detected")


def test_a_normal_brief_run_with_repair_is_not_halted(tmp_path, trace):
    data = load_morning_fixture(FIXTURE)

    class DraftThenSame:  # the draft fails a check, so the brief repairs: two calls in one run
        prompts = []

        def complete(self, prompt):
            self.prompts.append(prompt)
            return "a brief that misses everything"

    client = DraftThenSame()
    ex, _ = executor(trace, client_map={"local_private": client})
    result = build_brief(ex, data, tmp_path / "brief.md", trace=trace, repair=True, assemble_inbox=True)
    assert result.attempts == 2 and len(client.prompts) == 2
    assert not events(trace, "loop_detected")
