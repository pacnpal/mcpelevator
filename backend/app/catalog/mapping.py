"""Pure launch-spec mapping — a registry *package* → an mcpelevator run draft.

No I/O, no globals, no clocks: referentially transparent so the same package always
yields the same draft (Determinism), with stable arg/env ordering. This is the shared
core any *package-based* registry (one exposing ``registryType``/``identifier``/
``version`` like the official MCP Registry) reuses; per-source document shapes are
normalized in each source module before calling in here.

Runner semantics mirror what the bridge launches (``runners/base.py``) and the friendly
form (``ServerForm.syncFromFriendly``):

    npm  → npx :  command="npx", args=["-y", <id>[@ver], *pkg_args]
    pypi → uvx :  command="uvx", args=[<id>[==ver], *pkg_args]
"""

from __future__ import annotations

from typing import Any

# registryType → mcpelevator runner. The only two we can launch today (SSOT for the map).
RUNNER_BY_TYPE = {"npm": "npx", "pypi": "uvx"}

# Why a given registry type can't be auto-installed yet (shown in the UI).
UNSUPPORTED_REASON = {
    "oci": "Docker/OCI packages aren't installable yet (the docker runner is not enabled).",
    "nuget": "NuGet packages aren't supported yet.",
    "mcpb": "MCPB bundles aren't supported yet.",
}


def pin_identifier(registry_type: str, identifier: str, version: str | None) -> str:
    """
    Pin an identifier to a specific registry version for reproducible installs.
    
    Parameters:
        registry_type (str): The registry type.
        identifier (str): The package identifier.
        version (str | None): The package version to pin.
    
    Returns:
        str: The pinned identifier, or the original identifier when the version is missing or set to ``"latest"``.
    """
    if not version or version == "latest":
        return identifier
    if registry_type == "npm":
        return f"{identifier}@{version}"
    if registry_type == "pypi":
        return f"{identifier}=={version}"
    return identifier


def argument_tokens(args: list[Any], warnings: list[str]) -> list[str]:
    """
    Flatten registry package arguments into argv tokens.
    
    Parameters:
    	args (list[Any]): Package argument definitions to convert.
    	warnings (list[str]): A list that receives warnings for required arguments without values.
    
    Returns:
    	list[str]: The flattened argument tokens in input order.
    """
    tokens: list[str] = []
    for arg in args:
        if not isinstance(arg, dict):
            continue
        kind = arg.get("type", "positional")
        value = arg.get("value")
        if value is None:
            value = arg.get("default")
        if value is not None and "{" in str(value) and "}" in str(value):
            # The registry's variable-substitution syntax (e.g. "{path}") isn't expanded
            # here; flag it so the operator replaces the placeholder in the review form.
            warnings.append(
                f"Argument value '{value}' contains a {{…}} placeholder — replace it with a real value."
            )
        # A named arg "takes a value" if it advertises one (valueHint/format/choices);
        # without any of those it's a boolean flag (e.g. --verbose).
        takes_value = bool(arg.get("valueHint") or arg.get("format") or arg.get("choices"))
        if kind == "named":
            name = arg.get("name")
            if not name:
                continue
            if value is not None:
                tokens.extend([str(name), str(value)])
            elif arg.get("isRequired"):
                # A required option needs a value. Emit a VISIBLE placeholder, not an
                # empty string — the form's splitLines() drops blank tokens, which would
                # silently omit the argument; "<hint>" survives and shows where to fill in.
                hint = arg.get("valueHint") or "value"
                tokens.extend([str(name), f"<{hint}>"])
                warnings.append(f"Argument {name} needs a value — replace <{hint}> before starting.")
            elif takes_value:
                # Optional value-taking option left unset: omit it. Emitting a bare
                # "--categories" would consume the next token (or fail CLI parsing); the
                # operator can add it in the form's Advanced section if they want it.
                continue
            else:
                tokens.append(str(name))  # a bare flag (e.g. --verbose); no value to add
        else:  # positional
            if value is not None:
                tokens.append(str(value))
            elif arg.get("isRequired"):
                hint = arg.get("valueHint") or arg.get("description") or "value"
                tokens.append(f"<{hint}>")
                warnings.append(f"Replace the <{hint}> positional argument with a real value before starting.")
    return tokens


def environment(env_vars: list[Any], warnings: list[str]) -> dict[str, str]:
    """
    Build an environment mapping from registry environment variables.
    
    Parameters:
    	env_vars (list[Any]): Registry environment variable entries.
    	warnings (list[str]): Collected warning messages.
    
    Returns:
    	dict[str, str]: Environment variables keyed by name, with missing values represented as empty strings.
    """
    env: dict[str, str] = {}
    for var in env_vars:
        if not isinstance(var, dict):
            continue
        name = var.get("name")
        if not name:
            continue
        value = var.get("value")
        if value is None:
            value = var.get("default")
        required = bool(var.get("isRequired") or var.get("isSecret"))
        if value is None and not required:
            # Optional and unset: omit it. Exporting name="" overrides the package's own
            # default/absence behavior and can break startup; absence lets it fall back.
            continue
        env[str(name)] = "" if value is None else str(value)
        if value is None and required:
            kind = "secret" if var.get("isSecret") else "required"
            warnings.append(f"Environment variable {name} is {kind} — set its value before starting.")
    return env


def blank_draft(index: int, registry_type: str, identifier: str, version: Any) -> dict[str, Any]:
    """
    Create a non-installable draft scaffold for a registry package.
    
    Parameters:
        index (int): The package position in the registry list.
        registry_type (str): The package's registry type.
        identifier (str): The package identifier.
        version (Any): The package version value.
    
    Returns:
        dict[str, Any]: A draft with normalized package metadata and empty command, arguments, environment, warnings, and reason fields.
    """
    return {
        "package_index": index,
        "registry_type": registry_type or "unknown",
        "identifier": identifier,
        "version": None if version in (None, "") else str(version),
        "runner": None,
        "command": "",
        "args": [],
        "env": {},
        "installable": False,
        "reason": None,
        "warnings": [],
    }


def package_draft(index: int, pkg: dict[str, Any]) -> dict[str, Any]:
    """Map a registry package entry to an install draft.
    
    Produces a draft for local stdio packages with supported registry types, or a
    non-installable draft with a reason when the package cannot be mapped.
    
    Parameters:
    	index (int): Package index in the registry list.
    	pkg (dict[str, Any]): Registry package entry.
    
    Returns:
    	dict[str, Any]: The mapped install draft.
    """
    registry_type = str(pkg.get("registryType") or "").lower()
    identifier = str(pkg.get("identifier") or "")
    version = pkg.get("version")
    transport = pkg.get("transport") or {}
    transport_type = str(transport.get("type") or "stdio").lower()

    draft = blank_draft(index, registry_type, identifier, version)

    if transport_type != "stdio":
        draft["reason"] = f"Transport '{transport_type}' isn't a local stdio server — nothing to elevate."
        return draft

    runner = RUNNER_BY_TYPE.get(registry_type)
    if runner is None:
        draft["reason"] = UNSUPPORTED_REASON.get(
            registry_type, f"Registry type '{registry_type or 'unknown'}' isn't supported yet."
        )
        return draft

    if not identifier:
        draft["reason"] = "Package is missing an identifier."
        return draft

    warnings: list[str] = []
    pinned = pin_identifier(registry_type, identifier, version)
    # runtimeArguments are flags for the runner itself (e.g. npx --package=…, uvx --from …)
    # and belong BEFORE the package identifier; packageArguments go after it.
    runtime_args = argument_tokens(pkg.get("runtimeArguments") or [], warnings)
    pkg_args = argument_tokens(pkg.get("packageArguments") or [], warnings)
    env = environment(pkg.get("environmentVariables") or [], warnings)

    if runtime_args:
        warnings.append(
            "This package uses runtime arguments — they're placed before the package; "
            "review the resolved command before starting."
        )

    if runner == "npx":
        draft["command"] = "npx"
        draft["args"] = ["-y", *runtime_args, pinned, *pkg_args]
    else:  # uvx
        draft["command"] = "uvx"
        draft["args"] = [*runtime_args, pinned, *pkg_args]

    draft["runner"] = runner
    draft["env"] = env
    draft["installable"] = True
    draft["warnings"] = warnings
    return draft
