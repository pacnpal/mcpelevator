"""base_url must advertise a *reachable* host, even when bound to a wildcard."""

from __future__ import annotations

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
