import pytest

from sensei_core.config import ConfigError, load_models


def test_default_models_toml_loads():
    routes = load_models()
    assert set(routes) == {"local_private", "cheap_api", "frontier_api"}
    local = routes["local_private"]
    assert local.provider == "ollama"
    assert local.tier == "local"
    assert "LOCAL_ONLY" in local.sensitivity
    for name in ("cheap_api", "frontier_api"):
        assert "LOCAL_ONLY" not in routes[name].sensitivity


def test_unknown_tier_is_rejected(tmp_path):
    bad = tmp_path / "models.toml"
    bad.write_text(
        '[routes.x]\nprovider = "p"\nmodel = "m"\ntier = "premium"\nsensitivity = ["API_OK"]\nmax_sensitivity = "API_OK"\nest_cost_per_1k_tokens = 0.0\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown tier"):
        load_models(bad)


def test_api_tier_may_not_accept_local_only(tmp_path):
    bad = tmp_path / "models.toml"
    bad.write_text(
        '[routes.x]\nprovider = "p"\nmodel = "m"\ntier = "cheap_api"\nsensitivity = ["LOCAL_ONLY"]\nmax_sensitivity = "LOCAL_ONLY"\nest_cost_per_1k_tokens = 0.0\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="LOCAL_ONLY"):
        load_models(bad)


def _route(tmp_path, max_sensitivity, cost):
    p = tmp_path / "models.toml"
    p.write_text(
        '[routes.x]\nprovider = "p"\nmodel = "m"\ntier = "local"\n'
        'sensitivity = ["LOCAL_ONLY", "API_OK"]\n'
        f'max_sensitivity = "{max_sensitivity}"\nest_cost_per_1k_tokens = {cost}\n',
        encoding="utf-8",
    )
    return p


def test_default_routes_have_cost_and_max_sensitivity():
    routes = load_models()
    assert routes["local_private"].max_sensitivity == "LOCAL_ONLY"
    assert routes["local_private"].est_cost_per_1k_tokens == 0.0
    for name in ("cheap_api", "frontier_api"):
        assert routes[name].max_sensitivity == "API_OK"
        assert routes[name].est_cost_per_1k_tokens > 0


def test_class_above_max_sensitivity_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="above its max_sensitivity"):
        load_models(_route(tmp_path, "API_OK", 0.0))


def test_negative_cost_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="non-negative"):
        load_models(_route(tmp_path, "LOCAL_ONLY", -1.0))
