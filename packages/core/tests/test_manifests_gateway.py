"""Slice 24: the Gateway refuses to start on tampered manifests, and /run returns a distinct 500."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from test_manifests import TASK_DIR, build, manifests, tamper, trace  # noqa: E402,F401

# ---- Gateway: refuses to start, and /run returns a distinct 500 -----------------------------

from fastapi.testclient import TestClient  # noqa: E402

from sensei_core.gateway import TOKEN_ENV, GatewayConfigError, create_app  # noqa: E402
from sensei_core.memory import MemoryStore  # noqa: E402

AUTH = {"X-Sensei-Token": "t"}


def gateway(manifests, trace, tmp_path, monkeypatch, **kw):
    monkeypatch.setenv(TOKEN_ENV, "t")
    ex, clients = build(manifests, trace)
    return ex, clients, lambda: TestClient(create_app(ex, trace.path, MemoryStore(tmp_path / "m"), trace, **kw))


def test_gateway_run_refuses_with_a_distinct_500(manifests, trace, tmp_path, monkeypatch):
    _, clients, make = gateway(manifests, trace, tmp_path, monkeypatch)
    client = make()
    run = {"prompt": "hi", "sensitivity_class": "LOCAL_ONLY", "difficulty": "simple"}
    assert client.post("/run", json=run, headers=AUTH).status_code == 200
    tamper(manifests["tools.toml"])
    r = client.post("/run", json=run, headers=AUTH)
    assert r.status_code == 500
    assert r.json() == {"error": "manifest_tamper_detected", "changed": ["tools.toml"]}
    assert len(clients["local_private"].prompts) == 1  # the refused run never reached the model


def test_gateway_brief_run_refuses_too(manifests, trace, tmp_path, monkeypatch):
    fixture = TASK_DIR / "fixture"
    _, _, make = gateway(manifests, trace, tmp_path, monkeypatch, brief_fixture=fixture, brief_dir=tmp_path / "b")
    client = make()
    tamper(manifests["models.toml"])
    r = client.post("/brief/run", headers=AUTH)
    assert r.status_code == 500 and r.json()["error"] == "manifest_tamper_detected"


def test_gateway_refuses_to_start_on_tampered_manifests(manifests, trace, tmp_path, monkeypatch):
    _, _, make = gateway(manifests, trace, tmp_path, monkeypatch)
    tamper(manifests["models.toml"])
    with pytest.raises(GatewayConfigError, match="manifest_tamper_detected"):
        make()
