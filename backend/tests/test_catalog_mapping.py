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


def test_boolean_flag_kept_but_optional_value_option_omitted():
    draft = mapping.package_draft(
        0,
        _pkg(
            packageArguments=[
                {"type": "named", "name": "--verbose"},  # bare flag → kept
                {"type": "named", "name": "--categories", "valueHint": "CATEGORY"},  # value-option, unset → omitted
            ]
        ),
    )
    assert draft["args"] == ["-y", "pkg@1.0.0", "--verbose"]


def test_optional_secret_named_arg_omitted():
    # A secret option like --password takes a value; unset + optional → omit it rather
    # than emit a bare "--password" that breaks CLI parsing.
    draft = mapping.package_draft(
        0, _pkg(packageArguments=[{"type": "named", "name": "--password", "isSecret": True}])
    )
    assert draft["args"] == ["-y", "pkg@1.0.0"]


def test_runtime_arguments_scaffold_but_not_auto_installable():
    draft = mapping.package_draft(
        0,
        _pkg(
            runtimeArguments=[{"type": "named", "name": "--package", "value": "@scope/real"}],
            packageArguments=[{"type": "positional", "value": "serve"}],
        ),
    )
    # The scaffold folds runtime args before the package, but stays manual since runtime
    # args may already supply the package (avoids duplicating it).
    assert draft["args"] == ["-y", "--package", "@scope/real", "pkg@1.0.0", "serve"]
    assert draft["installable"] is False
    assert "runtime arguments" in (draft["reason"] or "")


def test_unsupported_runtime_hint_not_installable():
    draft = mapping.package_draft(0, _pkg(runtimeHint="node"))
    assert draft["installable"] is False
    assert "node" in (draft["reason"] or "")


def test_supported_runtime_hint_is_installable():
    assert mapping.package_draft(0, _pkg(runtimeHint="npx"))["installable"] is True
    assert mapping.package_draft(0, _pkg(registryType="pypi", identifier="t", runtimeHint="uv"))["installable"] is True


def test_templated_env_value_warns():
    draft = mapping.package_draft(0, _pkg(environmentVariables=[{"name": "TOKEN", "value": "{token}"}]))
    assert draft["env"] == {"TOKEN": "{token}"}
    assert any("TOKEN" in w and "placeholder" in w for w in draft["warnings"])


def test_optional_secret_without_value_is_omitted():
    # isSecret alone doesn't make a var required; an optional secret left unset is omitted
    # rather than scaffolded as NAME="".
    draft = mapping.package_draft(
        0, _pkg(environmentVariables=[{"name": "OPTIONAL_TOKEN", "isSecret": True}])
    )
    assert draft["env"] == {}


def test_required_secret_without_value_is_prefilled_and_warned():
    draft = mapping.package_draft(
        0, _pkg(environmentVariables=[{"name": "API_KEY", "isRequired": True, "isSecret": True}])
    )
    assert draft["env"] == {"API_KEY": ""}
    assert any("API_KEY" in w and "secret" in w for w in draft["warnings"])


def test_optional_env_var_without_value_is_omitted():
    draft = mapping.package_draft(
        0,
        _pkg(
            environmentVariables=[
                {"name": "OPTIONAL_TUNING"},  # no value/default, not required → omit
                {"name": "LOG_LEVEL", "default": "info"},  # has default → keep
            ]
        ),
    )
    assert draft["env"] == {"LOG_LEVEL": "info"}


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


def test_required_named_argument_without_value_warns():
    draft = mapping.package_draft(
        0,
        _pkg(packageArguments=[{"type": "named", "name": "--config", "isRequired": True, "valueHint": "PATH"}]),
    )
    assert draft["args"] == ["-y", "pkg@1.0.0", "--config", "<PATH>"]
    assert any("--config" in w and "PATH" in w for w in draft["warnings"])


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


def test_official_dedupe_keeps_latest_version():
    def entry(name, version, is_latest):
        e = _official([_pkg()], name=name, version=version)
        e["_meta"]["io.modelcontextprotocol.registry/official"]["isLatest"] = is_latest
        return e

    entries = [
        entry("io.x/srv", "1.0.0", False),
        entry("io.x/srv", "1.0.1", True),
        entry("io.y/other", "2.0.0", True),
    ]
    deduped = official.dedupe_latest(entries)
    # One row per server name, and the kept io.x/srv row is the isLatest version.
    items = [official._list_item(e) for e in deduped]
    by_name = {i["name"]: i for i in items}
    assert set(by_name) == {"io.x/srv", "io.y/other"}
    assert by_name["io.x/srv"]["version"] == "1.0.1"


def test_official_dedupe_falls_back_to_first_seen_without_islatest():
    # No isLatest flags at all → keep the first occurrence per name (no duplicates).
    entries = [_official([_pkg()], name="io.x/srv", version="1.0.0"),
               _official([_pkg()], name="io.x/srv", version="1.0.1")]
    deduped = official.dedupe_latest(entries)
    assert len(deduped) == 1
    assert official._list_item(deduped[0])["version"] == "1.0.0"  # first-seen is kept


def test_official_dedupe_never_drops_server_when_all_non_latest():
    # Upstream quirk: every row flagged isLatest=False. The server must still appear.
    def row(version):
        e = _official([_pkg()], name="io.x/srv", version=version)
        e["_meta"]["io.modelcontextprotocol.registry/official"]["isLatest"] = False
        return e

    deduped = official.dedupe_latest([row("1.0.0"), row("1.0.1")])
    assert len(deduped) == 1


def test_official_deleted_status_blocks_install():
    entry = _official([_pkg()], status="deleted")
    assert official._list_item(entry)["installable"] is False
    detail = official.to_detail(entry)
    assert all(d["installable"] is False for d in detail["drafts"])
    # The runnable command is stripped so the review form can't launch a removed package.
    assert all(d["command"] == "" and d["runner"] is None and d["args"] == [] for d in detail["drafts"])
    assert any("deleted" in n for n in detail["notes"])


def test_official_remotes_surfaced_not_installed():
    detail = official.to_detail(
        _official(packages=[], remotes=[{"type": "streamable-http", "url": "https://x/mcp"}])
    )
    assert detail["drafts"] == []
    assert detail["remotes"] == [
        {"type": "streamable-http", "url": "https://x/mcp", "headers": {}, "warnings": []}
    ]


def test_official_remote_headers_scaffolded_with_warnings():
    detail = official.to_detail(
        _official(
            packages=[],
            remotes=[
                {
                    "type": "streamable-http",
                    "url": "https://{tenant}.x/mcp",
                    "headers": [
                        {"name": "Authorization", "isRequired": True, "isSecret": True},
                        {"name": "X-Default", "default": "v"},
                    ],
                }
            ],
        )
    )
    remote = detail["remotes"][0]
    # Required header is scaffolded empty + warned; a defaulted header is prefilled.
    assert remote["headers"] == {"Authorization": "", "X-Default": "v"}
    assert any("Authorization" in w for w in remote["warnings"])
    # The templated URL is flagged too.
    assert any("{…} placeholder" in w for w in remote["warnings"])


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
    # Only the required key is scaffolded; optional REGION is omitted (no VAR= override).
    assert draft["env"] == {"TOKEN": ""}
    assert any("TOKEN" in w for w in draft["warnings"])
    # Full URL (scheme included), not a bare host substring, so it reads as a literal
    # presence check rather than URL-host sanitization.
    assert any("https://github.com/x/cool-mcp" in n for n in detail["notes"])


def test_glama_list_item_id_prefers_namespace_slug():
    item = glama._list_item({**_glama(), "namespace": "acme", "slug": "cool"})
    # The stable detail route is /v1/servers/{namespace}/{slug}; key on that, not the
    # deprecated opaque id.
    assert item["id"] == "acme/cool"


def test_glama_list_item_id_falls_back_to_opaque_id():
    item = glama._list_item({"id": "abc123", "name": "x"})
    assert item["id"] == "abc123"
