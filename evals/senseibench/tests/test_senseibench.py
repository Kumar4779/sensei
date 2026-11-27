"""SenseiBench v1 definition tests (#28): manifest integrity, schema validity, ground-truth isolation, fake-backend smoke.

No live model calls: the conftest refuses every socket, and the kimi path is exercised with an injected fake client.
"""

import json
import re
from pathlib import Path

import pytest

import senseibench as sb

FROZEN_MANIFEST_SHA256 = "sha256:240f162649fb1f596dd285a6d91aa59d411ac2380e61d660aa33ccffe0311a9d"


# 1. Manifest integrity: the union is frozen, and every source it was built from is pinned

def test_the_manifest_itself_is_pinned():
    assert sb.manifest_sha256() == FROZEN_MANIFEST_SHA256  # a changed manifest is a new bench version, not an edit


def test_every_pinned_source_hash_still_matches():
    assert sb.verify_manifest() == []


def test_a_changed_source_is_caught(monkeypatch):
    m = sb.load_manifest()
    m["families"]["voice"]["sha256"] = "sha256:" + "0" * 64
    m["families"]["core"]["tasks"][0]["sha256"] = "sha256:" + "1" * 64
    drifts = sb.verify_manifest(m)
    assert any("voice.sha256" in d for d in drifts) and any("core.tasks[0].sha256" in d for d in drifts)


def test_freeze_refuses_to_overwrite_v1(capsys):
    assert sb.main(["freeze"]) == 2 and "frozen" in capsys.readouterr().err


def test_the_manifest_records_provenance_for_every_family():
    f = sb.load_manifest()["families"]
    assert set(f) == {"core", "voice", "graphrag", "macpilot"} and all(f[x]["provenance"] for x in f)
    assert [f"{t['id']}@v{t['version']}" for t in f["core"]["tasks"]] == [
        "morning-brief-001@v1", "morning-brief-002-adversarial@v3", "doc-summary-001@v1"]
    assert {e["reason"] for e in f["core"]["excluded"]} == {"superseded by v3"}
    assert f["voice"]["eval_version"] == "voice-bench-v2" and len(f["voice"]["tasks"]) == 10
    assert f["graphrag"]["corpus_version"] == "v1" and f["graphrag"]["tasks"] == {"probes": 100, "canaries": 10}
    assert len(f["macpilot"]["tasks"]) == 20 and f["macpilot"]["scored"] == 0
    assert {t["status"] for t in f["macpilot"]["tasks"]} == {"defined-only"}


# 2. Ground truth stays isolated from the agent under test

AGENT_CODE = (
    [p for p in (sb.ROOT / "packages/core/sensei_core").glob("*.py") if p.name not in ("bench.py", "containment.py")]
    + list((sb.ROOT / "packages/voice/voice").glob("*.py"))
    + [sb.ROOT / "packages/graphrag/graphrag" / n for n in
       ("answer.py", "retrieval.py", "acl.py", "chunking.py", "embedders.py", "text.py")]
)
EVAL_PATHS = re.compile(r"evals/|ground_truth\.json|expected\.json|gold-summary|tasks\.json|senseibench|checker\.py")


@pytest.mark.parametrize("path", AGENT_CODE, ids=lambda p: p.relative_to(sb.ROOT).as_posix())
def test_agent_code_never_names_an_eval_or_ground_truth_path(path):
    """bench.py and containment.py are core's harness side (the runner and the sandbox probe); they're excluded."""
    code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#"))
    assert not EVAL_PATHS.search(code), path


def test_ground_truth_lives_outside_what_the_agent_reads():
    corpus = (sb.ROOT / sb.GRAPHRAG_CORPUS).resolve()
    assert not (sb.ROOT / sb.GRAPHRAG_GROUND_TRUTH).resolve().is_relative_to(corpus)
    for t in sb.load_manifest()["families"]["core"]["tasks"]:
        fixture = sb.ROOT / t["dir"] / "fixture"  # the only directory core copies into a trial's sandbox
        assert not list(fixture.rglob("checker.py")) and not list(fixture.rglob("expected.json"))
        assert not list(fixture.rglob("gold-summary.json"))


# 3. Schema validity and the fake-backend smoke

@pytest.fixture(scope="module")
def fake_run(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    return sb.run(list(sb.FAMILIES), "fake", 1, max_usd=0.50, home=home)


def test_a_fake_run_of_every_family_is_valid_and_complete(fake_run):
    assert sb.validate_results(fake_run) == []
    f = fake_run["families"]
    assert [t["status"] for t in f["core"]["tasks"]] == ["scored"] * 3
    assert f["voice"]["summary"]["tasks_scored"] == 10 and f["voice"]["summary"]["hard_gate"] == "PASS"
    assert f["graphrag"]["summary"]["tasks_scored"] == 110 and f["graphrag"]["summary"]["hard_gate"] == "PASS"
    assert f["macpilot"]["summary"]["tasks_scored"] == 0 and f["macpilot"]["summary"]["tasks_not_scored"] == {"defined-only": 20}
    assert fake_run["spend"]["est_usd"] == 0.0  # a fake backend spends nothing


def test_the_results_header_lists_every_family_version(fake_run):
    h = fake_run["families_included"]
    assert set(h) == {"core", "voice", "graphrag", "macpilot"}
    assert h["voice"]["eval_version"] == "voice-bench-v2" and h["graphrag"]["corpus_version"] == "v1"
    assert h["core"]["tasks"] == ["morning-brief-001@v1", "morning-brief-002-adversarial@v3", "doc-summary-001@v1"]
    assert fake_run["manifest_sha256"] == FROZEN_MANIFEST_SHA256


def test_family_signals_are_reported(fake_run):
    voice = fake_run["families"]["voice"]["tasks"][0]
    assert voice["signals"]["gate_decisions"] and voice["signals"]["hard_gate"] == "PASS"
    canary = next(t for t in fake_run["families"]["graphrag"]["tasks"] if t["kind"] == "leak-gate")
    assert canary["trials_detail"][0]["attacks_checked"] == 16 and canary["signals"]["hard_gate"] == "PASS"
    core = fake_run["families"]["core"]["tasks"][0]
    assert "verifier" in core["signals"] and core["trials_detail"][0]["route"] == "local_private"


def test_schema_problems_are_reported(fake_run):
    broken = json.loads(json.dumps(fake_run))
    del broken["families_included"]["voice"]
    broken["families"]["voice"]["tasks"][0]["status"] = "maybe"
    broken["families"]["core"]["tasks"][0]["trials_detail"] = []
    problems = sb.validate_results(broken)
    assert any("header" in p for p in problems) and any("bad status" in p for p in problems)
    assert any("trials != k" in p for p in problems)


def test_results_are_written_once_per_run_never_overwritten(fake_run, tmp_path):
    a, b = sb.write_results(fake_run, tmp_path), sb.write_results(fake_run, tmp_path)
    assert a != b and a.name.startswith("senseibench-v1-all-fake-k1-") and b.name.endswith("-2.json")


# 4. The kimi backend, offline: LOCAL_ONLY never routed to it; the spend cap holds

class FakeKimi:
    model = "kimi-k3"
    reasoning_effort = "low"
    destination_host = "api.moonshot.ai"

    def __init__(self):
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return "not a structured summary"


def test_kimi_backend_skips_local_only_tasks_and_routes_api_ok_to_cheap_api(tmp_path):
    kimi = FakeKimi()
    r = sb.run(["core"], "kimi", 1, max_usd=0.50, home=tmp_path, client_factory=lambda route: kimi)
    tasks = {t["id"]: t for t in r["families"]["core"]["tasks"]}
    assert tasks["morning-brief-001"]["status"] == "not_applicable" and "LOCAL_ONLY" in tasks["morning-brief-001"]["reason"]
    assert tasks["morning-brief-002-adversarial"]["status"] == "not_applicable"
    doc = tasks["doc-summary-001"]
    assert doc["status"] == "scored" and doc["trials_detail"][0]["route"] == "cheap_api" and kimi.prompts
    assert sb.validate_results(r) == []


def test_the_spend_cap_stops_a_task_before_it_runs(tmp_path):
    kimi = FakeKimi()
    r = sb.run(["core"], "kimi", 3, max_usd=0.01, home=tmp_path, client_factory=lambda route: kimi)
    doc = next(t for t in r["families"]["core"]["tasks"] if t["id"] == "doc-summary-001")
    assert doc["status"] == "not_run" and "spend cap" in doc["reason"] and kimi.prompts == []


def test_unknown_backend_and_bad_k_are_refused(tmp_path):
    with pytest.raises(ValueError):
        sb.run(["voice"], "gpt", 3, max_usd=1, home=tmp_path)
    with pytest.raises(ValueError):
        sb.run(["voice"], "fake", 0, max_usd=1, home=tmp_path)
