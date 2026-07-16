"""base_url must advertise a *reachable* host, even when bound to a wildcard."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_base_url_rewrites_wildcard_bind_to_loopback():
    # 0.0.0.0 / :: are bind addresses; a client using them would send Host: 0.0.0.0
    # and fail the allowlist. The advertised URL must use loopback instead.
    assert Settings(host="0.0.0.0", port=8080, public_base_url=None).base_url == "http://127.0.0.1:8080"
    assert Settings(host="::", port=8080, public_base_url=None).base_url == "http://127.0.0.1:8080"


def test_base_url_keeps_real_host_and_prefers_public_url():
    assert Settings(host="127.0.0.1", port=8080, public_base_url=None).base_url == "http://127.0.0.1:8080"
    assert Settings(public_base_url="https://mcp.example.com/").base_url == "https://mcp.example.com"


def test_base_url_brackets_ipv6_literal_host():
    assert Settings(host="::1", port=8080, public_base_url=None).base_url == "http://[::1]:8080"
    assert Settings(host="fe80::1", port=8080, public_base_url=None).base_url == "http://[fe80::1]:8080"


def test_public_host_extracts_configured_url():
    assert Settings(public_base_url="https://mcp.example.com:8443/").public_host == "mcp.example.com"
    assert Settings(public_base_url=None).public_host is None


def test_extra_allowed_hosts_parses_normalizes_and_dedupes():
    s = Settings(allowed_hosts="mcp.example.com, https://Other.test:8443 , mcp.example.com, ")
    # comma-split, trimmed, reduced to bare lowercased hostnames, empties dropped, deduped
    assert s.extra_allowed_hosts == ["mcp.example.com", "other.test"]
    assert Settings(allowed_hosts="").extra_allowed_hosts == []


def test_env_values_tolerate_surrounding_whitespace(monkeypatch):
    # A stray trailing space (compose/.env line) or newline (mounted secret file)
    # must not blow up bootstrap — pydantic's strict bool/path parsers would
    # otherwise reject "true " before the app can start.
    monkeypatch.setenv("MCPE_ALLOW_PRIVATE_LAN", "true ")
    monkeypatch.setenv("MCPE_ADMIN_TOKEN", " secret\n")
    monkeypatch.setenv("MCPE_HOST", " 0.0.0.0 ")
    settings = Settings(_env_file=None)
    assert settings.allow_private_lan is True
    assert settings.admin_token == "secret"
    assert settings.host == "0.0.0.0"


def test_activation_settings_defaults_and_environment(monkeypatch):
    defaults = Settings(_env_file=None)
    assert defaults.start_timeout_s == 120
    assert defaults.restart_budget == 5
    assert defaults.restart_stable_s == 60

    monkeypatch.setenv("MCPE_START_TIMEOUT_S", "3.5")
    monkeypatch.setenv("MCPE_RESTART_BUDGET", "2")
    monkeypatch.setenv("MCPE_RESTART_STABLE_S", "9")
    configured = Settings(_env_file=None)
    assert configured.start_timeout_s == 3.5
    assert configured.restart_budget == 2
    assert configured.restart_stable_s == 9


@pytest.mark.parametrize(
    ("field", "value"),
    [("start_timeout_s", 0), ("restart_budget", 0), ("restart_stable_s", -1)],
)
def test_activation_settings_reject_invalid_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
