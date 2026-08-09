"""Known-failure hints: log-signature matching and the last_error enrichment."""

from __future__ import annotations

from app.supervisor.hints import startup_hint

_MCP2_TRACEBACK_LINE = (
    "ImportError: cannot import name 'McpError' from 'mcp.shared.exceptions' "
    "(/root/.cache/uv/archive-v0/x/lib/python3.11/site-packages/mcp/shared/exceptions.py)"
)


def test_mcp2_import_break_recommends_the_uvx_pin():
    hint = startup_hint(["[mcpelevator] attempt 1/1: readiness", _MCP2_TRACEBACK_LINE], "uvx")
    assert hint is not None
    assert '--with "mcp<2"' in hint


def test_mcp2_import_break_on_other_runners_recommends_an_env_pin():
    hint = startup_hint([_MCP2_TRACEBACK_LINE], "command")
    assert hint is not None
    assert "mcp<2" in hint
    assert "--with" not in hint


def test_unrecognized_failures_produce_no_hint():
    lines = [
        "Traceback (most recent call last):",
        "ImportError: cannot import name 'foo' from 'mcp_server_time'",  # not the SDK
        "ValueError: something else entirely",
    ]
    assert startup_hint(lines, "uvx") is None
    assert startup_hint([], "uvx") is None


def test_signature_is_found_within_the_scanned_tail():
    lines = ["noise"] * 300 + [_MCP2_TRACEBACK_LINE] + ["noise"] * 50
    assert startup_hint(lines, "uvx") is not None
