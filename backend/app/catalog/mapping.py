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

# registryType → mcpelevator runner (SSOT for the map). oci maps to the docker runner;
# whether an oci draft is actually installable is gated at the API layer by the opt-in,
# root-equivalent `docker_runner` setting (this mapping stays pure/settings-free).
RUNNER_BY_TYPE = {"npm": "npx", "pypi": "uvx", "oci": "docker"}

# runtimeHint values each runner can honor. A package asking for a different runtime
# (node, bun, dnx, …) wouldn't actually launch via that runner, so it's treated as manual.
_ACCEPTED_HINTS = {"npx": {"npx"}, "uvx": {"uvx", "uv"}, "docker": {"docker"}}

# Why a given registry type can't be auto-installed (shown in the UI).
UNSUPPORTED_REASON = {
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
    if registry_type == "oci":
        # An OCI image is pinned by tag; the docker runner launches "image:tag". If the
        # identifier already carries a tag (a ":" in the last path segment) or a digest
        # ("@sha256:…"), it's a complete reference — don't double-tag it into "img:1:1".
        last_segment = identifier.rsplit("/", 1)[-1]
        if "@" in identifier or ":" in last_segment:
            return identifier
        return f"{identifier}:{version}"
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
        # A named arg "takes a value" if it advertises one in any way (valueHint /
        # format / choices / placeholder, or it's a secret like --password); without
        # any of those signals it's a boolean flag (e.g. --verbose).
        takes_value = bool(
            arg.get("valueHint")
            or arg.get("format")
            or arg.get("choices")
            or arg.get("placeholder")
            or arg.get("isSecret")
        )
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


def environment(
    env_vars: list[Any], warnings: list[str], *, label: str = "Environment variable"
) -> dict[str, str]:
    """
    Build an environment mapping from registry environment variables.

    Also reused for remote-server HTTP ``headers`` (same name/value/isRequired/isSecret
    shape) — pass ``label="Header"`` so the warnings read correctly.

    Parameters:
    	env_vars (list[Any]): Registry environment variable / header entries.
    	warnings (list[str]): Collected warning messages.
    	label (str): Noun used in warning text (e.g. "Environment variable" or "Header").

    Returns:
    	dict[str, str]: Variables keyed by name, with missing values represented as empty strings.
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
        # Only isRequired decides whether to prefill an unset var; isSecret is display
        # metadata. An OPTIONAL secret left unset must be omitted, not scaffolded as
        # NAME="" (that would override the package's own fallback/absence behavior).
        required = bool(var.get("isRequired"))
        if value is None and not required:
            continue
        env[str(name)] = "" if value is None else str(value)
        if value is None and required:
            kind = "secret" if var.get("isSecret") else "required"
            warnings.append(f"{label} {name} is {kind} — set its value before starting.")
        elif value is not None and "{" in str(value) and "}" in str(value):
            # Unexpanded server.json "{variable}" placeholder — flag it so the draft isn't
            # auto-started with a literal "{token}" value.
            warnings.append(
                f"{label} {name} contains a {{…}} placeholder — replace it with a real value."
            )
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
    transport = pkg.get("transport")
    if not isinstance(transport, dict):
        transport = {}
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

    # A package can ask for a specific runtime; if it isn't one this runner provides,
    # an npx/uvx command would launch the wrong thing, so leave it for manual setup.
    runtime_hint = str(pkg.get("runtimeHint") or "").lower()
    if runtime_hint and runtime_hint not in _ACCEPTED_HINTS[runner]:
        draft["reason"] = f"Package requests the '{runtime_hint}' runtime, which isn't supported yet."
        return draft

    warnings: list[str] = []
    pinned = pin_identifier(registry_type, identifier, version)
    # runtimeArguments are flags for the runner itself (e.g. npx --package=…, uvx --from …)
    # and belong BEFORE the package identifier; packageArguments go after it.
    runtime_args = argument_tokens(pkg.get("runtimeArguments") or [], warnings)
    pkg_args = argument_tokens(pkg.get("packageArguments") or [], warnings)
    env = environment(pkg.get("environmentVariables") or [], warnings)

    if runner == "npx":
        draft["command"] = "npx"
        draft["args"] = ["-y", *runtime_args, pinned, *pkg_args]
    elif runner == "uvx":
        draft["command"] = "uvx"
        draft["args"] = [*runtime_args, pinned, *pkg_args]
    else:  # docker — canonical shape is command=image ref, args=the container's own args.
        # The docker runner synthesizes all `docker run` flags itself, so registry-declared
        # runtimeArguments (docker flags) are NOT stored; the tail check below flags that.
        draft["command"] = pinned
        draft["args"] = pkg_args

    draft["runner"] = runner
    draft["env"] = env
    draft["warnings"] = warnings

    if runtime_args:
        # runtimeArguments may already supply the package/executable (e.g. uvx --from
        # pkg binary), so blindly appending the pinned identifier can duplicate it. The
        # command above is a best-effort scaffold — keep it for the form, but require
        # manual review rather than marking it auto-installable.
        draft["reason"] = (
            "This package declares runtime arguments; the generated command is a "
            "best-effort scaffold — review and adjust it before starting."
        )
        draft["installable"] = False
    else:
        draft["installable"] = True
    return draft
