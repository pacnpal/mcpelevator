"""GraphQL → MCP translation for the ``graphql`` runner (see app.runners.graphql).

Like the ``openapi`` runner there is no MCP upstream: at startup the bridge loads the API's
GraphQL schema and builds a FastMCP server from it, and :func:`app.bridge.host.build_proxy`
puts the same tool-policy transform on it as on every proxy, so hidden tools, renames, the
REST mirror and the group hub behave exactly as they do for an MCP upstream.

The schema comes from, in order: the stored document (SDL or an introspection result,
pasted or uploaded), a schema URL (``args[0]``), or introspection against the endpoint.
Each field of the ``Query`` and ``Mutation`` types becomes one tool:

- its arguments become the tool's JSON Schema (input objects as ``$defs``, enums as
  string enums, ``@oneOf`` inputs as exactly-one-property objects);
- the call is sent as a GraphQL operation whose arguments are *variables*, never text
  spliced into the query, so no argument value can change the operation;
- the result's fields come from a generated selection set (scalars, plus nested objects
  a couple of levels down, Relay connections walked through to their nodes), and a
  ``selection_set`` argument lets the caller ask for exactly the fields it wants.

One more tool, ``graphql_query``, runs any operation the caller writes, for what the
per-field tools can't express (aliases, several fields in one round trip, fragments).
Every operation is validated against the schema before it's sent, so a bad selection or
query comes back as the validation error rather than an API round trip. Subscriptions
need a long-lived transport MCP tools don't have, so they aren't served.

The schema is read once per activation (a restart re-reads it). Anything wrong with it
raises :class:`SchemaError` out of :func:`build_server`, and the bridge exits with that one
line in the server's log.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
import httpx2
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool, ToolResult
from graphql import (
    FieldNode,
    GraphQLEnumType,
    GraphQLError,
    GraphQLField,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLUnionType,
    build_client_schema,
    build_schema,
    get_introspection_query,
    get_named_type,
    get_operation_ast,
    is_leaf_type,
    is_required_argument,
    is_required_input_field,
    parse,
    validate,
    validate_schema,
    value_from_ast_untyped,
)
from graphql.pyutils import Undefined
from mcp.types import ToolAnnotations

logger = logging.getLogger(__name__)


class SchemaError(ValueError):
    """The GraphQL schema can't be served: unreachable, unparseable or invalid. The bridge
    reports it as one log line and exits (see app.bridge.host.main)."""


# How long one request to the API (and the schema fetch) may take.
TIMEOUT_S = 30.0
# Ceiling on a schema's size, SDL or introspection JSON. GitHub's introspection result is
# ~10 MB; the bridge parses it in memory, so an endpoint streaming without end mustn't
# exhaust it.
MAX_SCHEMA_BYTES = 32 * 1024 * 1024
# How deep the generated selection set follows object fields below the operation's own
# field, and how many fields it may hold in all before it drops a level. Relay connection
# plumbing (``edges`` / ``node`` / ``nodes``) doesn't count against the depth.
SELECTION_DEPTH = 2
MAX_SELECTION_FIELDS = 300
# The raw-operation tool's name (an API field with the same name keeps it; this one moves).
RAW_TOOL = "graphql_query"
# The argument that overrides a tool's selection set (``_selection_set`` if an API field
# already takes an argument by that name).
SELECTION_ARG = "selection_set"
ACCEPT = "application/graphql-response+json, application/json;q=0.9"


# --- loading the schema --------------------------------------------------------------- #


def _read_capped(response: httpx.Response, what: str) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_SCHEMA_BYTES:
            raise SchemaError(f"{what} is larger than {MAX_SCHEMA_BYTES // (1024 * 1024)} MiB")
    return bytes(body)


def _origin(url) -> tuple[str, str, int | None]:
    url = httpx2.URL(str(url))
    return url.scheme, url.host, url.port


def parse_schema(raw: bytes | str) -> GraphQLSchema:
    """A schema from SDL text or an introspection result (``{"data": {"__schema": …}}``,
    or the ``{"__schema": …}`` inside it), refusing one that isn't a valid schema."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise SchemaError("the GraphQL schema isn't UTF-8 text") from None
    else:
        text = raw
    if len(text.encode()) > MAX_SCHEMA_BYTES:
        raise SchemaError(
            f"the GraphQL schema is larger than {MAX_SCHEMA_BYTES // (1024 * 1024)} MiB"
        )
    stripped = text.lstrip()
    try:
        if stripped.startswith("{"):
            try:
                loaded = json.loads(text)
            except ValueError as exc:
                raise SchemaError(f"the GraphQL schema looks like JSON but isn't: {exc}") from None
            if isinstance(loaded, dict) and isinstance(loaded.get("data"), dict):
                loaded = loaded["data"]
            if not isinstance(loaded, dict) or "__schema" not in loaded:
                raise SchemaError(
                    "a JSON GraphQL schema must be an introspection result "
                    '({"data": {"__schema": …}} or {"__schema": …})'
                )
            schema = build_client_schema(loaded, assume_valid=False)
        else:
            schema = build_schema(text)
    except SchemaError:
        raise
    except (GraphQLError, TypeError, KeyError, ValueError) as exc:
        raise SchemaError(f"not a valid GraphQL schema: {exc}") from None
    except RecursionError:
        raise SchemaError("the GraphQL schema is nested too deeply") from None
    errors = validate_schema(schema)
    if errors:
        raise SchemaError(
            "not a valid GraphQL schema: " + "; ".join(e.message for e in errors[:5])
        )
    if not _root_fields(schema):
        raise SchemaError("the GraphQL schema has no query or mutation fields to serve")
    return schema


def introspect(endpoint: str, headers: dict[str, str], *, transport=None) -> bytes:
    """Ask the API itself for its schema (an introspection query, with the configured
    headers: the endpoint is the API, so its credentials belong there).

    The query asks for ``isOneOf`` (``@oneOf`` input objects), which servers older than
    that addition reject as an unknown field; any failure is retried once with the classic
    query, and the second answer is the one reported."""
    try:
        return _introspect(endpoint, headers, one_of=True, transport=transport)
    except SchemaError:
        return _introspect(endpoint, headers, one_of=False, transport=transport)


def _introspect(endpoint: str, headers: dict[str, str], *, one_of: bool, transport=None) -> bytes:
    body = {
        "query": get_introspection_query(descriptions=True, one_of=one_of),
        "operationName": "IntrospectionQuery",
    }
    with httpx.Client(timeout=TIMEOUT_S, transport=transport) as client:
        try:
            with client.stream(
                "POST",
                endpoint,
                json=body,
                headers={**headers, "Accept": ACCEPT},
                follow_redirects=False,
            ) as response:
                raw = _read_capped(response, "the introspection result")
                status = response.status_code
        except httpx.HTTPError as exc:
            raise SchemaError(f"can't reach the GraphQL endpoint: {exc}") from exc
    try:
        result = json.loads(raw)
    except ValueError:
        result = None
    if isinstance(result, dict) and isinstance(result.get("data"), dict) and "__schema" in result["data"]:
        return raw
    detail = _error_messages(result) if isinstance(result, dict) else ""
    hint = (
        "check that this is the API's GraphQL endpoint"
        if status in (404, 405)
        else "the API may have introspection turned off, or need headers it wasn't sent; "
        "paste its schema (SDL) or give a schema URL instead"
    )
    if status >= 400 or not detail:
        raise SchemaError(f"introspection answered HTTP {status}{': ' + detail if detail else ''}; {hint}")
    raise SchemaError(f"introspection failed: {detail}; {hint}")


def fetch_schema_url(
    url: str, endpoint: str, headers: dict[str, str], *, transport=None
) -> bytes:
    """Download a schema (SDL or introspection JSON) published at ``url``.

    Fetched without credentials first, like an OpenAPI document: the configured headers
    are the API's, not the schema host's. A 401/403 is retried with them only when the
    schema is served from the endpoint's own origin, which the operator typed, and without
    following a redirect, so the credentials reach exactly that URL."""
    with httpx.Client(timeout=TIMEOUT_S, transport=transport) as client:
        try:
            with client.stream("GET", url, follow_redirects=True) as response:
                denied = response.status_code in (401, 403)
                if not (denied and headers):
                    response.raise_for_status()
                    return _read_capped(response, "the GraphQL schema")
            if _origin(url) != _origin(endpoint):
                raise SchemaError(
                    f"the schema URL answered {response.status_code}; the configured headers "
                    "are only sent to the GraphQL endpoint's own host"
                )
            with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                response.raise_for_status()
                return _read_capped(response, "the GraphQL schema")
        except httpx.HTTPError as exc:
            raise SchemaError(f"can't fetch the GraphQL schema: {exc}") from exc


def load_schema(spec: dict, *, transport=None) -> tuple[GraphQLSchema, str]:
    """The schema a bridge spec points at, and where it came from (for the log)."""
    endpoint = spec.get("command") or ""
    headers = dict(spec.get("env") or {})
    args = list(spec.get("args") or [])
    if spec.get("document") is not None:
        return parse_schema(spec["document"]), "the stored schema"
    if args:
        return parse_schema(fetch_schema_url(args[0], endpoint, headers, transport=transport)), args[0]
    return parse_schema(introspect(endpoint, headers, transport=transport)), "introspection"


# --- schema → tool shapes ------------------------------------------------------------- #


def _root_fields(schema: GraphQLSchema) -> list[tuple[str, str, GraphQLField]]:
    """Every servable root field: ``(operation, field name, field)``, queries first."""
    out = []
    for operation, root in (("query", schema.query_type), ("mutation", schema.mutation_type)):
        if root is not None:
            out.extend((operation, name, field) for name, field in root.fields.items())
    return out


_SCALARS = {
    "Int": {"type": "integer"},
    "Float": {"type": "number"},
    "String": {"type": "string"},
    "Boolean": {"type": "boolean"},
    # ID serializes as a string but accepts an integer on input.
    "ID": {"type": ["string", "integer"]},
}


def _default_of(definition) -> Any:
    """A JSON-safe default for an argument or input field, or ``Undefined``."""
    default = getattr(definition, "default", None)
    if default is None:
        return Undefined
    value = (
        value_from_ast_untyped(default.literal) if default.literal is not None else default.value
    )
    if value is Undefined:
        return Undefined
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return Undefined
    return value


def _with_doc(schema: dict, definition) -> dict:
    description = getattr(definition, "description", None)
    if description:
        schema = {**schema, "description": description}
    default = _default_of(definition)
    if default is not Undefined:
        schema = {**schema, "default": default}
    if getattr(definition, "deprecation_reason", None):
        schema = {**schema, "deprecated": True}
    return schema


def _nullable(schema: dict) -> dict:
    """``schema`` that also admits ``null``: a nullable GraphQL input distinguishes an
    explicit null (e.g. "clear this value") from leaving it out, so null is a real value."""
    if "$ref" in schema:
        return {"anyOf": [schema, {"type": "null"}]}
    if "type" not in schema:
        return schema  # any JSON value already includes null
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    out = {**schema, "type": [*types, "null"]}
    if "enum" in out:
        out["enum"] = [*out["enum"], None]
    return out


def input_schema(type_, defs: dict[str, dict]) -> dict:
    """The JSON Schema of a GraphQL input type: nullable unless it's non-null. Input objects
    land once in ``defs`` and are referenced from there, so a recursive input type stays
    finite."""
    if isinstance(type_, GraphQLNonNull):
        return _input_schema(type_.of_type, defs)
    return _nullable(_input_schema(type_, defs))


def _input_schema(type_, defs: dict[str, dict]) -> dict:
    if isinstance(type_, GraphQLList):
        return {"type": "array", "items": input_schema(type_.of_type, defs)}
    if isinstance(type_, GraphQLScalarType):
        known = _SCALARS.get(type_.name)
        if known is not None:
            return dict(known)
        note = f"GraphQL scalar {type_.name}"
        if type_.description:
            note += f": {type_.description}"
        return {"description": note}
    if isinstance(type_, GraphQLEnumType):
        return {"type": "string", "enum": list(type_.values)}
    if isinstance(type_, GraphQLInputObjectType):
        if type_.name not in defs:
            defs[type_.name] = {}  # placeholder first: the fields may refer back to it
            properties = {
                name: _with_doc(input_schema(field.type, defs), field)
                for name, field in type_.fields.items()
            }
            definition: dict[str, Any] = {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            }
            required = [n for n, f in type_.fields.items() if is_required_input_field(f)]
            if required:
                definition["required"] = required
            if getattr(type_, "is_one_of", False):
                # @oneOf: exactly one field, and not null.
                definition["minProperties"] = 1
                definition["maxProperties"] = 1
            if type_.description:
                definition["description"] = type_.description
            defs[type_.name] = definition
        return {"$ref": f"#/$defs/{type_.name}"}
    return {}


def _is_connection_plumbing(parent, field_name: str) -> bool:
    """Relay connection fields that only lead to the data (edges → node, or nodes): walking
    through them shouldn't use up the depth, or a connection would show no node fields."""
    parent_name = getattr(parent, "name", "")
    if parent_name.endswith("Connection") and field_name in ("edges", "nodes"):
        return True
    return parent_name.endswith("Edge") and field_name == "node"


def _union_conflicts(union: GraphQLUnionType) -> set[str]:
    """Field names the union's members declare with different types. Selected side by side
    under one response name they would fail GraphQL's overlapping-fields rule (``A.value:
    Int`` and ``B.value: String``), so the generated selection aliases them per member."""
    seen: dict[str, set[str]] = {}
    for member in union.types:
        for name, field in member.fields.items():
            seen.setdefault(name, set()).add(str(field.type))
    return {name for name, types in seen.items() if len(types) > 1}


def selection_for(
    type_, depth: int, counter: list[int], *, conflicts: frozenset[str] = frozenset()
) -> str:
    """The generated selection set for an output type (empty for a leaf). ``conflicts``
    names fields to alias as ``<name>_<Type>`` (see :func:`_union_conflicts`)."""
    named = get_named_type(type_)
    if is_leaf_type(named):
        return ""
    if isinstance(named, GraphQLUnionType):
        clashing = frozenset(_union_conflicts(named))
        parts = ["__typename"]
        for member in named.types:
            sub = selection_for(member, depth, counter, conflicts=clashing)
            if sub:
                parts.append(f"... on {member.name} {sub}")
        return "{ " + " ".join(parts) + " }"
    if not isinstance(named, (GraphQLObjectType, GraphQLInterfaceType)):
        return ""
    parts: list[str] = []
    for name, field in named.fields.items():
        if name.startswith("__") or field.deprecation_reason:
            continue
        # A field that can't be asked for without an argument we can't guess is left out.
        if any(is_required_argument(arg) for arg in field.args.values()):
            continue
        child = get_named_type(field.type)
        selected = f"{name}_{named.name}: {name}" if name in conflicts else name
        if is_leaf_type(child):
            parts.append(selected)
            counter[0] += 1
            continue
        next_depth = depth if _is_connection_plumbing(named, name) else depth - 1
        if next_depth <= 0:
            continue
        sub = selection_for(field.type, next_depth, counter)
        if sub:
            parts.append(f"{selected} {sub}")
            counter[0] += 1
    if isinstance(named, GraphQLInterfaceType) or not parts:
        parts.append("__typename")
    return "{ " + " ".join(parts) + " }"


def _candidate_selections(field: GraphQLField):
    """Selection sets to try, best first: as deep as :data:`SELECTION_DEPTH` allows within
    :data:`MAX_SELECTION_FIELDS`, then shallower, then just the type name."""
    for depth in range(SELECTION_DEPTH, 0, -1):
        counter = [0]
        selection = selection_for(field.type, depth, counter)
        if counter[0] <= MAX_SELECTION_FIELDS or depth == 1:
            yield selection
    yield "{ __typename }" if selection else ""


def operation_text(operation: str, field_name: str, field: GraphQLField, args, selection: str) -> str:
    """``<operation> <field>($a: T, …) { <field>(a: $a, …) <selection> }``: the operation a
    field's tool sends, for the arguments named in ``args`` (each passed as a variable)."""
    definitions = ", ".join(f"${name}: {field.args[name].type}" for name in args)
    call = ", ".join(f"{name}: ${name}" for name in args)
    text = f"{operation} {field_name}"
    if definitions:
        text += f"({definitions})"
    text += " { " + field_name
    if call:
        text += f"({call})"
    if selection:
        text += " " + selection
    return text + " }"


def default_selection(
    field: GraphQLField, schema: GraphQLSchema | None = None, operation: str = "query",
    field_name: str = "",
) -> str:
    """The selection set a field's tool asks for when the caller doesn't choose one.

    With ``schema``, each candidate is validated in the operation it will be sent in, and
    the first valid one wins, so a schema shape the generator doesn't foresee costs depth
    rather than every call to the tool."""
    candidates = _candidate_selections(field)
    if schema is None:
        return next(candidates)
    for selection in candidates:
        text = operation_text(operation, field_name, field, list(field.args), selection)
        if not validate(schema, parse(text)):
            return selection
        logger.warning("GraphQL field %r: generated selection set invalid, trying a smaller one", field_name)
    return selection


def _type_label(type_) -> str:
    return str(type_)


def _error_messages(result: dict) -> str:
    errors = result.get("errors")
    if not isinstance(errors, list):
        return ""
    messages = []
    for error in errors[:5]:
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            messages.append(error["message"])
        elif isinstance(error, str):
            messages.append(error)
    return "; ".join(messages)


# --- executing ----------------------------------------------------------------------- #


class _Executor:
    """Sends operations to the endpoint: always that URL, with the configured headers and
    none of the caller's, and never following a redirect elsewhere."""

    def __init__(self, endpoint: str, headers: dict[str, str], schema: GraphQLSchema, transport=None):
        self.endpoint = endpoint
        self.schema = schema
        self.client = httpx2.AsyncClient(
            headers={**headers, "Accept": ACCEPT},
            timeout=TIMEOUT_S,
            transport=transport,
            follow_redirects=False,
        )

    def check(self, query: str, operation_name: str | None = None):
        """Parse and validate ``query`` against the schema: the document, and the operation
        to run."""
        try:
            document = parse(query)
        except GraphQLError as exc:
            raise ToolError(f"GraphQL syntax error: {exc.message}") from None
        errors = validate(self.schema, document)
        if errors:
            raise ToolError(
                "the operation doesn't match the API's schema: "
                + "; ".join(e.message for e in errors[:5])
            )
        operation = get_operation_ast(document, operation_name)
        if operation is None:
            raise ToolError(
                "name the operation to run (operation_name)"
                if operation_name is None
                else f"no operation named {operation_name!r} in the query"
            )
        if operation.operation.value == "subscription":
            raise ToolError("subscriptions aren't supported; use a query or mutation")
        return document, operation

    async def run(self, query: str, variables: dict | None, operation_name: str | None) -> dict:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        if operation_name:
            payload["operationName"] = operation_name
        try:
            response = await self.client.post(self.endpoint, json=payload)
        except httpx2.HTTPError as exc:
            raise ToolError(f"can't reach the GraphQL API: {exc}") from exc
        try:
            result = response.json()
        except ValueError:
            result = None
        # GraphQL over HTTP answers request errors with a 4xx and a GraphQL body, so a
        # body with data/errors is read whatever the status.
        if not isinstance(result, dict) or not ({"data", "errors"} & result.keys()):
            raise ToolError(
                f"the GraphQL API answered HTTP {response.status_code}: {response.text[:500]}"
            )
        return result


def _structured(value: Any, errors: Any = None) -> ToolResult:
    if errors:
        return ToolResult(structured_content={"data": value, "errors": errors})
    if isinstance(value, dict):
        return ToolResult(structured_content=value)
    return ToolResult(
        structured_content={"result": value}, meta={"fastmcp": {"wrap_result": True}}
    )


class GraphQLFieldTool(Tool):
    """One root field (a query or a mutation), called with its arguments as variables."""

    def __init__(
        self,
        executor: _Executor,
        operation: str,
        field_name: str,
        field: GraphQLField,
        *,
        name: str,
    ):
        defs: dict[str, dict] = {}
        properties = {
            arg_name: _with_doc(input_schema(arg.type, defs), arg)
            for arg_name, arg in field.args.items()
        }
        required = [n for n, a in field.args.items() if is_required_argument(a)]
        selection = default_selection(field, executor.schema, operation, field_name)
        selection_arg = SELECTION_ARG if SELECTION_ARG not in field.args else f"_{SELECTION_ARG}"
        if selection:
            preview = selection if len(selection) <= 400 else selection[:400] + " …"
            properties[selection_arg] = {
                "type": "string",
                "description": (
                    "GraphQL selection set naming the fields to return, e.g. "
                    "`{ id name }`. Omit for the default: " + preview
                ),
            }
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        if defs:
            parameters["$defs"] = defs
        description = field.description or f"GraphQL {operation} `{field_name}`."
        description += f"\n\nReturns `{_type_label(field.type)}`."
        if field.deprecation_reason:
            description += f"\n\nDeprecated: {field.deprecation_reason}"
        super().__init__(
            name=name,
            description=description,
            parameters=parameters,
            annotations=ToolAnnotations(
                readOnlyHint=operation == "query",
                destructiveHint=False if operation == "query" else None,
            ),
        )
        self._executor = executor
        self._operation = operation
        self._field_name = field_name
        self._field = field
        self._selection = selection
        self._selection_arg = selection_arg

    def operation_text(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """The operation for a call, and its variables. Only the arguments given are sent,
        so the API applies its own defaults to the rest; one given as null is sent as null,
        which GraphQL tells apart from leaving it out (clearing a value, say)."""
        selection = arguments.get(self._selection_arg)
        if selection is None or not str(selection).strip():
            selection = self._selection
        elif self._selection:
            selection = str(selection).strip()
            if not selection.startswith("{"):
                selection = "{ " + selection + " }"
        else:
            raise ToolError(f"`{self._field_name}` returns a scalar; it takes no selection set")
        variables = {name: value for name, value in arguments.items() if name in self._field.args}
        text = operation_text(self._operation, self._field_name, self._field, variables, selection)
        return text, variables

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        unknown = set(arguments) - set(self._field.args) - {self._selection_arg}
        if unknown:
            raise ToolError(
                f"unknown arguments: {', '.join(sorted(unknown))}; "
                f"accepted: {', '.join(sorted([*self._field.args, self._selection_arg])) or 'none'}"
            )
        query, variables = self.operation_text(arguments)
        document, operation = self._executor.check(query, self._field_name)
        # The selection set is the caller's text: it must stay inside this field's braces.
        # One that closes them early ("{ id } deleteAll") would add root fields of its own,
        # running a mutation the operator hid as part of this one, so the parsed operation
        # has to be exactly this tool's field and nothing else.
        roots = operation.selection_set.selections
        if (
            len(document.definitions) != 1
            or len(roots) != 1
            or not isinstance(roots[0], FieldNode)
            or roots[0].name.value != self._field_name
            or roots[0].alias is not None
        ):
            raise ToolError(
                f"`{self._selection_arg}` must be one selection set for `{self._field_name}`'s "
                "result, e.g. `{ id name }`; use graphql_query for anything else"
            )
        result = await self._executor.run(query, variables, self._field_name)
        data = result.get("data")
        value = data.get(self._field_name) if isinstance(data, dict) else None
        errors = result.get("errors")
        if errors and value is None:
            raise ToolError(_error_messages(result) or json.dumps(errors)[:1000])
        return _structured(value, errors)


class GraphQLQueryTool(Tool):
    """Any operation the caller writes: aliases, several fields at once, fragments."""

    def __init__(self, executor: _Executor, *, name: str, schema_summary: str):
        super().__init__(
            name=name,
            description=(
                "Run a GraphQL query or mutation against this API and return its `data`. "
                "Use it for what the per-field tools can't express: several fields in one "
                "request, aliases, fragments. The operation is checked against the API's "
                "schema first. Pass values through `variables` rather than writing them "
                f"into the query.\n\n{schema_summary}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The GraphQL document."},
                    "variables": {
                        "type": "object",
                        "description": "Values for the operation's variables.",
                    },
                    "operation_name": {
                        "type": "string",
                        "description": "Which operation to run, when the document has several.",
                    },
                },
                "required": ["query"],
            },
            annotations=ToolAnnotations(readOnlyHint=False),
        )
        self._executor = executor

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("`query` is required")
        variables = arguments.get("variables") or None
        if variables is not None and not isinstance(variables, dict):
            raise ToolError("`variables` must be an object")
        operation_name = arguments.get("operation_name") or None
        self._executor.check(query, operation_name)
        result = await self._executor.run(query, variables, operation_name)
        data = result.get("data")
        errors = result.get("errors")
        if errors and data is None:
            raise ToolError(_error_messages(result) or json.dumps(errors)[:1000])
        return _structured(data, errors)


_TOOL_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _schema_summary(schema: GraphQLSchema) -> str:
    def names(root):
        return ", ".join(sorted(root.fields)) if root is not None else ""

    lines = []
    if schema.query_type is not None:
        lines.append(f"Query fields: {names(schema.query_type)}")
    if schema.mutation_type is not None:
        lines.append(f"Mutation fields: {names(schema.mutation_type)}")
    text = "\n".join(lines)
    return text if len(text) <= 2000 else text[:2000] + " …"


def build_tools(schema: GraphQLSchema, executor: _Executor) -> list[Tool]:
    """One tool per query and mutation field, plus :data:`RAW_TOOL`. A mutation sharing a
    query's name is served as ``mutation_<name>``."""
    tools: list[Tool] = []
    taken: set[str] = set()
    for operation, field_name, field in _root_fields(schema):
        name = _TOOL_NAME.sub("_", field_name)
        if name in taken:
            name = f"{operation}_{name}"
        while name in taken:
            name += "_"
        taken.add(name)
        tools.append(GraphQLFieldTool(executor, operation, field_name, field, name=name))
    raw = RAW_TOOL
    while raw in taken:
        raw = "_" + raw
    tools.append(GraphQLQueryTool(executor, name=raw, schema_summary=_schema_summary(schema)))
    return tools


def build_server(spec: dict, *, transport=None, api_transport=None) -> FastMCP:
    """A FastMCP server whose tools are the API's root fields, from a bridge spec.

    ``transport`` / ``api_transport`` are httpx / httpx2 transports for loading the schema
    and for the API calls — test seams only; the bridge always uses the network."""
    endpoint = spec.get("command") or ""
    headers = dict(spec.get("env") or {})
    schema, source = load_schema(spec, transport=transport)
    executor = _Executor(endpoint, headers, schema, transport=api_transport)
    tools = build_tools(schema, executor)
    skipped = len(schema.subscription_type.fields) if schema.subscription_type else 0
    logger.info(
        "serving %d GraphQL field(s) from %s against %s%s",
        len(tools) - 1,
        source,
        endpoint,
        f" ({skipped} subscription field(s) skipped)" if skipped else "",
    )
    server = FastMCP(name=spec.get("name") or "graphql")
    for tool in tools:
        server.add_tool(tool)
    return server
