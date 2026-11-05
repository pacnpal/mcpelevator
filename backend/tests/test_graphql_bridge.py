"""graphql runner, bridge side: a GraphQL schema translated into MCP tools.

A real graphql-core schema with resolvers stands in for the API, behind mock transports
(httpx for loading the schema, httpx2 for the calls), so these exercise the actual path —
schema load → tools → a validated operation → an HTTP request the API executes.
"""

from __future__ import annotations

import json

import httpx
import httpx2
import pytest
from fastmcp import Client
from graphql import build_schema, get_introspection_query, graphql_sync

from app.bridge import graphql as gql
from app.bridge.host import build_app, build_proxy

ENDPOINT = "https://api.example/graphql"

SDL = '''
"A person"
type User {
  id: ID!
  name: String
  role: Role
  friends(first: Int = 2): UserConnection
  posts: [Post!]!
  secret(token: String!): String
  legacy: String @deprecated(reason: "gone")
}
type UserConnection { totalCount: Int edges: [UserEdge] pageInfo: PageInfo }
type UserEdge { cursor: String node: User }
type PageInfo { hasNextPage: Boolean }
type Post { title: String author: User }
union Item = User | Post
"A new user"
input NewUser { name: String! tags: [String!] parent: NewUser role: Role = MEMBER }
input Lookup @oneOf { id: ID name: String }
enum Role { ADMIN MEMBER }
scalar DateTime
type Query {
  "Look up a user"
  user(id: ID!): User
  find(by: Lookup!): User
  search(q: String!, role: Role = MEMBER, limit: Int = 10): [Item]
  version: String
  now(at: DateTime): String
  graphql_query: String
}
type Mutation {
  createUser(input: NewUser!): User
  user(id: ID!): Boolean
}
type Subscription { ticks: Int }
'''

SCHEMA = build_schema(SDL)
USERS = {"1": {"id": "1", "name": "Ada", "role": "ADMIN", "posts": [{"title": "hi"}]}}


def _resolve(obj, info, **kwargs):
    value = obj.get(info.field_name) if isinstance(obj, dict) else None
    return value(**kwargs) if callable(value) else value


def _root():
    return {
        "user": lambda id: USERS.get(str(id)),
        "find": lambda by: USERS["1"],
        "version": "1.0",
        "now": lambda at=None: "2026-11-05",
        "search": lambda q, role, limit: [dict(USERS["1"], __typename="User")],
    }


def _mutation_root():
    return {
        "createUser": lambda input: {"id": "2", "name": input["name"], "posts": []},
        "user": lambda id: True,
    }


class FakeAPI:
    """The GraphQL API: executes each POSTed operation against :data:`SCHEMA`."""

    def __init__(self, *, introspection=True, status=200, raw=None):
        self.seen: list[tuple[dict, dict]] = []
        self.introspection = introspection
        self.status = status
        self.raw = raw

    def _execute(self, body: dict) -> dict:
        query = body["query"]
        if not self.introspection and "__schema" in query:
            return {"errors": [{"message": "GraphQL introspection is not allowed"}]}
        is_mutation = query.lstrip().startswith("mutation")
        result = graphql_sync(
            SCHEMA,
            query,
            root_value=_mutation_root() if is_mutation else _root(),
            variable_values=body.get("variables"),
            operation_name=body.get("operationName"),
            field_resolver=_resolve,
            type_resolver=lambda value, info, type_: value.get("__typename", "User"),
        )
        return result.formatted

    def sync(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.seen.append((dict(request.headers), body))
        return httpx.Response(self.status, json=self._execute(body))

    def async_(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.seen.append((dict(request.headers), body))
        if self.raw is not None:
            return httpx2.Response(self.status, text=self.raw)
        return httpx2.Response(self.status, json=self._execute(body))


def _server(api: FakeAPI | None = None, **spec):
    api = api or FakeAPI()
    server = gql.build_server(
        {"command": ENDPOINT, "env": {"Authorization": "Bearer k1"}, "document": SDL.encode(), **spec},
        api_transport=httpx2.MockTransport(api.async_),
    )
    return server, api


# --- schema loading ------------------------------------------------------------------ #


def test_parse_schema_takes_sdl_and_introspection_results():
    introspection = graphql_sync(SCHEMA, get_introspection_query(one_of=True)).data
    for raw in (SDL, json.dumps({"data": introspection}), json.dumps(introspection)):
        schema = gql.parse_schema(raw)
        assert set(schema.query_type.fields) >= {"user", "search"}
        assert schema.get_type("Lookup").is_one_of


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("type Query { x: Nope }", "not a valid GraphQL schema"),
        ("type Query {", "not a valid GraphQL schema"),
        ('{"openapi": "3.1.0"}', "must be an introspection result"),
        ("{ not json", "looks like JSON but isn't"),
        ("type Foo { x: Int } schema { query: Foo }", None),  # a non-standard root name is fine
        ("scalar X", "Query root type must be provided"),
        (b"\xff\xfe\x00", "isn't UTF-8"),
    ],
)
def test_parse_schema_refuses_what_it_cant_serve(raw, message):
    if message is None:
        assert gql.parse_schema(raw).query_type.name == "Foo"
        return
    with pytest.raises(gql.SchemaError, match=message):
        gql.parse_schema(raw)


def test_parse_schema_caps_its_size(monkeypatch):
    monkeypatch.setattr(gql, "MAX_SCHEMA_BYTES", 100)
    with pytest.raises(gql.SchemaError, match="larger than"):
        gql.parse_schema("type Query { " + "a: Int " * 50 + "}")


def test_introspection_asks_the_endpoint_with_its_headers():
    api = FakeAPI()
    raw = gql.introspect(ENDPOINT, {"Authorization": "Bearer k1"}, transport=httpx.MockTransport(api.sync))
    assert "user" in gql.parse_schema(raw).query_type.fields
    headers, body = api.seen[0]
    assert headers["authorization"] == "Bearer k1"
    assert body["operationName"] == "IntrospectionQuery"


def test_introspection_falls_back_for_servers_without_one_of():
    """A server older than @oneOf refuses `isOneOf` as an unknown field (with a 400); the
    classic introspection query still works there."""
    api = FakeAPI()
    asked = []

    def older(request):
        query = json.loads(request.content)["query"]
        asked.append("isOneOf" in query)
        if "isOneOf" in query:
            return httpx.Response(
                400, json={"errors": [{"message": 'Cannot query field "isOneOf" on type "__Type".'}]}
            )
        return api.sync(request)

    raw = gql.introspect(ENDPOINT, {}, transport=httpx.MockTransport(older))
    assert "user" in gql.parse_schema(raw).query_type.fields
    assert asked == [True, False]


def test_introspection_turned_off_says_what_to_do_instead():
    api = FakeAPI(introspection=False)
    with pytest.raises(gql.SchemaError, match="introspection is not allowed.*paste its schema"):
        gql.introspect(ENDPOINT, {}, transport=httpx.MockTransport(api.sync))


def test_introspection_http_error_is_a_schema_error():
    def denied(request):
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(gql.SchemaError, match="HTTP 401"):
        gql.introspect(ENDPOINT, {}, transport=httpx.MockTransport(denied))


def test_introspection_404_points_at_the_endpoint():
    def missing(request):
        return httpx.Response(404, text="Not found")

    with pytest.raises(gql.SchemaError, match="HTTP 404.*check that this is the API's GraphQL endpoint"):
        gql.introspect(ENDPOINT, {}, transport=httpx.MockTransport(missing))


def test_introspection_follows_no_redirect():
    def moved(request):
        return httpx.Response(302, headers={"location": "https://elsewhere.example/graphql"})

    with pytest.raises(gql.SchemaError, match="HTTP 302"):
        gql.introspect(ENDPOINT, {"Authorization": "Bearer k1"}, transport=httpx.MockTransport(moved))


def test_schema_url_is_fetched_without_credentials():
    seen = []

    def serve(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, text=SDL)

    raw = gql.fetch_schema_url(
        "https://docs.example/schema.graphql", ENDPOINT, {"Authorization": "Bearer k1"},
        transport=httpx.MockTransport(serve),
    )
    assert b"type Query" in raw
    assert "authorization" not in seen[0]


def test_schema_url_off_the_endpoint_origin_never_gets_credentials():
    seen = []

    def denied(request):
        seen.append(dict(request.headers))
        return httpx.Response(401)

    with pytest.raises(gql.SchemaError, match="only sent to the GraphQL endpoint's own host"):
        gql.fetch_schema_url(
            "https://docs.example/schema.graphql", ENDPOINT, {"Authorization": "Bearer k1"},
            transport=httpx.MockTransport(denied),
        )
    assert all("authorization" not in h for h in seen)


def test_schema_url_on_the_endpoint_origin_is_retried_with_credentials():
    def guarded(request):
        if request.headers.get("authorization") == "Bearer k1":
            return httpx.Response(200, text=SDL)
        return httpx.Response(401)

    raw = gql.fetch_schema_url(
        "https://api.example/schema.graphql", ENDPOINT, {"Authorization": "Bearer k1"},
        transport=httpx.MockTransport(guarded),
    )
    assert b"type Query" in raw


def test_load_schema_prefers_stored_then_url_then_introspection():
    api = FakeAPI()

    def serve(request):
        if request.method == "GET":
            return httpx.Response(200, text=SDL)
        return api.sync(request)

    transport = httpx.MockTransport(serve)
    _, source = gql.load_schema({"command": ENDPOINT, "document": SDL.encode()}, transport=transport)
    assert source == "the stored schema"
    _, source = gql.load_schema(
        {"command": ENDPOINT, "args": ["https://docs.example/s.graphql"]}, transport=transport
    )
    assert source == "https://docs.example/s.graphql"
    _, source = gql.load_schema({"command": ENDPOINT}, transport=transport)
    assert source == "introspection"


# --- tools --------------------------------------------------------------------------- #


async def test_fields_become_tools_with_their_argument_schemas():
    server, _ = _server()
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    # Every query and mutation field, a mutation sharing a query's name prefixed, the raw
    # tool moved aside by a field already named graphql_query, no subscription.
    assert set(tools) == {
        "user", "find", "search", "version", "now", "graphql_query",
        "createUser", "mutation_user", "_graphql_query",
    }
    user = tools["user"]
    assert user.description.startswith("Look up a user")
    assert user.input_schema["required"] == ["id"]
    assert user.input_schema["properties"]["id"] == {"type": ["string", "integer"]}
    assert "selection_set" in user.input_schema["properties"]
    assert user.annotations.readOnlyHint is True
    assert tools["createUser"].annotations.readOnlyHint is False

    search = tools["search"].input_schema["properties"]
    assert search["role"] == {"type": "string", "enum": ["ADMIN", "MEMBER"], "default": "MEMBER"}
    assert search["limit"] == {"type": "integer", "default": 10}

    create = tools["createUser"].input_schema
    assert create["properties"]["input"] == {"$ref": "#/$defs/NewUser"}
    new_user = create["$defs"]["NewUser"]
    assert new_user["required"] == ["name"]
    assert new_user["properties"]["parent"] == {"$ref": "#/$defs/NewUser"}  # recursive, finite
    assert new_user["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert new_user["additionalProperties"] is False

    # Used once, so FastMCP inlines it; either way it's an exactly-one-property object.
    find = tools["find"].input_schema
    lookup = find["properties"]["by"]
    if "$ref" in lookup:
        lookup = find["$defs"][lookup["$ref"].rsplit("/", 1)[1]]
    assert (lookup["minProperties"], lookup["maxProperties"]) == (1, 1)  # @oneOf

    assert tools["now"].input_schema["properties"]["at"]["description"].startswith("GraphQL scalar DateTime")
    # A scalar result has nothing to select.
    assert "selection_set" not in tools["version"].input_schema["properties"]


def test_default_selection_walks_connections_and_skips_what_it_cant_ask_for():
    selection = gql.default_selection(SCHEMA.query_type.fields["user"])
    assert selection.startswith("{ id name role friends {")
    # Relay plumbing doesn't use up the depth: the connection's nodes show their fields.
    assert "friends { totalCount edges { cursor node { id name role } } }" in selection
    assert "secret" not in selection  # needs an argument
    assert "legacy" not in selection  # deprecated
    union = gql.default_selection(SCHEMA.query_type.fields["search"])
    assert union.startswith("{ __typename ... on User {") and "... on Post {" in union


def test_default_selection_drops_a_level_when_too_wide(monkeypatch):
    monkeypatch.setattr(gql, "MAX_SELECTION_FIELDS", 5)
    selection = gql.default_selection(SCHEMA.query_type.fields["user"])
    assert "friends" not in selection and selection.startswith("{ id name role")


async def test_calls_send_arguments_as_variables_with_the_configured_headers():
    server, api = _server()
    async with Client(server) as client:
        result = await client.call_tool("user", {"id": "1"})
    assert result.structured_content["name"] == "Ada"
    headers, body = api.seen[-1]
    assert headers["authorization"] == "Bearer k1"
    assert "application/graphql-response+json" in headers["accept"]
    assert body["variables"] == {"id": "1"}
    assert body["operationName"] == "user"
    assert body["query"].startswith("query user($id: ID!) { user(id: $id) {")
    assert '"1"' not in body["query"]  # never spliced into the text


async def test_argument_values_cannot_change_the_operation():
    server, api = _server()
    hostile = '1") { id } secret(token: "x'
    async with Client(server) as client:
        result = await client.call_tool("user", {"id": hostile})
    assert result.structured_content == {"result": None}
    assert api.seen[-1][1]["variables"] == {"id": hostile}
    assert "secret" not in api.seen[-1][1]["query"]


async def test_selection_set_override_and_mutations():
    server, api = _server()
    async with Client(server) as client:
        picked = await client.call_tool("user", {"id": "1", "selection_set": "name posts { title }"})
        created = await client.call_tool(
            "createUser", {"input": {"name": "Bo"}, "selection_set": "{ id name }"}
        )
        listed = await client.call_tool("search", {"q": "a"})
        scalar = await client.call_tool("version", {})
    assert picked.structured_content == {"name": "Ada", "posts": [{"title": "hi"}]}
    assert created.structured_content == {"id": "2", "name": "Bo"}
    assert api.seen[-3][1]["query"].startswith("mutation createUser($input: NewUser!)")
    assert listed.structured_content["result"][0]["__typename"] == "User"
    assert scalar.structured_content == {"result": "1.0"}


async def test_invalid_operations_are_refused_before_reaching_the_api():
    server, api = _server()
    async with Client(server) as client:
        bad = await client.call_tool("user", {"id": "1", "selection_set": "{ nope }"}, raise_on_error=False)
        unknown = await client.call_tool("user", {"id": "1", "extra": 1}, raise_on_error=False)
        scalar = await client.call_tool("version", {"selection_set": "{ x }"}, raise_on_error=False)
    assert bad.is_error and "Cannot query field 'nope' on type 'User'" in bad.content[0].text
    assert unknown.is_error and "unknown arguments: extra" in unknown.content[0].text
    assert scalar.is_error
    assert api.seen == []


async def test_raw_query_tool_runs_any_operation():
    server, api = _server()
    async with Client(server) as client:
        result = await client.call_tool(
            "_graphql_query",
            {"query": "query Q($i: ID!) { a: user(id: $i) { name } version }", "variables": {"i": "1"}},
        )
        subscription = await client.call_tool(
            "_graphql_query", {"query": "subscription { ticks }"}, raise_on_error=False
        )
        two = await client.call_tool(
            "_graphql_query", {"query": "query A { version } query B { version }"}, raise_on_error=False
        )
    assert result.structured_content == {"a": {"name": "Ada"}, "version": "1.0"}
    assert subscription.is_error and "subscriptions aren't supported" in subscription.content[0].text
    assert two.is_error and "operation_name" in two.content[0].text
    assert len(api.seen) == 1


async def test_graphql_errors_become_tool_errors():
    class Failing(FakeAPI):
        def _execute(self, body):
            return {"data": {"user": None}, "errors": [{"message": "not allowed"}]}

    class Partial(FakeAPI):
        def _execute(self, body):
            return {"data": {"user": {"name": "Ada"}}, "errors": [{"message": "posts unavailable"}]}

    server, _ = _server(Failing())
    async with Client(server) as client:
        failed = await client.call_tool("user", {"id": "1"}, raise_on_error=False)
    assert failed.is_error and "not allowed" in failed.content[0].text

    server, _ = _server(Partial())
    async with Client(server) as client:
        partial = await client.call_tool("user", {"id": "1"})
    assert partial.structured_content == {
        "data": {"name": "Ada"}, "errors": [{"message": "posts unavailable"}]
    }


async def test_http_errors_without_a_graphql_body_are_tool_errors():
    server, _ = _server(FakeAPI(status=502, raw="<html>bad gateway</html>"))
    async with Client(server) as client:
        result = await client.call_tool("version", {}, raise_on_error=False)
    assert result.is_error and "HTTP 502" in result.content[0].text


async def test_caller_request_headers_never_reach_the_api():
    from fastmcp.server.dependencies import _current_http_request
    from starlette.requests import Request as StarletteRequest

    server, api = _server()
    caller = StarletteRequest({
        "type": "http", "method": "POST", "path": "/mcp", "query_string": b"",
        "headers": [(b"authorization", b"Bearer caller"), (b"x-caller-secret", b"s3cret")],
    })
    token = _current_http_request.set(caller)
    try:
        async with Client(server) as client:
            await client.call_tool("version", {})
    finally:
        _current_http_request.reset(token)
    sent = api.seen[-1][0]
    assert sent["authorization"] == "Bearer k1"
    assert "x-caller-secret" not in sent


# --- the bridge host ------------------------------------------------------------------ #


async def test_tool_policy_applies_to_translated_tools(monkeypatch):
    api = FakeAPI()
    real = gql.build_server
    monkeypatch.setattr(
        gql, "build_server",
        lambda spec: real(spec, api_transport=httpx2.MockTransport(api.async_)),
    )
    proxy = build_proxy({
        "command": ENDPOINT, "transport": "graphql", "document": SDL.encode(), "env": {},
        "disabled_tools": ["createUser"],
        "tool_overrides": {"user": {"name": "get_user", "description": "Fetch a user."}},
    })
    async with Client(proxy) as client:
        names = {t.name for t in await client.list_tools()}
        result = await client.call_tool("get_user", {"id": "1", "selection_set": "{ name }"})
    assert "createUser" not in names and "get_user" in names and "user" not in names
    assert result.structured_content == {"name": "Ada"}


def test_rest_mirror_serves_translated_tools(monkeypatch):
    from fastapi.testclient import TestClient

    api = FakeAPI()
    real = gql.build_server
    monkeypatch.setattr(
        gql, "build_server",
        lambda spec: real(spec, api_transport=httpx2.MockTransport(api.async_)),
    )
    spec = {"command": ENDPOINT, "transport": "graphql", "document": SDL.encode(), "env": {},
            "name": "gql", "mcp_http": True, "rest_openapi": True}
    app = build_app(spec, build_proxy(spec))
    with TestClient(app) as client:
        response = client.post("/rest/user", json={"id": "1", "selection_set": "{ name }"})
    assert response.status_code == 200, response.text
    assert response.json()["structured_content"] == {"name": "Ada"}


def test_bridge_exits_with_one_line_on_a_bad_schema(monkeypatch):
    from app.bridge import host

    def bad(spec):
        raise gql.SchemaError("introspection answered HTTP 401")

    monkeypatch.setattr(gql, "build_server", bad)
    monkeypatch.setenv("MCPE_BRIDGE_SPEC", json.dumps({"command": ENDPOINT, "transport": "graphql"}))
    monkeypatch.setenv("MCPE_BRIDGE_PORT", "0")
    with pytest.raises(SystemExit) as exited:
        host.main()
    assert str(exited.value.code) == "graphql: introspection answered HTTP 401"
