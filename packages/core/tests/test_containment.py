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
    r = c.check_trace_store(traces)  # nothing there yet: skipped, not passed (Slice 34)
    assert r.status == "skipped" and r.evidence.startswith("not_created:")
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


# ---- Slice 34: every content store ------------------------------------------------------

STORES = ["trace_store", "run_store", "memory_store", "brief_store", "egress_log"]


def owner_only_home(tmp_path):
    """A synthetic home with every store present, owner only, nested where the real ones nest."""
    home = tmp_path / "home"
    files = ["traces/gateway.jsonl", "runs/abc.json", "memory/entries/e1.json", "memory/entries/e1.history.jsonl",
             "memory/changelog.jsonl", "memory/derived/embeddings.json", "briefs/2026-11-26.md",
             "briefs/2026-11-26.json", "egress.jsonl"]
    for rel in files:
        f = home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}\n")
    for p in [*home.rglob("*")]:
        chmod(p, 0o700 if p.is_dir() else 0o600)
    return home


# (store check, the path made loose, its loose mode, what the evidence names)
LOOSE = [
    ("trace_store", "traces", 0o755, "the directory 755"),
    ("trace_store", "traces/gateway.jsonl", 0o644, "gateway.jsonl 644"),
    ("run_store", "runs/abc.json", 0o644, "abc.json 644"),
    ("memory_store", "memory/entries/e1.json", 0o644, "entries/e1.json 644"),
    ("memory_store", "memory/derived/embeddings.json", 0o604, "derived/embeddings.json 604"),
    ("memory_store", "memory/derived", 0o703, "derived 703"),  # world-writable directory
    ("brief_store", "briefs", 0o755, "the directory 755"),
    ("brief_store", "briefs/2026-11-26.md", 0o644, "2026-11-26.md 644"),
    ("egress_log", "egress.jsonl", 0o644, "egress.jsonl 644"),
]


def test_an_owner_only_home_passes_every_store(tmp_path):
    home = owner_only_home(tmp_path)
    results = c.check_stores(home)
    assert [r.check for r in results] == STORES
    assert all(r.status == "pass" for r in results), [r.evidence for r in results]
    egress = results[-1]
    assert egress.evidence == f"{home / 'egress.jsonl'} (600) owner only" and egress.mode == "600"


@pytest.mark.parametrize("check,rel,mode,named", LOOSE)
def test_a_loose_store_fails_only_its_own_check(tmp_path, check, rel, mode, named):
    home = owner_only_home(tmp_path)
    chmod(home / rel, mode)
    by_check = {r.check: r for r in c.check_stores(home)}
    assert {k for k, r in by_check.items() if r.status != "pass"} == {check}
    r = by_check[check]
    assert r.status == "fail" and named in r.evidence and "by any local user" in r.evidence
    assert r.mode == oct(mode)[2:] and r.path == str(home / STORE_NAMES[check])
    store = home / STORE_NAMES[check]
    assert r.evidence.endswith(f"fix: chmod {'-R ' if store.is_dir() else ''}go-rwx {store}")


@pytest.mark.parametrize("check", STORES)
def test_a_group_readable_store_warns(tmp_path, check):
    home = owner_only_home(tmp_path)
    store = home / STORE_NAMES[check]
    target = next(p for p in store.rglob("*") if p.is_file()) if store.is_dir() else store
    chmod(target, 0o640)
    by_check = {r.check: r for r in c.check_stores(home)}
    assert {k for k, r in by_check.items() if r.status != "pass"} == {check}
    assert by_check[check].status == "warn" and "readable by its group" in by_check[check].evidence
    chmod(target, 0o620)  # group-writable, not readable: still a warn, named as writable
    r = c.check_stores(home)[STORES.index(check)]
    assert r.status == "warn" and "writable by its group" in r.evidence


@pytest.mark.parametrize("check", STORES)
def test_an_absent_store_is_skipped_not_failed(tmp_path, check):
    home = owner_only_home(tmp_path)
    store = home / STORE_NAMES[check]
    shutil.rmtree(store) if store.is_dir() else store.unlink()
    by_check = {r.check: r for r in c.check_stores(home)}
    assert by_check[check].status == "skipped" and by_check[check].evidence.startswith("not_created:")
    assert all(r.status == "pass" for k, r in by_check.items() if k != check)
    posture = c.run_probe(home=home, token="x" * 20, environ={})
    assert posture.to_dict()["summary"]["skipped"] == 1 and not posture.failed
    assert f"SKIP  {check}" in c.format_posture(posture) and "1 skipped" in c.format_posture(posture)


def test_a_store_the_process_was_not_given_is_skipped_as_not_configured(tmp_path):
    home = owner_only_home(tmp_path)
    r = c.check_stores(home, {"run_store": None})[1]
    assert r.status == "skipped" and r.evidence.startswith("not_configured:")


STORE_NAMES = {k: name for k, (name, _) in c.STORE_CHECKS.items()}


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
                                                  *STORES, "token"]
    assert set(d) == {"probed_at", "strict", "summary", "results", "stores"} and sum(d["summary"].values()) == 10
    assert set(d["summary"]) == {"pass", "warn", "fail", "skipped"}
    assert [s["store"] for s in d["stores"]] == STORES
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
