"""Slice 32: the containment self-probe. Each check is tested against synthetic setups."""

import json
import os
import shutil
from pathlib import Path

import pytest

from sensei_core import containment as c
from sensei_core.config import load_models
from sensei_core.killswitch import KillSwitch
from sensei_core.router import FakeClient

TASKS = Path(__file__).resolve().parents[3] / "evals" / "tasks"


def chmod(path, mode):
    os.chmod(path, mode)
    return path


# ---- a. manifests -----------------------------------------------------------------------


def manifests(tmp_path, mode_tools=0o644, mode_models=0o644):
    tools, models = tmp_path / "tools.toml", tmp_path / "models.toml"
    tools.write_text("# tools\n")
    models.write_text("# models\n")
    chmod(tools, mode_tools)
    chmod(models, mode_models)
    return {"tools.toml": tools, "models.toml": models}


def test_manifests_pass_warn_fail_by_who_can_write(tmp_path):
    assert c.check_manifests(manifests(tmp_path)).status == "pass"
    r = c.check_manifests(manifests(tmp_path, mode_models=0o664))
    assert r.status == "warn" and "models.toml writable by the file's group" in r.evidence
    r = c.check_manifests(manifests(tmp_path, mode_tools=0o666))
    assert r.status == "fail" and "tools.toml writable by any local user" in r.evidence and "666" in r.evidence
    missing = manifests(tmp_path)
    missing["tools.toml"].unlink()
    assert c.check_manifests(missing).status == "fail"


def test_the_shipped_manifests_are_checked_by_default():
    r = c.check_manifests()
    assert r.check == "manifests" and "tools.toml" in r.evidence and "models.toml" in r.evidence


# ---- b. ground truth --------------------------------------------------------------------


def test_the_real_tasks_checkers_are_unreachable_from_a_sandbox():
    r = c.check_ground_truth(environ={})
    assert r.status == "pass", r.evidence
    assert "not an OS sandbox" in r.evidence  # never more than it can show


def planted(tmp_path, where):
    tasks = tmp_path / "tasks"
    task = tasks / "morning-brief-001"
    shutil.copytree(TASKS / "morning-brief-001", task)
    if where == "fixture":  # a task that ships its checker inside the fixture the trial is handed
        shutil.copy(task / "checker.py", task / "fixture" / "notes.py")
    return tasks, task


def test_a_sandbox_reaching_a_planted_checker_fails(tmp_path):
    tasks, _ = planted(tmp_path, "fixture")
    r = c.check_ground_truth(tasks, environ={})
    assert r.status == "fail" and "a copy of the checker in the sandbox" in r.evidence


def test_an_environment_naming_the_checker_fails(tmp_path):
    tasks, task = planted(tmp_path, None)
    assert c.check_ground_truth(tasks, environ={}).status == "pass"
    r = c.check_ground_truth(tasks, environ={"GRADER": str(task / "checker.py")})
    assert r.status == "fail" and "env GRADER names grading material" in r.evidence


def test_no_tasks_is_a_warn_not_a_pass(tmp_path):
    r = c.check_ground_truth(tmp_path / "none", environ={})
    assert r.status == "warn" and "nothing probed" in r.evidence


# ---- c. egress: the wording is pinned ---------------------------------------------------


def test_egress_never_claims_enforcement():
    assert c.EGRESS_NOT_ENFORCED == "egress enforcement not built: egress is LOGGED, NOT ENFORCED"
    routes = load_models()
    for clients in (None, {"cheap_api": FakeClient(), "frontier_api": FakeClient()}):
        r = c.check_egress(routes, clients)
        assert r.status == "warn"
        assert r.evidence.startswith(c.EGRESS_NOT_ENFORCED + "; ")
        assert "enforced" not in r.evidence.lower().replace("not enforced", "")  # no claim of enforcement
    r = c.check_egress(routes, {"cheap_api": FakeClient(), "frontier_api": FakeClient()})
    assert r.evidence.endswith("API routes configured: cheap_api (fake client), frontier_api (fake client)")
    local_only = {"local_private": routes["local_private"]}
    assert c.check_egress(local_only).evidence.endswith("no API route configured")


# ---- d. kill state ----------------------------------------------------------------------


def test_kill_state_reports_the_file_and_honours_it(tmp_path):
    switch = KillSwitch(tmp_path / "killed.json")
    assert c.check_kill_state(switch).status == "pass"
    switch.engage()
    r = c.check_kill_state(switch)
    assert r.status == "warn" and r.evidence.startswith("ENGAGED") and "refused until POST /revive" in r.evidence
    (tmp_path / "killed.json").write_text("{corrupt")
    assert c.check_kill_state(switch).status == "warn"  # unreadable: fails closed, still engaged


def test_a_kill_file_that_is_not_honoured_fails(tmp_path, monkeypatch):
    switch = KillSwitch(tmp_path / "killed.json")
    switch.engage()
    monkeypatch.setattr(switch, "engaged", lambda: False)  # a regression that ignored the file
    assert c.check_kill_state(switch).status == "fail"


# ---- e. trace store ---------------------------------------------------------------------


def test_trace_store_pass_warn_fail_by_who_can_read(tmp_path):
    traces = tmp_path / "traces"
    assert c.check_trace_store(traces).status == "pass"  # nothing there yet
    traces.mkdir()
    f = traces / "gateway.jsonl"
    f.write_text("{}\n")
    chmod(traces, 0o700)
    chmod(f, 0o600)
    assert c.check_trace_store(traces).status == "pass"
    chmod(f, 0o640)
    r = c.check_trace_store(traces)
    assert r.status == "warn" and "gateway.jsonl 640" in r.evidence and "chmod -R go-rwx" in r.evidence
    chmod(traces, 0o755)
    r = c.check_trace_store(traces)
    assert r.status == "fail" and "readable by any local user: the directory 755" in r.evidence


# ---- f. token ---------------------------------------------------------------------------


def test_token_reports_length_only():
    assert c.check_token(None).status == "fail"
    assert c.check_token("").status == "fail"
    r = c.check_token("short-token")
    assert r.status == "fail" and "11 characters" in r.evidence and "short-token" not in r.evidence
    r = c.check_token("a-long-enough-token-value")
    assert r.status == "pass" and r.evidence.endswith("25 characters") and "a-long-enough" not in r.evidence


# ---- the whole probe and the CLI --------------------------------------------------------


def clean_home(tmp_path):
    home = tmp_path / "home"
    (home / "traces").mkdir(parents=True)
    chmod(home / "traces", 0o700)
    return home


def test_probe_structure_and_cli(tmp_path, monkeypatch, capsys):
    home = clean_home(tmp_path)
    posture = c.run_probe(home=home, token="x" * 20, environ={})
    d = posture.to_dict()
    assert [r["check"] for r in d["results"]] == ["manifests", "ground_truth", "egress", "kill_state",
                                                  "trace_store", "token"]
    assert set(d) == {"probed_at", "strict", "summary", "results"} and sum(d["summary"].values()) == 6
    json.dumps(d)  # traceable as-is

    monkeypatch.setenv("SENSEI_HOME", str(home))
    monkeypatch.delenv(c.TOKEN_ENV, raising=False)
    assert c.main([]) == 0  # default: a fail is reported, not fatal
    out = capsys.readouterr().out
    assert "FAIL  token" in out and "WARN  egress" in out
    assert c.main(["--strict"]) == 1
    monkeypatch.setenv(c.TOKEN_ENV, "y" * 24)
    assert c.main(["--strict"]) == 0  # warns are allowed in strict mode
    assert "y" * 24 not in capsys.readouterr().out
