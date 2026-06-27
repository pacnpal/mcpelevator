"""Pure catalog-mapping tests — registry documents → install drafts (no network).

Exercises the shared launch-spec core (``mapping.package_draft``) and each source's
pure document normalization (``official.to_detail`` / ``glama.to_detail``).
"""

from __future__ import annotations

from app.catalog import glama, mapping, official


def _pkg(**over):
    base = {"registryType": "npm", "identifier": "pkg", "version": "1.0.0", "transport": {"type": "stdio"}}
    base.update(over)
    return base


def _official(packages=None, *, name="io.example/srv", version="1.0.0", remotes=None, status="active"):
    """
    Build a mock official registry document.
    
    Parameters:
        packages: Package records to include under server packages.
        name: Server name to place in the document.
        version: Server version to place in the document.
        remotes: Remote entries to include under server remotes.
        status: Official registry status to store in metadata.
    
    Returns:
        dict: A registry document with server data and official metadata.
    """
    server = {"name": name, "title": "Srv", "description": "d", "version": version}
    if packages is not None:
        server["packages"] = packages
    if remotes is not None:
        server["remotes"] = remotes
    return {
        "server": server,
        "_meta": {"io.modelcontextprotocol.registry/official": {"status": status}},
    }


# --- shared launch-spec core (mapping.package_draft) -----------------------


def test_npm_maps_to_npx_with_pinned_version():
    draft = mapping.package_draft(0, _pkg(identifier="@me/pkg", version="2.1.0"))
    assert draft["installable"] is True
    assert draft["runner"] == "npx"
    assert draft["command"] == "npx"
    assert draft["args"] == ["-y", "@me/pkg@2.1.0"]


def test_pypi_maps_to_uvx_with_pep508_pin():
    draft = mapping.package_draft(0, _pkg(registryType="pypi", identifier="mcp-server-time", version="1.2.3"))
    assert draft["runner"] == "uvx"
    assert draft["command"] == "uvx"
    assert draft["args"] == ["mcp-server-time==1.2.3"]


def test_missing_or_latest_version_is_unpinned():
    npm = mapping.package_draft(0, {"registryType": "npm", "identifier": "pkg", "transport": {"type": "stdio"}})
    pypi = mapping.package_draft(1, _pkg(registryType="pypi", identifier="tool", version="latest"))
    assert npm["args"] == ["-y", "pkg"]
    assert pypi["args"] == ["tool"]


def test_package_arguments_positional_and_named():
    draft = mapping.package_draft(
        0,
        _pkg(
            packageArguments=[
                {"type": "positional", "value": "serve"},
                {"type": "named", "name": "--port", "value": "8080"},
                {"type": "named", "name": "--verbose"},
            ]
        ),
    )
    assert draft["args"] == ["-y", "pkg@1.0.0", "serve", "--port", "8080", "--verbose"]


def test_required_argument_without_value_warns():
    draft = mapping.package_draft(
        0,
        _pkg(
            registryType="pypi",
            identifier="tool",
            packageArguments=[{"type": "positional", "isRequired": True, "valueHint": "DB_PATH"}],
        ),
    )
    # A visible placeholder (not "") so the form's splitLines() can't silently drop it.
    assert draft["args"] == ["tool==1.0.0", "<DB_PATH>"]
    assert any("DB_PATH" in w for w in draft["warnings"])


def test_environment_variables_become_env_with_warnings():
    draft = mapping.package_draft(
        0,
        _pkg(
            environmentVariables=[
                {"name": "LOG_LEVEL", "default": "info"},
                {"name": "API_KEY", "isRequired": True, "isSecret": True},
            ]
        ),
    )
    assert draft["env"] == {"LOG_LEVEL": "info", "API_KEY": ""}
    assert any("API_KEY" in w and "secret" in w for w in draft["warnings"])


def test_oci_and_nuget_not_installable_with_reason():
    oci = mapping.package_draft(0, _pkg(registryType="oci", identifier="ghcr.io/x/y"))
    nuget = mapping.package_draft(1, _pkg(registryType="nuget", identifier="X.Y"))
    assert oci["installable"] is False and oci["reason"]
    assert nuget["installable"] is False and nuget["reason"]
    assert oci["registry_type"] == "oci"


def test_non_stdio_transport_not_installable():
    draft = mapping.package_draft(0, _pkg(transport={"type": "streamable-http"}))
    assert draft["installable"] is False
    assert "stdio" in draft["reason"]


def test_determinism_and_purity():
    pkg = _pkg(packageArguments=[{"type": "named", "name": "--x", "value": "1"}],
               environmentVariables=[{"name": "A", "default": "b"}])
    import copy

    original = copy.deepcopy(pkg)
    first = mapping.package_draft(0, pkg)
    # Deterministic: an equivalent fresh input yields an equal draft...
    assert first == mapping.package_draft(0, copy.deepcopy(original))
    # ...and pure: the call must not mutate its input.
    assert pkg == original


# --- official document normalization ---------------------------------------


def test_official_multiple_packages_yield_multiple_drafts():
    pkgs = [_pkg(identifier="a"), _pkg(registryType="pypi", identifier="b")]
    detail = official.to_detail(_official(pkgs))
    assert [d["runner"] for d in detail["drafts"]] == ["npx", "uvx"]


def test_official_status_from_meta():
    detail = official.to_detail(_official([_pkg()], status="deprecated"))
    assert detail["server"]["status"] == "deprecated"
    assert detail["manual_install"] is False


def test_official_remotes_surfaced_not_installed():
    detail = official.to_detail(
        _official(packages=[], remotes=[{"type": "streamable-http", "url": "https://x/mcp"}])
    )
    assert detail["drafts"] == []
    assert detail["remotes"] == [{"type": "streamable-http", "url": "https://x/mcp"}]


# --- glama (discovery-only, manual scaffold) -------------------------------


def _glama(**over):
    """
    Build a mock Glama discovery object.
    
    Parameters:
    	**over: Additional fields to merge into the default object.
    
    Returns:
    	dict: A mock Glama server record.
    """
    base = {
        "id": "abc123",
        "name": "cool-mcp",
        "description": "a tool",
        "repository": {"url": "https://github.com/x/cool-mcp"},
        "url": "https://glama.ai/mcp/servers/abc123",
        "environmentVariablesJsonSchema": {"type": "object", "properties": {}, "required": []},
    }
    base.update(over)
    return base


def test_glama_detail_is_manual_scaffold_with_env_keys():
    schema = {
        "type": "object",
        "properties": {"TOKEN": {"type": "string"}, "REGION": {"type": "string"}},
        "required": ["TOKEN"],
    }
    detail = glama.to_detail(_glama(environmentVariablesJsonSchema=schema))
    assert detail["manual_install"] is True
    draft = detail["drafts"][0]
    assert draft["installable"] is False
    assert draft["command"] == "" and draft["runner"] is None
    assert draft["env"] == {"TOKEN": "", "REGION": ""}
    assert any("TOKEN" in w for w in draft["warnings"])
    # Full URL (scheme included), not a bare host substring, so it reads as a literal
    # presence check rather than URL-host sanitization.
    assert any("https://github.com/x/cool-mcp" in n for n in detail["notes"])
