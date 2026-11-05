"""Known-failure hints: log-signature matching and the last_error enrichment."""

from __future__ import annotations

from app.supervisor.hints import startup_hint

_MCP2_TRACEBACK_LINE = (
    "ImportError: cannot import name 'McpError' from 'mcp.shared.exceptions' "
    "(/root/.cache/uv/archive-v0/x/lib/python3.11/site-packages/mcp/shared/exceptions.py)"
)


def test_mcp2_import_break_on_uvx_recommends_the_pin_toggle():
    hint = startup_hint(
        ["[mcpelevator] attempt 1/1: readiness", _MCP2_TRACEBACK_LINE],
        "uvx",
        command="uvx",
        args=["mcp-server-time"],
    )
    assert hint is not None
    assert "Pin mcp SDK < 2" in hint


def test_mcp2_import_break_on_an_unpinnable_uvx_shape_recommends_the_manual_pin():
    # The service refuses the toggle for shapes with no certain pin placement —
    # the hint must not direct the operator to an action whose save is rejected.
    hint = startup_hint(
        [_MCP2_TRACEBACK_LINE],
        "uvx",
        command="uv",
        args=["--directory", "/srv", "tool", "run", "pkg"],
    )
    assert hint is not None
    assert "Pin mcp SDK < 2" not in hint
    assert '--with "mcp<2"' in hint


def test_mcp2_import_break_with_the_pin_already_on_does_not_recommend_the_toggle():
    # The toggle is on and the import STILL failed — pointing at the toggle
    # again is a dead end; the hint must acknowledge the pin and redirect.
    hint = startup_hint(
        [_MCP2_TRACEBACK_LINE],
        "uvx",
        command="uvx",
        args=["--with", "mcp<2", "mcp-server-time"],
        pinned=True,
    )
    assert hint is not None
    assert "Enable the server's" not in hint
    assert "already on" in hint


def test_mcp2_import_break_on_a_non_uv_executable_recommends_an_env_pin():
    # An arbitrary executable on a uvx-classified row takes neither the toggle
    # nor any --with argv advice — only its own Python environment can pin.
    hint = startup_hint(
        [_MCP2_TRACEBACK_LINE], "uvx", command="python", args=["server.py"]
    )
    assert hint is not None
    assert "Pin mcp SDK < 2" not in hint
    assert "--with" not in hint
    assert "environment" in hint


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
    hint = startup_hint(
        ["[mcpelevator] attempt 1/1: setup", _MCP2_TRACEBACK_LINE],
        "uvx",
        setup_failed=True,
    )
    assert hint is not None
    assert "setup script" in hint
    assert "toggle" not in hint


def test_signature_only_counts_toward_the_phase_that_emitted_it():
    # A setup script that PRINTS the traceback but succeeds, followed by an
    # unrelated launch failure: the launch-side scan must not credit the
    # setup-emitted line (the toggle can't change the setup shell — and the
    # terminal failure isn't this signature at all).
    lines = [
        "[mcpelevator] attempt 1/1: setup",
        _MCP2_TRACEBACK_LINE,
        "[mcpelevator] attempt 1/1: bridge",
        "[mcpelevator] attempt 1/1: readiness",
        "something unrelated crashed",
    ]
    assert startup_hint(lines, "uvx") is None
    # And the mirror: a bridge-emitted signature is not setup evidence.
    lines = [
        "[mcpelevator] attempt 1/1: setup",
        "setup output",
        "[mcpelevator] attempt 1/1: bridge",
        _MCP2_TRACEBACK_LINE,
    ]
    assert startup_hint(lines, "uvx", setup_failed=True) is None
    # The same bridge-emitted signature IS launch evidence.
    assert startup_hint(lines, "uvx") is not None


def test_signature_from_an_earlier_attempt_is_not_final_evidence():
    # Attempt 1 died on the import break, but the FINAL attempt failed for some
    # other reason — the hint must describe what actually killed the activation.
    lines = [
        "[mcpelevator] attempt 1/2: bridge",
        _MCP2_TRACEBACK_LINE,
        "[mcpelevator] attempt 2/2: bridge",
        "[mcpelevator] attempt 2/2: readiness",
        "something unrelated crashed",
    ]
    assert startup_hint(lines, "uvx") is None
    # When the final attempt DOES carry the signature, the hint fires.
    lines = [
        "[mcpelevator] attempt 1/2: bridge",
        "transient network error",
        "[mcpelevator] attempt 2/2: bridge",
        _MCP2_TRACEBACK_LINE,
    ]
    assert startup_hint(lines, "uvx") is not None


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


def test_marker_shaped_child_output_with_huge_attempt_number_is_ignored():
    # Child output is untrusted: a marker-SHAPED line with thousands of digits
    # must fall through as ordinary output (never reach int(), which CPython
    # caps for str conversion) — and must not disturb real-marker sectioning.
    forged = f"[mcpelevator] attempt {'9' * 5000}/1: bridge"
    lines = [
        "[mcpelevator] attempt 1/1: bridge",
        forged,
        _MCP2_TRACEBACK_LINE,
    ]
    assert startup_hint(lines, "uvx") is not None


# --- Playwright's missing browser (issue #225) -------------------------------- #

_PLAYWRIGHT_LINE = (
    "[health] Error: browserType.launchPersistentContext: Executable doesn't exist at "
    "/root/.cache/ms-playwright/chromium-1179/chrome-linux/chrome"
)


def test_playwright_missing_browser_in_the_health_phase_recommends_the_setup_install():
    lines = [
        "[mcpelevator] attempt 1/1: readiness",
        "[mcpelevator] attempt 1/1: health",
        _PLAYWRIGHT_LINE,
    ]
    hint = startup_hint(lines, "npx", command="npx", args=["-y", "@playwright/mcp"])
    assert hint is not None
    assert "npx playwright install chromium" in hint
    assert "setup script" in hint
    assert "MCPE_APT_PACKAGES" in hint and "--with-deps" in hint


def test_playwright_install_banner_line_is_evidence_too():
    banner = "[health] ║     npx playwright install                                   ║"
    lines = ["[mcpelevator] attempt 1/1: health", banner]
    assert "npx playwright install chromium" in (startup_hint(lines, "command") or "")
    # The command merely mentioned in prose is not.
    prose = ["[mcpelevator] attempt 1/1: health", "tip: run npx playwright install first"]
    assert startup_hint(prose, "command") is None


def test_playwright_text_in_a_setup_section_that_succeeded_gets_no_hint():
    # The setup script printed the message but exited 0; the start then failed in the
    # health phase for another reason. A browser remedy would send the operator astray.
    lines = [
        "[mcpelevator] attempt 1/1: setup",
        _PLAYWRIGHT_LINE.removeprefix("[health] "),
        "[mcpelevator] attempt 1/1: bridge",
        "[mcpelevator] attempt 1/1: readiness",
        "[mcpelevator] attempt 1/1: health",
        "[health] connection refused",
    ]
    assert startup_hint(lines, "npx") is None


def test_playwright_hint_adapts_to_the_runner():
    lines = ["[mcpelevator] attempt 1/1: health", _PLAYWRIGHT_LINE]
    docker = startup_hint(lines, "docker") or ""
    assert "image" in docker and "setup script" not in docker
    remote = startup_hint(lines, "remote") or ""
    assert "upstream" in remote and "setup script" not in remote
    assert "uvx playwright install chromium" in (startup_hint(lines, "uvx") or "")
