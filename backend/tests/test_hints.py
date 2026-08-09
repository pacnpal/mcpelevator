"""Known-failure hints: log-signature matching and the last_error enrichment."""

from __future__ import annotations

from app.supervisor.hints import startup_hint

_MCP2_TRACEBACK_LINE = (
    "ImportError: cannot import name 'McpError' from 'mcp.shared.exceptions' "
    "(/root/.cache/uv/archive-v0/x/lib/python3.11/site-packages/mcp/shared/exceptions.py)"
)


def test_mcp2_import_break_on_uvx_recommends_the_pin_toggle():
    hint = startup_hint(["[mcpelevator] attempt 1/1: readiness", _MCP2_TRACEBACK_LINE], "uvx")
    assert hint is not None
    assert "Pin mcp SDK < 2" in hint


def test_mcp2_import_break_on_docker_recommends_an_image_remedy():
    # Only the image controls the container's packages — an env/argv pin can't.
    hint = startup_hint([_MCP2_TRACEBACK_LINE], "docker")
    assert hint is not None
    assert "image" in hint
    assert "toggle" not in hint


def test_mcp2_import_break_on_other_runners_recommends_an_env_pin():
    hint = startup_hint([_MCP2_TRACEBACK_LINE], "command")
    assert hint is not None
    assert "mcp<2" in hint
    assert "toggle" not in hint


def test_mcp2_import_break_during_setup_targets_the_setup_script():
    # The setup script runs in its own shell: a uvx launch-argv pin can't reach
    # it, so even a uvx server must get the setup-side remedy for this phase.
    hint = startup_hint([_MCP2_TRACEBACK_LINE], "uvx", setup_failed=True)
    assert hint is not None
    assert "setup script" in hint
    assert "toggle" not in hint


def test_unrecognized_failures_produce_no_hint():
    lines = [
        "Traceback (most recent call last):",
        # An SDK ImportError that is NOT a known 2.0 removal: could be a server
        # importing a too-new symbol from an old SDK — a downgrade can't fix it.
        "ImportError: cannot import name 'BrandNewThing' from 'mcp.shared.exceptions'",
        "ImportError: cannot import name 'foo' from 'mcp_server_time'",
        "ValueError: something else entirely",
    ]
    assert startup_hint(lines, "uvx") is None
    assert startup_hint([], "uvx") is None


def test_signature_is_found_within_the_scanned_tail():
    lines = ["noise"] * 300 + [_MCP2_TRACEBACK_LINE] + ["noise"] * 50
    assert startup_hint(lines, "uvx") is not None
