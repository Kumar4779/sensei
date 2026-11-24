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
        '[routes.x]\nprovider = "p"\nmodel = "m"\ntier = "premium"\nsensitivity = ["API_OK"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown tier"):
        load_models(bad)


def test_api_tier_may_not_accept_local_only(tmp_path):
    bad = tmp_path / "models.toml"
    bad.write_text(
        '[routes.x]\nprovider = "p"\nmodel = "m"\ntier = "cheap_api"\nsensitivity = ["LOCAL_ONLY"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="LOCAL_ONLY"):
        load_models(bad)
