"""Version resolution — derives from the release tag (MCPE_VERSION), else pyproject."""

from __future__ import annotations

from app import _resolve_version, _version_from_pyproject


def test_version_prefers_env(monkeypatch):
    # The published image injects the release tag as MCPE_VERSION; it must win.
    monkeypatch.setenv("MCPE_VERSION", "9.9.9")
    assert _resolve_version() == "9.9.9"


def test_version_falls_back_to_pyproject(monkeypatch):
    # A source checkout (no MCPE_VERSION, virtual package) resolves from pyproject.toml,
    # never the "unknown" placeholder. Force the metadata lookup to miss so the fallback is
    # always exercised — otherwise an ambient editable install of mcpelevator (with a
    # possibly-different version) would satisfy step 2 and this test wouldn't test the fallback.
    import importlib.metadata

    monkeypatch.delenv("MCPE_VERSION", raising=False)

    def _miss(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _miss)

    resolved = _resolve_version()
    assert resolved == _version_from_pyproject()
    assert resolved
    assert resolved[0].isdigit()


def test_version_from_pyproject_reads_project_version():
    v = _version_from_pyproject()
    assert v is not None
    assert v.count(".") >= 2  # e.g. "1.1.0"
