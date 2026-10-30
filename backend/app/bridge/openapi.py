"""OpenAPI → MCP translation for the ``openapi`` runner (see app.runners.openapi).

The bridge has no MCP upstream to proxy here: at startup it fetches the server's OpenAPI
document and builds a FastMCP server from it (FastMCP's ``OpenAPIProvider``), one tool per
operation, whose calls are plain HTTP requests to the API. :func:`app.bridge.host.build_proxy`
then installs the same tool-policy transform it puts on every proxy, so hidden tools,
renames, the REST mirror and the group hub behave exactly as they do for an MCP upstream.

The document is read once per activation — a restart re-reads it, the way a restart
re-discovers an MCP upstream's tools. Anything wrong with it (unreachable, not JSON/YAML,
Swagger 2.0, no usable base URL) raises out of :func:`build_server`, so the bridge exits
before it ever listens and the startup attempt fails with that message in the server's log.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import components as _openapi_components
from fastmcp.tools.tool import Tool
from fastmcp.utilities.openapi import HTTPRoute

logger = logging.getLogger(__name__)


# FastMCP's OpenAPITool copies the INCOMING MCP request's headers onto every API call
# (everything but Authorization/Cookie and hop-by-hop). Here that request is the caller's,
# relayed by the /s proxy, so the API would receive whatever the caller sent the elevator —
# a Cloudflare Access assertion, forwarded-for addresses, any custom key header. The same
# rule as a remote upstream applies (app.bridge.host.build_proxy, forward_incoming_headers):
# the API sees the operator's configured headers and nothing of the caller's. The tool
# looks the function up in its module's namespace, so that is where it's replaced; the
# guard makes a FastMCP that moved it fail at startup rather than quietly resume leaking.
def _no_caller_headers(*_args, **_kwargs) -> dict[str, str]:
    return {}


if not hasattr(_openapi_components, "get_http_headers"):
    raise RuntimeError(
        "fastmcp.server.providers.openapi.components no longer imports get_http_headers; "
        "re-check how OpenAPITool forwards caller headers before serving openapi servers"
    )
_openapi_components.get_http_headers = _no_caller_headers

class DocumentError(ValueError):
    """The OpenAPI document can't be served: unreachable, unparseable, or untranslatable.
    The bridge reports it as one log line and exits (see app.bridge.host.main)."""


# How long one request to the API (and the document fetch) may take.
TIMEOUT_S = 30.0
# Ceiling on the document's size: a real spec runs to a few MB (Stripe's is ~7 MB), and the
# bridge parses it in memory, so an endpoint streaming without end mustn't exhaust it.
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


def _read_capped(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_DOCUMENT_BYTES:
            raise DocumentError(
                f"OpenAPI document is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB"
            )
    return bytes(body)


def fetch_document(url: str, headers: dict[str, str], *, transport=None) -> bytes:
    """Download the OpenAPI document at ``url``.

    Tried without credentials first: most documents are public, and the configured headers
    are the API's credentials, not the document host's. Only when the document itself
    answers 401/403 is it retried with them (the API serving its own spec behind its own
    auth), and that retry follows no redirect, so the credentials reach exactly the URL the
    operator typed and nowhere it might bounce them.
    """
    with httpx.Client(timeout=TIMEOUT_S, transport=transport) as client:
        with client.stream("GET", url, follow_redirects=True) as response:
            denied = response.status_code in (401, 403)
            if not (denied and headers):
                response.raise_for_status()
                return _read_capped(response)
        with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
            response.raise_for_status()
            return _read_capped(response)


def parse_document(raw: bytes) -> dict[str, Any]:
    """Parse a JSON or YAML OpenAPI document, refusing what FastMCP can't translate."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise DocumentError("OpenAPI document isn't UTF-8 text") from None
    try:
        doc = json.loads(text)
    except ValueError:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise DocumentError(f"OpenAPI document is neither JSON nor YAML: {exc}") from None
    if not isinstance(doc, dict):
        raise DocumentError("OpenAPI document must be a JSON/YAML object")
    if "swagger" in doc:
        raise DocumentError(
            f"Swagger {doc.get('swagger')} documents aren't supported; convert it to "
            "OpenAPI 3 first (e.g. https://converter.swagger.io)"
        )
    version = str(doc.get("openapi") or "")
    if not version.startswith("3."):
        raise DocumentError(
            f"not an OpenAPI 3 document (openapi: {version or 'missing'!r})"
        )
    if not isinstance(doc.get("paths") or {}, dict):
        raise DocumentError("OpenAPI document's `paths` must be an object")
    return doc


def resolve_base_url(doc: dict[str, Any], document_url: str, override: str | None) -> str:
    """The URL API calls are made against.

    The operator's override wins. Otherwise the document's first ``servers`` entry, with
    each server variable at its default — resolved against the document's own URL, since a
    relative entry ("/api/v3") is common and means "on the host that served this". With no
    ``servers`` at all OpenAPI's own default applies: a single server at "/".
    """
    if override:
        return override
    servers = doc.get("servers") or [{"url": "/"}]
    first = servers[0] if isinstance(servers, list) and servers else {}
    url = str(first.get("url") or "/") if isinstance(first, dict) else "/"
    variables = first.get("variables") if isinstance(first, dict) else None
    for name, variable in (variables or {}).items():
        default = variable.get("default", "") if isinstance(variable, dict) else ""
        url = url.replace(f"{{{name}}}", str(default))
    base = urljoin(document_url, url)
    if not base.startswith(("http://", "https://")):
        raise DocumentError(
            f"can't call the API at {base!r}; set a base URL override for this server"
        )
    return base


def _configured_header_hider(headers: dict[str, str]):
    """An ``mcp_component_fn`` dropping, from each tool's arguments, every header parameter
    the operator already supplies (typically the API key the document declares per
    operation). The model is never asked for a credential it can't know, and — together
    with :func:`_force_headers` — can't override the real one by guessing."""
    configured = {name.lower() for name in headers}

    def hide(route: HTTPRoute, component) -> None:
        if not configured or not isinstance(component, Tool):
            return
        drop = {
            arg
            for arg, where in route.parameter_map.items()
            if where.get("location") == "header"
            and str(where.get("openapi_name", "")).lower() in configured
        }
        if not drop:
            return
        schema = dict(component.parameters)
        schema["properties"] = {
            k: v for k, v in (schema.get("properties") or {}).items() if k not in drop
        }
        if "required" in schema:
            schema["required"] = [k for k in schema["required"] if k not in drop]
        component.parameters = schema

    return hide


def _force_headers(headers: dict[str, str]):
    """An httpx request hook putting the operator's headers on every API request. The tool
    only fills a header the request doesn't already carry, so a header parameter in the
    arguments would otherwise win over the configured credential."""

    async def force(request: httpx.Request) -> None:
        request.headers.update(headers)

    return force


def build_server(spec: dict, *, transport=None, api_transport=None) -> FastMCP:
    """A FastMCP server whose tools are the API's operations, from a bridge spec.

    ``transport`` / ``api_transport`` are httpx transports for the document fetch and the
    API calls respectively — test seams only; the bridge always uses the network.
    """
    url = spec["command"]
    headers = dict(spec.get("env") or {})
    args = list(spec.get("args") or [])
    try:
        raw = fetch_document(url, headers, transport=transport)
    except httpx.HTTPError as exc:
        raise DocumentError(f"can't fetch the OpenAPI document: {exc}") from exc
    doc = parse_document(raw)
    base_url = resolve_base_url(doc, url, args[0] if args else None)
    info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
    title = info.get("title")
    logger.info("serving OpenAPI document %r against %s", title or url, base_url)
    client = httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=TIMEOUT_S,
        transport=api_transport,
        event_hooks={"request": [_force_headers(headers)]},
    )
    try:
        return FastMCP.from_openapi(
            doc,
            client=client,
            name=spec.get("name") or title or "openapi",
            mcp_component_fn=_configured_header_hider(headers),
            # Real APIs drift from their own documents (an extra field, a nullable one that
            # isn't declared so); validating responses against the declared schema would
            # turn a working call into a failure. Results are still structured JSON.
            validate_output=False,
        )
    except ValueError as exc:  # FastMCP's own "Invalid OpenAPI specification: …"
        raise DocumentError(str(exc)) from exc
