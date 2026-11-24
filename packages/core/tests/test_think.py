import json

import pytest

import sensei_core.clients as clients
from sensei_core.bench import BenchReport, load_task, report_to_dict
from sensei_core.clients import OllamaClient
from sensei_core.config import ConfigError, load_models
from sensei_core.router import FakeClient

BASE = (
    '[routes.x]\nprovider = "ollama"\nmodel = "m"\ntier = "local"\n'
    'sensitivity = ["LOCAL_ONLY", "API_OK"]\nmax_sensitivity = "LOCAL_ONLY"\n'
    "est_cost_per_1k_tokens = 0.0\n"
)


def models_file(tmp_path, extra=""):
    p = tmp_path / "models.toml"
    p.write_text(BASE + extra, encoding="utf-8")
    return p


@pytest.mark.parametrize("line, expected", [("think = true\n", True), ("think = false\n", False), ("", None)])
def test_loader_accepts_think_true_false_or_absent(tmp_path, line, expected):
    assert load_models(models_file(tmp_path, line))["x"].think is expected


@pytest.mark.parametrize("line", ['think = "no"\n', "think = 0\n", "think = 1\n"])
def test_loader_rejects_non_bool_think(tmp_path, line):
    with pytest.raises(ConfigError, match="think must be true or false"):
        load_models(models_file(tmp_path, line))


@pytest.fixture
def captured(monkeypatch):
    sent = []

    def fake_post(url, body, timeout):
        sent.append(body)
        return {"response": "ok"}

    monkeypatch.setattr(clients, "_post_json", fake_post)
    return sent


def test_request_body_omits_think_when_unset(captured):
    assert OllamaClient("qwen3:8b").complete("hi") == "ok"
    assert "think" not in captured[0]


@pytest.mark.parametrize("value", [True, False])
def test_request_body_includes_think_when_configured(captured, value):
    OllamaClient("qwen3:8b", think=value).complete("hi")
    assert captured[0]["think"] is value
    assert captured[0]["model"] == "qwen3:8b" and captured[0]["stream"] is False


def test_for_route_takes_think_from_config(tmp_path, captured):
    route = load_models(models_file(tmp_path, "think = false\n"))["x"]
    OllamaClient.for_route(route).complete("hi")
    assert captured[0]["think"] is False
    route = load_models(models_file(tmp_path))["x"]
    OllamaClient.for_route(route).complete("hi")
    assert "think" not in captured[1]


def test_fake_client_ignores_think():
    assert FakeClient("canned").complete("hi") == "canned"


def test_shipped_config_think_is_valid():
    assert load_models()["local_private"].think in (True, False, None)


def test_settings_are_recorded_and_printed():
    from pathlib import Path

    task = load_task(Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001")
    report = BenchReport(task, [], "2026-11-24T00:00:00+00:00", "2026-11-24T00:00:01+00:00",
                         {"k": 0}, workflow="w", settings={"local_private.think": False})
    assert report_to_dict(report)["settings"] == {"local_private.think": False}
    assert json.dumps(report_to_dict(report))  # serialisable
