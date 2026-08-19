"""Per-server bridge host — the child process that does the actual elevation.

One bridge host == one FastMCP proxy in front of one stdio MCP server, served over
Streamable HTTP at ``/mcp`` by its own uvicorn on a loopback port. Running each
server in its own process gives fault isolation (a hung/crashing server can't take
down the control plane or its peers) and a real PID/port for supervision.

The control plane resolves the runner -> a literal ProcessSpec, then launches this
module as a subprocess, passing the spec + port via environment variables:

    MCPE_BRIDGE_SPEC  JSON: {command, args, env, cwd, name, transport, mcp_http, rest_openapi}
    MCPE_BRIDGE_HOST  loopback host (default 127.0.0.1)
    MCPE_BRIDGE_PORT  port to listen on

``transport`` selects the upstream: ``stdio`` (spawn command/args) or
``streamable-http`` / ``sse`` (front a remote URL; ``command`` is the URL and ``env``
is the upstream HTTP headers).

Session isolation: ``FastMCP.as_proxy(transport)`` gives a fresh upstream session
per request (no cross-client context mixing). "Sharing one subprocess" means
sharing this process + the package install across the MCP and REST surfaces —
never a single shared MCP session.

When the spec sets ``rest_openapi``, the same app additionally serves each tool as
plain REST (``POST /rest/<tool>``, body = the tool's JSON arguments) plus a
generated ``GET /rest/openapi.json`` — so non-MCP clients (curl, automation, GPT
Actions) can call the server's tools through the identical auth/proxy path. See
:func:`build_rest_routes`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

from fastmcp import Client, FastMCP
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers.proxy import ProxyClient
from fastmcp.server.transforms import ToolTransform
from fastmcp.tools.tool import Tool
from fastmcp.tools.tool_transform import ToolTransformConfig
from mcp.types import ClientCapabilities, Root, RootsCapability


async def _forward_roots(context) -> list[Root]:
    """Roots handler for the proxy's upstream (stdio) client.

    An upstream MCP server may ask its client — here, this proxy — to list the
    caller's filesystem roots. FastMCP's default proxy handler forwards that
    request straight to whichever client is connected to the elevator over HTTP.
    But many MCP clients (Claude.ai, and anything that connects without declaring
    the ``roots`` capability) reject ``roots/list``, and the upstream server then
    logs the rejection as a noisy, recurring::

        [FastMCP error] received error listing roots.
        McpError: MCP error -32603

    Forward the request only when the connected client actually advertises roots
    support, and degrade to an empty list on any failure. A client that can't
    list roots simply gets ``[]`` instead of a spurious error — which is exactly
    what "no roots" means to the upstream server.
    """
    try:
        ctx = get_context()
    except RuntimeError:
        # No active request context (e.g. the upstream asks during handshake).
        return []
    try:
        if not ctx.session.check_client_capability(
            ClientCapabilities(roots=RootsCapability())
        ):
            return []
        return await ctx.list_roots()
    except Exception:
        # Client advertised roots but failed to deliver them — don't surface the
        # failure to the upstream server as an internal error.
        return []


# SSOT for which env keys the docker CLI itself consumes lives in the docker runner (the
# service layer also rejects these as container env vars). ``is_reserved_docker_env`` is the
# NARROW set we inherit from the operator's env into the CLI; ``is_forbidden_container_env`` is
# the broader "a container must not supply this" set (adds Go proxy vars).
from app.config import is_control_plane_env_var as _is_control_plane_env_var  # noqa: E402
from app.runners.docker import (  # noqa: E402
    is_forbidden_container_env as _is_forbidden_container_env,
    is_reserved_docker_env as _is_reserved_docker_env,
)


def _child_env(spec: dict) -> dict[str, str]:
    """Environment for a stdio child.

    Default (npx/uvx/command): merge the bridge's inherited environment (PATH, HOME, proxy/CA,
    caches) with the server-specific vars so tools resolve; server vars win. The elevator's OWN
    ``MCPE_*`` config namespace is stripped first, so a passthrough server — even one that shells out
    to docker or reads ``BASH_ENV`` — does not INHERIT the control plane's secrets (admin token,
    signing keys, DB/data dir), and the secret subset is also kept out of the bridge parent's env
    (``ServerUnit._bridge_env``) so it isn't recoverable from ``/proc/<ppid>/environ``. NOTE: on a
    same-UID host without process isolation a child can still read an operator-supplied admin token
    from the control-plane process's own ``/proc/<pid>/environ``; a hard guarantee needs the docker
    runner, a separate UID / PID namespace / ``hidepid``, or supplying the token off-env. This env
    boundary is the primary in-process mitigation; the registry's shell-wrapped-docker detection is
    best-effort defense-in-depth on top of it. See docs/security.md.

    When the spec sets ``minimal_env`` (the docker runner), pass ONLY the bridge's docker-CLI env
    (PATH/HOME + the operator's ``DOCKER_*`` config) plus the server's NON-reserved vars — never the
    full ``os.environ`` — so the elevator's secrets can't leak into a container via a ``-e KEY``
    passthrough.
    """
    server_env = dict(spec.get("env") or {})
    if spec.get("minimal_env"):
        # The CLI's own env from the bridge: PATH/HOME + ALL the operator's DOCKER_* config
        # (DOCKER_HOST to reach dind, DOCKER_API_VERSION, DOCKER_CONFIG, …) so the runner CLI
        # behaves like the control plane's.
        base = {k: v for k, v in os.environ.items() if _is_reserved_docker_env(k)}
        # Strip forbidden keys from the server's env: a server-declared DOCKER_HOST /
        # DOCKER_API_VERSION / PATH must never retarget or alter the docker CLI (breaking dind
        # isolation or the daemon request), and a proxy var (HTTP_PROXY/…) must never land in the
        # CLI's env where it could reroute the control-plane's own daemon request on a TCP
        # DOCKER_HOST. `base` (the bridge's copies) wins. The service layer already rejects these;
        # this is defense in depth for a legacy row.
        safe = {k: v for k, v in server_env.items() if not _is_forbidden_container_env(k)}
        return {**safe, **base}
    # Scrub the control plane's own secrets from the inherited env before a passthrough child sees
    # it; a server may still set an ``MCPE_``-named var explicitly (it wins) — that carries the
    # operator's chosen value, not the elevator's.
    base = {k: v for k, v in os.environ.items() if not _is_control_plane_env_var(k)}
    return {**base, **server_env}


def _build_oauth_auth(oauth: dict):
    """Build a refresh-only OAuth httpx auth for a remote upstream.

    The interactive authorization already happened in the control plane (see
    ``app.auth.oauth_flow``); the tokens + DCR client info live in the shared file
    store. Here the bridge only needs an ``OAuthClientProvider`` that reads those
    tokens and refreshes them automatically as the access token expires. Discovery
    metadata is preloaded from the store so refresh targets the real token endpoint
    rather than the SDK's ``<server-url>/token`` fallback.

    The redirect/callback handlers raise: a bridge can't run an interactive sign-in.
    If the refresh token itself has lapsed, the upstream 401 leads here, the request
    fails, and the server goes unhealthy — the operator re-authenticates from the UI.
    """
    from app.auth.oauth_client import SingleChannelOAuthClientProvider
    from app.auth.oauth_store import ServerTokenStorage  # local import: keeps bridge import light

    from mcp.client.auth import TokenStorage
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyHttpUrl

    async def _no_interactive(*_args, **_kwargs):
        raise RuntimeError(
            "this upstream needs OAuth sign-in — re-authenticate it from the mcpelevator UI"
        )

    class _RefreshOnlyStorage(TokenStorage):
        """Bridge-side storage: reads tokens/client info and writes back REFRESHED tokens,
        but never persists a client REGISTRATION. Client registration is the control plane's
        interactive job. The primary guard against a probe-time DCR is the no-tokens early
        return in ``_build_oauth_auth`` (an unauthenticated server gets no provider at all);
        this no-op ``set_client_info`` is defense in depth for the residual case where tokens
        exist but a refresh has failed, so the SDK re-enters the 401 path — we still refuse to
        write a bridge-side registration (which would recreate a cleared file for the dummy
        loopback redirect)."""

        def __init__(self, inner: ServerTokenStorage):
            self._inner = inner

        async def get_tokens(self):
            return await self._inner.get_tokens()

        async def set_tokens(self, tokens) -> None:
            await self._inner.set_tokens(tokens)  # a real refresh must persist

        async def get_client_info(self):
            return await self._inner.get_client_info()

        async def set_client_info(self, client_info) -> None:
            return  # no-op: the bridge never registers a client

    url = oauth["url"]
    storage = ServerTokenStorage(oauth["server_id"])
    if not storage.status().get("authenticated"):
        # No stored access token yet (fresh create / post-Disconnect / lapsed refresh). Do
        # NOT attach an OAuth provider at all: with no stored client_info the SDK's 401 path
        # performs Dynamic Client Registration against the UPSTREAM on every readiness probe —
        # creating a throwaway client and burning the provider's registration quota — before
        # ``_no_interactive`` can stop it. (The no-op ``set_client_info`` below only blocks
        # LOCAL persistence of that registration, not the upstream call.) Returning ``None``
        # means the probe just gets a clean 401 with no OAuth requests at all, and the server
        # surfaces as needing sign-in. Re-authenticating from the UI restarts the bridge, which
        # rebuilds this with tokens present and a real refresh-capable provider.
        return None
    client_metadata = OAuthClientMetadata(
        client_name="mcpelevator",
        # Never used for refresh, but the model requires a redirect URI; a loopback
        # placeholder is fine (the bridge never performs an interactive grant).
        redirect_uris=[AnyHttpUrl("http://127.0.0.1/callback")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=oauth.get("scopes") or None,
    )
    # The SHARED provider, not a locally composed one: refresh sends token requests too,
    # so it needs the same one-credential-channel rule the control plane applies to the
    # code exchange — otherwise a sign-in would succeed and then fail at the first
    # refresh against a Basic-registered client.
    provider = SingleChannelOAuthClientProvider(
        server_url=url,
        client_metadata=client_metadata,
        storage=_RefreshOnlyStorage(storage),
        redirect_handler=_no_interactive,
        callback_handler=_no_interactive,
    )
    # Preload the discovered auth-server metadata so refresh knows the token endpoint
    # without a fresh 401/discovery round-trip (see ServerTokenStorage.set_metadata).
    metadata = storage.get_metadata()
    if metadata is not None:
        provider.context.oauth_metadata = metadata
    # Preload the protected-resource metadata too: if the auth server bound the tokens to
    # the PRM ``resource`` (a parent of the MCP URL), refresh must use that same resource,
    # not one recomputed from the URL — otherwise a resource-bound refresh is rejected.
    prm = storage.get_protected_resource_metadata()
    if prm is not None:
        provider.context.protected_resource_metadata = prm
    # Preload the stored token expiry so a lapsed access token is *refreshed* (silent)
    # rather than mistaken for valid and driven into the interactive 401 path.
    expiry = storage.get_token_expiry()
    if expiry is not None:
        provider.context.token_expiry_time = expiry
    # The SDK resets context.token_expiry_time from each refresh response's expires_in via
    # calculate_token_expiry(); when the provider OMITS expires_in that becomes None and
    # is_token_valid() then treats the access token as valid FOREVER — so a token that actually
    # lapses is sent stale and 401s into the (headless-impossible) interactive path until the
    # bridge restarts. Mirror the file store's default-TTL policy in memory (see
    # ServerTokenStorage._expires_at_for) so a running bridge keeps refreshing proactively:
    # when a refresh carries a refresh token but no expires_in, stamp the same modest fallback.
    import time as _time

    from mcp.shared.auth_utils import calculate_token_expiry

    from app.auth.oauth_store import _DEFAULT_REFRESHABLE_TTL

    _context = provider.context

    def _update_token_expiry(token) -> None:
        if token.expires_in is None and token.refresh_token:
            _context.token_expiry_time = _time.time() + _DEFAULT_REFRESHABLE_TTL
        else:
            _context.token_expiry_time = calculate_token_expiry(token.expires_in)

    # Instance-level override: the SDK calls ``self.context.update_token_expiry(token)`` from
    # both token-exchange and refresh handlers, so shadowing it on the instance covers both. If
    # a future SDK inlines the calc this simply stops applying (no breakage).
    _context.update_token_expiry = _update_token_expiry
    return provider


# `_meta` namespace under which a renamed tool carries its upstream name. PUBLIC: it's a
# contract between this bridge and the control plane's discovery probe
# (supervisor.unit.tool_summary), which reads it back so a rename never costs the UI the
# tool's stable identity. Namespaced to keep it clear of the upstream server's own meta
# and FastMCP's.
UPSTREAM_META_KEY = "io.mcpelevator/upstream"

# `normalize_schema_dialect` rewrites a `$schema` declaring exactly draft-07 — the dialect
# the MCP TypeScript SDK hardcodes with no config option (mcpelevator/mcpelevator#123) — to
# this one. 2020-12 is what the MCP spec itself targets and what a strict client's default
# validator expects; rewriting only the dialect pointer (never the schema's actual keywords)
# is safe for the common case a proxied tool schema is in — plain type/properties/required
# with nothing draft-07-specific — PROVIDED the source dialect really is draft-07: an older
# dialect (draft-04's boolean-form `exclusiveMinimum`/`exclusiveMaximum`, for one) has its
# own incompatibilities this module doesn't catalogue, so normalization intentionally covers
# ONLY draft-07's known-safe-when-guarded case (see `_DRAFT_07_SCHEMA_URIS`,
# `_has_incompatible_draft07_construct`) and leaves every other dialect — including one we've
# simply never seen — exactly as declared.
_SCHEMA_DIALECT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

# Every `$schema` string form draft-07 is plausibly declared with (with/without the trailing
# `#` fragment some generators omit; http vs the https json-schema.org now also serves).
_DRAFT_07_SCHEMA_URIS = frozenset(
    {
        "http://json-schema.org/draft-07/schema#",
        "http://json-schema.org/draft-07/schema",
        "https://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft-07/schema",
    }
)

# Keyword positions whose value is DIRECTLY a sub-schema (not a schema wrapped in another
# structure) — a construct nested under one of these still needs checking.
_SCHEMA_KEYWORDS = (
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "items",  # only when NOT list-valued — the list-valued (tuple) case is the construct itself
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
)
# Keyword positions whose value is a LIST of sub-schemas.
_SCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
# Keyword positions whose value is a MAP OF ARBITRARY NAME -> sub-schema — the keys are
# property/definition NAMES, never schema keywords, so they must never be checked as if they
# were one (a property literally named "dependencies" is not the `dependencies` keyword).
_SCHEMA_NAME_MAP_KEYWORDS = ("properties", "patternProperties", "$defs", "definitions")

# Pure annotations: keywords that carry no validation behavior in EITHER dialect, so their
# presence beside `$ref` is never the sibling-semantics hazard below — only an ASSERTION
# keyword sitting next to `$ref` changes meaning across the dialect boundary.
_ANNOTATION_ONLY_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)


def _has_incompatible_draft07_construct(schema: object) -> bool:
    """Whether ``schema`` (a JSON Schema node) uses a keyword whose MEANING changed between
    draft-07 and 2020-12 — so relabeling `$schema` alone would silently mis-declare it, not
    just rename it. Three known cases (mcpelevator/mcpelevator#123 and #124 review):

    * An array-valued ``items`` means positional TUPLE validation in draft-07, but ``items``
      must be a single schema in 2020-12 (tuples moved to ``prefixItems``) — left as
      array-valued under a 2020-12 label, a strict validator either rejects the schema
      outright or silently stops enforcing the per-position types.
    * ``dependencies`` (property/schema dependencies) has no keyword of that name in 2020-12
      at all (split into ``dependentRequired``/``dependentSchemas``) — left in place, it
      would just be ignored, silently DROPPING whatever constraint the upstream author
      intended (under-enforcement).
    * ``$ref`` alongside an ASSERTION keyword (``required``, ``minLength``, a sibling
      ``properties``, …): draft-07 ignores every sibling of ``$ref`` and validates only the
      referenced schema; 2020-12 evaluates the siblings too. Relabeling such a schema makes
      it STRICTER — arguments the upstream tool previously accepted (because the sibling was
      silently ignored) can start failing validation post-proxy (over-enforcement, the
      mirror image of the ``dependencies`` case). Pure-annotation siblings
      (``_ANNOTATION_ONLY_KEYWORDS`` — ``title``, ``description``, …) don't count: they carry
      no validation behavior in either dialect, so their presence changes nothing.

    Checked recursively — any of the three can be nested arbitrarily deep — but ONLY through
    positions the JSON Schema grammar actually declares as sub-schemas
    (`_SCHEMA_KEYWORDS`/`_SCHEMA_LIST_KEYWORDS`/`_SCHEMA_NAME_MAP_KEYWORDS`). A naive
    "recurse into every dict value" walk would descend into a `properties` map and mistake a
    PROPERTY NAME that happens to read `dependencies` for the keyword itself (review on #124),
    both missing real matches hidden behind other keywords this function doesn't yet know
    about and reporting one that isn't there.
    """
    if not isinstance(schema, dict):
        return False
    if isinstance(schema.get("items"), list) or "dependencies" in schema:
        return True
    if "$ref" in schema and any(k not in _ANNOTATION_ONLY_KEYWORDS for k in schema if k != "$ref"):
        return True
    for key in _SCHEMA_KEYWORDS:
        if key in schema and _has_incompatible_draft07_construct(schema[key]):
            return True
    for key in _SCHEMA_LIST_KEYWORDS:
        sub = schema.get(key)
        if isinstance(sub, list) and any(_has_incompatible_draft07_construct(v) for v in sub):
            return True
    for key in _SCHEMA_NAME_MAP_KEYWORDS:
        sub = schema.get(key)
        if isinstance(sub, dict) and any(_has_incompatible_draft07_construct(v) for v in sub.values()):
            return True
    return False


class _ToolTransform(ToolTransform):
    """FastMCP's ``ToolTransform`` with the gaps that matter to elevation closed.

    Elevation must be faithful: an operator relabelling a tool is changing its NAME and
    DESCRIPTION, and nothing else may move as a side effect.

    * **The upstream definition survives.** ``TransformedTool.from_tool`` (FastMCP 3.4.4)
      rebuilds the tool and drops the client-visible fields in ``_CARRIED_FIELDS`` — an
      upstream ``icons`` would vanish, and an ``execution`` declaring MCP task support
      (``taskSupport: required``) would too, so clients would issue an ordinary call the
      tool rejects — through even a description-only edit. ``ToolTransformConfig.meta``
      likewise REPLACES rather than merges, which would evict the upstream's own ``_meta``
      (vendor extensions, presentation hints) from every renamed tool.

    * **A rename never takes a name that's already taken.** FastMCP maps the target name
      back to the rename's source unconditionally. That silently misroutes in both
      directions: with a live tool of the target's name, the rename shadows it (calls to it
      reach the wrong tool); with the source GONE upstream, the target resolves to nothing,
      so a real tool is advertised by ``tools/list`` and then refused on call. Neither is
      reachable through the UI's own collision check, which only sees the tool list as it
      was BEFORE the change — an override written straight to the API, or an upstream that
      later adds a tool of that name, lands in exactly this state. So the rule is enforced
      here, against the live list: a rename applies only when nothing else answers to its
      target, and otherwise goes inert, leaving both tools reachable under their own names
      (the UI still flags the collision so the operator can resolve it).

    * **Identity can't be spoofed.** ``UPSTREAM_META_KEY`` means "the elevator renamed this
      tool, and here is its real name". An upstream that sets that key itself would hand the
      UI a false identity to key policy off — the operator would disable one row and a
      different tool would stay exposed. The key is stripped from every upstream tool, so
      only this transform can ever put it there.

    * **A draft-07 schema dialect can be normalized — but never mis-declared, and never
      guessed at for a dialect this bridge doesn't know.** ``normalize_schema_dialect``
      rewrites a tool's ``$schema`` (parameters/output schema) from EXACTLY draft-07 to
      2020-12, independent of the hide/rename policy above — a server whose SDK hardcodes
      draft-07 (issue #123) would otherwise have every tool refused by a strict client,
      whether or not the operator has touched its name or description. Two things stop
      that rewrite: a schema using a construct whose MEANING changed between the two
      dialects (an array-valued ``items`` — draft-07 tuple validation, invalid shape in
      2020-12; ``dependencies`` — no 2020-12 keyword of that name) is left under draft-07
      instead, since relabeling only the pointer there would silently invalidate or drop a
      constraint; and a schema declaring any OTHER dialect (draft-04, draft-06, a custom
      URI) is left untouched entirely, since this bridge only catalogues draft-07's
      specific incompatibilities.
    """

    def __init__(
        self,
        transforms: dict[str, ToolTransformConfig],
        *,
        normalize_schema_dialect: bool = False,
    ) -> None:
        # Own the reverse map rather than inheriting it: a HIDDEN tool exposes no name, so
        # it must not reserve one. FastMCP's constructor reserves a target for every entry
        # and raises on a duplicate, which made "hide `b`, rename `a` to `b`" — a
        # combination the UI and API both accept — a ValueError at proxy build, i.e. a
        # bridge that crash-loops and takes the server offline.
        #
        # Only REAL renames are recorded. Mapping an un-renamed tool to itself (FastMCP
        # does this) is not merely redundant: a key that carries labels but no rename would
        # overwrite an earlier entry claiming the same name, and since entries arrive
        # sorted, `a`->`x` alongside a description-only `x` lost its mapping. `list_tools`
        # would still advertise the renamed `x` while `get_tool("x")` looked for a native
        # `x` that doesn't exist — advertised and uncallable, the exact split this class
        # exists to prevent.
        self._transforms = transforms
        self._normalize_schema_dialect = normalize_schema_dialect
        self._name_reverse = {}
        for source, config in transforms.items():
            if not config.enabled or not config.name or config.name == source:
                continue
            self._name_reverse[config.name] = source

    @staticmethod
    def _normalized_schema(schema: dict | None) -> dict | None:
        """Rewrite ``schema``'s ``$schema`` to 2020-12, if — and only if — it declares
        EXACTLY draft-07 (``_DRAFT_07_SCHEMA_URIS``) and doesn't use a construct the
        dialect bump alone would silently mis-declare (see
        ``_has_incompatible_draft07_construct``). Absent, already 2020-12, or any OTHER
        dialect (draft-04, draft-06, a custom URI, …) is left exactly as declared — this
        function only knows draft-07's specific incompatibilities, so normalizing a
        dialect it hasn't catalogued would be a guess, not a fix. Returns the input
        unchanged (same object) otherwise, so callers can cheaply tell whether anything
        actually moved."""
        if not isinstance(schema, dict):
            return schema
        if schema.get("$schema") not in _DRAFT_07_SCHEMA_URIS:
            return schema
        if _has_incompatible_draft07_construct(schema):
            return schema
        return {**schema, "$schema": _SCHEMA_DIALECT_2020_12}

    def _scrub(self, tool: Tool) -> Tool:
        """Drop our reserved identity key, and — when enabled — normalize the schema
        dialect, on an upstream tool (see the class docstring)."""
        meta = tool.meta or {}
        update: dict = {}
        if UPSTREAM_META_KEY in meta:
            update["meta"] = {k: v for k, v in meta.items() if k != UPSTREAM_META_KEY}
        if self._normalize_schema_dialect:
            parameters = self._normalized_schema(tool.parameters)
            if parameters is not tool.parameters:
                update["parameters"] = parameters
            output_schema = self._normalized_schema(tool.output_schema)
            if output_schema is not tool.output_schema:
                update["output_schema"] = output_schema
        return tool.model_copy(update=update) if update else tool

    def _hides(self, name: str) -> bool:
        config = self._transforms.get(name)
        return config is not None and not config.enabled

    def _renames_to(self, name: str) -> str | None:
        """The name this tool would be renamed to, or None if it isn't renamed."""
        config = self._transforms.get(name)
        target = config.name if config is not None else None
        return target if target and target != name else None

    def _apply(self, source: Tool, config: ToolTransformConfig, *, rename: bool) -> Tool:
        """Relabel ``source`` in place of transforming it.

        ``ToolTransformConfig.apply`` REBUILDS the tool, and the rebuild isn't faithful: it
        drops ``icons`` and ``execution``, replaces rather than merges ``_meta``, and — worst
        — regenerates the input schema and the forwarding closure from it, so a tool with an
        open-ended schema ends up rejecting every argument it is handed, valid ones included.
        None of that machinery is needed here: this transform never rewrites arguments, only
        labels. Copying the source tool with new labels keeps its schema, its metadata and
        its dispatch exactly as the upstream declared them, by construction rather than by
        restoring fields one at a time as they're discovered missing.
        """
        update: dict = {}
        if rename and config.name:
            update["name"] = config.name
            # Carry the pre-rename identity (see the class docstring).
            update["meta"] = {**(source.meta or {}), UPSTREAM_META_KEY: {"name": source.name}}
        if config.description:
            update["description"] = config.description
        return source.model_copy(update=update) if update else source

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        sources = [self._scrub(tool) for tool in tools]
        # A hidden tool answers to no name, so it doesn't hold one against a rename.
        taken = {tool.name for tool in sources if not self._hides(tool.name)}
        result = []
        for source in sources:
            if self._hides(source.name):
                continue  # hidden: absent from discovery
            config = self._transforms.get(source.name)
            if config is None:
                result.append(source)
                continue
            target = self._renames_to(source.name)
            result.append(self._apply(source, config, rename=target is not None and target not in taken))
        return result

    async def _resolve_native(
        self, direct: Tool, name: str, call_next, version
    ) -> Tool | None:
        """Resolve ``name`` as the tool that natively carries it, under its OWN policy."""
        if self._hides(name):
            return None  # hidden tools are refused, not just unlisted
        source = self._scrub(direct)
        config = self._transforms.get(name)
        if config is None:
            return source
        target = self._renames_to(name)
        if target is not None:
            other = await call_next(target, version=version)
            if other is None or self._hides(target):
                return None  # renamed away: this name is dead
            # Rename refused because the target is taken — the tool keeps its own name,
            # but the rest of its policy (hiding, description) still applies.
            return self._apply(source, config, rename=False)
        return self._apply(source, config, rename=True)

    async def get_tool(self, name: str, call_next, *, version=None) -> Tool | None:
        # Whoever natively answers to this name wins it — that settles the misrouting
        # cases (a rename shadowing a live tool, and a stale rename stranding one).
        direct = await call_next(name, version=version)
        original_name = self._name_reverse.get(name, name)

        if original_name != name:  # `name` is some tool's rename target
            if direct is None or self._hides(name):
                # The name is free (or its owner is hidden), so the rename may take it —
                # provided its source is actually there.
                source = await call_next(original_name, version=version)
                if source is not None:
                    return self._apply(
                        self._scrub(source), self._transforms[original_name], rename=True
                    )
                if direct is None:
                    return None
                # Stale rename. The name reverts to the tool that carries it — under that
                # tool's own policy, so a HIDDEN one stays refused rather than leaking.
            else:
                pass  # taken by a live tool: the rename doesn't apply
        elif direct is None:
            return None
        return await self._resolve_native(direct, name, call_next, version)


def _tool_transform(spec: dict) -> ToolTransform:
    """The single place per-tool policy is applied to what this bridge serves.

    Two operator controls, one mechanism:

    * ``disabled_tools`` (issue #105) — names hidden from clients, because a passthrough
      server often ships internal-only tools that just waste the model's context.
    * ``tool_overrides`` (issue #112) — a rename and/or a replacement description, keyed by
      the UPSTREAM tool name, because a server the operator can't rebuild (a closed-source
      paid endpoint, say) may expose names and descriptions models handle badly.

    Both ride FastMCP's own ``ToolTransform`` rather than hand-rolled middleware, so the
    library owns the semantics: a disabled tool drops out of ``tools/list`` AND is refused on
    call with the same ``Unknown tool`` error an unregistered one raises (so a client holding
    a stale list can't still invoke it), and a renamed tool answers to its NEW name only —
    the upstream name stops resolving, exactly as if the server had been rebuilt.

    Applying it here covers all three exposed surfaces at once, because they all resolve
    tools through this same FastMCP instance: the Streamable-HTTP ``tools/list`` +
    ``tools/call``, the REST/OpenAPI routes (which list tools over an in-memory client, so
    the routes themselves are generated from the overridden names), and the group hub
    (which proxies this bridge's ``/mcp`` over HTTP).

    Both are part of the launch spec (``config_hash``), so a change restarts the bridge and
    rebuilds this transform — the control plane's tool-discovery probe then caches exactly
    what clients see, and the UI maps those names back to their upstream keys via the same
    overrides it holds in the DB row.

    A third, orthogonal control also rides this transform: ``normalize_schema_dialect``
    (issue #123) rewrites a legacy ``$schema`` dialect declaration on every tool's
    parameters/output schema to 2020-12, regardless of whether that tool carries any
    hide/rename policy — see ``_ToolTransform._scrub``.

    Always returns a transform, even with an empty policy: ``_ToolTransform`` also strips our
    reserved identity key from upstream tools, and a server can forge that key whether or not
    anyone has set a policy on it. A key naming a tool the upstream doesn't (or no longer)
    exposes is a no-op — including when its rename target matches a real upstream tool.
    """
    disabled = set(spec.get("disabled_tools") or [])
    overrides: dict = spec.get("tool_overrides") or {}
    configs: dict[str, ToolTransformConfig] = {}
    for tool in sorted(disabled | set(overrides)):
        override = overrides.get(tool) or {}
        # Only pass fields the operator actually set: ToolTransformConfig applies
        # `enabled` only when explicitly provided, and an unset name/description means
        # "keep the upstream's".
        fields: dict = {k: override[k] for k in ("name", "description") if override.get(k)}
        if fields.get("name"):
            # Stamp the upstream name into the tool's `_meta` so identity survives the
            # rename. The control plane caches it (see supervisor.unit.tool_summary) and the
            # UI keys its rows off it — inferring identity by reversing the rename map is
            # lossy, because an exposed name is not unique: a stale override key whose
            # target later matches a real upstream tool would silently re-identify that
            # tool, and edits would then be staged against the wrong one.
            fields["meta"] = {UPSTREAM_META_KEY: {"name": tool}}
        if tool in disabled:
            # Hiding wins over renaming: a tool that's both is simply gone, under either name.
            fields["enabled"] = False
        if fields:
            configs[tool] = ToolTransformConfig(**fields)
    return _ToolTransform(
        configs, normalize_schema_dialect=bool(spec.get("normalize_schema_dialect"))
    )


def _build_transport(spec: dict):
    """Pick the upstream transport from the spec's ``transport`` discriminator.

    ``stdio`` (the default) spawns the local command; ``streamable-http`` / ``sse``
    front an already-remote MCP URL. For the remote kinds ``command`` is the URL and
    ``env`` is the upstream HTTP headers — so they are NOT merged into ``os.environ``
    (that merge is only meaningful for a real child process). When ``oauth`` is set the
    upstream authenticates via an OAuth token (auto-refreshed) instead of static headers.
    """
    kind = spec.get("transport") or "stdio"
    oauth = spec.get("oauth")
    auth = _build_oauth_auth(oauth) if oauth else None
    headers = dict(spec.get("env") or {})
    if oauth:
        # OAuth owns the Authorization header. Drop any static one left over from a
        # Headers→OAuth switch: when there's no/expired OAuth token (startup, post-
        # disconnect, lapsed refresh) the auth object adds no bearer, and a stale static
        # token would otherwise still authenticate the upstream despite the UI saying
        # the server needs to re-authenticate.
        headers = {k: v for k, v in headers.items() if k.strip().lower() != "authorization"}
    if kind in ("streamable-http", "http"):
        return StreamableHttpTransport(url=spec["command"], headers=headers, auth=auth)
    if kind == "sse":
        return SSETransport(url=spec["command"], headers=headers, auth=auth)
    return StdioTransport(
        command=spec["command"],
        args=list(spec.get("args") or []),
        env=_child_env(spec),
        cwd=spec.get("cwd") or None,
    )


def build_proxy(spec: dict) -> FastMCP:
    """Build the FastMCP proxy that fronts one upstream MCP server.

    The upstream — a local stdio process or a remote HTTP/SSE URL, per
    :func:`_build_transport` — is wrapped in a ``ProxyClient`` carrying our tolerant
    roots handler (see :func:`_forward_roots`); all other advanced forwarding and the
    fresh-session-per-request isolation keep FastMCP's proxy defaults.
    """
    transport = _build_transport(spec)
    # Wrap the transport in a ProxyClient ourselves so we can install a roots
    # handler that tolerates clients without roots support (see _forward_roots).
    # Everything else — sampling, elicitation, logging, progress forwarding, and
    # the fresh-session-per-request isolation — keeps FastMCP's proxy defaults.
    client = ProxyClient(transport, roots=_forward_roots)
    proxy = create_proxy(client, name=spec.get("name") or "mcpelevator-proxy")
    # Apply the operator's per-tool policy (hidden tools, renames, descriptions) to every
    # surface (MCP list/call, REST, group hub) — they all resolve tools through this
    # proxy. See _tool_transform.
    # Always installed, even with no policy configured: besides applying the operator's
    # overrides, the transform strips our reserved identity key from upstream tools, and a
    # server can forge that key whether or not anyone has set a policy on it.
    proxy.add_transform(_tool_transform(spec))
    # The outer proxy already authenticated the caller. Forwarding its headers from
    # this bridge to a remote MCP server would disclose bearer/OAuth credentials to
    # that upstream. Static headers and upstream OAuth live on the transport itself.
    if hasattr(transport, "forward_incoming_headers"):
        transport.forward_incoming_headers = False
    return proxy


# --- REST/OpenAPI surface (rest_openapi exposure) -----------------------------


def _rest_envelope(result) -> dict:
    """The stable REST response body for one tool call. MCP semantics are mirrored:
    ``is_error`` carries the tool's own failure (HTTP stays 200, like MCP's 200 +
    isError), ``structured_content`` is the tool's structured output when declared,
    and ``content`` is the raw MCP content blocks for everything else."""
    return {
        "is_error": bool(result.is_error),
        "content": [block.model_dump(mode="json") for block in result.content or []],
        "structured_content": result.structured_content,
    }


def _openapi_document(tools, name: str) -> dict:
    """An OpenAPI 3.1 document for the tool routes, generated from the live tool
    list so it always matches what the upstream currently serves. The relative
    ``servers`` url ("../") resolves against the document's own retrieval URL —
    ``…/s/<slug>/rest/openapi.json`` → ``…/s/<slug>`` — so the doc is correct
    behind the proxy without the bridge knowing its public slug or base URL."""
    paths: dict = {}
    for tool in tools:
        responses: dict = {
            "200": {
                "description": (
                    "Tool result envelope. `is_error` mirrors MCP's isError: the tool "
                    "itself failed but the call transported fine."
                ),
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "is_error": {"type": "boolean"},
                                "content": {"type": "array", "items": {"type": "object"}},
                                "structured_content": {
                                    **(tool.outputSchema or {}),
                                    "description": "Structured tool output (null unless the tool declares an outputSchema).",
                                },
                            },
                        }
                    }
                },
            }
        }
        paths[f"/rest/{tool.name}"] = {
            "post": {
                "operationId": tool.name,
                "summary": (tool.description or tool.name).strip().splitlines()[0][:120],
                "description": tool.description or "",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": tool.inputSchema or {"type": "object"}
                        }
                    },
                },
                "responses": responses,
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": name, "version": "1.0.0"},
        "servers": [{"url": "../"}],
        "paths": paths,
    }


def build_rest_routes(proxy: FastMCP, spec: dict) -> list:
    """Starlette routes for the REST surface, backed by in-memory client sessions
    against the same proxy the MCP surface serves — so each REST call gets the
    identical fresh-upstream-session semantics as an MCP request."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    name = spec.get("name") or "mcpelevator-proxy"

    async def openapi(request):
        async with Client(proxy) as client:
            tools = await client.list_tools()
        return JSONResponse(_openapi_document(tools, name))

    async def index(request):
        async with Client(proxy) as client:
            tools = await client.list_tools()
        return JSONResponse(
            {
                "tools": [
                    {"name": t.name, "description": t.description or "", "path": f"rest/{t.name}"}
                    for t in tools
                ],
                "openapi": "rest/openapi.json",
            }
        )

    async def call(request):
        tool = request.path_params["tool"]
        body = await request.body()
        if body:
            try:
                arguments = json.loads(body)
            except ValueError:
                return JSONResponse({"detail": "request body must be JSON"}, status_code=400)
            if not isinstance(arguments, dict):
                return JSONResponse(
                    {"detail": "request body must be a JSON object of tool arguments"},
                    status_code=400,
                )
        else:
            arguments = {}
        async with Client(proxy) as client:
            known = {t.name for t in await client.list_tools()}
            if tool not in known:
                return JSONResponse({"detail": f"unknown tool {tool!r}"}, status_code=404)
            result = await client.call_tool(tool, arguments, raise_on_error=False)
        return JSONResponse(_rest_envelope(result))

    return [
        Route("/rest", index, methods=["GET"]),
        Route("/rest/openapi.json", openapi, methods=["GET"]),
        Route("/rest/{tool}", call, methods=["POST"]),
    ]


def build_app(spec: dict, proxy: FastMCP):
    """The bridge's ASGI app: the Streamable-HTTP MCP surface at ``/mcp``, plus the
    REST routes when the server's ``rest_openapi`` exposure is on."""
    app = proxy.http_app(path="/mcp")
    if spec.get("rest_openapi"):
        # Static REST paths first; the {tool} route is POST-only so it can't shadow
        # the GET routes anyway, but explicit ordering keeps intent obvious.
        app.router.routes.extend(build_rest_routes(proxy, spec))
    return app


def main() -> None:
    """Entry point: read the ProcessSpec + port from the environment and serve."""
    spec = json.loads(os.environ["MCPE_BRIDGE_SPEC"])
    host = os.environ.get("MCPE_BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ["MCPE_BRIDGE_PORT"])

    proxy = build_proxy(spec)
    if spec.get("rest_openapi"):
        # Compose MCP + REST into one app; run uvicorn directly (http_app carries
        # the session-manager lifespan, which uvicorn executes).
        import uvicorn

        uvicorn.run(build_app(spec, proxy), host=host, port=port, log_level="info")
    else:
        # run() handles uvicorn + the Streamable HTTP session-manager lifespan for us.
        proxy.run(transport="http", host=host, port=port, show_banner=False)


if __name__ == "__main__":
    main()
