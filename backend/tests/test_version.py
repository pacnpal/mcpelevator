"""Version resolution — derives from the release tag (MCPE_VERSION), else pyproject."""

from __future__ import annotations

from app import _resolve_version, _version_from_pyproject


def test_version_prefers_env(monkeypatch):
    # The published image injects the release tag as MCPE_VERSION; it must win.
    monkeypatch.setenv("MCPE_VERSION", "9.9.9")
    assert _resolve_version() == "9.9.9"


def test_version_prefers_pyproject_over_installed_metadata(monkeypatch):
    # Running from a source tree, the adjacent pyproject.toml is authoritative and is checked
    # BEFORE installed metadata — so a stale editable install can't shadow the tree's version.
    # (No metadata mock needed: pyproject wins first, so this is inherently isolated.)
    monkeypatch.delenv("MCPE_VERSION", raising=False)
    resolved = _resolve_version()
    assert resolved == _version_from_pyproject()
    assert resolved
    assert resolved[0].isdigit()


def test_version_from_pyproject_reads_project_version():
    v = _version_from_pyproject()
    assert v is not None
    assert v.count(".") >= 2  # e.g. "1.1.0"
